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


async def run_network_query(
    query_text: str,
    gateway,
    user_id: Optional[str] = None,
    user_role: str = "investigator",
    top_k: int = DEFAULT_TOP_K,
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

    Returns {"kind": "network_synthesis", "results": [...], "community_ids": [...], "case_ids": [...]}.
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

    results = await query_similar_communities(query_text, top_k=top_k)

    community_ids = [r["community_id"] for r in results]
    case_ids = sorted({cid for r in results for cid in r.get("case_ids", [])})

    return {
        "kind": "network_synthesis",
        "results": results,
        "community_ids": community_ids,
        "case_ids": case_ids,
    }
