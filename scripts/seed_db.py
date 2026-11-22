#!/usr/bin/env python3
"""Stage 8 — load the processed artifacts into PostgreSQL (Neon dev).

Reads ``data/processed/`` (07_merged.jsonl, properties.jsonl,
compound_registry.json, market_stats.json) and writes:

* ``locations``         — get-or-create on (governorate, city, compound);
* ``scraped_listings``  — upsert on (platform, source_id); ``content_hash``
                          makes re-seeding a no-op for unchanged content;
* ``financials``        — upsert on listing_id (1:1);
* ``properties``        — rebuilt each run (deterministic grouping), then
                          listings are relinked via ``property_id``;
* ``market_stats``      — upsert on (location_id, property_type, stat_month).

Idempotency contract: running twice leaves the same logical state —
listings/financials/locations/stats upsert, properties are regenerated from
the same deterministic drafts. Exits non-zero on any constraint violation.

Usage::

    DATABASE_URL=postgres+asyncpg://... python scripts/seed_db.py [--dry-run]

``--dry-run`` validates every artifact and prints the planned row counts per
table without touching the database (CI never runs this script).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import Counter
from decimal import Decimal
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from geoalchemy2 import WKTElement

PROCESSED = Path("data/processed")


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _polygon_wkt(polygon: dict[str, Any] | None) -> str | None:
    """GeoJSON Polygon → WKT POLYGON (exterior ring only)."""
    if not polygon or polygon.get("type") != "Polygon":
        return None
    rings = polygon.get("coordinates") or []
    if not rings:
        return None
    points = [f"{float(p[0])} {float(p[1])}" for p in rings[0] if len(p) >= 2]
    if len(points) < 4:
        return None
    return f"POLYGON(({', '.join(points)}))"


def _point_wkt(lng: float | None, lat: float | None) -> str | None:
    if lng is None or lat is None:
        return None
    return f"POINT({lng} {lat})"


def build_seed_plan(processed: Path) -> dict[str, Any]:
    """Validate artifacts and build the full in-memory seed plan."""
    listings = _load_jsonl(processed / "07_merged.jsonl")
    properties = _load_jsonl(processed / "properties.jsonl")
    registry = _load_json(processed / "compound_registry.json")
    market_stats = _load_json(processed / "market_stats.json")

    # City display names: most common raw city per canonical key.
    city_display: dict[str, str] = {}
    city_votes: dict[str, Counter[str]] = {}
    for listing in listings:
        votes = city_votes.setdefault(listing["city_canonical"], Counter())
        votes[listing["city"]] += 1
    for canonical, votes in city_votes.items():
        city_display[canonical] = votes.most_common(1)[0][0]

    # Locations: compound rows from the registry + city rows for the rest.
    from src.etl.geo import governorate_for_city

    locations: list[dict[str, Any]] = []
    location_index: dict[str, int] = {}
    for entry in registry:
        key = ("compound", entry["city_canonical"], entry["compound_key"])
        location_index[key[1] + "/" + key[2]] = len(locations)
        locations.append(
            {
                "governorate": governorate_for_city(entry["city_canonical"]),
                "city": city_display.get(entry["city_canonical"], entry["city_canonical"]),
                "compound": entry["display_name"],
                "name_normalized": entry["compound_key"],
                "boundary_wkt": _polygon_wkt(entry.get("boundary")),
                "centroid": entry.get("centroid"),
            }
        )
    for canonical, display in city_display.items():
        location_index.setdefault(canonical, len(locations))
        locations.append(
            {
                "governorate": governorate_for_city(canonical),
                "city": display,
                "compound": None,
                "name_normalized": canonical,
                "boundary_wkt": None,
                "centroid": None,
            }
        )

    def location_id_for(listing: dict[str, Any]) -> int:
        if listing.get("compound_key"):
            return location_index[listing["city_canonical"] + "/" + listing["compound_key"]]
        return location_index[listing["city_canonical"]]

    for listing in listings:
        listing["_location_id"] = location_id_for(listing)
    for stat in market_stats:
        scope = stat["scope"]
        if scope != stat["city_canonical"]:
            stat["_location_id"] = location_index[stat["city_canonical"] + "/" + scope]
        else:
            stat["_location_id"] = location_index[scope]

    return {
        "listings": listings,
        "properties": properties,
        "locations": locations,
        "market_stats": market_stats,
        "city_display": city_display,
    }


def validate_plan(plan: dict[str, Any]) -> list[str]:
    """Cheap artifact sanity checks before touching the database."""
    errors: list[str] = []
    listing_ids = {(row["platform"], row["source_id"]) for row in plan["listings"]}
    if len(listing_ids) != len(plan["listings"]):
        errors.append("duplicate (platform, source_id) in 07_merged.jsonl")
    property_ids = {p["property_id"] for p in plan["properties"]}
    for listing in plan["listings"]:
        if listing.get("property_id") not in property_ids:
            errors.append(
                f"listing {listing['platform']}/{listing['source_id']} references "
                f"missing property {listing.get('property_id')}"
            )
    for listing in plan["listings"]:
        if not listing.get("content_hash"):
            errors.append(f"listing {listing['source_id']} missing content_hash")
    return errors


async def seed(plan: dict[str, Any], database_url: str) -> None:
    from sqlalchemy import bindparam, delete, select, update
    from sqlalchemy.dialects.postgresql import insert as pg_insert
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from src.db.models import Financial, Location, MarketStat, Property, ScrapedListing

    engine = create_async_engine(database_url, pool_pre_ping=True)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    chunk = 500

    async with Session() as session:
        async with session.begin():
            # --- locations (get-or-create) -------------------------------------
            location_rows: dict[str, int] = {}
            for entry in plan["locations"]:
                stmt = (
                    pg_insert(Location)
                    .values(
                        governorate=entry["governorate"],
                        city=entry["city"],
                        compound=entry["compound"],
                        name_normalized=entry["name_normalized"],
                        boundary=(
                            WKTElement(entry["boundary_wkt"], srid=4326)
                            if entry["boundary_wkt"]
                            else None
                        ),
                        geom=(
                            WKTElement(_point_wkt(*entry["centroid"]) or "", srid=4326)
                            if entry.get("centroid")
                            else None
                        ),
                    )
                    .on_conflict_do_nothing(constraint="uq_locations_geo_hierarchy")
                    .returning(Location.id)
                )
                result = await session.execute(stmt)
                row_id = result.scalar_one_or_none()
                if row_id is None:
                    row_id = (
                        await session.execute(
                            select(Location.id).where(
                                Location.governorate == entry["governorate"],
                                Location.city == entry["city"],
                                Location.compound == entry["compound"],
                            )
                        )
                    ).scalar_one()
                location_rows[
                    f"{entry['governorate']}|{entry['city']}|{entry['compound']}"
                ] = row_id
            await session.flush()

            # --- listings upsert (idempotent on platform+source_id) ------------
            listing_values = [
                {
                    "platform": listing["platform"],
                    "source_id": listing["source_id"],
                    "source_url": listing["source_url"],
                    "listing_ref": listing.get("listing_ref"),
                    "broker_name": listing.get("broker_name"),
                    "agent_name": listing.get("agent_name"),
                    "listed_date": _iso(listing.get("listed_date")),
                    "title_raw": listing["title_raw"],
                    "title_normalized": listing["title_normalized"],
                    "content_hash": listing["content_hash"],
                }
                for listing in plan["listings"]
            ]
            for start in range(0, len(listing_values), chunk):
                batch = listing_values[start : start + chunk]
                stmt = pg_insert(ScrapedListing).values(batch)
                await session.execute(
                    stmt.on_conflict_do_update(
                        constraint="uq_scraped_listings_source_id",
                        set_={
                            "content_hash": stmt.excluded.content_hash,
                            "listing_ref": stmt.excluded.listing_ref,
                            "broker_name": stmt.excluded.broker_name,
                            "agent_name": stmt.excluded.agent_name,
                            "listed_date": stmt.excluded.listed_date,
                            "title_normalized": stmt.excluded.title_normalized,
                        },
                    )
                )

            # one round trip: (platform, source_id) -> database listing id
            db_listing_ids = {
                (platform, source_id): db_id
                for platform, source_id, db_id in (
                    await session.execute(
                        select(
                            ScrapedListing.platform,
                            ScrapedListing.source_id,
                            ScrapedListing.id,
                        )
                    )
                ).all()
            }

            # --- properties: deterministic rebuild ------------------------------
            await session.execute(update(ScrapedListing).values(property_id=None))
            await session.execute(delete(MarketStat))
            await session.execute(delete(Property))

            property_row_ids: dict[int, int] = {}
            for draft in plan["properties"]:
                location_entry = _location_entry_for_draft(plan, draft)
                location_key = (
                    f"{location_entry['governorate']}|{location_entry['city']}|"
                    f"{location_entry['compound']}"
                )
                prop = Property(
                    property_type=draft["property_type"],
                    size_m2=_dec(draft.get("size_m2")),
                    plot_size_m2=_dec(draft.get("plot_size_m2")),
                    bedrooms=draft.get("bedrooms"),
                    bathrooms=draft.get("bathrooms"),
                    finishing=draft.get("finishing"),
                    furnished=draft.get("furnished"),
                    delivery_year=draft.get("delivery_year"),
                    completion_status=draft.get("completion_status"),
                    resale=draft.get("resale"),
                    developer_name=draft.get("developer_name"),
                    location_id=location_rows[location_key],
                    geom=(
                        WKTElement(
                            _point_wkt(draft.get("longitude"), draft.get("latitude")) or "",
                            srid=4326,
                        )
                        if draft.get("latitude") is not None
                        else None
                    ),
                    title_normalized=draft["title_normalized"],
                    description_normalized=draft.get("description_normalized"),
                    features=draft.get("features") or {},
                    image_urls=draft.get("image_urls") or [],
                    floor_plan_urls=draft.get("floor_plan_urls") or [],
                    master_plan_url=draft.get("master_plan_url"),
                    price_egp=_dec(draft.get("price_egp")),
                )
                session.add(prop)
                await session.flush()
                property_row_ids[draft["property_id"]] = prop.id

            # --- relink listings to their properties (executemany) --------------
            await session.execute(
                update(ScrapedListing)
                .where(
                    ScrapedListing.platform == bindparam("plat"),
                    ScrapedListing.source_id == bindparam("sid"),
                )
                .values(property_id=bindparam("pid")),
                [
                    {
                        "plat": listing["platform"],
                        "sid": listing["source_id"],
                        "pid": property_row_ids[listing["property_id"]],
                    }
                    for listing in plan["listings"]
                ],
            )

            # --- financials upsert (1:1 with listings) --------------------------
            financial_values = []
            for listing in plan["listings"]:
                financial_values.append(
                    {
                        "listing_id": db_listing_ids[(listing["platform"], listing["source_id"])],
                        "payment_scheme": listing.get("payment_scheme"),
                        "payment_frequency": listing.get("payment_frequency"),
                        "price_egp": _dec(listing["price_egp"]) or Decimal(0),
                        "price_per_m2": _dec(listing.get("price_per_m2")),
                        "down_payment_egp": _dec(listing.get("down_payment_egp")),
                        "installment_years": listing.get("installment_years"),
                        "monthly_payment_egp": _dec(listing.get("monthly_payment_egp")),
                        "currency": listing.get("currency", "EGP"),
                    }
                )
            for start in range(0, len(financial_values), chunk):
                batch = financial_values[start : start + chunk]
                stmt = pg_insert(Financial).values(batch)
                await session.execute(
                    stmt.on_conflict_do_update(
                        index_elements=["listing_id"],
                        set_={
                            "payment_scheme": stmt.excluded.payment_scheme,
                            "payment_frequency": stmt.excluded.payment_frequency,
                            "price_egp": stmt.excluded.price_egp,
                            "price_per_m2": stmt.excluded.price_per_m2,
                            "down_payment_egp": stmt.excluded.down_payment_egp,
                            "installment_years": stmt.excluded.installment_years,
                            "monthly_payment_egp": stmt.excluded.monthly_payment_egp,
                            "currency": stmt.excluded.currency,
                        },
                    )
                )

            # --- market_stats upsert --------------------------------------------
            stat_values = []
            for stat in plan["market_stats"]:
                location_entry = _location_entry_for_stat(plan, stat)
                location_key = (
                    f"{location_entry['governorate']}|{location_entry['city']}|"
                    f"{location_entry['compound']}"
                )
                stat_values.append(
                    {
                        "location_id": location_rows[location_key],
                        "property_type": stat["property_type"],
                        "stat_month": stat["stat_month"],
                        "avg_price_per_m2": _dec(stat["avg_price_per_m2"]),
                        "min_price_per_m2": _dec(stat["min_price_per_m2"]),
                        "max_price_per_m2": _dec(stat["max_price_per_m2"]),
                        "avg_size_m2": _dec(stat["avg_size_m2"]),
                        "listing_count": stat["listing_count"],
                    }
                )
            for start in range(0, len(stat_values), chunk):
                batch = stat_values[start : start + chunk]
                stmt = pg_insert(MarketStat).values(batch)
                await session.execute(
                    stmt.on_conflict_do_update(
                        constraint="uq_market_stats_slice",
                        set_={
                            "avg_price_per_m2": stmt.excluded.avg_price_per_m2,
                            "min_price_per_m2": stmt.excluded.min_price_per_m2,
                            "max_price_per_m2": stmt.excluded.max_price_per_m2,
                            "avg_size_m2": stmt.excluded.avg_size_m2,
                            "listing_count": stmt.excluded.listing_count,
                        },
                    )
                )

    await engine.dispose()


# --- small helpers ------------------------------------------------------------
def _iso(value: str | None) -> Any:
    if not value:
        return None
    from datetime import datetime

    return datetime.fromisoformat(str(value))


def _dec(value: Any) -> Decimal | None:
    if value is None:
        return None
    return Decimal(str(value))


def _location_entry_for_draft(plan: dict[str, Any], draft: dict[str, Any]) -> dict[str, Any]:
    """The registry/city location row a property draft belongs to."""
    compound_key = draft.get("compound_key")
    city_canonical = draft["city_canonical"]
    for entry in plan["locations"]:
        if compound_key:
            if entry["name_normalized"] == compound_key and entry["compound"] is not None:
                return entry
        elif entry["compound"] is None and entry["name_normalized"] == city_canonical:
            return entry
    raise KeyError(f"no location entry for draft {draft['property_id']}")


def _location_entry_for_stat(plan: dict[str, Any], stat: dict[str, Any]) -> dict[str, Any]:
    for entry in plan["locations"]:
        if entry["name_normalized"] == stat["scope"]:
            if stat["scope"] != stat["city_canonical"] and entry["compound"] is None:
                continue
            return entry
    raise KeyError(f"no location entry for stat {stat['scope']}/{stat['property_type']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--processed", type=Path, default=PROCESSED)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    plan = build_seed_plan(args.processed)
    errors = validate_plan(plan)
    if errors:
        for error in errors:
            print(f"VALIDATION ERROR: {error}", file=sys.stderr)
        return 1

    print(
        f"seed plan: {len(plan['locations'])} locations, "
        f"{len(plan['listings'])} listings, {len(plan['properties'])} properties, "
        f"{len(plan['market_stats'])} market_stat rows"
    )
    if args.dry_run:
        print("dry-run: database not touched")
        return 0

    from src.core.config import get_settings

    settings = get_settings()
    if not settings.database_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return 2
    asyncio.run(seed(plan, settings.async_database_url))
    print("seed complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
