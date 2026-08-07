"""
Tests for src/pipeline/harness/tools/rag.py (Phase 0, foundation layer).

Asserts:
  (a) it returns the standardized RagToolResult/EvidenceChunk shape;
  (b) it reproduces today's practical RAG-route behavior — evaluator-
      feedback retry loop, abstain (not error) on retry exhaustion, never
      falls back to WEB;
  (c) the multi-tenant scoping guard (never search unscoped) and the
      retrieval-infrastructure-failure -> FAILED distinction hold.
"""
import pytest

import src.pipeline.harness.tools.rag as rag_mod
from src.pipeline.harness.tools.rag import RagToolInput, RagToolResult, rag_tool
from src.pipeline.harness.types import CallerContext, ExecutionContext, ToolStatus


def _chunk(id_, text="text", case_id="CASE-001", **extra):
    meta = {"source": "doc.pdf", "case_id": case_id, **extra}
    return {"id": id_, "text": text, "metadata": meta, "rrf_score": 0.5}


@pytest.fixture(autouse=True)
def stub_pipeline(monkeypatch):
    """Fast, deterministic stand-ins for every wrapped function."""
    async def _embed_text(q, **kwargs):
        return [0.1, 0.2]

    async def _query_similar(q, emb, top_k=10, where=None, **kwargs):
        return [_chunk("c1"), _chunk("c2")]

    async def _get_all_chunks(where=None):
        return [_chunk("c1"), _chunk("c2"), _chunk("c3")]

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

    monkeypatch.setattr(rag_mod, "embed_text", _embed_text)
    monkeypatch.setattr(rag_mod, "query_similar", _query_similar)
    monkeypatch.setattr(rag_mod, "get_all_chunks", _get_all_chunks)
    monkeypatch.setattr(rag_mod, "retrieve_bm25", _retrieve_bm25)
    monkeypatch.setattr(rag_mod, "rerank_results", _rerank_results)
    monkeypatch.setattr(rag_mod, "cross_rerank", _cross_rerank)
    monkeypatch.setattr(rag_mod, "expand_query", _expand_query)
    monkeypatch.setattr(rag_mod, "generate_cross_script_variant", _cross_script_variant)


def _caller(case_id="CASE-001"):
    return CallerContext(user_id="u1", role="investigator", active_case_id=case_id)


def _execution(case_id="CASE-001", **kw):
    return ExecutionContext(caller=_caller(case_id), **kw)


@pytest.mark.asyncio
async def test_relevant_on_first_try_returns_ok(monkeypatch):
    async def _relevant(orig, rewritten, chunks):
        return {"relevant": True, "reason": "good match"}

    monkeypatch.setattr(rag_mod, "evaluate_relevance", _relevant)

    result = await rag_tool(RagToolInput(query_text="q", execution=_execution()))

    assert isinstance(result, RagToolResult)
    assert result.status == ToolStatus.OK
    assert result.fallback_to_rag is False
    assert result.retries_used == 0
    assert result.evaluator_verdict == "relevant"
    assert len(result.chunks) > 0
    assert all(c.metadata.source_tool == "RAG" for c in result.chunks)
    assert all(c.metadata.case_id == "CASE-001" for c in result.chunks)


@pytest.mark.asyncio
async def test_retry_exhaustion_abstains_never_reaches_web(monkeypatch):
    calls = {"rewrite": 0}

    async def _not_relevant(orig, rewritten, chunks):
        return {"relevant": False, "reason": "missing detail"}

    async def _rewrite_for_retry(original_message, previous_query, evaluator_feedback):
        calls["rewrite"] += 1
        return previous_query + " refined"

    monkeypatch.setattr(rag_mod, "evaluate_relevance", _not_relevant)
    monkeypatch.setattr(rag_mod, "rewrite_for_retry", _rewrite_for_retry)

    result = await rag_tool(RagToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.EMPTY
    assert result.evaluator_verdict == "not_relevant"
    assert result.chunks == []
    # Retry actually happened (evaluator feedback drove a rewrite) — not a
    # single silent attempt.
    assert calls["rewrite"] >= 1
    # No WEB source ever appears — RAG's fallback removal is by construction
    # (this module imports nothing web-related), but assert the observable
    # contract too: fallback_to_rag is pinned False even on abstain.
    assert result.fallback_to_rag is False


@pytest.mark.asyncio
async def test_retrieval_infra_failure_is_failed_not_empty(monkeypatch):
    async def _broken_embed(q, **kwargs):
        raise RuntimeError("embedding service unreachable")

    monkeypatch.setattr(rag_mod, "embed_text", _broken_embed)

    result = await rag_tool(RagToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.FAILED
    assert result.error is not None
    assert result.error.kind == "upstream_failure"


@pytest.mark.asyncio
async def test_no_case_and_no_global_returns_empty_without_searching(monkeypatch):
    called = {"embed": False}

    async def _embed_text(q, **kwargs):
        called["embed"] = True
        return [0.1]

    monkeypatch.setattr(rag_mod, "embed_text", _embed_text)

    execution = _execution(case_id=None)
    result = await rag_tool(RagToolInput(query_text="q", execution=execution, include_global=False))

    assert result.status == ToolStatus.EMPTY
    assert called["embed"] is False  # never even attempted an unscoped search


def test_build_where_prefers_case_over_global():
    caller = CallerContext(user_id="u1", role="investigator", active_case_id="CASE-009")
    assert rag_mod._build_where(caller, include_global=True) == {"case_id": "CASE-009"}


def test_build_where_falls_back_to_global_when_no_case():
    caller = CallerContext(user_id="u1", role="investigator", active_case_id=None)
    assert rag_mod._build_where(caller, include_global=True) == {"is_global": True}


def test_build_where_prefers_project_over_global_when_no_case():
    # [Contract retrofit — plan §10.1/§10.3] project_id, newly carried on
    # ExecutionContext, narrows scope when there's no active case, before
    # falling back to global-only.
    caller = CallerContext(user_id="u1", role="investigator", active_case_id=None)
    assert rag_mod._build_where(caller, include_global=True, project_id="PROJ-1") == {
        "project_id": "PROJ-1"
    }


def test_build_where_case_still_wins_over_project():
    caller = CallerContext(user_id="u1", role="investigator", active_case_id="CASE-009")
    assert rag_mod._build_where(caller, include_global=True, project_id="PROJ-1") == {
        "case_id": "CASE-009"
    }


def test_rag_tool_result_fallback_cannot_be_true():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RagToolResult(status=ToolStatus.OK, fallback_to_rag=True)
