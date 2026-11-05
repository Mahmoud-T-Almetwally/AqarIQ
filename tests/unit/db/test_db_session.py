"""Tests for src/db/session.py — engine kwargs + session semantics.

Engine building (pooler detection, pool sizing, timeout wiring) is tested
purely; commit/rollback semantics run the real code path against an
in-memory SQLite engine (aiosqlite) — no Postgres needed.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.engine import make_url

from src.core.exceptions import ConfigurationError
from src.db.session import (
    create_db_engine,
    create_session_factory,
    dispose_engine,
    engine_kwargs,
    ping_database,
    session_scope,
)

POOLER_URL = (
    "postgresql+asyncpg://u:p@ep-demo-pooler."
    "eu-central-1.aws.neon.tech/neondb"
)
DIRECT_URL = "postgresql+asyncpg://u:p@ep-demo.eu-central-1.aws.neon.tech/neondb"


@pytest.fixture
def settings(make_settings):
    return make_settings(
        db_pool_size=7,
        db_max_overflow=3,
        db_pool_timeout_seconds=11,
        db_statement_timeout_ms=2500,
    )


class TestEngineKwargs:
    def test_pooler_url_disables_statement_cache(self, settings):
        kwargs = engine_kwargs(make_url(POOLER_URL), settings)
        assert kwargs["connect_args"] == {"ssl": "required", "statement_cache_size": 0}

    def test_direct_url_keeps_statement_cache(self, settings):
        kwargs = engine_kwargs(make_url(DIRECT_URL), settings)
        assert "statement_cache_size" not in kwargs["connect_args"]

    def test_pool_sizes_from_settings(self, settings):
        kwargs = engine_kwargs(make_url(DIRECT_URL), settings)
        assert kwargs["pool_size"] == 7
        assert kwargs["max_overflow"] == 3
        assert kwargs["pool_timeout"] == 11
        assert kwargs["pool_pre_ping"] is True  # Neon autosuspend guard

    def test_sqlite_test_path_has_no_pool_args(self, settings):
        kwargs = engine_kwargs(make_url("sqlite+aiosqlite://"), settings)
        assert "pool_size" not in kwargs
        assert "connect_args" not in kwargs


class TestCreateDbEngine:
    def test_missing_url_raises_configuration_error(self, make_settings):
        with pytest.raises(ConfigurationError, match="DATABASE_URL"):
            create_db_engine(make_settings())

    def test_garbage_url_raises_configuration_error(self, settings):
        with pytest.raises(ConfigurationError, match="valid connection"):
            create_db_engine(settings, url="not a url at all")

    def test_statement_timeout_registered_for_postgres(
        self, settings, monkeypatch
    ):
        registered = {}
        monkeypatch.setattr(
            "src.db.session.event.listens_for",
            lambda target, identifier: (lambda fn: registered.update(
                target=target, identifier=identifier, fn=fn
            ) or fn),
        )
        create_db_engine(settings, url=DIRECT_URL)
        assert registered["identifier"] == "begin"
        # The hook emits the configured timeout as SET LOCAL.
        executed = {}
        registered["fn"](type("Conn", (), {
            "exec_driver_sql": staticmethod(
                lambda sql: executed.update(sql=sql)
            ),
        })())
        assert executed["sql"] == "SET LOCAL statement_timeout = 2500"

    def test_sqlite_engine_gets_no_timeout_hook(self, settings, monkeypatch):
        registered = {}
        monkeypatch.setattr(
            "src.db.session.event.listens_for",
            lambda target, identifier: (lambda fn: registered.update(
                identifier=identifier, fn=fn
            ) or fn),
        )
        engine = create_db_engine(settings, url="sqlite+aiosqlite://")
        assert engine.dialect.name == "sqlite"
        # The postgres-only "begin" listener must not be registered.
        assert registered == {}


class TestSessionSemantics:
    @pytest.fixture
    async def engine(self, settings):
        engine = create_db_engine(settings, url="sqlite+aiosqlite://")
        async with engine.begin() as connection:
            await connection.execute(text(
                "CREATE TABLE widget (id INTEGER PRIMARY KEY, name TEXT)"
            ))
        yield engine
        await dispose_engine(engine)

    @pytest.fixture
    def factory(self, engine):
        return create_session_factory(engine)

    async def test_session_scope_commits(self, factory):
        async with session_scope(factory) as session:
            await session.execute(
                text("INSERT INTO widget (name) VALUES ('merge')")
            )
        async with session_scope(factory) as session:
            rows = (await session.execute(text("SELECT name FROM widget"))).all()
        assert rows == [("merge",)]

    async def test_session_scope_rolls_back_on_exception(self, factory):
        with pytest.raises(RuntimeError, match="stage failed"):
            async with session_scope(factory) as session:
                await session.execute(
                    text("INSERT INTO widget (name) VALUES ('doomed')")
                )
                raise RuntimeError("stage failed")
        async with session_scope(factory) as session:
            count = (await session.execute(
                text("SELECT count(*) FROM widget")
            )).scalar_one()
        assert count == 0

    async def test_rollback_on_commit_failure_is_atomic(self, factory):
        # A failing INSERT (constraint) rolls back the WHOLE unit of work;
        # no partial state survives.
        async with session_scope(factory) as session:
            await session.execute(
                text("CREATE UNIQUE INDEX uq_widget_name ON widget (name)")
            )
        with pytest.raises(Exception):
            async with session_scope(factory) as session:
                await session.execute(
                    text("INSERT INTO widget (name) VALUES ('x')")
                )
                await session.execute(  # duplicate name → IntegrityError
                    text("INSERT INTO widget (id, name) VALUES (99, 'x')")
                )
        async with session_scope(factory) as session:
            count = (await session.execute(
                text("SELECT count(*) FROM widget")
            )).scalar_one()
        assert count == 0

    def test_expire_on_commit_disabled(self, factory):
        # ORM objects must stay usable after the scope commits (API reads);
        # async_sessionmaker exposes its kwargs directly.
        assert factory.kw["expire_on_commit"] is False
        assert factory.kw["autoflush"] is False

    async def test_ping_database_true_when_alive(self, engine):
        assert await ping_database(engine) is True

    async def test_ping_database_false_when_unreachable(self, settings):
        # A path that cannot exist: sqlite refuses to open it.
        engine = create_db_engine(
            settings, url="sqlite+aiosqlite:////nonexistent-dir/widget.db"
        )
        assert await ping_database(engine) is False
        await dispose_engine(engine)
