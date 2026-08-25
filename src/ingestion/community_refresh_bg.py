import logging

logger = logging.getLogger(__name__)


async def refresh_if_stale() -> dict:
    """
    The awaitable core of community refresh: check whether the
    whole-graph community partition has drifted enough to be worth a
    full recompute via community_detection.get_staleness() (the same
    10%-drift heuristic scripts/check_community_staleness.py already
    used manually), and only actually re-run
    detect_communities()/summarize_communities() when that heuristic
    says yes.

    Extracted from _run_community_refresh_bg() (findings.md Module 6)
    so a caller that must NOT fire-and-forget — e.g. a one-shot CLI sync
    script, where a background asyncio.create_task() would race the
    process's own connection-pool teardown/exit and very likely never
    finish — can await this directly instead. _run_community_refresh_bg()
    below is now a thin fire-and-forget wrapper around this function,
    unchanged in behavior/shape for its own caller (src/ingestion/service.py).

    Returns {"ran": bool, "staleness": <get_staleness() dict>,
    "summarize_result": <summarize_communities() dict> | None}.
    "ran" is False when the partition was not stale (nothing recomputed).

    Unlike _run_community_refresh_bg(), this function does NOT catch
    exceptions — best-effort/swallow-on-failure is the fire-and-forget
    wrapper's concern for its own caller, not this function's.
    """
    from src.graph import community_detection

    staleness = await community_detection.get_staleness()
    if not staleness["stale"]:
        logger.debug("Community refresh: not stale (%s) — skipping.", staleness["reason"])
        return {"ran": False, "staleness": staleness, "summarize_result": None}

    logger.info("Community refresh: stale (%s) — running detect_communities()+summarize_communities().", staleness["reason"])
    await community_detection.detect_communities()

    from src.graph import community_summarization
    result = await community_summarization.summarize_communities()
    logger.info(
        "Community refresh: run complete — %d attempted, %d written, %d skipped.",
        result.get("attempted", 0), result.get("written", 0), result.get("skipped", 0),
    )
    return {"ran": True, "staleness": staleness, "summarize_result": result}


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
    stale enough to be worth a full recompute" (refresh_if_stale(), the
    extracted core below — see its own docstring for the staleness
    heuristic and why this is now a thin wrapper around it).

    Best-effort, exactly like reprioritization_bg.py: a failure here must
    never fail the ingestion job it rides alongside — the community
    partition simply keeps its previous (possibly stale) shape until the
    next successful check, or a supervisor's manual
    /api/admin/community/refresh sweep.
    """
    try:
        await refresh_if_stale()
    except Exception as exc:
        logger.error("Community refresh failed (best-effort, ingestion unaffected): %s", exc)
