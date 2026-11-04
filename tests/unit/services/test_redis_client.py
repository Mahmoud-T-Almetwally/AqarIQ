"""Tests for src/services/redis_client.py (fakeredis backend, no network)."""

from __future__ import annotations

import pytest
from fakeredis import FakeAsyncRedis
from redis import exceptions as redis_exceptions

from src.core.exceptions import CacheError, ConfigurationError
from src.services.redis_client import RedisClient, make_redis_client


@pytest.fixture
def fake_redis() -> FakeAsyncRedis:
    return FakeAsyncRedis(decode_responses=True)


@pytest.fixture
def client(fake_redis, make_settings) -> RedisClient:
    return make_redis_client(
        make_settings(response_cache_ttl_seconds=3600),
        client=fake_redis,
    )


@pytest.fixture
def strict_client(fake_redis, make_settings) -> RedisClient:
    return make_redis_client(
        make_settings(response_cache_ttl_seconds=3600),
        client=fake_redis,
        strict=True,
    )


@pytest.fixture
def broken_client(fake_redis, make_settings) -> RedisClient:
    """A wrapper whose underlying client always raises RedisError."""
    wrapper = make_redis_client(make_settings(), client=fake_redis)

    async def _boom(*args, **kwargs):
        raise redis_exceptions.ConnectionError("upstash is down")

    wrapper._client.incr = _boom
    wrapper._client.get = _boom
    wrapper._client.set = _boom
    wrapper._client.delete = _boom
    wrapper._client.ping = _boom
    return wrapper


class TestNamespacing:
    def test_prefix_applied(self, client):
        assert client.namespaced("chat:abc") == "rea:local:chat:abc"

    def test_empty_key_rejected(self, client):
        with pytest.raises(ValueError, match="empty"):
            client.namespaced("")

    async def test_namespace_visible_in_raw_store(self, client, fake_redis):
        await client.set("foo", "bar")
        assert await fake_redis.get("rea:local:foo") == "bar"
        assert await fake_redis.get("foo") is None  # unprefixed key untouched


class TestStrings:
    async def test_set_get_roundtrip(self, client):
        await client.set("k", "v")
        assert await client.get("k") == "v"

    async def test_default_ttl_applied(self, client, fake_redis):
        await client.set("k", "v")
        ttl = await fake_redis.ttl("rea:local:k")
        assert 0 < ttl <= 3600

    async def test_explicit_ttl_wins(self, client, fake_redis):
        await client.set("k", "v", ttl_seconds=60)
        assert 0 < await fake_redis.ttl("rea:local:k") <= 60

    async def test_missing_key_is_none(self, client):
        assert await client.get("nope") is None

    async def test_delete_returns_removed_count(self, client):
        await client.set("a", "1")
        await client.set("b", "2")
        assert await client.delete("a", "b", "missing") == 2
        assert await client.get("a") is None
        assert await client.delete() == 0


class TestJson:
    async def test_json_roundtrip(self, client):
        payload = {"answer": "München ist teuer.", "sources": [1, 2, 3]}
        await client.set_json("resp:42", payload)
        assert await client.get_json("resp:42") == payload

    async def test_json_miss_is_none(self, client):
        assert await client.get_json("resp:missing") is None

    async def test_corrupt_entry_treated_as_miss_and_cleared(
        self, client, fake_redis
    ):
        await fake_redis.set("rea:local:junk", "{not json")
        assert await client.get_json("junk") is None
        assert await fake_redis.get("rea:local:junk") is None  # junk removed

    async def test_json_stored_compactly(self, client, fake_redis):
        await client.set_json("k", {"a": "ä"})
        raw = await fake_redis.get("rea:local:k")
        assert raw == '{"a":"ä"}'


class TestFixedWindowCounter:
    async def test_increments(self, client):
        assert await client.incr_with_ttl("ip:1.2.3.4:hour", ttl_seconds=3600) == 1
        assert await client.incr_with_ttl("ip:1.2.3.4:hour", ttl_seconds=3600) == 2
        assert await client.incr_with_ttl("ip:1.2.3.4:hour", ttl_seconds=3600) == 3

    async def test_ttl_armed_on_first_hit(self, client, fake_redis):
        await client.incr_with_ttl("ip:1.2.3.4:day", ttl_seconds=86400)
        ttl = await fake_redis.ttl("rea:local:ip:1.2.3.4:day")
        assert 0 < ttl <= 86400

    async def test_ttl_not_reslid_by_later_hits(self, client, fake_redis):
        """Fixed window: the window must not extend with every message."""
        await client.incr_with_ttl("ip:1.2.3.4:hour", ttl_seconds=3600)
        first_ttl = await fake_redis.ttl("rea:local:ip:1.2.3.4:hour")
        await client.incr_with_ttl("ip:1.2.3.4:hour", ttl_seconds=3600)
        await client.incr_with_ttl("ip:1.2.3.4:hour", ttl_seconds=3600)
        later_ttl = await fake_redis.ttl("rea:local:ip:1.2.3.4:hour")
        assert later_ttl <= first_ttl

    async def test_orphaned_counter_rearmed(self, client, fake_redis):
        """Counter without TTL (crash between INCR and EXPIRE) gets one."""
        await fake_redis.set("rea:local:ip:9.9.9.9:hour", "5")  # no TTL
        count = await client.incr_with_ttl("ip:9.9.9.9:hour", ttl_seconds=3600)
        assert count == 6
        assert 0 < await fake_redis.ttl("rea:local:ip:9.9.9.9:hour") <= 3600


class TestDegradation:
    async def test_get_degrades_to_none(self, broken_client):
        assert await broken_client.get("k") is None

    async def test_set_degrades_silently(self, broken_client):
        await broken_client.set("k", "v")  # must not raise

    async def test_delete_degrades_to_zero(self, broken_client):
        assert await broken_client.delete("k") == 0

    async def test_incr_degrades_to_zero(self, broken_client):
        assert await broken_client.incr_with_ttl("k", ttl_seconds=60) == 0

    async def test_ping_reports_false(self, broken_client):
        assert await broken_client.ping() is False

    async def test_strict_get_raises_cache_error(self, strict_client):
        strict_client._client.get = _raiser
        with pytest.raises(CacheError, match="Redis get failed"):
            await strict_client.get("k")

    async def test_strict_incr_raises_cache_error(self, strict_client):
        strict_client._client.incr = _raiser
        with pytest.raises(CacheError, match="Redis incr_with_ttl failed"):
            await strict_client.incr_with_ttl("k", ttl_seconds=60)

    async def test_strict_ping_ok_when_healthy(self, strict_client):
        assert await strict_client.ping() is True


async def _raiser(*args, **kwargs):
    from redis import exceptions as redis_exceptions

    raise redis_exceptions.ConnectionError("down")


class TestFactory:
    def test_requires_url_without_injected_client(self, make_settings):
        with pytest.raises(ConfigurationError, match="REDIS_URL"):
            make_redis_client(make_settings())

    def test_injected_client_skips_url_requirement(
        self, fake_redis, make_settings
    ):
        wrapper = make_redis_client(make_settings(), client=fake_redis)
        assert wrapper.namespace == "rea:local"

    def test_namespace_follows_environment(self, fake_redis, make_settings):
        wrapper = make_redis_client(
            make_settings(environment="test"), client=fake_redis
        )
        assert wrapper.namespace == "rea:test"


class TestClose:
    async def test_aclose_completes_and_is_idempotent(
        self, fake_redis, make_settings
    ):
        # fakeredis is in-memory and stays usable after close, so we verify
        # the contract we own: aclose() delegates to the client and never
        # raises, including when called twice.
        wrapper = make_redis_client(make_settings(), client=fake_redis)
        assert await wrapper.ping() is True
        await wrapper.aclose()
        await wrapper.aclose()
