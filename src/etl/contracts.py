"""Pydantic contracts and content hashes — Stage 1.

Two models pin the pipeline's two ends:

* :class:`RawListing` mirrors the Scrapy item exactly — every field optional,
  tolerant types — because it must accept whatever the spiders emitted
  (``source_id`` is int on Nawy, str on PropertyFinder; ``listed_date``
  carries a trailing ``Z``).
* :class:`CleanListing` is the strict canonical record the rest of the
  pipeline (and ``scripts/seed_db.py``) consumes.

Hashes serve two distinct roles and are intentionally different:

* :func:`content_hash` — SHA-256 over the canonical JSON of the *normalized*
  payload core (sorted keys, stable separators). It is the
  ``scraped_listings.content_hash`` idempotency key: re-seeding is a no-op
  while the normalized content is unchanged, and a mapping fix changes the
  hash exactly when it changes what the database would store.
* :func:`text_hash` — SHA-256 over the normalized title + description; the
  LLM-extraction cache key, so the frozen dataset pays for enrichment once.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

PLATFORM_VALUES = ("nawy", "property_finder")
PLATFORM_MAP = {"Nawy": "nawy", "PropertyFinder": "property_finder"}


def canonical_json(payload: Any) -> str:
    """Deterministic JSON used by both hash builders."""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)


class RawListing(BaseModel):
    """Lenient mirror of ``scrappers/rea/items.ReaItem`` (Stage 1 input)."""

    model_config = ConfigDict(extra="allow")

    platform: str
    source_id: str | int | float
    source_url: str
    listing_ref: str | None = None
    listed_date: str | None = None
    broker_name: str | None = None
    agent_name: str | None = None
    developer_name: str | None = None
    title_raw: str
    property_type: str | None = None
    description: str | None = None
    size_m2: int | float | None = None
    plot_size_m2: int | float | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None
    finishing: str | None = None
    furnished: str | None = None
    delivery_year: int | None = None
    completion_status: str | None = None
    resale: bool | None = None
    has_nanny_room: bool | None = None
    features: list[str] = Field(default_factory=list)
    payment_methods: list[str] = Field(default_factory=list)
    price_egp: int | float
    currency: str = "EGP"
    down_payment_egp: int | float | None = None
    installment_years: int | None = None
    payment_frequency: str | None = None
    price_per_m2: int | float | None = None
    governorate: str | None = None
    city: str | None = None
    compound: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    compound_geometry: dict[str, Any] | None = None
    image_urls: list[str] = Field(default_factory=list)
    floor_plan_urls: list[str] = Field(default_factory=list)
    master_plan_url: str | None = None

    @field_validator("platform")
    @classmethod
    def _normalize_platform(cls, value: str) -> str:
        if value not in PLATFORM_MAP:
            raise ValueError(f"unknown platform {value!r}; expected Nawy or PropertyFinder")
        return PLATFORM_MAP[value]

    @field_validator("source_id")
    @classmethod
    def _coerce_source_id(cls, value: str | int | float) -> str:
        return str(int(value)) if isinstance(value, float) and value.is_integer() else str(value)

    @field_validator("listed_date")
    @classmethod
    def _parse_iso_z(cls, value: str | None) -> str | None:
        return value.replace("Z", "+00:00") if value and value.endswith("Z") else value


class CleanListing(BaseModel):
    """Strict canonical record (output of Stages 1–4, input to Stages 5–7)."""

    model_config = ConfigDict(extra="forbid")

    # identity / provenance
    platform: Literal["nawy", "property_finder"]
    source_id: str
    source_url: str
    listing_ref: str | None = None
    broker_name: str | None = None
    agent_name: str | None = None
    listed_date: datetime | None = None

    # text (Stage 2)
    title_raw: str
    title_normalized: str
    description_normalized: str | None = None

    # canonical vocabularies (Stage 3)
    property_type: str
    size_m2: Decimal | None = None
    plot_size_m2: Decimal | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None
    finishing: str | None = None
    furnished: bool | None = None
    delivery_year: int | None = None
    completion_status: str | None = None
    resale: bool | None = None
    developer_name: str | None = None
    features: dict[str, Any] = Field(default_factory=dict)
    payment_scheme: str | None = None
    payment_frequency: str | None = None

    # location (Stage 4)
    governorate: str
    city: str
    city_canonical: str
    compound: str | None = None
    compound_key: str | None = None
    latitude: float | None = None
    longitude: float | None = None

    # financial (raw-normalized; Stage 5 refines)
    price_egp: Decimal
    price_per_m2: Decimal | None = None
    down_payment_egp: Decimal | None = None
    installment_years: int | None = None
    monthly_payment_egp: Decimal | None = None
    currency: Literal["EGP", "USD"] = "EGP"

    # media
    image_urls: list[str] = Field(default_factory=list)
    floor_plan_urls: list[str] = Field(default_factory=list)
    master_plan_url: str | None = None

    # pipeline metadata
    flags: list[str] = Field(default_factory=list)
    extraction: dict[str, Any] = Field(default_factory=dict)
    property_id: int | None = None
    content_hash: str = ""
    text_hash: str = ""


# Fields that constitute the "normalized payload" for the idempotency hash.
# Derived metadata (flags, extraction, property_id) and the hashes themselves
# are deliberately excluded so rule changes that only add flags do not churn
# the database identity.
_HASH_FIELDS = (
    "platform",
    "source_id",
    "source_url",
    "listing_ref",
    "broker_name",
    "agent_name",
    "listed_date",
    "title_normalized",
    "description_normalized",
    "property_type",
    "size_m2",
    "plot_size_m2",
    "bedrooms",
    "bathrooms",
    "finishing",
    "furnished",
    "delivery_year",
    "completion_status",
    "resale",
    "developer_name",
    "payment_scheme",
    "payment_frequency",
    "governorate",
    "city_canonical",
    "compound_key",
    "price_egp",
    "down_payment_egp",
    "installment_years",
    "currency",
)


def content_hash(listing: CleanListing) -> str:
    """SHA-256 of the normalized payload core (idempotency key)."""
    payload = listing.model_dump(mode="json", include=set(_HASH_FIELDS))
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def text_hash(title_normalized: str, description_normalized: str | None) -> str:
    """SHA-256 over the normalized text (LLM-extraction cache key)."""
    return hashlib.sha256(
        canonical_json([title_normalized, description_normalized or ""]).encode("utf-8")
    ).hexdigest()


def finalize_hashes(listing: CleanListing) -> CleanListing:
    """Return a copy with both hashes filled (they are not set during build)."""
    return listing.model_copy(
        update={
            "content_hash": content_hash(listing),
            "text_hash": text_hash(listing.title_normalized, listing.description_normalized),
        }
    )


__all__ = [
    "CleanListing",
    "PLATFORM_MAP",
    "PLATFORM_VALUES",
    "RawListing",
    "canonical_json",
    "content_hash",
    "finalize_hashes",
    "text_hash",
]
