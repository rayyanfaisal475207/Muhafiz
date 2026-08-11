import logging
from src.data_gateway import get_gateway

logger = logging.getLogger(__name__)

async def _run_conflict_detection_bg(c_id: str, d_id: str):
    import asyncio
    from src.graph import conflict_detection
    gateway = await get_gateway()
    
    try:
        await conflict_detection.detect_conflicts(c_id)
        # [Reconciliation fix — harness-reconciliation Unit 6, migration
        # 018] Recorded ONLY here, after detect_conflicts() has genuinely
        # completed without raising — never at schedule time (that would
        # re-open the exact race this marker exists to close: a query
        # arriving while detection is still in flight would find a marker
        # that asserts a check that hasn't happened yet). Best-effort: a
        # failure to write this marker must not turn a successful conflict
        # detection run into a failed ingestion job.
        try:
            await gateway.mark_conflicts_checked(c_id)
        except Exception as marker_exc:
            logger.warning(
                f"Failed to record conflicts_checked_at for case {c_id}: {marker_exc}"
            )
        await gateway.update_ingestion_job_by_doc(d_id, {
            "status": "success",
            "error_message": "Conflict detection: success"
        })
    except Exception as exc:
        logger.error(f"Conflict detection failed for case {c_id}: {exc}")
        await gateway.update_ingestion_job_by_doc(d_id, {
            "status": "failed",
            "error_message": f"Conflict detection failed: {exc}"
        })
