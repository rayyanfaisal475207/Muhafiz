"""
Score the 32 Gold-QA answers against ground truth with DeepEval.

Scoring philosophy (per the testing team's explicit guidance):
  "Do NOT match word by word. If the LLM covers the main points in its own
   way — even with extra info — and gets the FACTS right, that's a pass.
   The problem is when it states something OPPOSITE or INCOMPLETE."

So the primary metric is a custom G-Eval 'FactualCorrectness' that rewards
semantic coverage of the key facts and penalizes contradiction/omission, NOT
lexical overlap. We also compute Answer Relevancy and Faithfulness, and derive
a pass/fail classification for precision/recall/F1.

Judge: Qwen via Groq (3-key rotation), fully local orchestration.

Run: .venv/Scripts/python.exe evaluation/gold32_score.py
"""
from __future__ import annotations
import json, os, sys, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.join(HERE, "gold32_pipeline_outputs.json")
RESULTS = os.path.join(HERE, "gold32_results.json")

# Faithfulness dropped from the live metric set: it decomposes the answer into
# atomic claims and makes one judge call per claim, which hangs indefinitely on
# the long analytical answers (M/G series, 3,500–5,000 chars) even after
# truncation. It was also the least meaningful metric here — our `context` is
# just the ground-truth answer (thin), so its grounding check was noisy. The two
# kept metrics are the ones that actually answer the evaluation question:
# FactualCorrectness (semantic, vs ground truth, per the team's guidance) and
# AnswerRelevancy. Any Faithfulness scores already collected are retained in the
# data but not required for a row to count as complete.
_METRICS = ["FactualCorrectness", "AnswerRelevancy"]


def _judge():
    # Judge = Gemini flash-lite. The earlier Qwen-27B judge (via Groq) was too
    # weak to honor the testing team's "semantic, close-numbers-OK" grading
    # rule — it reverted to literal fact-matching and unfairly scored correct
    # answers low (e.g. A1's exact-24-females + 92-vs-94 got 0.20). Gemini
    # flash-lite follows the nuanced instruction and scores the same case 0.80.
    # A raised per-attempt timeout accommodates Gemini's slower GEval calls.
    os.environ.setdefault("DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE", "180")
    from deepeval.models import GeminiModel
    key = os.environ.get("GEMINI_JUDGE_KEY") or os.environ.get("GEMINI_API_KEY")
    return GeminiModel(model="gemini-flash-lite-latest", api_key=key)


def build_metrics(judge):
    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric, GEval
    from deepeval.test_case import LLMTestCaseParams as P
    factual = GEval(
        name="FactualCorrectness",
        criteria=(
            "Judge whether the ACTUAL OUTPUT is factually correct compared to the "
            "EXPECTED OUTPUT (the verified ground-truth answer). Follow these rules "
            "strictly: (1) Do NOT require word-for-word matching or the same phrasing. "
            "(2) If the actual output covers the key facts of the expected answer — in "
            "its own words, and even with EXTRA correct information — that is a PASS "
            "(high score). (3) Penalize ONLY when the actual output states something "
            "that CONTRADICTS the expected answer, or is materially INCOMPLETE (omits a "
            "key fact the question asked for), or refuses/abstains when the expected "
            "answer has real content. (4) Numbers close to the expected (e.g. 92 vs 94 "
            "for a count that depends on de-duplication) are acceptable; wildly wrong "
            "numbers (4 vs 94) are a failure. (5) An answer that correctly says the data "
            "does not contain something, when the expected answer agrees, is a PASS."
        ),
        evaluation_params=[P.INPUT, P.ACTUAL_OUTPUT, P.EXPECTED_OUTPUT],
        model=judge, threshold=0.6,
    )
    # Faithfulness intentionally omitted — see the _METRICS comment. Kept only
    # the two metrics that matter and that don't hang on long answers.
    return {
        "FactualCorrectness": factual,
        "AnswerRelevancy": AnswerRelevancyMetric(model=judge, threshold=0.6, async_mode=False),
    }


def main():
    outs = json.load(open(OUTPUTS, encoding="utf-8"))
    from deepeval.test_case import LLMTestCase
    judge = _judge()
    metrics = build_metrics(judge)

    results, done = [], set()
    if os.path.exists(RESULTS):
        for r in json.load(open(RESULTS, encoding="utf-8")):
            if all(r["scores"].get(m) is not None for m in _METRICS):
                results.append(r); done.add(r["id"])
        if done: print(f"resuming — {len(done)} scored")

    import threading

    def _measure_once(metric, tc, box):
        try:
            metric.measure(tc)
            box["score"] = round(float(metric.score), 3)
            box["reason"] = (metric.reason or "")[:300]
        except Exception as e:  # noqa: BLE001
            box["exc"] = e

    def measure(metric, tc, tries=8):
        for a in range(tries):
            box = {}
            # Run each metric in a thread with a hard 120s cap. Faithfulness on
            # long analytical answers can hang indefinitely (claim-by-claim
            # explosion) — a timeout records None for THAT metric and moves on,
            # keeping the rest of the row intact rather than stalling the run.
            t = threading.Thread(target=_measure_once, args=(metric, tc, box), daemon=True)
            t.start(); t.join(timeout=120)
            if t.is_alive():
                return None, "TIMEOUT (metric exceeded 120s — likely long-answer claim explosion)"
            if "score" in box:
                time.sleep(2)
                return box["score"], box["reason"]
            e = box.get("exc")
            if e and any(x in str(e) for x in ("RateLimit", "rate_limit", "429")):
                time.sleep(min(90, 15 * (a + 1))); continue
            return None, f"ERROR: {e}"[:200]
        return None, "rate-limited"

    for o in outs:
        if o["id"] in done:
            continue
        actual = o.get("actual_answer") or "(no answer produced)"
        # Tight cap: Faithfulness splits the output into atomic claims and makes
        # ONE judge call PER claim — a 3,500–5,000-char analytical answer
        # explodes into dozens of serial calls and hangs the run (observed
        # stalling on M4/G6). The facts we score against appear early, so 900
        # chars preserves what matters while bounding the claim count.
        if len(actual) > 900:
            actual = actual[:900] + " …[truncated for scoring]"
        ctx = [o.get("expected_answer", "")]
        tc = LLMTestCase(input=o["question"], actual_output=actual,
                         expected_output=o.get("expected_answer", ""),
                         retrieval_context=ctx, context=ctx)
        row = {"id": o["id"], "type": o["type"], "language": o["language"],
               "route": o.get("route"), "scores": {}, "reasons": {}}
        for mn, m in metrics.items():
            sc, rs = measure(m, tc)
            row["scores"][mn] = sc; row["reasons"][mn] = rs
            print(f"  {o['id']:5} {mn:18} = {sc}", flush=True)
            json.dump(results + [row], open(RESULTS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
        results.append(row)
        json.dump(results, open(RESULTS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"\nwrote {len(results)} to {RESULTS}")


if __name__ == "__main__":
    main()
