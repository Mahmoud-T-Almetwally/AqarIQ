#!/usr/bin/env python3
"""Build the stratified CI fixture from the frozen raw snapshot.

Re-run whenever ``data/processed/manifest.json`` changes (new crawl): the
fixture is committed so CI exercises the full pipeline offline on rows that
really exhibit every defect class — rare property types, USD rows,
placeholder down payments, card-tier rows, relisting pairs, contract-resale
rows, Arabic-text hygiene cases and compound-name variants.

Deterministic: each category picks its rows by lowest source_id, so the
same snapshot always yields the same fixture.

Usage: python scripts/build_fixture.py [--raw scrappers/unified_output.jsonl]
                                        [--out tests/fixtures/sample_listings.jsonl]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.etl.arabic import RE_ARABIC, RE_ARABIC_DIGITS, RE_TASHKEEL_TATWEEL

FixtureRow = dict[str, Any]


def load_raw(path: Path) -> list[FixtureRow]:
    return [json.loads(line) for line in path.open(encoding="utf-8") if line.strip()]


def row_id(row: FixtureRow) -> tuple[str, str]:
    return (row["platform"], str(row["source_id"]))


def build_fixture(raw: list[FixtureRow]) -> list[FixtureRow]:
    picked: dict[tuple[str, str], FixtureRow] = {}

    def take(predicate: Callable[[FixtureRow], bool], n: int) -> None:
        matching = sorted((r for r in raw if predicate(r)), key=row_id)
        for row in matching[:n]:
            picked[row_id(row)] = row

    def platform(p: str) -> Callable[[FixtureRow], bool]:
        return lambda r: r["platform"] == p

    nawy = platform("Nawy")
    pf = platform("PropertyFinder")

    # --- rare property types (one or two each, both platforms) ---
    for value, n in [
        ("iVilla", 2), ("Palace", 1), ("Land", 1), ("Bulk Sale Unit", 1),
        ("Full Floor", 1), ("Hotel Apartment", 1), ("Whole Building", 1),
        ("Loft", 2), ("Family House", 1), ("Cabin", 2), ("Twin House", 2),
        ("Roof", 1), ("Penthouse", 2), ("Studio", 2),
    ]:
        take(lambda r, v=value: r.get("property_type") == v, n)

    # --- currency / payment artifacts ---
    take(lambda r: r.get("currency") == "USD", 3)
    take(lambda r: r.get("down_payment_egp") is not None and r["down_payment_egp"] <= 1000, 3)

    # --- reduced-detail PF tier ---
    take(lambda r: r["platform"] == "PropertyFinder" and not r.get("description"), 4)

    # --- finishing vocabulary coverage ---
    for value in (
        "Fully Finished", "Not Finished Core Shell", "Semi Finished",
        "Fully Finished Furnished", "Flexi Finishing", "Luxury", "Premium ",
        "White Box", "Ultra-luxury",
    ):
        take(lambda r, v=value: (r.get("finishing") or "").strip() == v.strip(), 1)
    take(lambda r: r.get("finishing") == "PARTLY", 1)
    take(lambda r: r.get("furnished") == "YES", 1)
    take(lambda r: r.get("furnished") == "NO", 1)

    # --- payment frequency variants ---
    for value in ("quarterly", "monthly", "semi-annually", "annually"):
        take(lambda r, v=value: r.get("payment_frequency") == v, 1)

    # --- completion status variants + contract resale ---
    for plat, value in (("Nawy", "off_plan_primary"), ("Nawy", "ready"),
                        ("PropertyFinder", "completed"), ("PropertyFinder", "off_plan"),
                        ("PropertyFinder", "off_plan_primary"), ("PropertyFinder", "completed_primary")):
        take(lambda r, p=plat, v=value: r["platform"] == p and r.get("completion_status") == v, 1)
    take(
        lambda r: r["platform"] == "Nawy" and r.get("resale")
        and r.get("completion_status") == "ready"
        and (r.get("delivery_year") or 0) > 2026,
        2,
    )

    # --- structural anomalies ---
    take(lambda r: r.get("size_m2") is None, 1)
    take(
        lambda r: r.get("plot_size_m2") is not None and r.get("size_m2")
        and r["plot_size_m2"] < r["size_m2"],
        2,
    )
    take(lambda r: r.get("property_type") == "Apartment" and r.get("bedrooms") == 0, 2)
    take(lambda r: r.get("installment_years") == 15, 1)

    # --- Arabic text hygiene cases ---
    take(lambda r: r.get("description") and RE_ARABIC_DIGITS.search(r["description"]), 2)
    take(lambda r: r.get("description") and RE_TASHKEEL_TATWEEL.search(r["description"]), 2)
    take(lambda r: r.get("description") and RE_ARABIC.search(r["description"] or "") and len(r.get("description") or "") > 80, 3)
    take(lambda r: r.get("description") and "contact details hidden" in (r["description"] or ""), 2)
    take(lambda r: r.get("description") and "#" in (r["description"] or ""), 2)

    # --- compound-name hygiene + price-suspect exemplar ---
    take(lambda r: (r.get("compound") or "") != (r.get("compound") or "").strip(), 2)
    take(lambda r: (r.get("compound") or "").lower().startswith("stella di mare"), 2)
    take(lambda r: str(r.get("source_id")) == "125117969", 1)  # Zed West price=down-payment

    # --- relisting duplicates (exact key within/across platforms) ---
    def exact_key(r: FixtureRow) -> tuple | None:
        if not r.get("size_m2") or r.get("bedrooms") is None:
            return None
        city = re.sub(r"\s+", " ", (r.get("city") or "").strip().lower())
        compound = re.sub(r"\s+", " ", (r.get("compound") or "").strip().lower())
        return (city, compound, r.get("property_type"), r["size_m2"], r["bedrooms"], r["price_egp"])

    by_key: dict[tuple, list[FixtureRow]] = defaultdict(list)
    for r in raw:
        key = exact_key(r)
        if key:
            by_key[key].append(r)
    same_platform_groups = [g for g in by_key.values() if len(g) > 1 and len({x["platform"] for x in g}) == 1]
    cross_groups = [g for g in by_key.values() if len(g) > 1 and len({x["platform"] for x in g}) > 1]
    for group in sorted(same_platform_groups, key=lambda g: row_id(g[0]))[:3] + sorted(
        cross_groups, key=lambda g: row_id(g[0])
    )[:4]:
        for row in group:
            picked[row_id(row)] = row

    # --- governors: plain coverage rows from each platform ---
    take(nawy, 2)
    take(pf, 2)

    return sorted(picked.values(), key=row_id)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--raw", type=Path, default=Path("scrappers/unified_output.jsonl"))
    parser.add_argument("--out", type=Path, default=Path("tests/fixtures/sample_listings.jsonl"))
    args = parser.parse_args()

    raw = load_raw(args.raw)
    fixture = build_fixture(raw)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("w", encoding="utf-8") as handle:
        for row in fixture:
            handle.write(json.dumps(row, ensure_ascii=False))
            handle.write("\n")

    platforms = {"Nawy": 0, "PropertyFinder": 0}
    for row in fixture:
        platforms[row["platform"]] += 1
    print(
        f"wrote {len(fixture)} fixture rows to {args.out} "
        f"(Nawy {platforms['Nawy']} / PropertyFinder {platforms['PropertyFinder']})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
