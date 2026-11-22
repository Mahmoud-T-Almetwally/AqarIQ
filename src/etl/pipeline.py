"""Batch orchestrator for the static-dataset pivot.

Local, idempotent, replayable: reads the frozen raw JSONL once and walks it
through Stages 1–7, checkpointing every stage under ``data/processed/`` so a
misfiring rule is a diff between two files, then computes market_stats and
the run manifest. The RabbitMQ consumer stays deferred — if local re-crawls
resume, the same normalizer and deduplicator run behind the queue.

Usage::

    python -m src.etl.pipeline --raw scrappers/unified_output.jsonl \
        --out data/processed [--llm] [--no-checkpoints] [--no-strict-flags]

Outputs (``--out`` directory):

* ``01_validated.jsonl.gz`` … ``06_enriched.jsonl.gz`` — stage checkpoints
* ``07_merged.jsonl``            — final listings carrying ``property_id``
* ``properties.jsonl``           — deduplicated property drafts
* ``compound_registry.json``     — (city, compound) registry with geometry
* ``market_stats.json``          — 2026-11 precompute (stats-excluded rows out)
* ``flags_summary.json`` / ``dedup_stats.json`` / ``dedup_review_queue.json``
* ``manifest.json``              — raw SHA-256, counts, crawl window, FX rate

``--strict-flags`` (default) fails the run when a flag count lands outside
its expected range (phase plan Appendix A) — out-of-range numbers are
pipeline bugs, not bad luck. Fixture runs pass ``--no-strict-flags``.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Iterator

from src.core.logging import get_logger
from src.etl.contracts import CleanListing
from src.etl.deduplicator import deduplicate
from src.etl.extractor import ExtractionCache, enrich_missing, run_llm_extraction
from src.etl.flags import EXPECTED_FLAG_COUNTS, STATS_EXCLUDED_FLAGS
from src.etl.financials import EGP_PER_USD, apply_financial_rules, detect_price_outliers
from src.etl.geo import CompoundRegistry
from src.etl.normalizer import stage1_validate, stage2_hygiene, stage3_canonical, stage4_geo
from src.etl.vocab import SNAPSHOT_YEAR

logger = get_logger(__name__)

SNAPSHOT_STAT_MONTH = date(2026, 11, 1)  # the crawl window lives entirely in 2026-11


def _iter_raw_lines(raw_path: Path) -> Iterator[dict[str, Any]]:
    with raw_path.open(encoding="utf-8") as handle:
        for line_no, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError as error:
                raise ValueError(f"malformed JSON on line {line_no}: {error}") from error


def _write_checkpoint(path: Path, records: list[dict[str, Any]]) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, default=str))
            handle.write("\n")


def _write_jsonl(path: Path, records: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, default=str))
            handle.write("\n")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=1, ensure_ascii=False, default=str), encoding="utf-8")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _flag_counts(listings: list[CleanListing]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for listing in listings:
        for flag in listing.flags:
            counts[flag] = counts.get(flag, 0) + 1
    return dict(sorted(counts.items()))


def _check_flag_counts(counts: dict[str, int]) -> list[str]:
    """Compare real counts against the expected ranges; return violations."""
    violations: list[str] = []
    for flag, (low, high) in EXPECTED_FLAG_COUNTS.items():
        actual = counts.get(flag, 0)
        if not low <= actual <= high:
            violations.append(f"{flag}: {actual} outside expected [{low}, {high}]")
    return violations


def _rederive_completion(listings: list[CleanListing]) -> list[CleanListing]:
    """Post-enrichment completion derivation.

    Affects zero rows in this snapshot (every status-bearing row keeps its
    portal status; card-tier rows have no text to enrich) but keeps the
    pipeline correct for future re-crawls: rows whose delivery year was just
    extracted and whose status is missing derive it from the year.
    """
    out: list[CleanListing] = []
    for listing in listings:
        extracted = listing.extraction.get("delivery_year")
        if extracted and listing.completion_status is None:
            year = int(extracted["value"])
            status = "completed_primary" if year <= SNAPSHOT_YEAR else "off_plan"
            out.append(listing.model_copy(update={"completion_status": status}))
        else:
            out.append(listing)
    return out


def _apply_extraction_cache(
    listings: list[CleanListing], cache: ExtractionCache
) -> list[CleanListing]:
    """Fill still-missing fields from a (possibly pre-existing) LLM cache."""
    out: list[CleanListing] = []
    for listing in listings:
        fields = cache.get(listing.text_hash)
        if not fields or not listing.description_normalized:
            out.append(listing)
            continue
        updates: dict[str, Any] = {}
        extraction = dict(listing.extraction)
        for key, value in fields.items():
            current = getattr(listing, key, None)
            if current is not None:
                continue
            if key == "down_payment_egp":
                parsed_value: Any = Decimal(value)
                stored_value: Any = str(parsed_value)
            elif isinstance(value, str) and value.isdigit():
                parsed_value = int(value)
                stored_value = parsed_value
            else:
                parsed_value = value
                stored_value = value
            updates[key] = parsed_value
            extraction[key] = {"value": stored_value, "source": "llm"}
        if listing.payment_scheme is None and (
            "down_payment_egp" in updates or "installment_years" in updates
        ):
            updates["payment_scheme"] = "installments"
            extraction["payment_scheme"] = {"value": "installments", "source": "llm"}
        if updates:
            updates["extraction"] = extraction
            out.append(listing.model_copy(update=updates))
        else:
            out.append(listing)
    return out


def _compute_market_stats(drafts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Precompute 2026-11 aggregates per (compound | city) × type, flagged rows out."""
    groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for draft in drafts:
        excluded = bool(set(draft.get("flags", [])) & set(STATS_EXCLUDED_FLAGS))
        if excluded or not draft.get("size_m2") or not draft.get("price_egp"):
            continue
        scope = draft.get("compound_key") or draft.get("city_canonical") or ""
        display = draft.get("compound") or draft.get("city") or ""
        key = (scope, draft["property_type"], display)
        groups.setdefault(key, []).append(draft)
    stats: list[dict[str, Any]] = []
    for (scope, property_type, display), members in sorted(groups.items()):
        ppm2 = [
            float(Decimal(m["price_egp"]) / Decimal(m["size_m2"]))
            for m in members
        ]
        sizes = [float(m["size_m2"]) for m in members]
        stats.append(
            {
                "city_canonical": members[0]["city_canonical"],
                "scope": scope,
                "display_name": display,
                "property_type": property_type,
                "stat_month": SNAPSHOT_STAT_MONTH.isoformat(),
                "avg_price_per_m2": round(sum(ppm2) / len(ppm2), 2),
                "min_price_per_m2": round(min(ppm2), 2),
                "max_price_per_m2": round(max(ppm2), 2),
                "avg_size_m2": round(sum(sizes) / len(sizes), 2),
                "listing_count": len(members),
            }
        )
    return stats


def run_pipeline(
    raw_path: Path,
    out_dir: Path,
    checkpoints: bool = True,
    strict_flags: bool = True,
    llm: bool = False,
) -> dict[str, Any]:
    """Execute Stages 1–9; returns the run statistics dictionary."""
    out_dir.mkdir(parents=True, exist_ok=True)
    registry = CompoundRegistry()
    dead_letters: list[dict[str, Any]] = []

    # --- Stages 1–4: per-row normalization -------------------------------------
    validated: list[dict[str, Any]] = []
    hygiene: list[dict[str, Any]] = []
    canonical: list[dict[str, Any]] = []
    listings: list[CleanListing] = []
    platform_counts: dict[str, int] = {}
    crawl_min: str | None = None
    crawl_max: str | None = None

    for raw_line in _iter_raw_lines(raw_path):
        try:
            rec_validated = stage1_validate(raw_line)
            rec_hygiene = stage2_hygiene(rec_validated)
            rec_canonical = stage3_canonical(rec_hygiene)
            listing = stage4_geo(rec_canonical, registry)
        except Exception as error:  # noqa: BLE001 - dead-letter, never crash the run
            dead_letters.append({"line": raw_line, "error": str(error)})
            continue
        validated.append(rec_validated.model_dump())
        hygiene.append(rec_hygiene)
        canonical.append(rec_canonical)
        listings.append(listing)
        platform_counts[listing.platform] = platform_counts.get(listing.platform, 0) + 1
        if listing.listed_date:
            iso = listing.listed_date.date().isoformat()
            crawl_min = iso if crawl_min is None else min(crawl_min, iso)
            crawl_max = iso if crawl_max is None else max(crawl_max, iso)

    logger.info(
        "stages_1_4_complete",
        listings=len(listings),
        dead_letters=len(dead_letters),
        compounds=len(registry),
    )
    registry.finalize()

    # --- centroid backfill for coordinate-less rows (registry pass 2) ----------
    backfilled = 0
    final_stage4: list[CleanListing] = []
    for listing in listings:
        if listing.latitude is None and listing.compound_key:
            centroid = registry.centroid(listing.city_canonical, listing.compound_key)
            if centroid:
                final_stage4.append(
                    listing.model_copy(update={"longitude": centroid[0], "latitude": centroid[1]})
                )
                backfilled += 1
                continue
        final_stage4.append(listing)
    listings = final_stage4
    logger.info("centroid_backfill", rows=backfilled)

    if checkpoints:
        _write_checkpoint(out_dir / "01_validated.jsonl.gz", validated)
        _write_checkpoint(out_dir / "02_hygiene.jsonl.gz", hygiene)
        _write_checkpoint(out_dir / "03_canonical.jsonl.gz", canonical)
        _write_checkpoint(
            out_dir / "04_located.jsonl.gz",
            [listing.model_dump(mode="json") for listing in listings],
        )

    # --- Stage 5: financial rules + outlier group pass --------------------------
    listings = [apply_financial_rules(listing) for listing in listings]
    listings = detect_price_outliers(listings)
    if checkpoints:
        _write_checkpoint(
            out_dir / "05_financial.jsonl.gz",
            [listing.model_dump(mode="json") for listing in listings],
        )

    # --- Stage 6: regex enrichment (+ optional LLM tier) ------------------------
    listings = [enrich_missing(listing) for listing in listings]
    if llm:
        listings = asyncio_run_llm(listings, out_dir)
    else:
        cache = ExtractionCache(out_dir / "extraction_cache.json")
        if len(cache):
            listings = _apply_extraction_cache(listings, cache)
    listings = _rederive_completion(listings)
    if checkpoints:
        _write_checkpoint(
            out_dir / "06_enriched.jsonl.gz",
            [listing.model_dump(mode="json") for listing in listings],
        )

    # --- Stage 7: entity resolution ---------------------------------------------
    result = deduplicate(listings)
    _write_jsonl(
        out_dir / "07_merged.jsonl",
        [listing.model_dump(mode="json") for listing in result.listings],
    )
    _write_jsonl(out_dir / "properties.jsonl", [d.to_json() for d in result.drafts])
    _write_json(out_dir / "compound_registry.json", registry.to_json())
    _write_json(out_dir / "dedup_stats.json", result.stats)
    _write_json(out_dir / "dedup_review_queue.json", result.review_queue)

    # --- Stage 8 artifacts: market_stats + manifest + flag summary --------------
    drafts_json = [d.to_json() for d in result.drafts]
    market_stats = _compute_market_stats(drafts_json)
    _write_json(out_dir / "market_stats.json", market_stats)

    flag_counts = _flag_counts(result.listings)
    _write_json(out_dir / "flags_summary.json", flag_counts)

    manifest = {
        "raw_file": str(raw_path),
        "raw_sha256": _sha256(raw_path),
        "rows": len(result.listings) + len(dead_letters),
        "listings": len(result.listings),
        "dead_letters": len(dead_letters),
        "platform_counts": platform_counts,
        "crawl_window": {"min": crawl_min, "max": crawl_max},
        "stat_month": SNAPSHOT_STAT_MONTH.isoformat(),
        "usd_to_egp_rate": str(EGP_PER_USD),
        "properties": len(result.drafts),
        "flag_counts": flag_counts,
        "expected_flag_ranges": {
            flag: list(bounds) for flag, bounds in EXPECTED_FLAG_COUNTS.items()
        },
    }
    _write_json(out_dir / "manifest.json", manifest)
    if dead_letters:
        _write_json(out_dir / "dead_letters.json", dead_letters)

    violations = _check_flag_counts(flag_counts) if strict_flags else []
    logger.info(
        "pipeline_complete",
        listings=len(result.listings),
        properties=len(result.drafts),
        merged=result.stats.get("merged_properties", 0),
        market_stat_rows=len(market_stats),
        flag_violations=violations,
    )
    if strict_flags and violations:
        for violation in violations:
            logger.error("flag_range_violation", violation=violation)
        raise SystemExit(1)
    return {
        "manifest": manifest,
        "stats": result.stats,
        "market_stats": market_stats,
        "flag_counts": flag_counts,
        "violations": violations,
    }


def asyncio_run_llm(listings: list[CleanListing], out_dir: Path) -> list[CleanListing]:
    """Run the Groq extraction tier (requires ``GROQ_API_KEY``), then apply.

    A rate-limit stop (daily quota, auth failure, persistent errors) is a
    *warning*, not a crash: everything extracted so far is already in the
    cache and applied below, and re-running the command resumes from where
    it stopped.
    """
    import asyncio

    from src.core.config import get_settings

    settings = get_settings()
    if not settings.groq_api_key:
        logger.error("llm_tier_skipped", reason="GROQ_API_KEY not set")
        raise SystemExit(2)
    result = asyncio.run(
        run_llm_extraction(listings, out_dir / "extraction_cache.json", settings)
    )
    logger.info(
        "llm_extraction_done",
        fills=result["fills"],
        batches=f"{result['batches_done']}/{result['batches_total']}",
        requests=result["requests"],
        rate_limit_waits=result["rate_limit_waits"],
        tpm_pace_waits=result["tpm_pace_waits"],
        models=result["models"],
    )
    if result["stopped_reason"]:
        logger.warning(
            "llm_tier_stopped_early",
            reason=result["stopped_reason"],
            hint="re-run the same command to resume from the cache",
        )
    cache = ExtractionCache(out_dir / "extraction_cache.json")
    return _apply_extraction_cache(listings, cache)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--raw", type=Path, default=Path("scrappers/unified_output.jsonl"),
        help="frozen raw JSONL snapshot (gitignored; see data/manifest.json)",
    )
    parser.add_argument(
        "--out", type=Path, default=Path("data/processed"),
        help="output directory for checkpoints and final artifacts",
    )
    parser.add_argument("--llm", action="store_true", help="run the Groq extraction tier")
    parser.add_argument("--no-checkpoints", action="store_true", help="skip stage checkpoints")
    parser.add_argument(
        "--no-strict-flags", action="store_true",
        help="skip the expected-flag-range assertion (fixture runs)",
    )
    args = parser.parse_args(argv)
    if not args.raw.exists():
        logger.error("raw_file_missing", path=str(args.raw))
        return 2
    run_pipeline(
        args.raw,
        args.out,
        checkpoints=not args.no_checkpoints,
        strict_flags=not args.no_strict_flags,
        llm=args.llm,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
