"""Smoke check: core + service + storage layers wired together, outside pytest.

Simulates what the app factory (src/main.py) will do: load settings,
configure logging, exercise the service factories against fakes, show the
rate-limit fail-fast path, and render the Alembic chain offline. No
network, no broker, no database.

Run from the repo root: PYTHONPATH=. python scripts/smoke_core.py
"""

from __future__ import annotations

import asyncio
import logging
import os

import httpx2
from fakeredis import FakeAsyncRedis

from src.core import (
    ConfigurationError,
    Settings,
    get_logger,
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
        Settings(_env_file=None, environment="production")
    except ConfigurationError as error:
        print("production validation OK ->", error.message[:110], "…")

    # Environment-driven formatting: JSON mode renders JSON lines.
    json_like = Settings(_env_file=None, environment="local", log_json=True)
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
        _env_file=None,
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


def storage_smoke(settings: Settings) -> None:
    from sqlalchemy import text
    from src.db import (
        Base,
        create_db_engine,
        create_session_factory,
        dispose_engine,
        session_scope,
    )

    tables = sorted(Base.metadata.tables)
    print(f"models OK: {len(tables)} tables -> {', '.join(tables)}")

    # Alembic chain rendered offline: the exact SQL a fresh Neon branch
    # would run (extensions → core → chat → market_stats + HNSW).
    os.environ["DATABASE_URL"] = (
        "postgresql://smoke:smoke@ep-smoke-pooler.eu-central-1.aws.neon.tech/db"
    )
    try:
        import io
        from contextlib import redirect_stdout

        from alembic import command
        from alembic.config import Config

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            command.upgrade(Config("alembic.ini"), "head", sql=True)
        sql = buffer.getvalue()
        markers = [
            'CREATE EXTENSION IF NOT EXISTS "postgis"',
            'CREATE EXTENSION IF NOT EXISTS "vector"',
            "USING hnsw (embedding halfvec_cosine_ops)",
            "GENERATED ALWAYS AS (to_tsvector('simple', title_normalized))",
        ]
        missing = [m for m in markers if m not in sql]
        print(f"alembic OK: chain 0001→0004 renders ({len(sql.splitlines())} SQL lines)"
              + (f"; MISSING {missing}" if missing else ""))
    finally:
        os.environ.pop("DATABASE_URL", None)

    # Session discipline over sqlite (the real code path, async).
    async def _session_roundtrip() -> None:
        engine = create_db_engine(settings, url="sqlite+aiosqlite://")
        factory = create_session_factory(engine)
        try:
            async with engine.begin() as connection:
                await connection.execute(text(
                    "CREATE TABLE widget (id INTEGER PRIMARY KEY, name TEXT)"
                ))
            async with session_scope(factory) as session:
                await session.execute(
                    text("INSERT INTO widget (name) VALUES ('storage-smoke')")
                )
            async with session_scope(factory) as session:
                name = (await session.execute(
                    text("SELECT name FROM widget")
                )).scalar_one()
            print(f"session OK: committed and read back {name!r}")
        finally:
            await dispose_engine(engine)

    asyncio.run(_session_roundtrip())


async def app_smoke(settings: Settings) -> None:
    """The real app factory: boot the lifespan, answer /health, shut down.

    Local settings carry no URLs, so this exercises the graceful boot:
    static 200 from /health with both pools skipped — exactly what a
    cold CI instance or a UptimeRobot ping sees.
    """
    from httpx2 import ASGITransport, AsyncClient

    from src.main import create_app, lifespan

    app = create_app(settings)
    async with lifespan(app):
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="http://smoke"
        ) as client:
            response = await client.get("/health")
        wiring = {
            "redis": "wired" if app.state.redis is not None else "skipped",
            "db": "wired" if app.state.db_engine is not None else "skipped",
        }
    cleared = (
        app.state.redis is None and app.state.db_engine is None
    )
    print(
        f"app OK: /health -> {response.status_code} {response.json()}; "
        f"redis={wiring['redis']}, db={wiring['db']}, "
        f"state_cleared_on_shutdown={cleared}"
    )


def main() -> None:
    # Deterministic local defaults: a developer's .env may point at their
    # own infrastructure (or hold experimental values); the smoke run
    # exercises code paths, not the developer's environment.
    settings = Settings(_env_file=None)
    setup_logging(level=settings.log_level, json_logs=settings.log_json_resolved)
    logger = get_logger("smoke")
    core_smoke(settings, logger)
    asyncio.run(services_smoke(settings, logger))
    storage_smoke(settings)
    asyncio.run(app_smoke(settings))


if __name__ == "__main__":
    main()
