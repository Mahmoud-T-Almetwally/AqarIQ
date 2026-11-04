"""Tests for src/services/rate_limit.py."""

from __future__ import annotations

import time

import pytest

from src.services.rate_limit import (
    DEFAULT_COOLDOWN_SECONDS,
    ProviderQuota,
    RateLimitRegistry,
    get_rate_limit_registry,
    parse_duration,
    parse_retry_after,
)


class TestParseRetryAfter:
    def test_seconds(self):
        assert parse_retry_after("30") == 30.0
    def test_seconds_with_whitespace(self):
        assert parse_retry_after("  15 ") == 15.0
    def test_decimal(self):
        assert parse_retry_after("2.5") == 2.5
    def test_negative_clamped_to_zero(self):
        assert parse_retry_after("-3") == 0.0
    def test_empty_is_none(self):
        assert parse_retry_after("") is None
        assert parse_retry_after(None) is None
    def test_garbage_is_none(self):
        assert parse_retry_after("soon-ish") is None
    def test_http_date(self):
        future = time.time() + 120
        date = time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(future))
        seconds = parse_retry_after(date)
        assert seconds is not None
        assert 110 < seconds <= 120


class TestParseDuration:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("47s", 47.0),
            ("1m30s", 90.0),
            ("1.5s", 1.5),
            ("2h", 7200.0),
            ("500ms", 0.5),
            ("10", 10.0),      # bare number = seconds
            ("1m", 60.0),
        ],
    )
    def test_values(self, raw, expected):
        assert parse_duration(raw) == expected

    def test_empty_is_none(self):
        assert parse_duration("") is None
        assert parse_duration(None) is None
    def test_garbage_is_none(self):
        assert parse_duration("fast") is None


class TestProviderQuota:
    def test_starts_available(self):
        quota = ProviderQuota(name="gemini")
        assert quota.available is True
        assert quota.seconds_until_reset() is None

    def test_exhaustion_window(self):
        quota = ProviderQuota(name="gemini")
        quota.mark_exhausted(30.0)
        assert quota.available is False
        reset = quota.seconds_until_reset()
        assert reset is not None
        assert 25 < reset <= 30

    def test_window_elapses_and_clears(self):
        quota = ProviderQuota(name="gemini")
        quota.mark_exhausted(0.05)
        assert quota.available is False
        time.sleep(0.06)
        assert quota.available is True
        assert quota.seconds_until_reset() is None

    def test_mark_exhausted_default_cooldown(self):
        quota = ProviderQuota(name="groq")
        quota.mark_exhausted(None)
        reset = quota.seconds_until_reset()
        assert reset is not None
        assert reset <= DEFAULT_COOLDOWN_SECONDS

    def test_headers_update_remaining(self):
        quota = ProviderQuota(name="gemini")
        quota.update_from_headers({
            "x-ratelimit-remaining-requests": "8",
            "x-ratelimit-remaining-tokens": "5000",
        })
        assert quota.remaining_requests == 8
        assert quota.remaining_tokens == 5000
        assert quota.available is True

    def test_zero_remaining_arms_cooldown_from_reset(self):
        quota = ProviderQuota(name="gemini")
        quota.update_from_headers({
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": "1m",
        })
        assert quota.available is False
        reset = quota.seconds_until_reset()
        assert reset is not None
        assert 55 < reset <= 60

    def test_zero_remaining_without_reset_uses_default(self):
        quota = ProviderQuota(name="groq")
        quota.update_from_headers({"x-ratelimit-remaining-requests": "0"})
        assert quota.available is False
        reset = quota.seconds_until_reset()
        assert reset is not None
        assert reset <= DEFAULT_COOLDOWN_SECONDS

    def test_success_clears_stale_cooldown(self):
        quota = ProviderQuota(name="gemini")
        quota.mark_exhausted(60.0)
        quota.update_from_headers({"x-ratelimit-remaining-requests": "10"})
        assert quota.available is True

    def test_partial_headers_only_update_what_is_present(self):
        quota = ProviderQuota(name="sr")
        quota.remaining_tokens = 999
        quota.update_from_headers({"x-ratelimit-remaining-requests": "3"})
        assert quota.remaining_requests == 3
        assert quota.remaining_tokens == 999  # untouched

    def test_case_insensitive_headers(self):
        quota = ProviderQuota(name="gemini")
        quota.update_from_headers({"X-RateLimit-Remaining-Requests": "7"})
        assert quota.remaining_requests == 7

    def test_malformed_values_ignored(self):
        quota = ProviderQuota(name="gemini")
        quota.update_from_headers({
            "x-ratelimit-remaining-requests": "many",
            "x-ratelimit-remaining-tokens": "??",
        })
        assert quota.remaining_requests is None
        assert quota.remaining_tokens is None
        assert quota.available is True

    def test_snapshot_shape(self):
        quota = ProviderQuota(name="gemini")
        quota.update_from_headers({"x-ratelimit-remaining-requests": "5"})
        snapshot = quota.snapshot()
        assert snapshot == {
            "available": True,
            "remaining_requests": 5,
            "remaining_tokens": None,
            "reset_in_seconds": None,
        }


class TestRegistry:
    def test_get_creates_once(self):
        registry = RateLimitRegistry()
        assert registry.get("gemini") is registry.get("gemini")

    def test_snapshot_lists_providers(self):
        registry = RateLimitRegistry()
        registry.get("gemini").mark_exhausted(10.0)
        registry.get("groq")
        snapshot = registry.snapshot()
        assert set(snapshot) == {"gemini", "groq"}
        assert snapshot["gemini"]["available"] is False
        assert snapshot["groq"]["available"] is True

    def test_reset_clears_everything(self):
        registry = RateLimitRegistry()
        registry.get("gemini").mark_exhausted(10.0)
        registry.reset()
        assert registry.snapshot() == {}

    def test_process_wide_registry_is_cached(self):
        assert get_rate_limit_registry() is get_rate_limit_registry()


class TestHeaderMapping:
    """Plain dict inputs work case-insensitively (httpx.Headers already are)."""

    def test_plain_dict_finds_header(self):
        quota = ProviderQuota(name="x")
        quota.update_from_headers({"X-RateLimit-Remaining-Requests": "42"})
        assert quota.remaining_requests == 42
