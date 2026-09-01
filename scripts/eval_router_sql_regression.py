# ============================================================
# Quick regression check: confirm genuine SQL-shaped queries (real
# penal-code-section / is-this-offense-cognizable lookups) still route to
# SQL after tightening the SQL definition line in prompts/router.txt to
# exclude conceptual "why" questions. Companion to
# eval_router_direct_vs_rag.py — kept separate since these are true-SQL
# controls, not part of the DIRECT-vs-RAG eval set.
# ============================================================
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline.router import route_query

RUNS_PER_QUERY = 3

SQL_CONTROL_SET = [
    "What PPC section applies to mobile phone theft?",
    "Is cyber harassment a cognizable offense?",
    "Is theft of a motorcycle a cognizable offense?",
    "What section covers burglary?",
]


async def _route_with_retry(query: str, attempts: int = 5) -> dict:
    last_exc = None
    for attempt in range(attempts):
        try:
            return await route_query(query)
        except Exception as exc:
            last_exc = exc
            print(f"    (transient error, attempt {attempt + 1}/{attempts}: {exc})", flush=True)
            await asyncio.sleep(5 * (attempt + 1))
    raise last_exc


async def main():
    print(f"\n{'='*80}")
    print("SQL regression control — confirming real SQL lookups still route to SQL")
    print('='*80)
    all_ok = True
    for query in SQL_CONTROL_SET:
        votes = Counter()
        for _ in range(RUNS_PER_QUERY):
            decision = await _route_with_retry(query)
            votes[decision.get("route")] += 1
            await asyncio.sleep(12)
        route, count = votes.most_common(1)[0]
        ok = route == "SQL"
        all_ok = all_ok and ok
        print(f"[{'OK' if ok else 'REGRESSION'}] votes={dict(votes)}  {query}")
    print(f"\nAll genuine SQL queries still route to SQL: {all_ok}")


if __name__ == "__main__":
    asyncio.run(main())
