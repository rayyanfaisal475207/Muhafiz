"""
HTTP layer — auth, ownership, downloads.

Guards:
  * every data route requires authentication
  * a user cannot read, rename, or delete another user's session
  * downloads carry a real filename + MIME type (they used to be extensionless
    application/octet-stream, which Windows can't open)
  * admin routes require the is_admin flag, not merely a valid login
"""
import os
import uuid
from contextlib import asynccontextmanager

import pytest
from fastapi.testclient import TestClient

from src import config
from src.main import app
from src.auth.routes import get_current_user
from src.data_gateway import get_gateway as real_get_gateway


class _User:
    def __init__(self, user_id, is_admin=False, role=None, police_station=None):
        self.id = uuid.UUID(user_id)
        self.email = "test@example.com"
        self.is_admin = is_admin
        # src.auth.jwt.require_role() reads .role, not the old is_admin flag
        # (Phase 7 replaced is_admin with a role enum) — default to the
        # least-privileged role unless the caller asks for admin.
        self.role = role or ("platform-admin" if is_admin else "investigator")
        self.company_name = "TestCo"
        self.plan = "free"
        # Phase 5, Module 5.1: station-scoping anchor, nullable until backfilled.
        self.police_station = police_station


@pytest.fixture
def api(gateway, user_id, monkeypatch):
    """TestClient with auth and the data gateway faked out."""
    async def _get_gateway():
        return gateway

    # Patch the package attribute too: several endpoints import get_gateway
    # lazily inside the function body (`from src.data_gateway import get_gateway`),
    # which resolves against the package at call time.
    for module in ("src.data_gateway", "src.data_gateway.selector", "src.main",
                   "src.api.sessions", "src.api.projects", "src.api.profile",
                   "src.api.admin", "src.auth.routes", "src.api.case_assignments",
                   "src.api.cases", "src.api.attachments"):
        monkeypatch.setattr(f"{module}.get_gateway", _get_gateway, raising=False)

    app.dependency_overrides[get_current_user] = lambda: _User(user_id)
    app.dependency_overrides[real_get_gateway] = _get_gateway

    yield TestClient(app), gateway

    app.dependency_overrides.clear()


@pytest.fixture
def admin_api(api, user_id):
    client, gateway = api
    # require_role("platform-admin")'s dependency itself depends on
    # get_current_user and checks .role — overriding get_current_user is
    # enough; require_role() builds a fresh closure per call site, so
    # overriding a freshly-called instance here wouldn't match the ones
    # already wired into src/api/admin.py's routes at import time.
    app.dependency_overrides[get_current_user] = lambda: _User(user_id, is_admin=True, role="platform-admin")
    return client, gateway


# ── Authentication ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("method,path", [
    ("get", "/api/sessions"),
    ("get", "/api/attachments"),
    ("post", "/api/chat"),
    ("get", "/api/admin/metrics"),
    ("get", "/api/admin/kb/stats"),
    ("get", "/api/admin/errors"),
])
def test_data_routes_require_authentication(method, path):
    """
    No dependency override installed → the real auth dependency must reject the
    request before the endpoint body (and therefore before any DB access) runs.
    TestClient is deliberately NOT used as a context manager: that would run the
    app lifespan, which tries to reach the database.
    """
    client = TestClient(app)

    request = getattr(client, method)
    response = request(path, json={}) if method in ("post", "patch", "put") else request(path)

    assert response.status_code in (401, 403), f"{path} is reachable unauthenticated"


# ── Auth response shape ───────────────────────────────────────────────────────
#
# Regression: the Phase 7 is_admin -> role migration removed the `is_admin`
# column from `users` and from every dict the gateway returns, but
# register/me still read/returned `is_admin` directly (KeyError /
# AttributeError on every call). Both endpoints must work end to end, and
# `is_admin` must still be present (derived from role) since existing
# frontend code (authStore.ts, AuthContext.tsx, UsersPage.tsx) reads it.

def test_register_returns_role_and_derived_is_admin(api):
    client, gateway = api

    response = client.post("/api/auth/register", json={
        "email": "new.investigator@example.com", "password": "hunter2pass2",
    })

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "investigator"
    assert body["is_admin"] is False


def test_me_returns_role_and_derived_is_admin(api, user_id):
    client, gateway = api

    app.dependency_overrides[get_current_user] = lambda: _User(user_id, is_admin=True, role="platform-admin")

    response = client.get("/api/auth/me")

    assert response.status_code == 200
    body = response.json()
    assert body["role"] == "platform-admin"
    assert body["is_admin"] is True


# ── Session history — case filtering ─────────────────────────────────────────
#
# Regression: selecting a case in the sidebar had no effect on the chat
# history list — GET /api/sessions only ever filtered on project_id. This
# guards the fix: passing case_id actually narrows the list.

def test_sessions_can_be_filtered_by_case(api, user_id):
    client, gateway = api
    gateway.sessions["s1"] = {"session_id": "s1", "user_id": user_id, "project_id": None, "case_id": "CASE-001", "title": "Case 1 chat"}
    gateway.sessions["s2"] = {"session_id": "s2", "user_id": user_id, "project_id": None, "case_id": "CASE-002", "title": "Case 2 chat"}
    gateway.sessions["s3"] = {"session_id": "s3", "user_id": user_id, "project_id": None, "case_id": None, "title": "No case"}

    response = client.get("/api/sessions", params={"case_id": "CASE-001"})

    assert response.status_code == 200
    titles = [s["title"] for s in response.json()]
    assert titles == ["Case 1 chat"]


def test_sessions_without_case_filter_returns_everything(api, user_id):
    client, gateway = api
    gateway.sessions["s1"] = {"session_id": "s1", "user_id": user_id, "project_id": None, "case_id": "CASE-001", "title": "Case 1 chat"}
    gateway.sessions["s2"] = {"session_id": "s2", "user_id": user_id, "project_id": None, "case_id": None, "title": "No case"}

    response = client.get("/api/sessions")

    assert response.status_code == 200
    assert len(response.json()) == 2


# ── Case assignments — email-based, role-gated ───────────────────────────────
#
# case_assignments.py had no frontend caller at all before this pass. These
# guard the fix that lets a station-admin assign someone by email (rather
# than needing the platform-admin-only user list to find a raw user_id), and
# that the endpoints stay gated to station-admin/platform-admin.

@pytest.fixture
def station_admin_api(api, user_id):
    client, gateway = api
    # F-08: a station-admin with no police_station is denied outright, so
    # this fixture's admin and CASE-001 must share a station for the
    # existing "can assign" tests below to exercise the intended path
    # rather than the now-fail-closed NULL-station case.
    gateway.cases["CASE-001"] = {"case_id": "CASE-001", "police_station": "Kohsar"}
    app.dependency_overrides[get_current_user] = lambda: _User(user_id, role="station-admin", police_station="Kohsar")
    return client, gateway


def test_assign_user_to_case_by_email(station_admin_api, gateway):
    client, gw = station_admin_api
    target_id = str(uuid.uuid4())
    gw.users[target_id] = {"id": target_id, "email": "investigator@example.com", "role": "investigator"}

    response = client.post("/api/cases/CASE-001/assignments/", json={
        "email": "investigator@example.com", "role": "investigator",
    })

    assert response.status_code == 200
    assignments = gw.case_assignments.get("CASE-001", [])
    assert len(assignments) == 1
    assert assignments[0]["email"] == "investigator@example.com"
    assert assignments[0]["role"] == "investigator"


def test_assign_user_to_case_unknown_email_404s(station_admin_api):
    client, _ = station_admin_api

    response = client.post("/api/cases/CASE-001/assignments/", json={
        "email": "nobody@example.com", "role": "investigator",
    })

    assert response.status_code == 404


def test_supervisor_cannot_assign_case_users(api):
    """Assignment endpoints require station-admin or higher — a plain
    supervisor (who CAN view the Case Management page read-only) must not
    be able to mutate assignments."""
    client, _ = api  # default _User is role="investigator" via the api fixture
    app.dependency_overrides[get_current_user] = lambda: _User(str(uuid.uuid4()), role="supervisor")

    response = client.post("/api/cases/CASE-001/assignments/", json={
        "email": "someone@example.com", "role": "investigator",
    })

    assert response.status_code == 403


def test_supervisor_can_view_entity_resolution_eval_metrics(api):
    """
    admin-frontend's Sidebar/App.tsx gate the Entity Eval page at
    supervisor-or-higher (same tier as Review Queue/Ingestion Quality) — the
    backend must not be stricter than the page it's serving. Confirmed live
    against a real running backend during a full E2E pass that a supervisor
    account previously got a 403 here; this guards the fix."""
    client, _ = api
    app.dependency_overrides[get_current_user] = lambda: _User(str(uuid.uuid4()), role="supervisor")

    response = client.get("/api/admin/eval/entity-resolution")

    assert response.status_code == 200


# ── Case mutation scoping (Phase 5, Module 5.1) ──────────────────────────────
#
# Any user assigned to a case, at any role, used to be able to permanently
# edit or delete the case record — check_case_access() only checked row
# EXISTENCE in case_assignments, ignoring the assignment's own role column.
# These guard the fix: update_case/delete_case now require the caller's
# PER-CASE assignment role to be supervisor-or-above; read/list access is
# unaffected.

def test_investigator_assignment_cannot_update_case(api, user_id):
    client, gateway = api
    gateway.cases["CASE-001"] = {"case_id": "CASE-001", "fir_number": "FIR-1"}
    gateway.case_assignments["CASE-001"] = [{"user_id": user_id, "email": "x@example.com", "role": "investigator"}]

    response = client.put("/api/cases/CASE-001", json={"fir_number": "FIR-2"})

    assert response.status_code == 403


def test_investigator_assignment_cannot_delete_case(api, user_id):
    client, gateway = api
    gateway.cases["CASE-001"] = {"case_id": "CASE-001", "fir_number": "FIR-1"}
    gateway.case_assignments["CASE-001"] = [{"user_id": user_id, "email": "x@example.com", "role": "investigator"}]

    response = client.delete("/api/cases/CASE-001")

    assert response.status_code == 403
    assert "CASE-001" in gateway.cases, "case must not be deleted on a 403"


def test_supervisor_assignment_can_update_and_delete_case(api, user_id):
    client, gateway = api
    gateway.cases["CASE-001"] = {"case_id": "CASE-001", "fir_number": "FIR-1"}
    gateway.case_assignments["CASE-001"] = [{"user_id": user_id, "email": "x@example.com", "role": "supervisor"}]

    update_response = client.put("/api/cases/CASE-001", json={"fir_number": "FIR-2"})
    assert update_response.status_code == 200
    assert gateway.cases["CASE-001"]["fir_number"] == "FIR-2"

    delete_response = client.delete("/api/cases/CASE-001")
    assert delete_response.status_code == 200
    assert "CASE-001" not in gateway.cases


def test_investigator_assignment_can_still_read_case(api, user_id):
    """Read access stays at the original 'any assignment' threshold."""
    client, gateway = api
    gateway.cases["CASE-001"] = {"case_id": "CASE-001", "fir_number": "FIR-1"}
    gateway.case_assignments["CASE-001"] = [{"user_id": user_id, "email": "x@example.com", "role": "investigator"}]

    response = client.get("/api/cases/CASE-001")

    assert response.status_code == 200


# ── Case-assignment station-scoping (Phase 5, Module 5.1) ────────────────────
#
# Case-assignment routes used to be gated only by the global "station-admin"
# role, with no check that the case actually belongs to that admin's
# station. These guard the fix, and (audit finding F-08) its NULL-station
# handling: a station-admin with no police_station configured is denied,
# not given unrestricted cross-station access.

def test_station_admin_mismatched_station_cannot_assign(api, user_id):
    client, gateway = api
    gateway.cases["CASE-001"] = {"case_id": "CASE-001", "police_station": "Kohsar"}
    gateway.users[user_id] = {"id": user_id, "email": "investigator@example.com", "role": "investigator"}
    app.dependency_overrides[get_current_user] = lambda: _User(user_id, role="station-admin", police_station="Aabpara")

    response = client.post("/api/cases/CASE-001/assignments/", json={
        "email": "investigator@example.com", "role": "investigator",
    })

    assert response.status_code == 403


def test_station_admin_matching_station_can_assign(api, user_id):
    client, gateway = api
    gateway.cases["CASE-001"] = {"case_id": "CASE-001", "police_station": "Kohsar"}
    target_id = str(uuid.uuid4())
    gateway.users[target_id] = {"id": target_id, "email": "investigator@example.com", "role": "investigator"}
    app.dependency_overrides[get_current_user] = lambda: _User(user_id, role="station-admin", police_station="Kohsar")

    response = client.post("/api/cases/CASE-001/assignments/", json={
        "email": "investigator@example.com", "role": "investigator",
    })

    assert response.status_code == 200


def test_station_admin_with_no_station_configured_is_denied(api, user_id):
    """
    F-08 fix: police_station IS NULL used to fall back to unrestricted
    cross-station access ("not yet backfilled"). It now denies by default —
    an account with no station configured has no station to scope against.
    """
    client, gateway = api
    gateway.cases["CASE-001"] = {"case_id": "CASE-001", "police_station": "Kohsar"}
    target_id = str(uuid.uuid4())
    gateway.users[target_id] = {"id": target_id, "email": "investigator@example.com", "role": "investigator"}
    app.dependency_overrides[get_current_user] = lambda: _User(user_id, role="station-admin", police_station=None)

    response = client.post("/api/cases/CASE-001/assignments/", json={
        "email": "investigator@example.com", "role": "investigator",
    })

    assert response.status_code == 403


def test_platform_admin_always_can_assign_regardless_of_station(api, user_id):
    client, gateway = api
    gateway.cases["CASE-001"] = {"case_id": "CASE-001", "police_station": "Kohsar"}
    target_id = str(uuid.uuid4())
    gateway.users[target_id] = {"id": target_id, "email": "investigator@example.com", "role": "investigator"}
    app.dependency_overrides[get_current_user] = lambda: _User(user_id, role="platform-admin", police_station="Somewhere Else")

    response = client.post("/api/cases/CASE-001/assignments/", json={
        "email": "investigator@example.com", "role": "investigator",
    })

    assert response.status_code == 200


def test_unassign_user_from_case(station_admin_api):
    client, gw = station_admin_api
    target_id = str(uuid.uuid4())
    gw.case_assignments["CASE-001"] = [{"user_id": target_id, "email": "x@example.com", "role": "investigator"}]

    response = client.delete(f"/api/cases/CASE-001/assignments/{target_id}")

    assert response.status_code == 200
    assert gw.case_assignments["CASE-001"] == []


# ── Chat / case ABAC ──────────────────────────────────────────────────────────
#
# Regression: /api/chat used to pass request.case_id straight into the
# pipeline with no authorization check at all — RLS alone only enforces "does
# this row belong to case_id", never "is this user assigned to this case_id".
# These guard the fix: a case_id the caller isn't assigned to must 403 before
# process_query ever runs, and a session-remembered case_id gets the same check.

def test_chat_rejects_a_case_the_user_is_not_assigned_to(api, session_id):
    client, gateway = api
    # A real case (must exist so the 404-before-403 existence check below
    # doesn't shadow the 403 this test actually means to exercise).
    gateway.cases["CASE-FORBIDDEN"] = {"case_id": "CASE-FORBIDDEN"}
    gateway.denied_case_ids.add("CASE-FORBIDDEN")

    response = client.post("/api/chat", json={
        "session_id": session_id, "message": "tell me about this case",
        "case_id": "CASE-FORBIDDEN",
    })

    assert response.status_code == 403


def test_chat_rejects_a_session_remembered_case_the_user_is_not_assigned_to(api, session_id, user_id):
    """The client can omit case_id and let it fall back to the session's
    stored case_id — that fallback path must be checked too, not just the
    explicit-request-body path."""
    client, gateway = api
    gateway.cases["CASE-FORBIDDEN"] = {"case_id": "CASE-FORBIDDEN"}
    gateway.sessions[session_id] = {
        "session_id": session_id, "user_id": user_id, "project_id": None,
        "case_id": "CASE-FORBIDDEN", "title": "Existing",
    }
    gateway.denied_case_ids.add("CASE-FORBIDDEN")

    response = client.post("/api/chat", json={"session_id": session_id, "message": "hello"})

    assert response.status_code == 403


# Regression [bug fix, 2026-08-27 route sweep]: check_case_access() used to
# short-circuit to True for platform-admin WITHOUT ever confirming the case
# exists, so a stale/foreign/mistyped case_id sailed straight into
# create_session()'s INSERT and died on a foreign-key violation — an
# unhandled 500 with a full stack trace, for every role, before the router
# or any pipeline step ever ran. A nonexistent case_id must now 404 instead,
# checked BEFORE the access check (and unconditionally, including for
# platform-admin) so a bad id can never reach that far.

def test_chat_rejects_a_case_that_does_not_exist(api, session_id):
    client, gateway = api
    assert "CASE-DOES-NOT-EXIST" not in gateway.cases

    response = client.post("/api/chat", json={
        "session_id": session_id, "message": "tell me about this case",
        "case_id": "CASE-DOES-NOT-EXIST",
    })

    assert response.status_code == 404


def test_chat_rejects_a_nonexistent_case_even_for_platform_admin(api, session_id, user_id):
    """The existence check must not be skipped for platform-admin the way
    check_case_access() itself skips its own assignment check — otherwise
    an admin's stale/mistyped case_id still 500s."""
    from src.auth.routes import get_current_user

    client, gateway = api
    assert "CASE-DOES-NOT-EXIST" not in gateway.cases
    app.dependency_overrides[get_current_user] = lambda: _User(user_id, is_admin=True, role="platform-admin")

    response = client.post("/api/chat", json={
        "session_id": session_id, "message": "tell me about this case",
        "case_id": "CASE-DOES-NOT-EXIST",
    })

    assert response.status_code == 404


def test_chat_rejects_a_session_remembered_case_that_no_longer_exists(api, session_id, user_id):
    """Same existence check on the session-fallback path — a case deleted
    since the session was created must 404, not 500."""
    client, gateway = api
    gateway.sessions[session_id] = {
        "session_id": session_id, "user_id": user_id, "project_id": None,
        "case_id": "CASE-DELETED-SINCE", "title": "Existing",
    }
    assert "CASE-DELETED-SINCE" not in gateway.cases

    response = client.post("/api/chat", json={"session_id": session_id, "message": "hello"})

    assert response.status_code == 404


def test_chat_accepts_a_real_assigned_case(api, session_id, user_id, monkeypatch):
    """The existence check must not false-positive on a real, assigned
    case — this must still reach 200 and stream normally."""
    import src.main as main_mod

    async def _fake_process_query(*args, **kwargs):
        yield {"step": "response", "status": "done", "detail": "ok"}

    monkeypatch.setattr(main_mod, "process_query", _fake_process_query)

    client, gateway = api
    gateway.cases["CASE-REAL"] = {"case_id": "CASE-REAL"}
    gateway.case_assignments["CASE-REAL"] = [{"user_id": str(user_id), "role": "investigator"}]

    response = client.post("/api/chat", json={
        "session_id": session_id, "message": "hello",
        "case_id": "CASE-REAL",
    })

    assert response.status_code == 200


# ── Live-traffic cutover gating (AGENT_HARNESS_IMPLEMENTATION_PLAN.md §6) ──
# config.HARNESS_CUTOVER_ROUTES decides, per classified route, whether a
# chat request goes through orchestrator.py::process_query() (default,
# every route until deliberately added) or
# src.pipeline.harness.cutover.run_cutover_query(). Both are mocked at the
# src.main module level -- no real classification/LLM call in either test.

def test_chat_uses_orchestrator_by_default_not_the_harness(api, session_id, monkeypatch):
    import src.main as main_mod

    calls = {"orchestrator": 0, "cutover": 0}

    async def _fake_process_query(*args, **kwargs):
        calls["orchestrator"] += 1
        yield {"step": "response", "status": "done", "detail": "ok"}

    async def _fake_cutover(*args, **kwargs):
        calls["cutover"] += 1
        yield {"step": "response", "status": "done", "detail": "ok"}

    monkeypatch.setattr(main_mod, "process_query", _fake_process_query)
    monkeypatch.setattr(main_mod, "run_cutover_query", _fake_cutover)
    monkeypatch.setattr(main_mod.config, "HARNESS_CUTOVER_ROUTES", frozenset())  # explicit default

    client, _ = api
    response = client.post("/api/chat", json={"session_id": session_id, "message": "hello"})

    assert response.status_code == 200
    assert calls["orchestrator"] == 1
    assert calls["cutover"] == 0


def test_chat_routes_through_the_harness_when_its_route_is_cut_over(api, session_id, monkeypatch):
    import src.main as main_mod

    calls = {"orchestrator": 0, "cutover": 0}

    async def _fake_route_query(message):
        return {"route": "RAG", "output_format": "chat"}

    async def _fake_process_query(*args, **kwargs):
        calls["orchestrator"] += 1
        yield {"step": "response", "status": "done", "detail": "ok"}

    async def _fake_cutover(*args, **kwargs):
        calls["cutover"] += 1
        yield {"step": "response", "status": "done", "detail": "ok"}

    monkeypatch.setattr(main_mod, "route_query", _fake_route_query)
    monkeypatch.setattr(main_mod, "process_query", _fake_process_query)
    monkeypatch.setattr(main_mod, "run_cutover_query", _fake_cutover)
    monkeypatch.setattr(main_mod.config, "HARNESS_CUTOVER_ROUTES", frozenset({"RAG"}))

    client, _ = api
    response = client.post("/api/chat", json={"session_id": session_id, "message": "search for X"})

    assert response.status_code == 200
    assert calls["cutover"] == 1
    assert calls["orchestrator"] == 0


def test_chat_file_output_classification_excluded_from_cutover_even_if_route_matches(api, session_id, monkeypatch):
    """[PRESERVE] classify_to_subagent() overrides ANY route to Report
    Drafting when output_format is a file format -- Report Drafting is not
    part of this session's cutover slice, so a file-output classification
    must never reach run_cutover_query even when its underlying route
    string is in HARNESS_CUTOVER_ROUTES."""
    import src.main as main_mod

    calls = {"orchestrator": 0, "cutover": 0}

    async def _fake_route_query(message):
        return {"route": "RAG", "output_format": "file_pdf"}

    async def _fake_process_query(*args, **kwargs):
        calls["orchestrator"] += 1
        yield {"step": "response", "status": "done", "detail": "ok"}

    async def _fake_cutover(*args, **kwargs):
        calls["cutover"] += 1
        yield {"step": "response", "status": "done", "detail": "ok"}

    monkeypatch.setattr(main_mod, "route_query", _fake_route_query)
    monkeypatch.setattr(main_mod, "process_query", _fake_process_query)
    monkeypatch.setattr(main_mod, "run_cutover_query", _fake_cutover)
    monkeypatch.setattr(main_mod.config, "HARNESS_CUTOVER_ROUTES", frozenset({"RAG"}))

    client, _ = api
    response = client.post("/api/chat", json={"session_id": session_id, "message": "generate a PDF report"})

    assert response.status_code == 200
    assert calls["cutover"] == 0
    assert calls["orchestrator"] == 1


def test_health_is_public(api):
    client, _ = api
    assert client.get("/health").status_code == 200


def test_health_reports_degraded_when_postgres_unreachable(api, monkeypatch):
    """
    Regression, confirmed live: /health used to hardcode {"status": "ok"}
    and never probed Postgres at all. With Postgres genuinely down (Docker
    daemon stopped), every Postgres-backed call (register, login, ...) was
    returning 500 in the same moment /health kept reporting "ok" — a
    monitoring platform watching only `status` would never have caught it.

    The fake here is a real @asynccontextmanager (matching get_session()'s
    actual shape, used via `async with`) — not a plain async generator.
    2026-08-06: the original version of both this test and the "reachable"
    one below used a bare async-generator fake, which happens to satisfy
    `async for` but NOT `async with` — that mismatch is exactly why these
    tests never caught /health's own real bug (main.py used `async for
    session in get_session():` against the real, `@asynccontextmanager`-
    decorated get_session(), which raised on every single call — see the
    fix commit). A mock shaped differently from the real dependency's
    contract can pass while the production code it's meant to guard is
    already broken.
    """
    client, _ = api

    @asynccontextmanager
    async def _broken_get_session():
        raise RuntimeError("connection refused")
        yield  # pragma: no cover - makes this a valid generator body

    monkeypatch.setattr("src.database.postgres.get_session", _broken_get_session)

    resp = client.get("/health")
    assert resp.status_code == 200  # still alive, just degraded
    body = resp.json()
    assert body["status"] == "degraded"
    assert "error" in body["database_status"]


def test_health_reports_ok_when_postgres_reachable(api, monkeypatch):
    """See test_health_reports_degraded_when_postgres_unreachable's
    docstring for why this fake must be a real @asynccontextmanager, not a
    bare async generator."""
    class _FakeSession:
        async def execute(self, *args, **kwargs):
            return None

    @asynccontextmanager
    async def _working_get_session():
        yield _FakeSession()

    monkeypatch.setattr("src.database.postgres.get_session", _working_get_session)

    client, _ = api
    resp = client.get("/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "ok"
    assert body["database_status"] == "ok"


# ── Session ownership ─────────────────────────────────────────────────────────

def test_lists_only_the_callers_sessions(api, user_id, session_id):
    client, gateway = api
    gateway.sessions[session_id] = {
        "session_id": session_id, "user_id": user_id, "project_id": None, "title": "Mine",
    }
    other = str(uuid.uuid4())
    gateway.sessions[other] = {
        "session_id": other, "user_id": str(uuid.uuid4()), "project_id": None, "title": "Theirs",
    }

    response = client.get("/api/sessions")

    assert response.status_code == 200
    assert [s["title"] for s in response.json()] == ["Mine"]


def test_reads_own_session_history(api, user_id, session_id):
    client, gateway = api
    gateway.sessions[session_id] = {
        "session_id": session_id, "user_id": user_id, "project_id": None, "title": "T",
    }
    gateway.messages.append({"message_id": "m1", "session_id": session_id,
                             "role": "user", "content": "hello"})

    response = client.get(f"/api/sessions/{session_id}")

    assert response.status_code == 200
    assert response.json()["history"][0]["content"] == "hello"


@pytest.mark.parametrize("method,payload", [
    ("get", None),
    ("delete", None),
    ("patch", {"title": "hijacked"}),
])
def test_cannot_touch_another_users_session(api, session_id, method, payload):
    client, gateway = api
    gateway.sessions[session_id] = {
        "session_id": session_id, "user_id": str(uuid.uuid4()),  # someone else
        "project_id": None, "title": "Theirs",
    }

    request = getattr(client, method)
    path = f"/api/sessions/{session_id}"
    response = request(path, json=payload) if payload is not None else request(path)

    assert response.status_code == 403


def test_missing_session_is_404(api):
    client, _ = api
    assert client.get(f"/api/sessions/{uuid.uuid4()}").status_code == 404


# ── Chat attachment ownership (Phase 5, Module 5.3) ──────────────────────────
#
# Regression: upload_attachment took session_id from the client with zero
# ownership check, unlike list_attachments/delete_attachment in the same
# file — any authenticated user could attach a file (and inject its
# extracted text into the LLM prompt) into another user's session_id.
# These guard the fix, and the accompanying "deny by default on a missing
# owner" fix to list/delete's own (previously fail-open) checks.

@pytest.fixture(autouse=False)
def no_op_extract(monkeypatch):
    """Upload tests only care about the ownership gate, not real parsing."""
    async def _fake_extract(path):
        return "extracted text"
    monkeypatch.setattr("src.api.attachments._extract_text", _fake_extract)


def test_upload_attachment_to_another_users_session_is_forbidden(api, gateway, no_op_extract):
    client, gw = api
    other_session = str(uuid.uuid4())
    gw.sessions[other_session] = {
        "session_id": other_session, "user_id": str(uuid.uuid4()), "project_id": None, "title": "Theirs",
    }

    response = client.post(
        "/api/attachments",
        data={"session_id": other_session},
        files={"file": ("note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 403


def test_upload_attachment_to_a_brand_new_session_succeeds(api, gateway, no_op_extract):
    """
    The frontend generates session_id client-side before the conversation's
    first message is sent — a session_id with no `sessions` row yet is a
    legitimate brand-new conversation, not someone else's, and must not be
    blocked by the new ownership check.
    """
    client, gw = api
    new_session = str(uuid.uuid4())

    response = client.post(
        "/api/attachments",
        data={"session_id": new_session},
        files={"file": ("note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200


def test_upload_attachment_to_own_existing_session_succeeds(api, gateway, user_id, no_op_extract):
    client, gw = api
    own_session = str(uuid.uuid4())
    gw.sessions[own_session] = {
        "session_id": own_session, "user_id": user_id, "project_id": None, "title": "Mine",
    }

    response = client.post(
        "/api/attachments",
        data={"session_id": own_session},
        files={"file": ("note.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 200


def test_list_attachments_denies_a_session_with_no_owner(api, gateway):
    """
    Falsy-ownership fix: a session row with user_id=None must deny by
    default, not fail open just because there's no owner to compare
    against. No current code path creates such a row (grepped every
    create_session call site) — this guards the latent gap regardless.
    """
    client, gw = api
    ownerless_session = str(uuid.uuid4())
    gw.sessions[ownerless_session] = {
        "session_id": ownerless_session, "user_id": None, "project_id": None, "title": "Legacy",
    }

    response = client.get("/api/attachments", params={"session_id": ownerless_session})

    assert response.status_code == 403


def test_delete_attachment_denies_an_attachment_with_no_owner(api, gateway):
    client, gw = api
    attachment_id = str(uuid.uuid4())
    gw.attachments.append({
        "attachment_id": attachment_id, "session_id": str(uuid.uuid4()),
        "user_id": None, "filename": "x.txt", "status": "ready",
    })

    response = client.delete(f"/api/attachments/{attachment_id}")

    assert response.status_code == 403


# ── Downloads ─────────────────────────────────────────────────────────────────

@pytest.fixture
def generated_file(gateway, user_id, tmp_path):
    path = tmp_path / "export.xlsx"
    path.write_bytes(b"PK\x03\x04fake-xlsx")
    file_id = str(uuid.uuid4())
    gateway.files[file_id] = {
        "file_id": file_id, "session_id": str(uuid.uuid4()), "user_id": user_id,
        "file_type": "xlsx", "file_name": "Offense Section Card.xlsx",
        "file_size_bytes": path.stat().st_size, "storage_path": str(path),
    }
    return file_id


def test_download_sends_the_correct_mime_and_filename(api, generated_file):
    """Regression: everything was served as extensionless octet-stream."""
    client, _ = api

    response = client.get(f"/api/files/{generated_file}/download")

    assert response.status_code == 200
    assert response.headers["content-type"] == (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    )
    assert ".xlsx" in response.headers["content-disposition"]


def test_download_repairs_a_legacy_extensionless_filename(api, gateway, user_id, tmp_path):
    """Old rows stored the bare title; the response must still name the file usably."""
    path = tmp_path / "old.pdf"
    path.write_bytes(b"%PDF-1.4 fake")
    file_id = str(uuid.uuid4())
    gateway.files[file_id] = {
        "file_id": file_id, "session_id": str(uuid.uuid4()), "user_id": user_id,
        "file_type": "pdf", "file_name": "Offense Section Card",  # no extension
        "file_size_bytes": 13, "storage_path": str(path),
    }
    client, _ = api

    response = client.get(f"/api/files/{file_id}/download")

    assert ".pdf" in response.headers["content-disposition"]
    assert response.headers["content-type"] == "application/pdf"


def test_cannot_download_another_users_file(api, gateway, tmp_path):
    path = tmp_path / "theirs.pdf"
    path.write_bytes(b"%PDF-1.4")
    file_id = str(uuid.uuid4())
    gateway.files[file_id] = {
        "file_id": file_id, "session_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()),
        "file_type": "pdf", "file_name": "Theirs.pdf",
        "file_size_bytes": 8, "storage_path": str(path),
    }
    client, _ = api

    assert client.get(f"/api/files/{file_id}/download").status_code == 403


def test_admin_may_download_any_users_file(admin_api, gateway, tmp_path):
    """The admin panel lists every user's files — it must be able to fetch them."""
    path = tmp_path / "theirs.pdf"
    path.write_bytes(b"%PDF-1.4")
    file_id = str(uuid.uuid4())
    gateway.files[file_id] = {
        "file_id": file_id, "session_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()),
        "file_type": "pdf", "file_name": "Theirs.pdf",
        "file_size_bytes": 8, "storage_path": str(path),
    }
    client, _ = admin_api

    assert client.get(f"/api/files/{file_id}/download").status_code == 200


# ── Generated-file download case-scoping (Phase 5, Module 5.4) ──────────────
#
# Regression: station-admin got blanket cross-case access to every
# generated file, purely on global role — unlike everywhere else in the
# system, where station-admin does NOT get cross-case visibility (only
# platform-admin does). These guard the fix: a file with a real case_id is
# now scoped by case_assignments for station-admin; a NULL case_id (no
# backfill for pre-migration rows) keeps the old blanket access.

def test_station_admin_with_case_assignment_can_download(station_admin_api, gateway, user_id, tmp_path):
    client, gw = station_admin_api
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4")
    file_id = str(uuid.uuid4())
    gw.files[file_id] = {
        "file_id": file_id, "session_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()),
        "case_id": "CASE-001", "file_type": "pdf", "file_name": "report.pdf",
        "file_size_bytes": 8, "storage_path": str(path),
    }
    gw.case_assignments["CASE-001"] = [{"user_id": user_id, "email": "x@example.com", "role": "investigator"}]

    assert client.get(f"/api/files/{file_id}/download").status_code == 200


def test_station_admin_without_case_assignment_cannot_download(station_admin_api, gateway, tmp_path):
    client, gw = station_admin_api
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4")
    file_id = str(uuid.uuid4())
    gw.files[file_id] = {
        "file_id": file_id, "session_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()),
        "case_id": "CASE-001", "file_type": "pdf", "file_name": "report.pdf",
        "file_size_bytes": 8, "storage_path": str(path),
    }
    # No case_assignments entry for CASE-001 at all.

    assert client.get(f"/api/files/{file_id}/download").status_code == 403


def test_station_admin_can_download_a_file_with_no_case_id(station_admin_api, gateway, tmp_path):
    """Legacy/not-case-derived files (case_id IS NULL) keep the old blanket access."""
    client, gw = station_admin_api
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4")
    file_id = str(uuid.uuid4())
    gw.files[file_id] = {
        "file_id": file_id, "session_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()),
        "file_type": "pdf", "file_name": "report.pdf",
        "file_size_bytes": 8, "storage_path": str(path),
    }

    assert client.get(f"/api/files/{file_id}/download").status_code == 200


def test_platform_admin_downloads_case_scoped_file_regardless_of_assignment(admin_api, gateway, tmp_path):
    client, gw = admin_api
    path = tmp_path / "report.pdf"
    path.write_bytes(b"%PDF-1.4")
    file_id = str(uuid.uuid4())
    gw.files[file_id] = {
        "file_id": file_id, "session_id": str(uuid.uuid4()), "user_id": str(uuid.uuid4()),
        "case_id": "CASE-001", "file_type": "pdf", "file_name": "report.pdf",
        "file_size_bytes": 8, "storage_path": str(path),
    }
    # No case_assignments entry — platform-admin must not need one.

    assert client.get(f"/api/files/{file_id}/download").status_code == 200


def test_malformed_file_id_is_rejected(api):
    client, _ = api
    assert client.get("/api/files/not-a-uuid/download").status_code == 400


def test_download_of_a_vanished_file_is_404(api, gateway, user_id):
    file_id = str(uuid.uuid4())
    gateway.files[file_id] = {
        "file_id": file_id, "session_id": str(uuid.uuid4()), "user_id": user_id,
        "file_type": "pdf", "file_name": "Gone.pdf", "file_size_bytes": 1,
        "storage_path": "/nonexistent/gone.pdf",
    }
    client, _ = api

    assert client.get(f"/api/files/{file_id}/download").status_code == 404


# ── Admin ─────────────────────────────────────────────────────────────────────

def test_admin_delete_removes_record_and_file_from_disk(admin_api, gateway, user_id, tmp_path):
    """Regression: this 500'd — the direct backend returned only {file_id}."""
    path = tmp_path / "doomed.pdf"
    path.write_bytes(b"%PDF-1.4")
    file_id = str(uuid.uuid4())
    gateway.files[file_id] = {
        "file_id": file_id, "session_id": str(uuid.uuid4()), "user_id": user_id,
        "file_type": "pdf", "file_name": "Doomed.pdf", "file_size_bytes": 8,
        "storage_path": str(path),
    }
    client, _ = admin_api

    response = client.delete(f"/api/admin/files/{file_id}")

    assert response.status_code == 200
    assert file_id not in gateway.files
    assert not os.path.exists(path), "the file was left on disk"


def test_admin_delete_of_a_record_with_no_disk_file_still_succeeds(admin_api, gateway, user_id):
    file_id = str(uuid.uuid4())
    gateway.files[file_id] = {
        "file_id": file_id, "session_id": str(uuid.uuid4()), "user_id": user_id,
        "file_type": "pdf", "file_name": "X.pdf", "file_size_bytes": 1,
        "storage_path": "/nonexistent/x.pdf",
    }
    client, _ = admin_api

    assert client.delete(f"/api/admin/files/{file_id}").status_code == 200


def test_admin_metrics_include_the_fields_the_dashboard_renders(admin_api):
    """Regression: the direct backend omitted route_metrics/table_stats."""
    client, _ = admin_api

    body = client.get("/api/admin/metrics").json()

    assert "route_metrics" in body
    assert "table_stats" in body


# ── Phase 7, Module 7.2: KB upload validation ───────────────────────────────
#
# validate_file()'s own checks are unit-tested directly in
# tests/test_ingestion_validation.py. These two cover the endpoint wiring:
# a bad upload must be rejected synchronously (400), not silently accepted
# and only failed later in the background ingestion job — and the
# rejected file must not be left behind on disk.

def test_kb_upload_rejects_mismatched_magic_bytes_synchronously(admin_api, monkeypatch, tmp_path):
    client, _ = admin_api
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path)

    response = client.post(
        "/api/admin/kb/upload",
        files={"file": ("evidence.pdf", b"MZ\x90\x00 not actually a pdf", "application/pdf")},
    )

    assert response.status_code == 400
    assert not (tmp_path / "evidence.pdf").exists(), "a rejected upload must not be left on disk"


def test_kb_upload_accepts_a_correctly_signed_file(admin_api, monkeypatch, tmp_path):
    client, _ = admin_api
    monkeypatch.setattr(config, "DOCUMENTS_DIR", tmp_path)

    # The actual chunk/embed/graph pipeline (real Docling etc.) is out of
    # scope for this endpoint-validation test — only the upload/validation
    # response matters here, not the background ingestion outcome.
    import src.api.admin as admin_module

    async def _noop_ingest(path, job_id):
        pass

    monkeypatch.setattr(admin_module, "_ingest_uploaded_file", _noop_ingest)

    response = client.post(
        "/api/admin/kb/upload",
        files={"file": ("evidence.pdf", b"%PDF-1.4\nreal-looking content", "application/pdf")},
    )

    assert response.status_code == 200
    assert (tmp_path / "evidence.pdf").exists()
