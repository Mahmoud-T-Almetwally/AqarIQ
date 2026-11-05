"""Database models — the Tier-2 relational source of truth (Neon Postgres).

Schema strategy (Project-Description §2): the Egyptian market lists the
same physical unit through many brokers, so the ad (``scraped_listings``)
is separated from the unit (``properties``). Deduplication (ETL Stage 5)
merges listings under a property via blocking keys (compound × type),
``pg_trgm`` similarity, and range rules (size/price ±5 %). Only properties
— post-dedup — get embeddings; only ads keep per-broker financials.

Search tier (Tier 3, consolidated onto Postgres instead of Elasticsearch):

* full text — ``tsv`` generated ``tsvector`` ('simple' config, GIN) over
  the *normalized* title (Arabic normalization happens in the pipeline at
  write time because Postgres's ``arabic`` config has no stemmer, §3);
* fuzzy — ``pg_trgm`` GIN on normalized titles and broker names;
* facets — btree on type/bedrooms/price/delivery year + GIN on JSONB
  features for the long-tail amenities;
* geo — PostGIS ``geography(Point, 4326)`` + GiST for ``ST_DWithin``
  radius filters around New Cairo / Sheikh Zayed;
* vector — pgvector ``halfvec(768)`` (Matryoshka-truncated from 3072) with
  an HNSW index, fused with full text via reciprocal rank fusion. Storing
  half precision keeps the 20K-property footprint ~30 MB instead of ~240 MB
  under Neon's 1 GB cap.

Storage frugality (§2): ``raw_jsonb`` on listings is pruned after the
property merge — raw payloads live in the lake; the ``lake_key`` pointer
keeps them re-fetchable. This module mirrors the FINAL schema after all
migrations (0001-0004); Alembic — never ``create_all`` — creates it.

Money columns are ``Numeric`` (EGP values reach billions; never floats).
``properties.price_egp`` is a denormalized representative ask (median of
merged listings) so the price-band facet and EGP/m² aggregates never join
``financials``; the deduplicator maintains it on merge.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from geoalchemy2 import Geography
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    Computed,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import Boolean, DateTime

# Constraint naming convention: stable, predictable names keep Alembic
# autogenerate diffs clean and make future migrations reference-safe.
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

EMBEDDING_DIMENSIONS = 768  # gemini-embedding-2, Matryoshka-truncated 3072→768


class Base(DeclarativeBase):
    """Declarative base shared by all models and by Alembic's env.py."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ---------------------------------------------------------------------------
# Geospatial hierarchy
# ---------------------------------------------------------------------------


class Location(Base):
    """Governorate → City → Compound, one row per distinct combination.

    Flat on purpose (not a self-referencing tree): every hot query filters
    or groups by compound, and ``market_stats`` joins on ``location_id``.
    ``name_normalized`` carries the Arabic-normalized compound name so
    dedup blocking keys match across portals' spellings; ``geom`` is the
    compound centroid used for radius search when a listing has no exact
    coordinates.
    """

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    governorate: Mapped[str] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(120))
    compound: Mapped[str | None] = mapped_column(String(160))
    name_normalized: Mapped[str] = mapped_column(
        String(200), comment="Arabic-normalized compound name (etl/arabic.py)"
    )
    geom: Mapped[Any] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        comment="Compound centroid (WGS84); GiST index defined explicitly",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "governorate", "city", "compound", name="uq_locations_geo_hierarchy"
        ),
        Index("ix_locations_name_normalized", "name_normalized"),
        # GiST (not the default GIST-on-gin) for ST_DWithin geography scans.
        Index("ix_locations_geom", "geom", postgresql_using="gist"),
    )


# ---------------------------------------------------------------------------
# The deduplicated physical unit
# ---------------------------------------------------------------------------


class Property(Base):
    """One physical unit after entity resolution.

    ``title_normalized`` is the canonical (deduped) title in normalized
    form; ``tsv`` is generated from it with the ``simple`` config —
    write-time Arabic normalization means the index needs no stemmer.
    ``embedding`` stays NULL until the embedder phase runs; it is
    halfvec(768) to keep the vector footprint ~1.5 KB/row (§4 Layer 3).
    """

    __tablename__ = "properties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_type: Mapped[str] = mapped_column(
        String(40),
        comment="apartment | villa | townhouse | duplex | penthouse | ...",
    )
    size_m2: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 2), comment="Unit size; dedup range rule is ±5 %"
    )
    bedrooms: Mapped[int | None] = mapped_column(SmallInteger)
    bathrooms: Mapped[int | None] = mapped_column(SmallInteger)
    finishing: Mapped[str | None] = mapped_column(
        String(40),
        comment="core & shell | semi-finished | finished | ultra | furnished",
    )
    delivery_year: Mapped[int | None] = mapped_column(
        SmallInteger, comment="Handover year (facet)"
    )
    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", name="fk_properties_location_id_locations"),
        index=True,
    )
    geom: Mapped[Any] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        comment="Exact unit location when known, else compound centroid",
    )
    title_normalized: Mapped[str] = mapped_column(Text)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed("to_tsvector('simple', title_normalized)", persisted=True),
        comment="Write-time normalized; 'arabic' config has no stemmer",
    )
    features: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        comment="LLM-extracted amenities etc.; GIN for long-tail facets",
    )
    price_egp: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2),
        comment="Representative ask (median of merged listings); dedup maintains",
    )
    embedding: Mapped[list[float] | None] = mapped_column(
        HALFVEC(EMBEDDING_DIMENSIONS),
        comment="768-dim text embedding; HNSW index in migration 0004",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, server_default=text("true"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=text("now()"),
        onupdate=text("now()"),
        nullable=False,
    )

    __table_args__ = (
        # Hot facets (§2): btree on type / beds / price band / delivery year.
        Index("ix_properties_type_bedrooms", "property_type", "bedrooms"),
        Index("ix_properties_price", "price_egp"),
        Index("ix_properties_delivery_year", "delivery_year"),
        # Full-text search over normalized titles.
        Index("ix_properties_tsv", "tsv", postgresql_using="gin"),
        # Typo-tolerant matching + dedup similarity.
        Index(
            "ix_properties_title_trgm",
            "title_normalized",
            postgresql_using="gin",
            postgresql_ops={"title_normalized": "gin_trgm_ops"},
        ),
        # Long-tail amenity facets.
        Index("ix_properties_features", "features", postgresql_using="gin"),
        # Radius search (ST_DWithin) on geography.
        Index("ix_properties_geom", "geom", postgresql_using="gist"),
        # Semantic search: HNSW over halfvec with cosine distance; created
        # by migration 0004 (index build pairs with the backfill).
        Index(
            "ix_properties_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "halfvec_cosine_ops"},
        ),
    )


# ---------------------------------------------------------------------------
# Broker ads (pre-dedup)
# ---------------------------------------------------------------------------


class ScrapedListing(Base):
    """One broker ad, linked to its deduplicated :class:`Property`.

    ``content_hash`` is the idempotency key: unchanged re-scrapes produce
    the same hash, are skipped by the ETL, and cut nightly LLM spend by
    75-90 % (§3 Stage 4). ``raw_jsonb`` is pruned after the property
    merge; ``lake_key`` keeps the immutable raw payload re-fetchable from
    the object lake (the system's backup, §8 risk 3).
    """

    __tablename__ = "scraped_listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id", name="fk_scraped_listings_property_id_properties"),
        index=True,
        comment="NULL until dedup matches/creates the property",
    )
    platform: Mapped[str] = mapped_column(
        String(40), comment="aqarmap | propertyfinder"
    )
    source_url: Mapped[str] = mapped_column(Text)
    broker_name: Mapped[str | None] = mapped_column(String(200))
    title_raw: Mapped[str] = mapped_column(Text)
    title_normalized: Mapped[str] = mapped_column(Text)
    raw_jsonb: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="Pruned after property merge; raw lives in the lake"
    )
    lake_key: Mapped[str | None] = mapped_column(
        Text, comment="Object-lake key of the immutable raw payload"
    )
    content_hash: Mapped[str] = mapped_column(
        String(64), comment="SHA-256 of normalized payload; idempotency key"
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("content_hash", name="uq_scraped_listings_content_hash"),
        UniqueConstraint(
            "platform", "source_url", name="uq_scraped_listings_source_url"
        ),
        # Dedup similarity: listing title vs property title, broker names.
        Index(
            "ix_scraped_listings_title_trgm",
            "title_normalized",
            postgresql_using="gin",
            postgresql_ops={"title_normalized": "gin_trgm_ops"},
        ),
        Index(
            "ix_scraped_listings_broker_trgm",
            "broker_name",
            postgresql_using="gin",
            postgresql_ops={"broker_name": "gin_trgm_ops"},
        ),
        Index("ix_scraped_listings_platform", "platform"),
    )


class Financial(Base):
    """Egyptian-market payment terms for one ad (1:1 with a listing).

    ``over_price_egp`` (الباقي فوق) is the resale mark-up over the
    developer's original contract price — a defining quirk of the Egyptian
    resale market (§2). All amounts EGP, ``Numeric`` — never floats.
    """

    __tablename__ = "financials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("scraped_listings.id", name="fk_financials_listing_id_scraped_listings"),
        unique=True,
    )
    payment_scheme: Mapped[str | None] = mapped_column(
        String(30), comment="cash | installments | cash_or_installments"
    )
    price_egp: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    down_payment_egp: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    installment_years: Mapped[int | None] = mapped_column(SmallInteger)
    monthly_payment_egp: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    over_price_egp: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2), comment="الباقي فوق — resale mark-up over contract"
    )
    currency: Mapped[str] = mapped_column(
        String(8), server_default=text("'EGP'"), nullable=False
    )

    __table_args__ = (
        # Price-band facet on the per-ad level (dedup range rule price ±5 %).
        Index("ix_financials_price", "price_egp"),
    )


# ---------------------------------------------------------------------------
# Nightly precomputed aggregates (migration 0004)
# ---------------------------------------------------------------------------


class MarketStat(Base):
    """Precomputed average EGP/m² per compound × property type × month.

    The chart tool reads stats-first and only falls back to guarded SQL
    for exotic slices (§2) — the LLM never runs ad-hoc GROUP BY against a
    freshly woken 0.1-CPU database. Warmed into Redis by the nightly job.
    """

    __tablename__ = "market_stats"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", name="fk_market_stats_location_id_locations"),
        index=True,
    )
    property_type: Mapped[str] = mapped_column(String(40))
    stat_month: Mapped[date] = mapped_column(
        comment="First day of the aggregated month"
    )
    avg_price_per_m2: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    min_price_per_m2: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    max_price_per_m2: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    avg_size_m2: Mapped[Decimal | None] = mapped_column(Numeric(8, 2))
    listing_count: Mapped[int] = mapped_column(Integer, nullable=False)

    __table_args__ = (
        UniqueConstraint(
            "location_id",
            "property_type",
            "stat_month",
            name="uq_market_stats_slice",
        ),
        Index("ix_market_stats_month", "stat_month"),
    )


# ---------------------------------------------------------------------------
# Chat history (migration 0003)
# ---------------------------------------------------------------------------


class ChatSession(Base):
    """One conversation. ``state`` snapshots the LangGraph checkpoint so
    the graph can resume across processes/restarts (awaiting_criteria
    state machine, §4 Layer 2)."""

    __tablename__ = "chat_sessions"

    id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid4
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    last_active_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )
    state: Mapped[dict[str, Any] | None] = mapped_column(
        JSONB, comment="LangGraph state snapshot (criteria, intent, artifacts)"
    )


class ChatMessage(Base):
    """One turn in a :class:`ChatSession` (user / assistant / tool)."""

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", name="fk_chat_messages_session_id_chat_sessions"),
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        # History read: last N messages of a session, newest last.
        Index("ix_chat_messages_session_created", "session_id", "created_at"),
    )


__all__ = [
    "Base",
    "ChatMessage",
    "ChatSession",
    "Financial",
    "Location",
    "MarketStat",
    "Property",
    "ScrapedListing",
    "EMBEDDING_DIMENSIONS",
    "NAMING_CONVENTION",
]
