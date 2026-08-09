"""
Project scope must reach retrieval, and its absence must not silently widen.

`CallerContext.project_id` is caller scope, like `active_case_id`. It lives
there rather than on `SubAgentInput` so it threads to tools automatically at
every hop, instead of each sub-agent remembering to forward it.

The failure mode this guards is asymmetric and easy to miss: `_build_where`
simply OMITS the `project_id` clause when it is absent, so a dropped project
scope returns a BROADER filter, not an empty one. Retrieval widens rather than
failing — silently, with no error and no empty result to notice. Investigative
Analysis previously declared a `project_id` parameter that nothing passed and
nothing read, so its project-scoped retrieval had been widening all along.
"""
from __future__ import annotations

import pytest

from src.pipeline.harness.contracts import CallerContext, RagToolInput, Role
from src.pipeline.harness.tools.real import _build_where


def _caller(project_id: str | None = "PROJ-1", case_id: str | None = "CASE-A") -> CallerContext:
    return CallerContext(
        user_id="u1", role=Role.INVESTIGATOR,
        active_case_id=case_id, project_id=project_id,
    )


# ── The widening this fix closes ─────────────────────────────────────────

def test_dropping_project_id_widens_rather_than_narrows():
    """
    Pins WHY this matters. An omitted project scope does not produce an empty
    or failing filter — it produces a broader one, which is the version that
    goes unnoticed.
    """
    scoped = _build_where("CASE-A", "PROJ-1", True)
    unscoped = _build_where("CASE-A", None, True)

    assert scoped == {"project_id": "PROJ-1", "case_id": "CASE-A"}
    assert unscoped == {"case_id": "CASE-A"}
    assert set(unscoped) < set(scoped), "dropping project_id must WIDEN the filter"


def test_case_and_project_are_anded_together():
    where = _build_where("CASE-A", "PROJ-1", True)

    assert where["case_id"] == "CASE-A"
    assert where["project_id"] == "PROJ-1"


def test_global_fallback_only_when_neither_scope_is_present():
    """`is_global` must not appear alongside a real scope — that would widen too."""
    assert _build_where(None, None, True) == {"is_global": True}
    assert "is_global" not in _build_where("CASE-A", None, True)
    assert "is_global" not in _build_where(None, "PROJ-1", True)


# ── Threading through the tool ───────────────────────────────────────────

@pytest.fixture
def capture_where(monkeypatch):
    """Capture the scope filter the RAG tool actually sends to retrieval."""
    seen: dict = {}

    async def _expand(_q, n=2):
        return []

    async def _variant(_q):
        return None

    async def _embed(_q, **k):
        return [0.0, 0.1]

    async def _query_similar(q, emb, top_k=None, where=None, **k):
        seen["where"] = where
        return []

    async def _all_chunks(where=None, **k):
        seen.setdefault("where", where)
        return []

    async def _cross_rerank(_q, candidates, top_k=None):
        return list(candidates)

    async def _evaluate(*a, **k):
        return {"relevant": False, "reason": "none"}

    monkeypatch.setattr("src.pipeline.query_expander.expand_query", _expand)
    monkeypatch.setattr(
        "src.pipeline.cross_script_variant.generate_cross_script_variant", _variant)
    monkeypatch.setattr("src.retrieval.embedder.embed_text", _embed)
    monkeypatch.setattr("src.retrieval.vector_store.query_similar", _query_similar)
    monkeypatch.setattr("src.retrieval.vector_store.get_all_chunks", _all_chunks)
    monkeypatch.setattr("src.retrieval.bm25_retriever.retrieve_bm25", lambda *a, **k: [])
    monkeypatch.setattr("src.retrieval.reranker.rerank_results", lambda *a, **k: [])
    monkeypatch.setattr("src.retrieval.cross_reranker.cross_rerank", _cross_rerank)
    monkeypatch.setattr("src.pipeline.evaluator.evaluate_relevance", _evaluate)
    return seen


async def test_caller_project_id_reaches_the_scope_filter(capture_where):
    """The point of putting it on CallerContext: no explicit forwarding needed."""
    from src.pipeline.harness.tools.real import rag_tool

    await rag_tool(RagToolInput(query_text="q", caller=_caller()))

    assert capture_where["where"].get("project_id") == "PROJ-1"


async def test_no_caller_project_means_no_project_clause(capture_where):
    """A caller genuinely outside any project must not gain a phantom scope."""
    from src.pipeline.harness.tools.real import rag_tool

    await rag_tool(RagToolInput(query_text="q", caller=_caller(project_id=None)))

    assert "project_id" not in capture_where["where"]


async def test_explicit_parameter_overrides_caller_scope(capture_where):
    """
    The explicit `project_id` argument still wins where a caller needs to scope
    one call differently — caller scope is the DEFAULT, not a hard override.
    """
    from src.pipeline.harness.tools.real import rag_tool

    await rag_tool(
        RagToolInput(query_text="q", caller=_caller(project_id="PROJ-1")),
        project_id="PROJ-OVERRIDE",
    )

    assert capture_where["where"]["project_id"] == "PROJ-OVERRIDE"


# ── Threading through a sub-agent, unforwarded ───────────────────────────

async def test_project_scope_survives_a_sub_agent_that_forwards_nothing(capture_where):
    """
    THE REGRESSION THIS CLOSES. Investigative Analysis forwards no project_id —
    it has no such parameter any more. Scope must still reach retrieval,
    because it rides on the caller rather than on the call.
    """
    from src.pipeline.harness.agents import investigative_analysis
    from src.pipeline.harness.contracts import SubAgentInput
    from src.pipeline.harness.tools import registry

    registry.use_real()
    await investigative_analysis.run(
        SubAgentInput(query_text="analyse", caller=_caller())
    )

    assert capture_where["where"].get("project_id") == "PROJ-1", (
        "project scope was lost between the caller and retrieval — the exact "
        "silent widening CallerContext.project_id exists to prevent"
    )


def test_investigative_analysis_has_no_dead_project_id_parameter():
    """
    It declared one that nothing passed and nothing read, so project scoping
    silently did nothing. Removed — the parameter's absence is the fix.
    """
    import inspect

    from src.pipeline.harness.agents import investigative_analysis

    params = inspect.signature(investigative_analysis.run).parameters
    assert "project_id" not in params
