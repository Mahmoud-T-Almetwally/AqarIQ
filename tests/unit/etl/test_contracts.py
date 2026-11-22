"""Unit tests for src.etl.contracts — validation, coercion and hashes."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from src.etl.contracts import (
    CleanListing,
    RawListing,
    content_hash,
    text_hash,
)
from tests.unit.etl.conftest import make_listing


class TestRawListing:
    def test_platform_normalized(self):
        raw = RawListing.model_validate(
            {"platform": "Nawy", "source_id": 123, "source_url": "https://x/1", "title_raw": "t",
             "price_egp": 100}
        )
        assert raw.platform == "nawy"

    def test_source_id_int_to_str(self):
        raw = RawListing.model_validate(
            {"platform": "Nawy", "source_id": 123, "source_url": "https://x/1", "title_raw": "t",
             "price_egp": 100}
        )
        assert raw.source_id == "123"

    def test_z_suffix_normalized(self):
        raw = RawListing.model_validate(
            {"platform": "PropertyFinder", "source_id": "1", "source_url": "https://x/1",
             "title_raw": "t", "price_egp": 100, "listed_date": "2026-11-13T15:10:08Z"}
        )
        assert raw.listed_date == "2026-11-13T15:10:08+00:00"

    def test_unknown_platform_rejected(self):
        with pytest.raises(ValidationError):
            RawListing.model_validate(
                {"platform": "Aqarmap", "source_id": "1", "source_url": "https://x/1",
                 "title_raw": "t", "price_egp": 100}
            )

    def test_missing_price_rejected(self):
        with pytest.raises(ValidationError):
            RawListing.model_validate(
                {"platform": "Nawy", "source_id": "1", "source_url": "https://x/1",
                 "title_raw": "t"}
            )


class TestHashes:
    def test_content_hash_stable(self):
        listing = make_listing()
        assert content_hash(listing) == content_hash(listing)

    def test_content_hash_changes_with_content(self):
        base = make_listing()
        changed = base.model_copy(update={"price_egp": Decimal("999")})
        assert content_hash(base) != content_hash(changed)

    def test_content_hash_ignores_flags(self):
        base = make_listing()
        flagged = base.model_copy(update={"flags": ["currency_converted"]})
        assert content_hash(base) == content_hash(flagged)

    def test_text_hash_differs_by_text(self):
        assert text_hash("a", "b") != text_hash("a", "c")

    def test_finalize_hashes(self):
        listing = make_listing()
        assert listing.content_hash  # factory already finalizes
        assert len(listing.content_hash) == 64

    def test_hash_determinism_across_instances(self):
        a = make_listing(source_id="7")
        b = make_listing(source_id="7")
        assert a.content_hash == b.content_hash


class TestCleanListing:
    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            CleanListing.model_validate(
                {
                    "platform": "nawy",
                    "source_id": "1",
                    "source_url": "https://x/1",
                    "title_raw": "t",
                    "title_normalized": "t",
                    "property_type": "villa",
                    "governorate": "Cairo",
                    "city": "New Cairo",
                    "city_canonical": "new_cairo",
                    "price_egp": "100",
                    "surprise": True,
                }
            )
