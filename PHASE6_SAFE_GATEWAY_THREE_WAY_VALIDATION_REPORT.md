# Phase 6 — End-to-End Three-Way Aggregate Validation

**Status: complete.** The 42-case corpus has been measured twice through the
Phase 5D safe-gateway architecture with a live model: once before the five
implementation fixes, and once after. **The post-fix run is the
authoritative Phase 6 baseline.**

Branch `feat/aggregate-engine-phase0` · HEAD `7fdd54c`
Pre-fix run 19:15–19:34 (18.9 min) · Post-fix run 19:56–20:17 (20.7 min)

---

## 1. Executive summary

The live model can plan AGE aggregates through `AgeQueryPlan`. In the
authoritative post-fix run there were **zero failures at every stage of the
pipeline** — no generation failures, no plan-parse failures, no validator
rejections, no compile failures, no compiler defects. Every execution
targeted `muhafiz.evidence_graph` and nothing else.

The five fixes — all to defects in my own Phase 5D code, none in the model
or the architecture — roughly **doubled AGE coverage**: numeric results
11 → 22, correct results 5 → 13, AGREEMENT 4 → 8.

One finding cuts the other way and is reported prominently rather than
averaged away: `time_window_2026` **regressed in kind**. It previously
failed closed; it now returns a confident wrong number (§7.2).

---

## 2. Two runs, clearly separated

| | PRE-FIX run | POST-FIX run *(authoritative)* |
|---|---|---|
| Report file | `phase6_three_way_report.PREFIX.json` | `phase4_three_way_report.json` |
| Registry as-of | 2026-09-16T14:15:25Z | 2026-09-16T14:56Z |
| Duration | 18.9 min | 20.7 min |

The historical Phase 4 run is preserved separately as
`phase4_three_way_report.PRE_PHASE6.json` — a different artifact, not to be
confused with the pre-fix Phase 6 run.

The eight individually re-run cases from the fix-verification step are
**not** a substitute for this full run and are not presented as one.

---

## 3. Preconditions (post-fix run)

| Check | Result |
|---|---|
| Five fixes present in code | verified line-by-line |
| Pre-run regression | **330 passed, 4 skipped** |
| Production before | 73 / 430 / 4702 / 4942 / 13467 / 1012 ✓ |
| Model endpoint | `/health` 200 |
| Corpus | unmodified, 42 cases |

---

## 4. Authoritative post-fix results

### Structured route

| | Count |
|---|---|
| Valid | **25** |
| Incorrect | **0** |
| Refused | **17** |

Exactly matching corpus expectations.

### AGE planner

| Outcome | Count |
|---|---|
| Valid plans generated | **22** |
| Numeric results | **22** |
| Correct | **13** |
| Wrong | **2** |
| Refused (`model_declined`) | 18 |
| Refused (`scope_denied`, role gate) | 2 |

### AGE non-results, classified separately (not merged)

| Class | Count |
|---|---|
| Correctly unsupported by grammar | **13** |
| Planner/schema failure | **0** |
| Validator refusal | **0** |
| Execution failure | **0** |
| Valid plan but semantically wrong | **2** |
| Model declined on an expressible shape | **5** |

### Reconciliation

| Classification | Count |
|---|---|
| SINGLE_ROUTE_VALID | **17** |
| CORRECT/REFUSED | **11** |
| ALL_AGREE → AGREEMENT | **8** |
| SEMANTICALLY_DIFFERENT | **4** |
| CONFLICT | **2** |

### Evaluation vs independent ground truth

| Verdict | Count |
|---|---|
| CORRECT_REFUSAL | 17 |
| ALL_AGREE | 12 |
| STRUCTURED_CORRECT_AGE_WRONG | 12 |
| STRUCTURED_AND_AGE_AGREE | 1 |
| INCORRECT_REFUSAL | **0** |

### Semantic route

**42/42 SUCCESS**, 0 failures. 485 numeric mentions recorded for diagnosis,
**0 promoted into an aggregate result**. `numeric` remains `None` on every
case.

---

## 5. Pre-fix → post-fix comparison

| Metric | Pre | Post | Δ |
|---|---|---|---|
| AGE numeric results | 11 | **22** | **+11** |
| AGE correct | 5 | **13** | **+8** |
| AGE wrong | 3 | **2** | −1 |
| AGREEMENT | 4 | **8** | **+4** |
| ALL_AGREE | 4 | **12** | **+8** |
| SINGLE_ROUTE_VALID | 20 | 17 | −3 |
| SEMANTICALLY_DIFFERENT | 2 | 4 | +2 |
| CONFLICT | 1 | 2 | +1 |
| REFUSED | 15 | 11 | −4 |
| STRUCTURED_CORRECT_AGE_WRONG | 20 | **12** | **−8** |
| Declines — expressible shape (defects) | 11 | **5** | **−6** |
| Declines — genuine grammar gap | 16 | 13 | −3 |
| CORRECT_REFUSAL | 17 | 17 | 0 |
| INCORRECT_REFUSAL | 0 | 0 | 0 |

**AGE valid-plan coverage roughly doubled** (11 → 22 of 42) and correct
results rose from 5 to 13, with no loss of correct refusals.

SINGLE_ROUTE_VALID and REFUSED fell because cases moved *up* into
AGREEMENT — the AGE route now independently corroborates figures it
previously could not compute.

### The 12 `STRUCTURED_CORRECT_AGE_WRONG`, split by real cause

| Cause | Count |
|---|---|
| Approved capability gap (ratio 2, threshold 1, time_window 7) | **10** |
| Model declined on an expressible shape | 1 |
| AGE produced a genuinely wrong number | **2** |

Only **2 of 12** are real AGE errors. Per §6 of the brief the classifier was
**not** modified; these are reported separately instead.

---

## 6. Ratio/percentage — approved structured-only outcome

| Case | Structured | AGE | Reconciliation |
|---|---|---|---|
| `ratio_cases_multi_officer_entity_grain` | SUCCESS 5.47945205479452 | declined | **SINGLE_ROUTE_VALID** |
| `ratio_cases_multi_officer_relationship_grain` | SUCCESS 95.89041095890411 | declined | **SINGLE_ROUTE_VALID** |
| `ratio_zero_denominator_refused` | REFUSED | declined | **REFUSED** |

Behaviour is exactly as approved. The harness labels the two value-cases
`STRUCTURED_CORRECT_AGE_WRONG`; that is a classifier artifact, **not an AGE
correctness failure**. **2 corpus questions currently lack independent AGE
verification** for this reason.

---

## 7. The six fixed cases — and one regression in kind

### 7.1 All six held in the full run ✓

| Case | Expected | Got | Reconciliation |
|---|---|---|---|
| `min_person_age` | 24 | **24** | AGREEMENT |
| `max_person_age` | 49 | **49** | AGREEMENT |
| `avg_person_age` | 31.842105263157894 | **31.842105263157894** | AGREEMENT |
| `sum_person_age` | 605 | **605** | AGREEMENT |
| `weapons_unlicensed_property_filter` | 30 | **30** | SEMANTICALLY_DIFFERENT |
| `malkhana_records` | 45 | **45** | SEMANTICALLY_DIFFERENT |

No regressions. The two SEMANTICALLY_DIFFERENT verdicts are correct despite
identical values — see §9C.

### 7.2 NEW RISK — `time_window_2026` regressed in kind

| | Plan filters | Compiled query | Result |
|---|---|---|---|
| Pre-fix | `date IN [...]` | date-restricted traversal | `empty_result` — **failed closed** |
| Post-fix | `[]` | `MATCH (a:Case) RETURN count(a)` | **73** (truth 51) |

The planner **silently dropped the date restriction** and counted the entire
population. Reconciliation caught it as **CONFLICT and served nothing**, so
the architecture held — but a confidently wrong, silently-unrestricted
population is a worse failure *mode* than an empty result.

This is not an implementation defect I am authorised to fix: `time_window`
is architectural decision **A**. It materially strengthens the case for
resolving it, and is recorded here as a new risk rather than absorbed into
"AGE wrong 3→2".

---

## 8. §11 — parse-failure reporting contradiction, resolved

The Phase 6 summary said both *"AGE parse failures: 1"* and *"zero
plan-parse failures"*. **Both were true of different pipeline stages, and my
summary conflated them under one label.** Corrected terminology:

| Stage | Pre-fix | Post-fix |
|---|---|---|
| `generation_unparseable` — model never returned usable JSON | **1** | **0** |
| `plan_unparseable` — JSON returned, failed `AgeQueryPlan` parsing | 0 | 0 |
| `plan_invalid` — parsed, validator rejected | 0 | 0 |
| `compile_failed` | 0 | 0 |
| `compiler_defect` | 0 | 0 |

The single pre-fix failure was `time_window_2024`, which failed **before**
reaching the plan schema. It was a **final per-case failure**, not a
recovered retry. Post-fix, every stage is zero.

`model_declined` (18) and `scope_denied` (2) are **not failures**: the first
is model judgement, the second the role gate working.

---

## 9. Security properties — held under load

| Property | Result |
|---|---|
| Execution targets seen | **`muhafiz.evidence_graph` only** |
| Raw LLM Cypher reaching production | **NO** — no `cypher` field exists in `AgeQueryPlan` |
| Plans generated | 22 |
| Validator violations | 0 |
| Guard findings | 4, all SEMANTIC-advisory; **0 SAFETY**, nothing blocked |
| Production after run | **unchanged** |

The runtime path remains
`LLM → AgeQueryPlan → strict parser → validator → deterministic read-only
compiler → Cypher guard → resource controls → muhafiz.evidence_graph`.
No fallback was introduced.

---

## 10. Remaining capability gaps

| Gap | Cases | Status |
|---|---|---|
| `time_window` | 7 declined + 1 wrong (§7.2) | Decision **A** |
| ratio/percentage | 2 value + 1 refusal | Decision **B** |
| `threshold` | 1 | Not in grammar |
| `comparison` | 1 | Not in grammar |
| `median` | 1 | No AGE percentile function |

**5 residual declines on expressible shapes** (down from 11) are now genuine
model judgement about semantics the graph does not carry — role types
distinguishing "accused", `investigation_status`, `verdict`, case
`category`, and the absence of a usable `Address` key. These are not card
defects and I made no change for them.

---

## 11. Performance

| Metric | Pre-fix | Post-fix |
|---|---|---|
| Total runtime | 18.9 min | **20.7 min** |
| Mean per case | 27.0 s | 29.6 s |
| AGE generation (median) | 29.7 s | **40.4 s** |
| AGE generation (max) | 44.9 s | 81.9 s |
| AGE execution (mean) | 102 ms | **96 ms** |

The enriched schema card (6,532 → 9,434 chars) raised generation latency
while roughly doubling coverage. Compiled execution remains ~96 ms —
generation dominates end-to-end cost by ~400×.

---

## 12. Regression tests

| | Result |
|---|---|
| Before post-fix run | 330 passed, 4 skipped |
| **After post-fix run** | **330 passed, 4 skipped** |

No regression. New Phase 6 tests remain 11 in
`tests/test_age_gateway_phase6_fixes.py` plus 1 boundary assertion.

---

## 13. Production integrity

| Object | Before | After |
|---|---|---|
| `Case` | 73 | **73** |
| `Person` | 430 | **430** |
| `SAME_AS` | 4702 | **4702** |
| vertices | 4942 | **4942** |
| edges | 13467 | **13467** |
| documents | 1012 | **1012** |

Unchanged across both full runs.

---

## 14. Architectural decisions — SUPERVISOR DECISION REQUIRED

### A. `time_window` — SUPERVISOR DECISION REQUIRED

**Current limitation.** `incident_date` is Postgres-authoritative
(`cases.incident_date`, 64/73). The AGE `Case` node carries no date property.
The structured route resolves windows via `temporal.resolve_window()` into a
case-id allow-list; `AgeQueryPlan` has no equivalent.

**Why it matters.** Largest single gap — 7 declines plus the §7.2 case where
the planner dropped the restriction and returned a wrong number. The failure
mode is now *silently unrestricted population*, not refusal.

**Realistic options.** (a) Accept permanently — 8 cases structured-only.
(b) Add a `case_id IN $allow_list` plan filter reusing the structured
resolver — small grammar change, but couples the routes through a shared
resolver and weakens independence. (c) Add a date traversal over
`(Incident)-[:OCCURRED_ON]->(Date)` — preserves independence, but the graph
date is known to be backfilled and unreliable.

### B. Ratio/percentage — SUPERVISOR DECISION REQUIRED

**Current limitation.** No ratio operator in `AgeQueryPlan`; 2 value-cases
have no independent AGE verification.

**Why it matters.** Percentages are among the most commonly requested
aggregates; these figures currently rest on a single route.

**Realistic options.** (a) Keep unsupported — structured stays sole
authority. (b) Add a numerator/denominator plan construct. (c) Let AGE
compute both populations and have reconciliation derive the ratio.

### C. `count_distinct` / grain semantics — SUPERVISOR DECISION REQUIRED

**Current limitation.** All four SEMANTICALLY_DIFFERENT verdicts are cases
where both routes returned the **same correct number** but declared
different semantics — e.g. `count_distinct/ENTITY` vs `count/ENTITY`
(208, 30), `count_distinct/RECORD` vs `count/ENTITY` (45), and
`count/ENTITY` vs `count_distinct/ENTITY` (13). `comparable_key()` compares
`(interpretation, grain, grouping)` before values, so these never reach
AGREEMENT.

**Why it matters.** Four correct corroborations are being reported as
semantic mismatches, understating real agreement. The rule itself is sound —
it is what keeps "92 distinct accused" apart from "94 accused involvements".

**Realistic options.** (a) Leave as-is — conservative, understates
agreement. (b) Prompt the planner to declare `count_distinct` whenever it
emits `count(DISTINCT …)`. (c) Have reconciliation treat
`count`/`count_distinct` as comparable when the compiled query demonstrably
used DISTINCT.

---

## 15. Files changed this task

| File | Change |
|---|---|
| `docs/aggregate-shadow/phase4_three_way_report.json` | post-fix run output (authoritative) |
| `docs/aggregate-shadow/phase6_three_way_report.PREFIX.json` | **new** — pre-fix run preserved |
| `PHASE6_SAFE_GATEWAY_THREE_WAY_VALIDATION_REPORT.md` | rewritten with both runs |

No source changes were made in this task.

---

## 16. Immediate next step

Take decisions **A**, **B** and **C** to the supervisor. **A** is the most
urgent: `time_window` is both the largest coverage gap and the only case
where the current behaviour produces a confidently wrong number rather than
a refusal.

No capability expansion started, per the stop condition.
