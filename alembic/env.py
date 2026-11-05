"""Alembic environment — async, settings-aware, CI-friendly.

URL resolution order (nothing secret is ever committed):

1. ``DATABASE_URL`` environment variable (Render, GitHub Actions, local
   shell) — accepts ``postgres://``, ``postgresql://`` and
   ``postgresql+asyncpg://``;
2. ``src.core.config.Settings`` — the same configuration the app uses
   (``.env`` in local development), so a single source of truth holds.

Offline mode (``alembic upgrade head --sql``) renders the full chain as
SQL without a database — used by unit tests to keep ``src/db/models.py``
and the migration files honest with each other.

Version-table name and transaction handling follow Alembic's async
recipe; ``compare_type`` and the shared naming convention keep future
autogenerate diffs minimal.
"""

from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from src.core.config import Settings
from src.db.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata

_ASYNCPG_SCHEME = "postgresql+asyncpg://"


def resolve_database_url() -> URL:
    """DATABASE_URL env var first, then Settings — as a SQLAlchemy URL."""
    raw = os.environ.get("DATABASE_URL") or config.get_main_option(
        "sqlalchemy.url"
    )
    if not raw:
        raw = Settings().async_database_url  # raises if unconfigured
    if raw.startswith("postgresql+"):
        pass  # already driver-qualified (e.g. postgresql+asyncpg://)
    elif raw.startswith("postgresql://"):
        raw = _ASYNCPG_SCHEME + raw[len("postgresql://") :]
    elif raw.startswith("postgres://"):
        raw = _ASYNCPG_SCHEME + raw[len("postgres://") :]
    return _url_from_string(raw)


def _url_from_string(raw: str) -> URL:
    return make_url(raw)


def run_migrations_offline() -> None:
    """Render migrations to SQL with no database connection.

    Uses the asyncpg dialect's DDL compiler (no driver connection is
    needed for offline rendering).
    """
    context.configure(
        url=resolve_database_url(),
        version_table="alembic_version",
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        target_metadata=target_metadata,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    context.configure(
        connection=connection,
        version_table="alembic_version",
        compare_type=True,
        target_metadata=target_metadata,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """The async recipe: connect with the app's asyncpg engine and run
    migrations inside one transaction per migration file."""
    configuration = config.get_section(config.config_ini_section, {})
    configuration["sqlalchemy.url"] = resolve_database_url().render_as_string(
        hide_password=False
    )
    connectable = async_engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        connect_args={"ssl": "require"}
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
