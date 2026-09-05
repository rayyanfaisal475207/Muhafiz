# ============================================================
# XNETWORK — cross-case open-ended network/theme queries (GraphRAG-inspired
# layer, Section 2). Modeled directly on xagg.py's shape: same audit
# logging, same supervisor+ role gate, same RLS cross-case arming pattern
# — reused, not reinvented.
#
# Distinct from:
#   - XAGG: canned counts/aggregates over case or graph metadata, always a
#     single deterministic evidence block.
#   - XGRAPH: named-entity traversal, cites per-case graph evidence for one
#     specific entity.
#   - GRAPH_HYBRID: within-case broad questions.
# XNETWORK answers "what's the overall picture/network" style questions by
# retrieving the top-k most relevant precomputed community summaries
# (src/graph/community_detection.py + community_summarization.py) and
# synthesizing across them — the community summaries are already grounded,
# LLM-written prose, not raw structured data, so this is closer to a RAG
# retrieval-and-cite shape than XAGG's "reproduce this data faithfully"
# shape.
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

from src.retrieval.community_vector_store import query_similar_communities

logger = logging.getLogger(__name__)

DEFAULT_TOP_K = 5

# [Module 12 — RC-1 relevance gate] `query_similar_communities()` always
# returns its top-k NEAREST communities, even when none of them are
# actually about what was asked — it has no notion of "not relevant
# enough," only "nearest available." For a broad/evaluative question with
# no named entity (e.g. "flag anything unusual in our caseload"), those
# nearest communities are frequently just whichever cluster happens to sit
# closest in embedding space, and XNETWORK was reciting them as if they
# answered the question (RC-1: Module 10's live sweep found 6/18 untouched
# gold-32 questions — CR3, CR4, CS4, M4, G1, G6 — hitting exactly this).
#
# Threshold picked from measured cosine-distance evidence, not guessed
# (see scratch_distance_probe.py, run against the live
# `muhafiz_community_reports` Chroma collection, 2026-09-05):
#   - The 6 confirmed-bad broad/evaluative questions above: nearest-
#     community distance ranged 0.1558-0.2057 across all of them.
#   - Four realistic, genuinely-relevant network queries (three
#     named-entity/case-specific asks, one topical-but-broad "which
#     clusters involve weapons/firearms as evidence"): nearest-community
#     distance ranged 0.1256-0.1395.
#   - A control query built from a community summary's own text (the
#     clearest possible "this SHOULD match" case): 0.0307.
# The two populations are cleanly separated by a ~0.016 gap (0.1395 to
# 0.1558); 0.145 sits in that gap. Below it: treated as relevant enough to
# narrate. At or above it: filtered out — see `run_network_query()`'s
# `no_relevant_reason` for the honest-refusal path this feeds.
RELEVANCE_DISTANCE_THRESHOLD = 0.145


def _no_relevant_cluster_message(query_text: str, nearest_distance: Optional[float]) -> str:
    """
    [Module 12 — RC-1] The honest, specific "nothing relevant" message —
    single source of truth for both the legacy orchestrator.py XNETWORK
    route and the harness XNETWORK tool/Cross-Case Linkage sub-agent, so
    the two live-traffic paths (direct dispatch and Meta-Analysis'
    decomposed sub-query, both reachable per config.HARNESS_CUTOVER_ROUTES)
    say the same thing. Deliberately NOT a generic "no information
    available" (that is RC-6's separate, already-flagged failure mode) —
    names the corpus/cluster search specifically and states plainly that
    nothing in it relates to the question actually asked, rather than
    reciting the nearest-but-unrelated clusters as if they were an answer.
    """
    stated_query = (query_text or "").strip()
    if len(stated_query) > 160:
        stated_query = stated_query[:160].rstrip() + "..."
    distance_note = (
        f" (nearest cluster found was distance {nearest_distance:.3f} against a "
        f"relevance cutoff of {RELEVANCE_DISTANCE_THRESHOLD})"
        if nearest_distance is not None
        else ""
    )
    return (
        "No community cluster in the case corpus is closely related to this "
        f'specific question ("{stated_query}"){distance_note}. Rather than '
        "describing an unrelated cluster, no cross-case network finding is "
        "being reported here."
    )


async def run_network_query(
    query_text: str,
    gateway,
    user_id: Optional[str] = None,
    user_role: str = "investigator",
    top_k: int = DEFAULT_TOP_K,
    jurisdiction_case_ids: Optional[list[str]] = None,
) -> dict:
    """
    Retrieve the top-k community summaries relevant to `query_text` and
    return them for orchestrator.py to synthesize into a cited response
    (same two-step shape as run_aggregate: this function computes the
    deterministic evidence, the orchestrator generates+verifies the
    natural-language answer around it).

    Cross-case, same as XAGG/XGRAPH — requires the same supervisor-or-higher
    role gate and audit logging (Phase 7 RBAC applies to every cross-case
    route uniformly).

    `jurisdiction_case_ids` [Milestone E1]: unlike XGRAPH/XAGG (a real
    pre-filter on the Cypher/SQL that runs BEFORE the graph/relational
    work), this is a POST-filter on `query_similar_communities()`'s
    already-computed top-k — `community_vector_store`'s Chroma collection
    stores `case_ids` as a comma-joined metadata string (see its own
    `upsert_community_reports()`), not a natively filterable list-typed
    field, so pushing this down into the Chroma `where` clause itself
    would need a metadata-schema change out of scope for E1. Narrowing
    the top-k results after the fact still keeps a jurisdiction-scoped
    query from citing an out-of-jurisdiction community in its final
    answer, which is what actually matters here — stated honestly as a
    narrower guarantee than E1's "before any vector work runs" framing
    for XGRAPH/XAGG, not silently presented as the same thing.
    """
    if user_role not in ("supervisor", "station-admin", "platform-admin"):
        logger.warning("Unauthorized cross-case network query attempted by %s (user_id: %s)", user_role, user_id)
        try:
            await gateway.log_audit_event(
                event_type="authorization_violation",
                user_id=user_id,
                case_id=None,
                details={"query": query_text, "role": user_role, "route": "XNETWORK"},
            )
        except Exception as e:
            logger.error("Failed to audit log unauthorized XNETWORK attempt: %s", e)
        raise PermissionError("Cross-case network queries require supervisor role or higher.")

    try:
        await gateway.log_audit_event(
            event_type="cross_case_network",
            user_id=user_id,
            case_id=None,
            details={"query": query_text},
        )
    except Exception as e:
        logger.error("Failed to audit log cross-case network query: %s", e)

    # RLS cross-case bypass, armed only after the role check passes — same
    # fix/rationale as xagg.py::run_aggregate() and
    # graph_retriever.py::retrieve_graph(). Self-armed here rather than
    # relying solely on the caller having already armed it (same
    # security-review-addendum reasoning xagg.py documents).
    from src.database.postgres import current_cross_case, current_rls_active
    current_rls_active.set(True)
    current_cross_case.set(True)

    raw_results = await query_similar_communities(query_text, top_k=top_k)
    if jurisdiction_case_ids is not None:
        allowed = set(jurisdiction_case_ids)
        raw_results = [r for r in raw_results if allowed & set(r.get("case_ids") or [])]

    # [Module 12 — RC-1 relevance gate] Nearest is not the same as relevant
    # — see RELEVANCE_DISTANCE_THRESHOLD's own comment for the evidence
    # behind this cutoff. This is a backstop, not a replacement for router
    # precision: a real named-entity/case-specific network query still
    # clears it (measured 0.1256-0.1395), so a legitimate XGRAPH/XNETWORK
    # ask is unaffected.
    results = [
        r for r in raw_results
        if r.get("distance") is not None and r["distance"] <= RELEVANCE_DISTANCE_THRESHOLD
    ]

    no_relevant_reason: Optional[str] = None
    if raw_results and not results:
        nearest = min(
            (r["distance"] for r in raw_results if r.get("distance") is not None),
            default=None,
        )
        no_relevant_reason = _no_relevant_cluster_message(query_text, nearest)
        logger.info(
            "XNETWORK relevance gate: %d nearest communities all fell short of the "
            "%.3f cutoff (nearest=%.3f) for query %r; returning no results.",
            len(raw_results), RELEVANCE_DISTANCE_THRESHOLD, nearest or -1.0, query_text[:80],
        )

    community_ids = [r["community_id"] for r in results]
    case_ids = sorted({cid for r in results for cid in r.get("case_ids", [])})

    return {
        "kind": "network_synthesis",
        "results": results,
        "community_ids": community_ids,
        "case_ids": case_ids,
        "no_relevant_reason": no_relevant_reason,
    }
