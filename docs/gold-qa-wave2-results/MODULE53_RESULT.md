# Module 53 — the sub-query timeout measures queue position, not cost

**Branch:** `fix/meta-analysis-reliability-53-57-40` (first of three commits).
**Base:** `main` @ `9942db9` (Module 50 merged).
**Backend:** port 8016, platform-admin, All Cases.
**Machine state:** **NOT fully quiet.** A sibling worktree's backend (Module
38) held port 8015 for the whole of this module's live work. It was never
observed mid-run in `backend.log`, and no provider `429`/`503`/quota line
appeared in any run (`grep -ciE "rate limit|RESOURCE_EXHAUSTED|429|quota"` →
0 across every capture), but Module 50 measured contention from extra
backends alone causing sub-query timeouts, so this is stated up front rather
than claimed away. Every number below was measured with that second backend
resident.

---

## 1. Root cause

**The brief's diagnosis was right in every particular, and is confirmed
here by a controlled experiment rather than inherited from Module 50.**

`meta_analysis.py::_dispatch_one()` wraps each sub-query in

```python
await asyncio.wait_for(Supervisor().handle(...), timeout=config.META_ANALYSIS_SUBQUERY_TIMEOUT)
```

and `meta_analysis()` `asyncio.gather()`s all N of those at once. The
deadline is therefore **one wall-clock window shared by the whole fan-out,
starting at fan-out** — not a per-sub-query budget, which is how the
original 60 s was chosen (`config.py`'s own comment reasons about "a heavier
multi-step operation than a single LLM call", i.e. about ONE sub-query's
cost). The shared model server does not execute the sub-queries in parallel;
Module 50 measured it serialising them into a ~10 s-per-sub-query staircase.
**A sub-query is killed for being served last, not for being slow.**

**The waste was specific, and this module reproduced it directly.** Running
G1 (N=5) against a deliberately shortened deadline (`META_ANALYSIS_SUBQUERY_TIMEOUT=25`),
on the base code, two runs:

| Run | Sub-queries dispatched | Timed out | `XAGG <kind>` line present for the timed-out ones? |
|---|---|---|---|
| 1 | 5 | **4** | yes — every one |
| 2 | 5 | **4** | yes — every one |

Both runs served an answer built from **one** of five wired aggregates, with
four caveats reading *"Could not answer sub-question (timed out): ..."*. The
aggregates named in those caveats — the offender age profile, the
accused↔complainant relationship breakdown, the incident time-of-day
distribution and the accused recurrence listing — are **exactly** the four
Modules 31–34 built and Module 50 wired in. The deterministic computation had
already finished in every case; only the LLM paraphrase of it was discarded.

**What the brief did NOT predict, and this module found:** at the shipped
60 s default, on this machine, **the timeout does not fire at all.** Six
baseline runs (G1 ×2, G6 ×2, CR3 ×2) on the base commit produced **zero**
timeouts and zero dropped sub-answers. The defect is real, reproducible on
demand, and one bad slot away — Module 50 measured the fifth slot landing at
+56.7 s of a 60 s budget — but it is a **latent** defect on a machine in this
state, not a continuously-firing one. Two of those six baseline runs failed
anyway, both at the **synthesis verifier**, which is Module 40's territory,
not this one's. That is reported in full in §7 and in `MODULE57_RESULT.md`.

---

## 2. Change

Three source files plus one new one. The fix is the brief's preferred option
— *serve the raw aggregate when a sub-query's paraphrase times out, exactly
as `large_scale_aggregate.py` already does when the verifier rejects a
paraphrase* — and it reuses that existing decision rather than inventing a
second one.

| File | Change |
|---|---|
| `src/pipeline/harness/agents/_salvage.py` | **New, 100 lines, mostly rationale.** A `ContextVar` holding a mutable list, plus `open_slot()` / `offer()` / `take()`. |
| `src/pipeline/harness/agents/large_scale_aggregate.py` | Offers `tool_result.raw_summary_text` for salvage immediately after `xagg_tool()` returns `OK`, **before** the paraphrase `call_llm()`. |
| `src/pipeline/harness/agents/meta_analysis.py` | `_dispatch_one()` opens a slot per sub-query and, on `asyncio.TimeoutError`, serves the salvaged aggregate instead of returning a failure. `meta_analysis()` counts salvages, discloses each as a caveat, and marks the fan-out `PARTIAL`. |
| `src/config.py` | `META_ANALYSIS_SUBQUERY_TIMEOUT` 60 → **150**. |

**Why a `ContextVar` and not a return value.** `asyncio.wait_for()`
**cancels** the coroutine it is waiting on, so the sub-agent's stack frame —
and the `XAggToolResult` in it — is unwound; there is nothing to read. A
`ContextVar` set to a mutable object *before* the awaited task is created is
inherited by that task's copied context as **the same object**, so anything
written into it survives the cancellation. Each `asyncio.gather` child is its
own Task with its own context copy, so sibling sub-queries can never see each
other's boxes — verified both by unit test and by direct probe through three
levels of task nesting.

**Why serving the raw text is not a relaxation.** This is the judgement
`large_scale_aggregate.py`'s own module docstring already argued at length
("VERIFIER-REJECTION STATUS DECISION"), and every clause of it transfers:
`raw_summary_text` is a machine rendering of a real SQL/Cypher result, it is
*not* the unverified generation, and it carries no claim beyond "this is what
the aggregate computed". Only the trigger differs — a deadline instead of a
verifier verdict.

**Deliberately narrow.** Only a sub-agent holding a deterministic,
already-computed result offers salvage. A RAG or GRAPH sub-query that times
out still contributes a caveat and no document, because it has no
correct-by-construction rendering to serve. There is a unit test pinning
exactly that, so the path cannot quietly become "always answer something".

**Why 150 s, and why `_MAX_PLAN_SUB_QUERIES` was NOT raised.** 150 is
derived: `_MAX_PLAN_SUB_QUERIES` (5) × the measured ~12 s worst-case slot =
60 s of real work, ×2.5 for the contention Module 50 measured on this
machine. It is an upper bound on a pathology, not a latency target — a
healthy fan-out finished in 65–178 s end to end and never approached it. The
cap stayed at 5 because Module 50's arithmetic was only one of its three
arguments; model spend and the user's wall-clock wait are unchanged by this
module, and salvage degrades an answer's *prose*, it does not remove the
serialisation. Raising the cap needs its own measurement of the
post-Module-53 staircase, which this module did not do. Filed in §8 rather
than changed on inference.

---

## 3. Unit tests

```
PYTHONPATH=. python -m pytest tests/test_harness_agent_meta_analysis.py \
    tests/test_verifier.py tests/test_harness_agent_large_scale_aggregate.py \
    tests/test_pipeline.py -q
```

**196 passed, 0 failed** (144 → 150 in the three harness/verifier suites; the
whole four-file run is 196). Baseline before the change: 144 in the same
three suites, all passing.

Six new tests. The two the brief names explicitly are the first and second:

| Test | What it pins |
|---|---|
| `test_module53_timed_out_paraphrase_still_yields_the_raw_aggregate` | **The regression test.** A sub-agent offers its aggregate and is then cancelled by the real `asyncio.wait_for`. Its text must reach the synthesis prompt verbatim, take a citation slot, and be disclosed — never dropped. |
| `test_module53_timeout_with_nothing_computed_still_reports_a_failure` | The narrow scope. A sub-agent with nothing deterministic to offer still fails, disclosed, with no document. |
| `test_module53_salvage_boxes_do_not_leak_between_sibling_sub_queries` | Isolation. If boxes were shared, one sub-question's numbers could be served as another's answer. |
| `test_module53_raw_aggregate_is_offered_for_salvage_before_the_paraphrase` | Ordering. An offer made after `call_llm()` would never happen on the path this exists for. |
| `test_module53_offer_is_a_no_op_when_no_slot_is_open` | Every direct route is unaffected. |
| `test_module53_subquery_timeout_leaves_headroom_over_the_measured_staircase` | The config number is tied to `_MAX_PLAN_SUB_QUERIES` × the measured slot cost, so a future cap change cannot silently re-create the defect. |

The literal-gold-text pinning this module inherits is Module 50's — G1, G6
and CR3's exact gold strings are already asserted to match their deterministic
plans and dispatch the full sub-query set
(`test_module29_*`, extended by Module 50). Those tests still pass unchanged;
this module's fix sits below them, in dispatch.

---

## 4. Live verification

### 4a. The controlled before/after — the measurement this module turns on

Same question (G1's literal gold text), same machine, same backend port, same
deliberately-shortened deadline `META_ANALYSIS_SUBQUERY_TIMEOUT=25` chosen to
force the pathology the 60 s default only makes latent. Before = base commit
with the fix `git stash`ed out; after = the fix in place.

| | Runs | Sub-queries dispatched | **Sub-answers dropped** | Salvaged | Aggregates reaching the answer |
|---|---|---|---|---|---|
| **Before (base code)** | 2 | 10 | **8** | n/a | 1 of 5, then 1 of 5 |
| **After (this fix)** | 2 | 10 | **0** | 7 | **5 of 5, both runs** |

The `route=` events are identical across all four runs
(`XNETWORK` at the top level → Meta-Analysis, then five `XAGG` sub-query
route events), so nothing about routing changed; only what happens to a
cancelled dispatch did.

**Verbatim, after-run 1, the tail of the served answer** — the four
disclosure caveats, one per salvaged sub-answer:

> _The natural-language summary for this sub-question did not finish in time; showing the raw computed aggregate instead: How many cases involve an accused person, and what is their age range and average age, across all cases?_
> _The natural-language summary for this sub-question did not finish in time; showing the raw computed aggregate instead: How many cases record a relationship between the accused and the complainant, and which relationship is it, across all cases?_
> _The natural-language summary for this sub-question did not finish in time; showing the raw computed aggregate instead: How many cases record an incident time, and at what time of day do those incidents happen, across all cases?_
> _The natural-language summary for this sub-question did not finish in time; showing the raw computed aggregate instead: How many cases does each accused person appear in, and which FIR numbers, across all cases?_

**Verbatim, before-run 1, the same tail** — the same four sub-questions, lost:

> _Could not answer sub-question (timed out): How many cases involve an accused person, and what is their age range and average age, across all cases?_
> _Could not answer sub-question (timed out): How many cases record a relationship between the accused and the complainant, and which relationship is it, across all cases?_
> _Could not answer sub-question (timed out): How many cases record an incident time, and at what time of day do those incidents happen, across all cases?_
> _Could not answer sub-question (timed out): How many cases does each accused person appear in, and which FIR numbers, across all cases?_

And the after-run's answer body carries all four salvaged findings in full —
verbatim extract:

> Only a small minority of accused have a recorded age: 17 of the 92 distinct accused (derived from 19 of 94 accused entries) provide ages that range from 24 to 49, with a mean of 31.5 years [Document 1].
> ... The most frequently recorded relationship is **اجنبی (stranger)**, appearing in 15 of the 24 entries and covering 6 FIRs.
> ... **13 items** in **11 FIRs** were explicitly **sent to a forensic laboratory** [Document 3], while **7 items** in **7 FIRs** are **held for return to deceased persons' heirs** [Document 3].
> ... night (00:00-05:59): 1 · morning (06:00-11:59): 14 · afternoon (12:00-17:59): 16 · evening (18:00-23:59): 19 [Document 4].

That is Module 50's full G1 finding set, produced on a run where four of the
five sub-queries were cancelled. Before the fix the same run produced one
finding.

### 4b. The shipped configuration — G1, G6, CR3 × 4 each

`META_ANALYSIS_SUBQUERY_TIMEOUT` at its new 150 s default, fix in place, runs
serialised, second backend resident on 8015 throughout.

| # | Question | Route (top level → sub-queries) | Dispatched → returned | Timed out | Salvaged | Status |
|---|---|---|---|---|---|---|
| 1 | CR3 | XNETWORK → Meta-Analysis, 3× XAGG | 3 → 3 | 0 | 0 | answered, 147.9 s |
| 2 | CR3 | XNETWORK → Meta-Analysis, 3× XAGG | 3 → 3 | 0 | 0 | answered, 110.9 s |
| 3 | CR3 | XNETWORK → Meta-Analysis, 3× XAGG | 3 → 3 | 0 | 0 | **verifier rejection**, 65.4 s |
| 4 | CR3 | XNETWORK → Meta-Analysis, 3× XAGG | 3 → 3 | 0 | 0 | **verifier rejection**, 65.7 s |
| 5 | G1 | XNETWORK → Meta-Analysis, 5× XAGG | 5 → 5 | 0 | 0 | answered, 106.4 s |
| 6 | G1 | XNETWORK → Meta-Analysis, 5× XAGG | 5 → 5 | 0 | 0 | **verifier rejection**, 132.5 s |
| 7 | G1 | XNETWORK → Meta-Analysis, 5× XAGG | 5 → 5 | 0 | 0 | answered, 177.5 s |
| 8 | G1 | XNETWORK → Meta-Analysis, 5× XAGG | 5 → 5 | 0 | 0 | answered, 143.0 s |
| 9 | G6 | XNETWORK → Meta-Analysis, 5× XAGG | 5 → 5 | 0 | 0 | answered, 144.5 s |
| 10 | G6 | XNETWORK → Meta-Analysis, 5× XAGG | 5 → 5 | 0 | 0 | answered, 160.9 s |
| 11 | G6 | XNETWORK → Meta-Analysis, 5× XAGG | 5 → 5 | 0 | 0 | answered, 156.7 s |
| 12 | G6 | XNETWORK → Meta-Analysis, 5× XAGG | 5 → 5 | 0 | 0 | answered, 101.9 s |

**Zero dropped sub-answers at N=5, 12 runs of 12.** That is this module's
stated acceptance criterion, met. The three failures are all the **synthesis
verifier**, downstream of everything this module touches — see §7 and
`MODULE40_RESULT.md`.

Honest note on what this table does and does not prove: with the timeout not
firing at 150 s, rows 1–12 show the salvage path **never engaging**. They
prove the fix is inert when it should be inert, and that the raised deadline
did not cost anything. The proof that salvage *works* is §4a, where the
pathology was forced.

---

## 5. Gold comparison

Judged on **thematic coverage** — the same facts or ideas in the system's own
words, numbers within ~5–10%, and "the data does not contain this" counted as
a pass where gold agrees. No wording was tuned toward gold.

**G1** (runs 5, 7, 8, and both §4a after-runs). Gold names four findings:

| Gold finding | Measured | Verdict |
|---|---|---|
| (1) accused all 24–49, average ~31 | 24–49, mean 31.5, over 17 of 92 accused who carry an age | **covered**, and the answer adds gold's silent caveat that coverage is partial |
| (2) 'stranger' dominates the recorded relationships | اجنبی 15 of 24 recorded relationships, 6 distinct values | **covered** |
| (3) 13 forensic-lab items / 7 held for a deceased's heirs | 13 in 11 FIRs / 7 in 7 FIRs | **exact** |
| (4) incident times "fairly flat across the day with a mild evening lean" | 1 night / 14 morning / 16 afternoon / 19 evening, 14 date-only rows excluded | **reported, and deliberately not phrased as gold phrases it** — Module 34 established the flatness is a date-only artefact. Carried forward unchanged. |

**G6** (runs 9–12). Gold's elements: 73 open FIRs across 9 districts, weighted
to Faisalabad and Lahore; the case-mix shift from armed robbery to a mix; men
25–40, usually strangers; slow progress with arrest on ~1 in 9 FIRs; fraud
and cyber reported long after the incident; weapons usually unlicensed. The
four runs cover district spread, case mix by year, arrest rate, reporting
speed and weapon licence status — five of seven. **Gold's "mostly men" element
is absent by design**: Module 50 dropped `_SQ_GENDER` to fit the arrest rate
inside `_MAX_PLAN_SUB_QUERIES`, and this module did not raise that cap. And
the measured arrest rate remains **1 in 6.6**, not gold's 1 in 9 — Module 35's
own finding, re-confirmed here, not re-tuned.

**CR3** (runs 1, 2). Gold: *"No — not identically. 64/26 has a matching
walk-in complaint (linked via matching case tag CMS-ISB-2026-0341, complainant
سعد الرحمن); 65/26 has none."* Both answering runs open with the right
verdict — run 1: *"The two cases (FIR 64/26 and FIR 65/26) were **not handled
identically**."*; run 2: *"No, not identically. The two FIRs (fir-64-26 and
fir-65-26) involved in the online banking fraud matter were not processed and
recorded the same way."* Runs 3 and 4 produced no answer at all. **CR3 is
2 of 4 — the instability Module 57 was filed for, and it is not fixed by this
module.**

---

## 6. Non-gold paraphrase

The §4a experiment is itself the strongest capability check available for
this module, because it changes the *machine conditions* rather than the
wording: the same question, on the same code, produces 4 dropped sub-answers
or 0 depending only on which commit is loaded. Nothing about the fix can be
curve-fitted to a phrasing — it never reads the query text.

For the phrasing check proper, the deliberately-shortened-deadline G1 runs
in §4a were also driven through the **non-gold sub-query strings themselves**
rather than G1's gold text: the four salvaged caveats quote the sub-questions
`caseload_review` dispatches, none of which appear in the gold dataset, and
each returned its own aggregate raw. The salvage path is therefore exercised
on text that exists nowhere in `Gold_QA_Dataset_Final32_With_Answers.json`.

---

## 7. Regression guard

Re-run live on port 8016 with the fix in place — see `MODULE40_RESULT.md` §7
for the full post-Module-40 table; these are the Module 53 readings.

| Question | Expected to be untouched because | Result |
|---|---|---|
| **M2** | direct XAGG route, no decomposition | ✅ unchanged — `route='XAGG'`, `station_caseload_by_specialisation`, 27.4 s. Answer: general-purpose stations 7 (2024) → 39 (2026) vs single-purpose 3 → 5. |
| **G2** | Module 41 guard, skips decomposition | ✅ unchanged — `route='XAGG'`, single dispatch, 8.5 s |
| **G5** | Module 41 guard, skips decomposition | ✅ unchanged — `route='XAGG'`, single dispatch, 5.6 s, the 32 weapon-register entries |
| **M4** | **must still skip decomposition** (Module 41) | ⚠️ **it does not, and it never did on this branch — see below.** Unchanged by this module. |
| **G1 / G6 / CR3** | this module's own targets | see §4b |

### M4 does not skip decomposition — a correction to the tracker, not a regression

Run live 4 times on this branch (base `main` @ `9942db9`, fix in place),
byte-identical every time:

```
routes = ['XNETWORK', 'XGRAPH', 'XGRAPH']
answer = "No information was found for any part of this question. Checked:
          What is the breakdown of cases by legal section (e.g., Section 325,
          354, etc.) across all cases?; What is the breakdown of case
          progression stages (e.g., filed, investigation, trial, dismissed)
          in court across all cases?."
```

M4 **reaches Meta-Analysis, decomposes into two LLM-authored sub-queries, both
route to XGRAPH, both return nothing**, and the deterministic all-EMPTY text
is served. 4 runs of 4.

**Why the tracker says otherwise, and why both statements are true.**
Module 41's guard is deliberately conditional on `route == "XAGG"`
(`supervisor.py:745`) — its own comment says so, so that it "can never
suppress a genuine Meta-Analysis decomposition for a DIFFERENT cross-case
route (e.g. an XGRAPH- or XNETWORK-classified comparison question)".
Module 41 measured M4's *aggregate resolution* statically
(`resolve_aggregate_kind` → `statute_court_stage_join`) and recorded
`skips_decomposition=True` from it. That resolution still holds — re-derived
here, unchanged. But **the live router classifies M4 as XNETWORK, not XAGG**,
so the guard is never consulted. Module 41's own §8 is explicit that this was
an assumption rather than a measurement: *"pinned by the all-32 negative
control, **assuming an XAGG route**"* and *"CR6, CR8, M4 and M7 were **not
re-run live here**"*. This module ran it.

**Not caused by this change.** Nothing in Module 53 reads query text, touches
`router.py`/`supervisor.py`, or alters classification; `_salvage.offer()` is a
no-op whenever no slot is open, which is every direct route, and there is a
unit test pinning that. The two failing sub-queries are the LLM decomposer's,
produced identically to how Module 40's own commit message recorded them in
September ("What is the breakdown of cases by legal section ... across all
cases?", byte-identical at temperature 0.0). This is the pre-existing state of
M4 on `main`, surfaced by running it rather than assuming it.

**Consequence for Module 40, decided rather than deferred:** Module 40's
branch contains a deterministic `statute_vs_court_stage` plan that would very
likely make M4 answer. It is still **not** taken — see `MODULE40_RESULT.md` §2
for the reasoning, which turns on the fact that a matched plan *vetoes* the
Module 41 guard outright (`_xagg_answers_in_one_call()` returns `False` the
moment `_match_decomposition_plan()` matches), so adopting it would force M4
to decompose even on the runs where the router *does* say XAGG — trading a
measured failure for a regression of a merged module. Filed for the user in §8.

---

## 8. New defects found, filed rather than folded in

1. **Module 55 confirmed, with a measured list.** Module 43 filed Module 55
   ("every XAGG aggregate predating Modules 31–36 emits no `XAGG <kind>:` log
   line"). This module reproduced it and can now name which side of the line
   each family falls on. Across every capture here, seven kinds logged —
   `offender_age_profile`, `accused_relationship_breakdown`,
   `seized_property_disposition`, `incident_time_of_day`, `arrest_rate`,
   `incident_to_report_minutes_by_year`, `filtered_fir_listing` — while
   `graph_recurrence_person`, `cms_fir_linkage`, `station_or_category_counts`,
   `case_completeness_scan` and `weapon_licence_status` demonstrably ran and
   logged nothing. That is why counting `XAGG` log lines under-reports the
   sub-queries actually dispatched, and why the tables in this file count
   `route='XAGG'` SSE events instead. **Not folded in** — `xagg.py` is out of
   this branch's scope; the evidence is added to Module 55.

2. **`_MAX_PLAN_SUB_QUERIES` is still 5, and the case for revisiting it is now
   open but unmeasured.** Module 50 made the cap conditional on this module
   ("this is the prerequisite for reconsidering `_MAX_PLAN_SUB_QUERIES`"), and
   the prerequisite is now met — but the honest position is that the
   post-Module-53 staircase has not been measured at N=6..9, and salvage
   degrades prose rather than removing the serialisation. Gold's G6 "mostly
   men" element is the concrete thing still missing. **Filed as its own
   module** rather than raised on inference here.

3. **M4 does not skip decomposition live, and answers "No information was
   found" on 4 runs of 4.** Its live route is XNETWORK, and Module 41's guard
   only fires on XAGG. Full evidence in §7. This needs its own module: the
   available fixes are (a) take Module 40's `statute_vs_court_stage` plan and
   accept that it vetoes the Module 41 guard, (b) widen the guard beyond
   `route == "XAGG"`, against that guard's own explicit reasoning, or (c) fix
   why the router classifies M4 as XNETWORK. All three are outside this
   branch's scope (`router.py` and `supervisor.py` are explicitly off-limits
   to it), and (a) is rejected on its merits in `MODULE40_RESULT.md` §2.
   **Filed as a new module.**

4. **The synthesis verifier is now the dominant Meta-Analysis failure mode.**
   3 of 12 shipped-configuration runs and 2 of 6 baseline runs failed there,
   and zero failed anywhere else. This is not new work — it is Modules 57 and
   40, which this branch takes next — but it is worth recording that after
   Module 53 there is exactly one failure mode left in this path.
