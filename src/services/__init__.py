"""Service-layer wrappers around external infrastructure (Tiers 1-4).

One module per dependency, each owning its error mapping, retry policy,
and degradation semantics; the future app factory builds them all from
settings at startup and closes them on shutdown::

    clients = make_llm_clients(settings)
    redis = make_redis_client(settings)
    storage = make_storage_client(settings)
    mq = make_mq_client(settings)

Rate-limit awareness (:mod:`src.services.rate_limit`) is threaded through
the LLM clients via a shared registry so the failover pool can skip
exhausted providers without spending a request to find out.
"""

from src.services.llm_client import LLMClient, LLMResponse, make_llm_clients
from src.services.mq_client import MQClient, make_mq_client
from src.services.rate_limit import (
    ProviderQuota,
    RateLimitRegistry,
    get_rate_limit_registry,
)
from src.services.redis_client import RedisClient, make_redis_client
from src.services.storage_client import StorageClient, make_storage_client

__all__ = [
    # LLM providers
    "LLMClient",
    "LLMResponse",
    "make_llm_clients",
    # Rate-limit tracking
    "ProviderQuota",
    "RateLimitRegistry",
    "get_rate_limit_registry",
    # Redis (Upstash)
    "RedisClient",
    "make_redis_client",
    # Object storage (Neon Object Storage)
    "StorageClient",
    "make_storage_client",
    # RabbitMQ (CloudAMQP)
    "MQClient",
    "make_mq_client",
]
