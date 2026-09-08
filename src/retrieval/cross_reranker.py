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
#
# RRF comes back for a second, different job in `cross_rerank_multi()`
# (Module 38): when the same candidates are cross-encoded against several
# phrasings of one question, those per-query scores are NOT comparable with
# each other, so the per-query lists are fused by rank — reusing
# reranker.py's `reciprocal_rank_fusion()` rather than repeating it here.
# ============================================================

import logging
import re
from collections import defaultdict, deque

from src import config
from src.retrieval.reranker import reciprocal_rank_fusion

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


# The key the cross-query fusion score is written under. Deliberately NOT
# "rrf_score": these chunks already carry the semantic-vs-BM25 fusion score
# under that name, and that earlier number is what `pipeline_logger` and the
# retrieval diagnostics mean by it.
CROSS_RRF_SCORE_KEY = "cross_rerank_rrf"

# [Module 38] How many phrasings may rescue their own rank-1 chunk past the
# fused window. Same shape, and the same reasoning, as reranker.py's
# SEMANTIC_FLOOR_MAX_RESCUED: rank-only fusion drops a candidate exactly one
# source is certain about, and a small, capped rescue is the fix — capped so
# it can never become a second route by which one phrasing floods the window.
#
# Every phrasing gets a voice, INCLUDING queries[0]. That is not symmetry for
# its own sake: measured live on KB8 and KB9 (MODULE38_RESULT.md §4), the
# chunk consensus dropped was the ORIGINAL question's own rank 1 — Punjab
# Police Rules chunks …_c1662 and …_c1561 respectively, each rank 1 for the
# Roman-Urdu question and ranks 18-25 for both statute hypotheses.
MAX_RESCUED_TOP_HITS = 2


async def cross_rerank_multi(
    queries: list[str], candidates: list[dict], top_k: int = None
) -> list[dict]:
    """
    Cross-rerank `candidates` against SEVERAL query phrasings and fuse the
    per-query ranked lists by RECIPROCAL RANK.

    By convention `queries[0]` is the original question and the rest are
    generated variants (Module 30's statute hypotheses), but no phrasing is
    privileged: the fusion depends only on the ranks, not on position. A
    per-list weight was built for this and measured away — see
    MODULE38_RESULT.md §2.

    Why several phrasings at all (Module 30): the cross-encoder is scored
    against one query string, and for a Roman-Urdu question about an English
    statute book that string carries almost no signal — measured live, every
    candidate came back inside 0.0007–0.0022, i.e. noise, and the correct
    CrPC s.173 chunk (RRF rank 1 going in) was cut. Scored against the same
    candidates with an English statute-vocabulary phrasing of the same
    question, that chunk ranked 2nd.

    Why by RANK and not by best score (Module 38): this function used to keep
    each candidate's MAXIMUM score across the phrasings. **Cross-encoder
    scores are not comparable across queries** — they are a per-query
    relevance judgement, not a calibrated absolute — so whichever phrasing
    happened to produce the largest numbers took the entire final window.
    Measured live on KB4: its two hypotheses were "Punjab Police Rules case
    property and malkhana" (the governing book for that question) and
    "Forensics guidelines handling and chain of custody" (not), the forensics
    query's scores simply ran higher, and the answer moved off the register
    material the question was about. The same mechanism is why Module 30
    measured a THIRD statute hypothesis as actively harmful for KB8 and KB9
    — re-measured under this fusion it no longer is (MODULE38_RESULT.md §8),
    but raising `DEFAULT_HYPOTHESES` is deliberately left to its own module.

    Reciprocal rank fusion removes the scale entirely: each phrasing votes by
    where it PUT a chunk, so a wrong hypothesis can promote its favourite
    chunks only as far as one list's votes reach, and a chunk that several
    phrasings rank well beats a chunk that exactly one of them loves. This is
    the same fusion `src/retrieval/reranker.py` already performs for
    semantic-vs-BM25, called directly rather than reimplemented — two
    implementations of one idea in one retrieval stack would drift.

    What fusion alone would cost, and the rescue that pays it: consensus
    ranking drops the chunk exactly ONE phrasing is certain about, which is
    the property Module 30 needed — and measured live on KB8 and KB9 the
    chunk it dropped was the ORIGINAL question's own rank 1, not a
    hypothesis's. So each phrasing keeps a guaranteed voice for its own rank-1
    candidate — capped at `MAX_RESCUED_TOP_HITS`, appended rather than
    promoted, so a wrong hypothesis gets one slot at the end instead of the
    whole window.

    Single-query behaviour is unchanged: with one query the fusion is
    monotonic in that query's own rank, so the order is `cross_rerank()`'s
    and the rescue never fires. Cost is one reranker call per query, so
    callers pass a small list.

    Returns up to `top_k + MAX_RESCUED_TOP_HITS` candidates in fused order,
    the rescued ones last. Each carries
    `CROSS_RRF_SCORE_KEY` (the fused score that decided the order) and
    "rerank_score" — the best cross-encoder score any phrasing gave it, kept
    because it is the interpretable per-chunk relevance number that reaches
    provenance and the UI. The order is the fusion's, not that score's.
    """
    if not candidates:
        return []

    top_k = top_k or config.TOP_K_RERANK
    usable = [q for q in queries if q and q.strip()]
    if not usable:
        return candidates[:top_k]

    # Ask each pass for every candidate, not top_k: a candidate cut by one
    # query's pass must still be able to place in another's ranking. Without
    # this the fusion could only ever see each query's own top_k.
    ranked_lists: list[list[dict]] = []
    best_score: dict[str, float] = {}
    for query in usable:
        ranked = await cross_rerank(query, candidates, top_k=len(candidates))
        ranked_lists.append(ranked)
        for chunk in ranked:
            score = chunk.get("rerank_score", 0.0)
            if score > best_score.get(chunk["id"], float("-inf")):
                best_score[chunk["id"]] = score

    merged = reciprocal_rank_fusion(
        ranked_lists,
        top_k=top_k,
        score_key=CROSS_RRF_SCORE_KEY,
        # A recency prior over the candidate POOL, already applied by the
        # fusion that built it. Re-applying it here would let a filename's
        # year outweigh several ranks of cross-encoder agreement.
        apply_year_boost=False,
    )

    # Rank fusion rewards agreement, and that is most of what we want — but it
    # can drop the one chunk a single phrasing is certain about, which is the
    # exact property Module 30 added this function for. Measured live on this
    # branch (MODULE38_RESULT.md §4), on the same candidate pools as the
    # before/after above: on KB8 and KB9 the chunk consensus dropped was the
    # ORIGINAL question's own rank 1 — Punjab Police Rules …_c1662 (rank 1 for
    # the Roman-Urdu question, 19 and 25 of 32 for the two hypotheses) and
    # …_c1561 (rank 1 for the question, 18 and 15). Neither survived pure
    # fusion; both are register/case-diary material the question is directly
    # about. So each phrasing keeps a guaranteed voice for its single
    # strongest candidate, appended and capped, exactly as reranker.py's
    # semantic floor rescues a high-confidence semantic-only hit that RRF's
    # rank-only math would have discarded.
    fused_ids = {c["id"] for c in merged}
    rescued: list[dict] = []
    for ranked in ranked_lists:
        if not ranked:
            continue
        top_hit = ranked[0]
        if top_hit["id"] in fused_ids:
            continue
        fused_ids.add(top_hit["id"])
        rescued.append(dict(top_hit))
    rescued = rescued[:MAX_RESCUED_TOP_HITS]
    if rescued:
        logger.info(
            "Cross-query fusion rescued %d rank-1 chunk(s) the fused window "
            "dropped: %s", len(rescued), [c["id"] for c in rescued],
        )
        merged = merged + rescued

    for chunk in merged:
        chunk["rerank_score"] = best_score.get(chunk["id"], chunk.get("rerank_score", 0.0))
    return merged
