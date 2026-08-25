"""
Tests for src/pipeline/harness/agents/local_search.py (findings.md Module 8,
Local Search).

Mirrors tests/test_harness_agent_semantic_search.py's own monkeypatch-at-
module-level pattern -- local_search_tool/rag_tool/call_llm/verify_grounding
are stubbed; no live infra.

Covers:
  (a) local_search_tool OK + verifier passes -> OK, tools_used=["GRAPH"];
  (b) local_search_tool EMPTY + rag_tool OK + verifier passes -> PARTIAL,
      tools_used=["RAG"], degraded_from=["GRAPH"] (the RAG-fallback
      composition this session's plan adds beyond findings.md's literal
      proposal);
  (c) both EMPTY -> EMPTY, not an error;
  (d) local_search_tool OK but verifier rejects -> ABSTAINED, no answer_text;
  (e) local_search_tool FAILED -> ABSTAINED with error propagated;
  (f) module-level self-registration + a Supervisor.handle() integration
      path, mirroring semantic_search's own (f).
"""
from __future__ import annotations

import pytest

import src.pipeline.harness.agents.local_search as local_search_mod
from src.pipeline.harness.agents.local_search import local_search
from src.pipeline.harness.supervisor import LOCAL_SEARCH, Supervisor, get_registered
from src.pipeline.harness.tools.local_search import LocalSearchToolResult
from src.pipeline.harness.tools.rag import RagToolResult
from src.pipeline.harness.types import (
    CallerContext,
    ChunkMetadata,
    EvidenceChunk,
    ExecutionContext,
    Role,
    SubAgentInput,
    SubAgentStatus,
    ToolError,
    ToolStatus,
)


def _chunk(id_="c1", text="Officer ندیم, investigating officer for this case.", case_id="CASE-001", source="fir-401-26"):
    return EvidenceChunk(id=id_, text=text, metadata=ChunkMetadata(source_tool="GRAPH", case_id=case_id, source_file=source))


def _rag_chunk(id_="r1", text="A document mentions the officer.", case_id="CASE-001", source="doc.pdf"):
    return EvidenceChunk(id=id_, text=text, metadata=ChunkMetadata(source_tool="RAG", case_id=case_id, source_file=source))


def _caller(case_id="CASE-001", role=Role.INVESTIGATOR, **kw):
    return CallerContext(user_id="u1", role=role, active_case_id=case_id, **kw)


def _execution(caller=None):
    return ExecutionContext(caller=caller or _caller())


def _agent_input(caller=None, query_text="who is the investigating officer in this case?", **kw):
    return SubAgentInput(query_text=query_text, execution=_execution(caller=caller), **kw)


def _stub_local_search_tool(monkeypatch, result: LocalSearchToolResult):
    async def _fake(tool_input):
        return result

    monkeypatch.setattr(local_search_mod, "local_search_tool", _fake)


def _stub_rag_tool(monkeypatch, result: RagToolResult):
    async def _fake(tool_input):
        return result

    monkeypatch.setattr(local_search_mod, "rag_tool", _fake)


def _stub_call_llm(monkeypatch, answer: str = "The investigating officer is ندیم [Document 1]."):
    async def _fake(system_prompt, user_message, **kwargs):
        return answer

    monkeypatch.setattr(local_search_mod, "call_llm", _fake)


def _stub_verify_grounding(monkeypatch, grounded: bool, off_topic: bool = False, reason: str = "ok"):
    async def _fake(answer, cited_chunks, case_id, cross_case_ids=None, target_date=None):
        return {
            "grounded": grounded, "off_topic": off_topic, "leaked_case_id": None,
            "unsupported_claims": [], "reason": reason,
        }

    monkeypatch.setattr(local_search_mod, "verify_grounding", _fake)


# ═══════════════════════════════════════════════════════════════════════
# (a) local_search_tool OK -> OK
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ok_result_with_grounded_answer(monkeypatch):
    chunks = [_chunk()]
    _stub_local_search_tool(monkeypatch, LocalSearchToolResult(status=ToolStatus.OK, chunks=chunks, hop_count=0, chain_confidence=0.9))
    _stub_call_llm(monkeypatch, "The investigating officer is ندیم [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await local_search(_agent_input())

    assert result.status == SubAgentStatus.OK
    assert result.answer_text == "The investigating officer is ندیم [Document 1]."
    assert result.tools_used == ["GRAPH"]
    assert result.degraded_from == []
    assert len(result.citations) == 1
    assert result.citations[0].source_tool == "GRAPH"


@pytest.mark.asyncio
async def test_community_omission_caveat_for_non_person_entity(monkeypatch):
    chunks = [_chunk()]
    _stub_local_search_tool(
        monkeypatch,
        LocalSearchToolResult(
            status=ToolStatus.OK, chunks=chunks, hop_count=0, chain_confidence=0.9,
            matched_entities=[{"entity_id": "off1", "label": "Officer", "case_id": "CASE-001", "canonical_name": "ندیم"}],
            community_reports_included=False,
        ),
    )
    _stub_call_llm(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await local_search(_agent_input())

    assert result.status == SubAgentStatus.OK
    assert any("community" in c.lower() for c in result.caveats)


# ═══════════════════════════════════════════════════════════════════════
# (b) local_search_tool EMPTY -> RAG fallback -> PARTIAL
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_empty_local_search_falls_back_to_rag(monkeypatch):
    _stub_local_search_tool(monkeypatch, LocalSearchToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True))
    _stub_rag_tool(monkeypatch, RagToolResult(status=ToolStatus.OK, chunks=[_rag_chunk()], evaluator_verdict="relevant"))
    _stub_call_llm(monkeypatch, "A document mentions the officer [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await local_search(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert result.tools_used == ["RAG"]
    assert result.degraded_from == ["GRAPH"]
    assert result.answer_text == "A document mentions the officer [Document 1]."
    assert result.citations[0].source_tool == "RAG"


# ═══════════════════════════════════════════════════════════════════════
# (c) both empty -> EMPTY, not an error
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_both_empty_returns_empty_not_abstained(monkeypatch):
    _stub_local_search_tool(monkeypatch, LocalSearchToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True))
    _stub_rag_tool(monkeypatch, RagToolResult(status=ToolStatus.EMPTY, chunks=[], evaluator_verdict=None))

    caller = _caller(case_id=None)
    result = await local_search(_agent_input(caller=caller))

    assert result.status == SubAgentStatus.EMPTY
    assert result.answer_text is None


@pytest.mark.asyncio
async def test_local_search_empty_rag_not_relevant_abstains(monkeypatch):
    _stub_local_search_tool(monkeypatch, LocalSearchToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True))
    _stub_rag_tool(monkeypatch, RagToolResult(status=ToolStatus.EMPTY, chunks=[], evaluator_verdict="not_relevant"))

    result = await local_search(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None


# ═══════════════════════════════════════════════════════════════════════
# (d) verifier rejects -> ABSTAINED, no answer served
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_verifier_rejection_abstains(monkeypatch):
    _stub_local_search_tool(monkeypatch, LocalSearchToolResult(status=ToolStatus.OK, chunks=[_chunk()]))
    _stub_call_llm(monkeypatch, "I don't have access to case files.")
    _stub_verify_grounding(monkeypatch, grounded=False, off_topic=True, reason="generic refusal")

    result = await local_search(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.citations == []


# ═══════════════════════════════════════════════════════════════════════
# (e) local_search_tool FAILED -> ABSTAINED, error propagated
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_local_search_tool_failure_abstains_with_error(monkeypatch):
    err = ToolError(kind="upstream_failure", message="chroma unreachable")
    _stub_local_search_tool(monkeypatch, LocalSearchToolResult(status=ToolStatus.FAILED, error=err))

    result = await local_search(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.error is err


# ═══════════════════════════════════════════════════════════════════════
# (f) Supervisor integration
# ═══════════════════════════════════════════════════════════════════════

def test_local_search_is_registered_under_its_own_name():
    assert get_registered(LOCAL_SEARCH) is local_search


@pytest.mark.asyncio
async def test_supervisor_dispatches_investigating_officer_query_to_local_search(monkeypatch):
    """
    End-to-end dispatch proof: a GRAPH-routed, descriptive officer-role
    query hits Supervisor.handle() and is classified to Local Search (not
    Case Summarization, GRAPH/GRAPH_HYBRID's usual mapping) via the
    supervisor.py trigger-pattern override, then runs the real
    local_search() -> real local_search_tool() chain (with the tool's own
    retrieve_graph/query_similar_entities dependencies stubbed).
    """
    import src.pipeline.harness.supervisor as supervisor_mod
    import src.pipeline.harness.tools.local_search as local_search_tool_mod

    async def _fake_route_query(query_text: str) -> dict:
        return {"route": "GRAPH", "output_format": "chat", "case_scope": "within_case", "target_entity": None}

    monkeypatch.setattr(supervisor_mod, "route_query", _fake_route_query)

    async def _fake_query_similar_entities(query, case_id, top_k=3):
        return [{"entity_id": "off1", "label": "Officer", "case_id": case_id, "canonical_name": "ندیم", "distance": 0.1}]

    async def _fake_retrieve_graph(query_text, target_entity, case_id, cross_case, max_hops, user_id, user_role):
        assert target_entity == "ندیم"  # the semantic match, not the raw query text
        return {
            "chunks": [{
                "id": "g1", "text": "Officer ندیم, belt GEN-0301, investigating officer for this case.",
                "metadata": {"source": "fir-401-26", "case_id": case_id}, "rrf_score": 0.9,
            }],
            "hop_count": 0, "compounded_confidence": 0.9,
            "seed_entities": [{"entity_id": "off1", "type": "Officer", "name": "ندیم"}],
            "unconfirmed_links": [],
        }

    async def _fake_fetch_community_chunks(entity_ids, case_id):
        return []

    async def _cross_rerank(query, candidates, top_k=None):
        return candidates[: (top_k or len(candidates))]

    async def _relevant(orig, rewritten, chunks):
        return {"relevant": True, "reason": "good match"}

    monkeypatch.setattr(local_search_tool_mod, "query_similar_entities", _fake_query_similar_entities)
    monkeypatch.setattr(local_search_tool_mod, "retrieve_graph", _fake_retrieve_graph)
    monkeypatch.setattr(local_search_tool_mod, "_fetch_community_chunks", _fake_fetch_community_chunks)
    monkeypatch.setattr(local_search_tool_mod, "cross_rerank", _cross_rerank)
    monkeypatch.setattr(local_search_tool_mod, "evaluate_relevance", _relevant)

    _stub_call_llm(monkeypatch, "The investigating officer is ندیم, belt GEN-0301 [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)

    sup = Supervisor()  # no override -> real module-level registry
    result = await sup.handle(_agent_input(query_text="who is the investigating officer in this case?"))

    assert result.status == SubAgentStatus.OK
    assert result.answer_text == "The investigating officer is ندیم, belt GEN-0301 [Document 1]."
    assert result.tools_used == ["GRAPH"]
    assert result.citations[0].source_file == "fir-401-26"
