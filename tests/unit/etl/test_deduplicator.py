"""Unit tests for src.etl.deduplicator — the three merge tiers."""

from __future__ import annotations

from decimal import Decimal

from src.etl.deduplicator import deduplicate, trigram_similarity
from tests.unit.etl.conftest import make_listing


class TestTrigramSimilarity:
    def test_identical(self):
        assert trigram_similarity("Villa in Bellagio", "Villa in Bellagio") == 1.0

    def test_disjoint(self):
        assert trigram_similarity("abcdefgh", "qrstuvwxyz") == 0.0

    def test_empty(self):
        assert trigram_similarity("", "abc") == 0.0


class TestTierA:
    def test_same_platform_exact_pair_merges(self):
        pair = [
            make_listing(source_id="1", platform="property_finder", price_egp=Decimal("8000000"),
                         broker_name="A"),
            make_listing(source_id="2", platform="property_finder", price_egp=Decimal("8000000"),
                         broker_name="A"),
        ]
        result = deduplicate(pair)
        assert len(result.drafts) == 1
        assert result.stats["tier_a_groups"] == 1
        assert result.stats["tier_a_rows"] == 2
        assert result.listings[0].property_id == result.listings[1].property_id

    def test_identical_key_across_platforms_is_tier_b(self):
        pair = [
            make_listing(source_id="1", platform="nawy"),
            make_listing(source_id="2", platform="property_finder"),
        ]
        result = deduplicate(pair)
        # same exact key across platforms → tier B merge, one property
        assert len(result.drafts) == 1
        assert result.stats["tier_b_groups"] == 1


class TestTierB:
    def test_cross_platform_exact_merges_with_median_price(self):
        pair = [
            make_listing(source_id="1", platform="nawy", price_egp=Decimal("30000000")),
            make_listing(source_id="2", platform="property_finder", price_egp=Decimal("30000000")),
        ]
        result = deduplicate(pair)
        assert len(result.drafts) == 1
        assert result.stats["tier_b_groups"] == 1
        assert result.drafts[0].price_egp == Decimal("30000000")

    def test_cross_platform_different_price_not_exact(self):
        pair = [
            make_listing(source_id="1", platform="nawy", price_egp=Decimal("30000000")),
            make_listing(source_id="2", platform="property_finder", price_egp=Decimal("31000000")),
        ]
        result = deduplicate(pair)
        assert result.stats["tier_b_groups"] == 0
        # Tier C: size+beds+baths all agree (3 signals) → still merges
        assert len(result.drafts) == 1
        assert result.stats["tier_c_groups"] == 1


class TestTierC:
    def test_two_signals_merge(self):
        pair = [
            make_listing(source_id="1", platform="nawy", price_egp=Decimal("30000000"),
                         bedrooms=4, bathrooms=3),
            make_listing(source_id="2", platform="property_finder", price_egp=Decimal("29000000"),
                         bedrooms=4, bathrooms=3),
        ]
        result = deduplicate(pair)
        assert len(result.drafts) == 1
        assert result.stats["tier_c_groups"] == 1
        assert result.review_queue

    def test_one_signal_no_merge(self):
        pair = [
            make_listing(source_id="1", platform="nawy", price_egp=Decimal("30000000"),
                         bedrooms=4, bathrooms=4, title_normalized="Villa fully finished"),
            make_listing(source_id="2", platform="property_finder", price_egp=Decimal("20000000"),
                         bedrooms=3, bathrooms=3, title_normalized="Apartment semi finished"),
        ]
        result = deduplicate(pair)
        assert len(result.drafts) == 2
        assert result.stats["tier_c_groups"] == 0

    def test_within_platform_same_broker_never_merges(self):
        # Sister units: same broker, identical size/price/beds, near-identical
        # titles — indistinguishable from relistings except by broker, so the
        # conservative rule refuses the merge.
        pair = [
            make_listing(source_id="1", platform="property_finder", price_egp=Decimal("5000000"),
                         bedrooms=3, bathrooms=2, broker_name="Same Broker",
                         title_normalized="Apartment 150m in Zed West with garden"),
            make_listing(source_id="2", platform="property_finder", price_egp=Decimal("5100000"),
                         bedrooms=3, bathrooms=2, broker_name="Same Broker",
                         title_normalized="Apartment 150m in Zed West prime view"),
        ]
        result = deduplicate(pair)
        assert len(result.drafts) == 2

    def test_within_platform_different_broker_merges(self):
        pair = [
            make_listing(source_id="1", platform="property_finder", price_egp=Decimal("5000000"),
                         bedrooms=3, bathrooms=2, broker_name="Broker A",
                         title_normalized="Apartment in Zed West"),
            make_listing(source_id="2", platform="property_finder", price_egp=Decimal("4900000"),
                         bedrooms=3, bathrooms=2, broker_name="Broker B",
                         title_normalized="Flat for sale in Zed West"),
        ]
        result = deduplicate(pair)
        assert len(result.drafts) == 1

    def test_no_compound_no_tier_c(self):
        pair = [
            make_listing(source_id="1", compound_key=None, compound=None, platform="nawy",
                         price_egp=Decimal("5000000"), bedrooms=3, bathrooms=2),
            make_listing(source_id="2", compound_key=None, compound=None, platform="property_finder",
                         price_egp=Decimal("5000000"), bedrooms=3, bathrooms=2),
        ]
        result = deduplicate(pair)
        # different platforms + exact key → tier B merge still fires
        assert len(result.drafts) == 1


class TestMergePolicy:
    def test_platform_precedence_nawy_inventory(self):
        group = [
            make_listing(source_id="1", platform="property_finder", finishing="finished",
                         developer_name=None, title_normalized="PF ad title",
                         description_normalized="PF description"),
            make_listing(source_id="2", platform="nawy", finishing=None,
                         developer_name="Hassan Allam", title_normalized="Nawy template",
                         description_normalized=None),
        ]
        result = deduplicate(group)
        draft = result.drafts[0]
        assert draft.developer_name == "Hassan Allam"  # Nawy wins inventory fields
        assert draft.title_normalized == "PF ad title"  # PF wins text fields
        assert draft.finishing == "finished"  # best-populated

    def test_flags_union_and_median_price(self):
        pair = [
            make_listing(source_id="1", price_egp=Decimal("30000000"),
                         flags=["currency_converted"]),
            make_listing(source_id="2", platform="property_finder",
                         price_egp=Decimal("31000000"), flags=["plot_lt_size"]),
        ]
        result = deduplicate(pair)
        draft = result.drafts[0]
        assert set(draft.flags) == {"currency_converted", "plot_lt_size"}
        assert draft.price_egp == Decimal("30500000")  # median

    def test_singleton_draft(self):
        result = deduplicate([make_listing()])
        assert len(result.drafts) == 1
        assert not result.drafts[0].merged
