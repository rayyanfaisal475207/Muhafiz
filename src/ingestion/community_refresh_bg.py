import logging

logger = logging.getLogger(__name__)


async def _run_community_refresh_bg() -> None:
    """
    Milestone E3's execution model — deliberately reuses D1's resolution
    (GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md's open point 4), not a
    deviation: this codebase still has no standalone scheduled worker/
    cron (confirmed again while implementing this, same as D1's own
    check), so this is the identical shape as
    src/ingestion/reprioritization_bg.py — a fire-and-forget task
    scheduled at the same point conflict detection/reprioritization
    already are (src/ingestion/service.py, right after a document's
    graph extraction), plus a supervisor-triggered manual full-sweep
    endpoint for the case there's no cron to run a schedule on
    (POST /api/admin/community/refresh, src/api/community_admin.py).

    Unlike D1 (case-scoped — a case's own pending candidates), this is
    NOT case-scoped: community detection clusters the WHOLE Person graph,
    not one case's slice of it, so there is nothing case-specific to pass
    in — every ingestion event just asks "is the whole-graph partition
    stale enough to be worth a full recompute" via
    community_detection.get_staleness() (the same 10%-drift heuristic
    scripts/check_community_staleness.py already used manually), and only
    actually re-runs detect_communities()/summarize_communities() when
    that heuristic says yes. This bounds how often the real cost here
    (an LLM call per community, inside summarize_communities()) can fire
    from ingestion — every ingest checks staleness, but only a drift-
    crossing ingest re-summarizes.

    Best-effort, exactly like reprioritization_bg.py: a failure here must
    never fail the ingestion job it rides alongside — the community
    partition simply keeps its previous (possibly stale) shape until the
    next successful check, or a supervisor's manual
    /api/admin/community/refresh sweep.
    """
    from src.graph import community_detection

    try:
        staleness = await community_detection.get_staleness()
        if not staleness["stale"]:
            logger.debug("Community refresh: not stale (%s) — skipping.", staleness["reason"])
            return

        logger.info("Community refresh: stale (%s) — running detect_communities()+summarize_communities().", staleness["reason"])
        await community_detection.detect_communities()

        from src.graph import community_summarization
        result = await community_summarization.summarize_communities()
        logger.info(
            "Community refresh: run complete — %d attempted, %d written, %d skipped.",
            result.get("attempted", 0), result.get("written", 0), result.get("skipped", 0),
        )
    except Exception as exc:
        logger.error("Community refresh failed (best-effort, ingestion unaffected): %s", exc)
