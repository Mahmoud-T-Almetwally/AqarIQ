"""Location canonicalization — Stage 4.

The highest-leverage stage in the phase: aligning the two portals' city
vocabularies grows cross-platform compound overlap from 15 to 104
``(city, compound)`` pairs (166 matching on compound name alone — emitted as
a review list by the pipeline, never auto-merged).

Components:

* :data:`CITY_CANONICAL` — every raw city string observed in the snapshot
  (95 entries) → canonical key. Unmapped cities raise, like every
  vocabulary in this package.
* :data:`GOVERNORATE_CANONICAL` — the portals' spellings to standard
  governorates. PropertyFinder uses the region name "North Coast" where the
  governorate is Matrouh, and carries the typos "South Sainai",
  "El Esmailia" and "Al Daqahlya".
* :data:`CITY_TO_GOVERNORATE` — frozen co-occurrence table (PropertyFinder
  majority vote, zero conflicts after canonicalization) plus static entries
  for the 21 Nawy-only cities (phase plan Appendix C) and one correctness
  override: Borg El Arab is Alexandria, not "North Coast".
* :class:`CompoundRegistry` — two-pass registry keyed on
  ``(canonical city, normalized compound)`` collapsing the 24 case-variant
  and 34 whitespace-padded spellings; carries the most frequent raw variant
  as display name, Nawy's polygon as boundary and its centroid.
* Coordinate validation against the Egypt bounding box (0 violations in the
  snapshot — kept as a cheap invariant).
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from src.etl.arabic import normalize_name_key
from src.etl.vocab import UnmappedValueError

EGYPT_LAT = (21.5, 32.0)
EGYPT_LON = (24.5, 37.2)

# ---------------------------------------------------------------------------
# City canonicalization — 95 raw strings → canonical keys
# ---------------------------------------------------------------------------

CITY_CANONICAL: dict[str, str] = {
    # --- shared / Cairo & Giza new cities ---
    "6th of October City": "6_october",
    "6 October City": "6_october",
    "October Gardens": "october_gardens",
    "El Sheikh Zayed": "sheikh_zayed",
    "Sheikh Zayed City": "sheikh_zayed",
    "New Zayed": "new_zayed",
    "New Sphinx": "new_sphinx",
    "New Cairo": "new_cairo",
    "New Cairo City": "new_cairo",
    "6th settlement": "6th_settlement",
    "South New Cairo": "south_new_cairo",
    "Mostakbal City": "mostakbal_city",
    "Mostakbal City - Future City": "mostakbal_city",
    "New Capital City": "new_capital",
    "Golden Square": "golden_square",
    "North Investors": "north_investors",
    "South Investors": "south_investors",
    "Northern Expansion": "northern_expansion",
    "El Shorouk": "shorouk",
    "Shorouk City": "shorouk",
    "New Heliopolis": "new_heliopolis",
    "Heliopolis": "heliopolis",
    "Heliopolis - Masr El Gedida": "heliopolis",
    "Madinaty": "madinaty",
    "El Lotus": "el_lotus",
    "El Choueifat": "el_choueifat",
    "Badr City": "badr_city",
    "Noor City": "noor_city",
    "Nasr City": "nasr_city",
    "Mansheyat Al Bakri": "mansheyat_al_bakri",
    "Hay El Zaytoun": "hay_el_zaytoun",
    # --- Cairo core districts ---
    "Central Cairo": "central_cairo",
    "Downtown Cairo": "downtown_cairo",
    "Old Cairo": "old_cairo",
    "Maadi": "maadi",
    "Hay El Maadi": "maadi",
    "Mokattam": "mokattam",
    "Mohandessin": "mohandessin",
    "Dokki": "dokki",
    "El Nozha": "el_nozha",
    "Zamalek": "zamalek",
    "Ring Road": "ring_road",
    "Cairo - Ismailia Desert Road": "cairo_ismailia_desert_road",
    "Cairo Alexandria Desert Road": "cairo_alexandria_desert_road",
    # --- Giza / Alexandria ---
    "Al Agouza": "al_agouza",
    "Ganoub El Giza": "ganoub_el_giza",
    "Hadayek El Ahram": "hadayek_el_ahram",
    "Hay El Haram": "hay_el_haram",
    "Alexandria": "alexandria",
    "Alexandria Compounds": "alexandria",
    "Borg El Arab City": "borg_el_arab",
    "Qesm Borg El Arab": "borg_el_arab",
    "Hay Al Amereyah": "hay_al_amereyah",
    "Hay Sharq": "hay_sharq",
    "Hay Awal El Montazah": "hay_awal_el_montazah",
    "Hay Than El Montazah": "hay_than_el_montazah",
    "Hay Wasat": "hay_wasat",
    "New Mansoura": "new_mansoura",
    "Al Mansoura": "al_mansoura",
    "New Obour City": "new_obour",
    "Obour City": "obour",
    # --- Red Sea / Sinai / Suez ---
    "Ain Sokhna": "ain_sokhna",
    "Al Ain Al Sokhna": "ain_sokhna",
    "Ras Sudr": "ras_sudr",
    "Ras Sedr": "ras_sudr",
    "Sharm El Sheikh": "sharm_el_sheikh",
    "Hurghada": "hurghada",
    "El Gouna": "el_gouna",
    "Makadi": "makadi",
    "Sahl Hasheesh": "sahl_hasheesh",
    "Somabay": "somabay",
    # --- North Coast / Matrouh ---
    "Ras El Hekma": "ras_el_hekma",
    "Ras Al Hekma": "ras_el_hekma",
    "Sidi Abdel Rahman": "sidi_abdel_rahman",
    "Sidi Heneish": "sidi_heneish",
    "Al Alamein": "al_alamein",
    "Al Dabaa": "al_dabaa",
    "Qesm Ad Dabaah": "al_dabaa",
    "El-Hamam": "el_hamam",
    "Markaz Al Hamam": "el_hamam",
    "Marsa Matrouh": "marsa_matrouh",
    "Qesm Marsa Matrouh": "marsa_matrouh",
    "North Coast-Sahel": "north_coast_sahel",
    "North Coast Resorts": "north_coast_resorts",
    "Alam Al Roum": "alam_al_roum",
    "Ghazala Bay": "ghazala_bay",
    "Caesar Bay": "caesar_bay",
    "Mersea": "mersea",
    "Silver Beach": "silver_beach",
    "Fayed": "fayed",
}


def canonical_city(raw: str | None) -> str:
    """Raw city string → canonical key. Raises on unmapped values."""
    if not raw or not raw.strip():
        raise UnmappedValueError("city is required (100% coverage in snapshot)")
    key = normalize_name_key(raw) or ""
    canonical = CITY_CANONICAL.get(key, CITY_CANONICAL.get(raw.strip()))
    if canonical is None:
        raise UnmappedValueError(f"unmapped city: {raw!r}")
    return canonical


# ---------------------------------------------------------------------------
# Governorates — portal spellings → standard names
# ---------------------------------------------------------------------------

GOVERNORATE_CANONICAL: dict[str, str] = {
    "North Coast": "Matrouh",  # portal region name; governorate is Matrouh
    "South Sainai": "South Sinai",
    "El Esmailia": "Ismailia",
    "Al Daqahlya": "Dakahlia",
    "Cairo": "Cairo",
    "Giza": "Giza",
    "Alexandria": "Alexandria",
    "Suez": "Suez",
    "Red Sea": "Red Sea",
    "Qalyubia": "Qalyubia",
    "Matrouh": "Matrouh",
    "South Sinai": "South Sinai",
    "Ismailia": "Ismailia",
    "Dakahlia": "Dakahlia",
}


def canonical_governorate(raw: str | None) -> str:
    if raw is None:
        raise UnmappedValueError("governorate value is None")
    value = GOVERNORATE_CANONICAL.get(raw.strip())
    if value is None:
        raise UnmappedValueError(f"unmapped governorate: {raw!r}")
    return value


# City → canonical governorate. The first 54 entries are the frozen
# PropertyFinder co-occurrence majority (2026-11-19 extraction, zero
# conflicts after "North Coast" → Matrouh); the remaining 21 are the static
# entries for Nawy-only cities (phase plan Appendix C); borg_el_arab is a
# deliberate override of the portal's "North Coast" majority (3 rows).
CITY_TO_GOVERNORATE: dict[str, str] = {
    # --- frozen from PropertyFinder co-occurrence ---
    "6_october": "Giza",
    "ain_sokhna": "Suez",
    "al_agouza": "Giza",
    "al_alamein": "Matrouh",
    "al_dabaa": "Matrouh",
    "al_mansoura": "Dakahlia",
    "alexandria": "Alexandria",
    "badr_city": "Cairo",
    "borg_el_arab": "Alexandria",  # override: PF majority said "North Coast"
    "caesar_bay": "Matrouh",
    "cairo_alexandria_desert_road": "Giza",
    "cairo_ismailia_desert_road": "Cairo",
    "dokki": "Giza",
    "el_hamam": "Matrouh",
    "el_nozha": "Cairo",
    "fayed": "Ismailia",
    "ganoub_el_giza": "Giza",
    "hadayek_el_ahram": "Giza",
    "hay_al_amereyah": "Alexandria",
    "hay_awal_el_montazah": "Alexandria",
    "hay_el_haram": "Giza",
    "hay_el_zaytoun": "Cairo",
    "hay_sharq": "Alexandria",
    "hay_than_el_montazah": "Alexandria",
    "hay_wasat": "Alexandria",
    "heliopolis": "Cairo",
    "hurghada": "Red Sea",
    "maadi": "Cairo",
    "madinaty": "Cairo",
    "mansheyat_al_bakri": "Cairo",
    "marsa_matrouh": "Matrouh",
    "mersea": "Matrouh",
    "mohandessin": "Giza",
    "mokattam": "Cairo",
    "mostakbal_city": "Cairo",
    "nasr_city": "Cairo",
    "new_cairo": "Cairo",
    "new_capital": "Cairo",
    "new_heliopolis": "Cairo",
    "new_mansoura": "Dakahlia",
    "new_obour": "Qalyubia",
    "noor_city": "Cairo",
    "north_coast_resorts": "Matrouh",
    "obour": "Qalyubia",
    "ras_el_hekma": "Matrouh",
    "ras_sudr": "South Sinai",
    "ring_road": "Cairo",
    "sharm_el_sheikh": "South Sinai",
    "sheikh_zayed": "Giza",
    "shorouk": "Cairo",
    "sidi_abdel_rahman": "Matrouh",
    "sidi_heneish": "Matrouh",
    "silver_beach": "Matrouh",
    "zamalek": "Cairo",
    # --- static entries for Nawy-only cities (Appendix C) ---
    "6th_settlement": "Cairo",
    "alam_al_roum": "Matrouh",
    "central_cairo": "Cairo",
    "downtown_cairo": "Cairo",
    "el_choueifat": "Cairo",       # New Cairo district — flagged for review
    "el_gouna": "Red Sea",
    "el_lotus": "Cairo",           # New Cairo district
    "ghazala_bay": "Matrouh",
    "golden_square": "Cairo",      # New Capital district
    "makadi": "Red Sea",
    "new_sphinx": "Giza",          # flagged for review
    "new_zayed": "Giza",
    "north_coast_sahel": "Matrouh",
    "north_investors": "Cairo",    # New Capital district
    "northern_expansion": "Cairo",  # New Capital district
    "october_gardens": "Giza",
    "old_cairo": "Cairo",
    "sahl_hasheesh": "Red Sea",
    "somabay": "Red Sea",
    "south_investors": "Cairo",    # New Capital district
    "south_new_cairo": "Cairo",
}


def governorate_for_city(city_canonical: str) -> str:
    """Canonical governorate for a canonical city key."""
    value = CITY_TO_GOVERNORATE.get(city_canonical)
    if value is None:
        raise UnmappedValueError(f"no governorate mapping for city: {city_canonical!r}")
    return value


# ---------------------------------------------------------------------------
# Coordinates & polygons
# ---------------------------------------------------------------------------

def validate_coords(lat: float | None, lon: float | None) -> bool:
    """True when the point lies inside the Egypt bounding box."""
    if lat is None or lon is None:
        return False
    return EGYPT_LAT[0] <= lat <= EGYPT_LAT[1] and EGYPT_LON[0] <= lon <= EGYPT_LON[1]


def polygon_centroid(polygon: dict[str, Any]) -> tuple[float, float] | None:
    """Centroid of a GeoJSON Polygon's exterior ring (shoelace formula).

    Returns ``None`` for degenerate rings instead of raising — a malformed
    boundary must never kill the pipeline, only the boundary.
    """
    if polygon.get("type") != "Polygon":
        return None
    rings = polygon.get("coordinates") or []
    if not rings or not isinstance(rings[0], list) or len(rings[0]) < 4:
        return None
    ring = [(float(p[0]), float(p[1])) for p in rings[0] if len(p) >= 2]
    if len(ring) < 4:
        return None
    # Ensure closed ring (GeoJSON repeats the first point; be tolerant).
    if ring[0] != ring[-1]:
        ring.append(ring[0])
    area2 = 0.0
    cx = 0.0
    cy = 0.0
    for (x1, y1), (x2, y2) in zip(ring, ring[1:]):
        cross = x1 * y2 - x2 * y1
        area2 += cross
        cx += (x1 + x2) * cross
        cy += (y1 + y2) * cross
    if abs(area2) < 1e-12:
        return None
    area = area2 / 2.0
    return (round(cx / (6.0 * area), 6), round(cy / (6.0 * area), 6))  # (lng, lat)


def valid_polygon(polygon: dict[str, Any] | None) -> bool:
    """A usable polygon: exterior ring inside the Egypt bounding box."""
    if not polygon or polygon.get("type") != "Polygon":
        return False
    ring = (polygon.get("coordinates") or [[]])[0]
    if len(ring) < 4:
        return False
    return all(
        len(p) >= 2 and EGYPT_LON[0] <= float(p[0]) <= EGYPT_LON[1]
        and EGYPT_LAT[0] <= float(p[1]) <= EGYPT_LAT[1]
        for p in ring
    )


# ---------------------------------------------------------------------------
# Compound registry
# ---------------------------------------------------------------------------

class CompoundRegistry:
    """Two-pass registry of compounds keyed on (canonical city, compound key).

    Pass 1 (:meth:`collect`) ingests every located record; pass 2
    (:meth:`finalize`) resolves display names (most frequent raw variant),
    boundary polygons (Nawy's ``compound_geometry``) and centroids (polygon
    centroid, else the Nawy listing coordinates, which the verification pass
    proved to be one distinct point per compound).
    """

    def __init__(self) -> None:
        self._counts: Counter[tuple[str, str]] = Counter()
        self._displays: dict[tuple[str, str], Counter[str]] = {}
        self._polygons: dict[tuple[str, str], dict[str, Any]] = {}
        self._points: dict[tuple[str, str], tuple[float, float]] = {}
        self._final: dict[tuple[str, str], dict[str, Any]] = {}

    def collect(
        self,
        city_canonical: str,
        compound: str | None,
        latitude: float | None = None,
        longitude: float | None = None,
        compound_geometry: dict[str, Any] | None = None,
    ) -> None:
        if compound is None:
            return
        key = (city_canonical, normalize_name_key(compound) or "")
        if not key[1]:
            return
        self._counts[key] += 1
        self._displays.setdefault(key, Counter())[compound.strip()] += 1
        if compound_geometry is not None and key not in self._polygons:
            self._polygons[key] = compound_geometry
        if latitude is not None and longitude is not None and key not in self._points:
            self._points[key] = (longitude, latitude)

    def finalize(self) -> None:
        for key in self._counts:
            polygon = self._polygons.get(key) if self._polygons.get(key) and valid_polygon(
                self._polygons[key]
            ) else None
            centroid = polygon_centroid(polygon) if polygon else self._points.get(key)
            if centroid is not None and not validate_coords(centroid[1], centroid[0]):
                centroid = self._points.get(key)
            display = self._displays[key].most_common(1)[0][0]
            self._final[key] = {
                "city_canonical": key[0],
                "compound_key": key[1],
                "display_name": display,
                "listing_count": self._counts[key],
                "boundary": polygon,
                "centroid": list(centroid) if centroid else None,
            }

    def entry(self, city_canonical: str, compound_key: str) -> dict[str, Any] | None:
        return self._final.get((city_canonical, compound_key))

    def centroid(self, city_canonical: str, compound_key: str) -> tuple[float, float] | None:
        entry = self.entry(city_canonical, compound_key)
        if entry and entry["centroid"]:
            return (entry["centroid"][0], entry["centroid"][1])
        return None

    def __len__(self) -> int:
        return len(self._counts)

    def to_json(self) -> list[dict[str, Any]]:
        return [self._final[key] for key in sorted(self._final)]


__all__ = [
    "CITY_CANONICAL",
    "CITY_TO_GOVERNORATE",
    "CompoundRegistry",
    "EGYPT_LAT",
    "EGYPT_LON",
    "GOVERNORATE_CANONICAL",
    "canonical_city",
    "canonical_governorate",
    "governorate_for_city",
    "polygon_centroid",
    "validate_coords",
    "valid_polygon",
]
