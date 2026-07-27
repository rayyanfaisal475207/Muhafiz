"""
Tests for src/database/postgres.py::get_session()'s RLS context wiring
(Phase 2). A fake AsyncSession/sessionmaker stands in for the real
SQLAlchemy engine — no live Postgres. See tests/test_rls_integration.py
for confirming the actual policy predicates behave correctly against a
real instance.
"""
import pytest

import src.database.postgres as pg


class FakeResult:
    pass


class FakeAsyncSession:
    def __init__(self):
        self.executed: list[tuple] = []
        self.committed = False
        self.rolled_back = False

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return FakeResult()

    async def commit(self):
        self.committed = True

    async def rollback(self):
        self.rolled_back = True

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSessionFactory:
    """Callable standing in for AsyncSessionLocal — records the one session it hands out."""

    def __init__(self):
        self.last_session: FakeAsyncSession | None = None

    def __call__(self):
        self.last_session = FakeAsyncSession()
        return self.last_session


@pytest.fixture(autouse=True)
def reset_context():
    pg.current_case_id.set(None)
    pg.current_cross_case.set(False)
    pg.current_rls_active.set(False)
    yield
    pg.current_case_id.set(None)
    pg.current_cross_case.set(False)
    pg.current_rls_active.set(False)


@pytest.fixture
def fake_session_factory(monkeypatch):
    factory = FakeSessionFactory()
    monkeypatch.setattr(pg, "AsyncSessionLocal", factory)
    return factory


def _case_id_param(session: FakeAsyncSession) -> str | None:
    for stmt, params in session.executed:
        if "set_config" in stmt and "app.case_id" in stmt:
            return params["case_id"]
    return None


@pytest.mark.asyncio
async def test_general_request_sets_case_id_to_empty_string_not_unset(fake_session_factory):
    """
    Direct regression test for the NULL-vs-NULL bug: when RLS is active
    but there's no case (a general chat/REST call), app.case_id must be
    explicitly set to '' — never left unset — so migration 010's policies
    compare against a real value instead of relying on
    current_setting(...) returning NULL for both sides.
    """
    pg.current_rls_active.set(True)
    pg.current_case_id.set(None)

    async with pg.get_session() as session:
        pass

    assert _case_id_param(session) == "", (
        "app.case_id must be set to '' for a general request when RLS is "
        "active — leaving it unset reproduces the NULL-vs-NULL bug."
    )


@pytest.mark.asyncio
async def test_case_scoped_request_sets_real_case_id(fake_session_factory):
    pg.current_rls_active.set(True)
    pg.current_case_id.set("CASE-007")

    async with pg.get_session() as session:
        pass

    assert _case_id_param(session) == "CASE-007"


@pytest.mark.asyncio
async def test_rls_inactive_request_never_touches_rls_session_vars(fake_session_factory):
    """
    When a request never armed RLS at all (rls_active still False —
    shouldn't happen for any wired-in router post-Phase-2, but this
    guards the fallback), get_session() must not issue any SET/set_config
    calls — there's nothing for the caller to have gotten wrong here that
    this test would catch, it just documents the boundary.
    """
    async with pg.get_session() as session:
        pass

    assert not any("rls_active" in stmt for stmt, _ in session.executed)
    assert not any("app.case_id" in stmt for stmt, _ in session.executed)


@pytest.mark.asyncio
async def test_cross_case_sets_bypass_flag(fake_session_factory):
    pg.current_rls_active.set(True)
    pg.current_case_id.set(None)
    pg.current_cross_case.set(True)

    async with pg.get_session() as session:
        pass

    assert any("app.cross_case" in stmt for stmt, _ in session.executed)
    # cross_case is still a request-wide setting on TOP of the always-set
    # case_id — the empty-string fix applies regardless of cross_case.
    assert _case_id_param(session) == ""
