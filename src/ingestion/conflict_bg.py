import logging
from src.data_gateway import get_gateway

logger = logging.getLogger(__name__)

async def _run_conflict_detection_bg(c_id: str, d_id: str):
    import asyncio
    from src.graph import conflict_detection
    gateway = await get_gateway()
    
    try:
        await conflict_detection.detect_conflicts(c_id)
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
