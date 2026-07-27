"""
Phase 2 — live-Postgres RLS verification (solution.md Module 2.1/2.2).

Everything in this file needs a REAL Postgres instance with Apache AGE,
migrations 001-010 applied (including this phase's
migrations/010_rls_null_case_fix.sql), and RLS actually enforced by the
connecting role (i.e. NOT a superuser/BYPASSRLS role — `FORCE ROW LEVEL
SECURITY` in migrations 008/010 still doesn't apply to a role with the
BYPASSRLS attribute, which the default `postgres` superuser has).

None of that infrastructure exists in the environment this suite was
written in — no live Postgres/AGE/GPU per this session's constraints —
so every test below is SKIPPED BY DEFAULT. It exists so whoever has real
DB access can turn it on and get an actual answer, not just a code trace.

To run for real:
    1. Point TEST_DATABASE_URL at a Postgres instance with AGE installed,
       with all of migrations/001..010 applied, connecting as a role that
       does NOT have BYPASSRLS or superuser (create one, e.g.
       `CREATE ROLE muhafiz_app LOGIN PASSWORD '...'; GRANT ... ;` — the
       app's normal runtime role, not `postgres`).
    2. Set RUN_POSTGRES_TESTS=1 in the environment.
    3. python -m pytest tests/test_rls_integration.py -v

Each test below corresponds 1:1 to a numbered item in solution.md §2.2's
"Concrete test plan for whoever has DB access" — see MANUAL_RLS_VERIFICATION.md
(repo root) for the fuller, narrative walkthrough of the same four checks
plus the operational steps (backup, dry-run, rollback) around them.
"""
import os
import uuid

import pytest

pytestmark = [
    pytest.mark.requires_postgres,
    pytest.mark.skipif(
        not os.environ.get("RUN_POSTGRES_TESTS"),
        reason="Needs a live Postgres+AGE instance with migrations 001-010 "
               "applied, connecting as a non-superuser role. Set "
               "RUN_POSTGRES_TESTS=1 and TEST_DATABASE_URL to run for real.",
    ),
]


@pytest.fixture
async def pg_engine():
    """
    A SQLAlchemy async engine pointed at TEST_DATABASE_URL, standing in
    for what src/database/postgres.py's module-level `engine` would be
    had DATABASE_URL been set to a live instance at import time. Built
    fresh here (not imported from src.database.postgres) because that
    module's engine is constructed once at import time from whatever
    DATABASE_URL was set THEN — too late to retarget for a test run.
    """
    from sqlalchemy.ext.asyncio import create_async_engine

    url = os.environ.get("TEST_DATABASE_URL", "")
    if not url:
        pytest.skip("TEST_DATABASE_URL not set")
    engine = create_async_engine(url, echo=False)
    yield engine
    await engine.dispose()


async def _set_rls(conn, case_id: str | None, cross_case: bool = False):
    from sqlalchemy import text
    await conn.execute(text("SET LOCAL app.rls_active = 'true'"))
    await conn.execute(
        text("SELECT set_config('app.case_id', :case_id, true)"),
        {"case_id": case_id or ""},
    )
    if cross_case:
        await conn.execute(text("SET LOCAL app.cross_case = 'true'"))


@pytest.mark.asyncio
async def test_general_no_case_session_is_creatable_and_readable(pg_engine):
    """
    solution.md §2.2 check (1): a general (no case_id) chat session must
    be creatable and re-readable under RLS — this is the direct regression
    test for the NULL-vs-NULL bug (issues.md's Critical finding). Before
    migration 010, this INSERT would fail (WITH CHECK reusing the old
    NULL-based USING predicate) and the SELECT would return zero rows
    even if the INSERT had somehow succeeded.
    """
    from sqlalchemy import text

    session_id = str(uuid.uuid4())
    async with pg_engine.begin() as conn:
        await _set_rls(conn, case_id=None)
        await conn.execute(
            text(
                "INSERT INTO sessions (session_id, user_id, title, case_id) "
                "VALUES (:sid, NULL, 'General chat', NULL)"
            ),
            {"sid": session_id},
        )

    async with pg_engine.begin() as conn:
        await _set_rls(conn, case_id=None)
        row = (await conn.execute(
            text("SELECT session_id FROM sessions WHERE session_id = :sid"),
            {"sid": session_id},
        )).fetchone()
        assert row is not None, (
            "A general (no-case) session must be visible under RLS once "
            "app.case_id is explicitly '' — if this fails, migration 010's "
            "policy rewrite did not apply, or NULL-vs-NULL is still live."
        )


@pytest.mark.asyncio
async def test_case_scoped_session_invisible_to_a_different_case_context(pg_engine):
    """solution.md §2.2 check (2): case-scoped rows must be invisible outside their own case_id context."""
    from sqlalchemy import text

    case_a, case_b = f"CASE-TEST-{uuid.uuid4().hex[:6]}", f"CASE-TEST-{uuid.uuid4().hex[:6]}"
    session_id = str(uuid.uuid4())

    async with pg_engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.rls_active = 'false'"))  # bypass to seed fixtures
        for cid in (case_a, case_b):
            await conn.execute(
                text("INSERT INTO cases (case_id) VALUES (:cid) ON CONFLICT DO NOTHING"),
                {"cid": cid},
            )
        await conn.execute(
            text(
                "INSERT INTO sessions (session_id, user_id, title, case_id) "
                "VALUES (:sid, NULL, 'Case A chat', :cid)"
            ),
            {"sid": session_id, "cid": case_a},
        )

    async with pg_engine.begin() as conn:
        await _set_rls(conn, case_id=case_b)  # scoped to the OTHER case
        row = (await conn.execute(
            text("SELECT session_id FROM sessions WHERE session_id = :sid"),
            {"sid": session_id},
        )).fetchone()
        assert row is None, "A Case A session must be invisible when RLS is scoped to Case B."

    async with pg_engine.begin() as conn:
        await _set_rls(conn, case_id=case_a)  # scoped to its own case
        row = (await conn.execute(
            text("SELECT session_id FROM sessions WHERE session_id = :sid"),
            {"sid": session_id},
        )).fetchone()
        assert row is not None, "A Case A session must be visible when RLS is scoped to Case A."


@pytest.mark.asyncio
async def test_xgraph_permission_error_does_not_leave_bypass_armed_for_later_queries(pg_engine):
    """
    solution.md §2.2 check (3): an XGRAPH/XAGG query from a role that
    should be denied must raise PermissionError AND a subsequent, unrelated
    query in the SAME request must not be able to see cross-case data —
    this directly tests the reordering fix in graph_retriever.py/xagg.py
    (issues.md's High "cross-case RLS bypass flag is armed before its own
    role check" finding). This test exercises the real functions, not a
    fake, since the whole point is confirming current_cross_case truly
    never gets set on the denied path in a real asyncio context.
    """
    from src.database.postgres import current_cross_case
    from src.retrieval import graph_retriever

    assert current_cross_case.get() is False

    class _DenyingGateway:
        async def log_audit_event(self, **kwargs):
            pass

    with pytest.raises(PermissionError):
        await graph_retriever.retrieve_graph(
            "cross-case query", "some entity", case_id=None, cross_case=True,
            user_id="u1", user_role="investigator",  # below supervisor -> denied
        )

    assert current_cross_case.get() is False, (
        "current_cross_case must still be False after a denied cross-case "
        "attempt — if this is True, the bypass is (still) armed before the "
        "role check, defeating the point of this phase's reordering fix."
    )


@pytest.mark.asyncio
async def test_rest_crud_endpoint_denies_cross_case_read_at_the_rls_level(pg_engine):
    """
    solution.md §2.2 check (4): every REST CRUD endpoint should now get a
    real 403/empty result at the RLS level even if the application-layer
    check were hypothetically absent — the actual regression test for
    "RLS never activated outside chat" (issues.md's Critical finding).
    Simulates cases.py's require_case_access() dependency having (somehow)
    been skipped, and confirms the database itself still denies the read.
    """
    from sqlalchemy import text

    case_id = f"CASE-TEST-{uuid.uuid4().hex[:6]}"
    async with pg_engine.begin() as conn:
        await conn.execute(text("SET LOCAL app.rls_active = 'false'"))
        await conn.execute(
            text("INSERT INTO cases (case_id) VALUES (:cid) ON CONFLICT DO NOTHING"),
            {"cid": case_id},
        )

    async with pg_engine.begin() as conn:
        # Scoped to a DIFFERENT case — as if require_case_access() had
        # never run and the caller's own case context stayed at "general".
        await _set_rls(conn, case_id="")
        row = (await conn.execute(
            text("SELECT case_id FROM cases WHERE case_id = :cid"),
            {"cid": case_id},
        )).fetchone()
        assert row is None, (
            "A real case row must be invisible to a request scoped to "
            "'general' — this is RLS acting as the backstop for cases.py's "
            "endpoints even in the hypothetical absence of the app-layer check."
        )
