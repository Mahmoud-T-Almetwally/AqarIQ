"""Database models — the Tier-2 relational source of truth (Neon Postgres)."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from src.core.config import Settings
from geoalchemy2 import Geography
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy import (
    CheckConstraint,
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

NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

EMBEDDING_DIMENSIONS = 768  # gemini-embedding-2, Matryoshka-truncated 3072→768
MAX_CHAT_ATTACHMENTS = 3


class Base(DeclarativeBase):
    """Declarative base shared by all models and by Alembic's env.py."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


# ---------------------------------------------------------------------------
# Geospatial hierarchy
# ---------------------------------------------------------------------------


class Location(Base):
    """Governorate → City → Compound, one row per distinct combination."""

    __tablename__ = "locations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    governorate: Mapped[str] = mapped_column(String(120))
    city: Mapped[str | None] = mapped_column(String(120))
    compound: Mapped[str | None] = mapped_column(String(160))
    name_normalized: Mapped[str] = mapped_column(
        String(200), comment="Arabic-normalized compound/city name (etl/arabic.py)"
    )
    geom: Mapped[Any | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=True,
        comment="Compound centroid (WGS84), nullable when coordinates unavailable",
    )
    boundary: Mapped[Any | None] = mapped_column(
        Geography(geometry_type="POLYGON", srid=4326, spatial_index=False),
        nullable=True,
        comment="Compound polygon boundary (WGS84, from Nawy compound_geometry)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        UniqueConstraint(
            "governorate",
            "city",
            "compound",
            name="uq_locations_geo_hierarchy",
            postgresql_nulls_not_distinct=True,
        ),
        Index("ix_locations_name_normalized", "name_normalized"),
        Index("ix_locations_geom", "geom", postgresql_using="gist"),
    )


# ---------------------------------------------------------------------------
# The deduplicated physical unit
# ---------------------------------------------------------------------------


class Property(Base):
    """One physical unit after entity resolution."""

    __tablename__ = "properties"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_type: Mapped[str] = mapped_column(
        String(40),
        comment="apartment | villa | townhouse | twin_house | duplex | ...",
    )
    size_m2: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 2), comment="Built-up area; dedup range rule is ±5 %"
    )
    plot_size_m2: Mapped[Decimal | None] = mapped_column(
        Numeric(8, 2), comment="Total plot/land area when applicable"
    )
    bedrooms: Mapped[int | None] = mapped_column(SmallInteger)
    bathrooms: Mapped[int | None] = mapped_column(SmallInteger)
    finishing: Mapped[str | None] = mapped_column(
        String(40),
        comment="core_and_shell | semi_finished | finished | ultra",
    )
    furnished: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    delivery_year: Mapped[int | None] = mapped_column(
        SmallInteger, comment="Handover year (facet)"
    )
    completion_status: Mapped[str | None] = mapped_column(
        String(40), comment="off_plan_primary | completed_primary | ready | resale"
    )
    resale: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    developer_name: Mapped[str | None] = mapped_column(String(160))
    location_id: Mapped[int] = mapped_column(
        ForeignKey("locations.id", name="fk_properties_location_id_locations"),
        index=True,
    )
    geom: Mapped[Any | None] = mapped_column(
        Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=True,
        comment="Exact unit location when known, else compound centroid",
    )
    title_normalized: Mapped[str] = mapped_column(Text)
    description_normalized: Mapped[str | None] = mapped_column(Text)
    tsv: Mapped[Any] = mapped_column(
        TSVECTOR,
        Computed(
            "to_tsvector('simple', title_normalized || ' ' || coalesce(description_normalized, ''))",
            persisted=True,
        ),
        comment="Write-time normalized; 'arabic' config has no stemmer",
    )
    features: Mapped[dict[str, Any]] = mapped_column(
        JSONB,
        comment="Normalized amenities & extracted attributes; GIN for long-tail facets",
    )
    image_urls: Mapped[list[str]] = mapped_column(
        JSONB,
        server_default=text("'[]'::jsonb"),
        nullable=False,
        comment="Capped <= 6 image URLs (floor plans & master plan prioritized)",
    )
    floor_plan_urls: Mapped[list[str]] = mapped_column(
        JSONB,
        server_default=text("'[]'::jsonb"),
        nullable=False,
    )
    master_plan_url: Mapped[str | None] = mapped_column(Text)
    price_egp: Mapped[Decimal | None] = mapped_column(
        Numeric(14, 2),
        comment="Representative ask in EGP (median of merged listings)",
    )
    embedding: Mapped[list[float] | None] = mapped_column(
        HALFVEC(EMBEDDING_DIMENSIONS),
        comment="768-dim multimodal embedding (text + up to 6 images)",
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
        Index("ix_properties_type_bedrooms", "property_type", "bedrooms"),
        Index("ix_properties_price", "price_egp"),
        Index("ix_properties_delivery_year", "delivery_year"),
        Index("ix_properties_tsv", "tsv", postgresql_using="gin"),
        Index(
            "ix_properties_title_trgm",
            "title_normalized",
            postgresql_using="gin",
            postgresql_ops={"title_normalized": "gin_trgm_ops"},
        ),
        Index("ix_properties_features", "features", postgresql_using="gin"),
        Index("ix_properties_geom", "geom", postgresql_using="gist"),
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
    """One broker/developer ad, linked to its deduplicated :class:`Property`.

    Raw payloads are stored compressed in the Neon object lake (``lake_key``);
    ``raw_jsonb`` is not stored in Postgres.
    """

    __tablename__ = "scraped_listings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id", name="fk_scraped_listings_property_id_properties"),
        index=True,
        comment="NULL until dedup matches/creates the property",
    )
    platform: Mapped[str] = mapped_column(
        String(40), comment="nawy | property_finder"
    )
    source_id: Mapped[str] = mapped_column(
        String(64), comment="Portal's own ID (stable dedup key per platform)"
    )
    source_url: Mapped[str] = mapped_column(Text)
    listing_ref: Mapped[str | None] = mapped_column(String(80))
    broker_name: Mapped[str | None] = mapped_column(String(200))
    agent_name: Mapped[str | None] = mapped_column(String(200))
    listed_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    title_raw: Mapped[str] = mapped_column(Text)
    title_normalized: Mapped[str] = mapped_column(Text)
    lake_key: Mapped[str | None] = mapped_column(
        Text, comment="Object-lake key (or key#line) of immutable raw payload"
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
            "platform", "source_id", name="uq_scraped_listings_source_id"
        ),
        UniqueConstraint(
            "platform", "source_url", name="uq_scraped_listings_source_url"
        ),
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
    """Egyptian-market payment terms for one ad (1:1 with a listing)."""

    __tablename__ = "financials"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("scraped_listings.id", name="fk_financials_listing_id_scraped_listings"),
        unique=True,
    )
    payment_scheme: Mapped[str | None] = mapped_column(
        String(30), comment="cash | installments | cash_or_installments"
    )
    payment_frequency: Mapped[str | None] = mapped_column(
        String(20), comment="monthly | quarterly | semi_annually | annually"
    )
    price_egp: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    price_per_m2: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
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
        Index("ix_financials_price", "price_egp"),
    )


# ---------------------------------------------------------------------------
# Precomputed aggregates
# ---------------------------------------------------------------------------


class MarketStat(Base):
    """Precomputed average EGP/m² per compound × property type × month."""

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
# Chat history
# ---------------------------------------------------------------------------


class ChatSession(Base):
    """One conversation with LangGraph state snapshot."""

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
    """One turn in a :class:`ChatSession` (user / assistant / tool).

    ``attached_files`` stores constrained compressed-object descriptors
    (e.g. ``[{"key": "...", "ext": ".jpg", "size_bytes": 12345, "compressed": true}]``)
    never raw external URLs; the API/client reconstructs download links.
    """

    __tablename__ = "chat_messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("chat_sessions.id", name="fk_chat_messages_session_id_chat_sessions"),
        index=True,
    )
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    attached_files: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB,
        nullable=True,
        comment="Constrained compressed-file lake keys + metadata (max 3 per turn)",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("now()"), nullable=False
    )

    __table_args__ = (
        Index("ix_chat_messages_session_created", "session_id", "created_at"),
        CheckConstraint(
            f"attached_files IS NULL OR (jsonb_typeof(attached_files) = 'array' "
            f"AND jsonb_array_length(attached_files) <= {MAX_CHAT_ATTACHMENTS})",
            name="attached_files_limit",
        ),
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
    "MAX_CHAT_ATTACHMENTS",
    "NAMING_CONVENTION",
]