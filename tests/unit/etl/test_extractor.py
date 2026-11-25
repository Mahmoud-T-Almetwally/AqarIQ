"""Unit tests for src.etl.extractor — regex tier + LLM payload validation."""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

import src.etl.extractor as extractor_module
from src.core.exceptions import LLMAuthError, LLMError, LLMRateLimitError
from src.etl.extractor import (
    ExtractionCache,
    build_llm_batch,
    enrich_missing,
    extract_delivery_year,
    extract_finishing,
    extract_payment,
    parse_llm_payload,
    run_llm_extraction,
)
from tests.unit.etl.conftest import make_listing


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        return self._payload


class _FakeChatClient:
    """Scripted chat client: each call pops the next scripted action
    (an exception to raise or a payload to return)."""

    provider = "groq"

    def __init__(self, script: list[Any]) -> None:
        self.script = list(script)
        self.calls = 0

    async def chat(self, messages: list[dict[str, str]], **kwargs: Any) -> _FakeResponse:
        self.calls += 1
        action = self.script.pop(0) if self.script else _FakeResponse([])
        if isinstance(action, Exception):
            raise action
        return action


def _pending_listing(source_id: str) -> Any:
    """A listing the LLM tier would pick up: text present, fields missing."""
    return make_listing(
        source_id=source_id,
        title_raw=f"Unit {source_id}",
        title_normalized=f"Unit {source_id}",
        description_normalized=f"Description of unit {source_id}",
        finishing=None,
        delivery_year=None,
        payment_scheme=None,
    )


class TestFinishingExtraction:
    def test_english(self):
        assert extract_finishing("Apartment fully finished with AC") == "finished"
        assert extract_finishing("core & shell unit") == "core_and_shell"
        assert extract_finishing("Semi-finished for sale") == "semi_finished"
        assert extract_finishing("ultra lux finishing") == "finished_ultra"
        assert extract_finishing("white box delivery") == "white_box"

    def test_arabic(self):
        assert extract_finishing("شقة متشطب كامل") == "finished"
        assert extract_finishing("على الطوب والأحمر") == "core_and_shell"
        assert extract_finishing("نص تشطيب") == "semi_finished"

    def test_furnished_beats_finished(self):
        assert extract_finishing("fully finished and furnished") == "finished_furnished"

    def test_none(self):
        assert extract_finishing("Villa with garden") is None


class TestDeliveryYearExtraction:
    def test_english_verb_then_year(self):
        assert extract_delivery_year("Delivery 2028 by the developer") == 2028
        assert extract_delivery_year("ready to move 2025") is None or extract_delivery_year("Handover in 2027") == 2027

    def test_year_then_verb(self):
        assert extract_delivery_year("2027 delivery date") == 2027

    def test_arabic(self):
        assert extract_delivery_year("تسليم 2029") == 2029

    def test_out_of_range_rejected(self):
        assert extract_delivery_year("delivery 1999") is None

    def test_none(self):
        assert extract_delivery_year("great location") is None


class TestPaymentExtraction:
    def test_down_payment_million(self):
        down, years = extract_payment("With a First payment : 5,500,000, the rest in installments")
        assert down == Decimal("5500000")
        assert years is None

    def test_down_payment_compact(self):
        down, _ = extract_payment("down payment 5.5m only")
        assert down == Decimal("5500000")

    def test_installment_years(self):
        _, years = extract_payment("installments over 8 years")
        assert years == 8
        _, years = extract_payment("تقسيط علي 10 سنوات")
        assert years == 10

    def test_arabic_scale(self):
        down, _ = extract_payment("مقدم 2 مليون والباقي تقسيط")
        assert down == Decimal("2000000")

    def test_none(self):
        down, years = extract_payment("cash only, great price")
        assert down is None
        assert years is None


class TestEnrichMissing:
    def test_fills_missing_fields(self):
        listing = make_listing(
            finishing=None,
            delivery_year=None,
            down_payment_egp=None,
            installment_years=None,
            payment_scheme=None,
            description_normalized="Fully finished apartment, delivery 2027, "
            "down payment 2,000,000 and installments over 7 years",
        )
        result = enrich_missing(listing)
        assert result.finishing == "finished"
        assert result.delivery_year == 2027
        assert result.down_payment_egp == Decimal("2000000")
        assert result.installment_years == 7
        assert result.payment_scheme == "installments"
        assert result.extraction["finishing"]["source"] == "regex"

    def test_never_overwrites_portal_values(self):
        listing = make_listing(
            finishing="core_and_shell",
            delivery_year=2029,
            description_normalized="fully finished, delivery 2027",
        )
        result = enrich_missing(listing)
        assert result.finishing == "core_and_shell"
        assert result.delivery_year == 2029
        assert not result.extraction

    def test_skips_without_description(self):
        listing = make_listing(description_normalized=None, finishing=None)
        result = enrich_missing(listing)
        assert result.finishing is None


class TestLLMTier:
    def test_parse_llm_payload_valid(self):
        listings = [
            make_listing(source_id=str(i), title_raw=f"Unit {i}", title_normalized=f"Unit {i}")
            for i in range(3)
        ]
        payload = [
            {"i": 1, "finishing": "finished", "delivery_year": 2028},
            {"i": 2, "down_payment_egp": 500000, "installment_years": 6},
            {"i": 3, "finishing": "garbage", "delivery_year": 1999},  # invalid → dropped
        ]
        parsed = parse_llm_payload(payload, listings)
        assert set(parsed) == {listings[0].text_hash, listings[1].text_hash}
        assert parsed[listings[0].text_hash]["finishing"] == "finished"
        assert parsed[listings[1].text_hash]["installment_years"] == 6

    def test_parse_llm_payload_rejects_non_list(self):
        assert parse_llm_payload({"i": 1}, [make_listing()]) == {}

    def test_build_llm_batch_numbered(self):
        batch = build_llm_batch([make_listing(source_id="1"), make_listing(source_id="2")])
        assert "--- listing 1 ---" in batch
        assert "--- listing 2 ---" in batch

    def test_cache_roundtrip(self, tmp_path: Path):
        cache = ExtractionCache(tmp_path / "cache.json")
        cache.put("abc", {"finishing": "finished"})
        cache.save()
        reloaded = ExtractionCache(tmp_path / "cache.json")
        assert reloaded.get("abc") == {"finishing": "finished"}

    def test_cache_survives_garbage(self, tmp_path: Path):
        (tmp_path / "cache.json").write_text("not json", encoding="utf-8")
        cache = ExtractionCache(tmp_path / "cache.json")
        assert len(cache) == 0

    def test_cache_serializes_decimals(self, tmp_path: Path):
        cache = ExtractionCache(tmp_path / "cache.json")
        cache.put("k", {"down_payment_egp": Decimal("500000")})
        cache.save()
        reloaded = ExtractionCache(tmp_path / "cache.json")
        assert reloaded.get("k") == {"down_payment_egp": "500000"}
        assert json.loads((tmp_path / "cache.json").read_text())["k"]["down_payment_egp"] == "500000"


class TestLLMRateLimitPolicy:
    """The extractor owns the batch-level Groq policy: await short cooldowns,
    stop gracefully on long ones — never crash the pipeline run."""

    async def test_awaits_short_cooldown_and_retries(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(extractor_module, "RATE_LIMIT_WAIT_BUFFER_SECONDS", 0.0)
        listing = _pending_listing("1")
        client = _FakeChatClient(
            [
                LLMRateLimitError("groq rate limit hit", retry_after=0),
                _FakeResponse([{"i": 1, "finishing": "finished"}]),
            ]
        )
        result = await run_llm_extraction(
            [listing], tmp_path / "cache.json", settings=None,
            batch_size=8, client=client,
        )
        assert client.calls == 2  # one 429, one success
        assert result["stopped_reason"] is None
        assert result["requests"] == 1
        assert result["rate_limit_waits"] == 1
        assert result["fills"]["finishing"] == 1
        cache = ExtractionCache(tmp_path / "cache.json")
        assert cache.get(listing.text_hash) == {"finishing": "finished"}

    async def test_stops_gracefully_on_daily_quota_cooldown(self, tmp_path: Path):
        listing = _pending_listing("1")
        client = _FakeChatClient(
            [LLMRateLimitError("groq rate limit hit", retry_after=3600)]
        )
        result = await run_llm_extraction(
            [listing], tmp_path / "cache.json", settings=None,
            batch_size=8, client=client,
        )
        assert "daily token budget exhausted" in result["stopped_reason"]
        assert result["requests"] == 0
        assert result["fills"]["finishing"] == 0

    async def test_stops_on_auth_error_without_retry(self, tmp_path: Path):
        client = _FakeChatClient([LLMAuthError("rejected the API key")])
        result = await run_llm_extraction(
            [_pending_listing("1")], tmp_path / "cache.json", settings=None,
            batch_size=8, client=client,
        )
        assert "authentication" in result["stopped_reason"]
        assert client.calls == 1  # no retries on a bad key

    async def test_skips_failing_batches_then_stops_after_five(
        self, tmp_path: Path
    ):
        listings = [_pending_listing(str(i)) for i in range(6)]
        client = _FakeChatClient(
            [
                _FakeResponse([{"i": 1, "finishing": "finished"}]),
                LLMError("unreachable"),
                LLMError("unreachable"),
                LLMError("unreachable"),
                LLMError("unreachable"),
                LLMError("unreachable"),
            ]
        )
        result = await run_llm_extraction(
            listings, tmp_path / "cache.json", settings=None,
            batch_size=1, client=client,
        )
        assert result["batches_done"] == 1
        assert result["fills"]["finishing"] == 1
        assert "5 consecutive batch failures" in result["stopped_reason"]
        assert client.calls == 6

    async def test_resumes_from_existing_cache(self, tmp_path: Path):
        listing_a, listing_b = _pending_listing("1"), _pending_listing("2")
        cache = ExtractionCache(tmp_path / "cache.json")
        cache.put(listing_a.text_hash, {"finishing": "finished"})
        cache.save()
        client = _FakeChatClient([_FakeResponse([{"i": 1, "delivery_year": 2028}])])
        result = await run_llm_extraction(
            [listing_a, listing_b], tmp_path / "cache.json", settings=None,
            batch_size=8, client=client,
        )
        assert result["batches_total"] == 1  # listing_a already cached
        assert result["fills"]["delivery_year"] == 1
        assert result["fills"]["finishing"] == 0
        assert client.calls == 1
