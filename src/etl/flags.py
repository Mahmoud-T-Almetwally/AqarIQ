"""Data-quality flags — the phase's keep-not-delete policy instrument.

Every anomaly the cleaning rules detect is recorded as a named flag on the
listing (carried into ``properties.features`` / the object-lake raw payload
keeps the pre-flag truth). No row is ever dropped except structurally
malformed JSON (currently zero rows in the snapshot). Downstream consumers
decide what to trust:

* ``market_stats`` precompute excludes ``currency_converted``,
  ``price_suspect_low`` and ``price_suspect_high`` rows;
* enrichment and embeddings skip ``source_tier_card`` rows (no text);
* everything stays searchable.

Expected counts are the verification measurements from 2026-11-19 (phase
plan, Appendix A); ``scripts/data_quality_report.py`` checks the real flag
counts against them and fails on out-of-range values.
"""

from __future__ import annotations

from enum import StrEnum


class Flag(StrEnum):
    """Named data-quality flags recorded on cleaned listings."""

    CURRENCY_CONVERTED = "currency_converted"
    """USD-priced row converted to EGP at the documented manifest rate (16 expected)."""

    DOWN_PAYMENT_PLACEHOLDER = "down_payment_placeholder"
    """Portal placeholder down payment (<= EGP 1,000) nulled (92 expected)."""

    PRICE_SUSPECT_LOW = "price_suspect_low"
    """EGP/m2 below 0.4x compound-type median — the price-is-the-down-payment
    pattern (36 expected)."""

    PRICE_SUSPECT_HIGH = "price_suspect_high"
    """EGP/m2 above 3x compound-type median (21 expected)."""

    SOURCE_TIER_CARD = "source_tier_card"
    """PropertyFinder reduced-detail row: no description/agent/coordinates/images,
    therefore not enrichable (2,786 expected)."""

    COMPLETION_CONFLICT = "completion_conflict"
    """Marked ready/resale yet carrying a future delivery year — contract resale
    of an off-plan unit; status re-derived to off_plan (468 expected)."""

    PLOT_LT_SIZE = "plot_lt_size"
    """Plot size below built-up area; both values kept (305 expected)."""

    ZERO_BEDROOM_RESIDENTIAL = "zero_bedroom_residential"
    """Apartment/chalet with 0 bedrooms, non-studio (155 expected)."""

    MISSING_SIZE = "missing_size"
    """No size captured; excluded from price-per-m2 math (1 expected)."""

    PPM2_MISMATCH = "ppm2_mismatch"
    """Scraped price_per_m2 deviates > 5% from price/size (0 expected — invariant)."""


# Expected count ranges for the QA report (phase plan Appendix A).
EXPECTED_FLAG_COUNTS: dict[Flag, tuple[int, int]] = {
    Flag.CURRENCY_CONVERTED: (16, 16),
    Flag.DOWN_PAYMENT_PLACEHOLDER: (92, 92),
    Flag.PRICE_SUSPECT_LOW: (33, 39),
    Flag.PRICE_SUSPECT_HIGH: (18, 24),
    Flag.SOURCE_TIER_CARD: (2786, 2786),
    Flag.COMPLETION_CONFLICT: (468, 468),
    Flag.PLOT_LT_SIZE: (231, 231),
    Flag.ZERO_BEDROOM_RESIDENTIAL: (160, 166),
    Flag.MISSING_SIZE: (1, 1),
    Flag.PPM2_MISMATCH: (0, 0),
}

# Flags that disqualify a row from market_stats aggregates.
STATS_EXCLUDED_FLAGS: frozenset[Flag] = frozenset(
    {Flag.CURRENCY_CONVERTED, Flag.PRICE_SUSPECT_LOW, Flag.PRICE_SUSPECT_HIGH}
)

__all__ = ["Flag", "EXPECTED_FLAG_COUNTS", "STATS_EXCLUDED_FLAGS"]
