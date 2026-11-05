"""Async engine & session management for Neon Postgres (Tier 2).

Built around three Neon realities (Project-Description §1, §2):

* **Autosuspend.** Neon sleeps after ~5 minutes idle, and idle pool
  connections are dropped underneath us. ``pool_pre_ping`` recycles dead
  connections transparently; the 0.5–1.5 s cold start is absorbed by
  Redis in front of hot reads, never by retry storms here.
* **The pooled endpoint.** Neon hands out a ``-pooler`` endpoint
  (PgBouncer, transaction mode) for serverless workloads. asyncpg's
  prepared-statement cache is incompatible with transaction pooling, so
  the builder detects ``-pooler`` hosts and disables it
  (``statement_cache_size=0``) — Neon's own recommendation.
* **The statement timeout (§5).** ``db_statement_timeout_ms`` (5 s) backs
  the text-to-SQL guardrails. It is applied with ``SET LOCAL`` at every
  transaction begin — the only form that survives PgBouncer transaction
  pooling (startup ``server_settings`` are rejected there, and plain
  ``SET`` may land on a different server connection).

Everything raises nothing exotic: missing configuration fails fast with
:class:`~src.core.exceptions.ConfigurationError`; runtime failures stay
native SQLAlchemy/asyncpg so callers (repositories, ETL stages) map them
through their own error policies.

Sessions: ``create_session_factory`` returns an ``async_sessionmaker``
with ``expire_on_commit=False`` (API reads use ORM objects after commit;
the ETL path commits batch after batch). :func:`session_scope` is the
single commit/rollback discipline — never hand-roll it per call site.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy import event, make_url
from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src.core.config import Settings
from src.core.exceptions import ConfigurationError
from src.core.logging import get_logger

logger = get_logger(__name__)

# asyncpg's prepared-statement cache must be off behind PgBouncer
# transaction pooling (Neon's -pooler endpoint).
_POOLER_HOST_MARKER = "-pooler"


def _is_pooler_url(url: URL) -> bool:
    host = url.host or ""
    return _POOLER_HOST_MARKER in host


def engine_kwargs(url: URL, settings: Settings) -> dict[str, Any]:
    """Pure builder of ``create_async_engine`` kwargs (unit-tested).

    Kept side-effect-free so the Neon-specific decisions (pre-ping,
    pooler statement cache, pool sizing) are testable without a database.
    SQLite (test path via aiosqlite) accepts none of the pool arguments,
    so they are postgres-only.
    """
    kwargs: dict[str, Any] = {"echo": settings.db_echo}
    if url.get_backend_name() == "postgresql":
        kwargs.update(
            pool_size=settings.db_pool_size,
            max_overflow=settings.db_max_overflow,
            pool_timeout=settings.db_pool_timeout_seconds,
            # Neon autosuspend drops idle connections; recycle them
            # instead of surfacing "connection was closed mid-operation".
            pool_pre_ping=True,
            connect_args={"ssl":"required"}
        )
        if _is_pooler_url(url):
            kwargs["connect_args"].update(statement_cache_size=0)
    return kwargs


def create_db_engine(
    settings: Settings, *, url: str | None = None
) -> AsyncEngine:
    """Build the application engine.

    Args:
        settings: application settings (pool sizes, timeout, echo).
        url: override the configured DATABASE_URL — tests run the same
            code path against sqlite+aiosqlite.
    """
    if url is None:
        url = settings.async_database_url  # raises ConfigurationError if unset
    try:
        parsed = make_url(url)
    except Exception as error:
        raise ConfigurationError(
            f"DATABASE_URL is not a valid connection string: {error}",
            details={"scheme": url.split(":", 1)[0]},
        ) from error

    engine = create_async_engine(
        parsed, **engine_kwargs(parsed, settings)
    )

    if parsed.get_backend_name() == "postgresql":
        timeout_ms = settings.db_statement_timeout_ms
        event.listens_for(engine.sync_engine, "begin")(
            _make_statement_timeout_hook(timeout_ms)
        )
        logger.info(
            "db.engine_created",
            pooler=_is_pooler_url(parsed),
            pool_size=settings.db_pool_size,
            statement_timeout_ms=timeout_ms,
        )
    return engine


def _make_statement_timeout_hook(timeout_ms: int):
    """Per-transaction ``SET LOCAL`` — PgBouncer-transaction-safe."""

    def _set_statement_timeout(conn: Any) -> None:
        conn.exec_driver_sql(f"SET LOCAL statement_timeout = {int(timeout_ms)}")

    return _set_statement_timeout


def create_session_factory(
    engine: AsyncEngine,
) -> async_sessionmaker[AsyncSession]:
    """Session factory; ``expire_on_commit=False`` for post-commit reads."""
    return async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )


@asynccontextmanager
async def session_scope(
    factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    """One unit of work: commit on success, rollback on any exception.

    The commit itself can fail (Neon cold start raced a DDL, constraint
    violation, ...) — rollback runs in ``finally`` so the pooled
    connection is never returned with an open aborted transaction.
    """
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception as error:
            await session.rollback()
            logger.warning(
                "db.session_rolled_back", error=str(error), error_type=type(error).__name__
            )
            raise


async def ping_database(engine: AsyncEngine) -> bool:
    """Cheap liveness probe; never raises (future deep-health endpoint)."""
    from sqlalchemy import text

    try:
        async with engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        return True
    except Exception as error:
        logger.error("db.ping_failed", error=str(error))
        return False


async def dispose_engine(engine: AsyncEngine) -> None:
    """Shutdown hook: close pooled connections before the app exits."""
    await engine.dispose()


__all__ = [
    "create_db_engine",
    "create_session_factory",
    "dispose_engine",
    "engine_kwargs",
    "ping_database",
    "session_scope",
]
