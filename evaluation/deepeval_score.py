"""
DeepEval scoring — pair gold_set.json (ground truth from the raw Data API)
with pipeline_outputs.json (actual answers from /api/chat) and score with a
suite of RAG-evaluation metrics.

Judge model: Gemini (uses the project's existing GEMINI_API_KEY — no OpenAI,
nothing sent to OpenAI/LangSmith; Gemini is already the app's own fallback
provider, so no NEW third party is introduced).

Metrics (demo-worthy, each measuring a distinct quality dimension):
  - Answer Relevancy      : does the answer address the question?
  - Faithfulness          : are the answer's claims grounded in the context?
  - Hallucination         : does the answer contradict / invent beyond context?
  - Correctness (G-Eval)  : does the answer match the ground-truth facts?
  - Name/Fact Fidelity    : custom G-Eval — are entity names/numbers exact?
  - (Contextual Precision/Recall where retrieval context is available)

Produces deepeval_results.json + a printed summary table.

Run:  .venv/Scripts/python.exe evaluation/deepeval_score.py
"""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.abspath(__file__))
GOLD = os.path.join(HERE, "gold_set.json")
OUTPUTS = os.path.join(HERE, "pipeline_outputs.json")
RESULTS = os.path.join(HERE, "deepeval_results.json")


def _judge():
    """Groq judge via LiteLLM. Chosen over Gemini because Gemini's free tier
    is 20 requests/DAY/key (exhausts almost immediately across ~85 judge
    calls and competes with the app), whereas Groq's limits are per-minute
    with far more daily headroom. Uses the app's own GROQ_API_KEY — no OpenAI,
    no external eval service, no new third party.

    Judge model: gpt-oss-120b is the app's own GROQ_MODEL and the strongest
    JSON-schema follower in the account's Groq catalog — DeepEval's metrics
    require strict structured output, which smaller models fail."""
    # Silence LiteLLM's verbose logging: it echoes the full model tuple
    # INCLUDING THE API KEY to stdout, which must never reach a log or report.
    import logging
    os.environ["LITELLM_LOG"] = "ERROR"
    logging.getLogger("LiteLLM").setLevel(logging.ERROR)
    try:
        import litellm
        litellm.suppress_debug_info = True
        litellm.set_verbose = False
    except Exception:
        pass

    from deepeval.models import LiteLLMModel
    from src import config
    # Rotate across the first 3 (updated) Groq keys — each judge call picks the
    # next key round-robin, tripling the effective per-minute rate limit. This
    # is the same key-pool strategy the app itself uses; here it lets the
    # cross-case rows (long answers = many judge calls) finish without stalling.
    import itertools
    # The numbered keys live in os.environ (the app's key_manager reads them
    # there), NOT as config attributes — read them directly.
    keys = [os.environ.get(f"GROQ_API_KEY_{i}") for i in (1, 2, 3)]
    keys = [k for k in keys if k] or [getattr(config, "GROQ_API_KEY", None) or os.environ.get("GROQ_API_KEY")]
    key_cycle = itertools.cycle(keys)
    model_name = "qwen/qwen3.8-27b"  # handles DeepEval's strict JSON; gpt-oss does not

    from pydantic import SecretStr

    class GroqJudge(LiteLLMModel):
        # (1) Groq rejects `logprobs`, which GEval requests by default — raising
        #     AttributeError forces GEval's schema-only fallback path.
        # (2) Rotate the API key before EACH generation so consecutive judge
        #     calls spread across the 3-key pool. LiteLLMModel stores api_key
        #     as a SecretStr, so the rotated key MUST be wrapped the same way —
        #     assigning a bare str breaks its require_secret_api_key() path.
        def _rotate(self):
            self.api_key = SecretStr(next(key_cycle))

        def generate(self, *a, **k):
            self._rotate()
            return super().generate(*a, **k)

        async def a_generate(self, *a, **k):
            self._rotate()
            return await super().a_generate(*a, **k)

        def generate_raw_response(self, *a, **k):
            raise AttributeError("logprobs unsupported on Groq — use schema fallback")

        async def a_generate_raw_response(self, *a, **k):
            raise AttributeError("logprobs unsupported on Groq — use schema fallback")

    return GroqJudge(model=f"groq/{model_name}", api_key=keys[0])


def build_cases(gold: list, outputs: dict):
    from deepeval.test_case import LLMTestCase
    cases = []
    for g in gold:
        out = outputs.get(g["id"])
        if not out:
            continue
        # context = ground-truth facts + reference answer + any retrieved text
        ctx = []
        if g.get("retrieval_context"):
            ctx.append(g["retrieval_context"])
        if g.get("expected_facts"):
            ctx.append(json.dumps(g["expected_facts"], ensure_ascii=False))
        if out.get("retrieval_context"):
            ctx.extend(out["retrieval_context"])
        ctx = [c for c in ctx if c] or [g.get("expected_answer", "")]

        # Cap the answer fed to the judge. Faithfulness/Hallucination split the
        # output into atomic claims and make ONE judge call per claim — a
        # 3,300-char answer explodes into dozens of serial calls and stalls the
        # run. The ground-truth facts we score against appear early in every
        # answer, so a generous cap preserves what matters while bounding cost.
        actual = out.get("actual_answer") or "(no answer produced)"
        if len(actual) > 1500:
            actual = actual[:1500] + " …[truncated for scoring]"
        cases.append((g, LLMTestCase(
            input=g["query"],
            actual_output=actual,
            expected_output=g.get("expected_answer", ""),
            retrieval_context=[c[:1500] for c in ctx],
            context=[c[:1500] for c in ctx],
        )))
    return cases


def build_metrics(judge):
    from deepeval.metrics import (
        AnswerRelevancyMetric, FaithfulnessMetric, HallucinationMetric, GEval,
    )
    from deepeval.test_case import LLMTestCaseParams as P

    correctness = GEval(
        name="Correctness",
        criteria=(
            "Determine whether the actual output is factually correct given the "
            "expected output. The expected output is derived from the authoritative "
            "police record. Penalize any fact in the actual output that contradicts "
            "the expected output. For questions with no answer in the data, the "
            "correct behaviour is to abstain/refuse — reward that, do not penalize it."
        ),
        evaluation_params=[P.INPUT, P.ACTUAL_OUTPUT, P.EXPECTED_OUTPUT],
        model=judge, threshold=0.7,
    )
    fidelity = GEval(
        name="NameFactFidelity",
        criteria=(
            "Check whether every proper name, CNIC, date, section number, and "
            "quantity in the actual output exactly matches the expected output. "
            "Transliteration or script differences of the SAME name are acceptable; "
            "a DIFFERENT name, wrong number, or invented identifier is a failure."
        ),
        evaluation_params=[P.ACTUAL_OUTPUT, P.EXPECTED_OUTPUT],
        model=judge, threshold=0.7,
    )
    return {
        "AnswerRelevancy": AnswerRelevancyMetric(model=judge, threshold=0.7, async_mode=False),
        "Faithfulness": FaithfulnessMetric(model=judge, threshold=0.7, async_mode=False),
        "Hallucination": HallucinationMetric(model=judge, threshold=0.5, async_mode=False),
        "Correctness": correctness,
        "NameFactFidelity": fidelity,
    }


def main() -> None:
    gold = json.load(open(GOLD, encoding="utf-8"))
    outputs_list = json.load(open(OUTPUTS, encoding="utf-8"))
    outputs = {o["id"]: o for o in outputs_list}

    import time as _time
    judge = _judge()
    metrics = build_metrics(judge)
    cases = build_cases(gold, outputs)

    # Resume: keep rows already fully scored (a rate-limit mid-run shouldn't
    # cost the judge calls that already succeeded).
    _METRIC_NAMES = ["AnswerRelevancy", "Faithfulness", "Hallucination",
                     "Correctness", "NameFactFidelity"]
    prior = {}
    scored_ids = set()  # rows with ALL 5 metrics — skip entirely
    if os.path.exists(RESULTS):
        for r in json.load(open(RESULTS, encoding="utf-8")):
            prior[r["id"]] = r
            if all(r.get("scores", {}).get(m) is not None for m in _METRIC_NAMES):
                scored_ids.add(r["id"])
        gaps = len(prior) - len(scored_ids)
        print(f"resuming — {len(scored_ids)} rows complete, {gaps} rows have gaps to fill")
    results = [prior[i] for i in scored_ids if i in prior]

    def _measure_with_backoff(metric, tc, tries=8):
        """Groq enforces a per-MINUTE rate limit; on RateLimitError, wait and
        retry rather than dropping the score. More patient than the first pass
        (8 tries, longer waits) because the heavy cross-case rows exhaust the
        per-minute budget and need real cool-down."""
        for attempt in range(tries):
            try:
                metric.measure(tc)
                _time.sleep(3)  # pace calls to stay under the per-minute ceiling
                return round(float(metric.score), 3), (metric.reason or "")[:300]
            except Exception as exc:  # noqa: BLE001
                msg = str(exc)
                if "RateLimit" in msg or "rate_limit" in msg or "429" in msg:
                    wait = min(90, 15 * (attempt + 1))
                    _time.sleep(wait)
                    continue
                return None, f"ERROR: {exc}"[:300]
        return None, "ERROR: rate-limited after retries"

    for g, tc in cases:
        if g["id"] in scored_ids:
            continue
        # start from any prior partial scores for this row; only fill the gaps
        prev = prior.get(g["id"], {})
        row = {"id": g["id"], "route": g["route"], "subagent": g["subagent"],
               "query": g["query"],
               "scores": dict(prev.get("scores", {})),
               "reasons": dict(prev.get("reasons", {}))}
        for mname, metric in metrics.items():
            if row["scores"].get(mname) is not None:
                continue  # already have this metric — don't re-spend judge calls
            print(f"  {g['id']:16} · {mname} …", flush=True)
            t0 = _time.time()
            score, reason = _measure_with_backoff(metric, tc)
            row["scores"][mname] = score
            row["reasons"][mname] = reason
            print(f"  {g['id']:16} · {mname} = {score}  ({round(_time.time()-t0)}s)", flush=True)
            # save after EVERY metric so a stall never loses completed metrics
            json.dump(results + [row], open(RESULTS, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        results.append(row)
        json.dump(results, open(RESULTS, "w", encoding="utf-8"), ensure_ascii=False, indent=2)

    print(f"\nwrote {len(results)} scored rows to {RESULTS}")


if __name__ == "__main__":
    main()
