"""
Score the 32 Gold-QA answers against ground truth with DeepEval.

Scoring philosophy (per the testing team's explicit guidance):
  "Do NOT match word by word. If the LLM covers the main points in its own
   way — even with extra info — and gets the FACTS right, that's a pass.
   The problem is when it states something OPPOSITE or INCOMPLETE."

So the primary metric is a custom G-Eval 'FactualCorrectness' that rewards
semantic coverage of the key facts and penalizes contradiction/omission, NOT
lexical overlap. Answer Relevancy is computed alongside it. (Faithfulness was
dropped — see the _METRICS comment.)

Judge: Gemini `gemini-flash-lite-latest` (see `_judge()`).

Two integrity rules this script enforces (Module 45) — both exist because a
number that is quietly wrong is worse than no number at all:

  1. A judge that returns NO score is UNSCORED, never zero. `measure()` retries
     a bounded number of times with backoff; if the judge still will not
     produce a number, the row records `None` and `summarize()` EXCLUDES it
     from every mean. CR8 in the 2026-09-08 run returned a bare `null` for
     FactualCorrectness and re-scored to 1.0 — read as a zero it would have
     dragged the published all-32 mean down by 0.031 on its own.
  2. The mean is computed HERE, by `summarize()`, not by hand in a report.
     One authoritative null-safe calculation, so no report can re-derive a
     different number from the same file.

Run:
  .venv/Scripts/python.exe evaluation/gold32_score.py          # score + summary
  .venv/Scripts/python.exe evaluation/gold32_score.py --summary  # summary only,
                                                    # no judge calls, no writes
"""
from __future__ import annotations
import json, os, sys, threading, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS = os.path.join(HERE, "gold32_pipeline_outputs.json")
RESULTS = os.path.join(HERE, "gold32_results.json")

# Best-effort .env load so GEMINI_API_KEY is present when this script is run
# directly. Without it the judge is silently constructed with api_key=None and
# every question fails identically — which looks like a model outage.
try:  # pragma: no cover - convenience only
    from dotenv import load_dotenv

    load_dotenv(os.path.join(os.path.dirname(HERE), ".env"))
except Exception:  # noqa: BLE001
    pass

# How many chars of the answer the judge is shown. See `truncate_for_scoring`.
# [Module 46] Default is now 0 = NO TRUNCATION. The 900 it replaces was
# introduced to bound *Faithfulness*, which made one judge call per atomic
# claim; Faithfulness was dropped from _METRICS and the literal outlived its
# reason. Module 45 measured both sides: removing the cap costs under 2s per
# metric and NO extra judge calls, while leaving it on makes an answer whose
# gold facts sit past char 900 score 0.0 instead of 1.0 -- total signal loss,
# not degradation (proved with a positive control). The build now emits
# 1,000-2,500-char KB answers, so the cap was about to start deleting real
# results. Set GOLD32_MAX_ANSWER_CHARS to re-enable a cap if Faithfulness or
# another per-claim metric ever returns.
MAX_ANSWER_CHARS = int(os.environ.get("GOLD32_MAX_ANSWER_CHARS", "0"))
TRUNCATION_MARKER = " …[truncated for scoring]"

# A metric that produces no number is retried this many times before the row is
# recorded as UNSCORED. Rate-limit retries are counted separately (below) so a
# quota wobble cannot burn the budget reserved for genuine judge failures.
NULL_RETRIES = 3
RATE_LIMIT_RETRIES = 8
# Hard per-attempt cap. A hung metric call is a judge failure like any other:
# it yields no number, so it is retried and then recorded as unscored.
ATTEMPT_TIMEOUT_S = 120

# Provider signatures for "the call failed for a reason that is the provider's,
# not the answer's" — retried on the separate budget above rather than the null
# budget. Groq says "rate limit"; Gemini says `RESOURCE_EXHAUSTED`; both can
# also surface a bare `429` (Module 42, Module 46).
#
# [Module 54] `503 UNAVAILABLE ... 'This model is currently experiencing high
# demand.'` was in NEITHER of the earlier patterns. It is a transient CAPACITY
# failure rather than a quota failure, but it has exactly the same consequence
# (the call did not happen) and exactly the same invisibility — Module 50 hit it
# live and the quota check reported a clean run over a failed call. Kept in one
# tuple deliberately: every consumer of this list cares about "transient
# provider failure", not about which of the two it was.
_RATE_LIMIT_MARKERS = ("RateLimit", "rate_limit", "429", "RESOURCE_EXHAUSTED",
                       "ResourceExhausted", "quota", "UNAVAILABLE", "503",
                       "rate limit")

# The same four provider signatures as a grep -E alternation, kept HERE so the
# docs that prescribe the log check and the code that classifies a judge error
# cannot drift apart. Every file that tells a reader how to certify a live run
# must prescribe exactly:
#
#     grep -ciE "rate limit|RESOURCE_EXHAUSTED|429|quota|UNAVAILABLE|503" backend.log
#
# A bare `grep -c "rate limit"` reports 0 over a Gemini 429 and over a 503, i.e.
# it certifies a clean run on top of a failed call. tests/
# test_provider_failure_visibility.py asserts the prescriptive docs match this
# constant and that no bare form survives.
# [Module 81] The bare numeric codes previously matched a TIMESTAMP: a log
# line stamped `16:19:29,503` scored as a 503 UNAVAILABLE. Every module that
# certified a clean run with this pattern was therefore reading its own
# milliseconds as a provider failure. The codes now require a non-numeric,
# non-timestamp boundary on each side, so `,503` and `:429` no longer count
# while a real `503 UNAVAILABLE` or `HTTP 429` still does.
LOG_GREP_PATTERN = (
    "rate limit|RESOURCE_EXHAUSTED|quota|UNAVAILABLE"
    "|(^|[^0-9.,:])(429|503)([^0-9]|$)"
)

# "Pass" in the project's reports means FactualCorrectness >= 0.5 — deliberately
# more lenient than the metric's own 0.6 threshold. Kept here so the pass rate
# and the mean come from the same place.
PASS_METRIC = "FactualCorrectness"
PASS_THRESHOLD = 0.5

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
    # answers low. Gemini flash-lite follows the nuanced instruction better,
    # but even Gemini needed the FactualCorrectness metric's evaluation_steps
    # (below) spelled out explicitly with a worked example before it actually
    # honored the close-numbers rule in practice — see Module 20.
    # A raised per-attempt timeout accommodates Gemini's slower GEval calls.
    os.environ.setdefault("DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE", "180")
    from deepeval.models import GeminiModel
    key = os.environ.get("GEMINI_JUDGE_KEY") or os.environ.get("GEMINI_API_KEY")
    return GeminiModel(model="gemini-flash-lite-latest", api_key=key)


def build_metrics(judge):
    from deepeval.metrics import AnswerRelevancyMetric, FaithfulnessMetric, GEval
    from deepeval.test_case import LLMTestCaseParams as P
    # `criteria` alone (free text) lets GEval silently regenerate its own
    # evaluation steps and drop the "close numbers are OK" rule — confirmed by
    # actually re-running this metric against A1's captured baseline answer
    # (92 total/65 males vs the expected 94 total/67 males, 24 females exact)
    # with the criteria-only version below: it still scored 0.30 and its own
    # reason cited the numeric difference as "a material error" — exactly the
    # harshness this module exists to fix. `evaluation_steps` are followed
    # literally instead of being reinterpreted, so the close-numbers rule is
    # spelled out as an explicit step with a worked example lifted from that
    # same real case.
    factual = GEval(
        name="FactualCorrectness",
        evaluation_steps=[
            "Read the QUESTION, the EXPECTED OUTPUT (verified ground truth), "
            "and the ACTUAL OUTPUT.",
            "List the key facts the EXPECTED OUTPUT asserts (the specific "
            "things the question asked for — counts, names, statuses, "
            "relationships, conclusions).",
            "For each key fact, check whether the ACTUAL OUTPUT states an "
            "equivalent fact, in its own words. Different phrasing, "
            "different order, or extra correct information beyond what was "
            "asked is NOT an error — do not penalize for any of that.",
            "For any number in the EXPECTED OUTPUT, compare it to the "
            "corresponding number in the ACTUAL OUTPUT by size, not by exact "
            "digit match. Treat two numbers as MATCHING (not an error) when "
            "they are close enough to plausibly be the same underlying fact "
            "measured with a slightly different count/de-duplication method "
            "— roughly within 5-10% of each other, or off by only a couple "
            "of units on a small total. Worked example, a real case this "
            "rule exists for: EXPECTED says 67 males, 24 females, 3 unclear, "
            "94 total; ACTUAL says 65 males, 24 females, 92 total. Here 24 "
            "is an exact match, and 65 vs 67 / 92 vs 94 are each off by only "
            "2 (about 2-3%) — under this rule that whole answer MATCHES the "
            "expected output and should score HIGH, not be marked as having "
            "'incorrect numbers'.",
            "Only score low when the ACTUAL OUTPUT does one of: (a) states "
            "something that CONTRADICTS the expected output in kind, not "
            "degree (e.g. reverses which group is larger, names the wrong "
            "entity, gives a number that is wildly off — an order of "
            "magnitude or a large fraction of the total, not a close "
            "count); (b) is materially INCOMPLETE, omitting a key fact the "
            "question specifically asked for; (c) refuses or abstains "
            "('the data does not specify', 'insufficient information') when "
            "the expected output shows real content was available.",
            "An ACTUAL OUTPUT that correctly states the data does NOT "
            "contain something, and the EXPECTED OUTPUT agrees with that, "
            "is a PASS (high score) — this is not an abstention, it is the "
            "correct answer.",
            "Score high (pass) whenever the actual output covers the "
            "expected output's key facts under the rules above, even with "
            "close-but-not-identical numbers or extra correct detail. Score "
            "low only for genuine contradiction, material omission, wildly "
            "wrong numbers, or an unwarranted refusal.",
        ],
        evaluation_params=[P.INPUT, P.ACTUAL_OUTPUT, P.EXPECTED_OUTPUT],
        model=judge, threshold=0.6,
    )
    # Faithfulness intentionally omitted — see the _METRICS comment. Kept only
    # the two metrics that matter and that don't hang on long answers.
    return {
        "FactualCorrectness": factual,
        "AnswerRelevancy": AnswerRelevancyMetric(model=judge, threshold=0.6, async_mode=False),
    }


# ── scoring primitives ────────────────────────────────────────────────────

def truncate_for_scoring(actual, max_chars=None):
    """Cap the answer text the judge is shown.

    History, because the number looks arbitrary and was: the original cap was
    900 chars and its stated reason was **Faithfulness**, which splits an
    answer into atomic claims and makes one judge call PER claim — a 5,000-char
    analytical answer exploded into dozens of serial calls and hung the run.
    Faithfulness was then dropped from `_METRICS`, and the 900 survived as an
    unexamined literal while KB answers grew to 1,000–2,500 chars.

    Module 45 measured what the cap costs — see
    `docs/gold-qa-wave2-results/MODULE45_RESULT.md` for the before/after table.
    It is a named, env-overridable constant now (`GOLD32_MAX_ANSWER_CHARS`;
    `0` disables capping entirely) instead of a literal buried in a loop, so
    every report can state the value its numbers were produced under.
    """
    if max_chars is None:
        max_chars = MAX_ANSWER_CHARS
    if max_chars and len(actual) > max_chars:
        return actual[:max_chars] + TRUNCATION_MARKER
    return actual


def no_answer_captured(output_row):
    """True when the runner never got an answer back for this question.

    [Module 42] `gold32_run.py` sets ``transport_ok: False`` when the
    ``/api/chat`` request itself failed — a client-side read timeout or a
    dropped connection — which leaves ``actual_answer`` empty and ``route``
    None because the SSE stream was never read at all.

    That is the ABSENCE of a measurement, not a bad answer, and it is exactly
    what produced KB6's row in the 2026-09-08 report: FactualCorrectness 0.0
    AND AnswerRelevancy 0.0 with ``route=None``, written up there as a
    *"genuine error, did not recover"*. Live on this branch KB6 returns
    ``route='RAG'`` on 5 of 5 runs and takes 513-628s — it was simply still
    running when the old hard-coded 300s ceiling gave up.

    Deliberately ``is False``, not falsy: an outputs file written before this
    key existed has no ``transport_ok`` at all, and those rows were genuinely
    measured. They must keep being scored, so a missing key means "fine".

    A row that DID come back is always scored, including a deliberate
    abstention — "No sufficiently relevant documents were found" is a real
    answer, and a real failure, which must go on counting as one.
    """
    return output_row.get("transport_ok") is False


def no_answer_reason(output_row):
    """The `reasons` text stored against an unscored transport failure."""
    return ("NO ANSWER CAPTURED — the /api/chat request itself failed (%s). "
            "Not a zero: the pipeline never returned, so there is nothing to "
            "judge. See GOLD32_TIMEOUT_S in gold32_run.py."
            % (output_row.get("error") or "no error recorded"))


def _measure_once(metric, tc, box):
    try:
        metric.measure(tc)
        raw = getattr(metric, "score", None)
        if raw is None:
            # THE Module 45 defect at its source: DeepEval leaves `score` as
            # None when the judge's reply cannot be parsed into a number. That
            # is a judge failure, not a score of zero, so it is raised here and
            # retried like any other error rather than silently becoming 0.0.
            box["exc"] = RuntimeError("judge returned no score (null)")
            return
        box["score"] = round(float(raw), 3)
        box["reason"] = (metric.reason or "")[:300]
    except Exception as e:  # noqa: BLE001
        box["exc"] = e


def _is_rate_limit(exc):
    # [Module 54] Case-insensitive. Groq's own message is "due to rate limits"
    # (a space, lower case) which the CamelCase/underscore markers above did
    # not match at all -- the code classifier and the prescribed log grep
    # disagreed about the most common signature of the two.
    if exc is None:
        return False
    err = str(exc).lower()
    return any(x.lower() in err for x in _RATE_LIMIT_MARKERS)


def measure(metric, tc, tries=NULL_RETRIES, rate_limit_tries=RATE_LIMIT_RETRIES,
            attempt_timeout=ATTEMPT_TIMEOUT_S, sleeper=time.sleep):
    """Score one metric on one test case. Returns ``(score, reason)``.

    ``score`` is ``None`` only when EVERY attempt failed to produce a number.
    A ``None`` here means **UNSCORED** and must never be read as 0.0 —
    :func:`summarize` excludes it from the mean and reports it loudly.

    Three failure modes, deliberately handled differently:

    * **rate limit** — retried up to ``rate_limit_tries`` times with linear
      backoff, and does NOT consume a null-retry: a quota wobble says nothing
      about the question being scored.
    * **timeout** — the metric call exceeded ``attempt_timeout``. Retried.
    * **null / error** — the judge answered but produced no parseable number
      (the CR8 case). Retried.

    Before Module 45 the last two returned after a single attempt, so one flaky
    judge call was indistinguishable from a genuine failure downstream.
    """
    attempts, rl, a = [], 0, 0
    while a < tries:
        box = {}
        t = threading.Thread(target=_measure_once, args=(metric, tc, box), daemon=True)
        t.start()
        t.join(timeout=attempt_timeout)
        if t.is_alive():
            attempts.append("TIMEOUT (>%ss)" % attempt_timeout)
        elif "score" in box:
            sleeper(2)
            return box["score"], box["reason"]
        else:
            e = box.get("exc")
            if _is_rate_limit(e):
                rl += 1
                if rl > rate_limit_tries:
                    attempts.append("RATE-LIMITED (retries exhausted)")
                    break
                sleeper(min(90, 15 * rl))
                continue  # a quota wobble does not consume a null-retry
            attempts.append(("%s: %s" % (type(e).__name__, e))[:160])
        a += 1
        if a < tries:
            sleeper(min(30, 5 * (2 ** (a - 1))))
    return None, ("UNSCORED after %d attempt(s) — NOT a zero: %s"
                  % (len(attempts), " | ".join(attempts)))[:400]


# ── the one authoritative, null-safe aggregation ─────────────────────────

def summarize(rows, metrics=None):
    """Aggregate scored rows. Unscored (``None``) rows are EXCLUDED, never
    zeroed.

    Every published mean should come from here. A mean computed by hand over a
    results file that contains a ``null`` silently understates the system by an
    unknown amount, and nothing in the file says so.
    """
    metrics = metrics or _METRICS
    out = {"n_rows": len(rows), "metrics": {}, "unscored": [], "by_type": {}}
    for m in metrics:
        vals, missing = [], []
        for r in rows:
            v = (r.get("scores") or {}).get(m)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                vals.append(float(v))
            else:
                missing.append(r.get("id"))
        out["metrics"][m] = {
            "mean": round(sum(vals) / len(vals), 3) if vals else None,
            "scored": len(vals),
            "unscored": len(missing),
            "unscored_ids": missing,
        }
        for qid in missing:
            reason = next((((r.get("reasons") or {}).get(m) or "")
                           for r in rows if r.get("id") == qid), "")
            out["unscored"].append({"id": qid, "metric": m, "reason": reason})

    scored_rows = [r for r in rows
                   if isinstance((r.get("scores") or {}).get(PASS_METRIC), (int, float))
                   and not isinstance(r["scores"][PASS_METRIC], bool)]
    passed = [r for r in scored_rows if r["scores"][PASS_METRIC] >= PASS_THRESHOLD]
    out["pass"] = {"metric": PASS_METRIC, "threshold": PASS_THRESHOLD,
                   "passed": len(passed), "of_scored": len(scored_rows),
                   "rate": round(len(passed) / len(scored_rows), 3) if scored_rows else None}

    for r in rows:
        b = out["by_type"].setdefault(r.get("type") or "(unknown)",
                                     {"scored": 0, "unscored": 0, "sum": 0.0, "passed": 0})
        v = (r.get("scores") or {}).get(PASS_METRIC)
        if isinstance(v, (int, float)) and not isinstance(v, bool):
            b["scored"] += 1
            b["sum"] += float(v)
            b["passed"] += int(v >= PASS_THRESHOLD)
        else:
            b["unscored"] += 1
    for b in out["by_type"].values():
        b["mean"] = round(b["sum"] / b["scored"], 3) if b["scored"] else None
        b.pop("sum")
    return out


def format_summary(sm):
    """Render :func:`summarize` as text. Unscored questions print FIRST and in
    full — they are the thing a reader must not miss."""
    L = ["", "=" * 66]
    if sm["unscored"]:
        L.append("!!  %d UNSCORED METRIC RESULT(S) — the judge returned no number."
                 % len(sm["unscored"]))
        L.append("!!  They are EXCLUDED from every mean below. An unscored question")
        L.append("!!  is NOT a zero — re-run it before publishing any figure.")
        for u in sm["unscored"]:
            L.append("!!    %-6s %-20s %s" % (u["id"], u["metric"], u["reason"][:110]))
    else:
        L.append("all metric results scored — no nulls, nothing excluded")
    L.append("=" * 66)
    for m, d in sm["metrics"].items():
        excl = ("  (excluded %d: %s)" % (d["unscored"], ", ".join(map(str, d["unscored_ids"]))
                                         )) if d["unscored"] else ""
        L.append("%-20s mean = %-6s over %d/%d scored%s"
                 % (m, d["mean"], d["scored"], sm["n_rows"], excl))
    p = sm["pass"]
    L.append("%-20s        %s/%s scored  (%s >= %s)"
             % ("pass rate", p["passed"], p["of_scored"], p["metric"], p["threshold"]))
    L.append("-" * 66)
    for t, b in sorted(sm["by_type"].items()):
        L.append("  %-30s mean=%-6s pass=%d/%d%s"
                 % (t[:30], b["mean"], b["passed"], b["scored"],
                    ("  UNSCORED=%d" % b["unscored"]) if b["unscored"] else ""))
    L.append("=" * 66)
    return "\n".join(L)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if "--summary" in argv:
        # Recompute the published numbers from an existing results file without
        # spending a single judge call, and without writing anything.
        print(format_summary(summarize(json.load(open(RESULTS, encoding="utf-8")))))
        return

    outs = json.load(open(OUTPUTS, encoding="utf-8"))
    from deepeval.test_case import LLMTestCase
    judge = _judge()
    metrics = build_metrics(judge)

    results, done = [], set()
    if os.path.exists(RESULTS):
        for r in json.load(open(RESULTS, encoding="utf-8")):
            # A row holding a None score is deliberately NOT "done": resuming
            # re-runs it, which is exactly the CR8 remedy.
            if all(r["scores"].get(m) is not None for m in _METRICS):
                results.append(r); done.add(r["id"])
        if done: print("resuming — %d scored" % len(done))

    for o in outs:
        if o["id"] in done:
            continue
        row = {"id": o["id"], "type": o["type"], "language": o["language"],
               "route": o.get("route"), "scores": {}, "reasons": {}}

        # [Module 42] A row the runner never got an answer for is the ABSENCE
        # of a measurement, and must not be judged.
        #
        # gold32_run.py records `transport_ok: False` when the /api/chat call
        # itself failed — a client timeout or a dropped connection — leaving
        # `actual_answer` empty and `route` None because the SSE stream was
        # never read. Judging that empty string (as `or "(no answer produced)"`
        # did) asks the judge to score a string the pipeline never produced,
        # and it duly returns 0.0 on every metric. That is what put KB6 in the
        # 2026-09-08 report as FactualCorrectness 0.0 AND AnswerRelevancy 0.0
        # with route=None, described as a "genuine error, did not recover",
        # when live it returns route='RAG' on 5 runs of 5 and simply takes
        # longer than the runner's old 300s ceiling on the 4 of those 5 that
        # abstain (428.8-628.2s; the run that answers takes 247.1s).
        #
        # Same principle as Module 45's null handling, one layer earlier: an
        # unscored row is EXCLUDED from every mean by summarize(), so a
        # transport failure now visibly costs coverage instead of silently
        # costing score. Older outputs files have no `transport_ok` key at all;
        # those are treated as fine (`is False`, not falsy) so this cannot
        # retroactively unscore anything that was genuinely measured.
        if no_answer_captured(o):
            for mn in metrics:
                row["scores"][mn] = None
                row["reasons"][mn] = no_answer_reason(o)
                print("  %-5s %-18s = None   <-- UNSCORED (no answer captured)"
                      % (o["id"], mn), flush=True)
            results.append(row)
            json.dump(results, open(RESULTS, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            continue

        actual = truncate_for_scoring(o.get("actual_answer") or "(no answer produced)")
        ctx = [o.get("expected_answer", "")]
        tc = LLMTestCase(input=o["question"], actual_output=actual,
                         expected_output=o.get("expected_answer", ""),
                         retrieval_context=ctx, context=ctx)
        for mn, m in metrics.items():
            sc, rs = measure(m, tc)
            row["scores"][mn] = sc; row["reasons"][mn] = rs
            flag = "" if sc is not None else "   <-- UNSCORED (not a zero)"
            print("  %-5s %-18s = %s%s" % (o["id"], mn, sc, flag), flush=True)
            json.dump(results + [row], open(RESULTS, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
        results.append(row)
        json.dump(results, open(RESULTS, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
    print("\nwrote %d to %s" % (len(results), RESULTS))
    print("answer cap in force for this run: %s chars" % (MAX_ANSWER_CHARS or "none"))
    print(format_summary(summarize(results)))


if __name__ == "__main__":
    main()
