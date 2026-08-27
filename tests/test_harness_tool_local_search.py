"""
Tests for src/pipeline/harness/tools/local_search.py (findings.md Module 8,
Local Search).

Mirrors tests/test_harness_tool_graph.py's own monkeypatch-at-module-level
pattern -- query_similar_entities/retrieve_graph/_fetch_community_chunks/
cross_rerank/evaluate_relevance are all stubbed; no live infra.

Covers:
  (a) THE REGRESSION TEST -- this session's own confirmed failure: a
      descriptive query with no literal name/identifier finds a real seed
      via query_similar_entities() (retrieve_graph is fed the MATCHED
      entity's own canonical_name as target_entity, not the query text);
  (b) no case in scope -> EMPTY, no query attempted at all (fail closed);
  (c) no semantic entity match -> EMPTY, fallback_to_rag=True;
  (d) fan-out correctly INCLUDES a community report when a matched entity's
      community has one;
  (e) fan-out correctly OMITS it (no crash) when no matched entity has one;
  (f) evaluator rejection -> EMPTY, fallback_to_rag=True;
  (g) every emitted chunk is tagged source_tool="GRAPH" (the closed-Literal
      tagging decision from the approved plan), never a new SourceTool value.
"""
from __future__ import annotations

import pytest

import src.pipeline.harness.tools.local_search as local_search_mod
from src.pipeline.harness.tools.local_search import LocalSearchToolInput, local_search_tool
from src.pipeline.harness.types import CallerContext, ExecutionContext, ToolStatus


def _caller(case_id="CASE-001", role="investigator"):
    return CallerContext(user_id="u1", role=role, active_case_id=case_id)


def _execution(case_id="CASE-001", role="investigator"):
    return ExecutionContext(caller=_caller(case_id, role))


def _entity_match(entity_id="off1", label="Officer", case_id="CASE-001", canonical_name="ندیم"):
    return {"entity_id": entity_id, "label": label, "case_id": case_id, "canonical_name": canonical_name, "distance": 0.1}


def _graph_chunk(id_="g1", case_id="CASE-001"):
    return {
        "id": id_, "text": "Officer ندیم appears in fir_structured record fir-401-26, investigating officer for this case.",
        "metadata": {"source": "fir-401-26", "case_id": case_id},
        "hop": 0, "graph_confidence": 0.9, "via_entity": "ندیم", "rrf_score": 0.9,
    }


def _graph_result(chunks, hop_count=0, confidence=0.9):
    return {
        "chunks": chunks, "hop_count": hop_count, "compounded_confidence": confidence,
        "seed_entities": [{"entity_id": "off1", "type": "Officer", "name": "ندیم"}],
        "unconfirmed_links": [],
    }


@pytest.fixture(autouse=True)
def stub_shared(monkeypatch):
    async def _cross_rerank(query, candidates, top_k=None):
        return candidates[: (top_k or len(candidates))]

    async def _evaluate_relevant(orig, rewritten, chunks):
        return {"relevant": True, "reason": "ok"}

    monkeypatch.setattr(local_search_mod, "cross_rerank", _cross_rerank)
    monkeypatch.setattr(local_search_mod, "evaluate_relevance", _evaluate_relevant)

    async def _no_community_reports(entity_ids, case_id):
        return []

    monkeypatch.setattr(local_search_mod, "_fetch_community_chunks", _no_community_reports)

    # Default: no confirmed SAME_AS expansion — passthrough. The dedicated
    # canonicalization test below overrides this to prove the expansion is
    # actually wired in.
    async def _passthrough(entity_ids):
        return entity_ids

    monkeypatch.setattr(local_search_mod, "_canonicalize_entity_ids", _passthrough)


def _stub_query_similar_entities(monkeypatch, matches):
    async def _fake(query, case_id, top_k=3):
        return matches

    monkeypatch.setattr(local_search_mod, "query_similar_entities", _fake)


def _stub_retrieve_graph(monkeypatch, result_by_target_entity):
    async def _fake(query_text, target_entity, case_id, cross_case, max_hops, user_id, user_role):
        assert cross_case is False
        return result_by_target_entity.get(target_entity, _graph_result([]))

    monkeypatch.setattr(local_search_mod, "retrieve_graph", _fake)


# ═══════════════════════════════════════════════════════════════════════
# (a) THE REGRESSION TEST -- semantic match feeds retrieve_graph() a real
#     seed for a descriptive query with no literal identifier
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_descriptive_query_finds_seed_via_semantic_match(monkeypatch):
    """
    The confirmed live failure this session reproduced: "who is the
    investigating officer in this case" has no literal name/cnic/phone/
    belt_no for graph_retriever._find_seed_nodes()'s CONTAINS match to
    find. This tool must still produce a real, cited result -- by feeding
    the semantically-matched entity's own canonical_name into
    retrieve_graph() as target_entity, which IS a literal string the graph
    contains.
    """
    _stub_query_similar_entities(monkeypatch, [_entity_match(canonical_name="ندیم")])
    _stub_retrieve_graph(monkeypatch, {"ندیم": _graph_result([_graph_chunk()])})

    result = await local_search_tool(
        LocalSearchToolInput(query_text="who is the investigating officer in this case", execution=_execution())
    )

    assert result.status == ToolStatus.OK
    assert result.fallback_to_rag is False
    assert len(result.chunks) == 1
    assert result.chunks[0].metadata.source_tool == "GRAPH"
    assert "investigating officer" in result.chunks[0].text
    assert result.matched_entities[0]["canonical_name"] == "ندیم"


# ═══════════════════════════════════════════════════════════════════════
# (b) no case in scope -> EMPTY, no query attempted (fail closed)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_active_case_fails_closed(monkeypatch):
    called = {"query_similar_entities": False}

    async def _fake(*a, **kw):
        called["query_similar_entities"] = True
        return []

    monkeypatch.setattr(local_search_mod, "query_similar_entities", _fake)

    result = await local_search_tool(
        LocalSearchToolInput(query_text="who is the investigating officer?", execution=_execution(case_id=None))
    )

    assert result.status == ToolStatus.EMPTY
    assert called["query_similar_entities"] is False


# ═══════════════════════════════════════════════════════════════════════
# (c) no semantic entity match -> EMPTY, fallback_to_rag=True
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_semantic_match_falls_back_to_rag(monkeypatch):
    _stub_query_similar_entities(monkeypatch, [])

    result = await local_search_tool(LocalSearchToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.EMPTY
    assert result.fallback_to_rag is True
    assert result.chunks == []


# ═══════════════════════════════════════════════════════════════════════
# (d)/(e) community-report fan-out, both directions
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_fan_out_includes_community_report_when_present(monkeypatch):
    _stub_query_similar_entities(monkeypatch, [_entity_match(entity_id="p1", label="Person", canonical_name="طارق")])
    _stub_retrieve_graph(monkeypatch, {"طارق": _graph_result([_graph_chunk(id_="g1")])})

    async def _with_community(entity_ids, case_id):
        assert entity_ids == ["p1"]
        return [{
            "id": "community:C-001", "text": "This community spans a recurring theft pattern.",
            "metadata": {"case_id": case_id, "source": "community:C-001", "evidence_kind": "community_report", "member_count": 4},
        }]

    monkeypatch.setattr(local_search_mod, "_fetch_community_chunks", _with_community)

    result = await local_search_tool(LocalSearchToolInput(query_text="who is طارق connected to?", execution=_execution()))

    assert result.status == ToolStatus.OK
    assert result.community_reports_included is True
    ids = {c.id for c in result.chunks}
    assert "community:C-001" in ids
    community_chunk = next(c for c in result.chunks if c.id == "community:C-001")
    assert community_chunk.metadata.source_tool == "GRAPH"
    assert community_chunk.metadata.model_dump().get("evidence_kind") == "community_report"


@pytest.mark.asyncio
async def test_fan_out_omits_community_report_without_crashing(monkeypatch):
    # community_membership is Person-only -- an Officer match legitimately
    # has no community. _fetch_community_chunks is already stubbed to
    # return [] by the autouse stub_shared fixture.
    _stub_query_similar_entities(monkeypatch, [_entity_match(entity_id="off1", label="Officer", canonical_name="ندیم")])
    _stub_retrieve_graph(monkeypatch, {"ندیم": _graph_result([_graph_chunk()])})

    result = await local_search_tool(LocalSearchToolInput(query_text="who is the investigating officer?", execution=_execution()))

    assert result.status == ToolStatus.OK
    assert result.community_reports_included is False
    assert all(c.id != "community:C-001" for c in result.chunks)


# ═══════════════════════════════════════════════════════════════════════
# (d.1) canonicalization — a semantically-matched NON-canonical duplicate
# still finds its community report via a confirmed SAME_AS expansion
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_community_join_expands_through_confirmed_same_as(monkeypatch):
    """
    The matched entity ("p-dup") is itself a physical duplicate of the real
    community member ("p1") via a confirmed SAME_AS edge. Without
    canonicalizing before the community_membership join, the report would
    be missed entirely (the KNOWN GAP this fix closes).
    """
    _stub_query_similar_entities(monkeypatch, [_entity_match(entity_id="p-dup", label="Person", canonical_name="کاشف")])
    _stub_retrieve_graph(monkeypatch, {"کاشف": _graph_result([_graph_chunk(id_="g1")])})

    async def _expand(entity_ids):
        assert entity_ids == ["p-dup"]
        return ["p-dup", "p1"]

    monkeypatch.setattr(local_search_mod, "_canonicalize_entity_ids", _expand)

    async def _with_community(entity_ids, case_id):
        assert set(entity_ids) == {"p-dup", "p1"}
        return [{
            "id": "community:C-002", "text": "Recurring pattern community report.",
            "metadata": {"case_id": case_id, "source": "community:C-002", "evidence_kind": "community_report", "member_count": 3},
        }]

    monkeypatch.setattr(local_search_mod, "_fetch_community_chunks", _with_community)

    result = await local_search_tool(LocalSearchToolInput(query_text="who is کاشف connected to?", execution=_execution()))

    assert result.status == ToolStatus.OK
    assert result.community_reports_included is True
    assert any(c.id == "community:C-002" for c in result.chunks)


# ═══════════════════════════════════════════════════════════════════════
# (f) evaluator rejection -> EMPTY, fallback_to_rag=True
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_evaluator_not_relevant_falls_back_to_rag(monkeypatch):
    _stub_query_similar_entities(monkeypatch, [_entity_match()])
    _stub_retrieve_graph(monkeypatch, {"ندیم": _graph_result([_graph_chunk()])})

    async def _not_relevant(orig, rewritten, chunks):
        return {"relevant": False, "reason": "not enough"}

    monkeypatch.setattr(local_search_mod, "evaluate_relevance", _not_relevant)

    result = await local_search_tool(LocalSearchToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.EMPTY
    assert result.fallback_to_rag is True


# ═══════════════════════════════════════════════════════════════════════
# (g) multi-entity fan-out dedupes across concurrent retrieve_graph() calls
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_multi_entity_fanout_dedupes_shared_chunks(monkeypatch):
    _stub_query_similar_entities(monkeypatch, [
        _entity_match(entity_id="p1", canonical_name="طارق"),
        _entity_match(entity_id="p2", canonical_name="بلال"),
    ])
    shared_chunk = _graph_chunk(id_="shared")
    _stub_retrieve_graph(monkeypatch, {
        "طارق": _graph_result([shared_chunk, _graph_chunk(id_="only_tariq")]),
        "بلال": _graph_result([shared_chunk]),
    })

    result = await local_search_tool(LocalSearchToolInput(query_text="q", execution=_execution()))

    ids = [c.id for c in result.chunks]
    assert ids.count("shared") == 1
    assert "only_tariq" in ids


# ═══════════════════════════════════════════════════════════════════════
# (h) LS-2 — every EMPTY return must carry the reason describing the state
#     it actually observed, and entity-retrieval failure must be contained
#     inside the tool contract instead of escaping as an exception.
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_case_scope_sets_no_case_scope_reason(monkeypatch):
    result = await local_search_tool(
        LocalSearchToolInput(query_text="who is the officer?", execution=_execution(case_id=None))
    )
    assert result.status == ToolStatus.EMPTY
    assert result.empty_reason == "no_case_scope"


@pytest.mark.asyncio
async def test_no_scoped_entity_match_sets_no_entity_match_reason(monkeypatch):
    """Covers an empty entity index AND case-scope filtering — the tool
    cannot tell them apart (query_similar_entities is hard-scoped
    server-side) and must not add an unscoped lookup to try."""
    _stub_query_similar_entities(monkeypatch, [])

    result = await local_search_tool(
        LocalSearchToolInput(query_text="who is the officer?", execution=_execution())
    )
    assert result.status == ToolStatus.EMPTY
    assert result.empty_reason == "no_entity_match"
    assert result.fallback_to_rag is True
    assert result.matched_entities == []


@pytest.mark.asyncio
async def test_no_linked_evidence_sets_its_own_reason(monkeypatch):
    """Semantic match succeeded, traversal produced nothing."""
    _stub_query_similar_entities(monkeypatch, [_entity_match()])
    _stub_retrieve_graph(monkeypatch, {"ندیم": _graph_result([])})

    result = await local_search_tool(
        LocalSearchToolInput(query_text="who is the officer?", execution=_execution())
    )
    assert result.status == ToolStatus.EMPTY
    assert result.empty_reason == "no_linked_evidence"
    # The state that makes the old generic caveat false.
    assert result.matched_entities and result.matched_entities[0]["canonical_name"] == "ندیم"


@pytest.mark.asyncio
async def test_evaluator_rejection_sets_evaluator_rejected_reason(monkeypatch):
    _stub_query_similar_entities(monkeypatch, [_entity_match()])
    _stub_retrieve_graph(monkeypatch, {"ندیم": _graph_result([_graph_chunk()])})

    async def _not_relevant(orig, rewritten, chunks):
        return {"relevant": False, "reason": "off topic"}

    monkeypatch.setattr(local_search_mod, "evaluate_relevance", _not_relevant)

    result = await local_search_tool(
        LocalSearchToolInput(query_text="what is the weather?", execution=_execution())
    )
    assert result.status == ToolStatus.EMPTY
    assert result.empty_reason == "evaluator_rejected"
    assert result.matched_entities  # a match DID exist


@pytest.mark.asyncio
async def test_entity_retrieval_exception_returns_failed_not_raises(monkeypatch):
    """§12 — before LS-2 this propagated out of the tool AND the agent, so
    Local Search could never emit any caveat at all."""
    async def _boom(query, case_id, top_k=3):
        raise RuntimeError("chroma unavailable")

    monkeypatch.setattr(local_search_mod, "query_similar_entities", _boom)

    result = await local_search_tool(
        LocalSearchToolInput(query_text="who is the officer?", execution=_execution())
    )
    assert result.status == ToolStatus.FAILED
    assert result.error is not None
    assert result.error.kind == "upstream_failure"
    assert "chroma unavailable" in result.error.message
    # A failure is not an emptiness claim.
    assert result.empty_reason is None


@pytest.mark.asyncio
async def test_successful_search_sets_no_empty_reason(monkeypatch):
    _stub_query_similar_entities(monkeypatch, [_entity_match()])
    _stub_retrieve_graph(monkeypatch, {"ندیم": _graph_result([_graph_chunk()])})

    result = await local_search_tool(
        LocalSearchToolInput(query_text="who is the officer?", execution=_execution())
    )
    assert result.status == ToolStatus.OK
    assert result.empty_reason is None
