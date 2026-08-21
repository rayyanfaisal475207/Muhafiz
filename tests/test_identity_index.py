"""
Tests for src/graph/identity_index.py (Graph Scale & Schema Expansion,
Milestone A1).

get_session is monkeypatched with a fake session — no real Postgres
(matches the `no_network` guard, conftest, autouse). Live behavior
(the real upsert/lookup round trip against Postgres, and the identity
lookup latency improvement) was verified against a real Postgres instance
during development — see docs/decisions/0002-graph-schema-expansion-and-
scale.md.
"""
import pytest

import src.graph.identity_index as identity_index


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def first(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, result_rows=None, raise_on_execute=False):
        self.executed: list[tuple] = []
        self._result_rows = result_rows or []
        self._raise = raise_on_execute

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        if self._raise:
            raise RuntimeError("simulated Postgres failure")
        return _FakeResult(self._result_rows)

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_get_session(session):
    def _factory():
        return session
    return _factory


# ── maintain() ──────────────────────────────────────────────────────────

async def test_maintain_skips_labels_not_in_identity_keys(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(identity_index, "get_session", _fake_get_session(session))

    await identity_index.maintain("Organization", "O-1", {"canonical_name": "X"})

    assert session.executed == []


async def test_maintain_skips_when_id_value_absent(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(identity_index, "get_session", _fake_get_session(session))

    await identity_index.maintain("Person", "P-1", {"canonical_name": "Zafar Iqbal"})

    assert session.executed == []


async def test_maintain_upserts_person_cnic(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(identity_index, "get_session", _fake_get_session(session))

    await identity_index.maintain("Person", "P-1", {"cnic": "00000-1234567-8", "canonical_name": "X"})

    assert len(session.executed) == 1
    sql, params = session.executed[0]
    assert "INSERT INTO identity_index" in sql
    assert "ON CONFLICT" in sql
    assert params == {
        "label": "Person", "id_key": "cnic", "id_value": "00000-1234567-8", "entity_id": "P-1",
    }


async def test_maintain_upserts_vehicle_plate(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(identity_index, "get_session", _fake_get_session(session))

    await identity_index.maintain("Vehicle", "V-1", {"plate": "ICT-273", "canonical_name": "ICT-273"})

    _, params = session.executed[0]
    assert params["id_key"] == "plate"
    assert params["id_value"] == "ICT-273"


async def test_maintain_upserts_officer_belt_no(monkeypatch):
    """Milestone B2 — Officer/belt_no gets the identical maintenance path
    Person/cnic and Vehicle/plate already get."""
    session = _FakeSession()
    monkeypatch.setattr(identity_index, "get_session", _fake_get_session(session))

    await identity_index.maintain("Officer", "OFFICER-1", {"belt_no": "HYD-3345", "canonical_name": "طارق جمالی"})

    _, params = session.executed[0]
    assert params["label"] == "Officer"
    assert params["id_key"] == "belt_no"
    assert params["id_value"] == "HYD-3345"
    assert params["entity_id"] == "OFFICER-1"


async def test_maintain_swallows_db_failure_without_raising(monkeypatch):
    session = _FakeSession(raise_on_execute=True)
    monkeypatch.setattr(identity_index, "get_session", _fake_get_session(session))

    # Must not raise — a graph write must never fail because the identity
    # index's side write failed.
    await identity_index.maintain("Person", "P-1", {"cnic": "00000-1234567-8"})


# ── lookup() ─────────────────────────────────────────────────────────────

async def test_lookup_hit_returns_entity_id(monkeypatch):
    session = _FakeSession(result_rows=[("P-1",)])
    monkeypatch.setattr(identity_index, "get_session", _fake_get_session(session))

    result = await identity_index.lookup("Person", "cnic", "00000-1234567-8")

    assert result == "P-1"


async def test_lookup_miss_returns_none(monkeypatch):
    session = _FakeSession(result_rows=[])
    monkeypatch.setattr(identity_index, "get_session", _fake_get_session(session))

    result = await identity_index.lookup("Person", "cnic", "00000-9999999-9")

    assert result is None


async def test_lookup_failure_returns_none_not_raise(monkeypatch):
    session = _FakeSession(raise_on_execute=True)
    monkeypatch.setattr(identity_index, "get_session", _fake_get_session(session))

    result = await identity_index.lookup("Person", "cnic", "00000-1234567-8")

    assert result is None


# ── entity_ids_excluding() ────────────────────────────────────────────────

async def test_entity_ids_excluding_returns_other_indexed_entities(monkeypatch):
    session = _FakeSession(result_rows=[("P-2",), ("P-3",)])
    monkeypatch.setattr(identity_index, "get_session", _fake_get_session(session))

    result = await identity_index.entity_ids_excluding("Person", "cnic", "00000-1234567-8")

    assert result == ["P-2", "P-3"]
    _, params = session.executed[0]
    assert params["exclude_id_value"] == "00000-1234567-8"


async def test_entity_ids_excluding_failure_returns_empty_list(monkeypatch):
    session = _FakeSession(raise_on_execute=True)
    monkeypatch.setattr(identity_index, "get_session", _fake_get_session(session))

    result = await identity_index.entity_ids_excluding("Person", "cnic", "00000-1234567-8")

    assert result == []
