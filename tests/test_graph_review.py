"""
Tests for src/api/graph_review.py (Phase 4.11 review queue).

age_client and versioning are monkeypatched with fakes — no real
Postgres/AGE (matches the `no_network` guard, conftest, autouse). The
full flow (list pending, confirm, the double-confirm 409, stats) was
verified live against a real AGE-enabled Postgres instance during
development, including the append-only-history bug this test suite
guards against (a double-confirm silently creating two "confirmed"
edges — fixed in versioning.py's write_edge and the pre-check here).
`admin` dependencies are passed as None directly — these tests call the
route functions, not through FastAPI's dependency-injected HTTP layer.
"""
import pytest
from fastapi import HTTPException

import src.api.graph_review as graph_review


class FakeAgeClient:
    def __init__(self):
        self.calls: list[dict] = []
        self.responses: list[list[dict]] = []

    def queue(self, response):
        self.responses.append(response)

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        self.calls.append({"cypher": cypher_query, "params": params or {}})
        if self.responses:
            return self.responses.pop(0)
        return []


class FakeVersioning:
    def __init__(self):
        self.edges_written: list[dict] = []
        self.next_id = 100

    async def write_edge(self, edge_label, from_label, from_match, to_label, to_match,
                          properties=None, *, source_doc_id, source_chunk_id=None,
                          confidence=1.0, supersedes_edge_id=None):
        self.next_id += 1
        record = {
            "edge_label": edge_label, "from_match": from_match, "to_match": to_match,
            "properties": properties or {}, "supersedes_edge_id": supersedes_edge_id,
            "id": self.next_id,
        }
        self.edges_written.append(record)
        return {"id": self.next_id, "label": edge_label, "properties": properties or {}}


@pytest.fixture
def fake_age(monkeypatch):
    client = FakeAgeClient()
    monkeypatch.setattr(graph_review, "age_client", client)
    return client


@pytest.fixture
def fake_versioning(monkeypatch):
    v = FakeVersioning()
    monkeypatch.setattr(graph_review, "versioning", v)
    return v


def _person(entity_id, name):
    return {"id": 1, "label": "Person", "properties": {"entity_id": entity_id, "canonical_name": name}}


def _same_as_edge(edge_id, tier="flagged_unverified", status="pending", superseded_by=None):
    return {
        "id": edge_id, "label": "SAME_AS",
        "properties": {
            "tier": tier, "status": status, "basis": "matched on near-identical name",
            "confidence": 0.85, "as_of": "2026-01-01T00:00:00+00:00",
            "source_doc_id": "DOC-1", "source_chunk_id": None,
            "superseded_by": superseded_by,
        },
    }


# ── list_pending ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_list_pending_shape(fake_age):
    fake_age.queue([{
        "a": _person("P-NEW", "عدنان قریشی وحید"),
        "r": _same_as_edge(1),
        "b": _person("P-006", "عدنان قریشی وحید"),
    }])

    result = await graph_review.list_pending(case_id=None, tier=None, admin=None)
    assert result["count"] == 1
    entry = result["pending"][0]
    assert entry["edge_id"] == 1
    assert entry["tier"] == "flagged_unverified"
    assert entry["mention"]["entity_id"] == "P-NEW"
    assert entry["candidate"]["entity_id"] == "P-006"
    assert "basis" in entry


@pytest.mark.asyncio
async def test_list_pending_tier_filter(fake_age):
    # execute_cypher returns a single list of rows, not per-row queues.
    fake_age.responses = [[
        {"a": _person("P-1", "X"), "r": _same_as_edge(1, tier="flagged_unverified"), "b": _person("P-2", "X")},
        {"a": _person("P-3", "Y"), "r": _same_as_edge(2, tier="human_review"), "b": _person("P-4", "Y")},
    ]]

    result = await graph_review.list_pending(case_id=None, tier="human_review", admin=None)
    assert result["count"] == 1
    assert result["pending"][0]["edge_id"] == 2


# ── confirm / reject ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_confirm_match_writes_superseding_confirmed_edge(fake_age, fake_versioning):
    fake_age.queue([{"a": _person("P-NEW", "X"), "r": _same_as_edge(1), "b": _person("P-EXISTING", "X")}])

    result = await graph_review.confirm_match(1, graph_review.ReviewAction(reviewed_by="inv1"), admin=None)

    assert result["status"] == "confirmed"
    write = fake_versioning.edges_written[0]
    assert write["edge_label"] == "SAME_AS"
    assert write["properties"]["status"] == "confirmed"
    assert write["properties"]["reviewed_by"] == "inv1"
    assert write["supersedes_edge_id"] == 1


@pytest.mark.asyncio
async def test_reject_match_writes_superseding_rejected_edge(fake_age, fake_versioning):
    fake_age.queue([{"a": _person("P-NEW", "X"), "r": _same_as_edge(1), "b": _person("P-EXISTING", "X")}])

    result = await graph_review.reject_match(1, graph_review.ReviewAction(reviewed_by="inv1"), admin=None)

    assert result["status"] == "rejected"
    assert fake_versioning.edges_written[0]["properties"]["status"] == "rejected"


@pytest.mark.asyncio
async def test_double_confirm_returns_409(fake_age, fake_versioning):
    # Already superseded (by some earlier review action) — must be
    # rejected before any write is attempted.
    fake_age.queue([{"a": _person("P-NEW", "X"), "r": _same_as_edge(1, superseded_by=42), "b": _person("P-EXISTING", "X")}])

    with pytest.raises(HTTPException) as exc_info:
        await graph_review.confirm_match(1, graph_review.ReviewAction(reviewed_by="inv2"), admin=None)
    assert exc_info.value.status_code == 409
    assert fake_versioning.edges_written == []  # no write attempted


@pytest.mark.asyncio
async def test_confirm_nonexistent_edge_returns_404(fake_age, fake_versioning):
    fake_age.queue([])  # _get_same_as_edge finds nothing

    with pytest.raises(HTTPException) as exc_info:
        await graph_review.confirm_match(999, graph_review.ReviewAction(reviewed_by="inv1"), admin=None)
    assert exc_info.value.status_code == 404


# ── stats ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_review_stats_counts_by_tier_and_status(fake_age):
    fake_age.responses = [[
        {"r": _same_as_edge(1, tier="flagged_unverified", status="pending")},
        {"r": _same_as_edge(2, tier="flagged_unverified", status="pending")},
        {"r": _same_as_edge(3, tier="human_review", status="pending")},
    ]]

    stats = await graph_review.review_stats(admin=None)
    assert stats["flagged_unverified"]["pending"] == 2
    assert stats["human_review"]["pending"] == 1
