"""Tests for src/services/llm_client.py.

Uses httpx2.MockTransport — no real network. Retry tests patch
``asyncio.sleep`` so backoff costs nothing.
"""

from __future__ import annotations

import inspect
import asyncio
import json

import httpx2
import pytest
from pydantic import SecretStr

from src.core.config import Settings
from src.core.exceptions import (
    ConfigurationError,
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMTimeoutError,
)
from src.services.llm_client import LLMClient, LLMResponse, make_llm_clients
from src.services.rate_limit import RateLimitRegistry

BASE_URL = "https://provider.test/v1"
MESSAGES = [{"role": "user", "content": "hello"}]


def ok_body(content: str = "hi there") -> dict:
    return {
        "choices": [
            {"message": {"content": content}, "finish_reason": "stop"}
        ],
        "model": "test-model",
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }


@pytest.fixture
def settings(make_settings) -> Settings:
    # No retries by default: deterministic single-shot behavior; retry
    # specifics are covered in the dedicated retry tests below.
    return make_settings(llm_max_retries=0, llm_timeout_seconds=5.0)


@pytest.fixture
def no_sleep(monkeypatch):
    async def _sleep(_seconds):
        return None

    monkeypatch.setattr(asyncio, "sleep", _sleep)


@pytest.fixture
def make_client(settings):
    """Build a client backed by a MockTransport; returns the client and a
    request journal (each entry is the httpx2.Request)."""

    def _factory(
        handler,
        *,
        provider="gemini",
        base_url=BASE_URL,
        api_key="test-key",
        model="test-model",
        registry=None,
    ):
        journal: list[httpx2.Request] = []

        async def _handler(request: httpx2.Request) -> httpx2.Response:
            journal.append(request)
            return await handler(request) if _is_coroutine(handler) else handler(request)

        http = httpx2.AsyncClient(
            transport=httpx2.MockTransport(_handler),
            timeout=httpx2.Timeout(settings.llm_timeout_seconds),
        )
        client = LLMClient(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            default_model=model,
            settings=settings,
            registry=registry or RateLimitRegistry(),
            http_client=http,
        )
        return client, journal

    return _factory


def _is_coroutine(fn) -> bool:
    return inspect.iscoroutinefunction(fn)


class TestHappyPath:
    async def test_returns_parsed_completion(self, make_client):
        client, journal = make_client(
            lambda request: httpx2.Response(200, json=ok_body("Berlin rocks"))
        )
        response = await client.chat(MESSAGES)
        assert response == LLMResponse(
            content="Berlin rocks",
            model="test-model",
            provider="gemini",
            finish_reason="stop",
            usage={"prompt_tokens": 10, "completion_tokens": 5},
        )

    async def test_request_shape(self, make_client):
        client, journal = make_client(lambda r: httpx2.Response(200, json=ok_body()))
        await client.chat(
            MESSAGES, model="other-model", temperature=0.7, max_tokens=99,
            response_json=True,
        )
        request = journal[0]
        assert str(request.url) == f"{BASE_URL}/chat/completions"
        assert request.headers["authorization"] == "Bearer test-key"
        payload = json.loads(request.content)
        assert payload["model"] == "other-model"
        assert payload["messages"] == MESSAGES
        assert payload["temperature"] == 0.7
        assert payload["stream"] is False
        assert payload["max_tokens"] == 99
        assert payload["response_format"] == {"type": "json_object"}

    async def test_default_model_used_when_not_overridden(self, make_client):
        client, journal = make_client(lambda r: httpx2.Response(200, json=ok_body()))
        await client.chat(MESSAGES)
        payload = json.loads(journal[0].content)
        assert payload["model"] == "test-model"

    async def test_quota_updated_from_headers(self, make_client):
        client, _ = make_client(
            lambda r: httpx2.Response(
                200,
                json=ok_body(),
                headers={"x-ratelimit-remaining-requests": "5"},
            )
        )
        await client.chat(MESSAGES)
        assert client.quota.remaining_requests == 5
        assert client.quota.available is True


class TestRateLimitIntegration:
    async def test_429_raises_with_retry_after(self, make_client):
        client, journal = make_client(
            lambda r: httpx2.Response(429, headers={"retry-after": "42"})
        )
        with pytest.raises(LLMRateLimitError) as exc_info:
            await client.chat(MESSAGES)
        assert exc_info.value.retry_after == 42
        assert len(journal) == 1

    async def test_429_arms_fail_fast(self, make_client):
        """After a 429, the next call must not touch the network."""
        client, journal = make_client(
            lambda r: httpx2.Response(429, headers={"retry-after": "60"})
        )
        with pytest.raises(LLMRateLimitError):
            await client.chat(MESSAGES)
        with pytest.raises(LLMRateLimitError) as second:
            await client.chat(MESSAGES)
        assert second.value.retry_after is not None
        assert len(journal) == 1  # no second HTTP request

    async def test_tracked_exhaustion_fails_fast(self, make_client):
        """remaining=0 in a 200 response arms the cooldown (fail fast)."""
        client, journal = make_client(
            lambda r: httpx2.Response(
                200,
                json=ok_body(),
                headers={
                    "x-ratelimit-remaining-requests": "0",
                    "x-ratelimit-reset-requests": "30s",
                },
            )
        )
        await client.chat(MESSAGES)  # success, but budget is now zero
        with pytest.raises(LLMRateLimitError):
            await client.chat(MESSAGES)
        assert len(journal) == 1


class TestErrorMapping:
    async def test_auth_error_is_immediate(self, make_client):
        client, journal = make_client(lambda r: httpx2.Response(401))
        with pytest.raises(LLMAuthError):
            await client.chat(MESSAGES)
        assert len(journal) == 1  # never retried

    async def test_non_retryable_status_is_terminal(self, make_client):
        client, journal = make_client(
            lambda r: httpx2.Response(404, text="no such model")
        )
        with pytest.raises(LLMError) as exc_info:
            await client.chat(MESSAGES)
        assert "404" in str(exc_info.value)
        assert len(journal) == 1

    async def test_empty_completion_is_format_error(self, make_client):
        client, _ = make_client(
            lambda r: httpx2.Response(200, json=ok_body("  "))
        )
        with pytest.raises(LLMResponseFormatError, match="empty completion"):
            await client.chat(MESSAGES)

    async def test_missing_choices_is_format_error(self, make_client):
        client, _ = make_client(lambda r: httpx2.Response(200, json={"ok": True}))
        with pytest.raises(LLMResponseFormatError, match="choices/content"):
            await client.chat(MESSAGES)

    async def test_non_json_body_is_format_error(self, make_client):
        client, _ = make_client(
            lambda r: httpx2.Response(200, text="<html>nope</html>")
        )
        with pytest.raises(LLMResponseFormatError, match="non-JSON body"):
            await client.chat(MESSAGES)


class TestRetries:
    async def test_retryable_status_then_success(self, make_client, no_sleep):
        calls = {"n": 0}

        def handler(request):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx2.Response(503)
            return httpx2.Response(200, json=ok_body("recovered"))

        client, journal = make_client(handler)
        client.settings.llm_max_retries = 2
        response = await client.chat(MESSAGES)
        assert response.content == "recovered"
        assert calls["n"] == 2

    async def test_retryable_status_exhausted(self, make_client, no_sleep):
        client, journal = make_client(lambda r: httpx2.Response(502))
        client.settings.llm_max_retries = 2
        with pytest.raises(LLMError, match="after 3 attempts"):
            await client.chat(MESSAGES)
        assert len(journal) == 3

    async def test_timeout_final_attempt(self, make_client, no_sleep):
        async def handler(request):
            raise httpx2.ConnectTimeout("too slow")

        client, journal = make_client(handler)
        client.settings.llm_max_retries = 1
        with pytest.raises(LLMTimeoutError):
            await client.chat(MESSAGES)
        assert len(journal) == 2

    async def test_connection_error_final_attempt(self, make_client, no_sleep):
        async def handler(request):
            raise httpx2.ConnectError("dns is a myth")

        client, journal = make_client(handler)
        client.settings.llm_max_retries = 1
        with pytest.raises(LLMError, match="unreachable"):
            await client.chat(MESSAGES)
        assert len(journal) == 2

    async def test_rate_limit_not_retried_even_with_budget(
        self, make_client, no_sleep
    ):
        """A 429 must fail fast — retries would only burn more quota."""
        client, journal = make_client(
            lambda r: httpx2.Response(429, headers={"retry-after": "5"})
        )
        client.settings.llm_max_retries = 3
        with pytest.raises(LLMRateLimitError):
            await client.chat(MESSAGES)
        assert len(journal) == 1


class TestSharedHttpClient:
    async def test_providers_keep_own_host_and_key(self, settings):
        """One pooled client, two providers: hosts and keys stay correct."""
        seen = []

        async def handler(request: httpx2.Request) -> httpx2.Response:
            seen.append((request.url.host, request.headers["authorization"]))
            return httpx2.Response(200, json=ok_body())

        http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        try:
            registry = RateLimitRegistry()
            gemini = LLMClient(
                provider="gemini",
                base_url="https://gemini.test/v1",
                api_key="key-gemini",
                default_model="g1",
                settings=settings,
                registry=registry,
                http_client=http,
            )
            groq = LLMClient(
                provider="groq",
                base_url="https://groq.test/v1",
                api_key="key-groq",
                default_model="q1",
                settings=settings,
                registry=registry,
                http_client=http,
            )
            await gemini.chat(MESSAGES)
            await groq.chat(MESSAGES)
        finally:
            await http.aclose()
        assert seen == [
            ("gemini.test", "Bearer key-gemini"),
            ("groq.test", "Bearer key-groq"),
        ]


class TestResponseJson:
    def test_plain_json(self):
        response = LLMResponse(
            content='{"a": 1}', model="m", provider="p"
        )
        assert response.json() == {"a": 1}

    def test_fenced_json(self):
        response = LLMResponse(
            content='```json\n{"a": 1}\n```', model="m", provider="p"
        )
        assert response.json() == {"a": 1}

    def test_fenced_json_without_language_tag(self):
        response = LLMResponse(
            content='```\n{"a": 1}\n```', model="m", provider="p"
        )
        assert response.json() == {"a": 1}

    def test_garbage_raises_format_error(self):
        response = LLMResponse(
            content="no json here at all", model="m", provider="p"
        )
        with pytest.raises(LLMResponseFormatError):
            response.json()


class TestClientConstruction:
    async def test_invalid_base_url_rejected(self, settings):
        with pytest.raises(ConfigurationError, match="base URL"):
            LLMClient(
                provider="x",
                base_url="not-a-url",
                api_key="k",
                default_model="m",
                settings=settings,
                registry=RateLimitRegistry(),
            )

    async def test_aclose_only_closes_owned_client(
        self, make_client, make_settings
    ):
        owned = LLMClient(
            provider="x",
            base_url=BASE_URL,
            api_key="k",
            default_model="m",
            settings=make_settings(llm_max_retries=0),
            registry=RateLimitRegistry(),
        )
        await owned.aclose()  # owned: closes
        client, _ = make_client(lambda r: httpx2.Response(200, json=ok_body()))
        await client.aclose()  # borrowed: must NOT close the shared client
        response = await client.chat(MESSAGES)  # still usable
        assert response.content == "hi there"


class TestMakeLlmClients:
    def _full_settings(self, make_settings) -> Settings:
        return make_settings(
            gemini_api_key=SecretStr("g"),
            groq_api_key=SecretStr("q"),
            sr_api_key=SecretStr("s"),
            sr_base_url="https://sr.test/v1",
        )

    def test_builds_three_providers(self, make_settings):
        clients = make_llm_clients(self._full_settings(make_settings))
        assert set(clients) == {"gemini", "groq", "sr"}
        assert clients["gemini"].base_url == (
            "https://generativelanguage.googleapis.com/v1beta/openai"
        )
        assert clients["gemini"].default_model == "gemini-3.5-flash-lite"
        assert clients["groq"].default_model == "openai/gpt-oss-120b"
        assert clients["sr"].default_model == "vllm-sr/d3-turbo"

    def test_missing_gemini_key_fails_fast(self, make_settings):
        settings = make_settings(
            groq_api_key=SecretStr("q"),
            sr_api_key=SecretStr("s"),
            sr_base_url="https://sr.test/v1",
        )
        with pytest.raises(ConfigurationError, match="GEMINI_API_KEY"):
            make_llm_clients(settings)

    def test_missing_groq_key_fails_fast(self, make_settings):
        settings = make_settings(
            gemini_api_key=SecretStr("g"),
            sr_api_key=SecretStr("s"),
            sr_base_url="https://sr.test/v1",
        )
        with pytest.raises(ConfigurationError, match="GROQ_API_KEY"):
            make_llm_clients(settings)

    def test_missing_sr_base_url_fails_fast(self, make_settings):
        settings = make_settings(
            gemini_api_key=SecretStr("g"),
            groq_api_key=SecretStr("q"),
            sr_api_key=SecretStr("s"),
        )
        with pytest.raises(ConfigurationError, match="SR_BASE_URL"):
            make_llm_clients(settings)
