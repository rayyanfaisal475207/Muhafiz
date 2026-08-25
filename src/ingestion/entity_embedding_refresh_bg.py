import logging

logger = logging.getLogger(__name__)


async def _run_entity_embedding_refresh_bg() -> None:
    """
    Fire-and-forget entity-description-embedding refresh, findings.md
    Module 8 (Local Search) — mirrors community_refresh_bg.py's own
    fire-and-forget wrapper shape exactly (same file-location split: the
    real logic lives in src/graph/, this thin wrapper lives in
    src/ingestion/ next to the other three per-document background tasks
    src/ingestion/service.py already schedules this way).

    Not case-scoped, same reasoning as _run_community_refresh_bg(): a new
    or changed entity anywhere in the graph should be embedded, not just
    ones in the case that happened to trigger this ingestion event.
    refresh_entity_embeddings() itself is a plain incremental diff (see its
    own module docstring) rather than a staleness-gated recompute — cheap
    enough to just run on every ingestion event without its own drift
    heuristic.

    Best-effort, exactly like the other three background tasks
    src/ingestion/service.py schedules alongside this one: a failure here
    must never fail the ingestion job it rides alongside — the entity
    embedding collection simply keeps its previous (possibly stale) shape
    until the next successful run, or the next full `scripts/
    sync_muhafiz_data.py` sweep.
    """
    try:
        from src.graph.entity_embedding_refresh import refresh_entity_embeddings

        result = await refresh_entity_embeddings()
        logger.info(
            "Entity embedding refresh: %d scanned, %d upserted, %d deleted.",
            result.get("scanned", 0), result.get("upserted", 0), result.get("deleted", 0),
        )
    except Exception as exc:
        logger.error("Entity embedding refresh failed (best-effort, ingestion unaffected): %s", exc)
