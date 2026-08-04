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
import re
from collections import defaultdict, deque

from src import config

logger = logging.getLogger(__name__)

# C-2 (audit 2026-08-04): the ideal fix here is matching candidates back by
# a stable id/index the reranker call echoes through, removing the
# dependency on exact-text round-tripping entirely. Checked live against
# the actual RERANKER_URL server before implementing anything: it returns
# ONLY {"document": str, "score": float} — no index/id field — and
# silently ignores an "ids" field added to the request (confirmed by
# passing one and observing it never appears in the response). Index-based
# matching is therefore not achievable from this repo alone; it would need
# the reranker server itself changed, which lives outside this codebase
# (a RERANKER_URL-owning-team change, not a client-side fix). This
# collapses-whitespace fallback is the improvement actually available from
# here — it reduces, not eliminates, the fragility (a genuinely MODIFIED
# document — not just re-whitespaced — still can't be matched back), which
# is a real limitation, not a hidden one.
_WHITESPACE_RE = re.compile(r"\s+")


def _normalize_for_match(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


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
        results = response.json()  # bare list: [{"document": str, "score": float}, ...]

    # The API returns matched document TEXT + score, not an index — map each
    # result back to its source candidate by text. A per-text queue handles
    # duplicate chunk text safely (matches consumed in original order). The
    # normalized-text index is built lazily from whatever's still
    # unclaimed at match time, NOT pre-built alongside the exact index —
    # two independently-built structures pointing at the same underlying
    # candidates would let a candidate already consumed via the exact-match
    # path get matched a second time via the normalized path.
    text_to_candidates: dict[str, deque] = defaultdict(deque)
    for c in candidates:
        text_to_candidates[c.get("text", "")].append(c)

    def _remaining_normalized_index() -> dict[str, deque]:
        idx: dict[str, deque] = defaultdict(deque)
        for queue in text_to_candidates.values():
            for c in queue:
                idx[_normalize_for_match(c.get("text", ""))].append(c)
        return idx

    reranked: list[dict] = []
    for r in results:
        doc_text = r.get("document", "")
        queue = text_to_candidates.get(doc_text)
        matched_item = None
        if queue:
            matched_item = queue.popleft()
        else:
            # Exact match failed — try a whitespace-normalized fallback
            # before giving up (see the module-level comment: this covers
            # re-whitespacing, not a genuinely modified echo, which still
            # can't be recovered without server-side id support). Rebuilt
            # from what's still unclaimed each time — small candidate sets
            # (TOP_K_RERANK's input size) make this cheap enough.
            norm_queue = _remaining_normalized_index().get(_normalize_for_match(doc_text))
            if norm_queue:
                matched_item = norm_queue.popleft()
                text_to_candidates[matched_item.get("text", "")].remove(matched_item)
        if matched_item is None:
            logger.warning("Cross-reranker returned a document not in the candidate set — skipping")
            continue
        matched = dict(matched_item)
        matched["rerank_score"] = r.get("score", 0.0)
        reranked.append(matched)

    return reranked[:top_k]
