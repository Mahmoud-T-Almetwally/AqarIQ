"""schema pivot: drop raw_jsonb, add property/listing/financial fields & chat attached_files

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-18
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geography
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- locations ---
    op.alter_column(
        "locations",
        "geom",
        existing_type=Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=True,
    )
    op.add_column(
        "locations",
        sa.Column(
            "boundary",
            Geography(geometry_type="POLYGON", srid=4326, spatial_index=False),
            nullable=True,
        ),
    )
    op.drop_constraint("uq_locations_geo_hierarchy", "locations", type_="unique")
    op.create_unique_constraint(
        "uq_locations_geo_hierarchy",
        "locations",
        ["governorate", "city", "compound"],
        postgresql_nulls_not_distinct=True,
    )

    # --- properties ---
    op.alter_column(
        "properties",
        "geom",
        existing_type=Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=True,
    )
    op.add_column("properties", sa.Column("plot_size_m2", sa.Numeric(8, 2), nullable=True))
    op.add_column("properties", sa.Column("furnished", sa.Boolean(), nullable=True))
    op.add_column(
        "properties", sa.Column("completion_status", sa.String(length=40), nullable=True)
    )
    op.add_column("properties", sa.Column("resale", sa.Boolean(), nullable=True))
    op.add_column(
        "properties", sa.Column("developer_name", sa.String(length=160), nullable=True)
    )
    op.add_column(
        "properties", sa.Column("description_normalized", sa.Text(), nullable=True)
    )
    op.add_column(
        "properties",
        sa.Column(
            "image_urls",
            JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "properties",
        sa.Column(
            "floor_plan_urls",
            JSONB(),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "properties", sa.Column("master_plan_url", sa.Text(), nullable=True)
    )

    # Rebuild generated tsv column to index title + description
    op.drop_index("ix_properties_tsv", table_name="properties")
    op.drop_column("properties", "tsv")
    op.add_column(
        "properties",
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed(
                "to_tsvector('simple', title_normalized || ' ' || coalesce(description_normalized, ''))",
                persisted=True,
            ),
            nullable=True,
        ),
    )
    op.create_index("ix_properties_tsv", "properties", ["tsv"], postgresql_using="gin")

    # --- scraped_listings ---
    op.drop_column("scraped_listings", "raw_jsonb")
    op.add_column(
        "scraped_listings",
        sa.Column("source_id", sa.String(length=64), nullable=False),
    )
    op.add_column(
        "scraped_listings",
        sa.Column("listing_ref", sa.String(length=80), nullable=True),
    )
    op.add_column(
        "scraped_listings",
        sa.Column("agent_name", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "scraped_listings",
        sa.Column("listed_date", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint(
        "uq_scraped_listings_source_id",
        "scraped_listings",
        ["platform", "source_id"],
    )

    # --- financials ---
    op.add_column(
        "financials",
        sa.Column("payment_frequency", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "financials",
        sa.Column("price_per_m2", sa.Numeric(12, 2), nullable=True),
    )

    # --- chat_messages ---
    op.add_column(
        "chat_messages",
        sa.Column("attached_files", JSONB(), nullable=True),
    )
    op.create_check_constraint(
        "attached_files_limit",
        "chat_messages",
        "attached_files IS NULL OR (jsonb_typeof(attached_files) = 'array' "
        "AND jsonb_array_length(attached_files) <= 3)",
    )


def downgrade() -> None:
    # --- chat_messages ---
    op.drop_constraint(
        "ck_chat_messages_attached_files_limit", "chat_messages", type_="check"
    )
    op.drop_column("chat_messages", "attached_files")

    # --- financials ---
    op.drop_column("financials", "price_per_m2")
    op.drop_column("financials", "payment_frequency")

    # --- scraped_listings ---
    op.drop_constraint(
        "uq_scraped_listings_source_id", "scraped_listings", type_="unique"
    )
    op.drop_column("scraped_listings", "listed_date")
    op.drop_column("scraped_listings", "agent_name")
    op.drop_column("scraped_listings", "listing_ref")
    op.drop_column("scraped_listings", "source_id")
    op.add_column("scraped_listings", sa.Column("raw_jsonb", JSONB(), nullable=True))

    # --- properties ---
    op.drop_index("ix_properties_tsv", table_name="properties")
    op.drop_column("properties", "tsv")
    op.add_column(
        "properties",
        sa.Column(
            "tsv",
            TSVECTOR(),
            sa.Computed("to_tsvector('simple', title_normalized)", persisted=True),
            nullable=True,
        ),
    )
    op.create_index("ix_properties_tsv", "properties", ["tsv"], postgresql_using="gin")
    op.drop_column("properties", "master_plan_url")
    op.drop_column("properties", "floor_plan_urls")
    op.drop_column("properties", "image_urls")
    op.drop_column("properties", "description_normalized")
    op.drop_column("properties", "developer_name")
    op.drop_column("properties", "resale")
    op.drop_column("properties", "completion_status")
    op.drop_column("properties", "furnished")
    op.drop_column("properties", "plot_size_m2")
    op.alter_column(
        "properties",
        "geom",
        existing_type=Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )

    # --- locations ---
    op.drop_constraint("uq_locations_geo_hierarchy", "locations", type_="unique")
    op.create_unique_constraint(
        "uq_locations_geo_hierarchy",
        "locations",
        ["governorate", "city", "compound"],
    )
    op.drop_column("locations", "boundary")
    op.alter_column(
        "locations",
        "geom",
        existing_type=Geography(geometry_type="POINT", srid=4326, spatial_index=False),
        nullable=False,
    )