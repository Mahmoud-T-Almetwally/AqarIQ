"""Unit tests for src.etl.financials — Stage 5 rules."""

from __future__ import annotations

from decimal import Decimal

from src.etl.financials import (
    EGP_PER_USD,
    apply_financial_rules,
    detect_price_outliers,
)
from tests.unit.etl.conftest import make_listing


class TestCurrencyConversion:
    def test_usd_row_converted_and_flagged(self):
        listing = make_listing(currency="USD", price_egp=Decimal("1000000"))
        result = apply_financial_rules(listing)
        assert result.price_egp == (Decimal("1000000") * EGP_PER_USD).quantize(Decimal("0.01"))
        assert "currency_converted" in result.flags
        assert result.currency == "USD"  # source currency preserved

    def test_usd_down_payment_converted(self):
        listing = make_listing(
            currency="USD",
            price_egp=Decimal("1000000"),
            down_payment_egp=Decimal("100000"),
            installment_years=8,
        )
        result = apply_financial_rules(listing)
        assert result.down_payment_egp == (Decimal("100000") * EGP_PER_USD).quantize(Decimal("0.01"))

    def test_egp_row_untouched(self):
        listing = make_listing()
        result = apply_financial_rules(listing)
        assert result.price_egp == Decimal("30000000")
        assert "currency_converted" not in result.flags


class TestDownPaymentFloor:
    def test_placeholder_nulled(self):
        listing = make_listing(down_payment_egp=Decimal("25"))
        result = apply_financial_rules(listing)
        assert result.down_payment_egp is None
        assert "down_payment_placeholder" in result.flags

    def test_boundary_value_is_placeholder(self):
        listing = make_listing(down_payment_egp=Decimal("1000"))
        result = apply_financial_rules(listing)
        assert result.down_payment_egp is None

    def test_real_down_payment_kept(self):
        listing = make_listing(down_payment_egp=Decimal("3000000"))
        result = apply_financial_rules(listing)
        assert result.down_payment_egp == Decimal("3000000")


class TestPricePerM2:
    def test_computed(self):
        listing = make_listing(price_egp=Decimal("3000000"), size_m2=Decimal("100"))
        result = apply_financial_rules(listing)
        assert result.price_per_m2 == Decimal("30000.00")

    def test_scraped_within_tolerance_kept_as_computed(self):
        listing = make_listing(
            price_egp=Decimal("3000000"), size_m2=Decimal("100"), price_per_m2=Decimal("30500")
        )
        result = apply_financial_rules(listing)
        assert result.price_per_m2 == Decimal("30000.00")
        assert "ppm2_mismatch" not in result.flags

    def test_scraped_mismatch_flagged(self):
        listing = make_listing(
            price_egp=Decimal("3000000"), size_m2=Decimal("100"), price_per_m2=Decimal("40000")
        )
        result = apply_financial_rules(listing)
        assert "ppm2_mismatch" in result.flags

    def test_missing_size_flagged(self):
        listing = make_listing(size_m2=None, price_per_m2=Decimal("1000"))
        result = apply_financial_rules(listing)
        assert result.price_per_m2 is None
        assert "missing_size" in result.flags


class TestDerivedAndStructural:
    def test_monthly_payment(self):
        listing = make_listing(
            price_egp=Decimal("5000000"),
            down_payment_egp=Decimal("1000000"),
            installment_years=8,
        )
        result = apply_financial_rules(listing)
        assert result.monthly_payment_egp == Decimal("41666.67")

    def test_monthly_payment_absent_without_terms(self):
        result = apply_financial_rules(make_listing())
        assert result.monthly_payment_egp is None

    def test_plot_lt_size_flagged(self):
        listing = make_listing(plot_size_m2=Decimal("200"), size_m2=Decimal("300"))
        result = apply_financial_rules(listing)
        assert "plot_lt_size" in result.flags

    def test_zero_plot_nulled(self):
        listing = make_listing(plot_size_m2=Decimal("0"))
        result = apply_financial_rules(listing)
        assert result.plot_size_m2 is None

    def test_zero_bedroom_apartment_flagged(self):
        listing = make_listing(property_type="apartment", bedrooms=0)
        assert "zero_bedroom_residential" in apply_financial_rules(listing).flags

    def test_zero_bedroom_studio_not_flagged(self):
        listing = make_listing(property_type="studio", bedrooms=0)
        assert "zero_bedroom_residential" not in apply_financial_rules(listing).flags

    def test_zero_bedroom_office_not_flagged(self):
        listing = make_listing(property_type="office", bedrooms=0)
        assert "zero_bedroom_residential" not in apply_financial_rules(listing).flags


class TestOutliers:
    """detect_price_outliers consumes price_per_m2 computed by
    apply_financial_rules — the tests mirror the pipeline's stage order."""

    @staticmethod
    def _prepare(listings):
        return detect_price_outliers([apply_financial_rules(listing) for listing in listings])

    def test_low_outlier_flagged_in_group(self):
        listings = []
        for i in range(6):
            listings.append(
                make_listing(
                    source_id=str(i),
                    price_egp=Decimal("10000000"),
                    size_m2=Decimal("200"),  # 50,000 EGP/m2
                )
            )
        listings.append(
            make_listing(
                source_id="suspect",
                price_egp=Decimal("2000000"),  # 10,000 EGP/m2 — the dp pattern
                size_m2=Decimal("200"),
            )
        )
        result = self._prepare(listings)
        suspect = next(r for r in result if r.source_id == "suspect")
        assert "price_suspect_low" in suspect.flags
        assert all("price_suspect_low" not in r.flags for r in result if r.source_id != "suspect")

    def test_small_group_not_judged(self):
        listings = [
            make_listing(source_id=str(i), price_egp=Decimal("100"), size_m2=Decimal("200"))
            for i in range(3)
        ]
        result = self._prepare(listings)
        assert not any("price_suspect" in r.flags for r in result)

    def test_no_compound_no_outlier(self):
        listings = [
            make_listing(source_id=str(i), compound_key=None, compound=None,
                         price_egp=Decimal("100"), size_m2=Decimal("200"))
            for i in range(8)
        ]
        result = self._prepare(listings)
        assert not any("price_suspect" in r.flags for r in result)
