"""End-to-end pipeline test on the committed stratified fixture.

The fixture (tests/fixtures/sample_listings.jsonl, 73 rows) is built by
``scripts/build_fixture.py`` to exhibit every defect class the phase plan
defines. Running the full pipeline against it offline is the CI-proof that
Stages 1–7 hold together without network, database, or the raw dataset.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path

import pytest

from src.etl.normalizer import normalize_listing
from src.etl.pipeline import run_pipeline

FIXTURE = Path(__file__).parents[2] / "fixtures" / "sample_listings.jsonl"


@pytest.fixture(scope="module")
def fixture_rows() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.open(encoding="utf-8")]


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> dict:
    out = tmp_path_factory.mktemp("processed")
    return run_pipeline(FIXTURE, out, checkpoints=False, strict_flags=False)


class TestEndToEnd:
    def test_no_dead_letters(self, run: dict):
        assert run["manifest"]["dead_letters"] == 0

    def test_all_rows_survive(self, run: dict, fixture_rows: list[dict]):
        assert run["manifest"]["listings"] == len(fixture_rows)

    def test_platform_split(self, run: dict):
        counts = run["manifest"]["platform_counts"]
        assert counts == {"nawy": 39, "property_finder": 34}

    def test_dedup_produced_properties(self, run: dict):
        assert 0 < run["manifest"]["properties"] <= run["manifest"]["listings"]

    def test_market_stats_populated(self, run: dict):
        assert len(run["market_stats"]) > 0
        for stat in run["market_stats"]:
            assert stat["stat_month"] == "2026-11-01"
            assert stat["listing_count"] >= 1

    def test_flag_counts_recorded(self, run: dict):
        assert "source_tier_card" in run["flag_counts"]
        assert run["flag_counts"]["source_tier_card"] == 15
        assert run["flag_counts"].get("currency_converted", 0) == 3

    def test_artifacts_written(self, run: dict):
        manifest = run["manifest"]
        assert manifest["raw_sha256"]
        assert manifest["crawl_window"]["min"] >= "2026-11-02"
        assert manifest["crawl_window"]["max"] <= "2026-11-15"
        assert manifest["usd_to_egp_rate"] == "53.81"


class TestNormalizerOnRealRows:
    def test_usd_row_converted_downstream(self, fixture_rows: list[dict]):
        from src.etl.financials import apply_financial_rules

        usd_rows = [r for r in fixture_rows if r.get("currency") == "USD"]
        assert usd_rows
        for raw in usd_rows[:1]:
            listing = normalize_listing(raw)
            assert listing.currency == "USD"
            converted = apply_financial_rules(listing)
            assert converted.price_egp > listing.price_egp
            assert "currency_converted" in converted.flags

    def test_nawy_governorate_derived(self, fixture_rows: list[dict]):
        nawy_rows = [r for r in fixture_rows if r["platform"] == "Nawy"]
        assert nawy_rows
        for raw in nawy_rows:
            listing = normalize_listing(raw)
            assert listing.governorate  # 100% coverage invariant

    def test_card_tier_flagged(self, fixture_rows: list[dict]):
        card_rows = [
            r for r in fixture_rows
            if r["platform"] == "PropertyFinder" and not r.get("description")
        ]
        assert card_rows
        listing = normalize_listing(card_rows[0])
        assert "source_tier_card" in listing.flags
        assert listing.description_normalized is None

    def test_contract_resale_flagged(self, fixture_rows: list[dict]):
        rows = [
            r for r in fixture_rows
            if r["platform"] == "Nawy" and r.get("resale")
            and r.get("completion_status") == "ready"
            and (r.get("delivery_year") or 0) > 2026
        ]
        assert rows
        listing = normalize_listing(rows[0])
        assert listing.completion_status == "off_plan"
        assert "completion_conflict" in listing.flags
        assert listing.resale is True

    def test_ivilla_marker_preserved(self, fixture_rows: list[dict]):
        rows = [r for r in fixture_rows if r.get("property_type") == "iVilla"]
        assert rows
        listing = normalize_listing(rows[0])
        assert listing.property_type == "villa"
        assert "ivilla" in listing.features.get("markers", [])

    def test_compound_whitespace_key_normalized(self, fixture_rows: list[dict]):
        rows = [
            r for r in fixture_rows
            if isinstance(r.get("compound"), str) and r["compound"] != r["compound"].strip()
        ]
        assert rows
        listing = normalize_listing(rows[0])
        assert listing.compound_key == listing.compound_key.strip()
        assert " " * 2 not in listing.compound_key

    def test_price_per_m2_computed_for_nawy(self, fixture_rows: list[dict]):
        from src.etl.financials import apply_financial_rules

        rows = [r for r in fixture_rows if r["platform"] == "Nawy" and r.get("size_m2")]
        listing = apply_financial_rules(normalize_listing(rows[0]))
        assert listing.price_per_m2 == (
            (listing.price_egp / Decimal(str(rows[0]["size_m2"]))).quantize(Decimal("0.01"))
        )

    def test_content_hash_unique_per_listing(self, fixture_rows: list[dict]):
        hashes = {normalize_listing(r).content_hash for r in fixture_rows}
        assert len(hashes) == len(fixture_rows)
