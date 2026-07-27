# ============================================================
# PostgreSQL Connection — Async SQLAlchemy Engine
#
# Provides the async engine, session factory, and startup helper
# for the optional PostgreSQL backend.  When DATABASE_URL is not
# set (or is empty), every export resolves to None / False so the
# existing SQLite path continues to work without changes.
#
# USAGE:
#   from src.database.postgres import get_session, init_postgres
#
#   # FastAPI dependency
#   async def my_route(session=Depends(get_session)):
#       ...
#
#   # Application startup
#   await init_postgres()
# ============================================================

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from typing import AsyncGenerator
import contextvars
from sqlalchemy import text

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from src import config

logger = logging.getLogger(__name__)


# ── Engine & Session Factory ──────────────────────────────────────────────────
# Both are set to None when DATABASE_URL is absent, keeping the module
# importable without a running Postgres instance.

_database_url: str | None = getattr(config, "DATABASE_URL", None) or None

# ── Context Variables for RLS ─────────────────────────────────────────────────
current_case_id: contextvars.ContextVar[str | None] = contextvars.ContextVar('current_case_id', default=None)
current_cross_case: contextvars.ContextVar[bool] = contextvars.ContextVar('current_cross_case', default=False)
current_rls_active: contextvars.ContextVar[bool] = contextvars.ContextVar('current_rls_active', default=False)



engine = (
    create_async_engine(
        _database_url,
        echo=False,
        pool_size=15,
        max_overflow=20,
        pool_recycle=1800,
    )
    if _database_url
    else None
)

AsyncSessionLocal = (
    async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )
    if engine
    else None
)


# ── Dependency — async session generator ──────────────────────────────────────

@asynccontextmanager
async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """
    Async context manager that yields an ``AsyncSession``.

    Intended for use as a FastAPI dependency::

        @router.get("/items")
        async def list_items(session=Depends(get_session)):
            ...

    Commits on success, rolls back on exception, and always closes.
    Raises ``RuntimeError`` if PostgreSQL is not configured.
    """
    if AsyncSessionLocal is None:
        raise RuntimeError(
            "PostgreSQL is not configured. "
            "Set DATABASE_URL in your .env file to enable Postgres."
        )

    async with AsyncSessionLocal() as session:
        try:
            case_id = current_case_id.get()
            cross_case = current_cross_case.get()
            rls_active = current_rls_active.get()

            if rls_active:
                await session.execute(text("SET LOCAL app.rls_active = 'true'"))
                # Phase 2: ALWAYS set app.case_id when RLS is active for
                # this request — never leave it unset. Migration 010's
                # policies compare against the empty string explicitly
                # (case_id = '' means "general, no case") instead of
                # relying on Postgres's NULL = NULL semantics, which
                # previously made every general (no-case) row invisible —
                # and, since these are FOR ALL policies with no separate
                # WITH CHECK, made inserting one fail too (issues.md's
                # Critical NULL-vs-NULL finding). set_config() is used
                # instead of "SET LOCAL app.case_id = ..." because
                # SET/SET LOCAL do not accept bind parameters at all
                # ("SET LOCAL app.case_id = $1" is a syntax error, not a
                # runtime one) — set_config() is a regular function call
                # and does, with is_local=true reproducing SET LOCAL's
                # transaction-scoped behavior.
                await session.execute(
                    text("SELECT set_config('app.case_id', :case_id, true)"),
                    {"case_id": case_id or ""},
                )
            if cross_case:
                await session.execute(text("SET LOCAL app.cross_case = 'true'"))

            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


# ── Table Initialisation ──────────────────────────────────────────────────────

async def init_postgres() -> None:
    """
    Create all tables defined in ``models.Base.metadata``.

    Safe to call multiple times — SQLAlchemy's ``create_all`` uses
    ``CREATE TABLE IF NOT EXISTS`` under the hood.

    Called once during application startup (only when Postgres is enabled).
    """
    if engine is None:
        logger.debug("init_postgres() skipped — DATABASE_URL is not set.")
        return

    try:
        from src.database.models import Base  # noqa: WPS433 (nested import)

        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)

        logger.info("PostgreSQL tables created / verified successfully.")
    except Exception as exc:
        logger.error("Failed to initialise PostgreSQL tables: %s", exc)
        raise


# ── Helper ────────────────────────────────────────────────────────────────────

def is_postgres_configured() -> bool:
    """Return ``True`` if ``DATABASE_URL`` is set and the engine was created."""
    return engine is not None
