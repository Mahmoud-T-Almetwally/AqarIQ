"""Entity resolution — Stage 7.

Three tiers, ordered from provably safe to judgment calls:

* **Tier A (same-platform exact)** — identical canonical city, compound,
  type, size, bedrooms and price within one platform. Verified in the
  snapshot: 47 groups / 94 rows (42 PropertyFinder, 5 Nawy developer
  inventory relistings).
* **Tier B (cross-platform exact)** — the same key across portals
  (4 groups / 8 rows verified, e.g. the Bellagio twin house listed on both).
* **Tier C (fuzzy, conservative)** — blocking key ``(compound, type)`` where
  size agrees within 5% AND price within 10% (both hard requirements, not
  votable signals — Nawy inventories carry templated titles and equal
  beds/baths across *different* units) plus at least one corroborating
  signal among bedrooms, bathrooms and trigram title similarity >= 0.55.
  Cross-platform pairs pass on those gates (a broker ad matching a
  developer listing at the same size and price is the same unit); within a
  platform a merge additionally requires a **different broker** — identical
  same-broker rows are sister units in the same phase, not relistings, and
  templated titles are never sufficient evidence on their own.

Merge policy: the property takes the best-populated field per the phase
plan's platform-precedence table (Nawy for developer/finishing/delivery,
PropertyFinder for broker/coordinates/text), its ``price_egp`` is the median
of merged listings, flags are the union, amenities the union. Every listing
keeps its own financials row and source URL (1:1 provenance). All Tier C
decisions are emitted to a review queue for manual sampling before seed.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover
    from src.etl.contracts import CleanListing

SIZE_TOLERANCE = Decimal("0.05")
PRICE_TOLERANCE = Decimal("0.10")
TITLE_SIM_THRESHOLD = 0.55


# ---------------------------------------------------------------------------
# Similarity
# ---------------------------------------------------------------------------

def trigram_similarity(a: str, b: str) -> float:
    """Jaccard similarity over character trigrams (pg_trgm in pure Python)."""
    if not a or not b:
        return 0.0
    ta = {a[i : i + 3] for i in range(len(a) - 2)}
    tb = {b[i : i + 3] for i in range(len(b) - 3 + 1)}
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def _within(value_a: Decimal | None, value_b: Decimal | None, tolerance: Decimal) -> bool | None:
    """True/False signal, or None when either side is missing."""
    if value_a is None or value_b is None:
        return None
    if value_b == 0 and value_a == 0:
        return True
    base = max(abs(value_a), abs(value_b))
    if base == 0:
        return None
    return abs(value_a - value_b) / base <= tolerance


def _equal(a: int | None, b: int | None) -> bool | None:
    if a is None or b is None:
        return None
    return a == b


@dataclass
class MatchSignals:
    size_ok: bool | None
    price_ok: bool | None
    beds_ok: bool | None
    baths_ok: bool | None
    title_ok: bool
    different_broker: bool


def compute_signals(a: CleanListing, b: CleanListing) -> MatchSignals:
    return MatchSignals(
        size_ok=_within(a.size_m2, b.size_m2, SIZE_TOLERANCE),
        price_ok=_within(a.price_egp, b.price_egp, PRICE_TOLERANCE),
        beds_ok=_equal(a.bedrooms, b.bedrooms),
        baths_ok=_equal(a.bathrooms, b.bathrooms),
        title_ok=trigram_similarity(a.title_normalized, b.title_normalized) >= TITLE_SIM_THRESHOLD,
        different_broker=bool(
            {a.broker_name, b.broker_name} != {None} and a.broker_name != b.broker_name
        ),
    )


def tier_c_match(a: CleanListing, b: CleanListing) -> MatchSignals | None:
    """Conservative Tier C gate — ``None`` when the pair cannot be the same unit.

    Size and price agreement are hard requirements, not votable signals:
    the first full-dataset run proved why. Nawy lists whole inventories with
    templated titles, equal beds/baths and *different* sizes and prices —
    as votable signals those rows produced 1,611 bogus merges (5,244
    "properties"). Size is the unit's defining physical attribute and the
    crawl window is two weeks (prices do not move >10% in it), so both must
    agree; beds/baths/title remain corroborating signals.
    """
    signals = compute_signals(a, b)
    if signals.size_ok is not True or signals.price_ok is not True:
        return None
    if not (signals.beds_ok or signals.baths_ok or signals.title_ok):
        return None
    return signals


# ---------------------------------------------------------------------------
# Union-Find over listing indices
# ---------------------------------------------------------------------------

class _DSU:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, x: int) -> int:
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def union(self, a: int, b: int) -> bool:
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        self.parent[rb] = ra
        return True


# ---------------------------------------------------------------------------
# Property drafts
# ---------------------------------------------------------------------------

@dataclass
class PropertyDraft:
    """One deduplicated physical unit + the listings that evidence it."""

    property_id: int
    members: list[int] = field(default_factory=list)
    # representative (merged) fields
    property_type: str = ""
    size_m2: Decimal | None = None
    plot_size_m2: Decimal | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None
    finishing: str | None = None
    furnished: bool | None = None
    delivery_year: int | None = None
    completion_status: str | None = None
    resale: bool | None = None
    developer_name: str | None = None
    governorate: str = ""
    city: str = ""
    city_canonical: str = ""
    compound: str | None = None
    compound_key: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    title_normalized: str = ""
    description_normalized: str | None = None
    features: dict[str, Any] = field(default_factory=dict)
    image_urls: list[str] = field(default_factory=list)
    floor_plan_urls: list[str] = field(default_factory=list)
    master_plan_url: str | None = None
    price_egp: Decimal | None = None
    flags: list[str] = field(default_factory=list)
    merged: bool = False

    def to_json(self) -> dict[str, Any]:
        payload = {k: v for k, v in self.__dict__.items() if k != "members"}
        payload["members"] = len(self.members)
        return payload


def _merge_group(group: list[CleanListing], property_id: int) -> PropertyDraft:
    """Best-populated field per platform precedence; median price; union flags."""
    nawy = [listing for listing in group if listing.platform == "nawy"]
    pf = [listing for listing in group if listing.platform == "property_finder"]
    # Nawy carries the inventory layer, PF the ad/text layer.
    inv = nawy[0] if nawy else group[0]
    text = next((listing for listing in pf if listing.description_normalized), None) or inv

    def first_non_null(attr: str) -> Any:
        for listing in group:
            if getattr(listing, attr) is not None:
                return getattr(listing, attr)
        return None

    sizes = [listing.size_m2 for listing in group if listing.size_m2 is not None]
    prices = [listing.price_egp for listing in group]
    amenities: set[str] = set(inv.features.get("amenities", []))
    markers: set[str] = set(inv.features.get("markers", []))
    for listing in group:
        amenities.update(listing.features.get("amenities", []))
        markers.update(listing.features.get("markers", []))
    images: list[str] = []
    for listing in [*pf, *nawy]:  # PF unit-level photos preferred over Nawy renders
        for url in listing.image_urls:
            if url not in images:
                images.append(url)
    floor_plans: list[str] = []
    for listing in group:
        for url in listing.floor_plan_urls:
            if url not in floor_plans:
                floor_plans.append(url)

    flags: list[str] = []
    for listing in group:
        for flag in listing.flags:
            if flag not in flags:
                flags.append(flag)

    geo = next((listing for listing in pf if listing.latitude is not None), None) or inv

    features: dict[str, Any] = {"amenities": sorted(amenities)}
    if markers:
        features["markers"] = sorted(markers)

    return PropertyDraft(
        property_id=property_id,
        members=[i for i in range(len(group))],  # replaced by caller with indices
        property_type=inv.property_type,
        size_m2=statistics.median(sizes) if sizes else None,  # type: ignore[arg-type]
        plot_size_m2=first_non_null("plot_size_m2"),
        bedrooms=first_non_null("bedrooms"),
        bathrooms=first_non_null("bathrooms"),
        finishing=first_non_null("finishing"),
        furnished=first_non_null("furnished"),
        delivery_year=first_non_null("delivery_year"),
        completion_status=first_non_null("completion_status"),
        resale=first_non_null("resale"),
        developer_name=first_non_null("developer_name"),
        governorate=inv.governorate,
        city=inv.city,
        city_canonical=inv.city_canonical,
        compound=inv.compound,
        compound_key=inv.compound_key,
        latitude=geo.latitude,
        longitude=geo.longitude,
        title_normalized=text.title_normalized,
        description_normalized=text.description_normalized,
        features=features,
        image_urls=images[:6],
        floor_plan_urls=floor_plans,
        master_plan_url=first_non_null("master_plan_url"),
        price_egp=statistics.median(prices) if prices else None,
        flags=flags,
        merged=len(group) > 1,
    )


@dataclass
class DedupResult:
    drafts: list[PropertyDraft]
    listings: list[CleanListing]
    stats: dict[str, Any]
    review_queue: list[dict[str, Any]]


def deduplicate(listings: list[CleanListing]) -> DedupResult:
    """Run tiers A/B/C and build property drafts; assigns ``property_id``."""
    dsu = _DSU(len(listings))
    stats: dict[str, Any] = {
        "tier_a_groups": 0,
        "tier_b_groups": 0,
        "tier_c_groups": 0,
        "tier_a_rows": 0,
        "tier_b_rows": 0,
        "tier_c_rows": 0,
    }
    review_queue: list[dict[str, Any]] = []

    # --- Tiers A + B: exact key ------------------------------------------------
    exact: dict[tuple[Any, ...], list[int]] = {}
    for idx, listing in enumerate(listings):
        if listing.size_m2 is None or listing.bedrooms is None:
            continue  # exact tier needs the full key
        key = (
            listing.city_canonical,
            listing.compound_key,
            listing.property_type,
            listing.size_m2,
            listing.bedrooms,
            listing.price_egp,
        )
        exact.setdefault(key, []).append(idx)
    for indices in exact.values():
        if len(indices) < 2:
            continue
        platforms = {listings[i].platform for i in indices}
        for i in indices[1:]:
            dsu.union(indices[0], i)
        if len(platforms) > 1:
            stats["tier_b_groups"] += 1
            stats["tier_b_rows"] += len(indices)
        else:
            stats["tier_a_groups"] += 1
            stats["tier_a_rows"] += len(indices)

    # --- Tier C: fuzzy within (compound, type) blocks --------------------------
    blocks: dict[tuple[str | None, str], list[int]] = {}
    for idx, listing in enumerate(listings):
        if listing.compound_key is None:
            continue
        blocks.setdefault((listing.compound_key, listing.property_type), []).append(idx)
    for block in blocks.values():
        if len(block) < 2:
            continue
        for pos, i in enumerate(block):
            for j in block[pos + 1 :]:
                if dsu.find(i) == dsu.find(j):
                    continue
                signals = tier_c_match(listings[i], listings[j])
                if signals is None:
                    continue
                same_platform = listings[i].platform == listings[j].platform
                if same_platform and not signals.different_broker:
                    continue  # sister units in the same phase, not a relist
                if dsu.union(i, j):
                    stats["tier_c_groups"] += 1
                    stats["tier_c_rows"] += 2
                    review_queue.append(
                        {
                            "members": [
                                {
                                    "platform": listings[k].platform,
                                    "source_id": listings[k].source_id,
                                    "title": listings[k].title_normalized[:80],
                                    "size_m2": str(listings[k].size_m2 or ""),
                                    "price_egp": str(listings[k].price_egp),
                                    "broker": listings[k].broker_name,
                                }
                                for k in (i, j)
                            ],
                            "signals": {
                                "size_ok": signals.size_ok,
                                "price_ok": signals.price_ok,
                                "beds_ok": signals.beds_ok,
                                "baths_ok": signals.baths_ok,
                                "title_ok": signals.title_ok,
                            },
                        }
                    )

    # --- group -> drafts -------------------------------------------------------
    groups: dict[int, list[int]] = {}
    for idx in range(len(listings)):
        groups.setdefault(dsu.find(idx), []).append(idx)

    drafts: list[PropertyDraft] = []
    assigned: list[CleanListing | None] = [None] * len(listings)
    ordered_keys = sorted(groups.values(), key=lambda members: members[0])
    for property_id, members in enumerate(ordered_keys, start=1):
        group = [listings[i] for i in members]
        draft = _merge_group(group, property_id)
        draft.members = members
        drafts.append(draft)
        for i in members:
            updated = listings[i].model_copy(update={"property_id": property_id})
            assigned[i] = updated

    stats["listings"] = len(listings)
    stats["properties"] = len(drafts)
    stats["merged_properties"] = sum(1 for d in drafts if d.merged)
    final_listings = [listing for listing in assigned if listing is not None]
    return DedupResult(
        drafts=drafts, listings=final_listings, stats=stats, review_queue=review_queue
    )


__all__ = [
    "DedupResult",
    "MatchSignals",
    "PropertyDraft",
    "compute_signals",
    "deduplicate",
    "trigram_similarity",
]
