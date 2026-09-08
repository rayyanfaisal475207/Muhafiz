# Module 43 — M7 answers with the wrong facts

**Branch:** `fix/m7-m2-cs4-factual-accuracy` (PR #38) · **Question:** M7 (roman-Urdu)
· **Gold:** mean minutes incident→report, **15.0 (2024) → 1401.3 (2026,
~23.4 hours)**

## Verdict, stated first

**Module 22's verification was right. The post-fix evaluation report's M7 row
is wrong — M7 did not regress, and there was never anything to fix in
`xagg.py`.**

The brief asked which of the two contradicting observations was wrong and said
that mattered more than the score. It is the report. Evidence below.

---

## 1. Root cause

The brief offered two hypotheses: M7 regressed after Module 22 (an aggregate
added since stole its dispatch), or Module 22's verification did not measure
what it reported. **Neither is what happened.** A third possibility the brief
did not list turned out to be the case: the report's M7 row does not describe
the code it was filed against.

### Layer 1 — the aggregate, re-derived by hand-written Cypher

Not by calling the aggregate, but by querying the graph directly and computing
the mean in the probe itself, so the aggregate could not launder its own bug:

```cypher
MATCH (i:Incident)
RETURN i.entity_id AS eid, i.incident_datetime AS idt, i.report_datetime AS rdt
```

```
total Incident nodes:     73
with BOTH timestamps:     64      <- matches the brief's stated 64 of 73
with incident only:        0
with neither:              9

  year 2024: n=13  mean_minutes=15.0     min=15.0  max=15.0
  year 2026: n=51  mean_minutes=1401.3   min=25.0  max=6510.0
```

**Gold exactly**, including both FIR counts. The run is confirmed against the
2026-09-08 dump: 64 of 73 Incidents carry both timestamps, as the brief
specified.

### Layer 2 — `run_aggregate()`'s dispatch

```
resolve_aggregate_kind(M7 gold text) -> "incident_to_report_minutes_by_year"
run_aggregate(M7 gold text)          -> {"kind": "time_bucketed_mean",
                                         "dimension": "incident_to_report_minutes_by_year",
                                         "buckets": [{2024, 15.0, 13},
                                                     {2026, 1401.3, 51}],
                                         "missing_timestamp_count": 0}
```

No aggregate added since Module 22 has taken M7's dispatch. The all-32
equality control now pins that (§3).

### Layer 3 — the live pipeline

Six live `/api/chat` runs (§4), all `route='XAGG' -> sub-agent='Large-Scale
Aggregate'`, all returning 15.0 / 13 FIRs and 1401.3 / 51 FIRs.

### So where did the report's wrong answer come from?

The only M7 answer recorded anywhere in this repository is in
`evaluation/gold32_pipeline_outputs.json`:

> "in 2024, **0%** of FIRs recorded a delay reason (0 of 13 FIRs), while in
> 2026, **14%** of FIRs recorded a delay reason (7 of 50 FIRs) … the system
> does not track delay days as a structured field"

That is not a wrong rendering of M7's aggregate. It is the **output of a
different function**: `_reporting_delay_rate_by_year()`, down to a paraphrase
of its own `note` field ("not a mean number of delay days — a day-level
reporting delay is not currently projected as a structured, queryable field").
Its buckets come from the day-granular `OCCURRED_ON`→`Date` edge, which is why
its 2026 denominator is 50 where the timestamp-based aggregate's is 51.

**`_reporting_delay_rate_by_year()` has had no dispatch entry in
`run_aggregate()` since Module 22 re-pointed M7 away from it.** It is
unreachable from any query text. So that answer cannot have been produced by
the code Module 22 shipped — it was produced by a build predating it.

The dates confirm it:

| Artifact | Last written | What it is |
|---|---|---|
| `evaluation/gold32_pipeline_outputs.json` | **2026-09-06 11:27** (commit `d313a60`) | Module 9's rerun |
| `evaluation/gold32_results.json` | 2026-09-06 11:27 (same commit) | Module 9's scores |
| Module 22's aggregate (`f364bf9`) | **2026-09-08 00:48** | two days later |
| `EVALUATION_REPORT_POST_FIXES.md` | 2026-09-08 16:01 | the report filing M7 at 0.0 |

And the committed scores are not the report's scores: `gold32_results.json`
records M7 at FC 0.0 / **AR 0.667**, M2 at 0.0/**0.0**, CS4 at 0.0/**0.2** —
where the post-fix report tabulates 1.0 answer-relevancy for all three. They
are provably different runs, and **the post-fix run's raw artifacts were never
committed to this repository.**

That is the honest limit of this finding: the post-fix run's M7 answer cannot
be inspected, only its score row, which reads `0.0 | 0.0 | no change`. Two
readings survive, and the evidence does not separate them:

- the 0.0 was carried forward from the pre-Module-22 measurement (the "no
  change" verdict is what that would look like), or
- the post-fix run predated **Module 41** (merged 2026-09-08, PR #31) and M7
  was decomposed by Meta-Analysis, the same mechanism the report itself
  diagnoses for G2/G5 in its §4 — in which case a sub-question dropping M7's
  comparison vocabulary lands on the neighbouring reporting-delay family.

Both readings put the fault outside `xagg.py`, and both are already closed on
`main`: the dispatch is pinned by an all-32 equality control, and Module 41's
guard keeps M7 out of Meta-Analysis (pinned in §3).

### The real defect this module found and fixed

**M7's aggregate emitted no log line, so no live run of it could be
identified from the log.** XAGG's SSE reports only `route='XAGG'`; the
`XAGG <kind>:` line in `backend.log` is the project's only proof of which
aggregate answered. Modules 31–36 each added one. Module 22 predates the
convention, so the single aggregate under investigation here was the one that
could not be observed — every run had to be identified by matching numbers out
of rendered prose, which is exactly the inference that made the original
contradiction unresolvable. That is a genuine, in-scope observability defect,
and it is what this module changes.

---

## 2. Change

| File | Change |
|---|---|
| `src/pipeline/xagg.py` | `_incident_to_report_minutes_by_year()` now emits `XAGG incident_to_report_minutes_by_year: <n> year bucket(s) [2024=…min/n=…, …], <n> row(s) excluded …`. The counts are in the line, not just the kind — a kind alone cannot distinguish a correct run from a wrong-metric one. |
| `tests/test_xagg.py` | Shared gold-32 fixtures (`_GOLD32_PATH`, `_gold32_items()`, `_M7_GOLD`/`_M2_GOLD`/`_CS4_GOLD`) with a test asserting the constants stay byte-identical to the dataset; three new M7 tests. |
| `tests/test_harness_supervisor.py` | M7's decomposition-skip pinned. |

No behaviour change. The aggregate, its dispatch and its rendering are
untouched, because measurement says they are correct.

**Why the fix sits there.** Everything else this investigation implicates is
out of scope by the brief: `supervisor.py` (Module 41's guard, already
correct), `meta_analysis.py` (Module 50), and the evaluation harness. Nothing
needed changing in any of them, so nothing was.

---

## 3. Unit tests

```
PYTHONPATH=. python -m pytest tests/test_xagg.py tests/test_harness_supervisor.py \
    tests/test_harness_tool_xagg.py -q
444 passed
```

New, all pinned to M7's **literal gold text**:

- `test_module43_m7_aggregate_logs_which_family_answered` — asserts the log
  line carries the kind *and* the per-bucket figures *and* the exclusion
  count.
- `test_module43_m7_gold_figures_survive_the_whole_dispatch_chain` — drives
  `run_aggregate()` with M7's gold text over a stub shaped to the live
  corpus's real distribution (13 rows at 15 min; 51 rows averaging 1401.3) and
  asserts the rendered text contains `15.0 minutes across 13 FIRs` and
  `1401.3 minutes (~23.4 hours) across 51 FIRs` — gold's own figures, end to
  end through both halves that failed independently in this question's history
  (Module 22 fixed the metric, Module 26 fixed the routing).
- `test_module43_m7_gold_text_still_resolves_only_to_the_mean_minutes_family`
  — **all-32 negative control asserting equality**: exactly `["M7"]` resolves
  to `incident_to_report_minutes_by_year`. This is the control that would have
  caught the brief's "an aggregate added since stole M7's dispatch"
  hypothesis. It does not fire, which is how that hypothesis was ruled out.
- `test_module43_m7_gold_text_skips_decomposition_and_reaches_the_aggregate`
  (supervisor) — `resolves_to_specific_aggregate(M7)` and
  `classify_to_subagent(...) == LARGE_SCALE_AGGREGATE`. Module 41 made this
  true; nothing pinned it for M7 until now, and it is the one remaining path
  back to a wrong-metric M7 answer.
- `test_gold32_fixture_texts_are_the_real_dataset_texts` — a gold text edited
  upstream breaks here rather than silently turning every control into a
  tautology.

---

## 4. Live verification

`POST /api/chat`, `admin@example.com`, All Cases, port 8014. **Six runs** —
three before the log line was added, three after.

**Question sent (verbatim gold text):**
`Kya log 2026 mein waqiaat ki police ko itni hi jaldi ittila de rahe hain jitni 2024 mein dete the?`

| Run | `route=` / sub-agent | Elapsed |
|---|---|---|
| 1 | `route='XAGG'` → `Large-Scale Aggregate` | 57.4 s |
| 2 | `route='XAGG'` → `Large-Scale Aggregate` | 53.4 s |
| 3 | `route='XAGG'` → `Large-Scale Aggregate` | 65.3 s |
| 4 | `route='XAGG'` → `Large-Scale Aggregate` | 126.4 s |
| 5 | `route='XAGG'` → `Large-Scale Aggregate` | 94.5 s |
| 6 | `route='XAGG'` → `Large-Scale Aggregate` | 57.0 s |

**Aggregate log line, runs 4–6** (identical all three times):

```
[INFO] src.pipeline.xagg: XAGG incident_to_report_minutes_by_year: 2 year bucket(s)
  [2024=15.0min/n=13, 2026=1401.3min/n=51], 0 row(s) excluded for a
  missing/unparseable/out-of-order timestamp
```

**Verbatim answer, run 6:**

> No, reporting is slower in 2026 compared to 2024. The mean time from
> incident to report was **15.0 minutes** across 13 FIRs in 2024, whereas in
> 2026, it was **1401.3 minutes (~23.4 hours)** across 51 FIRs [Document 1].

**Verbatim answer, run 4:**

> No, people are not reporting incidents to the police as quickly in 2026 as
> they did in 2024. According to [Document 1], the mean time from incident to
> report was **15.0 minutes** across **13 FIRs** in 2024, compared to
> **1401.3 minutes (~23.4 hours)** across **51 FIRs** in 2026. The document
> explicitly states that reporting is slower in 2026 than in 2024.

6 of 6 correct. No run produced the delay-reason metric.

---

## 5. Gold comparison

| Claim | Gold | Measured | Verdict |
|---|---|---|---|
| Direction | "Haan, bohat zyada farq se" (yes, hugely different) | "No, reporting is slower in 2026" — same finding, opposite polarity because gold answers *"is there a difference?"* and the system answers *"are they as quick?"* | ✅ agree |
| 2024 mean | 15.0 minutes | 15.0 minutes | ✅ exact |
| 2026 mean | 1401.3 minutes (~23.4 ghante) | 1401.3 minutes (~23.4 hours) | ✅ exact |
| 2024 n | (implied) | 13 FIRs | ✅ matches M1/M5's 13 |
| 2026 n | (implied) | 51 FIRs | ✅ |
| Coverage | — | 64 of 73 Incidents carry both timestamps; 9 carry neither | ✅ stated honestly |

**Gold's causal claim is not asserted by the system and was not checked
here.** Gold attributes the slowdown to "a shift toward fraud/cyber cases
victims discover later". That is a plausible reading of M1's statute mix, but
the aggregate computes elapsed time only and says nothing about why — which is
the correct behaviour, not a gap. Worth noting for the judge: an evaluator
scoring gold's *reasoning* rather than its *figures* could mark this down for
an omission that is deliberate.

---

## 6. Non-gold paraphrase

**Sent:** `How long does it typically take someone to report a crime to us
these days versus a couple of years ago?`
(Module 22's own paraphrase, reused so the two results are comparable.)

`route='XAGG'` → `Large-Scale Aggregate`, 22.2 s:

> According to [Document 1], the mean time from incident to report was **15.0
> minutes** in **2024** (across 13 FIRs) versus **1401.3 minutes (~23.4
> hours)** in **2026** (across 51 FIRs). Reporting is notably slower in 2026
> compared to 2024.

Correct, and it shares no vocabulary with the gold text — this is a capability,
not a string match.

---

## 7. Regression guard

Shared with Module 44 — see `MODULE44_RESULT.md` §7 for the full table
(M5, M4, M1, G2, G5, G3, CR7, and the Modules 31–36 questions G1, G6, CR3).

Module 43's own change is a `logger.info()` call and cannot alter any answer;
the unit suite (444 tests across the three files that cover XAGG dispatch, the
harness tool and the supervisor guard) is green.

---

## 8. New defects found

1. **Aggregates predating Modules 31–36 have no `XAGG <kind>:` log line.**
   Module 43 added M7's because it was the one under investigation. The same
   blind spot remains for the CR6/CR7/CR8/G2/G3 families and the
   entity-recurrence tier, and it is what makes any future "which aggregate
   answered?" question require a code change before it can be answered.
   **Filed as Module 55** — one line per remaining aggregate, mechanical, and
   worth doing before Module 27's rerun so that run is diagnosable.

Not folded into this module.

**Already filed, and independently confirmed here: Module 47.** This
module reached Module 47's finding — that the post-fix evaluation's artefacts
were never committed — from the opposite direction, and the two agree. Module
45 established it by re-deriving the *headline* figures from the committed
file (0.394 / 13-of-32, i.e. Module 9's, not the report's 0.572 / 19-of-32).
Module 43 establishes the same thing per-question: the committed M7 answer is
the output of a function Module 22 removed from the dispatch chain, and the
committed per-question scores (M7 AR 0.667, M2 AR 0.0, CS4 AR 0.2) are not the
report's (AR 1.0 for all three).

That convergence is the strongest single piece of evidence in this file, and
it raises Module 47's priority: **Modules 42, 43, 44 and 48 are each scoped
against a per-question score row whose answer nobody can read.** For M7 the
consequence is now measured rather than hypothesised — the question was filed
as broken and is not.
