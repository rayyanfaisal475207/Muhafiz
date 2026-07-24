# ============================================================
# BM25 Retriever — Keyword-Based Search
#
# WHAT IS BM25?
# BM25 (Best Match 25) is the gold-standard keyword search algorithm.
# It's what traditional search engines (Elasticsearch, Lucene) use.
# Unlike semantic search, BM25 finds documents that share exact words
# with the query, weighted by term frequency and inverse document frequency.
#
# WHY BOTH BM25 AND SEMANTIC SEARCH?
# Neither approach is perfect alone:
# - Semantic search: catches "aspirin" vs "acetylsalicylic acid" (synonyms)
#   but might miss a document that says literally "500mg dosage" when you
#   ask "what is the dose of aspirin?"
# - BM25: catches exact matches but misses synonyms and paraphrases
#
# HYBRID RETRIEVAL (BM25 + Semantic):
# - Run both searches independently
# - Feed their ranked result lists into RRF (Reciprocal Rank Fusion)
# - RRF combines them: chunks appearing high in BOTH lists win
#
# This is called "hybrid retrieval" and consistently outperforms either
# approach alone in RAG benchmarks.
#
# IMPLEMENTATION NOTE:
# The BM25 index is built IN MEMORY at search time by loading documents
# from ChromaDB. For a production system with millions of documents, you'd
# want a persistent BM25 index (e.g., Elasticsearch, Typesense).
# For this project, in-memory BM25 is fine.
# ============================================================

import logging
from typing import Optional

from src.ingestion.tokenizer import tokenize

logger = logging.getLogger(__name__)


def retrieve_bm25(
    query: str,
    all_documents: list[dict],
    top_k: int = 10,
) -> list[dict]:
    """
    Perform BM25 keyword search over a set of documents.

    Args:
        query:          The search query string.
        all_documents:  List of document dicts, each with at least a "text" key.
                        Typically loaded from ChromaDB via get_all_documents_metadata()
                        + their text content.
        top_k:          Number of top results to return.

    Returns:
        List of the top_k most relevant documents according to BM25 scoring,
        in descending order of relevance score.
        Each dict is from all_documents with an added "bm25_score" key.
    """
    try:
        from rank_bm25 import BM25Okapi
    except ImportError:
        logger.warning(
            "rank_bm25 is not installed. BM25 retrieval disabled. "
            "Install with: pip install rank-bm25"
        )
        return []

    if not all_documents:
        return []

    # Tokenize with the shared Urdu-aware tokenizer (Phase 2.2/2.5).
    #
    # This used to be `.lower().split()`. Both halves of that were wrong
    # for this corpus: `.lower()` is a no-op on Urdu (a caseless script),
    # and whitespace-splitting leaves Urdu punctuation (۔ ؟ ،) glued to
    # the words it follows, so "ہے۔" and "ہے" indexed as different terms.
    # Worse, it did nothing about Arabic-vs-Urdu codepoint variants
    # (ک/ك, ی/ي, ہ/ه), which meant a query and a chunk spelling the same
    # word with different keyboards scored zero against each other.
    #
    # CORPUS AND QUERY MUST USE THE SAME TOKENIZER — if these two calls
    # ever diverge, every score silently collapses toward zero.
    tokenized_corpus = [tokenize(doc["text"]) for doc in all_documents]

    # Build BM25 index
    bm25 = BM25Okapi(tokenized_corpus)

    # Score each document against the query
    tokenized_query = tokenize(query)
    scores = bm25.get_scores(tokenized_query)

    # Pair documents with their scores and sort descending
    scored_docs = [
        {**doc, "bm25_score": float(score)}
        for doc, score in zip(all_documents, scores)
    ]
    scored_docs.sort(key=lambda d: d["bm25_score"], reverse=True)

    # Return top_k (filter out zero-score documents — they have no matching terms)
    results = [d for d in scored_docs[:top_k] if d["bm25_score"] > 0]

    logger.debug(
        "BM25 retrieved %d documents for query: '%s'", len(results), query[:50]
    )
    return results
