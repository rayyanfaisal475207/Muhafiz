# ============================================================
# Cross-Encoder Re-ranker — bge-reranker-v2-m3 (served locally)
#
# RRF (src/retrieval/reranker.py) only fuses rank *positions* from semantic
# + BM25 search — it never looks at the actual text. A cross-encoder scores
# the query against each candidate's full text jointly, which is a much
# stronger relevance signal but too expensive to run over the whole corpus.
# So the pipeline runs it as a second pass over RRF's already-fused,
# still-wide candidate set, cutting down to the final top_k that goes to
# the evaluator/response LLM.
# ============================================================

import logging
from collections import defaultdict, deque

from src import config

logger = logging.getLogger(__name__)


async def cross_rerank(query: str, candidates: list[dict], top_k: int = None) -> list[dict]:
    """
    Re-score RRF-fused candidates with the cross-encoder reranker and return
    the top_k by its score.

    Args:
        query:      The search query to score candidates against.
        candidates: RRF-fused chunk dicts (must have a "text" key).
        top_k:      How many to return. Defaults to config.TOP_K_RERANK.

    Returns:
        Up to top_k candidate dicts, in cross-encoder score order (highest
        first), each with a "rerank_score" key added.
    """
    if not candidates:
        return []

    top_k = top_k or config.TOP_K_RERANK

    if not config.RERANKER_URL:
        logger.warning("RERANKER_URL not configured — skipping cross-encoder rerank, keeping RRF order")
        return candidates[:top_k]

    import httpx

    documents = [c.get("text", "") for c in candidates]

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.post(
            config.RERANKER_URL,
            json={"query": query, "documents": documents, "top_k": top_k},
        )
        response.raise_for_status()
        results = response.json()["results"]  # [{"document": str, "score": float}, ...]

    # The API returns matched document TEXT + score, not an index — map each
    # result back to its source candidate by text. A per-text queue handles
    # duplicate chunk text safely (matches consumed in original order).
    text_to_candidates: dict[str, deque] = defaultdict(deque)
    for c in candidates:
        text_to_candidates[c.get("text", "")].append(c)

    reranked: list[dict] = []
    for r in results:
        queue = text_to_candidates.get(r.get("document", ""))
        if not queue:
            logger.warning("Cross-reranker returned a document not in the candidate set — skipping")
            continue
        matched = dict(queue.popleft())
        matched["rerank_score"] = r.get("score", 0.0)
        reranked.append(matched)

    return reranked[:top_k]
