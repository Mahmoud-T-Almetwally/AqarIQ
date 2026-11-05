"""market_stats + HNSW vector index

Revision ID: 0004
Revises: 0003
Create Date: 2026-11-05

* ``market_stats`` — the nightly precomputed aggregate (average EGP/m²
  per compound × property type × month, §2) that the chart tool reads
  stats-first; unique on the slice so re-runs upsert instead of
  duplicating rows.
* HNSW index on ``properties.embedding`` — ``halfvec_cosine_ops`` is the
  halfvec-specific operator class; cosine matches the embedding model's
  semantics for text (§4 Layer 3). Created here, not with the table in
  0002, per the roadmap so index build and backfill arrive together.
"""

from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "market_stats",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("location_id", sa.Integer(), nullable=False),
        sa.Column("property_type", sa.String(length=40), nullable=False),
        sa.Column("stat_month", sa.Date(), nullable=False),
        sa.Column("avg_price_per_m2", sa.Numeric(12, 2), nullable=False),
        sa.Column("min_price_per_m2", sa.Numeric(12, 2), nullable=False),
        sa.Column("max_price_per_m2", sa.Numeric(12, 2), nullable=False),
        sa.Column("avg_size_m2", sa.Numeric(8, 2), nullable=True),
        sa.Column("listing_count", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["location_id"],
            ["locations.id"],
            name="fk_market_stats_location_id_locations",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_market_stats"),
        sa.UniqueConstraint(
            "location_id",
            "property_type",
            "stat_month",
            name="uq_market_stats_slice",
        ),
    )
    op.create_index(
        "ix_market_stats_location_id", "market_stats", ["location_id"]
    )
    op.create_index("ix_market_stats_month", "market_stats", ["stat_month"])
    op.create_index(
        "ix_properties_embedding_hnsw",
        "properties",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "halfvec_cosine_ops"},
    )


def downgrade() -> None:
    op.drop_index("ix_properties_embedding_hnsw", table_name="properties")
    op.drop_index("ix_market_stats_month", table_name="market_stats")
    op.drop_index("ix_market_stats_location_id", table_name="market_stats")
    op.drop_table("market_stats")
