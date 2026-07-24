import asyncio
import json
import logging
from pathlib import Path
from src.database.db import get_db

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EVAL_SET_PATH = Path("data/eval/eic_eval_set.json")

async def evaluate_keyword_search():
    if not EVAL_SET_PATH.exists():
        logger.error(f"Eval set not found: {EVAL_SET_PATH}")
        return

    with open(EVAL_SET_PATH, 'r', encoding='utf-8') as f:
        eval_data = json.load(f)
    
    queries = eval_data.get("queries", [])
    if not queries:
        logger.warning("No queries found in eval set.")
        return

    total_queries = len(queries)
    successful_recalls = 0
    mrr_sum = 0.0

    logger.info(f"Starting Keyword Search (tsvector) Eval on {total_queries} queries.")
    
    async for conn in get_db()._pool.acquire():
        for q in queries:
            query_text = q.get("query_text", "")
            expected_doc_ids = q.get("expected_doc_ids", [])
            
            if not query_text or not expected_doc_ids:
                total_queries -= 1
                continue

            # This assumes we have a simple FTS search available in Postgres
            # To actually simulate the keyword module, we would call the actual keyword search function
            # E.g., src.retrieval.keyword_search.search(query_text)
            
            # Since this is a placeholder for the eval logic, we will call the actual retrieval function
            try:
                from src.retrieval.hybrid_search import get_keyword_results
                # get_keyword_results might be sync or async
                results = await get_keyword_results(query_text, top_k=10)
                retrieved_ids = [res["doc_id"] for res in results]
                
                # Calculate Recall@10
                hit = any(expected_id in retrieved_ids for expected_id in expected_doc_ids)
                if hit:
                    successful_recalls += 1
                
                # Calculate MRR
                for rank, doc_id in enumerate(retrieved_ids, 1):
                    if doc_id in expected_doc_ids:
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

    # Dump the results to a file for Go/No-Go decision
    result_path = "data/eval/keyword_eval_results.json"
    with open(result_path, 'w', encoding='utf-8') as f:
        json.dump({
            "total_queries": total_queries,
            "recall_at_10": recall,
            "mrr": mrr
        }, f, indent=2)

if __name__ == "__main__":
    asyncio.run(evaluate_keyword_search())
