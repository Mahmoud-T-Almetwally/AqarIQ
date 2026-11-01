"""Unit tests for src/core/logging.py."""

from __future__ import annotations

import json
import logging
import string

import pytest
import structlog
from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.core.logging import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    clear_request_id,
    get_request_id,
    make_processor_formatter,
    set_request_id,
    setup_logging,
)


def _our_handlers() -> list[logging.Handler]:
    """Only the handlers setup_logging installed (foreign ones excluded)."""
    return [
        handler
        for handler in logging.getLogger().handlers
        if isinstance(handler.formatter, structlog.stdlib.ProcessorFormatter)
    ]


def _foreign_record(
    msg: str = "hello structured", level: int = logging.INFO
) -> logging.LogRecord:
    """A record from a plain stdlib logger (uvicorn/SQLAlchemy style)."""
    return logging.LogRecord(
        name="tests.foreign",
        level=level,
        pathname=__file__,
        lineno=1,
        msg=msg,
        args=(),
        exc_info=None,
    )


class TestFormatters:
    def test_json_formatter_foreign_record(self):
        formatter = make_processor_formatter(json_logs=True)
        set_request_id("req-abc-123")
        try:
            payload = json.loads(formatter.format(_foreign_record()))
        finally:
            clear_request_id()
        assert payload["event"] == "hello structured"
        assert payload["request_id"] == "req-abc-123"
        assert payload["level"] == "info"
        assert payload["logger"] == "tests.foreign"
        assert "timestamp" in payload

    def test_json_formatter_without_request_id(self):
        formatter = make_processor_formatter(json_logs=True)
        payload = json.loads(formatter.format(_foreign_record()))
        assert "request_id" not in payload

    def test_console_formatter_is_not_json(self):
        formatter = make_processor_formatter(json_logs=False)
        output = formatter.format(_foreign_record())
        assert not output.lstrip().startswith("{")
        assert "hello structured" in output


class TestSetupLogging:
    def test_configures_root_level_and_own_handler(self):
        setup_logging(level="WARNING", json_logs=False)
        assert logging.getLogger().level == logging.WARNING
        assert len(_our_handlers()) == 1

    def test_is_idempotent(self):
        setup_logging(level="WARNING", json_logs=False)
        setup_logging(level="WARNING", json_logs=False)
        assert len(_our_handlers()) == 1

    def test_preserves_foreign_handlers(self):
        setup_logging(level="INFO", json_logs=True)
        foreign = logging.Handler()
        logging.getLogger().addHandler(foreign)
        try:
            setup_logging(level="INFO", json_logs=True)
            assert foreign in logging.getLogger().handlers
        finally:
            logging.getLogger().removeHandler(foreign)

    def test_routes_uvicorn_loggers_through_root(self):
        setup_logging(level="INFO", json_logs=True)
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            uvicorn_logger = logging.getLogger(name)
            assert uvicorn_logger.handlers == []
            assert uvicorn_logger.propagate is True
        # Our own access line replaces uvicorn's; no double reporting.
        assert logging.getLogger("uvicorn.access").level == logging.WARNING

    def test_accepts_numeric_level(self):
        setup_logging(level=logging.ERROR, json_logs=True)
        assert logging.getLogger().level == logging.ERROR

    def test_rejects_unknown_level(self):
        with pytest.raises(ValueError, match="Unknown log level"):
            setup_logging(level="NOISY", json_logs=True)


class TestResolveRequestId:
    @staticmethod
    def _scope(header_value: str | None) -> dict:
        headers = [] if header_value is None else [(b"x-request-id", header_value.encode())]
        return {"type": "http", "headers": headers}

    def test_accepts_and_strips_incoming_id(self):
        assert (
            RequestContextMiddleware._resolve_request_id(self._scope(" trace-42 "))
            == "trace-42"
        )

    def test_oversized_id_replaced(self):
        resolved = RequestContextMiddleware._resolve_request_id(self._scope("x" * 200))
        assert len(resolved) == 32

    def test_whitespace_only_id_replaced(self):
        resolved = RequestContextMiddleware._resolve_request_id(self._scope("   "))
        assert len(resolved) == 32

    def test_missing_header_generates_hex_id(self):
        resolved = RequestContextMiddleware._resolve_request_id(self._scope(None))
        assert len(resolved) == 32
        assert all(c in string.hexdigits for c in resolved)


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)

    @app.get("/ok")
    async def ok():
        return {"request_id": get_request_id()}

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    return app


class TestRequestContextMiddleware:
    def test_generates_and_echoes_request_id(self, app):
        with TestClient(app) as client:
            response = client.get("/ok")
        request_id = response.headers[REQUEST_ID_HEADER]
        assert len(request_id) == 32
        # The contextvar is visible inside request handlers.
        assert response.json()["request_id"] == request_id

    def test_incoming_request_id_is_reused(self, app):
        with TestClient(app) as client:
            response = client.get(
                "/ok", headers={REQUEST_ID_HEADER: "trace-from-frontend"}
            )
        assert response.headers[REQUEST_ID_HEADER] == "trace-from-frontend"
        assert response.json()["request_id"] == "trace-from-frontend"

    def test_hostile_header_value_is_replaced(self, app):
        with TestClient(app) as client:
            response = client.get("/ok", headers={REQUEST_ID_HEADER: "x" * 300})
        assert response.headers[REQUEST_ID_HEADER] != "x" * 300
        assert len(response.headers[REQUEST_ID_HEADER]) == 32

    def test_access_line_logged_per_request(self, app, caplog, log_events):
        with TestClient(app) as client:
            client.get("/ok")
        access = [
            event
            for event in log_events(caplog.records)
            if event.get("event") == "request.access"
        ]
        assert len(access) == 1
        assert access[0]["method"] == "GET"
        assert access[0]["path"] == "/ok"
        assert access[0]["status"] == 200
        assert "duration_ms" in access[0]
        assert "request_id" in access[0]

    def test_access_line_on_unhandled_exception_assumes_500(
        self, app, caplog, log_events
    ):
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/boom")
        assert response.status_code == 500
        access = [
            event
            for event in log_events(caplog.records)
            if event.get("event") == "request.access"
        ]
        assert access[0]["status"] == 500
