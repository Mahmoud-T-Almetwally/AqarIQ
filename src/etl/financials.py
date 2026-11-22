"""Financial integrity — Stage 5.

Money rules are conservative and flag-first: wrong code here corrupts the
product's headline numbers, so nothing is deleted and every intervention is
recorded via :mod:`src.etl.flags`.

* **USD conversion** — the 16 Nawy El Gouna rows priced in USD are converted
  at the single documented :data:`EGP_PER_USD` constant (flagged
  ``currency_converted``, excluded from market_stats). The alternative,
  dropping them, would bias the Red Sea slice.
* **Down-payment floor** — the 92 PropertyFinder placeholder down payments
  (<= EGP 1,000; observed values 25 and 50 EGP) are nulled and flagged.
* **Price per m²** — computed wherever size exists, cross-checked against
  the scraped value at 5% (the EDA found zero mismatches; any regression is
  an invariant failure).
* **Outliers** — per ``(compound, type)`` group with at least 5 rows:
  EGP/m² below 0.4x the group median → ``price_suspect_low`` (the verified
  price-is-actually-the-down-payment pattern), above 3x →
  ``price_suspect_high``. Flagged and stats-excluded, never dropped.
* **Derived fields** — monthly payment only when price, down payment and
  tenor are all present and consistent.

The conversion rate is the CBE official midpoint on 2026-11-19 (buy
53.7418 / sell 53.8797), recorded in the run manifest and re-runnable with
one constant change: https://www.cbe.org.eg/en/economic-research/statistics/cbe-exchange-rates
"""

from __future__ import annotations

import statistics
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING

from src.etl.flags import Flag

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers
    from src.etl.contracts import CleanListing

EGP_PER_USD = Decimal("53.81")
"""CBE official USD/EGP midpoint, 2026-11-19 (see module docstring)."""

DOWN_PAYMENT_FLOOR_EGP = Decimal("1000")
PPM2_TOLERANCE = Decimal("0.05")  # 5% cross-check vs scraped price_per_m2
OUTLIER_LOW_FACTOR = 0.4
OUTLIER_HIGH_FACTOR = 3.0
OUTLIER_MIN_GROUP = 5

# Residential types where 0 bedrooms is anomalous (studios are excluded —
# 81 of 103 observed studios legitimately have 0 bedrooms).
_ZERO_BEDROOM_TYPES = frozenset(
    {
        "apartment", "chalet", "duplex", "penthouse", "hotel_apartment",
        "twin_house", "townhouse", "villa", "family_house",
    }
)

_TWO_PLACES = Decimal("0.01")


def _q(value: Decimal) -> Decimal:
    return value.quantize(_TWO_PLACES, rounding=ROUND_HALF_UP)


def _add_flag(listing: CleanListing, flag: Flag) -> CleanListing:
    if flag not in listing.flags:
        return listing.model_copy(update={"flags": [*listing.flags, flag]})
    return listing


def apply_financial_rules(listing: CleanListing) -> CleanListing:
    """Per-row Stage-5 rules: conversion, floors, ppm², derived fields."""
    flags: list[str] = list(listing.flags)
    updates: dict[str, object] = {}

    price = listing.price_egp
    down_payment = listing.down_payment_egp

    # --- currency conversion (16 USD rows, all Nawy El Gouna) ---
    if listing.currency == "USD":
        price = _q(price * EGP_PER_USD)
        if down_payment is not None:
            down_payment = _q(down_payment * EGP_PER_USD)
        updates["price_egp"] = price
        updates["down_payment_egp"] = down_payment
        if Flag.CURRENCY_CONVERTED not in flags:
            flags.append(Flag.CURRENCY_CONVERTED)

    # --- placeholder down payments (92 PF rows) ---
    if down_payment is not None and down_payment <= DOWN_PAYMENT_FLOOR_EGP:
        down_payment = None
        updates["down_payment_egp"] = None
        if Flag.DOWN_PAYMENT_PLACEHOLDER not in flags:
            flags.append(Flag.DOWN_PAYMENT_PLACEHOLDER)
    elif down_payment is not None and "down_payment_egp" not in updates:
        updates["down_payment_egp"] = down_payment

    # --- plot size: zero is an artifact, not a measurement ---
    if listing.plot_size_m2 is not None and listing.plot_size_m2 <= 0:
        updates["plot_size_m2"] = None

    # --- price per m² ---
    if listing.size_m2 is None or listing.size_m2 <= 0:
        if Flag.MISSING_SIZE not in flags:
            flags.append(Flag.MISSING_SIZE)
        updates["price_per_m2"] = None
    else:
        computed = _q(price / listing.size_m2)
        if listing.price_per_m2 is not None:
            scraped = listing.price_per_m2
            diff = abs(computed - scraped) / scraped if scraped else Decimal(0)
            if diff > PPM2_TOLERANCE and Flag.PPM2_MISMATCH not in flags:
                flags.append(Flag.PPM2_MISMATCH)
        updates["price_per_m2"] = computed

    # --- plot below built-up area (305 rows; both values kept) ---
    if (
        listing.plot_size_m2 is not None
        and listing.plot_size_m2 > 0
        and listing.size_m2 is not None
        and listing.plot_size_m2 < listing.size_m2
        and Flag.PLOT_LT_SIZE not in flags
    ):
        flags.append(Flag.PLOT_LT_SIZE)

    # --- zero-bedroom residential (non-studio) ---
    if (
        listing.bedrooms == 0
        and listing.property_type in _ZERO_BEDROOM_TYPES
        and Flag.ZERO_BEDROOM_RESIDENTIAL not in flags
    ):
        flags.append(Flag.ZERO_BEDROOM_RESIDENTIAL)

    # --- monthly payment, only when the terms are complete ---
    if (
        down_payment is not None
        and listing.installment_years
        and listing.installment_years > 0
        and price > down_payment
    ):
        months = listing.installment_years * 12
        updates["monthly_payment_egp"] = _q((price - down_payment) / months)
    else:
        updates["monthly_payment_egp"] = None

    updates["flags"] = flags
    return listing.model_copy(update=updates)


def detect_price_outliers(listings: list[CleanListing]) -> list[CleanListing]:
    """Group pass: flag EGP/m² outliers against compound-type medians.

    Groups with fewer than :data:`OUTLIER_MIN_GROUP` rows are skipped — a
    singleton listing defines its own median and everything would be an
    outlier. Returns new objects for flagged rows (does not mutate inputs).
    """
    groups: dict[tuple[str | None, str], list[int]] = {}
    ppm2: dict[int, Decimal] = {}
    for idx, listing in enumerate(listings):
        if (
            listing.compound_key
            and listing.price_per_m2 is not None
            and listing.price_per_m2 > 0
        ):
            key = (listing.compound_key, listing.property_type)
            groups.setdefault(key, []).append(idx)
            ppm2[idx] = listing.price_per_m2

    flagged: set[int] = set()
    for group_key, indices in groups.items():
        if len(indices) < OUTLIER_MIN_GROUP:
            continue
        median = statistics.median(ppm2[i] for i in indices)
        for i in indices:
            value = ppm2[i]
            if value < Decimal(str(OUTLIER_LOW_FACTOR)) * median:
                flagged.add(i)
            elif value > Decimal(str(OUTLIER_HIGH_FACTOR)) * median:
                flagged.add(i)

    out: list[CleanListing] = []
    for idx, listing in enumerate(listings):
        if idx not in flagged:
            out.append(listing)
            continue
        median = statistics.median(
            ppm2[i] for i in groups[(listing.compound_key, listing.property_type)]
        )
        low = ppm2[idx] < Decimal(str(OUTLIER_LOW_FACTOR)) * median
        flag = Flag.PRICE_SUSPECT_LOW if low else Flag.PRICE_SUSPECT_HIGH
        out.append(_add_flag(listing, flag))
    return out


__all__ = [
    "DOWN_PAYMENT_FLOOR_EGP",
    "EGP_PER_USD",
    "OUTLIER_HIGH_FACTOR",
    "OUTLIER_LOW_FACTOR",
    "OUTLIER_MIN_GROUP",
    "PPM2_TOLERANCE",
    "apply_financial_rules",
    "detect_price_outliers",
]
