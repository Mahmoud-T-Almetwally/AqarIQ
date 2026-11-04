"""Shared test fixtures.

Every test gets a deterministic environment: all settings-related env vars
are deleted (monkeypatch restores them afterwards), logging is configured
once per test so structlog loggers resolve against a known configuration,
and the cached ``get_settings`` instance is cleared.
"""

from __future__ import annotations

import logging

import pytest

from src.core.config import Settings, get_settings
from src.core.logging import clear_request_id, setup_logging


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Remove settings-bearing env vars so defaults are deterministic."""
    for field_name in Settings.model_fields:
        monkeypatch.delenv(field_name.upper(), raising=False)
    monkeypatch.delenv("CLOUDAMQP_URL", raising=False)


@pytest.fixture(autouse=True)
def configured_logging() -> None:
    setup_logging(level="INFO", json_logs=True)


@pytest.fixture(autouse=True)
def isolated_request_id() -> None:
    clear_request_id()


@pytest.fixture(autouse=True)
def clear_settings_cache() -> None:
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def make_settings():
    """Factory building Settings without touching the developer's .env."""

    def _factory(**overrides) -> Settings:
        return Settings(_env_file=None, **overrides)

    return _factory


@pytest.fixture
def event_records():
    """Find captured records whose structlog event name matches.

    structlog (>=25) stores the event dict directly on ``record.msg``;
    pass ``caplog.records`` *after* the action under test — the property
    returns a fresh list each access.
    """

    def _find(
        records: list[logging.LogRecord], event: str
    ) -> list[logging.LogRecord]:
        return [
            record
            for record in records
            if isinstance(record.msg, dict) and record.msg.get("event") == event
        ]

    return _find


@pytest.fixture
def log_events():
    """Extract event dicts from records captured by pytest's ``caplog``.

    structlog-native records carry their event dict in ``record.msg``;
    plain stdlib records are reduced to ``{"event": <message>}`` (exactly
    what the formatter does for foreign records).
    """

    def _extract(records: list[logging.LogRecord]) -> list[dict]:
        events = []
        for record in records:
            if isinstance(record.msg, dict):
                events.append(dict(record.msg))
            else:
                events.append({"event": record.getMessage()})
        return events

    return _extract
