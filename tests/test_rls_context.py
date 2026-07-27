"""
Tests for src/auth/rls_context.py (Phase 2 — request-scoped RLS context).

Pure contextvar logic, no live Postgres needed — see
tests/test_rls_integration.py for the live-DB verification of what these
contextvars actually cause Postgres to do.
"""
import pytest

from src.auth import rls_context
from src.database.postgres import current_case_id, current_cross_case, current_rls_active


@pytest.fixture(autouse=True)
def reset_context():
    """Contextvars are process-wide defaults; don't let one test's .set() leak into the next."""
    current_case_id.set(None)
    current_cross_case.set(False)
    current_rls_active.set(False)
    yield
    current_case_id.set(None)
    current_cross_case.set(False)
    current_rls_active.set(False)


def test_set_case_scope_arms_rls_and_sets_case_id():
    rls_context.set_case_scope("CASE-001")
    assert current_rls_active.get() is True
    assert current_case_id.get() == "CASE-001"
    assert current_cross_case.get() is False


def test_set_case_scope_with_falsy_case_id_sets_empty_string_not_none():
    """
    The direct fix for the NULL-vs-NULL bug: a general (no-case) request
    must set app.case_id to '' — an explicit, comparable value — never
    leave it as None/unset (issues.md's Critical finding).
    """
    rls_context.set_case_scope(None)
    assert current_rls_active.get() is True
    assert current_case_id.get() == ""
    assert current_cross_case.get() is False


def test_set_cross_case_scope_arms_bypass():
    rls_context.set_cross_case_scope()
    assert current_rls_active.get() is True
    assert current_case_id.get() == ""
    assert current_cross_case.get() is True


@pytest.mark.asyncio
async def test_case_rls_dependency_sets_scope_and_returns_case_id():
    result = await rls_context.case_rls_dependency("CASE-042")
    assert result == "CASE-042"
    assert current_case_id.get() == "CASE-042"
    assert current_cross_case.get() is False


@pytest.mark.asyncio
async def test_cross_case_rls_dependency_arms_bypass():
    await rls_context.cross_case_rls_dependency()
    assert current_rls_active.get() is True
    assert current_cross_case.get() is True
