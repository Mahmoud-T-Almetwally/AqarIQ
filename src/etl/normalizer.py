"""Per-row orchestration of Stages 1–4 — :func:`normalize_listing`.

One entry point per raw JSONL line, composing the pure stage functions so
the pipeline can checkpoint between them:

* **Stage 1** (:func:`stage1_validate`) — Pydantic validation of the raw
  spider item (platform normalization, ``source_id`` coercion, ``Z``-suffixed
  timestamps).
* **Stage 2** (:func:`stage2_hygiene`) — :mod:`src.etl.arabic` over title,
  description and compound; name strips for broker/agent/developer.
* **Stage 3** (:func:`stage3_canonical`) — :mod:`src.etl.vocab` maps
  (type, finishing, completion status, payment scheme, amenities) and the
  ``furnished`` PARTLY policy.
* **Stage 4** (:func:`stage4_geo`) — :mod:`src.etl.geo` (city, governorate
  derivation, compound key, coordinate validation, registry collection) and
  assembly of the strict :class:`CleanListing`.

The 2,786 reduced-detail PropertyFinder rows (no description) receive
``source_tier_card`` here so every downstream stage can skip them cheaply.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from src.etl.arabic import normalize_name_key, normalize_text
from src.etl.contracts import CleanListing, RawListing, finalize_hashes
from src.etl.flags import Flag
from src.etl.geo import (
    CompoundRegistry,
    canonical_city,
    canonical_governorate,
    governorate_for_city,
    validate_coords,
)
from src.etl.vocab import (
    build_features,
    derive_completion_status,
    map_finishing,
    map_furnished,
    map_payment_frequency,
    map_payment_scheme,
    map_property_type,
)


def stage1_validate(raw_line: dict[str, Any]) -> RawListing:
    """Structural validation; raises on a malformed row (dead-letter upstream)."""
    return RawListing.model_validate(raw_line)


def stage2_hygiene(raw: RawListing) -> dict[str, Any]:
    """Text hygiene; output is a plain dict the later stages extend."""
    return {
        "platform": raw.platform,
        "source_id": raw.source_id,
        "source_url": raw.source_url,
        "listing_ref": (raw.listing_ref or None),
        "broker_name": (raw.broker_name or None),
        "agent_name": (raw.agent_name or None),
        "developer_name": (raw.developer_name or None),
        "listed_date": raw.listed_date,
        "title_raw": raw.title_raw,
        "title_normalized": normalize_text(raw.title_raw) or raw.title_raw.strip(),
        "description_normalized": normalize_text(raw.description),
        "property_type": raw.property_type,
        "size_m2": raw.size_m2,
        "plot_size_m2": raw.plot_size_m2,
        "bedrooms": raw.bedrooms,
        "bathrooms": raw.bathrooms,
        "finishing": raw.finishing,
        "furnished": raw.furnished,
        "delivery_year": raw.delivery_year,
        "completion_status": raw.completion_status,
        "resale": raw.resale,
        "has_nanny_room": raw.has_nanny_room,
        "features": [f for f in raw.features if isinstance(f, str)],
        "payment_methods": raw.payment_methods,
        "price_egp": raw.price_egp,
        "price_per_m2": raw.price_per_m2,
        "down_payment_egp": raw.down_payment_egp,
        "installment_years": raw.installment_years,
        "payment_frequency": raw.payment_frequency,
        "currency": raw.currency,
        "governorate": raw.governorate,
        "city": raw.city,
        "compound": (raw.compound.strip() if isinstance(raw.compound, str) else None) or None,
        "latitude": raw.latitude,
        "longitude": raw.longitude,
        "compound_geometry": raw.compound_geometry,
        "image_urls": raw.image_urls,
        "floor_plan_urls": raw.floor_plan_urls,
        "master_plan_url": raw.master_plan_url,
        "flags": [],
    }


def stage3_canonical(rec: dict[str, Any]) -> dict[str, Any]:
    """Canonical vocabularies + completion-status derivation."""
    property_type, marker = map_property_type(rec["property_type"])
    furnished, furnished_markers = map_furnished(rec["furnished"])
    markers = [m for m in (marker, *furnished_markers) if m]
    completion, completion_flags = derive_completion_status(
        rec["platform"], rec["completion_status"], rec["resale"], rec["delivery_year"]
    )
    rec = dict(rec)
    rec["property_type"] = property_type
    rec["finishing"] = map_finishing(rec["finishing"])
    rec["furnished"] = furnished
    rec["completion_status"] = completion
    rec["payment_scheme"] = map_payment_scheme(rec["payment_methods"])
    rec["payment_frequency"] = map_payment_frequency(rec["payment_frequency"])
    rec["features"] = build_features(rec["features"], rec["has_nanny_room"], markers)
    rec["flags"] = [*rec["flags"], *completion_flags]
    del rec["payment_methods"]
    del rec["has_nanny_room"]
    return rec


def stage4_geo(
    rec: dict[str, Any],
    registry: CompoundRegistry | None = None,
) -> CleanListing:
    """Location canonicalization + CleanListing assembly (+ hash fill)."""
    city_canonical = canonical_city(rec["city"])
    if rec["governorate"]:
        governorate = canonical_governorate(rec["governorate"])
    else:
        governorate = governorate_for_city(city_canonical)
    compound_key = normalize_name_key(rec["compound"])

    latitude, longitude = rec["latitude"], rec["longitude"]
    if latitude is not None and longitude is not None and not validate_coords(latitude, longitude):
        latitude = longitude = None  # verified: 0 rows in the snapshot

    flags = list(rec["flags"])
    if rec["platform"] == "property_finder" and not rec["description_normalized"]:
        flags.append(Flag.SOURCE_TIER_CARD)

    if registry is not None:
        registry.collect(
            city_canonical,
            rec["compound"],
            latitude,
            longitude,
            rec["compound_geometry"],
        )

    listed_date: datetime | None = None
    if rec["listed_date"]:
        try:
            listed_date = datetime.fromisoformat(rec["listed_date"])
        except ValueError:
            listed_date = None

    def dec(value: Any) -> Decimal | None:
        return Decimal(str(value)) if value is not None else None

    listing = CleanListing(
        platform=rec["platform"],
        source_id=rec["source_id"],
        source_url=rec["source_url"],
        listing_ref=rec["listing_ref"],
        broker_name=rec["broker_name"],
        agent_name=rec["agent_name"],
        listed_date=listed_date,
        title_raw=rec["title_raw"],
        title_normalized=rec["title_normalized"],
        description_normalized=rec["description_normalized"],
        property_type=rec["property_type"],
        size_m2=dec(rec["size_m2"]),
        plot_size_m2=dec(rec["plot_size_m2"]),
        bedrooms=rec["bedrooms"],
        bathrooms=rec["bathrooms"],
        finishing=rec["finishing"],
        furnished=rec["furnished"],
        delivery_year=rec["delivery_year"],
        completion_status=rec["completion_status"],
        resale=rec["resale"],
        developer_name=rec["developer_name"],
        features=rec["features"],
        payment_scheme=rec["payment_scheme"],
        payment_frequency=rec["payment_frequency"],
        governorate=governorate,
        city=(rec["city"] or "").strip(),
        city_canonical=city_canonical,
        compound=rec["compound"],
        compound_key=compound_key,
        latitude=latitude,
        longitude=longitude,
        price_egp=dec(rec["price_egp"]) or Decimal(0),
        price_per_m2=dec(rec["price_per_m2"]),
        down_payment_egp=dec(rec["down_payment_egp"]),
        installment_years=rec["installment_years"],
        currency=rec["currency"],
        image_urls=list(rec["image_urls"]),
        floor_plan_urls=list(rec["floor_plan_urls"]),
        master_plan_url=rec["master_plan_url"],
        flags=sorted(set(flags)),
    )
    return finalize_hashes(listing)


def normalize_listing(
    raw_line: dict[str, Any],
    registry: CompoundRegistry | None = None,
) -> CleanListing:
    """Stages 1–4 in one call."""
    raw = stage1_validate(raw_line)
    rec = stage2_hygiene(raw)
    rec = stage3_canonical(rec)
    return stage4_geo(rec, registry)


__all__ = [
    "normalize_listing",
    "stage1_validate",
    "stage2_hygiene",
    "stage3_canonical",
    "stage4_geo",
]
