"""Per-provider rate-limit state, tracked from LLM response headers.

Free-tier LLM budgets (RPM/RPD/TPM) are the scarcest resource this service
has. The goal of this module is that **hitting a limit never becomes an
outage**: before every call the client checks whether the provider is
already known to be exhausted and, if so, fails fast locally (zero network
latency) so the failover pool can immediately try the next provider; after
every response it records what the provider said about remaining budget.

Where the data comes from (OpenAI-compatible providers, header names are
case-insensitive in ``httpx.Headers``):

* Gemini — ``x-ratelimit-remaining-requests``, ``x-ratelimit-reset-requests``
  (Go-style durations such as ``1m30s``), same for ``-tokens``;
* Groq — ``x-ratelimit-remaining-requests``, ``x-ratelimit-reset-requests``,
  ``retry-after`` (seconds) on 429s;
* vLLM (SR router) — whatever the deployment configures, typically none;
  the tracker simply stays neutral there.

State is in-memory and per-process: the API runs one process on Render, so
this is the whole truth; if that ever changes, persist cooldowns in Redis
(the wrapper in ``redis_client.py`` already namespaces per environment).
Reset deadlines use :func:`time.monotonic`, immune to wall-clock jumps.
"""

from __future__ import annotations

import email.utils
import math
import re
import time
from datetime import timezone
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Mapping

from src.core.logging import get_logger

logger = get_logger(__name__)

# Used when a 429 arrives without any retry hint, and when the provider
# reports remaining=0 without a reset duration. Long enough to hand traffic
# to another provider, short enough to recover quickly.
DEFAULT_COOLDOWN_SECONDS = 60.0

_DURATION_RE = re.compile(r"(?P<value>\d+(?:\.\d+)?)\s*(?P<unit>ms|s|m|h)?")
_DURATION_UNITS = {"ms": 0.001, "s": 1.0, "m": 60.0, "h": 3600.0, None: 1.0}


def parse_retry_after(value: str | None) -> float | None:
    """Parse a ``Retry-After`` header: seconds or HTTP-date. None if unknown."""
    if not value:
        return None
    stripped = value.strip()
    try:
        return max(0.0, float(stripped))
    except ValueError:
        pass
    try:
        reset = email.utils.parsedate_to_datetime(stripped)
    except (TypeError, ValueError):
        return None
    if reset is None:
        return None
    if reset.tzinfo is None:  # HTTP dates carry tz; assume UTC if missing
        reset = reset.replace(tzinfo=timezone.utc)
    return max(0.0, reset.timestamp() - time.time())


def parse_duration(value: str | None) -> float | None:
    """Parse Go-style durations ("47s", "1m30s", "1.5s", "2h") or bare seconds."""
    if not value:
        return None
    stripped = value.strip().lower()
    total = 0.0
    matched_any = False
    pos = 0
    while pos < len(stripped):
        match = _DURATION_RE.match(stripped, pos)
        if match is None or match.end() == pos:
            return None
        matched_any = True
        total += float(match.group("value")) * _DURATION_UNITS[match.group("unit")]
        pos = match.end()
    return total if matched_any else None


@dataclass
class ProviderQuota:
    """Rate-limit state for one provider, updated from response headers."""

    name: str
    remaining_requests: int | None = field(default=None)
    remaining_tokens: int | None = field(default=None)
    _exhausted_until: float | None = field(default=None, repr=False)

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    @property
    def available(self) -> bool:
        """False only while a known exhaustion window is still running."""
        if self._exhausted_until is None:
            return True
        if time.monotonic() >= self._exhausted_until:
            self._exhausted_until = None  # window elapsed
            return True
        return False

    def seconds_until_reset(self) -> float | None:
        """Seconds until the provider may accept traffic again, if cooling."""
        if not self.available:
            until = self._exhausted_until
            if until is not None:  # always true when `available` is False
                return max(0.0, math.ceil(until - time.monotonic()))
        return None

    def snapshot(self) -> dict[str, object]:
        """Plain dict for logs and the future health endpoint."""
        return {
            "available": self.available,
            "remaining_requests": self.remaining_requests,
            "remaining_tokens": self.remaining_tokens,
            "reset_in_seconds": self.seconds_until_reset(),
        }

    # ------------------------------------------------------------------
    # State updates
    # ------------------------------------------------------------------

    def update_from_headers(self, headers: Mapping[str, str]) -> None:
        """Record what a successful response reported about the budget.

        A 200 with budget left clears any stale cooldown (the provider is
        clearly serving again); ``remaining: 0`` arms the cooldown using the
        reset duration if given, so the *next* call fails fast locally.
        """
        self._read_remaining(headers)
        if self.remaining_requests == 0:
            reset = parse_duration(_header(headers, "x-ratelimit-reset-requests"))
            if reset is None:
                reset = parse_retry_after(_header(headers, "retry-after"))
            self.mark_exhausted(reset)
        else:
            self._exhausted_until = None

    def mark_exhausted(self, cooldown_seconds: float | None) -> None:
        """Arm the cooldown window (a 429 arrived, or budget hit zero)."""
        window = cooldown_seconds if cooldown_seconds and cooldown_seconds > 0 else DEFAULT_COOLDOWN_SECONDS
        self._exhausted_until = time.monotonic() + window
        logger.warning(
            "ratelimit.exhausted",
            provider=self.name,
            retry_after=math.ceil(window),
            remaining_requests=self.remaining_requests,
        )

    def _read_remaining(self, headers: Mapping[str, str]) -> None:
        requests = _parse_int(_header(headers, "x-ratelimit-remaining-requests"))
        tokens = _parse_int(_header(headers, "x-ratelimit-remaining-tokens"))
        if requests is not None:
            self.remaining_requests = requests
        if tokens is not None:
            self.remaining_tokens = tokens


def _header(headers: Mapping[str, str], name: str) -> str | None:
    # httpx.Headers is case-insensitive; plain dicts in tests may not be.
    if name in headers:
        return headers[name]
    lowered = name.lower()
    for key, value in headers.items():
        if key.lower() == lowered:
            return value
    return None


def _parse_int(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value.strip())
    except ValueError:
        return None


class RateLimitRegistry:
    """Provider-name → :class:`ProviderQuota` map shared across the app."""

    def __init__(self) -> None:
        self._quotas: dict[str, ProviderQuota] = {}

    def get(self, provider_name: str) -> ProviderQuota:
        quota = self._quotas.get(provider_name)
        if quota is None:
            quota = ProviderQuota(name=provider_name)
            self._quotas[provider_name] = quota
        return quota

    def snapshot(self) -> dict[str, dict[str, object]]:
        return {name: quota.snapshot() for name, quota in self._quotas.items()}

    def reset(self) -> None:
        """Forget all state (test isolation)."""
        self._quotas.clear()


@lru_cache
def get_rate_limit_registry() -> RateLimitRegistry:
    """Process-wide registry; the app factory passes it to client factories."""
    return RateLimitRegistry()
