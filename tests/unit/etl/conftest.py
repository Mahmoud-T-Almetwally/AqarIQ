"""Shared factory for CleanListing instances in etl tests."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from src.etl.contracts import CleanListing, finalize_hashes


def make_listing(**overrides: Any) -> CleanListing:
    """A valid canonical listing; tests override what they exercise."""
    values: dict[str, Any] = {
        "platform": "nawy",
        "source_id": "1",
        "source_url": "https://example.com/1",
        "title_raw": "Villa for sale",
        "title_normalized": "Villa for sale",
        "description_normalized": "Nice villa with garden",
        "property_type": "villa",
        "size_m2": Decimal("300"),
        "plot_size_m2": Decimal("500"),
        "bedrooms": 4,
        "bathrooms": 3,
        "governorate": "Cairo",
        "city": "New Cairo",
        "city_canonical": "new_cairo",
        "compound": "Bellagio",
        "compound_key": "bellagio",
        "price_egp": Decimal("30000000"),
        "currency": "EGP",
    }
    values.update(overrides)
    return finalize_hashes(CleanListing.model_validate(values))


@pytest.fixture
def listing_factory():
    """The make_listing factory, injectable into tests."""
    return make_listing
