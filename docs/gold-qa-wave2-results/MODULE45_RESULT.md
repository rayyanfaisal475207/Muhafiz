# Module 45 — a judge `null` must never be read as a zero

**Branch:** `fix/eval-harness-null-scores-and-truncation`
**Files:** `evaluation/gold32_score.py`, `tests/test_gold32_score.py`,
`evaluation/gold32_truncation_experiment.json` (new artefact)
**Judge used for every number below:** `gemini-flash-lite-latest`, the same
`_judge()` and the same `evaluation_steps` the reference run used.
**`src/` untouched** — this module is evaluation-harness-only.

This module gates the trustworthiness of every number the project reports,
including Module 27's forthcoming final rerun. It carries two investigations:
the `null`-as-zero defect it was filed for (**confirmed and fixed**), and the
900-character scoring cap (**hypothesis tested; a negative result on current
data, a proven mechanism, filed as Module 46**).

---

## 1. Root cause

### Defect 1 — a judge `null` is indistinguishable from a genuine 0.0

`EVALUATION_REPORT_POST_FIXES.md` §5 records that CR8 returned **no
FactualCorrectness at all** — a bare `null`, not a number — and that re-scoring
that one question returned **1.0/1.0**.

The brief's framing was that `measure()` "already returns `None` on timeout or
error", so the danger was purely downstream. **That is half right, and the half
it gets wrong is the more serious half.** Reading the pre-change code:

```python
def measure(metric, tc, tries=8):
    for a in range(tries):
        ...
        t.start(); t.join(timeout=120)
        if t.is_alive():
            return None, "TIMEOUT (...)"          # <-- returns on attempt 1
        if "score" in box:
            time.sleep(2); return box["score"], box["reason"]
        e = box.get("exc")
        if e and any(x in str(e) for x in ("RateLimit", "rate_limit", "429")):
            time.sleep(min(90, 15 * (a + 1))); continue
        return None, f"ERROR: {e}"[:200]          # <-- returns on attempt 1
    return None, "rate-limited"
```

The `tries=8` loop **only ever iterated for rate limits.** A timeout returned
`None` after a single attempt; any other exception returned `None` after a
single attempt. So the exact CR8 failure — a judge call that produces no
parseable number — was never retried at all, even though the report shows a
single re-run recovers it to 1.0.

Worse, the `null` case did not even reach that error path cleanly. DeepEval
leaves `metric.score` as `None` when the judge's reply cannot be parsed, and
the old `_measure_once` did:

```python
box["score"] = round(float(metric.score), 3)
```

`float(None)` raises `TypeError`, which fell into the generic `except` and
returned `None` after one attempt with the reason string `ERROR: float() ...`.
The reason recorded in the results file therefore said nothing about the
question being **unscored** rather than **wrong**.

Downstream, the brief's fear is confirmed: **there was no mean computation in
the repository at all.** `gold32_score.py` ended at `print(f"wrote {len(results)}
to {RESULTS}")`. Every published figure — the 0.572, the 19/32, the per-bucket
table in `EVALUATION_REPORT_POST_FIXES.md` §2, the 0.39/0.65/13-of-32 in
`evaluation/MODULE_9_RERUN_REPORT.md` — was computed by hand or ad hoc, once
per report, with nothing to stop a `null` being counted as 0.0 and nothing in
the output to say one was present. That is the real defect: not a wrong line of
code, but an **absent** one.

Sizing it: a single `null` read as zero among 32 questions understates the
all-32 mean by `score / 32`. For CR8's recovered 1.0 that is **0.031** — about
a fifth of the entire wave's reported improvement (0.425 → 0.572).

### Defect 2 — the 900-char cap: what the code says vs. what it does

`gold32_score.py` capped every answer at 900 characters before scoring, and its
comment justified the cap by **Faithfulness** — a metric that decomposes an
answer into atomic claims and makes one judge call per claim. Faithfulness was
subsequently dropped from `_METRICS`, which is now only
`["FactualCorrectness", "AnswerRelevancy"]`. **The cap's stated rationale no
longer exists.** Both surviving metrics make an O(1) number of judge calls
regardless of answer length, so nothing about them explodes with length.

The hypothesis to test was: if a gold fact appears after character 900, the
judge cannot see it and must score low — meaning some 0.0 scores are scoring
artefacts, not model failures. §5 below reports what measuring it actually
showed.

### A third finding, unasked for, that changes how §5 must be read

**The `evaluation/gold32_results.json` and
`evaluation/gold32_pipeline_outputs.json` in this repository are NOT the
2026-09-08 run the report describes.** Both files were last written by commit
`d313a60`, *"docs(eval): Module 9 — first full Gold-32 rerun report + captured
artifacts"* (2026-09-06), and have not been touched since:

```
$ git log --oneline -- evaluation/gold32_results.json
d313a60 docs(eval): Module 9 — first full Gold-32 rerun report + captured artifacts
```

Running this module's new `--summary` mode over the committed file reproduces
**`evaluation/MODULE_9_RERUN_REPORT.md`** exactly — 0.394 / 0.653 / 13 passing,
Fact Retrieval 0.983, Contextual Summarization 0.02, KB 0.000 — and not the
report's 0.572 / 0.886 / 19 passing. The same files are byte-identical in the
`muhafiz-m35`, `muhafiz-m40` and `muhafiz-m41` worktrees and in the main
checkout, so no sibling track is holding a newer copy.

That is a useful accident: reproducing a published report's headline numbers
from its source file is the strongest available evidence that the new
`summarize()` is correct. But it means the truncation experiment could only be
run against the **2026-09-06 Module 9 answers**, whose lengths are 52–1,582
chars — not the 1,000–2,500-char answers §3 of the report describes. This is
stated wherever it bears on a conclusion, and filed as **Module 47**.

---

## 2. Change

| File | Change |
|---|---|
| `evaluation/gold32_score.py` | `measure()`, `_measure_once()`, `truncate_for_scoring()`, `summarize()`, `format_summary()` lifted to module level (they were nested inside `main()` and untestable). Null/timeout/error now retried; rate-limit retries budgeted separately; `None` recorded as **unscored** with a reason that says so. New null-safe `summarize()` + loud `format_summary()`. New `--summary` mode. Best-effort `.env` load. The 900 literal became the named, env-overridable `MAX_ANSWER_CHARS`. |
| `tests/test_gold32_score.py` | New. 22 tests, no network. |
| `evaluation/gold32_truncation_experiment.json` | New captured artefact — every re-score in §5, with the judge, the source run and the variance probe. |

The fix sits in `gold32_score.py` and nowhere else because that is the only
place that knows the difference between "the judge said 0.0" and "the judge
said nothing". `gold32_run.py` was read and **not** changed: it captures
answers, never scores, and no finding landed there.

**Behaviour deliberately left alone: `MAX_ANSWER_CHARS` still defaults to 900.**
Changing it would change every number this harness produces, which is not a
change to make inside a scoring-integrity PR and not one the current data
justifies (§5). It is now a named constant with an accurate comment and an env
override, so Module 46 is a one-line decision made against real evidence.

### What the retry now does

Three failure modes, handled differently on purpose:

* **rate limit** — up to 8 retries with linear backoff, and it **does not
  consume a null-retry**: a quota wobble says nothing about the question. (The
  project has been burned by this once already — the 1,410-rate-limit pass that
  invalidated an entire evaluation.)
* **timeout** — the metric exceeded 120 s. Retried.
* **null / error** — the judge answered but produced no parseable number, the
  CR8 case. Retried, and `metric.score is None` is now detected explicitly
  rather than arriving as a `TypeError` from `float(None)`.

After `NULL_RETRIES` (3) genuine failures the row records `None` with the reason
`UNSCORED after N attempt(s) — NOT a zero: ...`, and `summarize()` excludes it.

### The summary output

```
==================================================================
!!  1 UNSCORED METRIC RESULT(S) — the judge returned no number.
!!  They are EXCLUDED from every mean below. An unscored question
!!  is NOT a zero — re-run it before publishing any figure.
!!    CR8    FactualCorrectness   UNSCORED after 3 attempt(s) — NOT a zero: ...
==================================================================
FactualCorrectness   mean = 0.394  over 32/32 scored
AnswerRelevancy      mean = 0.653  over 32/32 scored
pass rate                   13/32 scored  (FactualCorrectness >= 0.5)
```

---

## 3. Unit tests

```
$ PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/test_gold32_score.py \
      tests/test_eval_scripts.py -q
32 passed
```

(`tests/test_gold32_score.py` alone: **22 passed**. The full suite was **not**
run — per project memory it empties the `muhafiz_entity_descriptions` Chroma
collection.)

The regression test pinned to the literal defect —
`test_cr8_regression_a_single_null_does_not_drag_the_mean_down` — builds the
exact shape the report describes (31 scored rows plus CR8 `null`) and asserts
the mean is **1.0**, explicitly asserting it is **not** the 0.969 that zeroing
CR8 would produce. That 0.031 is the understatement §5 of the report warns
about, written into an assertion.

Also pinned:

* `test_persistent_null_returns_none_not_zero` — the core defect.
* `test_a_null_that_recovers_on_retry_is_scored` — CR8 exactly: `None` on the
  first attempt, 1.0 on the second. This test **fails against the old code**,
  which returned after one attempt.
* `test_rate_limits_do_not_consume_the_null_retry_budget` — three 429s then a
  score, with only one null-retry allowed.
* `test_a_timeout_yields_none_rather_than_hanging_the_run`.
* `test_pass_rate_is_over_scored_rows_only` — a null is not a failure.
* `TestRealResultsFile` — runs against the real committed results file and
  asserts the **relation** (published mean == mean of non-null scores, and
  every null in the file is reported as unscored) rather than a frozen number,
  so Module 27 overwriting that file cannot break CI.

The judge is a stub throughout and `measure()`'s backoff sleeps are injected,
so the file needs no network and runs in under a second.

---

## 4. Live verification

There is no `route=` event to capture here: this module changes the scoring
harness, not the pipeline, and never sends a question to `/api/chat`. No
backend and no Docker were started (free RAM was tight and three other tracks
were running). The equivalent live check is **real judge calls against the real
captured answers**, which is what §5 is.

What was verified live, with the real Gemini judge:

| Check | Result |
|---|---|
| Judge reachable, key from `.env` | yes — 33 judge calls completed (20 for the §5c re-scores, 13 for the §5d variance probe) |
| Rate-limit errors during this module's runs | **0** — no `429`/quota error was raised at any point |
| `--summary` over the committed results file | reproduces `MODULE_9_RERUN_REPORT.md`'s published 0.39 / 0.65 / 13-of-32 |

That last row is the strongest single check available: an independent
re-derivation of an already-published headline from its own source file.

---

## 5. Gold comparison — the truncation experiment

### 5a. Where the cap can even apply

Of all 32 captured answers, exactly **two exceed 900 characters**: M4 (1,582)
and M5 (1,318). For the other 30 the capped and uncapped strings are
**byte-identical**, so their scores are unchanged by construction, not by
measurement.

Every question currently at FactualCorrectness ≤ 0.1, with its answer length:

| Q | FC | AR | answer chars | truncated? | chars the judge never saw |
|---|---|---|---|---|---|
| CR3 | 0.0 | 1.0 | 526 | no | 0 |
| CR4 | 0.0 | 0.0 | 501 | no | 0 |
| CS4 | 0.0 | 0.2 | 628 | no | 0 |
| M1 | 0.0 | 1.0 | 258 | no | 0 |
| M2 | 0.0 | 0.0 | 76 | no | 0 |
| **M4** | **0.1** | 1.0 | **1,582** | **YES** | **682** |
| **M5** | **0.0** | 1.0 | **1,318** | **YES** | **418** |
| M7 | 0.0 | 0.667 | 606 | no | 0 |
| G1 | 0.0 | 1.0 | 512 | no | 0 |
| G6 | 0.0 | 0.0 | 538 | no | 0 |
| KB1 | 0.0 | 1.0 | 779 | no | 0 |
| KB2 | 0.0 | 0.0 | 52 | no | 0 |
| KB3 | 0.0 | 1.0 | 210 | no | 0 |
| KB4 | 0.0 | 0.0 | 52 | no | 0 |
| KB5 | 0.0 | 0.0 | 210 | no | 0 |
| KB6 | 0.0 | 0.0 | 210 | no | 0 |
| KB8 | 0.0 | 0.0 | 210 | no | 0 |
| KB9 | 0.0 | 0.0 | 210 | no | 0 |

**16 of the 18 low scorers are not truncated at all.** Whatever is wrong with
them, it is not this.

### 5b. What is in the 418 / 682 characters the judge never saw

Read directly. For **M5**, the lost tail is entirely verifier footnotes:

> *"The natural-language summary could not be verified as an accurate
> paraphrase…"*, *"A cited claim ([Document 1]) could only be partially
> confirmed…"*

For **M4**, the lost tail is a concluding paragraph plus two more verifier
footnotes. It contains **no gold fact**: M4's gold asserts *33 criminal records,
1 conviction, 30 still pending*, and none of those numbers appears anywhere in
M4's answer, before or after character 900.

So on this data the cap discards apparatus, not evidence.

### 5c. Before / after — same judge, same prompt

`evaluation/gold32_truncation_experiment.json`. `cap=900` is current behaviour;
`cap=0` disables capping.

| Q | variant | chars judged | FC @ cap 900 | FC @ no cap | AR @ cap 900 | AR @ no cap | verdict |
|---|---|---|---|---|---|---|---|
| **M5** | natural | 925 → 1,318 | **0.0** | **0.0** | 1.0 | 1.0 | no change |
| **M4** | natural | 925 → 1,582 | **0.1** | **0.1** | 1.0 | 0.909 | no change |
| CR8 | natural | 467 (uncapped) | 1.0 | — | 1.0 | — | regression: matches file |
| D1 | natural | 207 (uncapped) | 0.7 | — | 1.0 | — | see variance note |
| **CR8** | **padded** | 925 → 1,519 | **0.0** | **1.0** | **0.0** | 0.875 | **mechanism confirmed** |
| **G3** | **padded** | 925 → 1,749 | **0.0** | **0.9** | **0.0** | 0.833 | **mechanism confirmed** |

The *padded* rows are a positive control: 1,020 characters of filler prepended
to a question that scores 1.0 / 0.9, pushing every gold fact past character 900
while changing nothing else. Under the current cap **both collapse to exactly
0.0**; uncapped both recover.

### 5d. Judge variance — what bounds these claims

The judge is not deterministic, so "no change" needs an error bar. Same input,
`cap=900`, FactualCorrectness, repeated:

| Q | draws | spread |
|---|---|---|
| D1 | 0.7, 1.0, 1.0, 0.9, 1.0 | **0.3** |
| M5 | 0.0, 0.0, 0.0 | 0.0 |
| M4 | 0.1, 0.1, 0.1 | 0.0 |
| CR8 | 1.0, 1.0, 1.0 | 0.0 |

M4 and M5 — the only two rows the cap can affect — are stable to three decimal
places across three draws, which is what licenses the "no change" verdict for
them. D1 moved 0.3 on identical input, so **a single re-score of a single
question is not evidence of anything**; that is worth knowing before Module 27
attributes a 0.1 movement to a code change.

### 5e. Verdict on the cap

**On the data available, the truncation hypothesis is a negative result, and I
am reporting it as such: not one currently-0.0 score is a truncation artefact.**
M5 scores 0.0 with the full answer in front of the judge. M4 scores 0.1 either
way. The other 16 low scorers were never truncated. **Every 0.0 in this results
file is a model failure, not a scoring artefact** — Modules 41–44 keep their
priority exactly as filed, and nothing in this module changes how they should be
ranked.

**But the mechanism is real, and it is not subtle.** The padded control shows a
1.0 answer scoring 0.0 purely because its facts sit past character 900 — a
total loss of signal, not a degradation. Three things make that a live risk
rather than a curiosity:

1. The cap's stated justification (Faithfulness) **no longer exists**.
2. `EVALUATION_REPORT_POST_FIXES.md` §3 states KB answers are now **1,000–2,500
   characters** — that is, the current build routinely produces answers the cap
   truncates, where the 2026-09-06 run produced only two.
3. Both surviving metrics make an O(1) number of judge calls, so raising the cap
   costs input tokens, not calls. Measured here: M4 at 1,582 chars took the same
   5 s for GEval as at 925, and AnswerRelevancy 11 s either way; padded CR8 at
   1,519 chars took 5 s / 12 s. **The latency cost of removing the cap on these
   answers was under 2 s per metric and never a new judge call.**

So the honest verdict is: **the cap is a latent defect that does not affect any
number in the committed results file, and would very likely corrupt Module 27's
rerun.** Per this project's standing practice it is filed as **Module 46** with
the measurement above rather than folded into this PR, and it should be settled
**before** Module 27 runs.

---

## 6. Non-gold paraphrase

The paraphrase check for a harness module is a case the harness has never seen,
constructed so the answer is known independently of any gold question. Two were
used:

* **The padded control (§5c).** CR8 and G3 with 1,020 chars of filler prepended
  are inputs no gold run has ever produced. Their correct FactualCorrectness is
  known — the underlying answers score 1.0 and 0.9 — so the 0.0 they receive
  under the cap is unambiguously the harness's error and not the model's. This
  is what separates "the cap looks risky" from "the cap destroys a known-good
  answer".
* **The synthetic null (§3).** `_StubMetric` returns `None` from a judge that
  never touches the network — a failure no gold question has to reproduce for
  the retry-and-exclude path to be proven.

The brief was explicit that the truncation claim was a hypothesis to test, not
a fact to assume. It was tested, and on real data it did not hold; it holds only
under a control that does not currently occur. Both halves are reported.

---

## 7. Regression guard

The risk of this change is that refactoring `measure()` silently alters scores.
Guarded two ways.

**Re-scored with the new code, compared to the committed reference run:**

| Q | committed FC | re-scored FC | committed AR | re-scored AR | |
|---|---|---|---|---|---|
| CR8 | 1.0 | **1.0** | 1.0 | **1.0** | unchanged |
| M5 | 0.0 | **0.0** | 1.0 | **1.0** | unchanged |
| M4 | 0.1 | **0.1** | 1.0 | **1.0** | unchanged |
| D1 | 1.0 | 0.7, then 1.0, 1.0, 0.9, 1.0 | 1.0 | **1.0** | judge variance (§5d), not the change |

CR8 was chosen deliberately: it is the question the module exists for, and it
re-scores to 1.0 exactly as `EVALUATION_REPORT_POST_FIXES.md` §5 says it should.

**Whole-file guard:** `--summary` over the untouched committed results file
reproduces `MODULE_9_RERUN_REPORT.md`'s published numbers (0.394 vs its stated
0.39; 0.653 vs 0.65; 13 of 32; Fact Retrieval 0.983 vs 0.98; Complex Reasoning
0.588 vs 0.59; Contextual Summarization 0.02; Creative Generation 0.38; KB
0.00). The new aggregation agrees with the hand computation it replaces, on
every bucket.

**`evaluation/gold32_results.json` was not modified.** Every re-score in this
module went to `evaluation/gold32_truncation_experiment.json`. Confirmed by
`git status`: the results file is not in this branch's diff.

**Other tracks:** nothing under `src/` was read into the change or written.
`gold32_run.py` is unmodified.

---

## 8. New defects found

Both are filed as their own modules rather than folded into this one.

### Module 46 — the 900-char scoring cap will corrupt Module 27's rerun

Proven by the padded control in §5c: an answer whose gold facts fall past
character 900 scores **0.0** instead of 1.0/0.9. Does not currently fire (§5e),
because the committed run's answers are short — but the report states the
current build produces 1,000–2,500-char KB answers, and the cap's Faithfulness
justification is gone. Measured cost of removing it: **under 2 s per metric, no
additional judge calls.** Recommended: `GOLD32_MAX_ANSWER_CHARS = 3000`, decided
against the real 2026-09-08 answers once Module 47 makes them available.
**Should be settled before Module 27 runs.** The constant and its env override
already exist, so the change itself is one line plus a rerun.

### Module 47 — the 2026-09-08 evaluation's artefacts were never committed

`EVALUATION_REPORT_POST_FIXES.md` names `evaluation/gold32_results.json` and
`evaluation/gold32_pipeline_outputs.json` as its sources, but both files on
`main` are from `d313a60` (2026-09-06, Module 9) and produce that report's
numbers, not the post-fix report's. Consequences: §4.1's per-question table
cannot be reproduced from the repository; nobody can inspect the judge's
`reasons` for the six AR-1.0 / FC-0.0 questions that Modules 42–44 are scoped
against; and Modules 41–44 are currently specified against numbers with no
artefact behind them. Fix: commit the 2026-09-08 pair, or re-run and commit.
This does **not** invalidate the report — it makes it unverifiable, which for a
document written to be independently reproduced is its own defect.

### Not filed, but recorded

The judge moved **0.3** on D1 across five identical draws (§5d). No action is
proposed — the spread is tolerable for a 32-question mean — but Module 27 should
not read a sub-0.3 movement on any single question as a code effect. If that
becomes load-bearing, scoring each question `n=3` and taking the median is the
obvious remedy, and `summarize()` is now the single place it would go.
