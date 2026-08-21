"""
Phase 9 keyword-search-only eval — recall@10 and MRR of the BM25 leg in
isolation, against data/eval/eic_eval_set.json.

FIXED — M11 of the Muhafiz Data API migration
(docs/decisions/0001-muhafiz-api-migration.md). Three independent bugs:

  1. `eval_data.get("queries", [])` assumed a `{"queries": [...]}`
     wrapper; eic_eval_set.json is a bare list. Always evaluated 0
     queries.
  2. `from src.retrieval.hybrid_search import get_keyword_results` —
     that module does not exist anywhere in this codebase.
  3. `async for conn in get_db()._pool.acquire()` — a Postgres
     full-text-search (`ts_rank`) path this codebase did NOT have at the
     time. BM25 (src/retrieval/bm25_retriever.py) was and remains this
     codebase's actual keyword-RANKING component. Evaluated directly,
     alone (no RRF fusion with semantic results — that combined case is
     what eval_end_to_end.py measures).

     UPDATED — Graph Scale & Schema Expansion, Milestone A2: a Postgres
     tsvector/GIN index (`chunk_fulltext`, src/retrieval/fulltext_index.py)
     now DOES exist, but only as a persistent CANDIDATE-GENERATION index
     (which chunks share a token with the query) — it does not compute
     `ts_rank` or replace BM25's own scoring, which is still the same
     Python BM25Okapi pass this eval has always measured. The candidate
     pool below is now per-query (`fulltext_index.candidate_pool`), not
     one unscoped full-corpus fetch built once via
     `vector_store.get_all_chunks(where=None)` and reused across every
     query — narrower and index-backed, but not a ranking change.
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.retrieval.bm25_retriever import retrieve_bm25
from src.retrieval.fulltext_index import candidate_pool as bm25_candidate_pool

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EVAL_SET_PATH = Path("data/eval/eic_eval_set.json")
RESULTS_PATH = Path("data/eval/keyword_eval_results.json")


def _question_text(q: dict) -> str:
    return q.get("question_en") or q.get("question_ur") or q.get("question_roman_ur") or ""


async def evaluate_keyword_search():
    if not EVAL_SET_PATH.exists():
        logger.error(f"Eval set not found: {EVAL_SET_PATH}")
        return

    with open(EVAL_SET_PATH, "r", encoding="utf-8") as f:
        eval_data = json.load(f)

    queries = eval_data if isinstance(eval_data, list) else eval_data.get("queries", [])
    if not queries:
        logger.warning("No queries found in eval set.")
        return

    total_queries = len(queries)
    successful_recalls = 0
    mrr_sum = 0.0

    logger.info(f"Starting Keyword Search (BM25) Eval on {total_queries} queries.")

    for q in queries:
        query_text = _question_text(q)
        expected_source_docs = q.get("expected_source_docs", [])

        if not query_text or not expected_source_docs:
            total_queries -= 1
            continue

        try:
            # Milestone A2: the candidate pool is now per-query (a
            # persistent Postgres tsvector/GIN index lookup, not a full
            # unscoped corpus fetch reused across queries) — matches
            # orchestrator.py's own call sites, and mirrors this eval more
            # closely to actual production behavior per query.
            pool = await bm25_candidate_pool(query_text, where=None)
            results = retrieve_bm25(query_text, pool, top_k=10)
            retrieved_sources = [r.get("metadata", {}).get("source") for r in results]

            hit = any(expected in retrieved_sources for expected in expected_source_docs)
            if hit:
                successful_recalls += 1

            for rank, source in enumerate(retrieved_sources, 1):
                if source in expected_source_docs:
                    mrr_sum += 1.0 / rank
                    break

        except Exception as e:
            logger.error(f"Error executing search for query '{query_text}': {e}")

    recall = successful_recalls / total_queries if total_queries > 0 else 0
    mrr = mrr_sum / total_queries if total_queries > 0 else 0

    logger.info("=== Keyword Eval Results ===")
    logger.info(f"Queries: {total_queries}")
    logger.info(f"Recall@10: {recall:.2%}")
    logger.info(f"MRR: {mrr:.4f}")
    logger.info("============================")

    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "total_queries": total_queries,
            "recall_at_10": recall,
            "mrr": mrr,
        }, f, indent=2)


if __name__ == "__main__":
    asyncio.run(evaluate_keyword_search())
