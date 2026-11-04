"""RabbitMQ (CloudAMQP) client — ETL queue, DLQ topology, depth checks.

Owns the broker connection for both sides of the pipeline:

* **API side**: :meth:`MQClient.publish` enqueues listing payloads for the
  ETL consumer (Project-Description §5). Messages are persistent JSON with
  a UUID ``message_id``, published ``mandatory`` so an unroutable message
  (queue deleted out from under us) surfaces as an error instead of
  vanishing.
* **Consumer side**: :meth:`MQClient.consumer_channel` yields a
  prefetch-limited channel plus the declared queue; the consumer loop
  (ack/nack policy) is built on this in the ETL phase.

Topology is declared idempotently on connect — DLQ first, then the main
queue wired to it broker-side (``x-dead-letter-exchange`` /
  ``x-dead-letter-routing-key``): a consumer ``nack(requeue=False)`` lands
in the DLQ automatically with ``x-death`` headers, no client-side
republishing to get wrong. The connection is ``connect_robust``: CloudAMQP
restarts and idle disconnects are survived without dropping the consumer.

:meth:`queue_depth` (passive declare) backs the §8 operational guard —
alert when depth crosses ``queue_depth_alert_threshold`` (5 000,
comfortably under CloudAMQP's 20 000 queued-message cap).

Error policy: every broker error surfaces as
:class:`~src.core.exceptions.MessageQueueError` with the operation in
``details``. Publishes retry up to ``mq_publish_attempts`` on connection
trouble (channel acquisition included); an unroutable message is a
topology problem and fails immediately — retrying cannot fix it.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import aio_pika
from aio_pika import DeliveryMode, Message
from aio_pika.exceptions import DeliveryError

from src.core.config import Settings
from src.core.exceptions import MessageQueueError
from src.core.logging import get_logger

logger = get_logger(__name__)

_PUBLISH_BACKOFF_SECONDS = 1.0


class MQClient:
    """Robust-connection wrapper over one CloudAMQP virtual host.

    With an injected ``connection`` (tests, embedded brokers) the client
    never dials out; a closed connection of any kind is dropped and, on
    the next operation, re-established from the configured URL.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        connection: Any = None,
    ) -> None:
        self.settings = settings
        self._connection: Any = connection
        self._channel: Any = None
        self._topology_declared = False

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connection is not None and not self._connection.is_closed

    async def connect(self) -> None:
        """Ensure a live connection and declared topology. Idempotent."""
        if self.is_connected:
            await self._ensure_topology()
            return
        url_user = None
        if self._connection is not None:
            # Closed connections are worthless; drop them so the path
            # below re-establishes from the configured URL.
            self._connection = None
        if self._connection is None:
            url = self.settings.effective_amqp_url
            url_user = self._url_user(url)
            try:
                self._connection = await aio_pika.connect_robust(
                    url,
                    timeout=self.settings.mq_connect_timeout_seconds,
                    client_properties={"product": self.settings.app_name},
                )
            except Exception as error:
                raise self._wrap(error, "connect") from error
        self._channel = None
        self._topology_declared = False
        await self._ensure_topology()
        logger.info("mq.connected", url_user=url_user)

    async def aclose(self) -> None:
        connection, self._connection = self._connection, None
        self._channel = None
        self._topology_declared = False
        if connection is None:
            return
        try:
            await connection.close()
        except Exception as error:
            # Closing an already-dead broker must not mask the real error
            # a caller is handling during shutdown.
            logger.warning("mq.close_failed", error=str(error))

    async def _channel_of(self) -> Any:
        if self._channel is not None and not self._channel.is_closed:
            return self._channel
        if not self.is_connected:
            await self.connect()
        try:
            self._channel = await self._connection.channel()
        except Exception as error:
            raise self._wrap(error, "open_channel") from error
        return self._channel

    async def _ensure_topology(self) -> None:
        if self._topology_declared:
            return
        channel = await self._channel_of()
        try:
            # DLQ first: the main queue's dead-letter wiring points at it.
            await channel.declare_queue(
                self.settings.etl_dlq_name, durable=True
            )
            await channel.declare_queue(
                self.settings.etl_queue_name,
                durable=True,
                arguments=self._dlx_arguments(),
            )
        except MessageQueueError:
            raise
        except Exception as error:
            raise self._wrap(error, "declare_topology") from error
        self._topology_declared = True
        logger.info(
            "mq.topology_declared",
            queue=self.settings.etl_queue_name,
            dlq=self.settings.etl_dlq_name,
        )

    def _dlx_arguments(self) -> dict[str, Any]:
        # Broker-side dead-lettering: rejected (requeue=False) or expired
        # messages land in the DLQ with x-death headers — the consumer
        # never republishes by hand.
        return {
            "x-dead-letter-exchange": "",
            "x-dead-letter-routing-key": self.settings.etl_dlq_name,
        }

    # ------------------------------------------------------------------
    # Publishing (API side)
    # ------------------------------------------------------------------

    async def publish(
        self,
        payload: dict[str, Any],
        *,
        message_id: str | None = None,
        headers: dict[str, Any] | None = None,
    ) -> str:
        """Enqueue one JSON payload on the ETL queue. Returns message_id.

        Connection trouble (including channel acquisition after a blip)
        retries up to ``mq_publish_attempts`` with linear backoff; an
        unroutable message fails fast — missing topology is not transient.
        """
        message_id = message_id or uuid.uuid4().hex
        body = json.dumps(
            payload, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        message = Message(
            body,
            content_type="application/json",
            delivery_mode=DeliveryMode.PERSISTENT,
            message_id=message_id,
            headers=headers or {},
        )

        attempts = self.settings.mq_publish_attempts
        for attempt in range(attempts):
            try:
                await self._ensure_topology()
                channel = await self._channel_of()
                await channel.default_exchange.publish(
                    message,
                    routing_key=self.settings.etl_queue_name,
                    mandatory=True,
                )
                logger.debug(
                    "mq.published", message_id=message_id, bytes=len(body)
                )
                return message_id
            except DeliveryError as error:
                raise self._wrap(
                    error,
                    "publish",
                    extra={"reason": "unroutable", "message_id": message_id},
                    retryable=False,
                ) from error
            except MessageQueueError as error:
                if attempt + 1 >= attempts:
                    raise
                logger.warning(
                    "mq.publish_retry",
                    attempt=attempt + 1,
                    message_id=message_id,
                    error=str(error),
                )
                await asyncio.sleep(_PUBLISH_BACKOFF_SECONDS * (attempt + 1))
            except Exception as error:
                if attempt + 1 >= attempts:
                    raise self._wrap(
                        error, "publish", extra={"message_id": message_id}
                    ) from error
                logger.warning(
                    "mq.publish_retry",
                    attempt=attempt + 1,
                    message_id=message_id,
                    error=str(error),
                )
                await asyncio.sleep(_PUBLISH_BACKOFF_SECONDS * (attempt + 1))
        raise MessageQueueError(  # unreachable; for the type checker
            "publish loop exited unexpectedly",
            details={"message_id": message_id},
        )

    # ------------------------------------------------------------------
    # Consumer side (ETL phase)
    # ------------------------------------------------------------------

    async def consumer_channel(self) -> tuple[Any, Any]:
        """A prefetch-limited channel plus the declared ETL queue.

        Prefetch comes from settings so one slow embedding batch cannot
        hoard thousands of unacked messages (CloudAMQP memory limits).
        """
        await self._ensure_topology()
        channel = await self._channel_of()
        try:
            await channel.set_qos(
                prefetch_count=self.settings.mq_prefetch_count
            )
            queue = await channel.declare_queue(
                self.settings.etl_queue_name,
                durable=True,
                arguments=self._dlx_arguments(),
            )
        except MessageQueueError:
            raise
        except Exception as error:
            raise self._wrap(error, "consumer_channel") from error
        return channel, queue

    # ------------------------------------------------------------------
    # Operations support (§8)
    # ------------------------------------------------------------------

    async def queue_depth(self, *, queue: str | None = None) -> int:
        """Ready messages in the queue (passive declare — no side effects)."""
        name = queue or self.settings.etl_queue_name
        channel = await self._channel_of()
        try:
            declared = await channel.declare_queue(name, passive=True)
        except MessageQueueError:
            raise
        except Exception as error:
            raise self._wrap(error, "queue_depth") from error
        result = getattr(declared, "declaration_result", None)
        return int(getattr(result, "message_count", 0) or 0)

    async def is_healthy(self) -> bool:
        """Connectivity + depth probe; never raises (health endpoint)."""
        try:
            await self.connect()
            depth = await self.queue_depth()
        except Exception as error:
            logger.error("mq.health_failed", error=str(error))
            return False
        return depth < self.settings.queue_depth_alert_threshold

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _wrap(
        self,
        error: Exception,
        operation: str,
        *,
        extra: dict[str, Any] | None = None,
        retryable: bool = True,
    ) -> MessageQueueError:
        details = {"operation": operation, "broker_error": type(error).__name__}
        if extra:
            details.update(extra)
        return MessageQueueError(
            f"RabbitMQ {operation} failed: {error}",
            details=details,
            retryable=retryable,
        )

    @staticmethod
    def _url_user(url: str) -> str | None:
        """User label for logs, without the credentials."""
        try:
            from yarl import URL

            return URL(url).user or None
        except Exception:
            return None


def make_mq_client(
    settings: Settings, *, connection: Any = None
) -> MQClient:
    """Build the wrapper. An injected ``connection`` (tests, embedded
    brokers) bypasses CloudAMQP entirely."""
    return MQClient(settings, connection=connection)
