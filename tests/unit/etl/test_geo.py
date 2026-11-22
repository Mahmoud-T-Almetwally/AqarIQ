"""Unit tests for src.etl.geo — city/governorate canonicalization, geometry."""

from __future__ import annotations

import pytest

from src.etl.geo import (
    CITY_CANONICAL,
    CITY_TO_GOVERNORATE,
    CompoundRegistry,
    UnmappedValueError,
    canonical_city,
    canonical_governorate,
    governorate_for_city,
    polygon_centroid,
    validate_coords,
    valid_polygon,
)


class TestCityCanonicalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("New Cairo City", "new_cairo"),
            ("New Cairo", "new_cairo"),
            ("6th of October City", "6_october"),
            ("6 October City", "6_october"),
            ("El Sheikh Zayed", "sheikh_zayed"),
            ("Sheikh Zayed City", "sheikh_zayed"),
            ("Ras El Hekma", "ras_el_hekma"),
            ("Ras Al Hekma", "ras_el_hekma"),
            ("Al Ain Al Sokhna", "ain_sokhna"),
            ("Ain Sokhna", "ain_sokhna"),
            ("Mostakbal City - Future City", "mostakbal_city"),
            ("Heliopolis - Masr El Gedida", "heliopolis"),
            ("New Mansoura", "new_mansoura"),
            ("Al Mansoura", "al_mansoura"),
        ],
    )
    def test_variants_collapse(self, raw: str, expected: str):
        assert canonical_city(raw) == expected

    def test_unmapped_raises(self):
        with pytest.raises(UnmappedValueError):
            canonical_city("Atlantis")

    def test_every_mapped_city_has_governorate(self):
        """Invariant: no canonical city can appear without a governorate."""
        for canonical in CITY_CANONICAL.values():
            assert canonical in CITY_TO_GOVERNORATE, canonical

    def test_unmapped_city_raises_not_silently(self):
        with pytest.raises(UnmappedValueError):
            canonical_city(None)


class TestGovernorates:
    def test_portal_spellings_fixed(self):
        assert canonical_governorate("North Coast") == "Matrouh"
        assert canonical_governorate("South Sainai") == "South Sinai"
        assert canonical_governorate("El Esmailia") == "Ismailia"
        assert canonical_governorate("Al Daqahlya") == "Dakahlia"

    def test_nawy_derivation(self):
        assert governorate_for_city("el_gouna") == "Red Sea"
        assert governorate_for_city("ras_sudr") == "South Sinai"
        assert governorate_for_city("north_coast_sahel") == "Matrouh"
        assert governorate_for_city("new_zayed") == "Giza"

    def test_borg_el_arab_override(self):
        assert governorate_for_city("borg_el_arab") == "Alexandria"

    def test_unmapped_raises(self):
        with pytest.raises(UnmappedValueError):
            governorate_for_city("atlantis")


class TestCoordinates:
    def test_bbox(self):
        assert validate_coords(30.05, 31.24)  # Cairo
        assert not validate_coords(48.85, 2.35)  # Paris
        assert not validate_coords(None, 31.24)

    def test_polygon_centroid_square(self):
        polygon = {
            "type": "Polygon",
            "coordinates": [[[0.0, 0.0], [2.0, 0.0], [2.0, 2.0], [0.0, 2.0], [0.0, 0.0]]],
        }
        assert polygon_centroid(polygon) == (1.0, 1.0)

    def test_polygon_centroid_degenerate(self):
        assert polygon_centroid({"type": "Polygon", "coordinates": [[[0, 0], [1, 1]]]}) is None
        assert polygon_centroid({"type": "Polygon", "coordinates": []}) is None
        assert polygon_centroid({"type": "LineString", "coordinates": []}) is None

    def test_valid_polygon(self):
        good = {"type": "Polygon", "coordinates": [[[31.0, 30.0], [31.1, 30.0], [31.1, 30.1], [31.0, 30.1], [31.0, 30.0]]]}
        assert valid_polygon(good)
        bad = {"type": "Polygon", "coordinates": [[[131.0, 30.0], [131.1, 30.0], [131.1, 30.1], [131.0, 30.1]]]}
        assert not valid_polygon(bad)


class TestCompoundRegistry:
    def test_case_and_whitespace_collapse(self):
        registry = CompoundRegistry()
        registry.collect("sokhna", "Stella Di Mare 2", 29.7, 32.4)
        registry.collect("sokhna", "Stella di mare 2", 29.7, 32.4)
        registry.collect("sokhna", "Vida Mall Down Town ")
        registry.finalize()
        assert len(registry) == 2
        entry = registry.entry("sokhna", "stella di mare 2")
        assert entry is not None
        assert entry["listing_count"] == 2
        assert entry["display_name"] == "Stella Di Mare 2"
        assert entry["centroid"] == [32.4, 29.7]

    def test_polygon_from_nawy_only(self):
        registry = CompoundRegistry()
        polygon = {
            "type": "Polygon",
            "coordinates": [[[31.0, 30.0], [31.2, 30.0], [31.2, 30.2], [31.0, 30.2], [31.0, 30.0]]],
        }
        registry.collect("new_cairo", "Bellagio", compound_geometry=polygon)
        registry.finalize()
        entry = registry.entry("new_cairo", "bellagio")
        assert entry is not None
        assert entry["boundary"] is polygon
        assert entry["centroid"] == [31.1, 30.1]  # polygon centroid wins over points
