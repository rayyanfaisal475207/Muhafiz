# ============================================================
# Ingestion run quality admin surface — Ingestion Quality Control at
# Scale, Modules G1/G2 (see INGESTION_QUALITY_AT_SCALE_PLAN.md).
#
# Read-only rollup (G1) plus the one write action G2's circuit breaker
# needs: a human acknowledgment that clears a flagged run so the NEXT
# same-source run stops inheriting the flag (see
# src/graph/ingestion_circuit_breaker.py's PROPAGATION rule). This is the
# only mutation in this router — same "flags, never auto-remediates"
# discipline as the breaker itself; acknowledging a flag records that a
# human looked at it, it does not assert the run's data was fine.
#
# Kept as its own small router, same reasoning as community_admin.py:
# this is a different review surface (ingestion-run rollups, not the
# SAME_AS/CITES pending queue) with its own single action.
# ============================================================

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import text

from src.auth.jwt import require_role
from src.auth.rls_context import cross_case_rls_dependency
from src.data_gateway import get_gateway
from src.database.models import User
from src.database.postgres import get_session

logger = logging.getLogger(__name__)

# Ingestion runs are not case-scoped in general (a sync_muhafiz_data run
# spans many cases at once) — same cross-case-by-design shape
# graph_review.py's queue and community_admin.py's staleness surface
# already use.
router = APIRouter(
    prefix="/api/admin/ingestion-quality", tags=["ingestion-quality"],
    dependencies=[Depends(cross_case_rls_dependency)],
)


@router.get("/runs")
async def list_runs(source: str | None = None, limit: int = 50, admin: User = Depends(require_role("supervisor"))):
    """
    Most recent ingestion runs, newest first — G1's per-run tier rollup
    plus G2's flag columns. `source` narrows to one source
    ('ingest_file' | 'sync_muhafiz_data'); omitted, every source is
    returned together (still newest-first, not grouped) since the admin
    surface's own read shape (Module G4) needs both.
    """
    limit = max(1, min(limit, 200))
    async with get_session() as db:
        clause = "WHERE source = :source" if source else ""
        result = await db.execute(
            text(
                f"""
                SELECT run_id, source, case_id, started_at, finished_at,
                       tier_cnic_auto, tier_flagged_unverified, tier_human_review, tier_new,
                       corroboration_gate_rejections, extraction_errors,
                       flagged_for_review, flagged_reason
                FROM ingestion_run_quality
                {clause}
                ORDER BY started_at DESC
                LIMIT :limit
                """
            ),
            {"source": source, "limit": limit} if source else {"limit": limit},
        )
        rows = [dict(row._mapping) for row in result.fetchall()]
    return {"runs": rows, "count": len(rows)}


@router.get("/flagged")
async def list_flagged(admin: User = Depends(require_role("supervisor"))):
    """Every run currently awaiting acknowledgment — the queue an admin surface's "needs attention" banner reads from."""
    async with get_session() as db:
        result = await db.execute(
            text(
                """
                SELECT run_id, source, case_id, started_at, finished_at,
                       tier_cnic_auto, tier_flagged_unverified, tier_human_review, tier_new,
                       corroboration_gate_rejections, extraction_errors, flagged_reason
                FROM ingestion_run_quality
                WHERE flagged_for_review = true
                ORDER BY started_at DESC
                """
            ),
        )
        rows = [dict(row._mapping) for row in result.fetchall()]
    return {"flagged": rows, "count": len(rows)}


@router.post("/{run_id}/acknowledge")
async def acknowledge_run(run_id: str, admin: User = Depends(require_role("supervisor"))):
    """
    Clears flagged_for_review on this run so the next same-source run
    stops inheriting the flag (see ingestion_circuit_breaker.py's
    PROPAGATION rule). Never re-evaluates the run's own rates, never
    touches any other run's row, never writes to the graph — an
    acknowledgment records that a human looked at it, nothing more.
    """
    async with get_session() as db:
        result = await db.execute(
            text(
                "SELECT source, flagged_for_review FROM ingestion_run_quality WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        )
        row = result.fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="Ingestion run not found")
        if not row._mapping["flagged_for_review"]:
            raise HTTPException(status_code=409, detail="This run is not currently flagged")

        await db.execute(
            text(
                "UPDATE ingestion_run_quality SET flagged_for_review = false, "
                "flagged_reason = NULL WHERE run_id = :run_id"
            ),
            {"run_id": run_id},
        )

    gateway = await get_gateway()
    await gateway.log_audit_event(
        "ingestion_quality_acknowledge", {"run_id": run_id}, str(admin.id),
    )
    return {"run_id": run_id, "acknowledged": True}
