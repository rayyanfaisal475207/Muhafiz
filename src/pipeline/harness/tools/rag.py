"""
RAG tool — src/pipeline/harness/tools/rag.py (Phase 0, foundation layer).

Thin adapter over the EXISTING retrieval pipeline (AGENT_HARNESS_DESIGN.md
§2.1): `embed_text()` -> `query_similar()` + `fulltext_index.candidate_pool()`
(Milestone A2 — persistent Postgres tsvector/GIN index, replacing the
`get_all_chunks()` full-corpus fetch this used to call) ->
`retrieve_bm25()` -> `rerank_results()` (RRF) -> `cross_rerank()`
(bge-reranker-v2-m3) -> `evaluate_relevance()`, plus the evaluator-feedback
retry loop (`rewrite_for_retry()`) that already surrounds those calls in
`orchestrator.py`'s RAG route today. No new retrieval logic — every call
below is the same function, called the same way, that orchestrator.py
already calls; this module only gives that existing sequence a reusable,
typed entry point instead of it living inline in one giant coroutine.

[PRESERVE — design §2.1] RAG is the FALLBACK TARGET for GRAPH/GRAPH_HYBRID/
SQL/WEB — it has no fallback of its own. Retry is internal
(evaluator-feedback-driven query rewrite, bounded by `config.MAX_RETRIES`);
exhausting it abstains (`status=EMPTY`) rather than reaching for WEB. That
removal was a deliberate scope decision upstream (orchestrator.py's own
`enable_web_search` docstring) — this wrapper preserves it by construction:
it simply never imports or calls anything web-related.

[PRESERVE — design §2.1] Scoping is case-assignment-based, NOT role-based:
no role gate here, matching `RagToolInput`'s docstring ("Any caller who can
see the case can search it").

SCOPE NOTE — case-record synthetic chunk. `orchestrator.py`'s RAG/GRAPH/
GRAPH_HYBRID routes each inject `_case_record_chunk(gateway, case_id)` (the
case's structured Postgres fields, e.g. status/IO/station, as one synthetic
citable chunk) before the evaluator, so a case whose ingested documents
don't restate that data isn't wrongly judged "not relevant." Design §2.1's
own function list for what this tool wraps stops at `evaluate_relevance()`
and does not mention it, so it is deliberately NOT reproduced here — adding
a Postgres case-record dependency the interfaces doc's `RagToolInput` never
names would be scope creep past the documented wrap boundary. Flagged here,
not silently dropped, in case a later phase decides a sub-agent (Case
Summarization is the natural owner) should compose it back in.

SCOPE NOTE — case vs. project scoping. [UPDATED — contract retrofit,
AGENT_HARNESS_IMPLEMENTATION_PLAN.md §10.3] `CallerContext` carries
`active_case_id` only, not a `project_id`; Phase 0 shipped with this
wrapper reading `active_case_id` + `include_global` alone for that reason.
`ExecutionContext` (types.py, plan §10.1) now carries `project_id`
alongside the nested `CallerContext`, restoring a carrier for the
project/global precedence rule this wrapper previously had to drop. This
wrapper's `where` filter now also folds in `execution.project_id` when
present. It never passes an unscoped (`None`/`{}`) filter to
`query_similar`/`fulltext_index.candidate_pool` — doing so is the exact multi-tenant leak
`orchestrator.py`'s own comments document (Phase 8, Bug 1) — so the one
combination that still can't legitimately scope (no case, no project,
`include_global=False`) returns `EMPTY` without calling retrieval at all,
rather than searching unfiltered.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import Field

from src import config
from src.pipeline.cross_script_variant import generate_cross_script_variant
from src.pipeline.evaluator import evaluate_relevance
from src.pipeline.harness.types import (
    CallerContext,
    ChunkMetadata,
    EvidenceChunk,
    ToolError,
    ToolInput,
    ToolResult,
    ToolStatus,
)
from src.pipeline.query_expander import expand_query
from src.pipeline.query_rewriter import rewrite_for_retry
from src.retrieval.bm25_retriever import retrieve_bm25
from src.retrieval.cross_reranker import cross_rerank
from src.retrieval.embedder import embed_text
from src.retrieval.fulltext_index import candidate_pool as bm25_candidate_pool
from src.retrieval.reranker import rerank_results
from src.retrieval.vector_store import cap_case_diversity, query_similar

logger = logging.getLogger(__name__)


class RagToolInput(ToolInput):
    """
    Retrieve passages relevant to `query_text` within the caller's scope.

    Deliberately says nothing about HOW. Embedding model, fusion strategy,
    reranking, and candidate counts are all free to change without touching
    this contract.

    [PRESERVE — design §2.1] Scoping is case-assignment-based, NOT
    role-based: RAG carries no role gate. Any caller who can see the case
    can search it.
    """

    top_k: Optional[int] = Field(
        default=None, description="Caller's requested result count. None = tool default."
    )
    include_global: bool = Field(
        default=True,
        description=(
            "Whether shared/global reference material is eligible alongside "
            "case-scoped evidence. Composition with project/global scoping "
            "follows existing precedence rules."
        ),
    )


class RagToolResult(ToolResult):
    """
    [PRESERVE — design §2.1] RAG is the FALLBACK TARGET for GRAPH,
    GRAPH_HYBRID, SQL and WEB — it has no onward fallback of its own, so
    `fallback_to_rag` is pinned False.

    Retry is internal (evaluator-feedback-driven query rewrite, bounded by
    a retry budget). Exhausting it abstains — it does NOT silently reach
    for web search. That removal was a deliberate scope decision, not a
    bug: WEB is reachable only via explicit router classification or an
    explicit caller toggle. Do not "restore" it.
    """

    fallback_to_rag: Literal[False] = False
    retries_used: int = Field(
        default=0, description="Internal retry loop iterations consumed. Observability only."
    )
    evaluator_verdict: Optional[Literal["relevant", "not_relevant", "unavailable"]] = Field(
        default=None,
        description=(
            "Relevance gate outcome on the final attempt. `not_relevant` "
            "after retry exhaustion yields status=EMPTY, not FAILED — "
            "retrieval worked, the evidence just did not answer the "
            "question.\n\n"
            "[Reconciliation fix — harness-reconciliation Unit 3] "
            "`unavailable` — THE GATE COULD NOT RUN (the evaluator raised or "
            "returned malformed output). Chunks are passed through UNVETTED "
            "so a flaky evaluator does not take retrieval down with it, but "
            "the evidence carries no relevance guarantee. Previously this "
            "case was silently reported as `relevant` (fail-open with no "
            "distinguishing signal) — a caller could not tell 'the "
            "evaluator confirmed this' from 'the evaluator never actually "
            "ran'. Always accompanied by a caveat in `degradation_caveats`, "
            "which the composing sub-agent MUST propagate into "
            "`SubAgentResult.caveats`."
        ),
    )
    degradation_caveats: list[str] = Field(
        default_factory=list,
        description=(
            "[Reconciliation fix — Unit 3] User-facing qualifications about "
            "HOW this result was produced — currently, that the relevance "
            "gate could not run. Non-empty iff evaluator_verdict == "
            "'unavailable'."
        ),
    )


def _build_where(
    caller: CallerContext, include_global: bool, project_id: Optional[str] = None
) -> dict:
    """
    Mirrors orchestrator.py's RAG-route where-clause precedence (Module
    4.1): a case, when active, always wins; otherwise a project scope
    (when the caller's `ExecutionContext.project_id` is set — see module
    docstring's project-scoping note, plan §10.3) narrows to that project;
    otherwise fall back to global-only. Never returns an empty dict when
    that would mean "unscoped" — see `_chunk_pool_scope`.

    `project_id` defaults to `None` for backward compatibility with
    existing callers (e.g. GRAPH_HYBRID's within-case composition, which
    passes no project scope) — `None` means "no project scoping applied",
    the same behavior this wrapper had before `ExecutionContext` existed.
    """
    where: dict = {}
    if caller.active_case_id:
        where["case_id"] = caller.active_case_id
    elif project_id:
        where["project_id"] = project_id
    elif include_global:
        where["is_global"] = True
    return where


async def _retrieve_candidates(
    query_text: str, where: dict, fetch_top_k: int, final_top_k: int, is_cross_case: bool
) -> tuple[list[dict], list[dict]]:
    """
    The retrieval half of the RAG primitive — query expansion + cross-script
    variant -> embed -> vector search (deduped, diversity-capped) -> full
    scoped candidate pool -> BM25 — stopping short of RRF fusion.

    Factored out so GRAPH_HYBRID (src/pipeline/harness/tools/graph.py) can
    reuse it instead of re-implementing these steps inline, per
    SUBAGENT_INTERFACES.md §1.3 [RESOLVED-1]: "Compose it as 'GRAPH tool +
    RAG tool, RRF-merged' ... to avoid the current duplication
    (GRAPH_HYBRID re-implements query expansion/cross-script-variant/
    BM25-pool-fetch inline rather than sharing RAG's version)." Returns
    (semantic_results, bm25_results) — the caller RRF-fuses them, possibly
    together with graph chunks.
    """
    expanded_queries = await expand_query(query_text, n=2)
    cross_script_query = await generate_cross_script_variant(query_text)
    all_queries = [query_text] + expanded_queries + (
        [cross_script_query] if cross_script_query else []
    )

    embeddings = [await embed_text(q) for q in all_queries]

    semantic_results: list[dict] = []
    seen_ids: set[str] = set()
    for q, emb in zip(all_queries, embeddings):
        for chunk in await query_similar(q, emb, top_k=fetch_top_k, where=where):
            chunk_id = chunk.get("id")
            if chunk_id not in seen_ids:
                seen_ids.add(chunk_id)
                semantic_results.append(chunk)

    if is_cross_case:
        semantic_results = cap_case_diversity(
            semantic_results,
            per_case_cap=config.CROSS_CASE_PER_CASE_CAP,
            total_cap=final_top_k,
        )

    combined_query = " ".join(all_queries)
    try:
        # Milestone A2: persistent-index-backed candidate pool — see
        # src/retrieval/fulltext_index.py and orchestrator.py's own
        # equivalent call sites.
        full_candidate_pool = await bm25_candidate_pool(combined_query, where=where)
    except Exception as pool_exc:
        logger.error(
            "RAG tool: fetching full BM25 candidate pool failed: %s. "
            "Falling back to semantic_results only.", pool_exc,
        )
        full_candidate_pool = semantic_results
    bm25_results = retrieve_bm25(combined_query, full_candidate_pool, top_k=final_top_k)

    return semantic_results, bm25_results


def _to_evidence_chunk(raw: dict) -> EvidenceChunk:
    meta = dict(raw.get("metadata") or {})
    return EvidenceChunk(
        id=raw["id"],
        text=raw.get("text", ""),
        score=raw.get("rerank_score", raw.get("rrf_score")),
        metadata=ChunkMetadata(
            source_tool="RAG",
            case_id=meta.get("case_id"),
            source_file=meta.get("source"),
            **{k: v for k, v in meta.items() if k not in ("case_id", "source")},
        ),
    )


async def rag_tool(tool_input: RagToolInput) -> RagToolResult:
    """
    The RAG primitive: embed -> vector search + BM25 -> RRF -> cross-rerank
    -> evaluate, retrying with evaluator feedback up to `config.MAX_RETRIES`
    times before abstaining.
    """
    caller = tool_input.execution.caller
    where = _build_where(caller, tool_input.include_global, tool_input.execution.project_id)
    if not where:
        # No case to scope to, and global material explicitly excluded —
        # nothing legitimate to search. Never fall through to an unscoped
        # query_similar/fulltext_index.candidate_pool call (see module docstring).
        return RagToolResult(status=ToolStatus.EMPTY)

    is_cross_case = not caller.active_case_id
    top_k = tool_input.top_k or config.TOP_K_RETRIEVAL
    fetch_top_k = top_k * config.CROSS_CASE_RETRIEVAL_MULTIPLIER if is_cross_case else top_k

    current_query = tool_input.query_text
    evaluator_feedback: Optional[str] = None
    retry_count = 0

    while retry_count <= config.MAX_RETRIES:
        if retry_count > 0 and evaluator_feedback:
            try:
                current_query = await rewrite_for_retry(
                    original_message=tool_input.query_text,
                    previous_query=current_query,
                    evaluator_feedback=evaluator_feedback,
                )
            except Exception as exc:
                logger.error("RAG tool: retry rewriter failed: %s", exc)
                # Fall through with the unchanged current_query, matching
                # orchestrator.py's own retry-rewriter exception handling.

        try:
            semantic_results, bm25_results = await _retrieve_candidates(
                current_query, where, fetch_top_k, top_k, is_cross_case
            )
        except Exception as retr_exc:
            logger.error("RAG tool: retrieval infrastructure failed: %s", retr_exc)
            return RagToolResult(
                status=ToolStatus.FAILED,
                error=ToolError(kind="upstream_failure", message=str(retr_exc)),
                retries_used=retry_count,
            )

        fused = rerank_results(semantic_results, bm25_results, top_k=top_k)

        try:
            reranked = await cross_rerank(current_query, fused, top_k=config.TOP_K_RERANK)
        except Exception as exc:
            logger.error("RAG tool: cross-encoder rerank failed: %s. Falling back to RRF order.", exc)
            reranked = fused[: config.TOP_K_RERANK]

        # [Reconciliation fix — harness-reconciliation Unit 3] Track whether
        # the evaluator itself raised, distinct from it returning a genuine
        # "relevant" verdict. Previously both cases returned identically
        # (evaluator_verdict="relevant"), so a caller could not tell "the
        # evaluator confirmed this evidence" from "the evaluator never ran
        # and we proceeded anyway" — the exact fail-open-without-a-signal
        # gap AGENT_HARNESS_DESIGN.md's own hedging/confidence discussion
        # warns against for a different field. Chunks still pass through
        # unvetted either way (a flaky evaluator must not take retrieval
        # down with it) — what changes is that the caller can now see it.
        evaluator_unavailable = False
        try:
            evaluation = await evaluate_relevance(tool_input.query_text, current_query, reranked)
        except Exception as exc:
            logger.error("RAG tool: evaluator failed: %s", exc)
            evaluation = {"relevant": True, "reason": "Evaluator failed, proceeding"}
            evaluator_unavailable = True

        if evaluation.get("relevant", False):
            return RagToolResult(
                status=ToolStatus.OK,
                chunks=[_to_evidence_chunk(c) for c in reranked],
                retries_used=retry_count,
                evaluator_verdict="unavailable" if evaluator_unavailable else "relevant",
                degradation_caveats=(
                    ["The relevance of these results could not be automatically verified."]
                    if evaluator_unavailable else []
                ),
            )

        evaluator_feedback = evaluation.get("reason")
        retry_count += 1

    # Retry budget exhausted without a "relevant" verdict — abstain, not an
    # error. [PRESERVE — design §2.1] Never reaches for WEB from here.
    return RagToolResult(
        status=ToolStatus.EMPTY,
        retries_used=retry_count,
        evaluator_verdict="not_relevant",
    )


rag_tool.name = "RAG"
