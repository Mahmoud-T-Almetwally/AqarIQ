"""Async Redis (Upstash, Tier 4) wrapper — cache and abuse counters.

Two responsibilities, one connection:

* **Exact-match response cache** (Project-Description §5): ``get_json`` /
  ``set_json`` store serialized API answers under a TTL. The cache is an
  optimization, not a dependency — by default every data operation
  **degrades silently** (returns a miss / skips the write, logs a warning)
  when Upstash is unreachable, so an outage there never becomes an outage
  of the chat endpoint. Pass ``strict=True`` for callers that must know
  (e.g. the future deep health check).
* **Abuse counters** (``rate_limit_messages_per_hour`` /
  ``per_day``): ``incr_with_ttl`` implements fixed-window counters — the
  TTL is armed on the first increment and, defensively, on any counter
  found without one (a process crash between ``INCR`` and ``EXPIRE`` must
  not ban an IP forever).

Every key is transparently prefixed with ``settings.redis_namespace``
(``rea:<environment>``), so local, test and production can share one
Upstash database without collisions.

The underlying ``redis.asyncio.Redis`` client is exposed as
:meth:`RedisClient.raw` for pipeline-heavy call sites (the ETL embedder);
everything else should go through the namespaced helpers so the prefix
convention holds.
"""

from __future__ import annotations

import json
from typing import Any

import redis.asyncio as aioredis

from src.core.config import Settings
from src.core.exceptions import CacheError, ConfigurationError
from src.core.logging import get_logger

logger = get_logger(__name__)


class RedisClient:
    """Namespaced, degradation-first wrapper over ``redis.asyncio``."""

    def __init__(
        self,
        client: aioredis.Redis,
        *,
        namespace: str,
        default_ttl_seconds: int,
        strict: bool = False,
    ) -> None:
        self._client = client
        self.namespace = namespace
        self.default_ttl_seconds = default_ttl_seconds
        self.strict = strict

    # ------------------------------------------------------------------
    # Access
    # ------------------------------------------------------------------

    @property
    def raw(self) -> aioredis.Redis:
        """The underlying client — bypasses namespacing; callers must
        prefix keys themselves."""
        return self._client

    def namespaced(self, key: str) -> str:
        if not key:
            raise ValueError("Redis key must not be empty")
        return f"{self.namespace}:{key}"

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def ping(self) -> bool:
        """Connectivity probe; never raises (health endpoint decides)."""
        try:
            return bool(await self._client.ping())
        except aioredis.RedisError as error:
            logger.error("redis.ping_failed", error=str(error))
            return False

    # ------------------------------------------------------------------
    # String values
    # ------------------------------------------------------------------

    async def get(self, key: str) -> str | None:
        try:
            return await self._client.get(self.namespaced(key))
        except aioredis.RedisError as error:
            return self._degrade("get", key, error)

    async def set(
        self, key: str, value: str, *, ttl_seconds: int | None = None
    ) -> None:
        try:
            await self._client.set(
                self.namespaced(key),
                value,
                ex=ttl_seconds if ttl_seconds is not None else self.default_ttl_seconds,
            )
        except aioredis.RedisError as error:
            self._degrade("set", key, error)

    async def delete(self, *keys: str) -> int:
        """Delete keys; returns how many were removed (0 on outage)."""
        if not keys:
            return 0
        try:
            return int(
                await self._client.delete(*(self.namespaced(k) for k in keys))
            )
        except aioredis.RedisError as error:
            return self._degrade("delete", keys[0], error, default=0)

    # ------------------------------------------------------------------
    # JSON values (response cache)
    # ------------------------------------------------------------------

    async def get_json(self, key: str) -> Any | None:
        """Cached JSON value, or ``None`` on miss / outage / corruption."""
        raw = await self.get(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # Corrupt entry: behave like a miss and clear the junk.
            logger.warning("redis.corrupt_json_cache_entry", key=key)
            await self.delete(key)
            return None

    async def set_json(
        self, key: str, value: Any, *, ttl_seconds: int | None = None
    ) -> None:
        await self.set(
            key,
            json.dumps(value, separators=(",", ":"), ensure_ascii=False),
            ttl_seconds=ttl_seconds,
        )

    # ------------------------------------------------------------------
    # Fixed-window counters (abuse protection)
    # ------------------------------------------------------------------

    async def incr_with_ttl(self, key: str, *, ttl_seconds: int) -> int:
        """Increment a fixed-window counter, arming the TTL on first hit.

        The window is *fixed*, not sliding: ``EXPIRE`` runs only when the
        counter is new (or lost its TTL to a crash between INCR and
        EXPIRE — otherwise that IP would be banned forever). Returns the
        new count; callers compare against the per-hour/per-day limits.
        """
        namespaced = self.namespaced(key)
        try:
            count = int(await self._client.incr(namespaced))
            if count == 1 or await self._client.ttl(namespaced) == -1:
                await self._client.expire(namespaced, ttl_seconds)
            return count
        except aioredis.RedisError as error:
            default = self._degrade("incr_with_ttl", key, error, default=0)
            # A counter that cannot be incremented must not silently allow
            # unbounded traffic: strict mode propagates, degraded mode
            # returns 0 (the rate-limit layer treats 0 as "allow but log").
            return int(default)

    # ------------------------------------------------------------------
    # Degradation policy
    # ------------------------------------------------------------------

    def _degrade(
        self,
        operation: str,
        key: str,
        error: Exception,
        *,
        default: Any = None,
    ) -> Any:
        """Handle a Redis failure per the strictness policy."""
        if self.strict:
            raise CacheError(
                f"Redis {operation} failed for key {key!r}",
                details={"operation": operation},
            ) from error
        logger.warning(
            "redis.degraded",
            operation=operation,
            key=key,
            error=str(error),
        )
        return default


def make_redis_client(
    settings: Settings,
    *,
    client: aioredis.Redis | None = None,
    strict: bool = False,
) -> RedisClient:
    """Build the wrapper; ``client`` (tests / embedded setups) overrides
    the URL-based construction."""
    if client is None:
        if not settings.redis_url:
            raise ConfigurationError(
                "REDIS_URL is not configured",
                details={"missing": ["REDIS_URL"]},
            )
        client = aioredis.from_url(
            settings.redis_url,
            decode_responses=True,
            # Upstash TLS is on 6379; honor scheme, retry once on cold start.
            socket_connect_timeout=5,
            socket_timeout=5,
        )
    return RedisClient(
        client,
        namespace=settings.redis_namespace,
        default_ttl_seconds=settings.response_cache_ttl_seconds,
        strict=strict,
    )
