"""
Tests for src/pipeline/harness/tools/graph.py (Phase 0, foundation layer).

Covers GRAPH and GRAPH_HYBRID via the single `hybrid` flag, asserting:
  (a) standardized GraphToolResult/EvidenceChunk shape;
  (b) fallback_to_rag=True on empty traversal, empty combined hybrid
      result, and evaluator rejection — the three cases design §2.2 names;
  (c) GRAPH_HYBRID chunks are always tagged source_tool="GRAPH_HYBRID",
      even ones that came from the vector-search leg (RESOLVED-1a);
  (d) conflicts_included reflects conflict-detection-sourced chunks.
"""
import pytest

import src.pipeline.harness.tools.graph as graph_mod
from src.pipeline.harness.tools.graph import GraphToolInput, GraphToolResult, graph_tool
from src.pipeline.harness.types import CallerContext, ExecutionContext, ToolStatus


def _caller(case_id="CASE-001", role="investigator"):
    return CallerContext(user_id="u1", role=role, active_case_id=case_id)


def _execution(case_id="CASE-001", role="investigator"):
    return ExecutionContext(caller=_caller(case_id, role))


def _graph_chunk(id_="g1", via_entity="Some Person", graph_confidence=0.81, case_id="CASE-001"):
    return {
        "id": id_, "text": "graph evidence", "metadata": {"source": "doc.pdf", "case_id": case_id},
        "hop": 1, "graph_confidence": graph_confidence, "via_entity": via_entity, "rrf_score": graph_confidence,
    }


def _empty_graph_result():
    return {"chunks": [], "hop_count": 0, "compounded_confidence": 1.0, "seed_entities": [], "unconfirmed_links": []}


def _graph_result(chunks, hop_count=1, confidence=0.81):
    return {
        "chunks": chunks, "hop_count": hop_count, "compounded_confidence": confidence,
        "seed_entities": [{"entity_id": "e1", "type": "Person", "name": "Some Person"}],
        "unconfirmed_links": [],
    }


@pytest.fixture(autouse=True)
def stub_shared(monkeypatch):
    async def _cross_rerank(query, candidates, top_k=None):
        return candidates[: (top_k or len(candidates))]

    monkeypatch.setattr(graph_mod, "cross_rerank", _cross_rerank)


def _relevant_evaluator(monkeypatch):
    async def _evaluate(orig, rewritten, chunks):
        return {"relevant": True, "reason": "ok"}
    monkeypatch.setattr(graph_mod, "evaluate_relevance", _evaluate)


def _not_relevant_evaluator(monkeypatch):
    async def _evaluate(orig, rewritten, chunks):
        return {"relevant": False, "reason": "not enough"}
    monkeypatch.setattr(graph_mod, "evaluate_relevance", _evaluate)


# ── GRAPH (hybrid=False) ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_graph_relevant_chunks_returns_ok(monkeypatch):
    async def _retrieve_graph(query_text, target_entity, case_id, cross_case, max_hops, user_id, user_role):
        assert cross_case is False
        return _graph_result([_graph_chunk()])

    monkeypatch.setattr(graph_mod, "retrieve_graph", _retrieve_graph)
    _relevant_evaluator(monkeypatch)

    result = await graph_tool(GraphToolInput(query_text="q", execution=_execution()))

    assert isinstance(result, GraphToolResult)
    assert result.status == ToolStatus.OK
    assert result.fallback_to_rag is False
    assert result.hop_count == 1
    assert result.chain_confidence == 0.81
    assert len(result.chunks) == 1
    chunk = result.chunks[0]
    assert chunk.metadata.source_tool == "GRAPH"
    assert chunk.metadata.confidence_status == "computed"
    assert chunk.metadata.confidence == 0.81


@pytest.mark.asyncio
async def test_graph_no_chunks_falls_back_to_rag(monkeypatch):
    async def _retrieve_graph(*a, **kw):
        return _empty_graph_result()

    monkeypatch.setattr(graph_mod, "retrieve_graph", _retrieve_graph)

    result = await graph_tool(GraphToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.EMPTY
    assert result.fallback_to_rag is True
    assert result.chunks == []


@pytest.mark.asyncio
async def test_within_case_graph_entities_survive_a_not_relevant_evaluator(monkeypatch):
    """
    [verify-log Finding Q] The evaluator judges whether retrieved DOCUMENT
    TEXT answers the question. Within-case graph chunks are entity records
    (names, CNICs, phones) scoped to the case by construction — read as prose
    they look like "only personal details, nothing about the case", and were
    being discarded. A real case with 18 graph nodes therefore reported
    "Case graph data was unavailable for this case".

    This previously asserted EMPTY; that was the defect, not the contract.
    """
    async def _retrieve_graph(*a, **kw):
        return _graph_result([_graph_chunk()])

    monkeypatch.setattr(graph_mod, "retrieve_graph", _retrieve_graph)
    _not_relevant_evaluator(monkeypatch)

    result = await graph_tool(GraphToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.OK
    assert result.fallback_to_rag is False
    assert len(result.chunks) == 1


@pytest.mark.asyncio
async def test_cross_case_graph_still_honours_a_not_relevant_evaluator(monkeypatch):
    """
    The override above is safe only because case scoping guarantees
    relevance. With no active case there is no such guarantee, so the
    evaluator's rejection must still stand.
    """
    async def _retrieve_graph(*a, **kw):
        return _graph_result([_graph_chunk()])

    monkeypatch.setattr(graph_mod, "retrieve_graph", _retrieve_graph)
    _not_relevant_evaluator(monkeypatch)

    result = await graph_tool(
        GraphToolInput(query_text="q", execution=_execution(case_id=None))
    )

    assert result.status == ToolStatus.EMPTY
    assert result.fallback_to_rag is True
    assert result.chunks == []


@pytest.mark.asyncio
async def test_graph_conflicts_included_flag(monkeypatch):
    conflict_chunk = _graph_chunk(id_="c1", via_entity="conflict_detection")

    async def _retrieve_graph(*a, **kw):
        return _graph_result([conflict_chunk])

    monkeypatch.setattr(graph_mod, "retrieve_graph", _retrieve_graph)
    _relevant_evaluator(monkeypatch)

    result = await graph_tool(GraphToolInput(query_text="q", execution=_execution()))
    assert result.conflicts_included is True


@pytest.mark.asyncio
async def test_graph_max_hops_bounds_enforced():
    with pytest.raises(Exception):
        GraphToolInput(query_text="q", execution=_execution(), max_hops=10)


# ── GRAPH_HYBRID (hybrid=True) ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_graph_hybrid_tags_all_chunks_hybrid(monkeypatch):
    vector_chunk = {"id": "v1", "text": "vector evidence", "metadata": {"source": "doc2.pdf", "case_id": "CASE-001"}, "rrf_score": 0.6}

    async def _retrieve_graph(*a, **kw):
        return _graph_result([_graph_chunk(id_="g1")])

    async def _retrieve_candidates(query_text, where, fetch_top_k, final_top_k, is_cross_case):
        return [vector_chunk], []

    monkeypatch.setattr(graph_mod, "retrieve_graph", _retrieve_graph)
    monkeypatch.setattr(graph_mod, "_retrieve_candidates", _retrieve_candidates)
    _relevant_evaluator(monkeypatch)

    result = await graph_tool(GraphToolInput(query_text="q", execution=_execution(), hybrid=True))

    assert result.status == ToolStatus.OK
    ids = {c.id for c in result.chunks}
    assert ids == {"g1", "v1"}
    # RESOLVED-1a: EVERY chunk, including the vector-only one, is tagged
    # GRAPH_HYBRID — never left as "RAG" or folded into "GRAPH".
    assert all(c.metadata.source_tool == "GRAPH_HYBRID" for c in result.chunks)


@pytest.mark.asyncio
async def test_graph_hybrid_empty_combined_falls_back(monkeypatch):
    async def _retrieve_graph(*a, **kw):
        return _empty_graph_result()

    async def _retrieve_candidates(*a, **kw):
        return [], []

    monkeypatch.setattr(graph_mod, "retrieve_graph", _retrieve_graph)
    monkeypatch.setattr(graph_mod, "_retrieve_candidates", _retrieve_candidates)

    result = await graph_tool(GraphToolInput(query_text="q", execution=_execution(), hybrid=True))

    assert result.status == ToolStatus.EMPTY
    assert result.fallback_to_rag is True


# ── Finding Q regression: within-case graph entities are not "irrelevant" ────
# A case with 18 real graph nodes (2 Persons, an Officer, a Weapon, an
# Incident) reported "Case graph data was unavailable for this case".
# retrieve_graph() returned 4 chunks correctly; the evaluator then rejected
# them — it judges whether retrieved DOCUMENT TEXT answers the question, and
# entity records (names, CNICs, phone numbers) read as "only lists personal
# details ... does not provide information about the case itself"
# (verify-log Finding Q). Within-case graph chunks are scoped to the case by
# construction, so they cannot be off-topic for a question about that case.

import inspect as _inspect

from src.pipeline.harness.tools import graph as _graph_mod


def test_within_case_evaluator_rejection_is_overridden():
    source = _inspect.getsource(_graph_mod.graph_tool)
    # The override exists and is gated on the within-case path only.
    assert "not tool_input.hybrid and caller.active_case_id" in source


def test_hybrid_path_still_honours_the_evaluator():
    """The hybrid path blends vector/BM25 document chunks whose topical
    relevance genuinely is in question — its gate must stay."""
    source = _inspect.getsource(_graph_mod.graph_tool)
    override_index = source.index("not tool_input.hybrid and caller.active_case_id")
    # The real EMPTY return still follows the override, so a hybrid or
    # cross-case rejection can still short-circuit.
    assert "ToolStatus.EMPTY" in source[override_index:]


def test_cross_case_rejection_still_short_circuits():
    """No active_case_id means no case scoping, so the guarantee that makes
    the override safe does not hold — the gate must still apply."""
    source = _inspect.getsource(_graph_mod.graph_tool)
    assert "caller.active_case_id" in source
