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
        self.calls.append({"cypher": cypher_query, "params": params or {}})
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
async def test_deterministic_timeline_conflict(fake_client):
    # Setup graph response: same incident, two different dates
    fake_client.queue([
        {"entity_id": "I-1", "description": "Robbery at bank", "date": "2023-10-01", "source_text": "robbery happened on 1st", "doc_id": "DOC-1"},
        {"entity_id": "I-1", "description": "Robbery at bank", "date": "2023-10-02", "source_text": "robbery happened on 2nd", "doc_id": "DOC-2"},
    ])
    
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
