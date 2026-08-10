import pytest
import json

from src.graph import conflict_detection, versioning

class FakeAgeClient:
    def __init__(self):
        self.calls = []
        self.responses = []

    def queue(self, response):
        self.responses.append(response)

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        self.calls.append({"cypher": cypher_query, "params": params or {}, "columns": columns})
        if self.responses:
            return self.responses.pop(0)
        return []

@pytest.fixture
def fake_client(monkeypatch):
    client = FakeAgeClient()
    monkeypatch.setattr(conflict_detection, "age_client", client)
    # Also patch versioning's age_client so we can track write_edge
    monkeypatch.setattr(versioning, "age_client", client)
    return client

@pytest.mark.asyncio
async def test_fetch_query_does_not_have_match_after_optional_match(fake_client):
    """
    Regression, confirmed live against real AGE (2026-08-07): detect_conflicts()'s
    fetch query used to place a mandatory MATCH directly after an OPTIONAL
    MATCH -- invalid openCypher/AGE ("MATCH cannot follow OPTIONAL MATCH"),
    so this function always hit its own except-and-warn branch and never
    ran conflict detection for any case, ever. FakeAgeClient accepts any
    Cypher string unconditionally (a canned-response stub, not a real
    Cypher engine), so the existing tests couldn't catch this on their
    own -- this test pins the query's clause ORDER structurally instead:
    no mandatory MATCH line may appear after the last OPTIONAL MATCH line.
    """
    fake_client.queue([])  # empty result is fine -- only the query shape is under test

    await conflict_detection.detect_conflicts("CASE-1")

    assert len(fake_client.calls) == 1
    cypher = fake_client.calls[0]["cypher"]
    lines = [line.strip() for line in cypher.strip().splitlines() if line.strip()]
    match_lines = [i for i, line in enumerate(lines) if line.upper().startswith("MATCH")]
    optional_match_lines = [i for i, line in enumerate(lines) if line.upper().startswith("OPTIONAL MATCH")]
    assert optional_match_lines, "expected at least one OPTIONAL MATCH clause"
    last_optional = max(optional_match_lines)
    for m in match_lines:
        assert m <= last_optional, (
            f"mandatory MATCH at line {m} appears after OPTIONAL MATCH at line {last_optional} -- invalid AGE/Cypher"
        )


@pytest.mark.asyncio
async def test_fetch_query_requests_matching_columns(fake_client):
    """
    Regression, confirmed live (2026-08-07): the fetch call never passed
    `columns=` to execute_cypher(), defaulting to a single ("result",)
    column while the query RETURNs 5 named columns -- AGE/asyncpg raises
    DatatypeMismatchError ("return row and column definition list do not
    match") the moment this actually runs against real AGE. FakeAgeClient
    doesn't validate this (a stub, not a real Cypher engine), so this test
    asserts on the call's actual `columns` argument instead.
    """
    fake_client.queue([])

    await conflict_detection.detect_conflicts("CASE-1")

    assert len(fake_client.calls) == 1
    columns = list(fake_client.calls[0]["columns"])
    assert set(columns) == {"entity_id", "description", "date", "source_text", "doc_id"}


@pytest.mark.asyncio
async def test_deterministic_timeline_conflict(fake_client, monkeypatch):
    # Setup graph response: same incident, two different dates
    fake_client.queue([
        {"entity_id": "I-1", "description": "Robbery at bank", "date": "2023-10-01", "source_text": "robbery happened on 1st", "doc_id": "DOC-1"},
        {"entity_id": "I-1", "description": "Robbery at bank", "date": "2023-10-02", "source_text": "robbery happened on 2nd", "doc_id": "DOC-2"},
    ])

    # [Regression, confirmed live] detect_conflicts() ALWAYS proceeds to its
    # step-3 LLM-grounded comparison after the deterministic check, even
    # though this test only cares about the deterministic path -- without
    # mocking call_llm() here, this test made a REAL, uncaught Gemini call
    # every run (silently "passing" locally only because a real API key
    # happens to be configured; CI has none and failed loudly with
    # ValueError: No API key was provided). Violates this suite's own
    # stated rule in conftest.py ("No network... no real LLM"). An empty
    # parsed-list response means "no LLM-grounded conflicts found," which
    # adds no further fake_client calls -- consistent with this test's own
    # assertion that exactly one write_edge call (the deterministic one)
    # happens.
    async def mock_call_llm(*args, **kwargs):
        return "[]"

    monkeypatch.setattr(conflict_detection, "call_llm", mock_call_llm)

    # write_edge call response
    fake_client.queue([{"r": {"id": 10}}])

    await conflict_detection.detect_conflicts("CASE-1")

    # Should have called write_edge once for the deterministic conflict
    assert len(fake_client.calls) == 2
    write_call = fake_client.calls[1]
    assert "CONFLICTS_WITH" in write_call["cypher"]
    assert write_call["params"]["a_entity_id"] == "I-1"
    assert write_call["params"]["b_entity_id"] == "I-1"
    assert "Deterministic:" in write_call["params"]["p_basis"]

@pytest.mark.asyncio
async def test_llm_grounded_conflict(fake_client, monkeypatch):
    # Setup graph response: two incidents with contradictory details
    fake_client.queue([
        {"entity_id": "I-1", "description": "Red car seen at 8pm", "date": "2023-10-01", "source_text": "The suspects fled in a red car at 8pm.", "doc_id": "DOC-1"},
        {"entity_id": "I-2", "description": "Blue car seen at 8pm", "date": "2023-10-01", "source_text": "The suspects fled in a blue car at exactly 8pm.", "doc_id": "DOC-2"},
    ])

    # Mock call_llm
    async def mock_call_llm(*args, **kwargs):
        return json.dumps([{
            "incident_a_id": "I-1",
            "incident_b_id": "I-2",
            "basis": "Contradictory car colors for the same event",
            "quote_a": "red car",
            "quote_b": "blue car"
        }])
    
    monkeypatch.setattr(conflict_detection, "call_llm", mock_call_llm)

    # write_edge call response
    fake_client.queue([{"r": {"id": 11}}])

    await conflict_detection.detect_conflicts("CASE-1")

    # Should have called write_edge once for the LLM conflict
    assert len(fake_client.calls) == 2
    write_call = fake_client.calls[1]
    assert "CONFLICTS_WITH" in write_call["cypher"]
    assert write_call["params"]["a_entity_id"] == "I-1"
    assert write_call["params"]["b_entity_id"] == "I-2"
    assert "Contradictory car colors" in write_call["params"]["p_basis"]

@pytest.mark.asyncio
async def test_llm_grounded_conflict_fails_grounding(fake_client, monkeypatch):
    # Setup graph response: two incidents
    fake_client.queue([
        {"entity_id": "I-1", "description": "Red car seen at 8pm", "date": "2023-10-01", "source_text": "The suspects fled in a red car at 8pm.", "doc_id": "DOC-1"},
        {"entity_id": "I-2", "description": "Blue car seen at 8pm", "date": "2023-10-01", "source_text": "The suspects fled in a blue car at exactly 8pm.", "doc_id": "DOC-2"},
    ])

    # Mock call_llm with a hallucinated quote
    async def mock_call_llm(*args, **kwargs):
        return json.dumps([{
            "incident_a_id": "I-1",
            "incident_b_id": "I-2",
            "basis": "Contradictory car colors for the same event",
            "quote_a": "hallucinated quote",
            "quote_b": "blue car"
        }])
    
    monkeypatch.setattr(conflict_detection, "call_llm", mock_call_llm)

    await conflict_detection.detect_conflicts("CASE-1")

    # Should NOT have called write_edge because grounding failed
    assert len(fake_client.calls) == 1


@pytest.mark.asyncio
async def test_llm_conflict_with_missing_quotes_is_rejected(fake_client, monkeypatch):
    """
    Regression: `if quote_a and quote_a not in text_a: continue` treated a
    missing/empty quote as an automatic pass (the `and` short-circuited),
    letting an ungrounded LLM-claimed conflict through untouched. A missing
    quote must be rejected exactly like a wrong one.
    """
    fake_client.queue([
        {"entity_id": "I-1", "description": "Red car seen at 8pm", "date": "2023-10-01", "source_text": "The suspects fled in a red car at 8pm.", "doc_id": "DOC-1"},
        {"entity_id": "I-2", "description": "Blue car seen at 8pm", "date": "2023-10-01", "source_text": "The suspects fled in a blue car at exactly 8pm.", "doc_id": "DOC-2"},
    ])

    async def mock_call_llm(*args, **kwargs):
        return json.dumps([{
            "incident_a_id": "I-1",
            "incident_b_id": "I-2",
            "basis": "Contradictory car colors for the same event",
            "quote_a": "",  # LLM omitted the required quote
            "quote_b": "blue car",
        }])

    monkeypatch.setattr(conflict_detection, "call_llm", mock_call_llm)

    await conflict_detection.detect_conflicts("CASE-1")

    # Should NOT have called write_edge — an empty quote is not grounding.
    assert len(fake_client.calls) == 1
