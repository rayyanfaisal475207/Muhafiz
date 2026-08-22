import logging
import random

logger = logging.getLogger(__name__)


async def _run_entity_resolution_sampling_bg():
    """
    Ingestion Quality Control at Scale, Module G3 — see
    src/graph/entity_resolution_sampling.py's module docstring for why
    this is a fire-and-forget task scheduled at the same point conflict
    detection/reprioritization already are (src/ingestion/conflict_bg.py,
    reprioritization_bg.py), rather than new worker infrastructure.

    Throttled by SAMPLE_TRIGGER_PROBABILITY: this module's sample is a
    whole-corpus audit unrelated to whichever case just got ingested, so
    it does not need to run on every single document.

    Best-effort: a failure here must never fail the ingestion job it
    rides alongside, same resilience contract as the other two
    background tasks scheduled at this call site.
    """
    from src.graph import entity_resolution_sampling

    if random.random() >= entity_resolution_sampling.SAMPLE_TRIGGER_PROBABILITY:
        return
    try:
        result = await entity_resolution_sampling.run_sample()
        logger.info(
            "Entity-resolution consistency sampling: checked %d candidate(s), %d finding(s) recorded",
            result["sampled"], result["findings"],
        )
    except Exception as exc:
        logger.error("Entity-resolution consistency sampling failed: %s", exc)
