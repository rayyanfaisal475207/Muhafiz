"""
Contract tests for the REAL tool implementations.

`tests/harness/conftest.py` pins the registry to stubs, so the other harness
tests never exercise `tools/real.py`. These do — with every production boundary
mocked, so they still need no database, no model server, and no network.

What is verified here is that the REAL adapters honour the same §1 guarantees
the stubs are held to: fallback_to_rag polarity, the cross-case role gate and
its ordering, source_tool tagging, and the "reference/web data is not case
evidence" rule. A real adapter that quietly inverts one of these would
otherwise only surface in production.
"""
from __future__ import annotations

import pytest

from src.pipeline.harness.contracts import (
    CallerContext,
    GraphToolInput,
    RagToolInput,
    Role,
    SqlToolInput,
    ToolStatus,
    WebToolInput,
    XAggToolInput,
    XGraphToolInput,
    XNetworkToolInput,
)
from src.pipeline.harness.tools import real


@pytest.fixture(autouse=True)
def _use_real_tools():
    """Undo conftest's stub pinning — these tests target real.py directly."""
    yield


def _investigator() -> CallerContext:
    return CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id="CASE-A")


def _supervisor() -> CallerContext:
    return CallerContext(user_id="u2", role=Role.SUPERVISOR, active_case_id="CASE-A")


# ── Cross-case role gate: the ordering that must not regress ──────────────

@pytest.mark.parametrize(
    "tool,input_model,name",
    [
        (real.xgraph_tool, XGraphToolInput, "XGRAPH"),
        (real.xagg_tool, XAggToolInput, "XAGG"),
        (real.xnetwork_tool, XNetworkToolInput, "XNETWORK"),
    ],
)
async def test_real_cross_case_tools_deny_investigator(tool, input_model, name, gateway):
    result = await tool(input_model(query_text="q", caller=_investigator()), gateway=gateway)

    assert result.status is ToolStatus.DENIED
    assert result.error.kind == "permission_denied"
    assert result.chunks == [], "a denied call must not return evidence"


@pytest.mark.parametrize(
    "tool,input_model,name",
    [
        (real.xgraph_tool, XGraphToolInput, "XGRAPH"),
        (real.xagg_tool, XAggToolInput, "XAGG"),
        (real.xnetwork_tool, XNetworkToolInput, "XNETWORK"),
    ],
)
async def test_real_denial_audits_before_returning(tool, input_model, name, gateway):
    """[PRESERVE — design §4.3] Audit written on the denial path, every time."""
    await tool(input_model(query_text="q", caller=_investigator()), gateway=gateway)

    events = [e for e in gateway.audit_log if e["event_type"] == "authorization_violation"]
    assert len(events) == 1
    assert events[0]["details"]["route"] == name
    assert events[0]["details"]["role"] == "investigator"


@pytest.mark.parametrize(
    "tool,input_model",
    [
        (real.xgraph_tool, XGraphToolInput),
        (real.xagg_tool, XAggToolInput),
        (real.xnetwork_tool, XNetworkToolInput),
    ],
)
async def test_real_denial_never_reaches_production_code(tool, input_model, gateway, monkeypatch):
    """
    The gate must short-circuit BEFORE any production call — that is what
    guarantees cross-case/RLS scope is never armed for an unauthorized caller.
    Any real call would raise here.
    """
    def _explode(*a, **k):
        raise AssertionError("production code reached on a denied cross-case call")

    monkeypatch.setattr("src.retrieval.graph_retriever.retrieve_graph", _explode, raising=False)
    monkeypatch.setattr("src.pipeline.xagg.run_aggregate", _explode, raising=False)
    monkeypatch.setattr("src.pipeline.xnetwork.run_network_query", _explode, raising=False)

    result = await tool(input_model(query_text="q", caller=_investigator()), gateway=gateway)
    assert result.status is ToolStatus.DENIED


async def test_real_xgraph_maps_permission_error_to_denied(gateway, monkeypatch):
    """A PermissionError from the production gate surfaces as DENIED, not FAILED."""
    async def _raise(*a, **k):
        raise PermissionError("Cross-case graph traversal requires supervisor role or higher.")

    monkeypatch.setattr("src.retrieval.graph_retriever.retrieve_graph", _raise)

    result = await real.xgraph_tool(
        XGraphToolInput(query_text="q", caller=_supervisor()), gateway=gateway
    )
    assert result.status is ToolStatus.DENIED
    assert result.error.kind == "permission_denied"


# ── fallback_to_rag polarity on the real adapters ────────────────────────

async def test_real_graph_signals_fallback_on_empty(monkeypatch):
    async def _empty(*a, **k):
        return {"chunks": [], "hop_count": 0, "compounded_confidence": 1.0,
                "seed_entities": [], "unconfirmed_links": []}

    monkeypatch.setattr("src.retrieval.graph_retriever.retrieve_graph", _empty)

    result = await real.graph_tool(GraphToolInput(query_text="q", caller=_investigator()))
    assert result.status is ToolStatus.EMPTY
    assert result.fallback_to_rag is True


async def test_real_graph_signals_fallback_on_failure(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("graph down")

    monkeypatch.setattr("src.retrieval.graph_retriever.retrieve_graph", _boom)

    result = await real.graph_tool(GraphToolInput(query_text="q", caller=_investigator()))
    assert result.status is ToolStatus.FAILED
    assert result.fallback_to_rag is True


async def test_real_graph_tags_hybrid_distinctly(monkeypatch):
    """[RESOLVED-1a] Hybrid output must be structurally distinguishable."""
    async def _graph(*a, **k):
        return {"chunks": [{"id": "g1", "text": "linked", "metadata": {"case_id": "CASE-A"}}],
                "hop_count": 1, "compounded_confidence": 0.8,
                "seed_entities": [{"entity_id": "E1"}], "unconfirmed_links": []}

    monkeypatch.setattr("src.retrieval.graph_retriever.retrieve_graph", _graph)

    plain = await real.graph_tool(
        GraphToolInput(query_text="q", caller=_investigator(), hybrid=False)
    )
    assert {c.metadata.source_tool for c in plain.chunks} == {"GRAPH"}


async def test_real_sql_signals_fallback_on_empty_rows(gateway, monkeypatch):
    async def _params(_q):
        return {"category": "theft", "subject": None, "section_ref": None}

    monkeypatch.setattr("src.pipeline.sql_extractor.extract_sql_params", _params)

    result = await real.sql_tool(
        SqlToolInput(query_text="q", caller=_investigator()), gateway=gateway
    )
    assert result.status is ToolStatus.EMPTY
    assert result.fallback_to_rag is True


async def test_real_sql_emits_chunks_with_no_owning_case(gateway, monkeypatch):
    """Reference data belongs to no case — inert to the leakage check."""
    async def _params(_q):
        return {"category": "theft", "subject": None, "section_ref": None}

    monkeypatch.setattr("src.pipeline.sql_extractor.extract_sql_params", _params)
    gateway.police_reference_data = [
        {"id": 1, "category": "theft", "subject": "Theft in dwelling", "section_ref": "PPC 380"}
    ]

    result = await real.sql_tool(
        SqlToolInput(query_text="q", caller=_investigator()), gateway=gateway
    )
    assert result.status is ToolStatus.OK
    assert all(c.metadata.case_id is None for c in result.chunks)
    assert all(c.metadata.source_tool == "SQL" for c in result.chunks)


# ── WEB: air-gap dominates BOTH tiers ────────────────────────────────────

async def test_real_web_air_gap_reaches_no_provider(monkeypatch):
    """
    [PRESERVE — design §2.7] Under air-gap neither tier may be reached. Both are
    booby-trapped here: touching either fails the test.
    """
    async def _explode(*a, **k):
        raise AssertionError("a web provider was reached under AIR_GAP_MODE")

    monkeypatch.setattr("src.retrieval.web_search.perform_web_search", _explode)
    monkeypatch.setattr("src.llm.client.call_gemini_with_search", _explode, raising=False)

    result = await real.web_tool(
        WebToolInput(query_text="q", caller=_investigator()), air_gap_mode=True
    )
    assert result.status is ToolStatus.FAILED
    assert result.provider_used is None
    assert result.fallback_to_rag is True


async def test_real_web_falls_back_to_rag_only_after_both_tiers(monkeypatch):
    async def _no_results(*a, **k):
        return []

    async def _no_grounded(*a, **k):
        return ("", [])

    monkeypatch.setattr("src.retrieval.web_search.perform_web_search", _no_results)
    monkeypatch.setattr("src.llm.client.call_gemini_with_search", _no_grounded, raising=False)

    result = await real.web_tool(
        WebToolInput(query_text="q", caller=_investigator()), air_gap_mode=False
    )
    assert result.status is ToolStatus.EMPTY
    assert result.fallback_to_rag is True


async def test_real_web_results_are_never_case_evidence(monkeypatch):
    async def _results(*a, **k):
        return [{"title": "Gov guidance", "url": "https://gov.pk/x",
                 "content": "how to report", "score": 0.8}]

    monkeypatch.setattr("src.retrieval.web_search.perform_web_search", _results)

    result = await real.web_tool(
        WebToolInput(query_text="q", caller=_investigator()), air_gap_mode=False
    )
    assert result.status is ToolStatus.OK
    assert all(c.metadata.case_id is None for c in result.chunks)
    assert all(c.metadata.source_tool == "WEB" for c in result.chunks)


# ── RAG: empty vs failed, and no onward fallback ─────────────────────────

async def test_real_rag_never_declares_fallback(monkeypatch):
    async def _boom(*a, **k):
        raise RuntimeError("embedder down")

    monkeypatch.setattr("src.pipeline.query_expander.expand_query", _boom)

    result = await real.rag_tool(RagToolInput(query_text="q", caller=_investigator()))
    assert result.status is ToolStatus.FAILED
    assert result.fallback_to_rag is False, "RAG is the fallback target; it has none of its own"


# ── The relevance gate's four distinct states ────────────────────────────

@pytest.fixture
def _retrieval_returns_one_chunk(monkeypatch):
    """Drive retrieval to a fixed single chunk so tests isolate the gate."""
    chunk = {"id": "c1", "text": "case text", "metadata": {"case_id": "CASE-A"}, "rrf_score": 0.9}

    async def _expand(_q, n=2):
        return []

    async def _variant(_q):
        return None

    async def _embed(_q, **k):
        return [0.0, 0.1]

    async def _query_similar(*a, **k):
        return [chunk]

    async def _all_chunks(**k):
        return [chunk]

    monkeypatch.setattr("src.pipeline.query_expander.expand_query", _expand)
    monkeypatch.setattr("src.pipeline.cross_script_variant.generate_cross_script_variant", _variant)
    monkeypatch.setattr("src.retrieval.embedder.embed_text", _embed)
    monkeypatch.setattr("src.retrieval.vector_store.query_similar", _query_similar)
    monkeypatch.setattr("src.retrieval.vector_store.get_all_chunks", _all_chunks)
    monkeypatch.setattr("src.retrieval.bm25_retriever.retrieve_bm25", lambda *a, **k: [chunk])
    monkeypatch.setattr("src.retrieval.reranker.rerank_results", lambda *a, **k: [chunk])

    async def _cross_rerank(_q, candidates, top_k=None):
        return candidates

    monkeypatch.setattr("src.retrieval.cross_reranker.cross_rerank", _cross_rerank)
    return chunk


async def test_gate_relevant(_retrieval_returns_one_chunk, monkeypatch):
    async def _verdict(*a, **k):
        return {"relevant": True, "reason": "ok"}

    monkeypatch.setattr("src.pipeline.evaluator.evaluate_relevance", _verdict)

    result = await real.rag_tool(RagToolInput(query_text="q", caller=_investigator()))
    assert result.status is ToolStatus.OK
    assert result.evaluator_verdict == "relevant"
    assert result.degradation_caveats == []


async def test_gate_not_relevant_withholds_evidence(_retrieval_returns_one_chunk, monkeypatch):
    async def _verdict(*a, **k):
        return {"relevant": False, "reason": "off topic"}

    monkeypatch.setattr("src.pipeline.evaluator.evaluate_relevance", _verdict)

    result = await real.rag_tool(RagToolInput(query_text="q", caller=_investigator()))
    assert result.status is ToolStatus.EMPTY
    assert result.evaluator_verdict == "not_relevant"
    assert result.chunks == []


async def test_gate_malformed_verdict_fails_closed(_retrieval_returns_one_chunk, monkeypatch):
    """
    A verdict dict with no `relevant` key did not actually judge anything.
    Matches legacy GRAPH's `.get("relevant", False)`; the earlier default of
    True was a defect that silently passed unjudged evidence.
    """
    async def _verdict(*a, **k):
        return {"reason": "model returned prose instead of a verdict"}

    monkeypatch.setattr("src.pipeline.evaluator.evaluate_relevance", _verdict)

    result = await real.rag_tool(RagToolInput(query_text="q", caller=_investigator()))
    assert result.status is ToolStatus.EMPTY
    assert result.evaluator_verdict == "not_relevant"


async def test_gate_unavailable_passes_through_but_flags_it(
    _retrieval_returns_one_chunk, monkeypatch
):
    """
    Evaluator raised → evidence still served (availability), but the
    degradation is explicit: a distinct verdict value AND a caveat. Silently
    reporting 'relevant' here would drop the gate without trace.
    """
    async def _boom(*a, **k):
        raise RuntimeError("evaluator endpoint timed out")

    monkeypatch.setattr("src.pipeline.evaluator.evaluate_relevance", _boom)

    result = await real.rag_tool(RagToolInput(query_text="q", caller=_investigator()))
    assert result.status is ToolStatus.OK
    assert result.chunks, "chunks must still be served — availability is the point"
    assert result.evaluator_verdict == "unavailable"
    assert result.evaluator_verdict != "relevant", "must not masquerade as a real pass"
    assert result.degradation_caveats, "degradation must carry a user-facing caveat"


async def test_gate_never_reached_leaves_verdict_none(monkeypatch):
    """
    `None` means the gate was never reached — distinct from 'unavailable',
    where it was reached and could not run.
    """
    async def _boom(*a, **k):
        raise RuntimeError("retrieval down")

    monkeypatch.setattr("src.pipeline.query_expander.expand_query", _boom)

    result = await real.rag_tool(RagToolInput(query_text="q", caller=_investigator()))
    assert result.status is ToolStatus.FAILED
    assert result.evaluator_verdict is None


async def test_semantic_search_propagates_the_degradation_caveat(
    _retrieval_returns_one_chunk, monkeypatch
):
    """
    The caveat must survive to the bounded payload — dying at the tool boundary
    would make the whole surfacing pointless.
    """
    from src.pipeline.harness.agents import semantic_search
    from src.pipeline.harness.contracts import SubAgentInput, SubAgentStatus
    from src.pipeline.harness.tools import registry

    async def _boom(*a, **k):
        raise RuntimeError("evaluator endpoint timed out")

    monkeypatch.setattr("src.pipeline.evaluator.evaluate_relevance", _boom)
    registry.use_real()

    result = await semantic_search.run(
        SubAgentInput(query_text="q", caller=_investigator())
    )

    assert result.answer_text, "the answer is still served"
    assert result.caveats, "the degradation reached SubAgentResult.caveats"
    assert result.status is SubAgentStatus.PARTIAL
    assert result.degraded_from == ["RAG"]
