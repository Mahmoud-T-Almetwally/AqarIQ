"""Tests for the Alembic chain 0001→0004 (offline SQL, no database).

Renders ``alembic upgrade head --sql`` in-process and asserts:

* the revision chain is linear and has exactly one head;
* every extension, table, and special index from Project-Description §2
  is created;
* the SQL agrees with ``src/db/models.py`` — every model table and every
  model column appears in the migration chain (drift guard: models and
  migrations can never silently diverge).
"""

from __future__ import annotations

import io
import os
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory

from src.db.models import Base

DUMMY_URL = (
    "postgresql://user:pw@ep-demo-pooler.eu-central-1.aws.neon.tech/neondb"
)


@pytest.fixture(scope="module")
def alembic_config(tmp_path_factory) -> Config:
    repo_root = str(Path(__file__).parent.parent.parent.parent)
    config = Config(os.path.join(repo_root, "alembic.ini"))
    config.set_main_option("script_location", os.path.join(repo_root, "alembic"))
    return config


@pytest.fixture(scope="module")
def upgrade_sql(alembic_config) -> str:
    """The full chain rendered offline (env var URL → asyncpg dialect)."""
    os.environ["DATABASE_URL"] = DUMMY_URL
    try:
        buffer = io.StringIO()
        stdout, _ = _capture_alembic(alembic_config, buffer)
        return stdout
    finally:
        os.environ.pop("DATABASE_URL", None)


def _capture_alembic(config: Config, buffer: io.StringIO) -> tuple[str, str]:
    import contextlib
    import sys

    with contextlib.redirect_stdout(buffer):
        command.upgrade(config, "head", sql=True)
    return buffer.getvalue(), ""


class TestRevisionChain:
    def test_linear_chain_single_head(self, alembic_config):
        script = ScriptDirectory.from_config(alembic_config)
        heads = script.get_heads()
        assert heads == ["0004"]
        revisions = {
            rev: script.get_revision(rev) for rev in ("0001", "0002", "0003", "0004")
        }
        assert revisions["0001"].down_revision is None
        assert revisions["0002"].down_revision == "0001"
        assert revisions["0003"].down_revision == "0002"
        assert revisions["0004"].down_revision == "0003"

    def test_every_migration_has_downgrade(self, alembic_config):
        script = ScriptDirectory.from_config(alembic_config)
        for revision in script.walk_revisions():
            source = open(revision.path).read()
            assert "def downgrade" in source, revision.path


class TestExtensionMigration:
    def test_all_four_extensions_created(self, upgrade_sql):
        for extension in ("postgis", "vector", "pg_trgm", "unaccent"):
            assert f'CREATE EXTENSION IF NOT EXISTS "{extension}"' in upgrade_sql


class TestTablesAndSpecialIndexes:
    def test_all_seven_tables_created(self, upgrade_sql):
        for table in (
            "locations", "properties", "scraped_listings", "financials",
            "market_stats", "chat_sessions", "chat_messages",
        ):
            assert f"CREATE TABLE {table} (" in upgrade_sql

    def test_hnsw_halfvec_index(self, upgrade_sql):
        assert (
            "CREATE INDEX ix_properties_embedding_hnsw ON properties "
            "USING hnsw (embedding halfvec_cosine_ops)" in upgrade_sql
        )

    def test_geography_columns(self, upgrade_sql):
        assert upgrade_sql.count("geography(POINT,4326)") == 2

    def test_tsvector_generated_with_simple_config(self, upgrade_sql):
        assert (
            "GENERATED ALWAYS AS "
            "(to_tsvector('simple', title_normalized)) STORED" in upgrade_sql
        )

    def test_trgm_indexes(self, upgrade_sql):
        assert "gin_trgm_ops" in upgrade_sql
        assert "ix_scraped_listings_broker_trgm" in upgrade_sql

    def test_idempotency_and_slice_constraints(self, upgrade_sql):
        assert "uq_scraped_listings_content_hash" in upgrade_sql
        assert "uq_scraped_listings_source_url" in upgrade_sql
        assert "uq_market_stats_slice" in upgrade_sql

    def test_halfvec_column_768(self, upgrade_sql):
        assert "embedding HALFVEC(768)" in upgrade_sql


class TestModelsMatchMigrations:
    """Drift guard: every model table/column exists in the chain's SQL."""

    def test_every_model_table_in_sql(self, upgrade_sql):
        for table_name in Base.metadata.tables:
            assert f"CREATE TABLE {table_name} (" in upgrade_sql, table_name

    def test_every_model_column_in_sql(self, upgrade_sql):
        missing = []
        for table_name, table in Base.metadata.tables.items():
            for column in table.columns:
                if f"{column.name} " not in upgrade_sql and (
                    f"{column.name}," not in upgrade_sql
                    and f"{column.name}\n" not in upgrade_sql
                ):
                    missing.append(f"{table_name}.{column.name}")
        assert not missing, f"columns missing from migrations: {missing}"

    def test_every_model_index_name_in_sql(self, upgrade_sql):
        missing = []
        for table_name, table in Base.metadata.tables.items():
            for index in table.indexes:
                if f"CREATE INDEX {index.name}" not in upgrade_sql:
                    missing.append(f"{table_name}.{index.name}")
        assert not missing, f"indexes missing from migrations: {missing}"

    def test_unique_constraints_covered(self, upgrade_sql):
        for table_name, table in Base.metadata.tables.items():
            for constraint in table.constraints:
                if constraint.__class__.__name__ == "UniqueConstraint":
                    assert (
                        f"CONSTRAINT {constraint.name} UNIQUE" in upgrade_sql
                    ), f"{table_name}.{constraint.name}"
