"""
Tests for src/api/ingestion_quality_admin.py (Ingestion Quality Control
at Scale, Modules G1/G2).

get_session is monkeypatched with a fake session — no real Postgres.
`admin` dependencies are passed as plain objects directly, same style as
tests/test_graph_review.py / tests/test_community_admin.py.
"""
import pytest
from fastapi import HTTPException

import src.api.ingestion_quality_admin as iq_admin


class _Row:
    def __init__(self, d):
        self._mapping = d


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return [_Row(r) for r in self._rows]

    def fetchone(self):
        return _Row(self._rows[0]) if self._rows else None


class _FakeSession:
    def __init__(self, query_results: list):
        self._query_results = list(query_results)
        self.executed: list[tuple] = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        rows = self._query_results.pop(0) if self._query_results else []
        return _FakeResult(rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_get_session(session):
    def _factory():
        return session
    return _factory


class _Admin:
    def __init__(self, user_id="supervisor-1"):
        self.id = user_id


class FakeGateway:
    def __init__(self):
        self.audit_log = []

    async def log_audit_event(self, event_type, details=None, user_id=None, case_id=None):
        self.audit_log.append({"event_type": event_type, "details": details, "user_id": user_id})


@pytest.fixture
def fake_gateway(monkeypatch):
    gw = FakeGateway()

    async def _get_gateway():
        return gw

    monkeypatch.setattr(iq_admin, "get_gateway", _get_gateway)
    return gw


async def test_list_runs_returns_rows_newest_first(monkeypatch):
    rows = [{"run_id": "run-2", "source": "sync_muhafiz_data"}, {"run_id": "run-1", "source": "ingest_file"}]
    session = _FakeSession([rows])
    monkeypatch.setattr(iq_admin, "get_session", _fake_get_session(session))

    result = await iq_admin.list_runs(source=None, limit=50, admin=_Admin())

    assert result["count"] == 2
    assert result["runs"][0]["run_id"] == "run-2"


async def test_list_runs_clamps_limit(monkeypatch):
    session = _FakeSession([[]])
    monkeypatch.setattr(iq_admin, "get_session", _fake_get_session(session))

    await iq_admin.list_runs(source=None, limit=10_000, admin=_Admin())

    _, params = session.executed[0]
    assert params["limit"] == 200


async def test_list_flagged_returns_only_flagged_rows(monkeypatch):
    rows = [{"run_id": "run-3", "flagged_reason": "ambiguous-match rate spiked"}]
    session = _FakeSession([rows])
    monkeypatch.setattr(iq_admin, "get_session", _fake_get_session(session))

    result = await iq_admin.list_flagged(admin=_Admin())

    assert result["count"] == 1
    assert result["flagged"][0]["run_id"] == "run-3"


async def test_acknowledge_clears_the_flag_and_logs_audit(monkeypatch, fake_gateway):
    session = _FakeSession([[{"source": "sync_muhafiz_data", "flagged_for_review": True}], []])
    monkeypatch.setattr(iq_admin, "get_session", _fake_get_session(session))

    result = await iq_admin.acknowledge_run("run-1", admin=_Admin())

    assert result == {"run_id": "run-1", "acknowledged": True}
    update_calls = [c for c in session.executed if "UPDATE ingestion_run_quality" in c[0]]
    assert len(update_calls) == 1
    assert update_calls[0][1] == {"run_id": "run-1"}
    assert fake_gateway.audit_log[0]["event_type"] == "ingestion_quality_acknowledge"


async def test_acknowledge_404s_for_an_unknown_run(monkeypatch):
    session = _FakeSession([[]])
    monkeypatch.setattr(iq_admin, "get_session", _fake_get_session(session))

    with pytest.raises(HTTPException) as exc_info:
        await iq_admin.acknowledge_run("no-such-run", admin=_Admin())

    assert exc_info.value.status_code == 404


async def test_acknowledge_409s_when_not_currently_flagged(monkeypatch):
    session = _FakeSession([[{"source": "ingest_file", "flagged_for_review": False}]])
    monkeypatch.setattr(iq_admin, "get_session", _fake_get_session(session))

    with pytest.raises(HTTPException) as exc_info:
        await iq_admin.acknowledge_run("run-1", admin=_Admin())

    assert exc_info.value.status_code == 409
