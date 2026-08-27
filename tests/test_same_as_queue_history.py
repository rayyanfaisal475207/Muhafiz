"""
Tests for src/graph/same_as_queue_history.py (GRAPH_QUALITY_VISIBILITY_
FIX_PROMPT.md, Feature A).

age_client and get_session are both monkeypatched — no real AGE/Postgres
(matches the `no_network` guard, conftest, autouse), same pattern as
tests/test_ingestion_quality.py (get_session) and
tests/test_entity_resolution.py (age_client).
"""
import pytest

import src.graph.same_as_queue_history as history


class FakeAgeClient:
    def __init__(self, rows):
        self._rows = rows
        self.calls: list[dict] = []

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        self.calls.append({"cypher": cypher_query, "params": params or {}})
        return self._rows


class _FakeResultRow:
    def __init__(self, mapping):
        self._mapping = mapping


class _FakeSession:
    def __init__(self, select_rows=None):
        self.executed: list[tuple] = []
        self._select_rows = select_rows or []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return [_FakeResultRow(r) for r in self._select_rows]

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


def _edge(tier, status, a_case_ids=(), b_case_ids=()):
    return {
        "r": {"properties": {"tier": tier, "status": status}},
        "a_case_ids": list(a_case_ids),
        "b_case_ids": list(b_case_ids),
    }


# ── _aggregate ───────────────────────────────────────────────────────────

def test_aggregate_produces_a_global_rollup_row():
    rows = [_edge("flagged_unverified", "pending", ["CASE-1"], ["CASE-1"])]
    by_scope = history._aggregate(rows)
    assert by_scope[None] == {("flagged_unverified", "pending"): 1}


def test_aggregate_counts_toward_both_endpoints_different_cases():
    """A cross-case pair (the P-006 shape) must count toward BOTH case
    rows, not neither and not an arbitrary pick of one."""
    rows = [_edge("flagged_unverified", "pending", ["CASE-1"], ["CASE-2"])]
    by_scope = history._aggregate(rows)
    assert by_scope["CASE-1"] == {("flagged_unverified", "pending"): 1}
    assert by_scope["CASE-2"] == {("flagged_unverified", "pending"): 1}
    assert by_scope[None] == {("flagged_unverified", "pending"): 1}


def test_aggregate_same_case_both_sides_counts_once_not_twice():
    rows = [_edge("flagged_unverified", "pending", ["CASE-1"], ["CASE-1"])]
    by_scope = history._aggregate(rows)
    assert by_scope["CASE-1"] == {("flagged_unverified", "pending"): 1}


def test_aggregate_missing_case_on_both_sides_only_populates_global():
    rows = [_edge("human_review", "pending", [], [])]
    by_scope = history._aggregate(rows)
    assert by_scope[None] == {("human_review", "pending"): 1}
    assert list(by_scope.keys()) == [None]


def test_aggregate_unknown_tier_and_status_default_to_unknown():
    rows = [{"r": {"properties": {}}, "a_case_ids": [], "b_case_ids": []}]
    by_scope = history._aggregate(rows)
    assert by_scope[None] == {("unknown", "unknown"): 1}


def test_aggregate_does_not_multiply_when_an_endpoint_has_many_redundant_case_edges():
    """Confirmed live: a Person node can carry many (measured: 131)
    separate, non-superseded BELONGS_TO_CASE edges to the SAME case — a
    leftover from the pre-d5fa333 replay bug. _fetch_edges() collapses
    these to a DISTINCT list before this function ever sees them, so one
    edge whose endpoint lists the same case_id many times must still
    count as exactly ONE occurrence, not one per repeat."""
    rows = [_edge("flagged_unverified", "confirmed", ["fir-1001-26"] * 131, ["fir-1001-26"] * 131)]
    by_scope = history._aggregate(rows)
    assert by_scope["fir-1001-26"] == {("flagged_unverified", "confirmed"): 1}
    assert by_scope[None] == {("flagged_unverified", "confirmed"): 1}


# ── write_snapshot ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_snapshot_inserts_one_row_per_case_tier_status(monkeypatch):
    age = FakeAgeClient([_edge("flagged_unverified", "pending", ["CASE-1"], ["CASE-1"])])
    monkeypatch.setattr(history, "age_client", age)
    session = _FakeSession()
    monkeypatch.setattr(history, "get_session", _fake_get_session(session))

    written = await history.write_snapshot()

    assert written == 2  # one global row + one CASE-1 row
    assert len(session.executed) == 1  # single batched INSERT
    stmt, params = session.executed[0]
    assert "INSERT INTO same_as_queue_snapshot" in stmt
    case_ids = {p["case_id"] for p in params}
    assert case_ids == {None, "CASE-1"}


@pytest.mark.asyncio
async def test_write_snapshot_with_no_edges_writes_nothing(monkeypatch):
    age = FakeAgeClient([])
    monkeypatch.setattr(history, "age_client", age)
    session = _FakeSession()
    monkeypatch.setattr(history, "get_session", _fake_get_session(session))

    written = await history.write_snapshot()

    assert written == 0
    assert session.executed == []


# ── read_history ─────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_history_defaults_to_global_scope(monkeypatch):
    session = _FakeSession(select_rows=[
        {"snapshot_at": "2026-08-27T00:00:00+00:00", "case_id": None,
         "tier": "flagged_unverified", "status": "pending", "edge_count": 5},
    ])
    monkeypatch.setattr(history, "get_session", _fake_get_session(session))

    rows = await history.read_history()

    assert rows[0]["edge_count"] == 5
    stmt, params = session.executed[0]
    assert params["case_id"] is None
    assert params["days"] == 30


@pytest.mark.asyncio
async def test_read_history_scopes_to_a_case_when_given(monkeypatch):
    session = _FakeSession(select_rows=[])
    monkeypatch.setattr(history, "get_session", _fake_get_session(session))

    await history.read_history(case_id="CASE-1", days=7)

    stmt, params = session.executed[0]
    assert params["case_id"] == "CASE-1"
    assert params["days"] == 7
    assert "IS NOT DISTINCT FROM" in stmt  # NULL-safe case_id comparison
