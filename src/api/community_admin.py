# ============================================================
# Community detection admin surface — Milestone E3
# (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — incremental community refresh).
#
# E3's execution model reuses D1's (GET /queue/reprioritize's own
# precedent, src/api/graph_review.py): an incremental fire-and-forget
# check runs automatically after every ingest
# (src/ingestion/community_refresh_bg.py), PLUS this manual full-sweep
# endpoint, since this codebase has no cron to run a scheduled sweep on.
# Kept as its own small router rather than folded into graph_review.py —
# community detection is a different review surface (whole-graph
# clustering, not the SAME_AS/CITES pending queue) with no
# confirm/reject actions of its own.
# ============================================================

import logging

from fastapi import APIRouter, Depends

from src.auth.jwt import require_role
from src.auth.rls_context import cross_case_rls_dependency
from src.database.models import User
from src.graph import community_detection

logger = logging.getLogger(__name__)

# Community detection clusters the whole Person graph, not one case —
# same "deliberately cross-case by design" shape graph_review.py's own
# queue already documents, so the same cross_case_rls_dependency applies.
router = APIRouter(
    prefix="/api/admin/community", tags=["community-admin"],
    dependencies=[Depends(cross_case_rls_dependency)],
)


@router.get("/staleness")
async def staleness(admin: User = Depends(require_role("supervisor"))):
    """The same drift heuristic community_refresh_bg.py checks automatically after every ingest — read-only, no side effect."""
    return await community_detection.get_staleness()


@router.post("/refresh")
async def refresh(admin: User = Depends(require_role("supervisor"))):
    """
    Manual full-sweep — E3's execution-model point 4, path #2 (there is
    no cron in this codebase to run this on a schedule; a supervisor
    triggers it on demand, same shape as graph_review.py's
    POST /queue/reprioritize). Always runs detect_communities()+
    summarize_communities() regardless of the staleness check — a
    supervisor explicitly asking for a refresh is not gated behind the
    drift heuristic the automatic incremental trigger uses.
    """
    detect_result = await community_detection.detect_communities()

    from src.graph import community_summarization
    summarize_result = await community_summarization.summarize_communities()

    return {
        "run_id": detect_result["run_id"],
        "node_count": detect_result["node_count"],
        "edge_count": detect_result["edge_count"],
        "community_count": detect_result["community_count"],
        "attempted": summarize_result.get("attempted", 0),
        "written": summarize_result.get("written", 0),
        "skipped": summarize_result.get("skipped", 0),
    }
