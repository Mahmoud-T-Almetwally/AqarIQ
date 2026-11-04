"""Tests for src/services/mq_client.py.

Uses a hand-rolled fake of the aio-pika surface MQClient touches
(connection → channel → declare_queue / default_exchange), plus one
monkeypatched ``connect_robust`` for the real-connect path. No broker.
"""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import aio_pika
import pytest
from aio_pika.exceptions import DeliveryError

from src.core.exceptions import MessageQueueError
from src.services.mq_client import MQClient, make_mq_client

QUEUE = "etl.listings"
DLQ = "etl.listings.dlq"


class FakeDeclareResult:
    def __init__(self, message_count: int = 0) -> None:
        self.message_count = message_count


class FakeQueue:
    def __init__(self, name: str, *, depth: int = 0, arguments: dict | None = None):
        self.name = name
        self.declaration_result = FakeDeclareResult(depth)
        self.arguments = arguments

    async def declare(self):
        return self.declaration_result


class FakeExchange:
    def __init__(self, queues: dict[str, list], unroutable: set[str]) -> None:
        self._queues = queues
        self._unroutable = unroutable
        self.publish_calls = 0

    async def publish(self, message, routing_key, *, mandatory=True, **kwargs):
        self.publish_calls += 1
        if routing_key in self._unroutable or routing_key not in self._queues:
            # Broker behaviour with mandatory=True and no route.
            raise DeliveryError(None, SimpleNamespace(name="basic.return"))
        self._queues[routing_key].append(message)
        return message


class FakeChannel:
    def __init__(
        self, queues: dict[str, list], depths: dict[str, int], unroutable: set[str]
    ) -> None:
        self.is_closed = False
        self.declared: list[tuple[str, dict | None]] = []
        self.prefetch: int | None = None
        self.default_exchange = FakeExchange(queues, unroutable)
        self._queues = queues
        self._depths = depths

    async def set_qos(self, *, prefetch_count, **kwargs):
        self.prefetch = prefetch_count

    async def declare_queue(self, name, *, durable=False, passive=False,
                            arguments=None, **kwargs):
        if passive:
            if name not in self._depths:
                raise LookupError(f"queue {name!r} does not exist")
            return FakeQueue(name, depth=self._depths[name])
        self._queues.setdefault(name, [])
        self._depths.setdefault(name, 0)
        self.declared.append((name, arguments))
        return FakeQueue(name, depth=self._depths[name], arguments=arguments)


class FakeConnection:
    """Mimics aio-pika's connection where channel() is a coroutine method."""

    def __init__(self) -> None:
        self.is_closed = False
        self._channel: FakeChannel | None = None
        self.queues: dict[str, list] = {QUEUE: []}
        self.depths: dict[str, int] = {QUEUE: 0}
        self.unroutable: set[str] = set()
        self.channel_calls = 0

    async def channel(self):
        self.channel_calls += 1
        if self._channel is None:
            self._channel = FakeChannel(self.queues, self.depths, self.unroutable)
        return self._channel

    async def close(self):
        self.is_closed = True


@pytest.fixture
def fake_connection() -> FakeConnection:
    return FakeConnection()


@pytest.fixture
def client(fake_connection, make_settings) -> MQClient:
    settings = make_settings(
        etl_queue_name=QUEUE,
        etl_dlq_name=DLQ,
        mq_publish_attempts=2,
    )
    return make_mq_client(settings, connection=fake_connection)


class TestTopology:
    async def test_declares_dlq_and_main_with_dlx_wiring(self, client):
        await client.connect()
        declared = dict(client._channel.declared)
        assert declared[DLQ] is None  # plain durable queue
        assert declared[QUEUE] == {
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": DLQ,
        }

    async def test_declare_order_dlq_first(self, client):
        await client.connect()
        names = [name for name, _ in client._channel.declared]
        assert names.index(DLQ) < names.index(QUEUE)

    async def test_connect_is_idempotent(self, client):
        await client.connect()
        await client.connect()
        # topology declared exactly once (DLQ + main)
        assert len(client._channel.declared) == 2
        assert client.is_connected is True


class TestPublish:
    async def test_publish_persistent_json_with_id(
        self, client, fake_connection
    ):
        message_id = await client.publish({"listing_id": 42, "source": "immo"})
        published = fake_connection.queues[QUEUE][0]
        assert json.loads(published.body) == {"listing_id": 42, "source": "immo"}
        assert published.content_type == "application/json"
        assert published.delivery_mode == aio_pika.DeliveryMode.PERSISTENT
        assert published.message_id == message_id

    async def test_publish_honors_explicit_message_id_and_headers(
        self, client, fake_connection
    ):
        await client.publish(
            {"k": 1}, message_id="fixed", headers={"trace": "abc"}
        )
        published = fake_connection.queues[QUEUE][0]
        assert published.message_id == "fixed"
        assert published.headers["trace"] == "abc"

    async def test_unroutable_is_not_retried(self, make_settings):
        settings = make_settings(
            etl_queue_name="gone.queue", mq_publish_attempts=5
        )
        connection = FakeConnection()
        connection.unroutable.add("gone.queue")
        client = make_mq_client(settings, connection=connection)
        with pytest.raises(MessageQueueError) as exc_info:
            await client.publish({"k": 1})
        assert exc_info.value.retryable is False
        assert exc_info.value.details["reason"] == "unroutable"
        # failed fast: exactly one publish attempt hit the exchange
        assert connection._channel.default_exchange.publish_calls == 1

    async def test_channel_blip_retried_then_succeeds(
        self, client, fake_connection, monkeypatch
    ):
        sleeps: list[float] = []

        async def _sleep(seconds):
            sleeps.append(seconds)

        monkeypatch.setattr(asyncio, "sleep", _sleep)

        async def _flaky_channel(self):
            self.channel_calls += 1
            if self.channel_calls == 1:
                raise aio_pika.exceptions.AMQPConnectionError("blip")
            if self._channel is None:
                self._channel = FakeChannel(
                    self.queues, self.depths, self.unroutable
                )
            return self._channel

        monkeypatch.setattr(FakeConnection, "channel", _flaky_channel)
        message_id = await client.publish({"k": 1})
        assert fake_connection.channel_calls == 2
        assert sleeps == [1.0]  # linear backoff: 1s on first retry
        assert message_id

    async def test_persistent_channel_errors_raise(self, client, monkeypatch):
        async def _sleep(seconds):
            return None

        monkeypatch.setattr(asyncio, "sleep", _sleep)

        async def _dead_channel(self):
            raise aio_pika.exceptions.AMQPConnectionError("down for good")

        monkeypatch.setattr(FakeConnection, "channel", _dead_channel)
        client.settings.mq_publish_attempts = 1
        with pytest.raises(MessageQueueError, match="open_channel"):
            await client.publish({"k": 1})


class TestDepthAndHealth:
    async def test_queue_depth_passive(self, client, fake_connection):
        fake_connection.depths[QUEUE] = 1234
        assert await client.queue_depth() == 1234

    async def test_queue_depth_missing_queue_raises(self, client):
        with pytest.raises(MessageQueueError, match="queue_depth"):
            await client.queue_depth(queue="not.declared")

    async def test_healthy_under_threshold(self, client, fake_connection):
        fake_connection.depths[QUEUE] = 4999
        assert await client.is_healthy() is True

    async def test_unhealthy_over_threshold(self, client, fake_connection):
        fake_connection.depths[QUEUE] = 5001
        assert await client.is_healthy() is False

    async def test_unhealthy_on_connection_failure(self, make_settings):
        class DeadConnection:
            is_closed = False

            async def channel(self):
                raise aio_pika.exceptions.AMQPConnectionError("nope")

            async def close(self):
                return None

        dead = make_mq_client(make_settings(), connection=DeadConnection())
        assert await dead.is_healthy() is False


class TestConsumerChannel:
    async def test_prefetch_applied_from_settings(self, client):
        client.settings.mq_prefetch_count = 3
        channel, queue = await client.consumer_channel()
        assert channel.prefetch == 3
        assert queue.name == QUEUE

    async def test_consumer_queue_declared_with_dlx(self, client):
        _, queue = await client.consumer_channel()
        assert queue.arguments["x-dead-letter-routing-key"] == DLQ


class TestClose:
    async def test_aclose_closes_connection_and_resets_state(
        self, client, fake_connection
    ):
        await client.connect()
        await client.aclose()
        assert fake_connection.is_closed is True
        assert client.is_connected is False
        assert client._channel is None
        assert client._topology_declared is False

    async def test_aclose_on_never_connected_client(self, make_settings):
        client = make_mq_client(make_settings())
        await client.aclose()  # must not raise

    async def test_aclose_survives_broker_close_failure(self, make_settings):
        class UnClosableConnection:
            is_closed = False

            async def channel(self):
                return FakeChannel({}, {}, set())

            async def close(self):
                raise RuntimeError("already gone")

        client = make_mq_client(
            make_settings(), connection=UnClosableConnection()
        )
        await client.aclose()  # logs a warning, does not raise


class TestRealConnectPath:
    async def test_connect_uses_effective_url_and_timeout(self, monkeypatch):
        captured = {}
        url = "amqps://user:pw@host.cloudamqp.com/user"

        async def _fake_connect_robust(url_arg, **kwargs):
            captured["url"] = url_arg
            captured["timeout"] = kwargs.get("timeout")
            captured["product"] = kwargs.get("client_properties", {}).get("product")
            return FakeConnection()

        monkeypatch.setattr(aio_pika, "connect_robust", _fake_connect_robust)

        class _Settings:
            effective_amqp_url = url
            mq_connect_timeout_seconds = 7.5
            app_name = "RealEstateAnalysis"
            etl_queue_name = QUEUE
            etl_dlq_name = DLQ

        client = MQClient(_Settings())
        await client.connect()
        assert captured["url"] == url
        assert captured["timeout"] == 7.5
        assert captured["product"] == "RealEstateAnalysis"
        # topology still declared over the injected fake
        assert len(client._channel.declared) == 2
        await client.aclose()


class TestUrlUser:
    def test_extracts_user_without_credentials(self):
        assert MQClient._url_user("amqps://u:p@h.cloudamqp.com/u") == "u"

    def test_no_user(self):
        assert MQClient._url_user("amqp://localhost:5672") is None

    def test_garbage_url(self):
        assert MQClient._url_user("not a url") is None
