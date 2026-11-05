"""Tests for src/db/models.py — metadata-level (no database needed).

Guards the schema contract the ETL, repositories, and migrations all
depend on: table set, key columns, FK graph, uniqueness for idempotency,
and the Tier-3 search indexes (§2).
"""

from __future__ import annotations

import pytest
from geoalchemy2 import Geography
from pgvector.sqlalchemy import HALFVEC
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

from src.db.models import (
    EMBEDDING_DIMENSIONS,
    Base,
    ChatMessage,
    ChatSession,
    Financial,
    Location,
    MarketStat,
    Property,
    ScrapedListing,
)

EXPECTED_TABLES = {
    "locations",
    "properties",
    "scraped_listings",
    "financials",
    "market_stats",
    "chat_sessions",
    "chat_messages",
}


@pytest.fixture(scope="module")
def tables():
    return {name: table for name, table in Base.metadata.tables.items()}


class TestTableSet:
    def test_exactly_the_seven_planned_tables(self, tables):
        assert set(tables) == EXPECTED_TABLES

    def test_models_export_matches_metadata(self):
        for model in (
            Location, Property, ScrapedListing, Financial,
            MarketStat, ChatSession, ChatMessage,
        ):
            assert model.__tablename__ in EXPECTED_TABLES


class TestProperties:
    def test_core_facet_columns(self, tables):
        columns = tables["properties"].columns
        for name in (
            "id", "property_type", "size_m2", "bedrooms", "bathrooms",
            "finishing", "delivery_year", "location_id", "geom",
            "title_normalized", "tsv", "features", "price_egp",
            "embedding", "is_active", "created_at", "updated_at",
        ):
            assert name in columns

    def test_embedding_is_halfvec_768(self, tables):
        embedding = tables["properties"].columns["embedding"]
        assert isinstance(embedding.type, HALFVEC)
        assert embedding.type.dim == EMBEDDING_DIMENSIONS == 768

    def test_geom_is_geography_point_4326(self, tables):
        geom = tables["properties"].columns["geom"]
        assert isinstance(geom.type, Geography)
        assert geom.type.geometry_type == "POINT"
        assert geom.type.srid == 4326

    def test_tsv_is_generated_from_title(self, tables):
        tsv = tables["properties"].columns["tsv"]
        assert isinstance(tsv.type, TSVECTOR)
        assert tsv.computed is not None
        assert "title_normalized" in tsv.computed.sqltext.text
        assert "simple" in tsv.computed.sqltext.text

    def test_features_is_jsonb(self, tables):
        assert isinstance(tables["properties"].columns["features"].type, JSONB)

    def test_search_indexes_present(self, tables):
        indexes = {idx.name: idx for idx in tables["properties"].indexes}
        assert "ix_properties_tsv" in indexes
        assert indexes["ix_properties_tsv"].dialect_options["postgresql"]["using"] == "gin"
        trgm = indexes["ix_properties_title_trgm"]
        assert trgm.dialect_options["postgresql"]["using"] == "gin"
        assert (
            trgm.dialect_options["postgresql"]["ops"]["title_normalized"]
            == "gin_trgm_ops"
        )
        assert indexes["ix_properties_features"].dialect_options["postgresql"]["using"] == "gin"
        assert indexes["ix_properties_geom"].dialect_options["postgresql"]["using"] == "gist"
        # HNSW lives in migration 0004; the model declares it too so
        # autogenerate diffs stay clean once it exists.
        hnsw = indexes["ix_properties_embedding_hnsw"]
        assert hnsw.dialect_options["postgresql"]["using"] == "hnsw"
        assert (
            hnsw.dialect_options["postgresql"]["ops"]["embedding"]
            == "halfvec_cosine_ops"
        )

    def test_facet_btree_indexes(self, tables):
        names = {idx.name for idx in tables["properties"].indexes}
        assert {
            "ix_properties_type_bedrooms",
            "ix_properties_price",
            "ix_properties_delivery_year",
            "ix_properties_location_id",
        } <= names


class TestScrapedListings:
    def test_columns(self, tables):
        columns = tables["scraped_listings"].columns
        for name in (
            "id", "property_id", "platform", "source_url", "broker_name",
            "title_raw", "title_normalized", "raw_jsonb", "lake_key",
            "content_hash", "first_seen_at", "last_seen_at",
        ):
            assert name in columns

    def test_content_hash_unique_idempotency_key(self, tables):
        constraints = {
            c.name: c for c in tables["scraped_listings"].constraints
            if hasattr(c, "name")
        }
        assert "uq_scraped_listings_content_hash" in constraints

    def test_source_url_unique_per_platform(self, tables):
        constraints = {
            c.name: c for c in tables["scraped_listings"].constraints
        }
        assert "uq_scraped_listings_source_url" in constraints

    def test_raw_jsonb_nullable_for_post_merge_pruning(self, tables):
        assert tables["scraped_listings"].columns["raw_jsonb"].nullable is True

    def test_broker_trgm_index(self, tables):
        indexes = {idx.name: idx for idx in tables["scraped_listings"].indexes}
        broker = indexes["ix_scraped_listings_broker_trgm"]
        assert broker.dialect_options["postgresql"]["using"] == "gin"
        assert (
            broker.dialect_options["postgresql"]["ops"]["broker_name"]
            == "gin_trgm_ops"
        )


class TestFinancials:
    def test_one_to_one_with_listing(self, tables):
        constraints = {
            c.name for c in tables["financials"].constraints
        }
        assert "uq_financials_listing_id" in constraints

    def test_egyptian_market_fields(self, tables):
        columns = tables["financials"].columns
        for name in (
            "price_egp", "down_payment_egp", "installment_years",
            "monthly_payment_egp", "over_price_egp", "payment_scheme",
        ):
            assert name in columns

    def test_money_is_numeric_not_float(self, tables):
        from sqlalchemy import Numeric
        for name in ("price_egp", "down_payment_egp", "over_price_egp"):
            assert isinstance(columns_type(tables, "financials", name), Numeric)


def columns_type(tables, table_name, column_name):
    return tables[table_name].columns[column_name].type


class TestLocations:
    def test_hierarchy_unique(self, tables):
        constraints = {
            c.name for c in tables["locations"].constraints
        }
        assert "uq_locations_geo_hierarchy" in constraints

    def test_gist_geom_index(self, tables):
        indexes = {idx.name: idx for idx in tables["locations"].indexes}
        assert indexes["ix_locations_geom"].dialect_options["postgresql"]["using"] == "gist"


class TestMarketStats:
    def test_slice_is_unique_for_upserts(self, tables):
        constraints = {
            c.name for c in tables["market_stats"].constraints
        }
        assert "uq_market_stats_slice" in constraints

    def test_per_m2_aggregates(self, tables):
        columns = tables["market_stats"].columns
        for name in ("avg_price_per_m2", "min_price_per_m2", "max_price_per_m2"):
            assert name in columns


class TestChat:
    def test_session_state_jsonb(self, tables):
        assert isinstance(
            tables["chat_sessions"].columns["state"].type, JSONB
        )

    def test_messages_history_index(self, tables):
        indexes = {idx.name for idx in tables["chat_messages"].indexes}
        assert "ix_chat_messages_session_created" in indexes


class TestForeignKeyGraph:
    @pytest.mark.parametrize(
        ("table", "column", "target"),
        [
            ("properties", "location_id", "locations.id"),
            ("scraped_listings", "property_id", "properties.id"),
            ("financials", "listing_id", "scraped_listings.id"),
            ("market_stats", "location_id", "locations.id"),
            ("chat_messages", "session_id", "chat_sessions.id"),
        ],
    )
    def test_fk(self, tables, table, column, target):
        fk = next(
            fk for fk in tables[table].foreign_keys if fk.parent.name == column
        )
        assert fk.target_fullname == target

    def test_listing_property_link_is_nullable_pre_dedup(self, tables):
        # A new ad has no property until the deduplicator matches it.
        assert tables["scraped_listings"].columns["property_id"].nullable is True


class TestNamingConvention:
    def test_constraint_names_follow_convention(self, tables):
        for name, table in tables.items():
            pk = next(
                c for c in table.constraints if c.__class__.__name__ == "PrimaryKeyConstraint"
            )
            assert pk.name == f"pk_{name}"

    def test_fk_names_are_stable(self, tables):
        fk = next(
            fk for fk in tables["properties"].foreign_keys
            if fk.parent.name == "location_id"
        )
        assert fk.constraint.name == "fk_properties_location_id_locations"
