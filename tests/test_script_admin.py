"""
Tests for scripts/_script_admin.py — the guard that stops a mutation
script from acting under an invented identity.

This exists because of a real, measured failure: a locally-minted
uuid.uuid4() acting admin meant 103 SAME_AS confirmations landed in the
live graph with ZERO audit records (audit_logs.user_id has a foreign key
to users, so every write raised and was swallowed), and the fake id was
stamped onto every confirmed edge as `reviewed_by`. Each test below
names the specific way that must not be allowed to happen again.
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from scripts._script_admin import AdminIdentityError, ScriptAdmin, resolve_admin


class _FakeGateway:
    def __init__(self, user=None):
        self._user = user
        self.looked_up = []

    async def get_user_by_email(self, email):
        self.looked_up.append(email)
        return self._user


def _install(monkeypatch, gateway):
    async def _get_gateway():
        return gateway
    import src.data_gateway.selector as selector
    monkeypatch.setattr(selector, "get_gateway", _get_gateway)


REAL_ADMIN = {
    "id": "a71bca6f-6ebb-46ed-a7d5-1fe4b5bf4cee",
    "email": "admin@example.com",
    "role": "platform-admin",
}


@pytest.mark.asyncio
async def test_resolves_a_real_platform_admin(monkeypatch):
    _install(monkeypatch, _FakeGateway(REAL_ADMIN))
    admin = await resolve_admin("admin@example.com")
    assert isinstance(admin, ScriptAdmin)
    assert admin.id == uuid.UUID(REAL_ADMIN["id"])
    assert admin.email == "admin@example.com"
    assert admin.role == "platform-admin"


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", [None, "", "   "])
async def test_missing_email_is_refused_not_defaulted(monkeypatch, missing):
    """The original bug in one line: no identity supplied must NOT become
    'invent one' or 'pick the first admin you find'. An audit record
    naming the wrong person is worse than a loud failure."""
    gateway = _FakeGateway(REAL_ADMIN)
    _install(monkeypatch, gateway)
    with pytest.raises(AdminIdentityError, match="No admin identity supplied"):
        await resolve_admin(missing)
    assert gateway.looked_up == [], "must not go looking for a substitute identity"


@pytest.mark.asyncio
async def test_unknown_email_is_refused(monkeypatch):
    """A script must never create or assume an acting user."""
    _install(monkeypatch, _FakeGateway(None))
    with pytest.raises(AdminIdentityError, match="No user found"):
        await resolve_admin("ghost@example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["investigator", "", None, "viewer"])
async def test_insufficient_role_is_refused(monkeypatch, role):
    """graph_review's endpoints sit behind require_role("supervisor").
    A script acting outside the HTTP layer must enforce the same bar or
    it becomes a way to bypass that gate entirely."""
    _install(monkeypatch, _FakeGateway({**REAL_ADMIN, "role": role}))
    with pytest.raises(AdminIdentityError, match="cannot review graph matches"):
        await resolve_admin("someone@example.com")


@pytest.mark.asyncio
@pytest.mark.parametrize("role", ["platform-admin", "supervisor", "station-admin"])
async def test_supervisor_and_above_are_accepted(monkeypatch, role):
    _install(monkeypatch, _FakeGateway({**REAL_ADMIN, "role": role}))
    admin = await resolve_admin("someone@example.com")
    assert admin.role == role


@pytest.mark.asyncio
async def test_unusable_id_is_refused(monkeypatch):
    """The id must be a real UUID — confirm_match()'s audit write casts it
    to ::UUID, so a non-UUID reproduces the original swallowed failure."""
    _install(monkeypatch, _FakeGateway({**REAL_ADMIN, "id": "not-a-uuid"}))
    with pytest.raises(AdminIdentityError, match="unusable id"):
        await resolve_admin("admin@example.com")


@pytest.mark.asyncio
async def test_email_is_trimmed_before_lookup(monkeypatch):
    gateway = _FakeGateway(REAL_ADMIN)
    _install(monkeypatch, gateway)
    await resolve_admin("  admin@example.com  ")
    assert gateway.looked_up == ["admin@example.com"]
