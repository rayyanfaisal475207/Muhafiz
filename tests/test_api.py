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

import pytest
from fastapi.testclient import TestClient

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
    app.dependency_overrides[get_current_user] = lambda: _User(user_id, role="station-admin")
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
# station. These guard the fix, and its explicit NULL-police_station bridge
# (no backfill data exists yet, so a station-admin with police_station=None
# must keep working, not get locked out of every case).

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


def test_station_admin_with_no_station_backfilled_falls_back_to_unrestricted(api, user_id):
    """
    Bridge behavior: police_station IS NULL means "not yet backfilled", not
    "belongs to no station" — must not be locked out of every case.
    """
    client, gateway = api
    gateway.cases["CASE-001"] = {"case_id": "CASE-001", "police_station": "Kohsar"}
    target_id = str(uuid.uuid4())
    gateway.users[target_id] = {"id": target_id, "email": "investigator@example.com", "role": "investigator"}
    app.dependency_overrides[get_current_user] = lambda: _User(user_id, role="station-admin", police_station=None)

    response = client.post("/api/cases/CASE-001/assignments/", json={
        "email": "investigator@example.com", "role": "investigator",
    })

    assert response.status_code == 200


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
    gateway.sessions[session_id] = {
        "session_id": session_id, "user_id": user_id, "project_id": None,
        "case_id": "CASE-FORBIDDEN", "title": "Existing",
    }
    gateway.denied_case_ids.add("CASE-FORBIDDEN")

    response = client.post("/api/chat", json={"session_id": session_id, "message": "hello"})

    assert response.status_code == 403


def test_health_is_public(api):
    client, _ = api
    assert client.get("/health").status_code == 200


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
