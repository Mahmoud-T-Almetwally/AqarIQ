"""Tests for src/main.py — the lifespan wiring and the static /health.

Two harnesses, on purpose:

* Starlette's TestClient (sync, runs the real lifespan in a portal) —
  for the pure HTTP contract: /health is a static 200 whatever the
  infrastructure state.
* ``async with lifespan(app)`` plus httpx2 ASGITransport (async, single
  event loop) — for state wiring, because engines/redis clients must be
  awaited on the same loop that booted them.

The degradation matrix from §1 is exercised end to end: nothing
configured, Redis healthy (fakeredis), Redis unreachable, DB healthy
(sqlite through the real engine code path), DB unreachable, and
shutdown behavior for both pools.
"""

from __future__ import annotations

import pytest
from fakeredis import FakeAsyncRedis
from fastapi.testclient import TestClient
from httpx2 import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from src.db.session import create_session_factory, dispose_engine
from src.main import create_app, lifespan
from src.services.redis_client import make_redis_client

# Nothing listens here in any environment; TCP is refused instantly.
UNREACHABLE_REDIS = "redis://127.0.0.1:1/0"
UNREACHABLE_POSTGRES = "postgresql://u:p@127.0.0.1:1/neondb"


@pytest.fixture
async def sqlite_engine():
    """A real aiosqlite engine: the lifespan's DB code path, exercised
    without a Postgres server (the builder seam is swapped in tests
    because the URL validator is postgres-only by design)."""
    engine = create_async_engine("sqlite+aiosqlite://")
    yield engine
    try:
        await dispose_engine(engine)
    except Exception:
        pass


def find_event(records, event: str) -> list:
    return [
        r for r in records
        if isinstance(r.msg, dict) and r.msg.get("event") == event
    ]


class TestHealthEndpoint:
    def test_health_static_ok(self, make_settings):
        app = create_app(make_settings())
        with TestClient(app) as client:
            response = client.get("/health")
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_health_never_needs_infrastructure(self, make_settings):
        """No URLs configured at all — the UptimeRobot contract holds."""
        settings = make_settings()  # no redis_url, no database_url
        app = create_app(settings)
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200
        assert settings.redis_url is None
        assert settings.database_url is None

    def test_health_stays_ok_when_infra_down(self, make_settings):
        """Even with unreachable URLs configured, /health is 200 — the
        app boots degraded; the ping endpoint must not notice."""
        settings = make_settings(
            redis_url=UNREACHABLE_REDIS,
            database_url=UNREACHABLE_POSTGRES,
        )
        app = create_app(settings)
        with TestClient(app) as client:
            assert client.get("/health").status_code == 200


class TestLifespanState:
    def test_state_carries_settings(self, make_settings):
        settings = make_settings()
        app = create_app(settings)
        assert app.state.settings is settings

    async def test_boot_without_infra_skips_and_clears_state(self, make_settings):
        app = create_app(make_settings())
        async with lifespan(app):
            assert app.state.redis is None
            assert app.state.db_engine is None
            assert app.state.db_session_factory is None
            transport = ASGITransport(app=app)
            async with AsyncClient(
                transport=transport, base_url="http://test"
            ) as client:
                response = await client.get("/health")
            assert response.status_code == 200
        # Shutdown cleared every reference.
        assert app.state.redis is None
        assert app.state.db_engine is None
        assert app.state.db_session_factory is None


class TestRedisWiring:
    @pytest.fixture
    def healthy_redis(self, make_settings):
        settings = make_settings(redis_url="redis://example:6379/0")
        client = make_redis_client(
            settings, client=FakeAsyncRedis(decode_responses=True)
        )
        return settings, client

    async def test_ready_wired_onto_state(
        self, healthy_redis, monkeypatch
    ):
        settings, fake = healthy_redis
        monkeypatch.setattr("src.main.make_redis_client", lambda s: fake)
        app = create_app(settings)
        async with lifespan(app):
            assert app.state.redis is fake
            assert await app.state.redis.ping() is True
            # The wired client is the one serving future endpoints.
            await app.state.redis.set_json("probe", {"ok": 1})
            assert await app.state.redis.get_json("probe") == {"ok": 1}
        assert app.state.redis is None

    async def test_unreachable_degrades_with_warning(
        self, make_settings, caplog
    ):
        settings = make_settings(redis_url=UNREACHABLE_REDIS)
        app = create_app(settings)
        with caplog.at_level("WARNING"):
            async with lifespan(app):
                assert app.state.redis is not None
        assert find_event(
            caplog.records, "app.redis_unreachable_cache_degraded"
        ), "expected degraded-redis warning at startup"


class TestDbWiring:
    @pytest.fixture
    def postgres_settings(self, make_settings):
        return make_settings(database_url=UNREACHABLE_POSTGRES)

    async def test_ready_wires_usable_engine_and_factory(
        self, postgres_settings, monkeypatch, sqlite_engine, caplog
    ):
        monkeypatch.setattr(
            "src.main.create_db_engine", lambda s: sqlite_engine
        )
        app = create_app(postgres_settings)
        with caplog.at_level("INFO"):
            async with lifespan(app):
                assert app.state.db_engine is sqlite_engine
                assert app.state.db_session_factory is not None

                # The factory is genuinely usable, not just present.
                from src.db.session import session_scope

                async with sqlite_engine.begin() as connection:
                    await connection.execute(text(
                        "CREATE TABLE w (id INTEGER PRIMARY KEY, n TEXT)"
                    ))
                async with session_scope(
                    app.state.db_session_factory
                ) as session:
                    await session.execute(
                        text("INSERT INTO w (n) VALUES ('wired')")
                    )
                async with session_scope(
                    app.state.db_session_factory
                ) as session:
                    name = (await session.execute(
                        text("SELECT n FROM w")
                    )).scalar_one()
                assert name == "wired"
        assert find_event(caplog.records, "app.db_ready"), (
            "expected app.db_ready info at startup"
        )

    async def test_unreachable_continues_degraded(
        self, make_settings, caplog
    ):
        settings = make_settings(database_url=UNREACHABLE_POSTGRES)
        app = create_app(settings)
        with caplog.at_level("WARNING"):
            async with lifespan(app):
                # Engine still wired (lazy pool, retries per request).
                assert app.state.db_engine is not None
        assert find_event(
            caplog.records, "app.db_unreachable_continuing_degraded"
        ), "expected degraded-db warning at startup"

    async def test_engine_type_on_state(
        self, postgres_settings, monkeypatch, sqlite_engine
    ):
        monkeypatch.setattr(
            "src.main.create_db_engine", lambda s: sqlite_engine
        )
        app = create_app(postgres_settings)
        async with lifespan(app):
            assert isinstance(app.state.db_engine, AsyncEngine)


class TestShutdown:
    async def test_disposes_engine_and_closes_redis(
        self, make_settings, monkeypatch, sqlite_engine
    ):
        import src.main as main_module

        settings = make_settings(
            redis_url="redis://example:6379/0",
            database_url=UNREACHABLE_POSTGRES,
        )
        fake = make_redis_client(
            settings, client=FakeAsyncRedis(decode_responses=True)
        )
        monkeypatch.setattr("src.main.make_redis_client", lambda s: fake)
        monkeypatch.setattr(
            "src.main.create_db_engine", lambda s: sqlite_engine
        )

        disposed: list[AsyncEngine] = []
        real_dispose = main_module.dispose_engine

        async def dispose_spy(engine: AsyncEngine) -> None:
            disposed.append(engine)
            await real_dispose(engine)

        monkeypatch.setattr("src.main.dispose_engine", dispose_spy)

        redis_closed: list[bool] = []
        real_aclose = fake.aclose

        async def aclose_spy() -> None:
            redis_closed.append(True)
            await real_aclose()

        fake.aclose = aclose_spy

        app = create_app(settings)
        async with lifespan(app):
            assert app.state.redis is fake
            assert app.state.db_engine is sqlite_engine
        # Shutdown disposed the DB pool, closed Redis, cleared state.
        assert disposed == [sqlite_engine]
        assert redis_closed == [True]
        assert app.state.redis is None
        assert app.state.db_engine is None
        assert app.state.db_session_factory is None
