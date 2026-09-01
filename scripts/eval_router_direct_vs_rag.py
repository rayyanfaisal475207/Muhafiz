# ============================================================
# Labeled eval set for the router's DIRECT vs RAG classification —
# specifically the "why does this policy/practice exist" gap found live
# during Milestone 4 of the Muhafiz Knowledge Base integration.
# See RAG_ROUTER_RELIABILITY_FIX_PROMPT.md for the full rationale.
#
# Calls route_query() directly (no server/HTTP needed) for fast, repeatable
# eval iterations. Each query runs `RUNS_PER_QUERY` times since the
# classifier is itself an LLM call with some run-to-run variance — a single
# run isn't enough to say whether a fix actually helped.
#
# Run manually: python scripts/eval_router_direct_vs_rag.py
# ============================================================
import asyncio
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.pipeline.router import route_query

RUNS_PER_QUERY = 3

# (query, expected_route, category)
# "policy_rationale" = the gap being fixed: a "why" question about a
# specific police practice/law, which needs document lookup to answer
# correctly even though it's phrased as a general "why" question.
# "genuine_direct" = true negative controls: real "why" questions that
# SHOULD stay DIRECT, to catch overcorrection.
EVAL_SET = [
    ("Why doesn't the police store witness statement content themselves?", "RAG", "policy_rationale"),
    ("Why is a First Information Report required before an investigation starts?", "RAG", "policy_rationale"),
    ("Why are officer ranks structured the way they are under the Police Order?", "RAG", "policy_rationale"),
    ("Why is evidence packaged and labeled before it leaves a crime scene?", "RAG", "policy_rationale"),
    ("Why does the malkhana register track property issued to and returned from court?", "RAG", "policy_rationale"),
    ("Why can call data records be used to trace a phone number in an investigation?", "RAG", "policy_rationale"),
    ("Why do assault-related cases have special evidence marking rules?", "RAG", "policy_rationale"),
    ("Why does Section 154 CrPC require every complaint to be recorded in writing?", "RAG", "policy_rationale"),
    ("Why is a cognizable offence treated differently from a non-cognizable one?", "RAG", "policy_rationale"),
    ("Why do police need a warrant for some searches but not others?", "RAG", "policy_rationale"),
    # Negative controls — must stay DIRECT despite "why" phrasing.
    ("Why is the sky blue?", "DIRECT", "genuine_direct"),
    ("Why do cats purr?", "DIRECT", "genuine_direct"),
    ("Why did the match get postponed yesterday?", "DIRECT", "genuine_direct"),
    ("Why should I drink more water?", "DIRECT", "genuine_direct"),
]


async def _route_with_retry(query: str, attempts: int = 5) -> dict:
    """
    The local LLM tunnel and the cloud fallback have both shown transient
    failures during this project's live testing (dropped connections, TPM
    limits on a busy retry) — unrelated to router classification logic
    itself. Retry a few times with a short backoff so one bad call doesn't
    invalidate an entire eval run.
    """
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
    results = []
    for query, expected, category in EVAL_SET:
        votes = Counter()
        for _ in range(RUNS_PER_QUERY):
            decision = await _route_with_retry(query)
            votes[decision.get("route")] += 1
            # Pace calls: the router's own system prompt alone is ~7650 tokens
            # (see router.py's inline comment), so back-to-back calls that
            # each fall back to the cloud provider can exceed its 8000
            # tokens/minute account-wide budget on the SECOND call, before
            # any single call is individually oversized. Confirmed live —
            # every call in an unpaced run hit a 413 on the cloud fallback.
            await asyncio.sleep(12)
        majority_route, majority_count = votes.most_common(1)[0]
        consistent = majority_count == RUNS_PER_QUERY
        correct = majority_route == expected
        results.append((query, expected, dict(votes), correct, consistent, category))

    print(f"\n{'='*100}")
    print(f"Router DIRECT-vs-RAG eval — {len(EVAL_SET)} queries x {RUNS_PER_QUERY} runs each")
    print('='*100)
    n_correct = sum(1 for r in results if r[3])
    n_consistent = sum(1 for r in results if r[4])
    for query, expected, votes, correct, consistent, category in results:
        mark = "OK" if correct else "MISCLASSIFIED"
        stability = "" if consistent else " [INCONSISTENT across runs]"
        print(f"[{mark}]{stability} ({category}) expected={expected} votes={votes}")
        print(f"    {query}")
    print(f"\nCorrect (majority vote): {n_correct}/{len(EVAL_SET)}")
    print(f"Consistent (all {RUNS_PER_QUERY} runs agreed): {n_consistent}/{len(EVAL_SET)}")

    policy_results = [r for r in results if r[5] == "policy_rationale"]
    direct_results = [r for r in results if r[5] == "genuine_direct"]
    print(f"\npolicy_rationale correct: {sum(1 for r in policy_results if r[3])}/{len(policy_results)} (want: high)")
    print(f"genuine_direct correct:   {sum(1 for r in direct_results if r[3])}/{len(direct_results)} (want: all — regression check)")


if __name__ == "__main__":
    asyncio.run(main())
