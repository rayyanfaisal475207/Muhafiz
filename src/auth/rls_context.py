# ============================================================
# Request-scoped Postgres RLS context (Phase 2 of the audit
# remediation — see solution.md Module 2.1/2.2, issues.md's Critical
# "Postgres RLS is never activated for any REST CRUD endpoint" finding).
#
# Before this module, `current_rls_active`/`current_case_id`/
# `current_cross_case` (src/database/postgres.py) were set in exactly one
# place in the whole codebase: the chat pipeline (orchestrator.py). Every
# REST CRUD endpoint ran with RLS fully inactive, relying solely on
# application-layer checks with no database-level backstop. This module is
# the single place every authenticated router now arms that context from,
# instead of leaving each route to remember on its own.
#
# Two shapes, not one, because the routers this wires into fall into two
# genuinely different categories once you look at what they actually do:
#
#   - CASE-SCOPED (`set_case_scope`): the route's own case_id is known
#     before the query runs (a `{case_id}` path param, or one computed
#     early in a handler body before touching the gateway) — real
#     per-case enforcement. Used by cases.py's get/update/delete/create
#     and all of case_assignments.py.
#
#   - CROSS-CASE BYPASS (`set_cross_case_scope`): RLS is still armed
#     (`rls_active=True`, closing the "never activated" gap for these
#     routers too) but the case dimension is explicitly bypassed, because
#     either:
#       (a) the resource is looked up by its own id and gated by
#           user-ownership, not case membership (sessions.py,
#           attachments.py) — setting a restrictive case_id here would
#           incorrectly hide a legitimately case-scoped row from its own
#           owner, the exact NULL-vs-NULL failure mode this phase exists
#           to fix, just relocated to a new table/policy; or
#       (b) the endpoint is deliberately platform-wide (admin.py's
#           dashboards, which aggregate pipeline_runs/error_logs/etc.
#           across every case for station-admin/platform-admin — a real
#           per-case restriction here wouldn't narrow the dashboard, it
#           would silently break it) or deliberately cross-case by
#           product design pending a decision (graph_review.py's review
#           queue — see solution.md §9.2, not resolved by this phase); or
#       (c) the table touched carries no RLS policy at all (projects.py —
#           `projects` isn't one of migration 008/010's covered tables,
#           so this is a documented no-op either way).
#     In every one of these cases the REAL access control remains the
#     existing application-layer check (ownership/role) — this does not
#     weaken anything that worked before; it makes explicit, for the
#     first time, that RLS is armed-but-bypassed here rather than
#     accidentally never armed at all.
#
# Call the plain functions directly at the top of a route handler when the
# case_id isn't resolvable until partway through the handler body (e.g.
# cases.py::create_case, which generates/validates case_id before any
# gateway write). Use the `Depends()`-wrappers below only where every
# route on a router uniformly needs the same scope and the case_id (if
# any) is a genuine path parameter resolvable before the handler runs.
# ============================================================

from __future__ import annotations

from src.database.postgres import current_case_id, current_cross_case, current_rls_active


def set_case_scope(case_id: str | None) -> None:
    """
    Arm RLS for the remainder of this request, scoped to one case.

    `case_id` falsy -> general (no-case) scope: always set the empty
    string, never leave `app.case_id` unset — this is the direct fix for
    the NULL-vs-NULL bug (migration 010 rewrites the policies to compare
    against '' explicitly rather than relying on SQL's NULL semantics).
    """
    current_rls_active.set(True)
    current_case_id.set(case_id or "")
    current_cross_case.set(False)


def set_cross_case_scope() -> None:
    """
    Arm RLS for the remainder of this request, with the case dimension
    explicitly bypassed — see module docstring for which routers use this
    and why each one is safe to.
    """
    current_rls_active.set(True)
    current_case_id.set("")
    current_cross_case.set(True)


async def case_rls_dependency(case_id: str) -> str:
    """
    FastAPI dependency for routers whose every route has `case_id` as a
    real path parameter (case_assignments.py) — FastAPI resolves it from
    the path before this dependency runs, so it's safe to arm here.
    """
    set_case_scope(case_id)
    return case_id


async def cross_case_rls_dependency() -> None:
    """
    FastAPI router-level dependency for routers that uniformly need the
    bypass scope (sessions.py, attachments.py, admin.py, graph_review.py,
    projects.py) — see module docstring, category CROSS-CASE BYPASS.
    """
    set_cross_case_scope()
