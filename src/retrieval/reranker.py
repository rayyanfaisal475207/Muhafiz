# ============================================================
# RRF Re-ranker — Reciprocal Rank Fusion
#
# WHAT IS RRF?
# RRF is an algorithm that combines multiple ranked lists of documents
# into a single, better-ranked list. It was introduced in the 2009 paper
# "Reciprocal Rank Fusion outperforms Condorcet and individual Rank
# Learning Methods" by Cormack, Clarke, and Buettcher.
#
# THE CORE INSIGHT:
# A document that appears near the top of TWO different ranking systems
# (semantic search AND keyword search) is almost certainly more relevant
# than one that tops only one list. RRF turns this intuition into math.
#
# THE FORMULA (understand every part):
#   RRF_score(doc) = sum over each ranked list of: 1 / (rank + k)
#
# Where:
# - rank: the document's position in that list (1-indexed: 1st = rank 1)
# - k: a constant (typically 60). Acts as a smoothing factor that prevents
#   the #1 result from dominating too heavily. With k=60, rank 1 gives
#   1/61 ≈ 0.016, rank 10 gives 1/70 ≈ 0.014 — the difference between
#   top and middle is smaller than it would be without k.
# - The sum is across all lists the document appears in.
#
# WORKED EXAMPLE (from context.md):
#   Semantic: [Doc A, Doc B, Doc C]  → A=rank1, B=rank2, C=rank3
#   BM25:     [Doc C, Doc A, Doc D]  → C=rank1, A=rank2, D=rank3
#
#   Doc A: 1/(1+60) + 1/(2+60) = 0.01639 + 0.01613 = 0.03252  ← winner
#   Doc C: 1/(3+60) + 1/(1+60) = 0.01587 + 0.01639 = 0.03226
#   Doc B: 1/(2+60)            = 0.01613
#   Doc D: 1/(3+60)            = 0.01587
#
# Doc A wins because it's highly ranked in BOTH lists.
# ============================================================

import logging
from typing import Optional

logger = logging.getLogger(__name__)

RRF_K = 60  # The standard smoothing constant

# Semantic confidence floor (root-caused 2026-08-02): pure RRF only ever
# looks at a document's ORDINAL RANK in each list, never its raw similarity
# magnitude — a chunk found ONLY by semantic search, even at cosine
# similarity 0.90+, contributes a single term (1/(rank+RRF_K)) that
# routinely loses to a chunk ranking merely moderately in BOTH lists (two
# smaller terms summed). Confirmed live: for "What is the procedure to get
# a certified copy of an FIR, and what's the fee?", REAL-004-copy-of-fir-
# procedure.pdf — the only actually-relevant document — scored the #1 or #2
# highest raw semantic similarity (0.90-0.94) on every retry, but never
# appeared in the fused top-10 because BM25 never surfaced it at all: its
# distinguishing terms ("certified", "copy", "procedure", "fee") get diluted
# by the word "FIR" appearing in hundreds of unrelated case documents across
# the corpus. This is a real gap in RRF, not a tokenization or indexing bug
# (checked directly — the tokenizer overlaps correctly on both sides).
SEMANTIC_FLOOR_SCORE = 0.85
# Capped, not unconditional — this rescues a small number of very-high-
# confidence chunks RRF's math would otherwise discard, it must not become
# a backdoor that lets semantic search alone flood the candidate pool.
SEMANTIC_FLOOR_MAX_RESCUED = 2


def reciprocal_rank_fusion(
    ranked_lists: list[list[dict]],
    top_k: int = 5,
    id_key: str = "id",
) -> list[dict]:
    """
    Combine multiple ranked document lists using Reciprocal Rank Fusion.

    Args:
        ranked_lists:  A list of ranked document lists. Each inner list
                       contains document dicts in descending relevance order
                       (most relevant first = rank 1).
                       Example: [[doc_a, doc_b, doc_c], [doc_c, doc_a, doc_d]]
        top_k:         How many top documents to return after fusion.
        id_key:        The key in each document dict that holds the unique ID.
                       Used to match the same document across different lists.

    Returns:
        Top-k documents sorted by their RRF score (highest first).
        Each document dict has an added "rrf_score" key.
    """
    if not ranked_lists:
        return []

    # ── Step 1: Compute RRF scores ────────────────────────────────────────
    # rrf_scores maps doc_id → cumulative RRF score
    rrf_scores: dict[str, float] = {}
    # doc_lookup maps doc_id → the full document dict (so we can return it)
    doc_lookup: dict[str, dict] = {}

    for ranked_list in ranked_lists:
        for rank, doc in enumerate(ranked_list, start=1):  # rank is 1-indexed
            doc_id = doc.get(id_key)
            if doc_id is None:
                logger.warning("Document missing '%s' key — skipping in RRF", id_key)
                continue

            # Add 1/(rank + k) to this document's cumulative score
            rrf_scores[doc_id] = rrf_scores.get(doc_id, 0.0) + (1.0 / (rank + RRF_K))

            # Store the full document dict for later retrieval
            if doc_id not in doc_lookup:
                doc_lookup[doc_id] = doc

    # ── Step 2: Sort by RRF score (descending) ────────────────────────────
    sorted_ids = sorted(rrf_scores, key=lambda did: rrf_scores[did], reverse=True)

    # ── Step 3: Build result list with scores attached ────────────────────
    results: list[dict] = []
    import re
    for doc_id in sorted_ids:
        doc = dict(doc_lookup[doc_id])  # copy to avoid mutating original
        
        # Apply a time-decay boost to prioritize newer documents
        year_boost = 0.0
        # Check source filename in metadata or root
        source_str = doc.get("source") or doc.get("metadata", {}).get("source", "")
        if source_str:
            year_match = re.search(r'\b(20\d{2})\b', str(source_str))
            if year_match:
                year = int(year_match.group(1))
                # Base year 2020. Give 0.0005 boost per year.
                # A document from 2026 gets +0.003
                # A document from 2024 gets +0.002
                # This breaks ties and slightly elevates newer docs
                year_boost = max(0, (year - 2020) * 0.0005)
                
        doc["rrf_score"] = round(rrf_scores[doc_id] + year_boost, 6)
        results.append(doc)
        
    # Re-sort after applying the year boost
    results = sorted(results, key=lambda x: x["rrf_score"], reverse=True)[:top_k]

    logger.debug(
        "RRF: merged %d list(s) → %d unique docs → top %d selected",
        len(ranked_lists),
        len(rrf_scores),
        len(results),
    )

    return results


def rerank_results(
    semantic_results: list[dict],
    bm25_results: list[dict],
    top_k: int = 5,
) -> list[dict]:
    """
    Convenience wrapper: merge semantic + BM25 results using RRF.

    This is the function called by the orchestrator. It hides the internal
    RRF implementation detail — the caller doesn't need to know about rank
    lists and k constants.

    Args:
        semantic_results: Top-k chunks from ChromaDB vector search,
                          ordered by cosine similarity (most similar first).
        bm25_results:     Top-k chunks from BM25 keyword search,
                          ordered by BM25 score (highest first).
        top_k:            How many documents to return after re-ranking.

    Returns:
        Re-ranked list of top_k document dicts with "rrf_score" attached.
    """
    # Handle the case where one retrieval method returned nothing
    non_empty_lists = [
        lst for lst in [semantic_results, bm25_results] if lst
    ]

    if not non_empty_lists:
        logger.warning("Both semantic and BM25 results are empty — no documents to rerank.")
        return []

    fused = reciprocal_rank_fusion(
        ranked_lists=non_empty_lists,
        top_k=top_k,
    )

    # Semantic floor: rescue very-high-confidence semantic-only hits RRF's
    # rank-only math would otherwise drop entirely (see SEMANTIC_FLOOR_SCORE
    # above). semantic_results' own "rrf_score" is still the raw cosine
    # similarity at this point — reciprocal_rank_fusion() copies dicts
    # rather than mutating them, so this reads the pre-fusion value, not the
    # fused one. Appending (not replacing) means the pool can grow by up to
    # SEMANTIC_FLOOR_MAX_RESCUED beyond top_k; downstream cross-encoder
    # reranking narrows it back down, so this is a bounded, safe pool grow,
    # not a candidate to actually deliver un-reranked.
    fused_ids = {doc.get("id") for doc in fused}
    rescued = [
        doc for doc in semantic_results
        if doc.get("id") not in fused_ids and doc.get("rrf_score", 0.0) >= SEMANTIC_FLOOR_SCORE
    ][:SEMANTIC_FLOOR_MAX_RESCUED]
    if rescued:
        logger.info(
            "Semantic floor rescued %d chunk(s) RRF fusion would have dropped: %s",
            len(rescued),
            [d.get("id") for d in rescued],
        )
        fused = fused + rescued

    return fused
