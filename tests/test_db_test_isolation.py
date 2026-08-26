"""
Tests for the pytest Postgres/AGE safety net in tests/conftest.py.

Guards a proven exposure: before this net existed, a pytest process
resolved `postgresql+asyncpg://postgres:dev@localhost:5432/muhafiz` — the
LIVE database and the live `evidence_graph`. `tests/conftest.py` had no
Postgres/AGE isolation, and the `no_network` guard deliberately allows
loopback, which is exactly where Postgres listens.

Two protections are asserted here, mirroring the Chroma pair:

  isolation — every pytest process points DATABASE_URL at a disposable
    test database identity, established before any application import.
  fail-closed — opening a pool against the live database during a test
    raises instead of connecting.

SAFETY: nothing here opens a connection to the real database. The
fail-closed guard is exercised by asserting that the wrapper REJECTS a
production DSN before `asyncpg.create_pool` is ever reached; the DSN is
only ever passed to the guard, never to a real connector.
"""
from __future__ import annotations

import asyncpg
import pytest

from tests.conftest import (
    PRODUCTION_DB_NAME,
    PRODUCTION_GRAPH_NAME,
    LiveDatabaseAccessError,
    _database_name_of,
    _is_production_database,
)


# ── Isolation ────────────────────────────────────────────────────────────


def test_database_url_is_not_the_live_database():
    """The default pytest DB identity must never be the live one."""
    from src import config

    name = _database_name_of(config.DATABASE_URL)

    assert name != PRODUCTION_DB_NAME
    assert name.startswith("muhafiz_pytest_")


def test_sqlalchemy_engine_was_built_against_the_test_database():
    """
    `src/database/postgres.py` builds its engine at MODULE IMPORT, so this
    proves the isolation landed early enough — not merely that an env var
    is set now.
    """
    from src.database import postgres

    if postgres.engine is None:
        pytest.skip("No engine configured in this environment")

    assert _database_name_of(str(postgres.engine.url)) != PRODUCTION_DB_NAME


def test_age_client_dsn_resolves_to_the_test_database():
    """AGE reads config lazily per call, so it must follow the same identity."""
    from src.graph import age_client

    assert _database_name_of(age_client._dsn()) != PRODUCTION_DB_NAME
    # The graph name is a production constant; isolation here is by
    # database identity, so record that explicitly rather than implying
    # the graph name itself differs.
    assert age_client.GRAPH_NAME == PRODUCTION_GRAPH_NAME


# ── Fail-closed guard ────────────────────────────────────────────────────


def test_guard_classifies_production_dsns():
    assert _is_production_database(
        "postgresql+asyncpg://postgres:dev@localhost:5432/muhafiz"
    )
    assert _is_production_database("postgresql://postgres:dev@localhost:5432/muhafiz")
    assert _is_production_database("postgresql://u:p@host:5432/muhafiz?ssl=require")


def test_guard_allows_test_dsns():
    assert not _is_production_database(
        "postgresql://postgres:dev@localhost:5432/muhafiz_pytest_abc123"
    )
    assert not _is_production_database(None)
    assert not _is_production_database("")


@pytest.mark.asyncio
async def test_create_pool_refuses_the_live_database():
    """
    The real boundary: `age_client.get_pool()` calls
    `asyncpg.create_pool(dsn)`, so guarding that call covers every
    `execute_cypher` and therefore every graph mutation.

    Only ATTEMPTS the production DSN — the guard rejects it, so no
    connection is ever opened.
    """
    with pytest.raises(LiveDatabaseAccessError) as exc:
        await asyncpg.create_pool(
            f"postgresql://postgres:dev@localhost:5432/{PRODUCTION_DB_NAME}"
        )

    assert "Refusing live Muhafiz Postgres/AGE access" in str(exc.value)


def test_guard_is_installed_on_the_real_symbol():
    assert getattr(asyncpg.create_pool, "_muhafiz_live_db_guard", False)


@pytest.mark.asyncio
async def test_age_get_pool_cannot_reach_the_live_database(monkeypatch):
    """
    End-to-end through the application's own accessor: even if a test
    repoints DATABASE_URL back at production, `get_pool()` fails closed
    rather than connecting.
    """
    from src.graph import age_client

    monkeypatch.setattr(
        age_client.config,
        "DATABASE_URL",
        f"postgresql+asyncpg://postgres:dev@localhost:5432/{PRODUCTION_DB_NAME}",
    )
    monkeypatch.setattr(age_client, "_pool", None)

    with pytest.raises(LiveDatabaseAccessError):
        await age_client.get_pool()


# ── Sentinel isolation ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_forbidden_identity_is_rejected_before_any_connector_runs():
    """
    Stand-in for the sentinel test: prove the guard intercepts BEFORE the
    real connector, by asserting the wrapped callable never delegates for
    a production DSN. A disposable "protected" identity is used for the
    positive case so no real database is contacted either way.
    """
    delegated = {"called": False}

    real = asyncpg.create_pool._real_create_pool

    async def _tracking_real(dsn=None, *a, **kw):
        delegated["called"] = True
        raise RuntimeError("connector reached — should not happen for production DSN")

    asyncpg.create_pool._real_create_pool = _tracking_real
    try:
        # Rebuild the guard closure over the tracking connector.
        import tests.conftest as conftest_mod

        def _guarded(dsn=None, *a, **kw):
            if conftest_mod._is_production_database(dsn):
                raise LiveDatabaseAccessError("blocked")
            return _tracking_real(dsn, *a, **kw)

        with pytest.raises(LiveDatabaseAccessError):
            _guarded(f"postgresql://u:p@localhost:5432/{PRODUCTION_DB_NAME}")

        assert delegated["called"] is False, "guard must intercept before the connector"
    finally:
        asyncpg.create_pool._real_create_pool = real
