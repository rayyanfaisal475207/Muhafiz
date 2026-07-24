# ============================================================
# Alembic Environment — TaxIQ
#
# Configures Alembic to use:
#   1. DATABASE_URL from .env (loaded via src.config)
#   2. Base.metadata from src.database.models for autogenerate
#
# USAGE:
#   alembic revision --autogenerate -m "description"
#   alembic upgrade head
#   alembic downgrade -1
# ============================================================

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.ext.asyncio import create_async_engine

# ── Load project config (ensures .env is loaded) ─────────────────────────────
from src import config as app_config

# ── Import all models so metadata is populated ──────────────────────────────
from src.database.models import Base

# ── Alembic Config ────────────────────────────────────────────────────────────
alembic_cfg = context.config

if alembic_cfg.config_file_name is not None:
    fileConfig(alembic_cfg.config_file_name)

# The target metadata for autogenerate support
target_metadata = Base.metadata


def get_url() -> str:
    """Get DATABASE_URL from the application config (loaded from .env)."""
    url = getattr(app_config, "DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set in .env. "
            "Cannot run Alembic migrations without a PostgreSQL connection string."
        )
    return url


def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode — generates SQL without connecting.
    Useful for generating migration scripts to review before applying.
    """
    url = get_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection) -> None:
    """Run migrations against a live connection (used by both sync and async)."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """
    Run migrations in 'online' mode with an async engine.
    This is the default path when you run `alembic upgrade head`.
    """
    connectable = create_async_engine(
        get_url(),
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Entry point for online migrations — delegates to async runner."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
