"""
Runs src/pipeline/validation.py's full semantic tier against
data/eval/validation_eval_set.json — the pre-work AGENT_HARNESS_
IMPLEMENTATION_PLAN.md §7.2 requires before validation.py ships
local-only: hand-built (claim, cited-chunk) pairs with known labels,
including deliberately overstated claims.

USAGE:
    python scripts/run_validation_eval.py

Requires a reachable LOCAL_LLM_URL (see .env / src/config.py) — this
script deliberately does NOT set AIR_GAP_MODE itself and does NOT pass
force_cloud, so it exercises exactly the same call path validate_answer()
uses in production: local-first, with call_llm()'s own shared cloud
fallback only on a genuine local failure (see validation.py's own
"LOCAL-ONLY, PERMANENTLY" docstring section for why that's the correct
posture to test against, not a stricter one this script would invent).

Set AIR_GAP_MODE=true before running if you want a hard failure instead
of a silent cloud-answered run when the local endpoint is unreachable —
recommended for an eval run whose whole point is measuring the LOCAL
model's accuracy.

OUTPUT: per-pair expected vs. actual verdict, overall accuracy, and a
confusion matrix over the three ClaimSupport labels. Exit code 0 always
(this is a report, not a test that should fail CI) — read the printed
accuracy and decide per §7.2's own rule: passes reliably -> ship
local-only as designed; doesn't -> narrow the check's scope, never add
cloud escalation.
"""

from __future__ import annotations

import asyncio
import json
from collections import Counter
from pathlib import Path

from src.pipeline.validation import validate_answer

EVAL_SET_PATH = Path(__file__).resolve().parent.parent / "data" / "eval" / "validation_eval_set.json"


async def main() -> None:
    pairs = json.loads(EVAL_SET_PATH.read_text(encoding="utf-8"))
    print(f"Loaded {len(pairs)} pairs from {EVAL_SET_PATH}")

    correct = 0
    confusion: Counter = Counter()
    rows: list[tuple[str, str, str, str]] = []

    for p in pairs:
        status, results = await validate_answer(
            p["answer_text"], p["cited_chunks"], tier="full"
        )
        expected = p["expected_support"]
        if status.value == "not_run":
            actual = "CALL_FAILED"
        elif not results:
            actual = "NO_PAIR_EXTRACTED"
        else:
            actual = results[0].support.value

        confusion[(expected, actual)] += 1
        is_correct = actual == expected
        correct += int(is_correct)
        rows.append((p["id"], expected, actual, "OK" if is_correct else "MISMATCH"))

    print()
    print(f"{'id':<10}{'expected':<22}{'actual':<22}result")
    for row in rows:
        print(f"{row[0]:<10}{row[1]:<22}{row[2]:<22}{row[3]}")

    print()
    print(f"Accuracy: {correct}/{len(pairs)} = {correct / len(pairs):.1%}")
    print()
    print("Confusion (expected -> actual): count")
    for (expected, actual), count in sorted(confusion.items()):
        print(f"  {expected} -> {actual}: {count}")


if __name__ == "__main__":
    asyncio.run(main())
