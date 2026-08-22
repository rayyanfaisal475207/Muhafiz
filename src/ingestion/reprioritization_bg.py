import logging

logger = logging.getLogger(__name__)


async def _run_reprioritization_bg(case_id: str):
    """
    Milestone D1's incremental execution path — see
    src/graph/candidate_reprioritization.py's module docstring
    ("EXECUTION MODEL") for why this is a fire-and-forget task scheduled
    at the same point conflict detection already is
    (src/ingestion/conflict_bg.py), rather than new worker infrastructure.

    Best-effort: a failure here must never fail the ingestion job it
    rides alongside — the pending queue simply keeps its previous
    ordering until the next successful re-score (either the next ingest
    for this case, or a supervisor's manual /queue/reprioritize sweep).
    """
    from src.graph import candidate_reprioritization
    try:
        updated = await candidate_reprioritization.reprioritize_case(case_id)
        logger.info("Reprioritization: re-scored %d pending candidates for case %s", updated, case_id)
    except Exception as exc:
        logger.error("Pending-candidate reprioritization failed for case %s: %s", case_id, exc)
