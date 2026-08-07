"""
Tests for src/pipeline/harness/tools/xgraph.py (Phase 0, foundation layer).

Covers:
  (a) standardized XGraphToolResult/EvidenceChunk shape;
  (b) fallback_to_rag is permanently False (type-pinned — never blends
      cross-case evidence into a case-scoped RAG stream);
  (c) PermissionError from retrieve_graph() maps to status=DENIED, not an
      exception escaping the tool boundary, and not silently degrading;
  (d) an empty result with no unconfirmed links is a real EMPTY finding,
      not an error;
  (e) case_ids_touched is derived correctly for the Verifier's leakage
      backstop.
"""
import pytest

import src.pipeline.harness.tools.xgraph as xgraph_mod
from src.pipeline.harness.tools.xgraph import XGraphToolInput, XGraphToolResult, xgraph_tool
from src.pipeline.harness.types import CallerContext, ExecutionContext, ToolStatus


def _caller(role="supervisor"):
    return CallerContext(user_id="u1", role=role, active_case_id=None)


def _execution(role="supervisor"):
    return ExecutionContext(caller=_caller(role))


def _chunk(id_, case_id, confidence=0.8):
    return {
        "id": id_, "text": "cross-case evidence",
        "metadata": {"source": "doc.pdf", "case_id": case_id},
        "graph_confidence": confidence, "rrf_score": confidence,
    }


@pytest.mark.asyncio
async def test_ok_result_never_falls_back(monkeypatch):
    async def _retrieve_graph(query_text, target_entity, case_id, cross_case, max_hops, user_id, user_role):
        assert cross_case is True
        assert case_id is None
        return {
            "chunks": [_chunk("c1", "CASE-002"), _chunk("c2", "CASE-003")],
            "hop_count": 2, "compounded_confidence": 0.7,
            "seed_entities": [], "unconfirmed_links": [],
        }

    monkeypatch.setattr(xgraph_mod, "retrieve_graph", _retrieve_graph)

    result = await xgraph_tool(XGraphToolInput(query_text="find John across cases", execution=_execution()))

    assert isinstance(result, XGraphToolResult)
    assert result.status == ToolStatus.OK
    assert result.fallback_to_rag is False
    assert set(result.case_ids_touched) == {"CASE-002", "CASE-003"}
    assert all(c.metadata.source_tool == "XGRAPH" for c in result.chunks)


@pytest.mark.asyncio
async def test_empty_with_no_unconfirmed_links_is_a_real_finding(monkeypatch):
    async def _retrieve_graph(*a, **kw):
        return {"chunks": [], "hop_count": 0, "compounded_confidence": 1.0, "seed_entities": [], "unconfirmed_links": []}

    monkeypatch.setattr(xgraph_mod, "retrieve_graph", _retrieve_graph)

    result = await xgraph_tool(XGraphToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.EMPTY
    assert result.fallback_to_rag is False
    assert result.chunks == []


@pytest.mark.asyncio
async def test_empty_chunks_with_unconfirmed_links_still_reported(monkeypatch):
    async def _retrieve_graph(*a, **kw):
        return {
            "chunks": [], "hop_count": 0, "compounded_confidence": 1.0, "seed_entities": [],
            "unconfirmed_links": [{"entity": "A", "candidate": "B", "tier": "flagged_unverified", "confidence": 0.4}],
        }

    monkeypatch.setattr(xgraph_mod, "retrieve_graph", _retrieve_graph)

    result = await xgraph_tool(XGraphToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.EMPTY
    assert len(result.unconfirmed_links) == 1


@pytest.mark.asyncio
async def test_permission_error_maps_to_denied_not_exception(monkeypatch):
    async def _retrieve_graph(*a, **kw):
        raise PermissionError("Cross-case graph traversal requires supervisor role or higher.")

    monkeypatch.setattr(xgraph_mod, "retrieve_graph", _retrieve_graph)

    result = await xgraph_tool(
        XGraphToolInput(query_text="q", execution=_execution(role="investigator"))
    )

    assert result.status == ToolStatus.DENIED
    assert result.error is not None
    assert result.error.kind == "permission_denied"
    assert result.fallback_to_rag is False
    assert result.chunks == []


@pytest.mark.asyncio
async def test_other_exception_maps_to_failed_not_denied(monkeypatch):
    async def _retrieve_graph(*a, **kw):
        raise RuntimeError("AGE connection lost")

    monkeypatch.setattr(xgraph_mod, "retrieve_graph", _retrieve_graph)

    result = await xgraph_tool(XGraphToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.FAILED
    assert result.error.kind == "upstream_failure"


def test_fallback_to_rag_cannot_be_overridden_true():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        XGraphToolResult(status=ToolStatus.OK, fallback_to_rag=True)
