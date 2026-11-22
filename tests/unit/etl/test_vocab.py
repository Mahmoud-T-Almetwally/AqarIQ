"""Unit tests for src.etl.vocab — exhaustive canonical mappings."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.etl.vocab import (
    AMENITY_MAP,
    CANONICAL_PROPERTY_TYPES,
    PROPERTY_TYPE_MAP,
    UnmappedValueError,
    build_features,
    derive_completion_status,
    map_amenity,
    map_finishing,
    map_furnished,
    map_payment_frequency,
    map_payment_scheme,
    map_property_type,
)

FIXTURE = Path(__file__).parents[2] / "fixtures" / "sample_listings.jsonl"


@pytest.fixture(scope="module")
def fixture_rows() -> list[dict]:
    return [json.loads(line) for line in FIXTURE.open(encoding="utf-8")]


class TestPropertyType:
    def test_cross_platform_twin_house(self):
        assert map_property_type("Twinhouse")[0] == map_property_type("Twin House")[0] == "twin_house"

    def test_ivilla_maps_to_villa_with_marker(self):
        canonical, marker = map_property_type("iVilla")
        assert canonical == "villa"
        assert marker == "ivilla"

    def test_judgment_calls(self):
        assert map_property_type("Palace") == ("villa", "palace")
        assert map_property_type("Bulk Sale Unit") == ("building", "bulk_sale_unit")
        assert map_property_type("Full Floor") == ("apartment", "full_floor")
        assert map_property_type("Whole Building") == ("building", None)

    def test_every_canonical_value_is_valid(self):
        for canonical, _ in PROPERTY_TYPE_MAP.values():
            assert canonical in CANONICAL_PROPERTY_TYPES

    def test_unmapped_raises(self):
        with pytest.raises(UnmappedValueError):
            map_property_type("Spaceship")

    def test_fixture_values_all_map(self, fixture_rows):
        for row in fixture_rows:
            map_property_type(row["property_type"])  # raises if unmapped


class TestFinishing:
    def test_all_ten_raw_values(self):
        assert map_finishing("Not Finished Core Shell") == "core_and_shell"
        assert map_finishing("Semi Finished") == "semi_finished"
        assert map_finishing("PARTLY") == "semi_finished"
        assert map_finishing("Fully Finished") == "finished"
        assert map_finishing("Luxury") == "finished"
        assert map_finishing("Premium ") == "finished"  # trailing space in raw
        assert map_finishing("Ultra-luxury") == "finished_ultra"
        assert map_finishing("Fully Finished Furnished") == "finished_furnished"
        assert map_finishing("Flexi Finishing") == "flexi"
        assert map_finishing("White Box") == "white_box"
        assert map_finishing(None) is None

    def test_unmapped_raises(self):
        with pytest.raises(UnmappedValueError):
            map_finishing("Deluxe")


class TestCompletion:
    def test_pf_completed_maps(self):
        assert derive_completion_status("property_finder", "completed", False, None) == (
            "completed_primary",
            [],
        )

    def test_nawy_ready_delivered(self):
        assert derive_completion_status("nawy", "ready", True, 2024) == ("ready", [])

    def test_contract_resale_conflict(self):
        status, flags = derive_completion_status("nawy", "ready", True, 2029)
        assert status == "off_plan"
        assert "completion_conflict" in flags

    def test_none_stays_none(self):
        assert derive_completion_status("nawy", None, None, None) == (None, [])


class TestPayment:
    def test_schemes(self):
        assert map_payment_scheme(["cash"]) == "cash"
        assert map_payment_scheme(["installments"]) == "installments"
        assert map_payment_scheme(["cash", "installments"]) == "cash_or_installments"
        assert map_payment_scheme(["installments", "cash"]) == "cash_or_installments"
        assert map_payment_scheme([]) is None
        assert map_payment_scheme(None) is None

    def test_frequency(self):
        assert map_payment_frequency("semi-annually") == "semi_annually"
        assert map_payment_frequency("quarterly") == "quarterly"
        assert map_payment_frequency(None) is None

    def test_unmapped_methods_raise(self):
        with pytest.raises(UnmappedValueError):
            map_payment_scheme(["crypto"])


class TestFurnished:
    def test_yes_no(self):
        assert map_furnished("YES") == (True, [])
        assert map_furnished("NO") == (False, [])

    def test_partly(self):
        assert map_furnished("PARTLY") == (True, ["furnished_partial"])

    def test_none(self):
        assert map_furnished(None) == (None, [])


class TestAmenities:
    def test_cross_platform_synonyms(self):
        assert map_amenity("Nanny Room") == map_amenity("Maids Room") == "nanny_room"
        assert map_amenity("Garage") == map_amenity("Covered Parking") == map_amenity("Underground parking")
        assert map_amenity("A/C") == map_amenity("Central A/C") == "ac"
        assert map_amenity("Outdoor pools") == map_amenity("Shared Pool") == "shared_pool"
        assert map_amenity("Sports Clubs") == map_amenity("Sports Courts") == "sports"

    def test_noise_dropped(self):
        assert map_amenity("Infrastructure") is None
        assert map_amenity("Livability") is None

    def test_padded_strings(self):
        assert map_amenity("Clubhouse ") == "clubhouse"
        assert map_amenity("Commercial strip ") == "commercial_strip"

    def test_beach_rows_distinct(self):
        keys = {map_amenity(v) for v in ("First Row", "Second Row", "Third Row", "4th Row", "5th Row")}
        assert keys == {"first_row", "second_row", "third_row", "fourth_row", "fifth_row"}

    def test_unmapped_raises(self):
        with pytest.raises(UnmappedValueError):
            map_amenity("Helipad")

    def test_fixture_features_all_map(self, fixture_rows):
        for row in fixture_rows:
            for feature in row.get("features") or []:
                map_amenity(feature)

    def test_amenity_map_covers_itself(self):
        # keys must be stripped forms (lookup uses .strip())
        for key in AMENITY_MAP:
            assert key == key.strip()


class TestBuildFeatures:
    def test_merges_nanny_room_boolean(self):
        features = build_features(["Nanny Room", "Shared Gym"], has_nanny_room=True, markers=["ivilla"])
        assert features["amenities"] == ["nanny_room", "shared_gym"]
        assert features["markers"] == ["ivilla"]

    def test_empty(self):
        assert build_features([]) == {"amenities": []}
