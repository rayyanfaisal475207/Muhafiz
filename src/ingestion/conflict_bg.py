import logging
from src.data_gateway import get_gateway

logger = logging.getLogger(__name__)

async def _run_conflict_detection_bg(c_id: str, d_id: str):
    import asyncio
    from src.graph import conflict_detection
    gateway = await get_gateway()
    
    try:
        await conflict_detection.detect_conflicts(c_id)

        # Migration 019: record that detection COMPLETED for this case.
        #
        # Written HERE — inside the task, after detect_conflicts() returns —
        # and never at schedule time. service.py fires this task with a bare
        # asyncio.create_task() after the events are already in the graph, so a
        # timeline query can arrive while detection is still in flight. Marking
        # at schedule time would assert the check had happened before it had;
        # marking on return means that racing query correctly finds no marker
        # and reports ConflictState.UNKNOWN.
        #
        # This covers detect_conflicts()'s early return on fewer than two
        # incidents too, which is deliberate: "examined the case and found
        # nothing to compare" is a COMPLETED check, just one with nothing to
        # find. It does NOT cover the raise path — the except below leaves the
        # marker unset, which is exactly right, since a failed check is not a
        # check.
        try:
            await gateway.mark_conflicts_checked(c_id)
        except Exception as exc:
            # A missing marker degrades Timeline Building to UNKNOWN, which is
            # safe. Failing the whole ingestion job over it would not be.
            logger.warning(
                "Could not record conflict-detection completion for case %s: %s", c_id, exc
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
