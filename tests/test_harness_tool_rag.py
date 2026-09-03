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

    async def _bm25_candidate_pool(query_text, where=None):
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
    monkeypatch.setattr(rag_mod, "bm25_candidate_pool", _bm25_candidate_pool)
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


# ═══════════════════════════════════════════════════════════════════════
# Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 5: global_corpus_appears_empty
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_global_only_search_with_zero_candidates_flags_corpus_empty(monkeypatch):
    """A global-only-scoped search (no case, investigator role — the
    legal-knowledge-base scope) that finds literally nothing on every
    retry must set global_corpus_appears_empty, distinct from an ordinary
    'nothing matched this question' EMPTY."""
    async def _empty_query_similar(q, emb, top_k=10, where=None, **kwargs):
        return []

    async def _empty_bm25_pool(query_text, where=None):
        return []

    async def _not_relevant(orig, rewritten, chunks):
        return {"relevant": False, "reason": "no evidence"}

    monkeypatch.setattr(rag_mod, "query_similar", _empty_query_similar)
    monkeypatch.setattr(rag_mod, "bm25_candidate_pool", _empty_bm25_pool)
    monkeypatch.setattr(rag_mod, "evaluate_relevance", _not_relevant)
    async def _rewrite_for_retry(original_message, previous_query, evaluator_feedback):
        return previous_query

    monkeypatch.setattr(rag_mod, "rewrite_for_retry", _rewrite_for_retry)

    execution = _execution(case_id=None)
    result = await rag_tool(RagToolInput(query_text="what does Section 154 CrPC say", execution=execution))

    assert result.status == ToolStatus.EMPTY
    assert result.global_corpus_appears_empty is True


@pytest.mark.asyncio
async def test_case_scoped_search_never_flags_corpus_empty(monkeypatch):
    """The signal must only fire for the global-only scope — an ordinary
    within-case search finding nothing is ambiguous ('no matching case
    documents' is a normal, unremarkable EMPTY), not a KB-not-loaded
    situation."""
    async def _empty_query_similar(q, emb, top_k=10, where=None, **kwargs):
        return []

    async def _empty_bm25_pool(query_text, where=None):
        return []

    async def _not_relevant(orig, rewritten, chunks):
        return {"relevant": False, "reason": "no evidence"}

    monkeypatch.setattr(rag_mod, "query_similar", _empty_query_similar)
    monkeypatch.setattr(rag_mod, "bm25_candidate_pool", _empty_bm25_pool)
    monkeypatch.setattr(rag_mod, "evaluate_relevance", _not_relevant)
    async def _rewrite_for_retry(original_message, previous_query, evaluator_feedback):
        return previous_query

    monkeypatch.setattr(rag_mod, "rewrite_for_retry", _rewrite_for_retry)

    result = await rag_tool(RagToolInput(query_text="q", execution=_execution(case_id="CASE-001")))

    assert result.status == ToolStatus.EMPTY
    assert result.global_corpus_appears_empty is False


@pytest.mark.asyncio
async def test_global_only_search_with_real_candidates_never_flags_corpus_empty(monkeypatch):
    """Global scope + candidates found (just not relevant to this specific
    question) must NOT be reported as an empty corpus — the default stub
    fixture already returns non-empty candidates."""
    async def _not_relevant(orig, rewritten, chunks):
        return {"relevant": False, "reason": "no evidence"}

    monkeypatch.setattr(rag_mod, "evaluate_relevance", _not_relevant)
    async def _rewrite_for_retry(original_message, previous_query, evaluator_feedback):
        return previous_query

    monkeypatch.setattr(rag_mod, "rewrite_for_retry", _rewrite_for_retry)

    result = await rag_tool(RagToolInput(query_text="q", execution=_execution(case_id=None)))

    assert result.status == ToolStatus.EMPTY
    assert result.global_corpus_appears_empty is False


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


# [Scenario-test Finding A] The bug that forced harness cutover to be
# reverted: a supervisor+ with no case selected ("All Cases") fell through to
# the is_global branch and got global-reference-only scoping. The global
# corpus is empty in this deployment, so those queries silently returned
# nothing. orchestrator.py::_build_retrieval_where() always had this
# role-based fallback; this tool never did, and the two drifted.
@pytest.mark.parametrize("role", ["supervisor", "station-admin", "platform-admin"])
def test_build_where_gives_cross_case_roles_all_cases_when_no_case(role):
    caller = CallerContext(user_id="u1", role=role, active_case_id=None)
    assert rag_mod._build_where(caller, include_global=True) == {"all_cases": True}


def test_build_where_investigator_never_gains_cross_case_reach():
    """The other half of the same fix: an investigator with no case selected
    must NOT silently gain cross-case access — their fallback is unchanged."""
    caller = CallerContext(user_id="u1", role="investigator", active_case_id=None)
    assert rag_mod._build_where(caller, include_global=True) == {"is_global": True}


@pytest.mark.parametrize("role", ["supervisor", "station-admin", "platform-admin"])
def test_build_where_active_case_still_wins_for_cross_case_roles(role):
    """An explicitly selected case must still scope to that case, even for a
    role that would otherwise get the all_cases fallback."""
    caller = CallerContext(user_id="u1", role=role, active_case_id="CASE-009")
    assert rag_mod._build_where(caller, include_global=True) == {"case_id": "CASE-009"}


@pytest.mark.parametrize("role", ["supervisor", "platform-admin"])
def test_build_where_project_still_wins_over_all_cases(role):
    """Project scoping is narrower than 'All Cases' and must take precedence."""
    caller = CallerContext(user_id="u1", role=role, active_case_id=None)
    assert rag_mod._build_where(caller, include_global=True, project_id="PROJ-1") == {
        "project_id": "PROJ-1"
    }


def test_rag_tool_result_fallback_cannot_be_true():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        RagToolResult(status=ToolStatus.OK, fallback_to_rag=True)
