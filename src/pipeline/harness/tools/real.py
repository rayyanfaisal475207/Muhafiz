"""
Real tool primitives — thin adapters over the existing production retrieval,
graph, gateway, and web-search code.

DESIGN RULE: these wrap existing functions VERBATIM. No new retrieval or
generation logic lives here — only the adapter that exposes current behaviour
behind the harness contracts in `contracts.py` (transcribed from
docs/SUBAGENT_INTERFACES.md §1). If a behaviour looks wrong while wiring, it is
logged in AGENT_HARNESS_DESIGN.md §7 and left alone; this module is not the
place to fix the legacy pipeline.

`src/pipeline/orchestrator.py` is untouched and keeps running its own legacy
path. The harness runs ALONGSIDE it, not in place of it.

Imports of production modules are deliberately done INSIDE each function rather
than at module top level. Two reasons:
  * `tests/harness/test_isolation.py` asserts the harness package does not
    import live pipeline code at module scope, keeping the skeleton importable
    and testable on its own;
  * it keeps import cost off the path of any caller that only uses a subset of
    the tools.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from src.pipeline.harness.contracts import (
    CROSS_CASE_ROLES,
    ChunkMetadata,
    EvidenceChunk,
    GraphToolInput,
    GraphToolResult,
    RagToolInput,
    RagToolResult,
    SqlToolInput,
    SqlToolResult,
    ToolError,
    ToolStatus,
    WebToolInput,
    WebToolResult,
    XAggToolInput,
    XAggToolResult,
    XGraphToolInput,
    XGraphToolResult,
    XNetworkToolInput,
    XNetworkToolResult,
)
from src.pipeline.harness.events import EventRecorder

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════════
# Shared helpers
# ══════════════════════════════════════════════════════════════════════════

def _build_where(case_id: Optional[str], project_id: Optional[str], include_global: bool) -> dict:
    """
    Replicate the orchestrator's scoping precedence EXACTLY
    (orchestrator.py's `where_clause` construction).

    Filter on case_id alone when present; fall back to `is_global=True` only
    when there is neither a project_id nor a case_id. The older
    project_id-or-is_global fallback excluded real case evidence
    (`is_global=False`) from results — do not "simplify" this back.
    """
    where: dict = {}
    if project_id:
        where["project_id"] = project_id
    if case_id:
        where["case_id"] = case_id
    if not project_id and not case_id and include_global:
        where["is_global"] = True
    return where


def _to_evidence(raw: dict, source_tool: str, index: int) -> EvidenceChunk:
    """
    Convert a production chunk dict into the harness's `EvidenceChunk`.

    Production chunks are loose dicts with `id`/`text`/`metadata` plus whatever
    scoring key the producing stage attached (`rrf_score`, `bm25_score`,
    `cross_score`, `graph_confidence`, ...). The contract requires
    `metadata.source_tool` on every chunk, and treats `score` as opaque.

    Unknown metadata keys are preserved: `ChunkMetadata` allows extras, so
    per-source provenance already carried today (`graph_confidence`,
    `conflict_basis`, ...) survives the conversion rather than being dropped.
    """
    meta = dict(raw.get("metadata") or {})
    # Contract fields are set explicitly; everything else rides along as extras.
    meta.pop("source_tool", None)
    confidence = raw.get("graph_confidence", meta.pop("graph_confidence", None))
    score = (
        raw.get("cross_score")
        if raw.get("cross_score") is not None
        else raw.get("rrf_score", raw.get("bm25_score", raw.get("score")))
    )
    chunk = EvidenceChunk(
        id=str(raw.get("id") or f"{source_tool.lower()}-{index}"),
        text=raw.get("text") or "",
        score=score,
        metadata=ChunkMetadata(
            source_tool=source_tool,
            case_id=meta.pop("case_id", None),
            source_file=meta.pop("source_file", None),
            confidence=confidence,
            **meta,
        ),
    )

    # ── COMPATIBILITY SHIM: verifier.py reads `graph_confidence` TOP-LEVEL ──
    #
    # `_check_hedging()` (verifier.py) reads `chunk.get("graph_confidence")`
    # off the chunk dict's TOP LEVEL, not out of `metadata`. Normalizing that
    # value into `metadata.confidence` above — which is the right shape for the
    # contract — silently broke the check for every harness-produced chunk:
    # `.get("graph_confidence")` returned None, hit the check's
    # `if gc is None: continue` guard, and low-confidence graph evidence sailed
    # through UNHEDGED. Verified: the identical chunk flagged correctly in the
    # legacy shape and produced zero issues in the harness shape.
    #
    # The hedging gate has no backstop — the LLM judge does not reliably catch
    # missing hedges — so a silently-skipped check is a real safety hole, not a
    # cosmetic mismatch. It has not bitten only because nothing routes to the
    # harness yet.
    #
    # So the field is emitted in BOTH places: `metadata.confidence` is the
    # contract's shape and what harness code reads; `graph_confidence` exists
    # solely to satisfy verifier.py's positional/top-level expectation. This is
    # a DELIBERATE CONCESSION to that coupling, not a regression of the
    # metadata normalization.
    #
    # Removable by AGENT_HARNESS_DESIGN.md §7's Part B (the `confidence_state`
    # sentinel), which reworks how the verifier reads confidence and can drop
    # this field as part of that change. Do not remove it before then —
    # deleting it silently disables the hedging check again, and no test
    # outside test_hedging_shim.py would notice.
    if confidence is not None:
        chunk.graph_confidence = confidence

    return chunk


async def _deny_cross_case(
    tool: str,
    tool_input,
    gateway: Any,
    events: Optional[EventRecorder],
) -> Optional[ToolError]:
    """
    Steps 1 and 2 of the cross-case ordering contract
    ([PRESERVE — design §2.3, §4.3]).

        1. Check role against CROSS_CASE_ROLES.
        2. On failure: audit `authorization_violation`, return DENIED.
        3. ONLY on success may the caller arm cross-case / RLS scope.

    Arming strictly after the check is the fix for a documented historical bug
    where the RLS cross-case bypass flag was armed as soon as the router
    classified a query as cross-case — before any role check ran, and never
    reset on denial. Never hoist the arming above this call.

    NOTE: the underlying production functions (`retrieve_graph`,
    `run_aggregate`, `run_network_query`) each perform this same check
    internally and raise `PermissionError`. This wrapper check is deliberately
    redundant, not a replacement: it lets the tool return a typed DENIED result
    instead of raising, and it guarantees the audit record is written even if a
    future refactor of the production function moves its own check.
    """
    if tool_input.caller.role in CROSS_CASE_ROLES:
        return None

    if gateway is not None:
        try:
            await gateway.log_audit_event(
                event_type="authorization_violation",
                user_id=tool_input.caller.user_id,
                case_id=None,
                details={
                    "query": tool_input.query_text,
                    "role": tool_input.caller.role.value,
                    "route": tool,
                },
            )
        except Exception as exc:  # audit failure must not mask the denial
            logger.error("Failed to audit log unauthorized %s attempt: %s", tool, exc)

    if events:
        await events.emit(
            f"tool:{tool.lower()}", "error",
            f"{tool} refused: cross-case access requires supervisor role or higher",
        )
    return ToolError(
        kind="permission_denied",
        message=f"Cross-case {tool} queries require supervisor role or higher.",
    )


# ══════════════════════════════════════════════════════════════════════════
# §1.2 — RAG
# ══════════════════════════════════════════════════════════════════════════

async def rag_tool(
    tool_input: RagToolInput,
    events: Optional[EventRecorder] = None,
    project_id: Optional[str] = None,
) -> RagToolResult:
    """
    Real hybrid retrieval: query expansion + cross-script variant → embed →
    Chroma vector search → full-pool BM25 → RRF fuse → cross-encoder rerank →
    relevance evaluator.

    Mirrors the orchestrator's RAG leg, including two fixes that are easy to
    lose when re-composing these calls:
      * BM25 searches the FULL scoped candidate pool (`get_all_chunks`), not
        just what vector search already returned, so a keyword-relevant chunk
        vector search missed can still be rescued;
      * a cross-script query variant is generated so an Urdu-script or English
        query gets a fair BM25 shot at the corpus's opposite-script documents.

    [PRESERVE — design §2.1] No role gate — scoping is case-assignment-based.
    `fallback_to_rag` is pinned False by the contract: RAG is the fallback
    TARGET and has no onward fallback. Exhausting relevance abstains; it does
    NOT reach for web search.
    """
    from src import config
    from src.pipeline.cross_script_variant import generate_cross_script_variant
    from src.pipeline.evaluator import evaluate_relevance
    from src.pipeline.query_expander import expand_query
    from src.retrieval.bm25_retriever import retrieve_bm25
    from src.retrieval.cross_reranker import cross_rerank
    from src.retrieval.embedder import embed_text
    from src.retrieval.reranker import rerank_results
    from src.retrieval.vector_store import get_all_chunks, query_similar

    if events:
        await events.emit("tool:rag", "active", "Searching case documents")

    query = tool_input.query_text
    top_k = tool_input.top_k or config.TOP_K_RERANK
    where = _build_where(
        tool_input.caller.active_case_id, project_id, tool_input.include_global
    )

    try:
        import asyncio

        expanded = await expand_query(query, n=2)
        cross_script = await generate_cross_script_variant(query)
        all_queries = [query] + expanded + ([cross_script] if cross_script else [])

        embeddings = await asyncio.gather(*[embed_text(q) for q in all_queries])
        search_results = await asyncio.gather(*[
            query_similar(q, emb, top_k=config.TOP_K_RETRIEVAL, where=where)
            for q, emb in zip(all_queries, embeddings)
        ])

        vector_results: list[dict] = []
        seen: set = set()
        for res in search_results:
            for chunk in res:
                cid = chunk.get("id")
                if cid not in seen:
                    seen.add(cid)
                    vector_results.append(chunk)

        try:
            pool = await get_all_chunks(where=where)
        except Exception as pool_exc:
            logger.error(
                "Fetching full BM25 candidate pool failed: %s. "
                "Falling back to vector_results only for this query.", pool_exc
            )
            pool = vector_results

        bm25_results = retrieve_bm25(
            " ".join(all_queries), pool, top_k=config.TOP_K_RETRIEVAL
        )
        fused = rerank_results(vector_results, bm25_results, top_k=config.TOP_K_RETRIEVAL)
        reranked = await cross_rerank(query, fused, top_k=top_k)

    except Exception as exc:
        logger.error("RAG tool retrieval failed: %s", exc)
        if events:
            await events.emit("tool:rag", "error", f"Document search failed: {exc}")
        return RagToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
        )

    if not reranked:
        if events:
            await events.emit("tool:rag", "done", "Document search returned no passages")
        return RagToolResult(status=ToolStatus.EMPTY, evaluator_verdict="not_relevant")

    # ── Relevance gate ───────────────────────────────────────────────────
    # Three outcomes, deliberately distinguished (see RagToolResult):
    #
    #   ran + relevant      → OK
    #   ran + not relevant  → EMPTY. Retrieval worked; the evidence just does
    #                         not answer the question. Not a failure.
    #   COULD NOT RUN       → OK, chunks passed through UNVETTED, verdict
    #                         'unavailable', plus a caveat.
    #
    # On the last case: an evaluator that cannot run is NOT evidence of
    # relevance. Passing chunks through is a deliberate availability choice —
    # a flaky local model endpoint should not take retrieval down with it —
    # but it is a real degradation and must not be silent. The Verifier
    # downstream does NOT backstop this: it checks grounding (do claims trace
    # to cited chunks), not relevance, so it will happily pass a well-grounded
    # answer built from off-topic evidence.
    #
    # DEVIATION FROM LEGACY RAG, documented in AGENT_HARNESS_DESIGN.md §7:
    # legacy orchestrator.py's RAG route wraps this call in NO error handling
    # at all (lines ~1069, ~1813) — an evaluator exception propagates and takes
    # down the whole pipeline turn. Legacy GRAPH (line ~898) *does* fail open,
    # with `{"relevant": True, "reason": "Evaluator failed, proceeding"}`. This
    # adapter follows GRAPH's pattern rather than RAG's, which IS a behaviour
    # change for the RAG path — a deliberate one, not an oversight.
    evaluator_caveats: list[str] = []
    try:
        verdict = await evaluate_relevance(query, query, reranked)
        # Fail CLOSED on a malformed verdict, matching legacy GRAPH's
        # `.get("relevant", False)`. A verdict dict missing the key is not a
        # pass — the gate did not actually judge anything.
        relevant = bool(verdict.get("relevant", False))
        verdict_value = "relevant" if relevant else "not_relevant"
    except Exception as exc:
        logger.error(
            "Relevance evaluation could not run (%s) — passing %d chunk(s) through "
            "UNVETTED with an explicit caveat.", exc, len(reranked)
        )
        relevant = True
        verdict_value = "unavailable"
        evaluator_caveats.append(
            "The relevance check could not run for this search, so the supporting "
            "passages were not screened for topical relevance before being used."
        )
        if events:
            await events.emit(
                "tool:rag", "retry",
                "Relevance check unavailable — passages passed through unvetted",
            )

    if not relevant:
        if events:
            await events.emit(
                "tool:rag", "done", "Retrieved passages judged not relevant"
            )
        return RagToolResult(status=ToolStatus.EMPTY, evaluator_verdict="not_relevant")

    chunks = [_to_evidence(c, "RAG", i) for i, c in enumerate(reranked, start=1)]
    if events:
        await events.emit("tool:rag", "done", f"Retrieved {len(chunks)} passages")
    return RagToolResult(
        status=ToolStatus.OK,
        chunks=chunks,
        evaluator_verdict=verdict_value,
        degradation_caveats=evaluator_caveats,
    )


# ══════════════════════════════════════════════════════════════════════════
# §1.3 — GRAPH / GRAPH_HYBRID
# ══════════════════════════════════════════════════════════════════════════

async def graph_tool(
    tool_input: GraphToolInput,
    events: Optional[EventRecorder] = None,
    project_id: Optional[str] = None,
) -> GraphToolResult:
    """
    Real within-case graph traversal, wrapping
    `graph_retriever.retrieve_graph(..., cross_case=False, ...)`.

    With `hybrid=True` this additionally runs the vector/BM25 leg and merges at
    RRF before returning — GRAPH_HYBRID is graph discovery FUSED WITH document
    retrieval, not a third retrieval mechanism.

    [PRESERVE — design §2.2] Sets `fallback_to_rag=True` when traversal fails or
    yields nothing usable — GRAPH with no chunks (and no conflict chunks), or
    GRAPH_HYBRID with no combined result. The tool reports that a fallback is
    warranted; it does NOT perform the fallback itself. The calling sub-agent
    acts on the flag.

    [PRESERVE — design §2.2] Within-case only: `cross_case=False` is hard-coded,
    not caller-controllable. Cross-case traversal is `xgraph_tool`, a separate
    tool carrying its own role gate — so no caller can reach cross-case data by
    flipping a boolean here.

    [RESOLVED-1a] Hybrid output is tagged `source_tool="GRAPH_HYBRID"`, making it
    structurally distinguishable from plain GRAPH for citation and trace display.
    """
    from src import config
    from src.retrieval.graph_retriever import retrieve_graph

    label = "GRAPH_HYBRID" if tool_input.hybrid else "GRAPH"
    step = "tool:graph_hybrid" if tool_input.hybrid else "tool:graph"
    if events:
        await events.emit(step, "active", f"Traversing case graph ({label})")

    case_id = tool_input.caller.active_case_id

    try:
        graph_result = await retrieve_graph(
            tool_input.query_text,
            tool_input.target_entity,
            case_id,
            cross_case=False,
            max_hops=tool_input.max_hops,
            user_id=tool_input.caller.user_id,
            user_role=tool_input.caller.role.value,
        )
    except Exception as exc:
        logger.error("%s traversal failed: %s", label, exc)
        if events:
            await events.emit(step, "retry", f"{label} failed — falling back to document search")
        return GraphToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
            fallback_to_rag=True,
        )

    graph_chunks: list[dict] = list(graph_result.get("chunks") or [])

    if not tool_input.hybrid:
        if not graph_chunks:
            seed_count = len(graph_result.get("seed_entities") or [])
            reason = (
                "no seed entity matched" if seed_count == 0
                else "seed entity matched but no connected evidence"
            )
            if events:
                await events.emit(
                    step, "retry",
                    f"Graph traversal found no connected evidence ({reason}) — "
                    "falling back to document search",
                )
            return GraphToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True)

        chunks = [_to_evidence(c, "GRAPH", i) for i, c in enumerate(graph_chunks, start=1)]
        if events:
            await events.emit(step, "done", f"{label} returned {len(chunks)} linked items")
        return GraphToolResult(
            status=ToolStatus.OK,
            chunks=chunks,
            fallback_to_rag=False,
            hop_count=graph_result.get("hop_count", 0),
            chain_confidence=graph_result.get("compounded_confidence"),
            seed_entities=list(graph_result.get("seed_entities") or []),
            conflicts_included=bool(graph_chunks) and not graph_result.get("seed_entities"),
        )

    # ── GRAPH_HYBRID: fuse the graph leg with the document leg ──
    # Composed from the RAG tool's own primitives rather than re-implementing
    # them, per design §2.2 (the legacy path duplicates RAG's retrieval steps
    # inline; that duplication is logged in §7, not reproduced here).
    try:
        import asyncio

        from src.pipeline.cross_script_variant import generate_cross_script_variant
        from src.pipeline.query_expander import expand_query
        from src.retrieval.bm25_retriever import retrieve_bm25
        from src.retrieval.embedder import embed_text
        from src.retrieval.reranker import rerank_results
        from src.retrieval.vector_store import get_all_chunks, query_similar

        where = _build_where(case_id, project_id, include_global=True)
        query = tool_input.query_text

        expanded = await expand_query(query, n=2)
        cross_script = await generate_cross_script_variant(query)
        all_queries = [query] + expanded + ([cross_script] if cross_script else [])

        embeddings = await asyncio.gather(*[embed_text(q) for q in all_queries])
        search_results = await asyncio.gather(*[
            query_similar(q, emb, top_k=config.TOP_K_RETRIEVAL, where=where)
            for q, emb in zip(all_queries, embeddings)
        ])

        vector_results: list[dict] = []
        seen: set = set()
        for res in search_results:
            for chunk in res:
                cid = chunk.get("id")
                if cid not in seen:
                    seen.add(cid)
                    vector_results.append(chunk)

        try:
            pool = await get_all_chunks(where=where)
        except Exception as pool_exc:
            logger.error(
                "Fetching full BM25 candidate pool failed (GRAPH_HYBRID): %s. "
                "Falling back to vector_results only.", pool_exc
            )
            pool = vector_results

        bm25_results = retrieve_bm25(
            " ".join(all_queries), pool, top_k=config.TOP_K_RETRIEVAL
        )
        combined_semantic = vector_results + graph_chunks

        if not combined_semantic:
            if events:
                await events.emit(
                    step, "retry",
                    "No graph or hybrid results — falling back to document search",
                )
            return GraphToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True)

        fused = rerank_results(
            combined_semantic, bm25_results, top_k=config.TOP_K_RETRIEVAL
        )
    except Exception as exc:
        logger.error("GRAPH_HYBRID retrieval leg failed: %s", exc)
        if events:
            await events.emit(step, "retry", f"{label} failed — falling back to document search")
        return GraphToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
            fallback_to_rag=True,
        )

    chunks = [_to_evidence(c, "GRAPH_HYBRID", i) for i, c in enumerate(fused, start=1)]
    if events:
        await events.emit(
            step, "done",
            f"{len(vector_results)} document + {len(graph_chunks)} graph item(s) fused",
        )
    return GraphToolResult(
        status=ToolStatus.OK,
        chunks=chunks,
        fallback_to_rag=False,
        hop_count=graph_result.get("hop_count", 0),
        chain_confidence=graph_result.get("compounded_confidence"),
        seed_entities=list(graph_result.get("seed_entities") or []),
    )


# ══════════════════════════════════════════════════════════════════════════
# §1.4 — XGRAPH (cross-case entity traversal)
# ══════════════════════════════════════════════════════════════════════════

async def xgraph_tool(
    tool_input: XGraphToolInput,
    gateway: Any = None,
    events: Optional[EventRecorder] = None,
) -> XGraphToolResult:
    """
    Real cross-case traversal, wrapping
    `graph_retriever.retrieve_graph(..., cross_case=True, ...)`.

    ORDERING IS LOAD-BEARING ([PRESERVE — design §2.3, §4.3]):
      1. role check   → `_deny_cross_case` below, FIRST
      2. audit log    → written by `_deny_cross_case` on denial
      3. only then    → `retrieve_graph` arms `current_cross_case` /
                        `current_rls_active` internally, after its own check

    Cross-case / RLS scope is never armed before the check passes. An
    unauthorized caller never arms it at all — there is no window to close
    because none is opened. Do not hoist scope resolution above this call.

    [PRESERVE — design §2.3] Never falls back to RAG: `fallback_to_rag` is
    pinned False by `CrossCaseToolResult`, so cross-case evidence can never
    blend into a case-scoped answer stream. An empty result is a definite
    "no connections found", not a reason to degrade.
    """
    from src.retrieval.graph_retriever import retrieve_graph

    denial = await _deny_cross_case("XGRAPH", tool_input, gateway, events)
    if denial:
        return XGraphToolResult(status=ToolStatus.DENIED, error=denial)

    if events:
        await events.emit("tool:xgraph", "active", "Searching across cases")

    try:
        result = await retrieve_graph(
            tool_input.query_text,
            tool_input.target_entity,
            tool_input.caller.active_case_id,
            cross_case=True,
            max_hops=tool_input.max_hops,
            user_id=tool_input.caller.user_id,
            user_role=tool_input.caller.role.value,
        )
    except PermissionError as exc:
        # retrieve_graph's own gate fired. Surface as DENIED, not FAILED — it
        # already audit-logged; do not double-log.
        if events:
            await events.emit("tool:xgraph", "error", f"XGRAPH refused: {exc}")
        return XGraphToolResult(
            status=ToolStatus.DENIED,
            error=ToolError(kind="permission_denied", message=str(exc)),
        )
    except Exception as exc:
        logger.error("XGRAPH traversal failed: %s", exc)
        if events:
            await events.emit("tool:xgraph", "error", f"Cross-case search failed: {exc}")
        return XGraphToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
        )

    raw_chunks = list(result.get("chunks") or [])
    unconfirmed = list(result.get("unconfirmed_links") or [])

    if not raw_chunks:
        if events:
            await events.emit("tool:xgraph", "done", "No cross-case connections found")
        return XGraphToolResult(
            status=ToolStatus.EMPTY,
            unconfirmed_links=unconfirmed,
            hop_count=result.get("hop_count", 0),
        )

    chunks = [_to_evidence(c, "XGRAPH", i) for i, c in enumerate(raw_chunks, start=1)]
    case_ids = sorted({c.metadata.case_id for c in chunks if c.metadata.case_id})
    if events:
        await events.emit(
            "tool:xgraph", "done", f"Found connections across {len(case_ids)} case(s)"
        )
    return XGraphToolResult(
        status=ToolStatus.OK,
        chunks=chunks,
        case_ids_touched=case_ids,
        unconfirmed_links=unconfirmed,
        hop_count=result.get("hop_count", 0),
        chain_confidence=result.get("compounded_confidence"),
    )


# ══════════════════════════════════════════════════════════════════════════
# §1.5 — XAGG (cross-case aggregate)
# ══════════════════════════════════════════════════════════════════════════

def _render_aggregate(agg: dict) -> str:
    """
    Render a `run_aggregate` result into deterministic text.

    Verbatim port of the orchestrator's own rendering, kept identical on
    purpose: [PRESERVE — design §2.4] this text is what gets served when the
    Verifier rejects the generated paraphrase. The aggregate is machine-computed
    and correct by construction, so a rejection means the PARAPHRASE failed —
    not that the evidence is unsound. Changing this rendering changes what the
    user sees on that path.
    """
    kind = agg.get("kind")
    if kind == "graph_recurrence":
        lines = [
            f"- {r['name']} ({agg['entity_type']}): appears in {r['case_count']} "
            f"cases — {', '.join(r['case_ids'])}"
            for r in agg.get("results", [])
        ]
    elif kind == "case_listing":
        lines = [
            f"- {c['case_id']} (FIR {c['fir_number'] or 'N/A'}): "
            f"{c['crime_category'] or 'uncategorized'} — "
            f"{c['investigation_status'] or 'unknown status'}, "
            f"{c['police_station'] or 'unknown station'}"
            for c in agg.get("cases", [])
        ]
    else:
        lines = [f"- {c['key']}: {c['count']} cases" for c in agg.get("counts", [])]
    return "\n".join(lines) or "(no matching cases found)"


async def xagg_tool(
    tool_input: XAggToolInput,
    gateway: Any = None,
    events: Optional[EventRecorder] = None,
) -> XAggToolResult:
    """
    Real cross-case aggregate, wrapping `xagg.run_aggregate(...)`.

    [PRESERVE — design §2.4] TWO CANNED AGGREGATE FAMILIES, keyword-dispatched
    inside `run_aggregate` — relational group-by, or graph node-recurrence.
    This is deliberately NOT a general text-to-SQL/Cypher system; the bounded
    surface is the safety property. Do not "make it smarter" here.

    Same role-gate ordering as XGRAPH (check → audit → only then scope), and the
    same never-fall-back guarantee, pinned by `CrossCaseToolResult`.

    The aggregate is emitted BOTH as `raw_summary_text` (the deterministic
    rendering, for the Verifier-rejection path) and as a single wrapped chunk,
    matching how the legacy path treats aggregate text as its one "document".
    """
    from src.data_gateway import get_gateway
    from src.pipeline.xagg import run_aggregate

    denial = await _deny_cross_case("XAGG", tool_input, gateway, events)
    if denial:
        return XAggToolResult(status=ToolStatus.DENIED, error=denial)

    if events:
        await events.emit("tool:xagg", "active", "Computing cross-case aggregate")

    try:
        gw = gateway if gateway is not None else await get_gateway()
        agg = await run_aggregate(
            tool_input.query_text,
            tool_input.target_entity,
            gw,
            user_id=tool_input.caller.user_id,
            user_role=tool_input.caller.role.value,
        )
    except PermissionError as exc:
        if events:
            await events.emit("tool:xagg", "error", f"XAGG refused: {exc}")
        return XAggToolResult(
            status=ToolStatus.DENIED,
            error=ToolError(kind="permission_denied", message=str(exc)),
        )
    except Exception as exc:
        logger.error("XAGG aggregate failed: %s", exc)
        if events:
            await events.emit("tool:xagg", "error", f"Cross-case aggregate failed: {exc}")
        return XAggToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
        )

    raw_text = _render_aggregate(agg)
    kind = agg.get("kind")

    case_ids: list[str] = []
    if kind == "graph_recurrence":
        case_ids = sorted({
            cid for r in agg.get("results", []) for cid in (r.get("case_ids") or [])
        })
    elif kind == "case_listing":
        case_ids = sorted({
            c["case_id"] for c in agg.get("cases", []) if c.get("case_id")
        })

    chunk = EvidenceChunk(
        id="xagg-aggregate-1",
        text=raw_text,
        score=1.0,
        metadata=ChunkMetadata(
            source_tool="XAGG", case_id=None, source_file="cross_case_aggregate"
        ),
    )
    if events:
        await events.emit("tool:xagg", "done", f"Aggregate computed ({kind})")
    return XAggToolResult(
        status=ToolStatus.OK,
        chunks=[chunk],
        aggregate_kind=kind if kind in
        ("graph_recurrence", "relational_aggregate", "case_listing") else None,
        raw_summary_text=raw_text,
        case_ids_touched=case_ids,
    )


# ══════════════════════════════════════════════════════════════════════════
# §1.6 — XNETWORK (cross-case thematic synthesis)
# ══════════════════════════════════════════════════════════════════════════

async def xnetwork_tool(
    tool_input: XNetworkToolInput,
    gateway: Any = None,
    events: Optional[EventRecorder] = None,
) -> XNetworkToolResult:
    """
    Real cross-case pattern synthesis, wrapping `xnetwork.run_network_query(...)`.

    Backed by embedding search over a SEPARATE, PRECOMPUTED community-summary
    collection written offline — no graph traversal, no entity typing. Results
    therefore reflect the corpus as of the last summarization run, not live
    graph state.

    Same role-gate ordering and never-fall-back guarantee as XGRAPH/XAGG.

    [PRESERVE — design §2.5] The XNETWORK-specific one-shot forced-cloud
    regeneration retry on first Verifier rejection lives in the generation
    layer, NOT here — this tool only supplies the evidence and the
    `raw_summary_text` that backs the final fallback. Do not generalize that
    retry to XAGG/XGRAPH.
    """
    from src.data_gateway import get_gateway
    from src.pipeline.xnetwork import run_network_query

    denial = await _deny_cross_case("XNETWORK", tool_input, gateway, events)
    if denial:
        return XNetworkToolResult(status=ToolStatus.DENIED, error=denial)

    if events:
        await events.emit("tool:xnetwork", "active", "Synthesizing cross-case patterns")

    try:
        gw = gateway if gateway is not None else await get_gateway()
        result = await run_network_query(
            tool_input.query_text,
            gw,
            user_id=tool_input.caller.user_id,
            user_role=tool_input.caller.role.value,
            top_k=tool_input.top_k,
        )
    except PermissionError as exc:
        if events:
            await events.emit("tool:xnetwork", "error", f"XNETWORK refused: {exc}")
        return XNetworkToolResult(
            status=ToolStatus.DENIED,
            error=ToolError(kind="permission_denied", message=str(exc)),
        )
    except Exception as exc:
        logger.error("XNETWORK synthesis failed: %s", exc)
        if events:
            await events.emit("tool:xnetwork", "error", f"Cross-case synthesis failed: {exc}")
        return XNetworkToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
        )

    results = list(result.get("results") or [])
    if not results:
        if events:
            await events.emit("tool:xnetwork", "done", "No community summaries matched")
        return XNetworkToolResult(status=ToolStatus.EMPTY)

    chunks: list[EvidenceChunk] = []
    for i, r in enumerate(results, start=1):
        chunks.append(
            EvidenceChunk(
                id=str(r.get("community_id") or f"xnetwork-{i}"),
                # `summary_text` is the key query_similar_communities returns;
                # `distance` is a Chroma distance (LOWER is closer), so it is
                # deliberately NOT passed through as `score` — the contract
                # defines score as higher-is-better, and silently inverting the
                # ordering semantics would be worse than omitting it.
                text=r.get("summary_text") or "",
                score=None,
                metadata=ChunkMetadata(
                    source_tool="XNETWORK",
                    case_id=None,  # a community spans cases; no single owner
                    source_file="community_summaries",
                    community_id=r.get("community_id"),
                    community_case_ids=list(r.get("case_ids") or []),
                    member_count=r.get("member_count"),
                ),
            )
        )

    raw_text = "\n\n".join(c.text for c in chunks if c.text)
    if events:
        await events.emit(
            "tool:xnetwork", "done", f"Retrieved {len(chunks)} community summaries"
        )
    return XNetworkToolResult(
        status=ToolStatus.OK,
        chunks=chunks,
        community_ids=[str(c) for c in (result.get("community_ids") or [])],
        raw_summary_text=raw_text or None,
        case_ids_touched=list(result.get("case_ids") or []),
    )


# ══════════════════════════════════════════════════════════════════════════
# §1.7 — SQL (structured reference lookup)
# ══════════════════════════════════════════════════════════════════════════

async def sql_tool(
    tool_input: SqlToolInput,
    gateway: Any = None,
    events: Optional[EventRecorder] = None,
) -> SqlToolResult:
    """
    Real structured reference lookup: `extract_sql_params()` (LLM param
    extraction) → `gateway.query_police_reference_data(...)` (direct
    parameterized Postgres).

    [PRESERVE — design §2.6] This is the ONLY in-scope SQL path.
    `src/mcp/client.py`'s `execute_query()` is a separate, standalone
    integration wired only to the admin MCP-demo endpoint — not called from the
    chat pipeline and not part of this harness. Do not merge the two.

    [PRESERVE — design §2.6] Reference data, NOT case evidence: no case scoping,
    no role gate, and emitted chunks carry `case_id=None`. That is correct, and
    it is what makes them inert to the Verifier's leakage check — reference data
    belongs to no case.

    [PRESERVE — design §2.6] `fallback_to_rag=True` on empty rows OR any
    exception during extraction or query.
    """
    from src.data_gateway import get_gateway
    from src.pipeline.sql_extractor import extract_sql_params

    if events:
        await events.emit("tool:sql", "active", "Looking up penal-code reference data")

    try:
        params = await extract_sql_params(tool_input.query_text) or {}
        gw = gateway if gateway is not None else await get_gateway()
        rows = await gw.query_police_reference_data(
            category=params.get("category"),
            subject=params.get("subject"),
            section_ref=params.get("section_ref"),
        )
    except Exception as exc:
        logger.error("SQL reference lookup failed: %s", exc)
        if events:
            await events.emit(
                "tool:sql", "retry",
                "Reference lookup failed — falling back to document search",
            )
        return SqlToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
            fallback_to_rag=True,
        )

    rows = list(rows or [])
    if not rows:
        if events:
            await events.emit(
                "tool:sql", "retry",
                "No reference rows — falling back to document search",
            )
        return SqlToolResult(
            status=ToolStatus.EMPTY,
            fallback_to_rag=True,
            row_count=0,
            extracted_params=params,
        )

    chunks: list[EvidenceChunk] = []
    for i, row in enumerate(rows, start=1):
        parts = [
            f"{k.replace('_', ' ').title()}: {v}"
            for k, v in row.items()
            if v is not None and k != "id"
        ]
        chunks.append(
            EvidenceChunk(
                id=f"sql-{row.get('id', i)}",
                text=". ".join(parts),
                score=1.0,
                metadata=ChunkMetadata(
                    source_tool="SQL",
                    case_id=None,  # reference data belongs to no case
                    source_file="police_reference_data",
                ),
            )
        )

    if events:
        await events.emit("tool:sql", "done", f"Found {len(rows)} reference row(s)")
    return SqlToolResult(
        status=ToolStatus.OK,
        chunks=chunks,
        row_count=len(rows),
        extracted_params=params,
    )


# ══════════════════════════════════════════════════════════════════════════
# §1.8 — WEB (guarded external search)
# ══════════════════════════════════════════════════════════════════════════

async def web_tool(
    tool_input: WebToolInput,
    events: Optional[EventRecorder] = None,
    air_gap_mode: Optional[bool] = None,
) -> WebToolResult:
    """
    Real guarded external search: `perform_web_search()` (Tavily,
    domain-allowlisted) → on failure/empty, `call_gemini_with_search()` (Gemini
    grounded search, same allowlist applied post-hoc).

    [PRESERVE — design §2.7] AIR-GAP MODE DISABLES THIS ENTIRELY, checked BEFORE
    either provider is reached. `perform_web_search` and the Gemini fallback
    each carry their own internal air-gap check too; this one is deliberately
    redundant so no future re-wrapping can leave a path that reaches a provider
    with the flag set. A wrapper that checks once at the top and then calls
    through to a second provider on failure has silently reopened egress —
    the check must dominate BOTH tiers.

    [PRESERVE — design §2.7] Two-tier fallback-within-a-fallback is preserved;
    do not collapse it to a single provider check. `fallback_to_rag=True` only
    after BOTH tiers have failed.

    [PRESERVE — design §2.7] No case scope, no role gate. Web results are NEVER
    case evidence: chunks carry `case_id=None`.
    """
    from src import config
    from src.retrieval.web_search import perform_web_search

    if air_gap_mode is None:
        air_gap_mode = bool(getattr(config, "AIR_GAP_MODE", False))

    if air_gap_mode:
        if events:
            await events.emit(
                "tool:web", "skipped", "Web search disabled under air-gap mode"
            )
        return WebToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(
                kind="upstream_failure", message="Web search disabled (AIR_GAP_MODE)."
            ),
            fallback_to_rag=True,
        )

    if events:
        await events.emit("tool:web", "active", "Searching allowlisted external sources")

    # ── Tier 1: Tavily ──
    results: list[dict] = []
    try:
        results = list(await perform_web_search(
            tool_input.query_text, max_results=tool_input.max_results
        ) or [])
    except Exception as exc:
        logger.error("Primary web search failed: %s", exc)

    provider = "primary_search"

    # ── Tier 2: Gemini grounded search, only if tier 1 produced nothing ──
    if not results:
        try:
            from src.llm.client import call_gemini_with_search

            text, sources = await call_gemini_with_search(tool_input.query_text)
            if text:
                results = [{
                    "title": s.get("title") or "Grounded search result",
                    "url": s.get("url") or "",
                    "content": text,
                    "score": None,
                } for s in (sources or [{}])]
                provider = "grounded_search_fallback"
        except Exception as exc:
            logger.error("Grounded-search web fallback failed: %s", exc)

    if not results:
        if events:
            await events.emit(
                "tool:web", "retry",
                "Both search tiers failed — falling back to document search",
            )
        return WebToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True)

    chunks = [
        EvidenceChunk(
            id=f"web-{i}",
            text=f"{r.get('title') or ''} — {r.get('content') or ''}".strip(" —"),
            score=r.get("score"),
            metadata=ChunkMetadata(
                source_tool="WEB",
                case_id=None,  # never case evidence
                source_file=r.get("url"),
            ),
        )
        for i, r in enumerate(results, start=1)
    ]
    if events:
        await events.emit("tool:web", "done", f"Retrieved {len(chunks)} external result(s)")
    return WebToolResult(
        status=ToolStatus.OK,
        chunks=chunks,
        provider_used=provider,
        fallback_to_rag=False,
    )
