"""
Tests for the graph-projection completion marker in src/ingestion/service.py.

Guards a measured production defect. One legitimate corpus case,
`fir-1001-26`, had its narrative chunk re-projected repeatedly under the
sanitized provenance id `psrms_fir_fir-1001-26#narrative_c8bf2613`. Because
`entity_resolution._new_entity_id()` mints a random `uuid4` for every
mention that is not an exact CNIC match, each replay created fresh Person
nodes rather than matching the existing ones — measured live: 577 Person
nodes for 8 distinct names, one name duplicated 176 times, with each burst
adding roughly +41 APPEARS_IN, +35 BELONGS_TO_CASE and +32 pending SAME_AS.

The fix is a completion marker on the chunk's own Document node, checked
before the NER/LLM pass. The subtle requirement — and the reason an earlier
attempt deliberately stopped short — is that the Document node is written
at the START of extraction, so its mere existence must NOT count as
success: a half-projected document has to stay replayable.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import src.ingestion.service as service

REAL_DOC_ID = "psrms_fir_fir-1001-26#narrative_c8bf2613"
CHANGED_DOC_ID = "psrms_fir_fir-1001-26#narrative_9a91ee02"
OTHER_FIR_DOC_ID = "psrms_fir_fir-88-26#narrative_5c1d0b7a"


def _cypher_returning(rows):
    async def _fake(query, params=None, columns=None, graph=None):
        return rows

    return _fake


# ── The completion predicate ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_marked_document_is_complete():
    with patch("src.graph.age_client.execute_cypher", _cypher_returning([{"complete": True}])):
        assert await service._graph_projection_complete(REAL_DOC_ID) is True


@pytest.mark.asyncio
async def test_existing_but_unmarked_document_is_not_complete():
    """
    The whole point: the Document node is written before extraction runs,
    so existence alone must not suppress a replay.
    """
    with patch("src.graph.age_client.execute_cypher", _cypher_returning([{"complete": None}])):
        assert await service._graph_projection_complete(REAL_DOC_ID) is False


@pytest.mark.asyncio
async def test_absent_document_is_not_complete():
    with patch("src.graph.age_client.execute_cypher", _cypher_returning([])):
        assert await service._graph_projection_complete(CHANGED_DOC_ID) is False


@pytest.mark.asyncio
async def test_lookup_failure_fails_open():
    """Re-extracting costs time; wrongly skipping would drop graph state."""

    async def _boom(query, params=None, columns=None, graph=None):
        raise RuntimeError("graph unavailable")

    with patch("src.graph.age_client.execute_cypher", _boom):
        assert await service._graph_projection_complete(REAL_DOC_ID) is False


@pytest.mark.asyncio
async def test_predicate_keys_on_the_full_chunk_id_not_the_fir():
    """A changed narrative must not be suppressed by the old chunk's marker."""
    seen = {}

    async def _capture(query, params=None, columns=None, graph=None):
        seen["doc_id"] = (params or {}).get("doc_id")
        return []

    with patch("src.graph.age_client.execute_cypher", _capture):
        await service._graph_projection_complete(CHANGED_DOC_ID)

    assert seen["doc_id"] == CHANGED_DOC_ID
    assert seen["doc_id"] != REAL_DOC_ID


# ── The marker write ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_marker_write_targets_the_exact_document():
    calls = []

    async def _fake_write_node(label, match, properties=None, **kw):
        calls.append((label, match, properties))
        return {}

    with patch("src.graph.versioning.write_node", _fake_write_node):
        stats = {"errors": []}
        await service._mark_graph_projection_complete(REAL_DOC_ID, stats)

    assert stats["errors"] == []
    label, match, properties = calls[0]
    assert label == "Document"
    assert match == {"doc_id": REAL_DOC_ID}
    assert properties[service.PROJECTION_COMPLETE_PROPERTY] is True


@pytest.mark.asyncio
async def test_marker_write_failure_is_recorded_and_stays_replayable():
    """
    A run whose marker never landed is not durably complete, so the failure
    must surface rather than being swallowed as success.
    """

    async def _boom(label, match, properties=None, **kw):
        raise RuntimeError("write failed")

    with patch("src.graph.versioning.write_node", _boom):
        stats = {"errors": []}
        await service._mark_graph_projection_complete(REAL_DOC_ID, stats)

    assert len(stats["errors"]) == 1
    assert "projection_complete_marker" in stats["errors"][0]


@pytest.mark.asyncio
async def test_marker_write_is_idempotent():
    """MERGE on doc_id — re-stamping must not mint a second Document."""
    calls = []

    async def _fake_write_node(label, match, properties=None, **kw):
        calls.append(match)
        return {}

    with patch("src.graph.versioning.write_node", _fake_write_node):
        stats = {"errors": []}
        await service._mark_graph_projection_complete(REAL_DOC_ID, stats)
        await service._mark_graph_projection_complete(REAL_DOC_ID, stats)

    assert calls == [{"doc_id": REAL_DOC_ID}, {"doc_id": REAL_DOC_ID}]
    assert stats["errors"] == []


# ── Real-shaped replay regression ────────────────────────────────────────


class _FakeGraph:
    """Minimal stand-in for the Document-node completion state."""

    def __init__(self):
        self.complete: set[str] = set()
        self.extractions = 0

    async def execute_cypher(self, query, params=None, columns=None, graph=None):
        doc_id = (params or {}).get("doc_id")
        return [{"complete": True}] if doc_id in self.complete else []

    async def write_node(self, label, match, properties=None, **kw):
        if properties and properties.get(service.PROJECTION_COMPLETE_PROPERTY):
            self.complete.add(match["doc_id"])
        return {}

    async def project(self, doc_id, *, fail=False):
        """Stand-in for one _run_graph_extraction pass."""
        if await service._graph_projection_complete(doc_id):
            return {"skipped": "already_projected"}
        self.extractions += 1
        stats = {"errors": ["boom"] if fail else []}
        if not stats["errors"]:
            await service._mark_graph_projection_complete(doc_id, stats)
        return stats


@pytest.fixture
def fake_graph():
    g = _FakeGraph()
    with patch("src.graph.age_client.execute_cypher", g.execute_cypher), \
         patch("src.graph.versioning.write_node", g.write_node):
        yield g


@pytest.mark.asyncio
async def test_identical_replay_does_not_re_extract(fake_graph):
    """THE regression: the second identical replay must do no work."""
    await fake_graph.project(REAL_DOC_ID)
    assert fake_graph.extractions == 1

    result = await fake_graph.project(REAL_DOC_ID)
    assert fake_graph.extractions == 1, "identical replay re-ran extraction"
    assert result == {"skipped": "already_projected"}


@pytest.mark.asyncio
async def test_failed_projection_stays_replayable(fake_graph):
    """A partial/failed run must not be marked complete."""
    stats = await fake_graph.project(REAL_DOC_ID, fail=True)
    assert stats["errors"] == ["boom"]
    assert REAL_DOC_ID not in fake_graph.complete

    await fake_graph.project(REAL_DOC_ID)
    assert fake_graph.extractions == 2, "retry after failure was wrongly suppressed"
    assert REAL_DOC_ID in fake_graph.complete


@pytest.mark.asyncio
async def test_changed_content_still_extracts(fake_graph):
    await fake_graph.project(REAL_DOC_ID)
    await fake_graph.project(CHANGED_DOC_ID)
    assert fake_graph.extractions == 2


@pytest.mark.asyncio
async def test_other_fir_is_not_suppressed(fake_graph):
    await fake_graph.project(REAL_DOC_ID)
    await fake_graph.project(OTHER_FIR_DOC_ID)
    assert fake_graph.extractions == 2
