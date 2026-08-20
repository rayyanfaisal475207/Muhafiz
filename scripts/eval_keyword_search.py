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
     full-text-search (`ts_rank`) path this codebase does NOT have.
     src/retrieval/vector_store.py's own module docstring says so
     explicitly: "Keyword search (Postgres ts_rank) does not have a
     Chroma equivalent, so it is dropped from this layer... the
     orchestrator already runs an independent BM25 + RRF pass in
     Python" — BM25 (src/retrieval/bm25_retriever.py) IS this
     codebase's keyword-search component. Evaluated directly, alone
     (no RRF fusion with semantic results — that combined case is what
     eval_end_to_end.py measures), against the same unscoped candidate
     pool orchestrator.py builds via get_all_chunks(where=None).
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.retrieval.bm25_retriever import retrieve_bm25
from src.retrieval.vector_store import get_all_chunks

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

    # One unscoped candidate pool, built once — same pattern
    # orchestrator.py itself uses per query (see its own get_all_chunks()
    # call sites); reused across queries here purely because eval runs
    # are read-only and the corpus doesn't change mid-run.
    pool = await get_all_chunks(where=None)

    for q in queries:
        query_text = _question_text(q)
        expected_source_docs = q.get("expected_source_docs", [])

        if not query_text or not expected_source_docs:
            total_queries -= 1
            continue

        try:
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
