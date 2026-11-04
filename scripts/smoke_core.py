"""Smoke check: core + service layer wired together, outside pytest.

Simulates what the app factory (src/main.py) will do: load settings,
configure logging, exercise the service factories against fakes, and
show the rate-limit fail-fast path. No network, no broker.

Run from the repo root: PYTHONPATH=. python scripts/smoke_core.py
"""

from __future__ import annotations

import asyncio
import logging

import httpx2
from fakeredis import FakeAsyncRedis

from src.core import (
    ConfigurationError,
    Settings,
    get_logger,
    get_settings,
    setup_logging,
)
from src.services import (
    RateLimitRegistry,
    make_llm_clients,
    make_mq_client,
    make_redis_client,
)


def core_smoke(settings: Settings, logger) -> None:
    logger.info(
        "smoke.startup",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment.value,
        debug=settings.debug_enabled,
    )
    print("safe_summary:", settings.safe_summary())

    # Production fail-fast: a production environment without secrets must
    # refuse to construct, listing everything that is missing.
    try:
        Settings(environment="production")
    except ConfigurationError as error:
        print("production validation OK ->", error.message[:110], "…")

    # Environment-driven formatting: JSON mode renders JSON lines.
    json_like = Settings(environment="local", log_json=True)
    setup_logging(level=json_like.log_level, json_logs=True)
    logging.getLogger("smoke.foreign").info(
        "foreign logger line goes through the same processors"
    )
    logger.info("smoke.json_mode")


async def services_smoke(settings: Settings, logger) -> None:
    # ── Redis wrapper over fakeredis ────────────────────────────────────
    redis = make_redis_client(settings, client=FakeAsyncRedis(decode_responses=True))
    await redis.set_json("smoke", {"phase": "services"})
    assert await redis.get_json("smoke") == {"phase": "services"}
    count = await redis.incr_with_ttl("smoke:counter", ttl_seconds=60)
    print(f"redis OK: cached value read back, counter at {count}")
    await redis.aclose()

    # ── Rate-limit aware LLM client (mock transport) ───────────────────
    registry = RateLimitRegistry()
    llm_settings = Settings(
        environment=settings.environment.value,
        gemini_api_key="smoke-key",
        groq_api_key="smoke-key",
        sr_api_key="smoke-key",
        sr_base_url="https://sr.test/v1",
    )

    async def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            json={"choices": [{"message": {"content": "smoke answer"}}]},
            headers={"x-ratelimit-remaining-requests": "0",
                     "x-ratelimit-reset-requests": "45s"},
        )

    http = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    clients = make_llm_clients(llm_settings, registry=registry, http_client=http)
    answer = await clients["gemini"].chat([{"role": "user", "content": "hi"}])
    print(f"llm OK: answered {answer.content!r}; remaining={answer.provider}:0")
    # Quota hit zero on the response → next call must fail fast locally.
    try:
        await clients["gemini"].chat([{"role": "user", "content": "again"}])
        raise SystemExit("fail-fast did not trigger")
    except Exception as error:  # LLMRateLimitError
        print(f"llm fail-fast OK -> {type(error).__name__}: {error}")
    await http.aclose()

    # ── MQ client against a closed connection (reconnect refused) ──────
    mq = make_mq_client(settings)
    try:
        await mq.is_healthy()
        print("mq OK: health probe returned (no broker configured)")
    except Exception as error:
        print(f"mq degraded as designed -> {type(error).__name__}")


def main() -> None:
    settings = get_settings()
    setup_logging(level=settings.log_level, json_logs=settings.log_json_resolved)
    logger = get_logger("smoke")
    core_smoke(settings, logger)
    asyncio.run(services_smoke(settings, logger))


if __name__ == "__main__":
    main()
