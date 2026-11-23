"""Alembic environment.

Two entry points share this file:

  * The app. `init_db()` opens a connection and hands it over through
    `config.attributes["connection"]`, so migrations run on the same engine, inside the
    same event loop, with no second connection string to keep in sync.
  * The CLI (`uv run alembic upgrade head`, `alembic revision --autogenerate`). With no
    connection supplied, an async engine is built from DATABASE_URL.

`render_as_batch=True` is required for SQLite, which cannot ALTER a table to add a
constraint; Alembic rebuilds the table instead. It is a no-op on Postgres.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy.ext.asyncio import create_async_engine

from ab_dashboard.db import Base, normalise_database_url
from shared.config import get_settings

config = context.config
if config.config_file_name and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name, disable_existing_loggers=False)

target_metadata = Base.metadata


def _url() -> str:
    return normalise_database_url(
        config.get_main_option("sqlalchemy.url") or get_settings().database_url
    )


def _configure(**kwargs) -> None:
    context.configure(
        target_metadata=target_metadata,
        render_as_batch=True,
        compare_type=True,
        **kwargs,
    )


def run_offline() -> None:
    _configure(url=_url(), literal_binds=True, dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.run_migrations()


def _run_sync(connection) -> None:
    _configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    connection = config.attributes.get("connection")
    if connection is not None:
        _run_sync(connection)
        return

    async def _run() -> None:
        engine = create_async_engine(_url())
        try:
            async with engine.begin() as conn:
                await conn.run_sync(_run_sync)
        finally:
            await engine.dispose()

    asyncio.run(_run())


if context.is_offline_mode():
    run_offline()
else:
    run_online()
