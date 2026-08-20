"""
Phase 9 end-to-end retrieval eval — recall@5 of the real hybrid
retrieval stack (semantic + BM25 + RRF fusion) against data/eval/eic_eval_set.json.

FIXED — M11 of the Muhafiz Data API migration
(docs/decisions/0001-muhafiz-api-migration.md). Two independent bugs,
neither related to which corpus the eval set describes:

  1. `eval_data.get("queries", [])` assumed a `{"queries": [...]}` wrapper.
     eic_eval_set.json has always been a bare list — this always
     evaluated 0 queries and logged "No queries found", silently.
  2. `from src.retrieval.hybrid_search import get_hybrid_results` — that
     module does not exist anywhere in this codebase (confirmed by
     directory listing before writing this fix, not assumed). The real
     hybrid stack is orchestrator.py's own sequence: embed_text() ->
     query_similar() [semantic] + retrieve_bm25() [keyword] ->
     rerank_results() [RRF fusion] — reproduced directly below rather
     than importing a module that was never real.

`where=None` (unscoped search across the whole corpus) is intentional
and eval-only — never do this in request-serving code (see
src/retrieval/vector_store.py's own "unscoped search = the documented
multi-tenant leak" warning); an offline eval script measuring raw
retrieval quality has no case/tenant boundary to respect.

Matches against `expected_source_docs` by chunk `metadata["source"]` —
the same field every chunk carries regardless of which corpus produced
it (synthetic file, or a Muhafiz Data API record via
src/ingestion/muhafiz_records.py).
"""
import asyncio
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.retrieval.bm25_retriever import retrieve_bm25
from src.retrieval.embedder import embed_text
from src.retrieval.reranker import rerank_results
from src.retrieval.vector_store import get_all_chunks, query_similar

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EVAL_SET_PATH = Path("data/eval/eic_eval_set.json")
RESULTS_PATH = Path("data/eval/eval_results_phase9.json")


async def retrieve(query_text: str, top_k: int = 5) -> list[dict]:
    """The real hybrid stack, reproduced from orchestrator.py's own
    sequence — semantic search + BM25 + RRF fusion, unscoped (see module
    docstring)."""
    embedding = await embed_text(query_text)
    semantic = await query_similar(query_text, embedding, top_k=top_k, where=None)
    pool = await get_all_chunks(where=None)
    bm25 = retrieve_bm25(query_text, pool, top_k=top_k)
    return rerank_results(semantic, bm25, top_k=top_k)


def _question_text(q: dict) -> str:
    return q.get("question_en") or q.get("question_ur") or q.get("question_roman_ur") or ""


async def evaluate_end_to_end():
    if not EVAL_SET_PATH.exists():
        logger.error(f"Eval set not found: {EVAL_SET_PATH}")
        return

    with open(EVAL_SET_PATH, "r", encoding="utf-8") as f:
        eval_data = json.load(f)

    # eic_eval_set.json is a bare list, not {"queries": [...]}.
    queries = eval_data if isinstance(eval_data, list) else eval_data.get("queries", [])
    if not queries:
        logger.warning("No queries found in eval set.")
        return

    total_queries = len(queries)
    successful_recalls = 0

    logger.info(f"Starting E2E Eval on {total_queries} queries.")

    for q in queries:
        query_text = _question_text(q)
        expected_source_docs = q.get("expected_source_docs", [])

        if not query_text or not expected_source_docs:
            total_queries -= 1
            continue

        try:
            results = await retrieve(query_text, top_k=5)
            retrieved_sources = [r.get("metadata", {}).get("source") for r in results]

            hit = any(expected in retrieved_sources for expected in expected_source_docs)
            if hit:
                successful_recalls += 1

        except Exception as e:
            logger.error(f"Error processing query '{query_text}': {e}")

    pass_rate = successful_recalls / total_queries if total_queries > 0 else 0

    logger.info("=== Phase 9 E2E Eval Results ===")
    logger.info(f"Queries: {total_queries}")
    logger.info(f"Pass Rate: {pass_rate:.2%}")
    logger.info("=================================")

    with open(RESULTS_PATH, "w", encoding="utf-8") as f:
        json.dump({
            "total_queries": total_queries,
            "pass_rate": pass_rate,
        }, f, indent=2)
    logger.info(f"Results locked into {RESULTS_PATH}")


if __name__ == "__main__":
    asyncio.run(evaluate_end_to_end())
