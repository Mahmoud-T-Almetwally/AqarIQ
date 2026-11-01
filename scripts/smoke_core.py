"""Phase-0 smoke check: config → logging → exceptions wired together.

Simulates what the future app factory (src/main.py) will do, outside of
pytest: load settings, configure logging, and log one structured line per
environment mode. Run from the repo root: python scripts/smoke_core.py
"""

from __future__ import annotations

import logging

from src.core import (
    ConfigurationError,
    Settings,
    get_settings,
    get_logger,
    setup_logging,
)


def main() -> None:
    settings = get_settings()
    setup_logging(level=settings.log_level, json_logs=settings.log_json_resolved)
    logger = get_logger("smoke")

    logger.info(
        "smoke.startup",
        app=settings.app_name,
        version=settings.app_version,
        environment=settings.environment.value,
        debug=settings.debug_enabled,
    )
    print("safe_summary:", settings.safe_summary())

    # Production fail-fast: a production environment without secrets must
    # refuse to construct, listing everything that is missing.
    try:
        Settings(environment="production")
    except ConfigurationError as error:
        print("production validation OK ->", error.message[:120], "…")

    # Environment-driven formatting: production renders JSON lines.
    prod_like = Settings(environment="local", log_json=True)
    setup_logging(level=prod_like.log_level, json_logs=True)
    logging.getLogger("smoke.foreign").info(
        "foreign logger line goes through the same processors"
    )
    logger.info("smoke.json_mode")


if __name__ == "__main__":
    main()
