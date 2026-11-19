"""Unit tests for src/core/config.py."""

from __future__ import annotations

import json

import pytest

from src.core.config import Environment, get_settings
from src.core.exceptions import ConfigurationError

PROD_ENV = {
    "ENVIRONMENT": "production",
    "DEBUG": "false",
    "DATABASE_URL": "postgresql://u:p@ep-x.neon.tech/db?sslmode=require",
    "REDIS_URL": "rediss://default:p@host.upstash.io:6379",
    "AMQP_URL": "amqps://u:p@host.cloudamqp.com/u",
    "GEMINI_API_KEY": "gemini-key",
    "GROQ_API_KEY": "groq-key",
    "SR_API_KEY": "sr-key",
    "SR_BASE_URL": "https://jev.example.com/v1",
    "S3_ENDPOINT_URL": "https://storage.neon.tech",
    "S3_BUCKET": "rea-lake",
    "S3_ACCESS_KEY_ID": "s3-key",
    "S3_SECRET_ACCESS_KEY": "s3-secret",
    "SECRET_KEY": "signing-secret",
    "INTERNAL_API_SECRET": "internal-secret",
}

# Same configuration as Python init kwargs (field names, not env names —
# pydantic-settings matches init kwargs case-sensitively and extra="ignore"
# would silently drop anything else).
PROD_KWARGS = {
    "environment": "production",
    "debug": False,
    "database_url": "postgresql://u:p@ep-x.neon.tech/db",
    "redis_url": "rediss://default:p@host.upstash.io:6379",
    "amqp_url": "amqps://u:p@host.cloudamqp.com/u",
    "gemini_api_key": "gemini-key",
    "groq_api_key": "groq-key",
    "sr_api_key": "sr-key",
    "sr_base_url": "https://jev.example.com/v1",
    "s3_endpoint_url": "https://storage.neon.tech",
    "s3_bucket": "rea-lake",
    "s3_access_key_id": "s3-key",
    "s3_secret_access_key": "s3-secret",
    "secret_key": "signing-secret",
    "internal_api_secret": "internal-secret",
}


def apply_prod(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> None:
    values = {**PROD_ENV, **overrides}
    for key, value in values.items():
        monkeypatch.setenv(key, value)


class TestDefaults:
    def test_local_defaults(self, make_settings):
        settings = make_settings()
        assert settings.environment is Environment.LOCAL
        assert settings.debug_enabled is True
        assert settings.is_production is False
        assert settings.log_level == "INFO"
        assert settings.log_json_resolved is False  # console logs locally
        assert settings.app_name == "RealEstateAnalysis"
        assert settings.cors_origins == ["http://localhost:3000"]

    def test_doc_derived_defaults(self, make_settings):
        settings = make_settings()
        assert settings.db_statement_timeout_ms == 5000
        assert settings.queue_depth_alert_threshold == 5000
        assert settings.rate_limit_messages_per_hour == 10
        assert settings.rate_limit_messages_per_day == 20
        assert settings.response_cache_ttl_seconds == 3600
        assert settings.embedding_dimensions == 768
        assert settings.gemini_primary_model == "gemini-3.5-flash-lite"
        assert settings.gemini_fallback_model == "gemini-3.1-flash-lite"
        assert settings.gemini_embedding_model == "gemini-embedding-2"
        assert settings.groq_model == "openai/gpt-oss-120b"
        assert settings.groq_prompt_guard_model == (
            "meta-llama/llama-prompt-guard-2-86m"
        )
        assert settings.sr_router_model == "vllm-sr/d3-turbo"

    def test_test_environment(self, make_settings):
        settings = make_settings(environment="test")
        assert settings.is_testing is True
        assert settings.debug_enabled is True


class TestEnvironmentParsing:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("local", Environment.LOCAL),
            ("dev", Environment.LOCAL),
            ("development", Environment.LOCAL),
            ("LOCAL", Environment.LOCAL),
            ("ci", Environment.TEST),
            ("test", Environment.TEST),
            ("prod", Environment.PRODUCTION),
            ("production", Environment.PRODUCTION),
        ],
    )
    def test_aliases(self, make_settings, raw, expected):
        kwargs = {"environment": raw}
        if expected is Environment.PRODUCTION:
            # Production settings must carry the full secret set to pass
            # the fail-fast validator.
            kwargs = {**PROD_KWARGS, "environment": raw}
        assert make_settings(**kwargs).environment is expected

    def test_invalid_environment(self, make_settings):
        with pytest.raises(ValueError, match="staging"):
            make_settings(environment="staging")


class TestEnvOverrides:
    def test_flat_env_names(self, monkeypatch, make_settings):
        monkeypatch.setenv("LOG_LEVEL", "debug")
        monkeypatch.setenv("RATE_LIMIT_MESSAGES_PER_DAY", "50")
        monkeypatch.setenv("EMBEDDING_DIMENSIONS", "1024")
        settings = make_settings()
        assert settings.log_level == "DEBUG"
        assert settings.rate_limit_messages_per_day == 50
        assert settings.embedding_dimensions == 1024

    def test_unknown_env_vars_ignored(self, monkeypatch, make_settings):
        monkeypatch.setenv("PORT", "8000")
        monkeypatch.setenv("RENDER_SERVICE_ID", "srv-1")
        make_settings()  # must not raise

    def test_invalid_log_level_rejected(self, monkeypatch, make_settings):
        monkeypatch.setenv("LOG_LEVEL", "LOUD")
        with pytest.raises(ValueError, match="Unknown log level"):
            make_settings()


class TestCorsParsing:
    def test_comma_separated(self, monkeypatch, make_settings):
        monkeypatch.setenv(
            "CORS_ORIGINS", "https://rea.vercel.app, https://other.app "
        )
        assert make_settings().cors_origins == [
            "https://rea.vercel.app",
            "https://other.app",
        ]

    def test_empty_means_none(self, monkeypatch, make_settings):
        monkeypatch.setenv("CORS_ORIGINS", "")
        assert make_settings().cors_origins == []


class TestUrls:
    def test_async_database_url_converts_postgresql(self, make_settings):
        settings = make_settings(
            database_url="postgresql://u:p@h/db?sslmode=require"
        )
        assert settings.async_database_url == (
            "postgresql+asyncpg://u:p@h/db?sslmode=require"
        )

    def test_async_database_url_converts_legacy_postgres(self, make_settings):
        settings = make_settings(database_url="postgres://u:p@h/db")
        assert settings.async_database_url == "postgresql+asyncpg://u:p@h/db"

    def test_async_database_url_passthrough(self, make_settings):
        settings = make_settings(database_url="postgresql+psycopg://u:p@h/db")
        assert settings.async_database_url == "postgresql+psycopg://u:p@h/db"

    def test_async_database_url_unset(self, make_settings):
        with pytest.raises(ConfigurationError, match="DATABASE_URL"):
            _ = make_settings().async_database_url

    def test_database_scheme_validated(self, make_settings):
        with pytest.raises(ValueError, match="database_url"):
            make_settings(database_url="mysql://u:p@h/db")

    def test_redis_scheme_validated(self, make_settings):
        with pytest.raises(ValueError, match="redis_url"):
            make_settings(redis_url="http://cache.example.com:6379")


class TestAmqpFallback:
    def test_amqp_url_preferred(self, make_settings):
        settings = make_settings(
            amqp_url="amqps://preferred/one", cloudamqp_url="amqps://injected/one"
        )
        assert settings.effective_amqp_url == "amqps://preferred/one"

    def test_cloudamqp_url_fallback(self, monkeypatch, make_settings):
        monkeypatch.setenv("CLOUDAMQP_URL", "amqps://u:p@host.cloudamqp.com/u")
        settings = make_settings()
        assert settings.effective_amqp_url == "amqps://u:p@host.cloudamqp.com/u"

    def test_amqp_missing(self, make_settings):
        with pytest.raises(ConfigurationError, match="AMQP_URL"):
            _ = make_settings().effective_amqp_url

    def test_blank_secret_treated_as_missing(self, make_settings):
        settings = make_settings(amqp_url="")
        with pytest.raises(ConfigurationError):
            _ = settings.effective_amqp_url


class TestProductionValidation:
    def test_missing_settings_all_listed(self, make_settings):
        with pytest.raises(ConfigurationError) as exc_info:
            make_settings(environment="production")
        message = str(exc_info.value)
        for name in (
            "DATABASE_URL",
            "REDIS_URL",
            "AMQP_URL",
            "GEMINI_API_KEY",
            "GROQ_API_KEY",
            "SR_API_KEY",
            "SR_BASE_URL",
            "S3_ENDPOINT_URL",
            "S3_BUCKET",
            "S3_ACCESS_KEY_ID",
            "S3_SECRET_ACCESS_KEY",
            "SECRET_KEY",
            "INTERNAL_API_SECRET",
        ):
            assert name in message

    def test_debug_true_rejected(self, make_settings):
        with pytest.raises(ConfigurationError, match="DEBUG"):
            make_settings(environment="production", debug=True)

    def test_debug_log_level_rejected(self, make_settings):
        with pytest.raises(ConfigurationError, match="LOG_LEVEL"):
            make_settings(environment="production", log_level="DEBUG")

    def test_fully_configured_passes(self, monkeypatch, make_settings):
        apply_prod(monkeypatch)
        settings = make_settings()
        assert settings.is_production is True
        assert settings.debug_enabled is False
        assert settings.log_json_resolved is True

    def test_single_missing_field_named(self, monkeypatch, make_settings):
        apply_prod(monkeypatch, GEMINI_API_KEY="")
        with pytest.raises(ConfigurationError) as exc_info:
            make_settings()
        assert "GEMINI_API_KEY" in str(exc_info.value)
        assert "GROQ_API_KEY" not in str(exc_info.value)


class TestSafeSummary:
    def test_masks_all_secrets(self, monkeypatch, make_settings):
        apply_prod(monkeypatch)
        summary = make_settings().safe_summary()
        dumped = json.dumps(summary)
        for secret in (
            "gemini-key",
            "groq-key",
            "sr-key",
            "s3-secret",
            "signing-secret",
            "internal-secret",
            "u:p@host.cloudamqp.com",
        ):
            assert secret not in dumped

    def test_reports_configuration_state(self, monkeypatch, make_settings):
        apply_prod(monkeypatch)
        summary = make_settings().safe_summary()
        assert summary["database"]["configured"] is True
        assert summary["redis"]["configured"] is True
        assert summary["amqp"]["configured"] is True
        assert summary["llm"]["gemini"] is True
        assert summary["storage"]["configured"] is True
        assert summary["environment"] == "production"

    def test_unconfigured_local_summary(self, make_settings):
        summary = make_settings().safe_summary()
        assert summary["database"]["configured"] is False
        assert summary["llm"]["gemini"] is False


class TestRedisNamespace:
    def test_namespace_includes_environment(self, make_settings):
        assert make_settings().redis_namespace == "rea:local"
        assert make_settings(**PROD_KWARGS).redis_namespace == "rea:production"


class TestSettingsCache:
    # A real env var beats whatever is in the developer's .env file.
    @pytest.fixture(autouse=True)
    def _valid_database_url(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@h/db")

    def test_get_settings_is_cached(self):
        assert get_settings() is get_settings()

    def test_cache_clear_yields_new_instance(self):
        first = get_settings()
        get_settings.cache_clear()
        assert get_settings() is not first
