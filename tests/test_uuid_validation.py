"""
Unit + integration tests for audit finding F-06: unvalidated identifiers
reaching a UUID cast and surfacing as an unhandled 500 instead of a clean
4xx. Covers src/api/validation.py::validate_uuid_field() directly, plus the
two real-world entry points the audit reproduced live: a validly-signed JWT
with a non-UUID `sub` claim (src/auth/jwt.py), and a malformed session_id
on the chat endpoint (src/main.py).
"""
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from src.api.validation import validate_uuid_field
from src.auth.jwt import create_access_token
from src.main import app


# ── validate_uuid_field() unit tests ─────────────────────────────────────────

def test_validate_uuid_field_accepts_a_real_uuid():
    value = "12345678-1234-5678-1234-567812345678"
    assert validate_uuid_field(value, "session_id") == value


@pytest.mark.parametrize("bad_value", ["not-a-uuid", "", "12345", None, 123])
def test_validate_uuid_field_rejects_malformed_input(bad_value):
    with pytest.raises(HTTPException) as exc_info:
        validate_uuid_field(bad_value, "session_id")
    assert exc_info.value.status_code == 422
    assert "session_id" in exc_info.value.detail


# ── JWT `sub` claim: non-UUID → 401, not 500 ────────────────────────────────

def test_valid_signature_non_uuid_sub_returns_401_not_500():
    """
    F-06: UUID(user_id_str) in get_current_user() used to raise ValueError
    outside the `except JWTError` net, escaping as an unhandled 500. This
    still requires a valid signature (JWT_SECRET_KEY), so it was never an
    auth bypass — just a wrong status code / unhandled crash.
    """
    client = TestClient(app)
    token = create_access_token({"sub": "this-is-not-a-uuid"})
    client.cookies.set("access_token", token)

    response = client.get("/api/auth/me")

    assert response.status_code == 401


# ── Chat endpoint: malformed session_id → 422, not 500 ──────────────────────

def test_chat_endpoint_rejects_malformed_session_id(monkeypatch):
    from src.auth.routes import get_current_user
    import uuid as uuid_module

    class _User:
        def __init__(self):
            self.id = uuid_module.uuid4()
            self.role = "investigator"
            self.email = "test@example.com"

    app.dependency_overrides[get_current_user] = lambda: _User()
    try:
        client = TestClient(app)
        response = client.post("/api/chat", json={
            "session_id": "not-a-real-session-id",
            "message": "hello",
        })
        assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()
