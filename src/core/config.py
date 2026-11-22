"""Application settings — the single source of truth for configuration.

Values come from environment variables (Render / GitHub Actions inject them
at runtime) and, in local development, from a git-ignored ``.env`` file at
the repository root. Field names map to upper-cased environment variables
(``database_url`` ← ``DATABASE_URL``); the file is flat on purpose so the
names match what providers inject (``DATABASE_URL``, ``CLOUDAMQP_URL``, …)
rather than a nested naming scheme the platforms will never produce.

Design notes:

* **Fail fast in production.** A model validator collects *every* missing
  required setting and raises one :class:`~src.core.exceptions.ConfigurationError`
  at startup, instead of the first ``KeyError`` surfacing mid-request days
  later. Local and test environments stay lenient so modules can be
  developed and unit-tested without any credentials.
* **Secrets are ``SecretStr``** so accidental ``repr``/dump of settings
  never leaks keys; :meth:`Settings.safe_summary` exposes a deliberately
  non-secret snapshot for the startup log line.
* Provider details (model names, base URLs, quotas-derived knobs) live here
  too: free-tier model lineups change without notice, and swapping a model
  must be a deploy-time env change, not a code change.
"""

from __future__ import annotations

from enum import Enum
from functools import lru_cache
from typing import Annotated, Any, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from src.core.exceptions import ConfigurationError


class Environment(str, Enum):
    LOCAL = "local"
    TEST = "test"
    PRODUCTION = "production"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        # Cloud platforms inject unrelated variables (PORT, RENDER_*,
        # GITHUB_*); unknown ones must not crash startup.
        extra="ignore",
    )

    # ------------------------------------------------------------------
    # Application
    # ------------------------------------------------------------------
    app_name: str = "RealEstateAnalysis"
    app_version: str = "0.1.0"
    api_v1_prefix: str = "/api/v1"

    environment: Environment = Environment.LOCAL
    # None = derive from environment (True locally, False in production);
    # an explicit True in production is rejected by the validator below.
    debug: bool | None = None
    log_level: str = "INFO"
    # None = JSON logs in production, console logs elsewhere.
    log_json: bool | None = None

    # Signing key for anything the API issues itself (future: session
    # tokens, CSRF). Not the internal-API shared secret.
    secret_key: SecretStr | None = None
    # Shared secret required by /internal/* endpoints (GitHub Actions,
    # deep health checks). See Project-Description §5.
    internal_api_secret: SecretStr | None = None

    # Comma-separated in env vars ("https://a.com, https://b.com").
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # ------------------------------------------------------------------
    # Tier 2 — PostgreSQL (Neon)
    # ------------------------------------------------------------------
    database_url: str | None = None
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout_seconds: int = 30
    db_echo: bool = False
    # Guardrail for LLM-proposed SQL (Project-Description §5).
    db_statement_timeout_ms: int = 5000

    # ------------------------------------------------------------------
    # Tier 4 — Redis (Upstash)
    # ------------------------------------------------------------------
    redis_url: str | None = None
    redis_key_prefix: str = "rea"
    # Exact-match response cache TTL (Project-Description §5).
    response_cache_ttl_seconds: int = 3600

    # ------------------------------------------------------------------
    # RabbitMQ (CloudAMQP)
    # ------------------------------------------------------------------
    amqp_url: SecretStr | None = None
    # Render injects this name when the CloudAMQP addon is attached.
    cloudamqp_url: SecretStr | None = None
    etl_queue_name: str = "etl.listings"
    etl_dlq_name: str = "etl.listings.dlq"
    mq_prefetch_count: int = 10
    mq_connect_timeout_seconds: float = 10.0
    # Publish attempts before the request fails (robust connection
    # reconnects underneath; 1 means "fail on first error").
    mq_publish_attempts: int = 2
    # Alert threshold well under CloudAMQP's 20k queued-message cap (§8).
    queue_depth_alert_threshold: int = 5000

    # ------------------------------------------------------------------
    # LLM providers
    # ------------------------------------------------------------------
    gemini_api_key: SecretStr | None = None
    gemini_base_url: str = "https://generativelanguage.googleapis.com/v1beta/openai"
    gemini_primary_model: str = "gemini-3.5-flash-lite"
    gemini_fallback_model: str = "gemini-3.1-flash-lite"
    gemini_embedding_model: str = "gemini-embedding-2"

    groq_api_key: SecretStr | None = None
    groq_base_url: str = "https://api.groq.com/openai/v1"
    groq_model: str = "openai/gpt-oss-120b"
    groq_fallback_model: str = "qwen/qwen3.8-27b"  # ETL tier: fresh 8K TPM / 200K TPD bucket
    groq_prompt_guard_model: str = "meta-llama/llama-prompt-guard-2-86m"

    # Jev intent-router provider (vLLM-SR).
    sr_api_key: SecretStr | None = None
    sr_base_url: str | None = None
    sr_router_model: str = "vllm-sr/d3-turbo"

    llm_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    # Matryoshka truncation target for pgvector halfvec storage (§4).
    embedding_dimensions: int = 768

    # ------------------------------------------------------------------
    # Tier 1 — Object lake (Neon Object Storage, S3-compatible)
    # ------------------------------------------------------------------
    s3_endpoint_url: str | None = None
    s3_region: str | None = None
    s3_bucket: str | None = None
    s3_access_key_id: SecretStr | None = None
    s3_secret_access_key: SecretStr | None = None

    # ------------------------------------------------------------------
    # Abuse protection (Project-Description §5)
    # ------------------------------------------------------------------
    rate_limit_messages_per_hour: int = 10
    rate_limit_messages_per_day: int = 20

    # ------------------------------------------------------------------
    # Validators
    # ------------------------------------------------------------------

    @field_validator("environment", mode="before")
    @classmethod
    def _normalize_environment(cls, value: Any) -> Any:
        if isinstance(value, str):
            aliases = {
                "dev": "local",
                "development": "local",
                "ci": "test",
                "prod": "production",
            }
            return aliases.get(value.strip().lower(), value.strip().lower())
        return value

    @field_validator("log_level", mode="before")
    @classmethod
    def _normalize_log_level(cls, value: Any) -> Any:
        if isinstance(value, str):
            normalized = value.strip().upper()
            import logging

            if not isinstance(getattr(logging, normalized, None), int):
                raise ValueError(f"Unknown log level: {value!r}")
            return normalized
        return value

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: Any) -> Any:
        if value is None or value == "":
            return []
        if isinstance(value, str):
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        return value

    @field_validator("database_url", "redis_url")
    @classmethod
    def _check_url_scheme(cls, value: Any, info: Any) -> Any:
        if not isinstance(value, str) or not value:
            return value
        expected = {
            "database_url": ("postgresql://", "postgres://", "postgresql+"),
            "redis_url": ("redis://", "rediss://"),
        }[info.field_name]
        if not value.startswith(expected):
            raise ValueError(
                f"{info.field_name} must start with one of {expected}"
            )
        return value

    @model_validator(mode="after")
    def _validate_production(self) -> Self:
        if self.environment is not Environment.PRODUCTION:
            return self

        problems: list[str] = []
        if self.debug is True:
            problems.append("DEBUG must not be true in production")
        if self.log_level == "DEBUG":
            problems.append("LOG_LEVEL cannot be DEBUG in production")

        checks: dict[str, bool] = {
            "DATABASE_URL": bool(self.database_url),
            "REDIS_URL": bool(self.redis_url),
            "AMQP_URL (or CLOUDAMQP_URL)": self._amqp_url_value() is not None,
            "GEMINI_API_KEY": self._secret_set(self.gemini_api_key),
            "GROQ_API_KEY": self._secret_set(self.groq_api_key),
            "SR_API_KEY": self._secret_set(self.sr_api_key),
            "SR_BASE_URL": bool(self.sr_base_url),
            "S3_ENDPOINT_URL": bool(self.s3_endpoint_url),
            "S3_BUCKET": bool(self.s3_bucket),
            "S3_ACCESS_KEY_ID": self._secret_set(self.s3_access_key_id),
            "S3_SECRET_ACCESS_KEY": self._secret_set(self.s3_secret_access_key),
            "SECRET_KEY": self._secret_set(self.secret_key),
            "INTERNAL_API_SECRET": self._secret_set(self.internal_api_secret),
        }
        missing = [name for name, is_set in checks.items() if not is_set]
        if missing:
            problems.append(f"missing required settings: {', '.join(missing)}")

        if problems:
            raise ConfigurationError(
                "Invalid production configuration: " + "; ".join(problems),
                details={"problems": problems},
            )
        return self

    # ------------------------------------------------------------------
    # ETL pipeline
    # ------------------------------------------------------------------
    usd_to_egp_rate: float = 49.5
    etl_groq_batch_size: int = 5  # token-bounded: 8K TPM must fit one batch + answer
    etl_groq_minute_token_budget: int = 7_500  # buffer under Groq 8K TPM (per model)
    etl_groq_daily_token_budget: int = 180_000  # buffer under Groq 200K TPD (per model)
    etl_embedding_batch_size: int = 20
    etl_max_image_urls: int = 6

    # ------------------------------------------------------------------
    # Chat file attachments (strictly bounded, stored compressed in lake)
    # ------------------------------------------------------------------
    chat_max_attachments_per_message: int = 2
    chat_max_attachments_per_chat: int = 6
    chat_max_attachment_bytes: int = 2 * 1024 * 1024  # 2 MiB per file
    chat_allowed_attachment_extensions: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: [".jpg", ".jpeg", ".png", ".webp", ".pdf"]
    )

    # ------------------------------------------------------------------
    # Derived values
    # ------------------------------------------------------------------

    @property
    def is_production(self) -> bool:
        return self.environment is Environment.PRODUCTION

    @property
    def is_testing(self) -> bool:
        return self.environment is Environment.TEST

    @property
    def debug_enabled(self) -> bool:
        if self.debug is not None:
            return self.debug
        return not self.is_production

    @property
    def log_json_resolved(self) -> bool:
        if self.log_json is not None:
            return self.log_json
        return self.is_production

    @property
    def redis_namespace(self) -> str:
        """Per-environment prefix for every Redis key (no dev/prod collisions)."""
        return f"{self.redis_key_prefix}:{self.environment.value}"

    @property
    def async_database_url(self) -> str:
        """DATABASE_URL rewritten for SQLAlchemy's asyncpg driver.

        SSL/connection kwargs are deliberately left to the session layer
        (``db/session.py``), which knows the engine options asyncpg needs.
        """
        url = self.database_url
        if not url:
            raise ConfigurationError(
                "DATABASE_URL is not configured",
                details={"missing": ["DATABASE_URL"]},
            )
        if url.startswith("postgresql+"):
            return url
        if url.startswith("postgres://"):
            return "postgresql+asyncpg://" + url[len("postgres://") :]
        return "postgresql+asyncpg://" + url[len("postgresql://") :]

    @property
    def effective_amqp_url(self) -> str:
        """AMQP_URL if set, else the CLOUDAMQP_URL injected by Render."""
        value = self._amqp_url_value()
        if value is None:
            raise ConfigurationError(
                "No AMQP URL configured (set AMQP_URL or CLOUDAMQP_URL)",
                details={"missing": ["AMQP_URL", "CLOUDAMQP_URL"]},
            )
        return value

    def _amqp_url_value(self) -> str | None:
        for candidate in (self.amqp_url, self.cloudamqp_url):
            if self._secret_set(candidate):
                return candidate.get_secret_value()  # type: ignore[union-attr]
        return None

    @staticmethod
    def _secret_set(secret: SecretStr | None) -> bool:
        return secret is not None and bool(secret.get_secret_value().strip())

    def safe_summary(self) -> dict[str, Any]:
        """Non-secret configuration snapshot for the startup log line."""
        return {
            "app": f"{self.app_name} {self.app_version}",
            "environment": self.environment.value,
            "debug": self.debug_enabled,
            "log_level": self.log_level,
            "database": {
                "configured": bool(self.database_url),
                "pool_size": self.db_pool_size,
                "statement_timeout_ms": self.db_statement_timeout_ms,
            },
            "redis": {
                "configured": bool(self.redis_url),
                "namespace": self.redis_namespace,
            },
            "amqp": {
                "configured": self._amqp_url_value() is not None,
                "queue": self.etl_queue_name,
                "dlq": self.etl_dlq_name,
                "depth_alert_at": self.queue_depth_alert_threshold,
            },
            "llm": {
                "gemini": self._secret_set(self.gemini_api_key),
                "groq": self._secret_set(self.groq_api_key),
                "sr_router": self._secret_set(self.sr_api_key),
                "chat_chain": [
                    self.gemini_primary_model,
                    self.gemini_fallback_model,
                    f"groq/{self.groq_model}",
                ],
                "embedding_dimensions": self.embedding_dimensions,
            },
            "storage": {
                "configured": bool(
                    self.s3_endpoint_url
                    and self.s3_bucket
                    and self._secret_set(self.s3_access_key_id)
                    and self._secret_set(self.s3_secret_access_key)
                ),
                "bucket": self.s3_bucket,
            },
            "rate_limits": {
                "messages_per_hour": self.rate_limit_messages_per_hour,
                "messages_per_day": self.rate_limit_messages_per_day,
            },
        }


@lru_cache
def get_settings() -> Settings:
    """Cached application settings (import-safe; reads env and ``.env`` once).

    Tests and niche callers construct ``Settings(_env_file=None, ...)``
    directly; FastAPI endpoints get the cached instance via
    ``Depends(get_settings)``.
    """
    return Settings()
