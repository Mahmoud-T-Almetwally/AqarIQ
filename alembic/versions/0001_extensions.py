"""extensions: postgis, vector, pg_trgm, unaccent

Revision ID: 0001
Revises:
Create Date: 2026-11-05

Project-Description §2: the four extensions the consolidated Postgres
search tier needs — geospatial (postgis), embeddings (vector), fuzzy
matching (pg_trgm), and Latin diacritic folding (unaccent) — created in
migration 0001 so every later migration can rely on them. ``IF NOT
EXISTS`` keeps this idempotent on Neon branches restored from a snapshot.
"""

from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "0001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_EXTENSIONS = ("postgis", "vector", "pg_trgm", "unaccent")


def upgrade() -> None:
    for extension in _EXTENSIONS:
        op.execute(f'CREATE EXTENSION IF NOT EXISTS "{extension}"')


def downgrade() -> None:
    # Reverse order. By the time 0001 downgrades, every table using these
    # types is already gone (0004→0002 drop first), so the drops succeed.
    for extension in reversed(_EXTENSIONS):
        op.execute(f'DROP EXTENSION IF EXISTS "{extension}"')
