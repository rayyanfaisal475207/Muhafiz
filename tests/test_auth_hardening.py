"""
Phase 1, Module 1.3 — Auth/registration hardening.

Guards:
  * UserCreate.password enforces a 12-char minimum (no complexity rules —
    length is the one lever available on a platform with no MFA).
  * CORS_ORIGINS is environment-configurable, not a hardcoded Python list.
  * /api/chat, KB upload, attachment upload, case creation, and case
    assignment now carry a rate limit, matching the pattern already used
    by /api/auth/register and /api/auth/login.

Rate limits are asserted by introspecting slowapi's registered per-route
limit (limiter._route_limits), not by firing enough requests to actually
trip a 429 — the underlying limiter storage is a module-level singleton
shared across the whole test session, so deliberately exhausting it here
would leave that key poisoned for every other test that happens to hit
the same endpoint from the same TestClient IP afterward.
"""
import importlib

import pytest
from pydantic import ValidationError

import src.config as config
from src.auth.routes import UserCreate, limiter


# ── Password minimum length ──────────────────────────────────────────────────

@pytest.mark.parametrize("password", ["", "a", "eleven-cha1", "1234567890a"])  # all < 12 chars
def test_passwords_under_boundary_are_rejected(password):
    assert len(password) < 12
    with pytest.raises(ValidationError):
        UserCreate(email="a@example.com", password=password)


@pytest.mark.parametrize("password", ["twelve-chars", "a-real-passphrase-1234"])
def test_passwords_at_or_over_boundary_are_accepted(password):
    assert len(password) >= 12
    user = UserCreate(email="a@example.com", password=password)
    assert user.password == password


# ── CORS_ORIGINS environment-configurability ─────────────────────────────────

def test_cors_origins_defaults_to_known_localhost_ports(monkeypatch):
    monkeypatch.delenv("CORS_ORIGINS", raising=False)
    importlib.reload(config)
    try:
        assert "http://localhost:5173" in config.CORS_ORIGINS
        assert isinstance(config.CORS_ORIGINS, list)
    finally:
        importlib.reload(config)  # restore real env-derived state for later tests


def test_cors_origins_reads_from_environment(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", "https://example.com,https://admin.example.com")
    importlib.reload(config)
    try:
        assert config.CORS_ORIGINS == ["https://example.com", "https://admin.example.com"]
    finally:
        monkeypatch.delenv("CORS_ORIGINS", raising=False)
        importlib.reload(config)


# ── Rate limiting coverage ────────────────────────────────────────────────────

@pytest.mark.parametrize("qualified_name,expected", [
    ("src.main.chat_endpoint", "60 per 1 minute"),
    ("src.api.admin.upload_kb_document", "10 per 1 minute"),
    ("src.api.attachments.upload_attachment", "20 per 1 minute"),
    ("src.api.cases.create_case", "20 per 1 minute"),
    ("src.api.case_assignments.assign_user", "20 per 1 minute"),
    ("src.api.case_assignments.unassign_user", "20 per 1 minute"),
])
def test_endpoint_has_a_registered_rate_limit(qualified_name, expected):
    import src.main  # noqa: F401 — ensures every router module has been imported and decorated

    limits = limiter._route_limits.get(qualified_name)
    assert limits, f"{qualified_name} has no rate limit registered"
    assert str(limits[0].limit) == expected
