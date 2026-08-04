"""
Tests for src/graph/versioning.py (Phase 4.9).

age_client.execute_cypher is monkeypatched with a scriptable fake — no
real Postgres/AGE connection (matches the `no_network` guard, conftest,
autouse). The live end-to-end behavior (MERGE idempotency, supersede
chains, lock/unlock refusing a write) was verified against a real
AGE-enabled Postgres instance during development; these tests guard
versioning.py's own call-building logic — SQL/param shape, the
locked-edge refusal branch, the missing-endpoint branch — against
regressions.
"""
import pytest

import src.graph.versioning as versioning


class FakeAgeClient:
    """Records every execute_cypher() call and returns scripted responses in order."""

    def __init__(self):
        self.calls: list[dict] = []
        self.responses: list[list[dict]] = []

    def queue(self, response: list[dict]):
        self.responses.append(response)

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        self.calls.append({"cypher": cypher_query, "params": params or {}, "columns": columns})
        if self.responses:
            return self.responses.pop(0)
        return []


@pytest.fixture
def fake_client(monkeypatch):
    client = FakeAgeClient()
    monkeypatch.setattr(versioning, "age_client", client)
    return client


# ── write_node ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_node_builds_merge_with_individual_set_assignments(fake_client):
    fake_client.queue([{"n": {"id": 1, "label": "Person", "properties": {"entity_id": "P-1", "cnic": "00000-1234567-8"}}}])

    result = await versioning.write_node(
        "Person", {"entity_id": "P-1"}, {"cnic": "00000-1234567-8", "canonical_name": "X"},
        source_doc_id="DOC-1",
    )

    call = fake_client.calls[0]
    # += with a parameter reference doesn't work on this AGE version — the
    # regression this test guards against is silently reverting to it.
    assert "+=" not in call["cypher"]
    assert "n.cnic = $p_cnic" in call["cypher"]
    assert "n.canonical_name = $p_canonical_name" in call["cypher"]
    assert call["params"]["p_cnic"] == "00000-1234567-8"
    assert call["params"]["m_entity_id"] == "P-1"
    assert result["properties"]["entity_id"] == "P-1"


@pytest.mark.asyncio
async def test_write_node_rejects_malformed_property_key(fake_client):
    with pytest.raises(ValueError):
        await versioning.write_node(
            "Person", {"entity_id": "P-1"}, {"bad key; DROP": "x"}, source_doc_id="DOC-1"
        )
    assert fake_client.calls == []  # rejected before any query was sent


@pytest.mark.asyncio
async def test_write_node_with_no_properties(fake_client):
    fake_client.queue([{"n": {"id": 1, "label": "Person", "properties": {}}}])
    await versioning.write_node("Person", {"entity_id": "P-1"}, source_doc_id="DOC-1")
    call = fake_client.calls[0]
    assert "+=" not in call["cypher"]


# ── write_edge ──────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_edge_basic(fake_client):
    fake_client.queue([{"r": {"id": 10, "label": "ASSOCIATED_WITH", "properties": {"basis": "shared phone"}}}])

    result = await versioning.write_edge(
        "ASSOCIATED_WITH", "Person", {"entity_id": "P-1"}, "Person", {"entity_id": "P-2"},
        {"basis": "shared phone"}, source_doc_id="DOC-1", confidence=0.6,
    )

    call = fake_client.calls[0]
    assert "MATCH (a:Person {entity_id: $a_entity_id})" in call["cypher"]
    assert "MATCH (b:Person {entity_id: $b_entity_id})" in call["cypher"]
    assert "CREATE (a)-[r:ASSOCIATED_WITH]->(b)" in call["cypher"]
    assert call["params"]["a_entity_id"] == "P-1"
    assert call["params"]["b_entity_id"] == "P-2"
    assert result["id"] == 10


@pytest.mark.asyncio
async def test_write_edge_missing_endpoint_returns_none(fake_client):
    fake_client.queue([])  # MATCH found nothing
    result = await versioning.write_edge(
        "ASSOCIATED_WITH", "Person", {"entity_id": "NOPE"}, "Person", {"entity_id": "P-2"},
        {}, source_doc_id="DOC-1",
    )
    assert result is None


@pytest.mark.asyncio
async def test_write_edge_supersede_marks_old_edge(fake_client):
    # get_edge() call for the prior edge (not locked)
    fake_client.queue([{"r": {"id": 5, "label": "ASSOCIATED_WITH", "properties": {}}}])
    # the new edge write
    fake_client.queue([{"r": {"id": 6, "label": "ASSOCIATED_WITH", "properties": {}}}])
    # the supersede-marking write on the old edge
    fake_client.queue([{"old": {"id": 5, "properties": {"superseded_by": 6}}}])

    result = await versioning.write_edge(
        "ASSOCIATED_WITH", "Person", {"entity_id": "P-1"}, "Person", {"entity_id": "P-2"},
        {}, source_doc_id="DOC-2", supersedes_edge_id=5,
    )

    assert result["id"] == 6
    assert len(fake_client.calls) == 3
    supersede_call = fake_client.calls[2]
    assert supersede_call["params"] == {"old_id": 5, "new_id": 6}


@pytest.mark.asyncio
async def test_write_edge_conflicts_with_supersede(fake_client):
    # get_edge() call for the prior edge (not locked)
    fake_client.queue([{"r": {"id": 100, "label": "CONFLICTS_WITH", "properties": {}}}])
    # the new edge write
    fake_client.queue([{"r": {"id": 101, "label": "CONFLICTS_WITH", "properties": {}}}])
    # the supersede-marking write on the old edge
    fake_client.queue([{"old": {"id": 100, "properties": {"superseded_by": 101}}}])

    result = await versioning.write_edge(
        "CONFLICTS_WITH", "Incident", {"entity_id": "I-1"}, "Incident", {"entity_id": "I-2"},
        {"basis": "new basis"}, source_doc_id="DOC-2", supersedes_edge_id=100,
    )

    assert result["id"] == 101
    assert len(fake_client.calls) == 3
    supersede_call = fake_client.calls[2]
    assert supersede_call["params"] == {"old_id": 100, "new_id": 101}


@pytest.mark.asyncio
async def test_write_edge_refuses_to_supersede_already_superseded_edge(fake_client):
    # A version chain is linear — a second write claiming to supersede an
    # edge that was ALREADY superseded (by some other edge) must be
    # refused, not allowed to fork the history. This is the guard behind
    # the review queue's double-confirm 409.
    fake_client.queue([{"r": {"id": 5, "properties": {"superseded_by": 6}}}])
    result = await versioning.write_edge(
        "SAME_AS", "Person", {"entity_id": "P-1"}, "Person", {"entity_id": "P-2"},
        {}, source_doc_id="DOC-2", supersedes_edge_id=5,
    )
    assert result is None
    assert len(fake_client.calls) == 1  # only the get_edge() lookup, no write attempted


@pytest.mark.asyncio
async def test_write_edge_refuses_to_supersede_nonexistent_edge(fake_client):
    fake_client.queue([])  # get_edge() finds nothing
    result = await versioning.write_edge(
        "ASSOCIATED_WITH", "Person", {"entity_id": "P-1"}, "Person", {"entity_id": "P-2"},
        {}, source_doc_id="DOC-2", supersedes_edge_id=999,
    )
    assert result is None
    assert len(fake_client.calls) == 1  # only the get_edge() lookup, no write attempted


@pytest.mark.asyncio
async def test_write_edge_refuses_to_supersede_locked_edge(fake_client):
    fake_client.queue([{"r": {"id": 7, "label": "OCCURRED_ON", "properties": {"locked": True}}}])
    result = await versioning.write_edge(
        "OCCURRED_ON", "Incident", {"incident_id": "I-1"}, "Person", {"entity_id": "P-1"},
        {"date": "2026-02-01"}, source_doc_id="DOC-2", supersedes_edge_id=7,
    )
    assert result is None
    assert len(fake_client.calls) == 1  # refused before any write was attempted


# ── lock_event / unlock_event ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_lock_event_sets_locked_true(fake_client):
    fake_client.queue([{"r": {"id": 7, "properties": {"locked": True}}}])
    ok = await versioning.lock_event(7, locked_by="investigator1")
    assert ok is True
    call = fake_client.calls[0]
    assert "r.locked = true" in call["cypher"]
    assert call["params"]["locked_by"] == "investigator1"


@pytest.mark.asyncio
async def test_unlock_event_sets_locked_false(fake_client):
    fake_client.queue([{"r": {"id": 7, "properties": {"locked": False}}}])
    ok = await versioning.unlock_event(7, unlocked_by="investigator1")
    assert ok is True
    call = fake_client.calls[0]
    assert "r.locked = false" in call["cypher"]


@pytest.mark.asyncio
async def test_lock_event_on_nonexistent_edge_returns_false(fake_client):
    fake_client.queue([])
    ok = await versioning.lock_event(999, locked_by="investigator1")
    assert ok is False


# ── A-3: match-clause key validation ────────────────────────────────────
# _build_set_clause already validated property keys against _VALID_KEY_RE
# before Cypher interpolation; _match_clause/_prefixed_match_clause didn't.
# No live exploit path exists (every current caller passes hardcoded
# literal keys), but this closes the inconsistency defensively.

def test_match_clause_accepts_normal_keys():
    clause = versioning._match_clause("n", {"entity_id": "P-001", "case_id": "CASE-001"})
    assert clause == " {entity_id: $m_entity_id, case_id: $m_case_id}"


def test_match_clause_rejects_malicious_key():
    with pytest.raises(ValueError, match="Invalid match key"):
        versioning._match_clause("n", {"entity_id}) DETACH DELETE (n": "x"})


def test_match_clause_empty_dict_is_a_noop():
    assert versioning._match_clause("n", {}) == ""


def test_prefixed_match_clause_accepts_normal_keys():
    clause = versioning._prefixed_match_clause({"entity_id": "P-001"}, "a")
    assert clause == " {entity_id: $a_entity_id}"


def test_prefixed_match_clause_rejects_malicious_key():
    with pytest.raises(ValueError, match="Invalid match key"):
        versioning._prefixed_match_clause({"x); MATCH (n) DETACH DELETE (n": "y"}, "a")


def test_prefixed_match_clause_empty_dict_is_a_noop():
    assert versioning._prefixed_match_clause({}, "a") == ""
