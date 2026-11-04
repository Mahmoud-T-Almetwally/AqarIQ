"""Structured logging and request correlation.

One log format for the whole service: application code logs through
`structlog <https://www.structlog.org>`_, while third-party loggers
(uvicorn, SQLAlchemy, aio-pika, apscheduler…) are bridged through the
stdlib :mod:`logging` root handler so every line shares the same
processors. Output is JSON in production (Render log drains, one object per
line) and a pretty colored console format in local development.

Request correlation
-------------------
:class:`RequestContextMiddleware` is a pure-ASGI middleware (streaming/SSE
safe, unlike ``BaseHTTPMiddleware`` which has a history of buffering
streamed responses). For every request it:

1. resolves a request id — an incoming ``X-Request-ID`` (traced through
   from the frontend, capped to 64 printable chars so a hostile header
   cannot bloat logs) or a fresh UUID4 hex;
2. stores it in a module-level :class:`~contextvars.ContextVar` so every
   log line emitted anywhere during the request picks it up via the
   ``_add_request_id`` processor, and in the ASGI scope (``request.state``)
   so exception handlers can still read it after the contextvar is reset
   during exception unwinding (see ``exceptions.py``);
3. echoes it back as an ``X-Request-ID`` response header;
4. emits exactly one structured ``request.access`` line per request with
   method, path, status and duration in milliseconds.

Usage::

    from src.core.config import get_settings
    from src.core.logging import setup_logging

    setup_logging(level=settings.log_level, json_logs=settings.log_json_resolved)

When uvicorn is started programmatically, pass ``log_config=None`` so it
does not re-install its own handlers on top of this configuration.
"""

from __future__ import annotations

import logging
import sys
import time
import uuid
from contextvars import ContextVar
from typing import Any

import structlog
from starlette.datastructures import Headers
from starlette.types import ASGIApp, Message, Receive, Scope, Send

REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_MAX_LENGTH = 64

_request_id_var: ContextVar[str | None] = ContextVar("request_id", default=None)

# Handlers installed by setup_logging, so reconfiguration can replace only
# its own handlers and leave foreign ones (pytest capture, supervisors) alone.
_installed_handlers: list[logging.Handler] = []

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Request-id context
# ---------------------------------------------------------------------------


def new_request_id() -> str:
    return uuid.uuid4().hex


def get_request_id() -> str | None:
    """The request id for the current task/context, if one is bound."""
    return _request_id_var.get()


def set_request_id(request_id: str) -> None:
    """Bind a request id outside the middleware (tests; correlating
    non-HTTP work such as the ETL consumer under the same field).

    The middleware itself uses the token-based set/reset on the underlying
    contextvar so the value is restored exactly when a request ends.
    """
    _request_id_var.set(request_id)


def clear_request_id() -> None:
    """Unbind the request id (test isolation; requests overwrite it anyway)."""
    _request_id_var.set(None)


def _add_request_id(
    logger: Any, method_name: str, event_dict: structlog.typing.EventDict
) -> structlog.typing.EventDict:
    """structlog processor: stamp the current request id onto every event."""
    request_id = _request_id_var.get()
    if request_id is not None:
        event_dict.setdefault("request_id", request_id)
    return event_dict


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def make_processor_formatter(*, json_logs: bool) -> logging.Formatter:
    """Build the stdlib formatter shared by structlog and foreign records."""
    shared_processors: list[structlog.typing.Processor] = [
        _add_request_id,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]
    # ConsoleRenderer pretty-prints exception info itself; JSONRenderer needs
    # it pre-formatted into the event dict.
    if json_logs:
        shared_processors.append(structlog.processors.format_exc_info)
        renderer: structlog.typing.Processor = structlog.processors.JSONRenderer()
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())

    return structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared_processors,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            renderer,
        ],
    )


def setup_logging(*, level: str | int = "INFO", json_logs: bool = False) -> None:
    """Configure structlog and the stdlib root logger. Idempotent.

    Only handlers previously installed by this function are replaced, so
    calling it never destroys handlers owned by the host process (pytest's
    log capture, a supervisor's handlers, …).

    Args:
        level: Root level ("DEBUG"…"CRITICAL", case-insensitive) or a
            stdlib level number.
        json_logs: ``True`` renders one JSON object per line (production);
            ``False`` renders a human-friendly console format.
    """
    resolved = _resolve_level(level)

    shared_processors: list[structlog.typing.Processor] = [
        _add_request_id,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
    ]
    if json_logs:
        shared_processors.append(structlog.processors.format_exc_info)

    structlog.configure(
        processors=[
            *shared_processors,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )

    root = logging.getLogger()
    for stale in _installed_handlers:
        root.removeHandler(stale)
        stale.close()
    _installed_handlers.clear()

    handler = logging.StreamHandler(stream=sys.stdout)
    handler.setFormatter(make_processor_formatter(json_logs=json_logs))
    root.addHandler(handler)
    _installed_handlers.append(handler)
    root.setLevel(resolved)

    # Third-party loggers flow through the root handler instead of their own.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        third_party = logging.getLogger(name)
        third_party.handlers.clear()
        third_party.propagate = True
    # We log our own access line; keep uvicorn's from double-reporting.
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("aio_pika").setLevel(logging.WARNING)
    logging.captureWarnings(True)


def _resolve_level(level: str | int) -> int:
    if isinstance(level, int):
        return level
    normalized = level.strip().upper()
    numeric = getattr(logging, normalized, None)
    if not isinstance(numeric, int):
        raise ValueError(f"Unknown log level: {level!r}")
    return numeric


def get_logger(name: str | None = None) -> Any:
    """Return a structlog logger bound to the stdlib logging hierarchy."""
    return structlog.get_logger(name)


# ---------------------------------------------------------------------------
# Request correlation middleware
# ---------------------------------------------------------------------------


class RequestContextMiddleware:
    """Pure-ASGI middleware binding a request id and logging access lines.

    Add with ``app.add_middleware(RequestContextMiddleware)`` — it must sit
    inside Starlette's outermost error middleware, which is where
    ``add_middleware`` puts user middleware. Because it touches the raw
    ``send`` channel it never buffers streamed (SSE) responses.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request_id = self._resolve_request_id(scope)
        # The scope dict is shared with every layer of the stack, including
        # the outermost error middleware — the contextvar alone would be
        # reset during exception unwinding before its handler runs.
        scope.setdefault("state", {})["request_id"] = request_id
        token = _request_id_var.set(request_id)

        response_status: list[int | None] = [None]

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                response_status[0] = message["status"]
                headers = Headers(raw=message.get("headers", []))
                if REQUEST_ID_HEADER not in headers:
                    message = {
                        **message,
                        "headers": [
                            *message.get("headers", []),
                            (REQUEST_ID_HEADER.lower().encode(), request_id.encode()),
                        ],
                    }
            await send(message)

        start = time.perf_counter()
        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 2)
            # On an unhandled exception the outer error middleware builds the
            # response only after this frame unwinds — assume 500 there.
            logger.info(
                "request.access",
                method=scope.get("method"),
                path=scope.get("path"),
                status=response_status[0] or 500,
                duration_ms=duration_ms,
            )
            _request_id_var.reset(token)

    @staticmethod
    def _resolve_request_id(scope: Scope) -> str:
        incoming = Headers(scope=scope).get(REQUEST_ID_HEADER)
        if incoming:
            incoming = incoming.strip()
            if 0 < len(incoming) <= _REQUEST_ID_MAX_LENGTH and incoming.isprintable():
                return incoming
        return new_request_id()
