"""
[Gold-QA fix — Module 95] All-32 aggregate regression control.

Runs, for every question in the gold set, BOTH halves of the control the
plan asks for and nothing else:

  1. `xagg.resolve_aggregate_kind()` — the dispatch equality control. Pure,
     no DB, no backend.
  2. `xagg.run_aggregate()` + the shared renderer — the ANSWER equality
     control at the aggregate layer, i.e. the exact text the orchestrator
     would put in front of the model.

Writes one JSON file. Run it on the merge-base, then on the branch, then
diff the two files: every question except the ones the module intends to
move must be byte-identical.

No LLM, no backend, no gateway HTTP — reads the shared Postgres/AGE graph
and the local `cases` table only.

Run:  python scripts/module95_aggregate_baseline.py <out.json>
"""
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline import xagg
from src.pipeline.harness.tools.xagg import _render_aggregate_text

GOLD = Path(__file__).resolve().parent.parent / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"


async def main() -> None:
    out_path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("module95_baseline.json")
    questions = json.loads(GOLD.read_text(encoding="utf-8"))

    from src.data_gateway.selector import get_gateway
    gateway = await get_gateway()

    results = {}
    for item in questions:
        qid, q = item["id"], item["question"]
        kind = xagg.resolve_aggregate_kind(q)
        entry = {"kind": kind}
        try:
            agg = await xagg.run_aggregate(
                q, target_entity=None, gateway=gateway,
                user_id=None, user_role="supervisor",
            )
            entry["rendered"] = _render_aggregate_text(agg)
        except Exception as exc:  # a family that legitimately refuses still has a stable shape
            entry["error"] = f"{type(exc).__name__}: {exc}"
        results[qid] = entry
        print(f"{qid:4s} {kind}")

    out_path.write_text(json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nWrote {out_path}")


if __name__ == "__main__":
    asyncio.run(main())
