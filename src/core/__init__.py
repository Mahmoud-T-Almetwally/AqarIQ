"""Core cross-cutting concerns: configuration, logging, exceptions.

Import the stable names from here::

    from src.core import NotFoundError, Settings, get_logger, get_settings
"""

from src.core.config import Environment, Settings, get_settings
from src.core.exceptions import (
    REAError,
    CacheError,
    ConfigurationError,
    ConflictError,
    DatabaseError,
    ETLStageError,
    ExternalServiceError,
    LLMAuthError,
    LLMError,
    LLMRateLimitError,
    LLMResponseFormatError,
    LLMTimeoutError,
    MessageQueueError,
    NotAuthenticatedError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitExceededError,
    StorageError,
    register_exception_handlers,
)
from src.core.logging import (
    REQUEST_ID_HEADER,
    RequestContextMiddleware,
    get_logger,
    get_request_id,
    new_request_id,
    set_request_id,
    setup_logging,
)

__all__ = [
    # config
    "Environment",
    "Settings",
    "get_settings",
    # exceptions
    "REAError",
    "CacheError",
    "ConfigurationError",
    "ConflictError",
    "DatabaseError",
    "ETLStageError",
    "ExternalServiceError",
    "LLMAuthError",
    "LLMError",
    "LLMRateLimitError",
    "LLMResponseFormatError",
    "LLMTimeoutError",
    "MessageQueueError",
    "NotAuthenticatedError",
    "NotFoundError",
    "PermissionDeniedError",
    "RateLimitExceededError",
    "StorageError",
    "register_exception_handlers",
    # logging
    "REQUEST_ID_HEADER",
    "RequestContextMiddleware",
    "get_logger",
    "get_request_id",
    "new_request_id",
    "set_request_id",
    "setup_logging",
]
