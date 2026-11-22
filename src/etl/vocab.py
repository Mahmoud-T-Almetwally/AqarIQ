"""Canonical vocabularies — Stage 3.

Exhaustive mapping tables covering every value the EDA observed in the
snapshot (32 raw property types, 10 finishing values, 5 completion statuses,
4 payment frequencies, 74 amenity strings). An unmapped value raises
:class:`UnmappedValueError` — the maps are the contract, and the test suite
asserts coverage over the committed fixture. Judgment calls (iVilla → villa,
Palace → villa, Bulk Sale Unit → building, Full Floor → apartment) retain a
marker in ``features["markers"]`` so the raw signal is never lost.

Canonical sets (phase plan, Appendix B):

* 18 property types, 7 finishing levels, 4 completion statuses
  (+ derivation rules), 3 payment schemes, 4 payment frequencies,
  ~54 amenity keys after merging true synonyms across platforms
  (Nanny Room = Maids Room; Garage ≈ Covered Parking ≈ Underground parking;
  A/C = Central A/C; …). Compound-level marketing noise (Finishing,
  Infrastructure, Livability, Project Area) maps to ``None`` and is dropped
  from amenities — the raw payload in the object lake keeps the truth.
"""

from __future__ import annotations

from src.etl.flags import Flag

SNAPSHOT_YEAR = 2026  # crawl window 2026-11-02..15; "future" = beyond it


class UnmappedValueError(ValueError):
    """A raw vocabulary value missing from the exhaustive maps."""


# ---------------------------------------------------------------------------
# Property type — 32 raw values (16 per platform) → 18 canonical
# ---------------------------------------------------------------------------

CANONICAL_PROPERTY_TYPES: frozenset[str] = frozenset(
    {
        "apartment", "studio", "duplex", "penthouse", "loft", "chalet", "roof",
        "cabin", "villa", "twin_house", "townhouse", "family_house",
        "hotel_apartment", "administrative", "medical", "retail", "office",
        "building", "land",
    }
)

# raw → (canonical, marker | None)
PROPERTY_TYPE_MAP: dict[str, tuple[str, str | None]] = {
    "Apartment": ("apartment", None),
    "Villa": ("villa", None),
    "Chalet": ("chalet", None),
    "Townhouse": ("townhouse", None),
    "Twinhouse": ("twin_house", None),          # Nawy spelling
    "Twin House": ("twin_house", None),         # PropertyFinder spelling
    "Duplex": ("duplex", None),
    "Studio": ("studio", None),
    "Penthouse": ("penthouse", None),
    "Loft": ("loft", None),
    "Cabin": ("cabin", None),
    "Family House": ("family_house", None),
    "Administrative": ("administrative", None),
    "Medical": ("medical", None),
    "Retail": ("retail", None),
    "Office": ("office", None),
    "Building": ("building", None),
    "iVilla": ("villa", "ivilla"),              # brand twin-villa layout
    "Palace": ("villa", "palace"),
    "Hotel Apartment": ("hotel_apartment", None),
    "Whole Building": ("building", None),
    "Bulk Sale Unit": ("building", "bulk_sale_unit"),
    "Full Floor": ("apartment", "full_floor"),
    "Roof": ("roof", None),
    "Land": ("land", None),
}


def map_property_type(raw: str | None) -> tuple[str, str | None]:
    """Raw portal type → (canonical, marker). Raises on unmapped values."""
    if raw is None:
        raise UnmappedValueError("property_type is required")
    entry = PROPERTY_TYPE_MAP.get(raw.strip())
    if entry is None:
        raise UnmappedValueError(f"unmapped property_type: {raw!r}")
    return entry


# ---------------------------------------------------------------------------
# Finishing — 10 raw values → 7 canonical levels
# ---------------------------------------------------------------------------

FINISHING_MAP: dict[str, str] = {
    "Not Finished Core Shell": "core_and_shell",
    "Semi Finished": "semi_finished",
    "PARTLY": "semi_finished",
    "Fully Finished": "finished",
    "Luxury": "finished",
    "Premium": "finished",
    "Fully Finished Furnished": "finished_furnished",
    "Ultra-luxury": "finished_ultra",
    "Flexi Finishing": "flexi",
    "White Box": "white_box",
}


def map_finishing(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = FINISHING_MAP.get(raw.strip())
    if value is None:
        raise UnmappedValueError(f"unmapped finishing: {raw!r}")
    return value


# ---------------------------------------------------------------------------
# Completion status — raw values + derivation rules
# ---------------------------------------------------------------------------

CANONICAL_COMPLETION_STATUSES: frozenset[str] = frozenset(
    {"off_plan_primary", "completed_primary", "off_plan", "ready"}
)

_RAW_COMPLETION_MAP: dict[str, str] = {
    "off_plan_primary": "off_plan_primary",
    "off_plan": "off_plan",  # source-ambiguous; kept separate deliberately
    "completed": "completed_primary",
    "completed_primary": "completed_primary",
    "ready": "ready",
}


def derive_completion_status(
    platform: str,
    raw_status: str | None,
    resale: bool | None,
    delivery_year: int | None,
) -> tuple[str | None, list[str]]:
    """Canonical completion status + any flags raised by the derivation.

    Rules (phase plan Appendix B):

    * ``completed`` / ``completed_primary`` (all such rows are resale=false)
      → ``completed_primary``;
    * Nawy ``ready`` with a future delivery year and ``resale`` → contract
      resale of an off-plan unit → ``off_plan`` + ``completion_conflict``
      (468 rows verified);
    * everything else maps 1:1; a missing status stays missing.
    """
    flags: list[str] = []
    if raw_status is None:
        return None, flags
    canonical = _RAW_COMPLETION_MAP.get(raw_status)
    if canonical is None:
        raise UnmappedValueError(f"unmapped completion_status: {raw_status!r}")
    if (
        platform == "nawy"
        and raw_status == "ready"
        and resale
        and delivery_year is not None
        and delivery_year > SNAPSHOT_YEAR
    ):
        return "off_plan", [Flag.COMPLETION_CONFLICT]
    return canonical, flags


# ---------------------------------------------------------------------------
# Payment scheme / frequency / furnished
# ---------------------------------------------------------------------------

CANONICAL_PAYMENT_SCHEMES: frozenset[str] = frozenset(
    {"cash", "installments", "cash_or_installments"}
)


def map_payment_scheme(payment_methods: list[str] | None) -> str | None:
    """Payment-method set → three-value scheme; empty set → None (2,786 rows)."""
    methods = {m for m in (payment_methods or []) if m}
    if not methods:
        return None
    if methods == {"cash"}:
        return "cash"
    if methods == {"installments"}:
        return "installments"
    if methods == {"cash", "installments"}:
        return "cash_or_installments"
    raise UnmappedValueError(f"unmapped payment_methods set: {sorted(methods)}")


PAYMENT_FREQUENCY_MAP: dict[str, str] = {
    "monthly": "monthly",
    "quarterly": "quarterly",
    "semi-annually": "semi_annually",  # DB comment vocabulary
    "annually": "annually",
}


def map_payment_frequency(raw: str | None) -> str | None:
    if raw is None:
        return None
    value = PAYMENT_FREQUENCY_MAP.get(raw.strip())
    if value is None:
        raise UnmappedValueError(f"unmapped payment_frequency: {raw!r}")
    return value


def map_furnished(raw: str | None) -> tuple[bool | None, list[str]]:
    """PF YES/NO/PARTLY → (bool, markers). PARTLY → True + furnished_partial."""
    if raw is None:
        return None, []
    value = raw.strip().upper()
    if value == "YES":
        return True, []
    if value == "NO":
        return False, []
    if value == "PARTLY":
        return True, ["furnished_partial"]
    raise UnmappedValueError(f"unmapped furnished: {raw!r}")


# ---------------------------------------------------------------------------
# Amenities — 74 raw strings → ~54 canonical keys (None = compound noise)
# ---------------------------------------------------------------------------

AMENITY_MAP: dict[str, str | None] = {
    # --- shared across platforms ---
    "Children's Pool": "children_pool",
    "Clubhouse": "clubhouse",
    "Elevator": "elevator",
    "Kitchen Appliances": "kitchen_appliances",
    "Private Garden": "private_garden",
    "Private Pool": "private_pool",
    "Shared Gym": "shared_gym",
    "Shared Spa": "shared_spa",
    # --- Nawy ---
    "4th Row": "fourth_row",
    "5th Row": "fifth_row",
    "A/C": "ac",
    "Barbecue Area": "barbecue_area",
    "Built-in Wardrobe": "builtin_wardrobe",
    "Business Hub": "business_hub",
    "Charging Electric Cars Units": "ev_charging",
    "Children's Play Area": "children_play_area",
    "Commercial strip": "commercial_strip",
    "Concierge": "concierge",
    "Disability support": "accessible",
    "Dressing Room": "dressing_room",
    "Driver Room": "driver_room",
    "First Row": "first_row",
    "Food Court": "food_court",
    "Garage": "parking",
    "Garden": "shared_garden",
    "Garden view": "garden_view",
    "Has roof": "has_roof",
    "Infrastructure": None,  # compound-level marketing noise
    "Jogging trail": "jogging_trail",
    "Kitchen Cabinets": "kitchen_cabinets",
    "Lagoon View": "lagoon_view",
    "Laundry room": "laundry_room",
    "Livability": None,  # compound-level marketing noise
    "Living Room": "living_room",
    "Medical center": "medical_center",
    "Mosque": "mosque",
    "Nanny Room": "nanny_room",
    "Not Sea View": "no_sea_view",
    "Outdoor pools": "shared_pool",
    "Pool View": "pool_view",
    "Private Gym": "private_gym",
    "Project Area": None,  # compound-level marketing noise
    "Schools": "schools",
    "Sea View": "sea_view",
    "Second Row": "second_row",
    "Sports Clubs": "sports",
    "Terrace": "terrace",
    "Third Row": "third_row",
    "Underground parking": "parking",
    "bicycles lanes": "bicycle_lanes",
    "Finishing": None,  # placeholder "finishing on request"
    # --- PropertyFinder ---
    "Central A/C": "ac",
    "Covered Parking": "parking",
    "Double Glazed Windows": "double_glazed",
    "Laundry Room": "laundry_room",
    "Lobby in Building": "lobby",
    "Maids Room": "nanny_room",
    "Medical Center": "medical_center",
    "Service Elevator": "service_elevator",
    "Shared Pool": "shared_pool",
    "Solar Panels": "solar_panels",
    "Sports Courts": "sports",
    "Storage Room": "storage_room",
    "Study": "study",
    "View of Landmark": "view_landmark",
    "View of Water": "view_water",
}


def map_amenity(raw: str) -> str | None:
    """One raw amenity string → canonical key (``None`` = noise, dropped)."""
    value = AMENITY_MAP.get(raw.strip())
    if value is None and raw.strip() not in AMENITY_MAP:
        raise UnmappedValueError(f"unmapped feature: {raw!r}")
    return value


def build_features(
    raw_features: list[str] | None,
    has_nanny_room: bool | None = None,
    markers: list[str] | None = None,
) -> dict[str, object]:
    """Normalized ``features`` JSONB dict: sorted canonical amenities + markers.

    Nawy's ``has_nanny_room`` boolean merges into the ``nanny_room`` amenity
    (the PF spider derived its boolean from the Maids Room feature — 1:1).
    """
    amenities = {canon for raw in raw_features or [] if (canon := map_amenity(raw)) is not None}
    if has_nanny_room:
        amenities.add("nanny_room")
    features: dict[str, object] = {"amenities": sorted(amenities)}
    if markers:
        features["markers"] = sorted(set(markers))
    return features


__all__ = [
    "AMENITY_MAP",
    "CANONICAL_COMPLETION_STATUSES",
    "CANONICAL_PAYMENT_SCHEMES",
    "CANONICAL_PROPERTY_TYPES",
    "FINISHING_MAP",
    "PAYMENT_FREQUENCY_MAP",
    "PROPERTY_TYPE_MAP",
    "SNAPSHOT_YEAR",
    "UnmappedValueError",
    "build_features",
    "derive_completion_status",
    "map_amenity",
    "map_finishing",
    "map_furnished",
    "map_payment_frequency",
    "map_payment_scheme",
    "map_property_type",
]
