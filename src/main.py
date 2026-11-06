"""App factory + lifespan — the composition root of the single Render service.

One service, two roles (Project-Description §1): this FastAPI app serves
the API, and the same process will host the in-process ETL consumer
(§3) started from this lifespan. Free-tier realities drive the wiring:

* ``/health`` is **static** — no database, Redis, or AMQP calls (§1) — so
  UptimeRobot's 5-minute pings never wake Neon (a 5-min DB ping would
  burn ~180 CU-hrs against the 100 CU-hr cap) and never touch Upstash.
  A cold Render boot answers 200 before any infrastructure answers.
* Infrastructure connects at startup but a boot never *depends* on it:
  Redis is an optimization (§4 Tier 4), and Neon is *supposed* to sleep —
  an unreachable probe logs a warning and the app continues degraded,
  which is exactly the graceful-degradation story the platform is built
  on (§1: cached listings keep serving when compute is capped).
* Shutdown disposes in reverse startup order so pooled connections are
  returned cleanly across Render's daily restarts.

State contract (later phases read these from API dependencies):

* ``app.state.settings`` — the resolved :class:`Settings`;
* ``app.state.redis`` — :class:`RedisClient` or ``None`` (no URL);
* ``app.state.db_engine`` — :class:`AsyncEngine` or ``None``;
* ``app.state.db_session_factory`` — ``async_sessionmaker`` or ``None``.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from src.core.config import Settings, get_settings
from src.core.logging import get_logger, setup_logging
from src.db.session import (
    create_db_engine,
    create_session_factory,
    dispose_engine,
    ping_database,
)
from src.services.redis_client import RedisClient, make_redis_client

logger = get_logger(__name__)

# Startup DB probe cap: Neon's worst-case cold start is ~1.5 s; anything
# slower than this is logged as degraded and left to per-request retry
# (the engine's pool_pre_ping handles a later recovery transparently).
_DB_STARTUP_PING_TIMEOUT_SECONDS = 3.0


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Connect Redis and the DB pool at startup; dispose at shutdown.

    Failures follow two different policies on purpose:

    * *configuration bugs* (missing/malformed URL) propagate — the
      process must not boot green on a broken config;
    * *environmental failures* (Neon asleep, Upstash blip, cold-start
      race) only log — the platform expects to run degraded, and
      ``/health`` must stay 200 so keep-alive keeps the service warm.
    """
    settings: Settings = app.state.settings
    logger.info(
        "app.starting",
        environment=settings.environment.value,
        version=settings.app_version,
    )

    # ── Redis (fast, never sleeps) — front of every hot read ──────────
    redis_client: RedisClient | None = None
    if settings.redis_url:
        redis_client = make_redis_client(settings)
        if await redis_client.ping():
            logger.info("app.redis_ready")
        else:
            logger.warning("app.redis_unreachable_cache_degraded")
    else:
        logger.info("app.redis_skipped_no_url")
    app.state.redis = redis_client

    # ── Database (engine creation is lazy; probe warms one connection) ─
    db_engine = None
    db_session_factory = None
    if settings.database_url:
        db_engine = create_db_engine(settings)
        db_session_factory = create_session_factory(db_engine)
        try:
            db_ready = await asyncio.wait_for(
                ping_database(db_engine),
                timeout=_DB_STARTUP_PING_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            db_ready = False
        if db_ready:
            logger.info("app.db_ready")
        else:
            logger.warning("app.db_unreachable_continuing_degraded")
    else:
        logger.info("app.db_skipped_no_url")
    app.state.db_engine = db_engine
    app.state.db_session_factory = db_session_factory

    # §3: the in-process RabbitMQ consumer (etl/consumer.py) starts
    # here in the ETL phase, after Redis and DB are wired.

    yield

    # ── Shutdown: reverse startup order ────────────────────────────────
    if db_engine is not None:
        await dispose_engine(db_engine)
        logger.info("app.db_disposed")
    app.state.db_engine = None
    app.state.db_session_factory = None

    if redis_client is not None:
        await redis_client.aclose()
        logger.info("app.redis_closed")
    app.state.redis = None

    logger.info("app.stopped")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the application; all heavy lifting lives in the lifespan.

    Entrypoint (Render): ``uvicorn src.main:create_app --factory``.
    Deliberately no module-level ``app``: settings resolution must not
    run at import time, or a broken local ``.env`` would break imports
    (tests, Alembic, tooling) that never asked for an application.
    """
    if settings is None:
        settings = get_settings()
    setup_logging(level=settings.log_level, json_logs=settings.log_json_resolved)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        lifespan=lifespan,
    )
    app.state.settings = settings

    # The versioned API routers (src/api/router.py: chat, properties,
    # stats, internal — §5, §7) mount here as the next phases land.

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Static liveness probe — UptimeRobot-only (§1).

        Deliberately touches no infrastructure: no DB, no Redis, no AMQP,
        so a 5-minute ping cadence never keeps Neon awake or spends
        Upstash commands, and a cold boot answers 200 immediately.
        """
        return {"status": "ok"}

    return app


__all__ = ["create_app", "lifespan"]
