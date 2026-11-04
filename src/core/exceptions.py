"""Application exception hierarchy and uniform API error responses.

Every error the application raises on purpose derives from :class:`REAError`.
The class attributes give callers — the HTTP layer, the LLM failover pool,
the ETL pipeline — a stable machine code, an HTTP status, and a
``retryable`` flag to react to without parsing human messages.

HTTP contract
-------------
Errors are returned as a uniform envelope (distinct from the chat response
envelope owned by ``api/schemas.py``)::

    {
      "error": {
        "code": "not_found",
        "message": "Property 42 not found.",
        "details": {"property_id": 42},
        "request_id": "6f9c…"
      }
    }

Every error response carries an ``X-Request-ID`` header so a user report can
be correlated with the structured logs. In production, unexpected exceptions
never leak internals: the generic handler logs the traceback and returns
``internal_error``; ``debug=True`` adds the traceback to the body for local
development only.

The request id is read from the logging contextvar when still in scope and
falls back to ``request.state.request_id`` (written onto the ASGI scope by
``RequestContextMiddleware``), which survives stack unwinding — so even a
500 built by Starlette's outermost error middleware stays correlated.
"""

from __future__ import annotations

import logging
from typing import Any, Mapping

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from src.core.logging import REQUEST_ID_HEADER, get_logger, get_request_id

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Base class
# ---------------------------------------------------------------------------


class REAError(Exception):
    """Base class for all deliberate application errors.

    Attributes:
        code: Stable machine-readable identifier (snake_case) used in API
            error envelopes, logs, and metrics. Never rename a deployed code.
        http_status: Status returned when the error escapes to the API layer.
        retryable: Whether an immediate retry of the *same* operation may
            succeed (used by the LLM failover pool and the ETL consumer).
    """

    code: str = "rea_error"
    http_status: int = 500
    retryable: bool = False

    def __init__(
        self,
        message: str | None = None,
        *,
        details: Mapping[str, Any] | None = None,
        code: str | None = None,
        http_status: int | None = None,
        retryable: bool | None = None,
    ) -> None:
        self.message = message if message is not None else self.code
        self.details: dict[str, Any] = dict(details or {})
        if code is not None:
            self.code = code
        if http_status is not None:
            self.http_status = http_status
        if retryable is not None:
            self.retryable = retryable
        super().__init__(self.message)

    def __str__(self) -> str:
        return self.message


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


class ConfigurationError(REAError):
    """A required setting is missing or invalid.

    Raised at startup (fail fast) rather than at first use, so a bad deploy
    is visible immediately in the logs.
    """

    code = "configuration_error"
    http_status = 500


# ---------------------------------------------------------------------------
# Upstream / infrastructure services
# ---------------------------------------------------------------------------


class ExternalServiceError(REAError):
    """An external dependency (Neon, Upstash, CloudAMQP, an LLM provider) failed.

    Defaults assume transient infrastructure trouble — the free-tier world
    this project lives in really does have cold starts and quota blips — so
    callers may retry. Raise with explicit ``retryable=False`` for bugs.
    """

    code = "external_service_error"
    http_status = 503
    retryable = True


class DatabaseError(ExternalServiceError):
    """PostgreSQL/Neon is unreachable, suspended, or a query failed."""

    code = "database_error"
    http_status = 503


class CacheError(ExternalServiceError):
    """Redis (Upstash) is unreachable.

    The service layer should degrade (serve without cache) rather than fail
    the request; this error escaping means degradation was impossible.
    """

    code = "cache_error"
    http_status = 503


class MessageQueueError(ExternalServiceError):
    """RabbitMQ (CloudAMQP) publish/consume failed."""

    code = "message_queue_error"
    http_status = 503


class StorageError(ExternalServiceError):
    """The S3-compatible object lake (Neon Object Storage) failed."""

    code = "storage_error"
    http_status = 502


class LLMError(ExternalServiceError):
    """A chat-synthesis / extraction model call failed on all retries."""

    code = "llm_error"
    http_status = 502


class LLMRateLimitError(LLMError):
    """The provider quota (RPM/RPD/TPM) is exhausted.

    Distinguish from :class:`RateLimitExceededError`: that one is *our* abuse
    protection rejecting a client (429); this one is an upstream budget being
    drained (503 — retrying later may help, hammering now will not).

    Carries ``retry_after`` (seconds) parsed from the provider response or
    the tracked quota reset, surfaced as a ``Retry-After`` header by the
    API error envelope so well-behaved clients back off.
    """

    code = "llm_rate_limited"
    http_status = 503

    def __init__(
        self,
        message: str | None = None,
        *,
        retry_after: int | None = None,
        **kwargs: Any,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(message, **kwargs)


class LLMTimeoutError(LLMError):
    """The provider did not answer within the configured timeout."""

    code = "llm_timeout"
    http_status = 504


class LLMAuthError(LLMError):
    """The provider rejected our credentials. Never retry automatically."""

    code = "llm_auth_error"
    http_status = 502
    retryable = False


class LLMResponseFormatError(LLMError):
    """The model output failed JSON parsing/repair against its schema."""

    code = "llm_bad_response"
    http_status = 502


# ---------------------------------------------------------------------------
# ETL pipeline
# ---------------------------------------------------------------------------


class ETLStageError(REAError):
    """A pipeline stage (normalize / extract / dedup / embed) failed.

    The consumer treats retryable stage errors as poison-message candidates:
    after the retry budget is spent the message goes to the DLQ instead of
    blocking the queue.
    """

    code = "etl_stage_failed"
    http_status = 500
    retryable = True

    def __init__(
        self,
        message: str | None = None,
        *,
        stage: str = "",
        details: Mapping[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        merged: dict[str, Any] = {"stage": stage} if stage else {}
        merged.update(details or {})
        super().__init__(message, details=merged, **kwargs)


# ---------------------------------------------------------------------------
# HTTP-facing errors
# ---------------------------------------------------------------------------


class NotFoundError(REAError):
    code = "not_found"
    http_status = 404


class NotAuthenticatedError(REAError):
    code = "not_authenticated"
    http_status = 401


class PermissionDeniedError(REAError):
    code = "permission_denied"
    http_status = 403


class ConflictError(REAError):
    code = "conflict"
    http_status = 409


class RateLimitExceededError(REAError):
    """Our own per-IP token bucket rejected the client."""

    code = "rate_limited"
    http_status = 429
    retryable = True

    def __init__(
        self,
        message: str | None = None,
        *,
        retry_after: int | None = None,
        **kwargs: Any,
    ) -> None:
        self.retry_after = retry_after
        super().__init__(message, **kwargs)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


def _request_id_of(request: Request) -> str | None:
    """Current request id: contextvar first, ASGI scope as fallback.

    The scope dict is shared by the whole middleware stack, so it still
    carries the id after the contextvar has been reset while an exception
    unwound through ``RequestContextMiddleware``.
    """
    return get_request_id() or getattr(request.state, "request_id", None)


def _error_response(
    request: Request,
    *,
    status: int,
    code: str,
    message: str,
    details: Mapping[str, Any] | None = None,
    retry_after: int | None = None,
    traceback_text: str | None = None,
) -> JSONResponse:
    payload: dict[str, Any] = {
        "error": {
            "code": code,
            "message": message,
            "request_id": _request_id_of(request),
        }
    }
    if details:
        payload["error"]["details"] = dict(details)
    if traceback_text is not None:
        payload["error"]["traceback"] = traceback_text

    headers: dict[str, str] = {}
    request_id = payload["error"]["request_id"]
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id
    if retry_after is not None:
        headers["Retry-After"] = str(retry_after)
    return JSONResponse(status_code=status, content=payload, headers=headers or None)


def register_exception_handlers(app: FastAPI, *, debug: bool = False) -> None:
    """Attach the uniform error envelope to a FastAPI application.

    Call once from the app factory (``main.py``) after middleware is added.
    ``debug`` only controls payload verbosity (adds tracebacks to bodies);
    it must be False anywhere outside local development.
    """

    @app.exception_handler(REAError)
    async def _rea_error_handler(request: Request, exc: REAError) -> JSONResponse:
        log = logger.error if exc.http_status >= 500 else logger.warning
        log(
            "error.handled",
            error_code=exc.code,
            error_class=type(exc).__name__,
            status=exc.http_status,
            details=exc.details,
        )
        retry_after = getattr(exc, "retry_after", None)
        return _error_response(
            request,
            status=exc.http_status,
            code=exc.code,
            message=exc.message,
            details=exc.details or None,
            retry_after=retry_after,
            traceback_text=_format_traceback(exc) if debug else None,
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        # Keep loc/type/msg only — exc.errors() context values can echo
        # request payloads (and PII) back into logs.
        errors = [
            {
                "field": ".".join(str(part) for part in error["loc"]),
                "type": error["type"],
                "message": error["msg"],
            }
            for error in exc.errors()
        ]
        logger.warning(
            "error.validation_failed",
            status=422,
            errors=errors,
        )
        return _error_response(
            request,
            status=422,
            code="validation_error",
            message="Request validation failed.",
            details={"errors": errors},
        )

    @app.exception_handler(HTTPException)
    async def _http_exception_handler(
        request: Request, exc: HTTPException
    ) -> JSONResponse:
        code = _CODE_BY_STATUS.get(exc.status_code, "http_error")
        return _error_response(
            request,
            status=exc.status_code,
            code=code,
            message=str(exc.detail) if exc.detail is not None else code,
            retry_after=exc.headers.get("Retry-After") if exc.headers else None,
        )

    @app.exception_handler(Exception)
    async def _unhandled_handler(request: Request, exc: Exception) -> JSONResponse:
        # This handler runs in Starlette's outermost error middleware; the
        # request-id contextvar may already have been reset during unwinding,
        # hence the request.state fallback inside _error_response.
        logger.exception(
            "error.unhandled",
            error_class=type(exc).__name__,
            path=request.url.path,
        )
        return _error_response(
            request,
            status=500,
            code="internal_error",
            message=str(exc) if debug else "An unexpected error occurred.",
            traceback_text=_format_traceback(exc) if debug else None,
        )


_CODE_BY_STATUS = {
    400: "bad_request",
    401: "not_authenticated",
    403: "permission_denied",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    429: "rate_limited",
}


def _format_traceback(exc: BaseException) -> str:
    import traceback

    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
