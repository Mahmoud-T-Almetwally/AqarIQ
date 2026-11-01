"""Unit tests for src/core/exceptions.py (hierarchy + API handlers)."""

from __future__ import annotations

import logging

import pytest
from fastapi import FastAPI, HTTPException, Query
from fastapi.testclient import TestClient

from src.core.exceptions import (
    ETLStageError,
    LLMRateLimitError,
    NotFoundError,
    RateLimitExceededError,
    REAError,
    register_exception_handlers,
)
from src.core.logging import REQUEST_ID_HEADER, RequestContextMiddleware


@pytest.fixture
def app() -> FastAPI:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)

    @app.get("/raise-notfound")
    async def raise_notfound():
        raise NotFoundError("Property 42 not found.", details={"property_id": 42})

    @app.get("/raise-rate-limit")
    async def raise_rate_limit():
        raise RateLimitExceededError("Too many messages.", retry_after=60)

    @app.get("/raise-llm")
    async def raise_llm():
        raise LLMRateLimitError("All chat providers are out of quota.")

    @app.get("/raise-etl")
    async def raise_etl():
        raise ETLStageError("Listing rejected by contract.", stage="normalizer")

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    @app.get("/validation")
    async def validation(q: int = Query()):
        return {"q": q}

    @app.get("/http-exc")
    async def http_exc():
        raise HTTPException(status_code=404, detail="nope")

    return app


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


@pytest.fixture
def debug_client() -> TestClient:
    app = FastAPI()
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app, debug=True)

    @app.get("/boom")
    async def boom():
        raise RuntimeError("kaboom")

    return TestClient(app, raise_server_exceptions=False)


class TestHierarchy:
    def test_message_and_str(self):
        error = NotFoundError("Property 42 not found.")
        assert error.message == "Property 42 not found."
        assert str(error) == "Property 42 not found."

    def test_message_defaults_to_code(self):
        assert RateLimitExceededError().message == "rate_limited"

    def test_class_defaults(self):
        error = LLMRateLimitError()
        assert error.code == "llm_rate_limited"
        assert error.http_status == 503
        assert error.retryable is True

    def test_instance_overrides(self):
        error = REAError(
            "custom", code="custom_code", http_status=418, retryable=False
        )
        assert error.code == "custom_code"
        assert error.http_status == 418
        assert error.retryable is False

    def test_etl_stage_error_merges_stage_into_details(self):
        error = ETLStageError("bad listing", stage="normalizer", details={"k": "v"})
        assert error.details == {"stage": "normalizer", "k": "v"}

    def test_llm_hierarchy_is_retryable_but_auth_is_not(self):
        from src.core.exceptions import LLMAuthError, LLMError

        assert LLMError().retryable is True
        assert LLMAuthError().retryable is False


class TestErrorEnvelope:
    def test_not_found(self, client):
        response = client.get("/raise-notfound")
        assert response.status_code == 404
        body = response.json()["error"]
        assert body["code"] == "not_found"
        assert body["message"] == "Property 42 not found."
        assert body["details"] == {"property_id": 42}
        assert body["request_id"] == response.headers[REQUEST_ID_HEADER]

    def test_rate_limit_includes_retry_after(self, client):
        response = client.get("/raise-rate-limit")
        assert response.status_code == 429
        assert response.headers["Retry-After"] == "60"
        assert response.json()["error"]["code"] == "rate_limited"

    def test_llm_rate_limit_is_503_not_429(self, client):
        """Upstream budget exhaustion must not look like client abuse."""
        response = client.get("/raise-llm")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "llm_rate_limited"

    def test_etl_error_internal_status(self, client):
        response = client.get("/raise-etl")
        assert response.status_code == 500
        body = response.json()["error"]
        assert body["code"] == "etl_stage_failed"
        assert body["details"]["stage"] == "normalizer"


class TestUnhandledExceptions:
    def test_production_hides_internals_but_keeps_request_id(self, app):
        client = TestClient(app, raise_server_exceptions=False)
        response = client.get("/boom")
        assert response.status_code == 500
        body = response.json()["error"]
        assert body["code"] == "internal_error"
        assert body["message"] == "An unexpected error occurred."
        assert "kaboom" not in response.text
        assert "RuntimeError" not in response.text
        # The request.state fallback keeps the outermost 500 correlated.
        assert body["request_id"] == response.headers[REQUEST_ID_HEADER]
        assert len(body["request_id"]) == 32

    def test_debug_exposes_details(self, debug_client):
        response = debug_client.get("/boom")
        assert response.status_code == 500
        body = response.json()["error"]
        assert "kaboom" in body["message"]
        assert "RuntimeError" in body["traceback"]

    def test_unhandled_exception_logged_with_traceback(self, app, caplog, event_records):
        client = TestClient(app, raise_server_exceptions=False)
        client.get("/boom")
        unhandled = event_records(caplog.records, "error.unhandled")
        assert unhandled
        assert unhandled[0].levelno == logging.ERROR
        assert unhandled[0].exc_info is not None


class TestFrameworkErrors:
    def test_request_validation_uses_envelope(self, client):
        response = client.get("/validation", params={"q": "not-a-number"})
        assert response.status_code == 422
        body = response.json()["error"]
        assert body["code"] == "validation_error"
        error = body["details"]["errors"][0]
        assert error["field"] == "query.q"
        assert error["type"] == "int_parsing"

    def test_http_exception_uses_envelope(self, client):
        response = client.get("/http-exc")
        assert response.status_code == 404
        body = response.json()["error"]
        assert body["code"] == "not_found"
        assert body["message"] == "nope"

    def test_unknown_route_uses_envelope(self, client):
        response = client.get("/does-not-exist")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "not_found"


class TestHandlerLogging:
    def test_client_errors_logged_as_warnings(self, client, caplog, event_records):
        client.get("/raise-notfound")
        handled = event_records(caplog.records, "error.handled")
        assert handled
        assert handled[0].levelno == logging.WARNING
        assert handled[0].msg["error_code"] == "not_found"

    def test_server_errors_logged_as_errors(self, client, caplog, event_records):
        client.get("/raise-etl")
        handled = event_records(caplog.records, "error.handled")
        assert handled
        assert handled[0].levelno == logging.ERROR
