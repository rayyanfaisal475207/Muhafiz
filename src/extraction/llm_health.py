# ============================================================
# Model-server health check — shared by every LLM-dependent extraction
# module (doc_classifier, ner, domain_entities, entity_resolution's
# adjudicator).
#
# The model server sits behind a rotating free-tier ngrok tunnel in dev
# (config.MODEL_SERVER_BASE_URL). Firing a batch of hundreds of extraction
# calls at a dead tunnel means hundreds of slow timeouts before anything
# reports a problem. Every batch entry point in this phase checks health
# once up front instead, and degrades gracefully (skip + log) rather than
# hard-failing the whole run.
# ============================================================

import logging

from src import config

logger = logging.getLogger(__name__)


async def model_server_healthy(timeout: float = 5.0) -> bool:
    """
    GET {MODEL_SERVER_BASE_URL}/health. Returns False (never raises) on any
    failure — an unreachable tunnel is an expected, common condition here,
    not an exceptional one.
    """
    base = (config.MODEL_SERVER_BASE_URL or "").rstrip("/")
    if not base:
        return False

    import httpx

    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(f"{base}/health")
            return response.status_code < 400
    except Exception as exc:
        logger.warning("Model server health check failed: %s", exc)
        return False
