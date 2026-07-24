import asyncio
import json
import logging
from pathlib import Path
from src.retrieval.hybrid_search import get_hybrid_results

# NOTE: You will need to wire this into your main orchestrator or final 
# pipeline entrypoint for the end-to-end evaluation. This currently 
# mocks an end-to-end RAG check against expected document IDs.

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

EVAL_SET_PATH = Path("data/eval/eic_eval_set.json")
RESULTS_PATH = Path("data/eval/eval_results_phase9.json")

async def evaluate_end_to_end():
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

    logger.info(f"Starting E2E Eval on {total_queries} queries.")
    
    for q in queries:
        query_text = q.get("query_text", "")
        expected_doc_ids = q.get("expected_doc_ids", [])
        
        if not query_text or not expected_doc_ids:
            total_queries -= 1
            continue

        try:
            # Here we would normally call the entire pipeline and evaluate 
            # if the final generated answer references the correct document 
            # or if the retrieval stage surfaced it.
            # For this script, we'll hit the hybrid_search as a proxy for the stack
            results = await get_hybrid_results(query_text, top_k=5)
            retrieved_ids = [res["doc_id"] for res in results]
            
            # Did the full retrieval stack find it?
            hit = any(expected_id in retrieved_ids for expected_id in expected_doc_ids)
            if hit:
                successful_recalls += 1

        except Exception as e:
            logger.error(f"Error processing query '{query_text}': {e}")
            
    pass_rate = successful_recalls / total_queries if total_queries > 0 else 0

    logger.info("=== Phase 9 E2E Eval Results ===")
    logger.info(f"Queries: {total_queries}")
    logger.info(f"Pass Rate: {pass_rate:.2%}")
    logger.info("=================================")

    # Lock in numbers
    with open(RESULTS_PATH, 'w', encoding='utf-8') as f:
        json.dump({
            "total_queries": total_queries,
            "pass_rate": pass_rate
        }, f, indent=2)
    logger.info(f"Results locked into {RESULTS_PATH}")

if __name__ == "__main__":
    asyncio.run(evaluate_end_to_end())
