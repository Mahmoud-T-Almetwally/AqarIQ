"""Async client for OpenAI-compatible LLM APIs (Gemini, Groq, vLLM-SR).

All three providers in this stack (Project-Description §4) expose
OpenAI-compatible ``/chat/completions`` endpoints, so one client covers the
chat failover chain and the intent router:

* ``gemini`` — synthesis primary, then fallback model;
* ``groq``   — second fallback and ETL feature extraction;
* ``sr``     — Jev intent router.

Non-goals: streaming and tool calling are not needed yet and stay
unimplemented (``stream=False`` requests only). Embeddings arrive with the
ETL phase as a separate method.

Failure policy (per call, on top of the pool's cross-provider failover):

* rate-limited (429 or tracked quota exhausted) → fail **fast** with
  :class:`~src.core.exceptions.LLMRateLimitError` carrying ``retry_after``
  — never burn retries or another provider's budget on a known-exhausted
  one; the quota tracker (:mod:`src.services.rate_limit`) arms a cooldown
  from the response's ``Retry-After`` / reset headers;
* timeout / connection error → retried up to ``llm_max_retries`` with
  exponential backoff, then :class:`LLMTimeoutError` / :class:`LLMError`;
* 401/403 → :class:`LLMAuthError` immediately (retrying cannot fix a key);
* malformed body or empty completion → :class:`LLMResponseFormatError`.

The client is provider-agnostic on purpose: one instance per provider
(``make_llm_clients`` builds the three configured ones), the failover pool
owns *ordering* — that belongs to the agent layer.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any

import httpx2

from src.core.config import Settings
from src.core.exceptions import (
    ConfigurationError,
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMTimeoutError,
)
from src.core.logging import get_logger
from src.services.rate_limit import (
    ProviderQuota,
    RateLimitRegistry,
    get_rate_limit_registry,
    parse_retry_after,
)

logger = get_logger(__name__)

_RETRYABLE_STATUS = {408, 500, 502, 503, 504}
_BACKOFF_BASE_SECONDS = 1.5
_CHAT_COMPLETIONS_PATH = "/chat/completions"


@dataclass(frozen=True)
class LLMResponse:
    """One completed chat completion."""

    content: str
    model: str
    provider: str
    finish_reason: str | None = None
    usage: dict[str, int] | None = None

    def json(self) -> Any:
        """Parse ``content`` as JSON, raising on garbage.

        Extraction and routing prompts instruct the model to answer with
        JSON only; this is the enforcement point. A fence-stripping retry
        handles models that wrap the answer in a ```` ```json ```` block
        despite instructions.
        """
        try:
            return json.loads(self.content)
        except json.JSONDecodeError as error:
            try:
                return json.loads(_strip_code_fence(self.content))
            except json.JSONDecodeError:
                raise LLMResponseFormatError(
                    "Model response was not valid JSON",
                    details={
                        "provider": self.provider,
                        "model": self.model,
                        "parse_error": error.msg,
                    },
                ) from error


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        first_newline = stripped.find("\n")
        if first_newline != -1:
            stripped = stripped[first_newline + 1 :]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[:-3]
    return stripped.strip()


class LLMClient:
    """Async chat-completions client for one OpenAI-compatible provider.

    Requests go to the absolute ``{base_url}/chat/completions`` URL with
    per-request auth headers, so several clients can safely share one
    ``httpx2.AsyncClient`` (connection pool) while keeping their own host
    and API key.
    """

    def __init__(
        self,
        *,
        provider: str,
        base_url: str,
        api_key: str,
        default_model: str,
        settings: Settings,
        registry: RateLimitRegistry | None = None,
        http_client: httpx2.AsyncClient | None = None,
    ) -> None:
        if not base_url or not base_url.startswith(("http://", "https://")):
            raise ConfigurationError(
                f"{provider} base URL is not configured",
                details={"provider": provider, "base_url": base_url},
            )
        self.provider = provider
        self.base_url = base_url.rstrip("/")
        self.default_model = default_model
        self.settings = settings
        self.registry = registry or get_rate_limit_registry()
        self.quota: ProviderQuota = self.registry.get(provider)
        self._url = f"{self.base_url}{_CHAT_COMPLETIONS_PATH}"
        self._auth_headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        self._owns_client = http_client is None
        self._client = http_client or httpx2.AsyncClient(
            timeout=httpx2.Timeout(settings.llm_timeout_seconds)
        )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def chat(
        self,
        messages: list[dict[str, str]],
        *,
        model: str | None = None,
        temperature: float = 0.0,
        max_tokens: int | None = None,
        response_json: bool = False,
    ) -> LLMResponse:
        """Run one chat completion.

        Args:
            messages: OpenAI-format messages (``role``/``content``).
            model: Override the client's default model (failover chain).
            temperature: 0 for extraction/routing, higher for synthesis.
            max_tokens: Cap the answer length (free-tier TPM protection).
            response_json: Ask for guaranteed JSON (``response_format``) —
                providers pass it through or ignore it; parse either way.
        """
        resolved_model = model or self.default_model
        payload: dict[str, Any] = {
            "model": resolved_model,
            "messages": messages,
            "temperature": temperature,
            "stream": False,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_json:
            payload["response_format"] = {"type": "json_object"}

        # Fail fast when this provider is known-exhausted — the pool should
        # have routed elsewhere; reaching here means every option is spent.
        self._ensure_available()

        max_retries = self.settings.llm_max_retries
        for attempt in range(max_retries + 1):
            final_attempt = attempt >= max_retries
            try:
                response = await self._client.post(
                    self._url, json=payload, headers=self._auth_headers
                )
            except httpx2.TimeoutException as error:
                if final_attempt:
                    raise LLMTimeoutError(
                        f"{self.provider} timed out after "
                        f"{max_retries + 1} attempts",
                        details={
                            "provider": self.provider,
                            "model": resolved_model,
                        },
                    ) from error
                logger.warning(
                    "llm.timeout_retry",
                    provider=self.provider,
                    model=resolved_model,
                    attempt=attempt + 1,
                )
                await asyncio.sleep(_BACKOFF_BASE_SECONDS**attempt)
                continue
            except httpx2.HTTPError as error:
                if final_attempt:
                    raise LLMError(
                        f"{self.provider} unreachable: {error}",
                        details={
                            "provider": self.provider,
                            "model": resolved_model,
                        },
                    ) from error
                logger.warning(
                    "llm.connection_retry",
                    provider=self.provider,
                    model=resolved_model,
                    attempt=attempt + 1,
                )
                await asyncio.sleep(_BACKOFF_BASE_SECONDS**attempt)
                continue

            if response.status_code == 200:
                self.quota.update_from_headers(response.headers)
                return self._parse_success(response, resolved_model)

            failure = self._on_error_response(response, resolved_model)
            if failure is not None:
                raise failure
            if final_attempt:
                raise LLMError(
                    f"{self.provider} returned {response.status_code} "
                    f"after {max_retries + 1} attempts",
                    details={
                        "provider": self.provider,
                        "model": resolved_model,
                        "status": response.status_code,
                    },
                )
            logger.warning(
                "llm.status_retry",
                provider=self.provider,
                model=resolved_model,
                status=response.status_code,
                attempt=attempt + 1,
            )
            await asyncio.sleep(_BACKOFF_BASE_SECONDS**attempt)

        raise LLMError(  # unreachable; for the type checker
            f"{self.provider} exhausted its retry budget",
            details={"provider": self.provider, "model": resolved_model},
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_available(self) -> None:
        if not self.quota.available:
            reset = self.quota.seconds_until_reset()
            raise LLMRateLimitError(
                f"{self.provider} quota is exhausted; retry later",
                retry_after=int(reset) if reset else None,
            )

    def _on_error_response(
        self, response: httpx2.Response, model: str
    ) -> LLMError | None:
        """Map a non-200 response. ``None`` means "retryable status"."""
        status = response.status_code
        if status == 429:
            retry_after = parse_retry_after(response.headers.get("retry-after"))
            self.quota.mark_exhausted(retry_after)
            return LLMRateLimitError(
                f"{self.provider} rate limit hit",
                retry_after=int(retry_after) if retry_after is not None else None,
            )
        if status in (401, 403):
            return LLMAuthError(
                f"{self.provider} rejected the API key",
                details={"provider": self.provider, "status": status},
            )
        if status in _RETRYABLE_STATUS:
            return None
        return LLMError(
            f"{self.provider} returned {status}",
            details={
                "provider": self.provider,
                "model": model,
                "status": status,
                "body": response.text[:500],
            },
        )

    def _parse_success(
        self, response: httpx2.Response, model: str
    ) -> LLMResponse:
        try:
            data = response.json()
        except ValueError as error:
            raise LLMResponseFormatError(
                f"{self.provider} returned a non-JSON body",
                details={"provider": self.provider, "model": model},
            ) from error
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as error:
            raise LLMResponseFormatError(
                f"{self.provider} response missing choices/content",
                details={
                    "provider": self.provider,
                    "model": model,
                    "body": str(data)[:500],
                },
            ) from error
        if not isinstance(content, str) or not content.strip():
            raise LLMResponseFormatError(
                f"{self.provider} returned an empty completion",
                details={"provider": self.provider, "model": model},
            )
        usage = data.get("usage")
        return LLMResponse(
            content=content,
            model=data.get("model", model),
            provider=self.provider,
            finish_reason=data["choices"][0].get("finish_reason"),
            usage={
                key: value
                for key, value in (usage or {}).items()
                if isinstance(value, int)
            }
            or None,
        )


def make_llm_clients(
    settings: Settings,
    *,
    registry: RateLimitRegistry | None = None,
    http_client: httpx2.AsyncClient | None = None,
) -> dict[str, LLMClient]:
    """Build the gemini / groq / sr clients from settings.

    Raises :class:`ConfigurationError` naming the first missing setting, so
    startup fails loudly instead of at first call. An optional shared
    ``http_client`` reuses one connection pool across providers; each client
    still targets its own host with its own key.
    """

    def _required(field_name: str, *, kind: str = "key") -> str:
        value: object = getattr(settings, field_name)
        if hasattr(value, "get_secret_value"):
            value = value.get_secret_value()
        if not isinstance(value, str) or not value.strip():
            raise ConfigurationError(
                f"{field_name.upper()} is not configured",
                details={"missing": [field_name.upper()], "kind": kind},
            )
        return value.strip()

    specs = {
        "gemini": (
            settings.gemini_base_url,
            _required("gemini_api_key"),
            settings.gemini_primary_model,
        ),
        "groq": (
            settings.groq_base_url,
            _required("groq_api_key"),
            settings.groq_model,
        ),
        "sr": (
            _required("sr_base_url", kind="url"),
            _required("sr_api_key"),
            settings.sr_router_model,
        ),
    }
    return {
        provider: LLMClient(
            provider=provider,
            base_url=base_url,
            api_key=api_key,
            default_model=default_model,
            settings=settings,
            registry=registry,
            http_client=http_client,
        )
        for provider, (base_url, api_key, default_model) in specs.items()
    }
