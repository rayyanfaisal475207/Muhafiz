"""
Tests for src/pipeline/harness/agents/semantic_search.py (Phase 2).

Covers:
  (a) successful search with citations -- RAG OK + verifier passes ->
      status=OK, bounded citations (no chunk text), tools_used=["RAG"];
  (b) evaluator-rejection -> ABSTAINED (RAG EMPTY + evaluator_verdict
      "not_relevant" surviving the retry loop);
  (c) empty-result handling -- RAG EMPTY with no evaluator ever run (nothing
      in scope to search) -> status=EMPTY, not an error;
  (d) verifier-rejection of a generated answer -> ABSTAINED, answer_text
      stays None even though the RAG tool itself succeeded;
  (e) RAG tool FAILED -> ABSTAINED with the tool's error propagated;
  (f) module-level self-registration into the Supervisor's registry, and a
      Supervisor.handle() -> Semantic Search -> RAG tool integration path.

`rag_tool` and `call_llm`/`verify_grounding` are monkeypatched at the
module level (`semantic_search_mod.*`) in every test -- none of these hit
live infra, per this session's scope (test/mock data only).
"""
from __future__ import annotations

import pytest

import src.pipeline.harness.agents.semantic_search as semantic_search_mod
from src.pipeline.harness.agents.semantic_search import semantic_search
from src.pipeline.harness.supervisor import (
    SEMANTIC_SEARCH,
    Supervisor,
    get_registered,
)
from src.pipeline.harness.tools.rag import RagToolResult
from src.pipeline.harness.types import (
    CallerContext,
    ChunkMetadata,
    EvidenceChunk,
    Role,
    SubAgentInput,
    SubAgentStatus,
    ToolError,
    ToolStatus,
)


def _chunk(id_="c1", text="the suspect fled the scene", case_id="CASE-001", source="doc.pdf"):
    return EvidenceChunk(
        id=id_,
        text=text,
        metadata=ChunkMetadata(source_tool="RAG", case_id=case_id, source_file=source),
    )


def _caller(case_id="CASE-001", role=Role.INVESTIGATOR, **kw):
    return CallerContext(user_id="u1", role=role, active_case_id=case_id, **kw)


def _agent_input(caller=None, query_text="who was involved in the theft?", **kw):
    return SubAgentInput(query_text=query_text, caller=caller or _caller(), **kw)


def _stub_rag_tool(monkeypatch, result: RagToolResult):
    async def _fake(tool_input):
        return result

    monkeypatch.setattr(semantic_search_mod, "rag_tool", _fake)


def _stub_call_llm(monkeypatch, answer: str = "The suspect fled the scene [Document 1]."):
    async def _fake(system_prompt, user_message, **kwargs):
        return answer

    monkeypatch.setattr(semantic_search_mod, "call_llm", _fake)


def _stub_verify_grounding(monkeypatch, grounded: bool, off_topic: bool = False, reason: str = "ok"):
    async def _fake(answer, cited_chunks, case_id, cross_case_ids=None, target_date=None):
        return {
            "grounded": grounded,
            "off_topic": off_topic,
            "leaked_case_id": None,
            "unsupported_claims": [],
            "reason": reason,
        }

    monkeypatch.setattr(semantic_search_mod, "verify_grounding", _fake)


# ═══════════════════════════════════════════════════════════════════════
# (a) successful search with citations
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_successful_search_returns_ok_with_bounded_citations(monkeypatch):
    chunks = [_chunk("c1", source="fir.pdf"), _chunk("c2", source="witness.pdf")]
    _stub_rag_tool(monkeypatch, RagToolResult(status=ToolStatus.OK, chunks=chunks, evaluator_verdict="relevant"))
    _stub_call_llm(monkeypatch, "The suspect fled [Document 1], corroborated by a witness [Document 2].")
    _stub_verify_grounding(monkeypatch, grounded=True)

    result = await semantic_search(_agent_input())

    assert result.status == SubAgentStatus.OK
    assert result.answer_text == "The suspect fled [Document 1], corroborated by a witness [Document 2]."
    assert result.tools_used == ["RAG"]
    assert result.degraded_from == []
    assert len(result.citations) == 2
    assert result.citations[0].document_index == 1
    assert result.citations[0].source_tool == "RAG"
    assert result.citations[0].case_id == "CASE-001"
    assert result.citations[1].document_index == 2
    # Bounded payload -- never the underlying chunk text/EvidenceChunk.
    for citation in result.citations:
        assert not hasattr(citation, "text")


# ═══════════════════════════════════════════════════════════════════════
# (b) evaluator rejection after retry exhaustion -> ABSTAINED
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_evaluator_not_relevant_after_retries_abstains(monkeypatch):
    _stub_rag_tool(
        monkeypatch,
        RagToolResult(status=ToolStatus.EMPTY, chunks=[], retries_used=2, evaluator_verdict="not_relevant"),
    )

    result = await semantic_search(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.citations == []
    assert result.caveats  # some user-facing explanation survives


# ═══════════════════════════════════════════════════════════════════════
# (c) empty-result handling -- nothing in scope, no evaluator ran
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_nothing_in_scope_returns_empty_not_abstained(monkeypatch):
    _stub_rag_tool(monkeypatch, RagToolResult(status=ToolStatus.EMPTY, chunks=[], evaluator_verdict=None))

    caller = _caller(case_id=None)
    result = await semantic_search(_agent_input(caller=caller))

    assert result.status == SubAgentStatus.EMPTY
    assert result.answer_text is None
    assert result.caveats


# ═══════════════════════════════════════════════════════════════════════
# (d) verifier rejects a generated answer -> ABSTAINED, no answer served
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_verifier_rejection_abstains_even_though_rag_succeeded(monkeypatch):
    _stub_rag_tool(monkeypatch, RagToolResult(status=ToolStatus.OK, chunks=[_chunk()], evaluator_verdict="relevant"))
    _stub_call_llm(monkeypatch, "I don't have access to case files.")
    _stub_verify_grounding(monkeypatch, grounded=False, off_topic=True, reason="generic refusal")

    result = await semantic_search(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.citations == []
    assert result.caveats


# ═══════════════════════════════════════════════════════════════════════
# (e) RAG tool FAILED -> ABSTAINED, error propagated
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_rag_tool_failure_abstains_with_error(monkeypatch):
    err = ToolError(kind="upstream_failure", message="embedding service unreachable")
    _stub_rag_tool(monkeypatch, RagToolResult(status=ToolStatus.FAILED, error=err))

    result = await semantic_search(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.error is err


# ═══════════════════════════════════════════════════════════════════════
# (f) Supervisor integration -- real registration, real dispatch chain
# ═══════════════════════════════════════════════════════════════════════

def test_semantic_search_is_registered_under_its_own_name():
    # Import-time self-registration (module docstring) -- importing the
    # module above already registered it into the live, module-level
    # registry; this just asserts that actually happened.
    assert get_registered(SEMANTIC_SEARCH) is semantic_search


@pytest.mark.asyncio
async def test_supervisor_dispatches_to_real_semantic_search_and_real_rag_tool(monkeypatch):
    """
    Supervisor.handle() -> real Semantic Search -> real rag_tool() ->
    SubAgentResult, with router.route_query() and rag_tool's own wrapped
    pipeline functions stubbed to deterministic test data (not live infra).
    Proves Phase 1 (Supervisor) and Phase 2 (Semantic Search) actually
    connect end to end.
    """
    import src.pipeline.harness.supervisor as supervisor_mod
    import src.pipeline.harness.tools.rag as rag_tool_mod

    async def _fake_route_query(query_text: str) -> dict:
        return {"route": "RAG", "output_format": "chat"}

    monkeypatch.setattr(supervisor_mod, "route_query", _fake_route_query)

    def _raw_chunk(id_):
        return {
            "id": id_,
            "text": "the vehicle was seen near the market",
            "metadata": {"source": "cctv_report.pdf", "case_id": "CASE-001"},
            "rrf_score": 0.9,
        }

    async def _embed_text(q, **kwargs):
        return [0.1, 0.2]

    async def _query_similar(q, emb, top_k=10, where=None, **kwargs):
        return [_raw_chunk("c1")]

    async def _get_all_chunks(where=None):
        return [_raw_chunk("c1")]

    def _retrieve_bm25(query, docs, top_k=10):
        return docs[:top_k]

    def _rerank_results(semantic, bm25, top_k=5):
        merged = {c["id"]: c for c in semantic + bm25}
        return list(merged.values())[:top_k]

    async def _cross_rerank(query, candidates, top_k=None):
        return candidates[: (top_k or len(candidates))]

    async def _expand_query(q, n=2):
        return []

    async def _cross_script_variant(q):
        return None

    async def _relevant(orig, rewritten, chunks):
        return {"relevant": True, "reason": "good match"}

    monkeypatch.setattr(rag_tool_mod, "embed_text", _embed_text)
    monkeypatch.setattr(rag_tool_mod, "query_similar", _query_similar)
    monkeypatch.setattr(rag_tool_mod, "get_all_chunks", _get_all_chunks)
    monkeypatch.setattr(rag_tool_mod, "retrieve_bm25", _retrieve_bm25)
    monkeypatch.setattr(rag_tool_mod, "rerank_results", _rerank_results)
    monkeypatch.setattr(rag_tool_mod, "cross_rerank", _cross_rerank)
    monkeypatch.setattr(rag_tool_mod, "expand_query", _expand_query)
    monkeypatch.setattr(rag_tool_mod, "generate_cross_script_variant", _cross_script_variant)
    monkeypatch.setattr(rag_tool_mod, "evaluate_relevance", _relevant)

    _stub_call_llm(monkeypatch, "The vehicle was seen near the market [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)

    sup = Supervisor()  # no override -> real module-level registry
    result = await sup.handle(_agent_input(query_text="where was the vehicle seen?"))

    assert result.status == SubAgentStatus.OK
    assert result.answer_text == "The vehicle was seen near the market [Document 1]."
    assert result.tools_used == ["RAG"]
    assert len(result.citations) == 1
    assert result.citations[0].source_file == "cctv_report.pdf"
    assert result.citations[0].case_id == "CASE-001"
