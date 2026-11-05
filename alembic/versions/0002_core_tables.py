"""core tables: locations, properties, scraped_listings, financials

Revision ID: 0002
Revises: 0001
Create Date: 2026-11-05

Schema per Project-Description §2, mirrored by ``src/db/models.py``
(constraint names match the models' naming convention so future
autogenerate diffs stay empty). Search-tier indexes (§2 Tier 3):

* tsvector GIN on ``properties.tsv`` (generated, 'simple' config —
  normalization happened in the pipeline at write time);
* pg_trgm GIN on normalized titles and broker names (typo tolerance +
  dedup similarity);
* btree facets: type×bedrooms, price, delivery year, platform;
* JSONB GIN on ``properties.features`` for long-tail amenity facets;
* GiST on geography columns for ST_DWithin radius filters.

``properties.embedding`` (halfvec 768) is created here with the table;
its HNSW index arrives in 0004 per the roadmap.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "locations",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("governorate", sa.String(length=120), nullable=False),
        sa.Column("city", sa.String(length=120), nullable=True),
        sa.Column("compound", sa.String(length=160), nullable=True),
        sa.Column("name_normalized", sa.String(length=200), nullable=False),
        sa.Column(
            "geom",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_locations"),
        sa.UniqueConstraint(
            "governorate", "city", "compound", name="uq_locations_geo_hierarchy"
        ),
    )
    op.create_index(
        "ix_locations_name_normalized", "locations", ["name_normalized"]
    )
    op.create_index(
        "ix_locations_geom", "locations", ["geom"], postgresql_using="gist"
    )

    op.create_table(
        "properties",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("property_type", sa.String(length=40), nullable=False),
        sa.Column("size_m2", sa.Numeric(8, 2), nullable=True),
        sa.Column("bedrooms", sa.SmallInteger(), nullable=True),
        sa.Column("bathrooms", sa.SmallInteger(), nullable=True),
        sa.Column("finishing", sa.String(length=40), nullable=True),
        sa.Column("delivery_year", sa.SmallInteger(), nullable=True),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column(
            "geom",
            Geography(geometry_type="POINT", srid=4326, spatial_index=False),
            nullable=False,
        ),
        sa.Column("title_normalized", sa.Text(), nullable=False),
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('simple', title_normalized)", persisted=True),
            nullable=True,
        ),
        sa.Column("features", JSONB(), nullable=False),
        sa.Column("price_egp", sa.Numeric(14, 2), nullable=True),
        sa.Column("embedding", HALFVEC(768), nullable=True),
        sa.Column(
            "is_active",
            sa.Boolean(),
            server_default=sa.text("true"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["locations.id"],
            name="fk_properties_location_id_locations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_properties"),
    )
    op.create_index(
        "ix_properties_location_id", "properties", ["location_id"]
    )
    op.create_index(
        "ix_properties_type_bedrooms",
        "properties",
        ["property_type", "bedrooms"],
    )
    op.create_index("ix_properties_price", "properties", ["price_egp"])
    op.create_index(
        "ix_properties_delivery_year", "properties", ["delivery_year"]
    )
    op.create_index(
        "ix_properties_tsv", "properties", ["tsv"], postgresql_using="gin"
    )
    op.create_index(
        "ix_properties_title_trgm",
        "properties",
        ["title_normalized"],
        postgresql_using="gin",
        postgresql_ops={"title_normalized": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_properties_features",
        "properties",
        ["features"],
        postgresql_using="gin",
    )
    op.create_index(
        "ix_properties_geom", "properties", ["geom"], postgresql_using="gist"
    )

    op.create_table(
        "scraped_listings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("property_id", sa.Integer(), nullable=True),
        sa.Column("platform", sa.String(length=40), nullable=False),
        sa.Column("source_url", sa.Text(), nullable=False),
        sa.Column("broker_name", sa.String(length=200), nullable=True),
        sa.Column("title_raw", sa.Text(), nullable=False),
        sa.Column("title_normalized", sa.Text(), nullable=False),
        sa.Column("raw_jsonb", JSONB(), nullable=True),
        sa.Column("lake_key", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["property_id"],
            ["properties.id"],
            name="fk_scraped_listings_property_id_properties",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_scraped_listings"),
        sa.UniqueConstraint(
            "content_hash", name="uq_scraped_listings_content_hash"
        ),
        sa.UniqueConstraint(
            "platform", "source_url", name="uq_scraped_listings_source_url"
        ),
    )
    op.create_index(
        "ix_scraped_listings_property_id", "scraped_listings", ["property_id"]
    )
    op.create_index(
        "ix_scraped_listings_platform", "scraped_listings", ["platform"]
    )
    op.create_index(
        "ix_scraped_listings_title_trgm",
        "scraped_listings",
        ["title_normalized"],
        postgresql_using="gin",
        postgresql_ops={"title_normalized": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_scraped_listings_broker_trgm",
        "scraped_listings",
        ["broker_name"],
        postgresql_using="gin",
        postgresql_ops={"broker_name": "gin_trgm_ops"},
    )

    op.create_table(
        "financials",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("listing_id", sa.Integer(), nullable=False),
        sa.Column("payment_scheme", sa.String(length=30), nullable=True),
        sa.Column("price_egp", sa.Numeric(14, 2), nullable=False),
        sa.Column("down_payment_egp", sa.Numeric(14, 2), nullable=True),
        sa.Column("installment_years", sa.SmallInteger(), nullable=True),
        sa.Column("monthly_payment_egp", sa.Numeric(12, 2), nullable=True),
        sa.Column("over_price_egp", sa.Numeric(14, 2), nullable=True),
        sa.Column(
            "currency",
            sa.String(length=8),
            server_default=sa.text("'EGP'"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["listing_id"],
            ["scraped_listings.id"],
            name="fk_financials_listing_id_scraped_listings",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_financials"),
        sa.UniqueConstraint("listing_id", name="uq_financials_listing_id"),
    )
    op.create_index("ix_financials_price", "financials", ["price_egp"])


def downgrade() -> None:
    op.drop_index("ix_financials_price", table_name="financials")
    op.drop_table("financials")
    op.drop_index("ix_scraped_listings_broker_trgm", table_name="scraped_listings")
    op.drop_index("ix_scraped_listings_title_trgm", table_name="scraped_listings")
    op.drop_index("ix_scraped_listings_platform", table_name="scraped_listings")
    op.drop_index("ix_scraped_listings_property_id", table_name="scraped_listings")
    op.drop_table("scraped_listings")
    op.drop_index("ix_properties_geom", table_name="properties")
    op.drop_index("ix_properties_features", table_name="properties")
    op.drop_index("ix_properties_title_trgm", table_name="properties")
    op.drop_index("ix_properties_tsv", table_name="properties")
    op.drop_index("ix_properties_delivery_year", table_name="properties")
    op.drop_index("ix_properties_price", table_name="properties")
    op.drop_index("ix_properties_type_bedrooms", table_name="properties")
    op.drop_index("ix_properties_location_id", table_name="properties")
    op.drop_table("properties")
    op.drop_index("ix_locations_geom", table_name="locations")
    op.drop_index("ix_locations_name_normalized", table_name="locations")
    op.drop_table("locations")
