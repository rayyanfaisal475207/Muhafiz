# Phase 7C — Gate-Profile + Multi-Path Full Corpus Validation

**Status: complete, with a regression that must be fixed before this
architecture ships.** The multi-path composition layer works exactly as
designed. The gate-profile layer contains four implementation defects — all
mine, all in Phase 7B/7C code — one of which cost two previously-correct
answers.

Branch `feat/aggregate-engine-phase0` · HEAD `7fdd54c` · run 25.1 min ·
production unchanged.

---

## 1. Executive summary

**Multi-path composition: working.** All 12 composite cases behaved
correctly. All nine time-window value cases returned the exact truth
(13 / 51 / 0 / 51 / 13 / 13 / 0 / 1 / 1) via Postgres authority, and the
three refusal cases refused while *naming the unapplied constraint*. **Zero
constraints were silently dropped across all 42 cases.** The Phase 6
failure class (truth 51, answered 73) is eliminated on the structured side.

**Gate profiles: not yet safe to rely on.** AGREEMENT fell 8 → 6 and
ALL_AGREE 12 → 8. Two cases that were correct in Phase 6 now refuse
(`min_person_age` = 24, `max_person_age` = 49), blocked by my own
`incompatible_gate_profile` check. Worse, gate evaluation on the direct
path is **advisory** — five cases failed gates and were served anyway,
contradicting the rule that a failed gate blocks the result.

The honest summary: the part of Phase 7 that computes answers got better;
the part I added in 7B to police answers is not yet correct.

---

## 2. Starting baseline

| Item | Value |
|---|---|
| Regression before | 442 passed, 10 skipped |
| Production before | 73 / 430 / 4702 / 4942 / 13467 / 1012 |
| Phase 6 baseline preserved | `phase6_three_way_report.POSTFIX.json` |

**One wiring defect was fixed before the run.** The harness called
`route_structured.run()` without `gate_profile_id`, and `route_age` never
surfaced `plan.gate_profile_id`. Running as-is would have recorded
`selected=None` on all 42 cases and reported "0 exact / 0 escalated /
0 unknown" as though the model never chose. AGE now runs first and its
selection is passed through; only the profile id crosses between routes.

---

## 3. Corpus results (42 cases)

### Reconciliation — Phase 6 → Phase 7C

| Classification | Phase 6 | Phase 7C | Δ |
|---|---|---|---|
| AGREEMENT | 8 | **6** | **−2** |
| SINGLE_ROUTE_VALID | 17 | 20 | +3 |
| SEMANTICALLY_DIFFERENT | 4 | **2** | −2 |
| CONFLICT | 2 | 3 | +1 |
| REFUSED | 11 | 11 | 0 |

### Evaluation vs ground truth

| Verdict | Phase 6 | Phase 7C | Δ |
|---|---|---|---|
| ALL_AGREE | 12 | **8** | **−4** |
| STRUCTURED_AND_AGE_AGREE | 1 | 1 | 0 |
| STRUCTURED_CORRECT_AGE_WRONG | 12 | 14 | +2 |
| **AGE_CORRECT_STRUCTURED_WRONG** | 0 | **2** | **+2** |
| CORRECT_REFUSAL | 17 | 17 | 0 |
| INCORRECT_REFUSAL | 0 | 0 | 0 |

`AGE_CORRECT_STRUCTURED_WRONG` appearing for the first time is the
regression signal: the structured route lost two answers the AGE route got
right.

---

## 4. Gate-profile selection

| Outcome | Count |
|---|---|
| Exact compatible | 6 |
| Escalated (under-powered) | 5 |
| Stronger compatible | 8 |
| Unknown profile | **0** |
| Incompatible | **3** |
| No selection (model omitted it) | 20 |

**The model omitted `gate_profile_id` on 20 of 42 cases** — nearly half.
Trusted derivation supplied the profile in those cases, so nothing was
weakened, but selection coverage is poor and the prompt may need work.

All five escalations were correct and all were time-window cases:
`FILTERED_COUNT → MULTI_SOURCE_FILTERED_COUNT` (×4) and
`→ MULTI_SOURCE_FILTERED_DISTINCT_COUNT` (×1). In every case the stronger
profile was genuinely required, the stronger gates were enforced, and no
constraint was skipped.

---

## 5. Implementation defects found

### D1 — Value measures have no profile *(caused the regression)*

`min/max/avg/sum` derive `required = SIMPLE_COUNT`, a **counting** shape.
The model selected `GROUPED_AGGREGATE`; `_shape_incompatible()` refuses
rather than escalating, so the route refused **before routing**
(`engine=None`).

Lost: `min_person_age` (24), `max_person_age` (49). A third case,
`avg_over_fanning_population_refused`, refused for this *wrong reason* —
it was expected to refuse, so the outcome masked the defect.

`avg_person_age` (31.84) and `sum_person_age` (605) survived only because
the model happened to select an accepted profile for those two.

### D2 — Gate evaluation is advisory on the direct path

The composite path blocks on a failed gate (line 268). The direct path
computes `direct_evaluation`, writes it to provenance, and **never checks
it** — zero occurrences of a blocking condition. Five gate-failed cases
were served regardless. This contradicts §15 and my own Phase 7B report,
which claimed failed gates block results.

### D3 — "Stronger compatible" accepts unsatisfiable profiles

A single-source query where the model selected
`MULTI_SOURCE_FILTERED_DISTINCT_COUNT` is accepted as "stronger
compatible", requiring `STABLE_JOIN_KEYS` and `SET_COMPOSITION_COMPLETE` —
gates a direct result can never satisfy. Hit
`accused_distinct_through_relationship` and
`active_persons_via_belongs_to_case`. This is the §3 hazard occurring for
real, and it is only invisible today because of D2.

### D4 — `COVERAGE_ACCEPTABLE` disagrees with the executor

Three cases carry `coverage_verdict = INSUFFICIENT` and the executor
**serves them anyway** (Phase 6 and Phase 7C alike). My gate fails them.
Either the gate is wrong or the executor is — they cannot both be right,
and today the disagreement is silent because of D2.

**None of these are capability gaps.** All four are defects in gate-layer
code I wrote in Phase 7B/7C.

---

## 6. Multi-path effectiveness

| Routing | Count |
|---|---|
| Direct | 27 |
| Composite | 12 |
| Composite successful | 9 |

The three composite "failures" are correct refusals: an inverted range, an
unsupported temporal field (`Person.age`), and a population that never
reaches Case — each naming its unapplied constraint.

**Constraint completeness: 0 cases served a value with an unapplied
constraint.** The core Phase 7 invariant held across the whole corpus.

---

## 7. Structured route

| | Count |
|---|---|
| SUCCESS | 23 |
| REFUSED | 19 |
| **Correct** | **23** |
| **Wrong** | **0** |

Every served structured answer was correct. The regression is entirely in
*refusing* answers it previously served, never in serving wrong ones.

Refusal codes include the three defective `incompatible_gate_profile`
refusals alongside 16 legitimate ones.

---

## 8. AGE route

| Outcome | Count |
|---|---|
| SUCCESS | 21 |
| REFUSED | 20 |
| EXECUTION_ERROR | 1 |
| Numeric results | 21 |
| Correct | 11 |
| Wrong | 3 |

| Refusal code | Count |
|---|---|
| `model_declined` | 17 |
| `scope_denied` | 2 |
| `generation_unparseable` | 1 |
| `plan_invalid` | 1 |

Coverage improved (11 → 21 numeric results vs Phase 6's 22, correct 11 vs
13 — broadly stable). Zero `plan_unparseable`, zero `compile_failed`, zero
`compiler_defect`.

---

## 9. Semantic route

42/42 SUCCESS, 0 empty, 0 errors, all `result_shape = evidence`, 485
numeric mentions recorded for diagnosis and **0 promoted into an aggregate**.

*An earlier probe of mine reported "42 leaks" — that check was wrong. It
tested for an `interpretation` field, which `EvidenceResult` legitimately
carries. The correct test (a `NumericResult` on the semantic route) finds
zero.*

---

## 10. Semantic normalization

| Case | Phase 6 | Phase 7C |
|---|---|---|
| `count_distinct_active_persons` (208) | SEMANTICALLY_DIFFERENT | **AGREEMENT** |
| `weapons_unlicensed_property_filter` (30) | SEMANTICALLY_DIFFERENT | SEMANTICALLY_DIFFERENT |
| `malkhana_records` (45) | SEMANTICALLY_DIFFERENT | SEMANTICALLY_DIFFERENT |
| `time_window_2024` (13) | SEMANTICALLY_DIFFERENT | **CONFLICT** |

The 208 case canonicalised correctly (no traversal, `Person.entity_id`
unique on all 430). `malkhana_records` stays different on **grain** (RECORD
vs ENTITY) — correct. `time_window_2024` became a CONFLICT because AGE now
returns 0 against structured 13 — a genuine disagreement surfaced, not a
normalization failure.

---

## 11. Conflicts (all 3 inspected)

| Case | Structured | AGE | Truth | Assessment |
|---|---|---|---|---|
| `accused_distinct_through_relationship` | 92 | 208 | 92 | genuine — AGE counted without role filter |
| `time_window_2024` | 13 | 0 | 13 | genuine — AGE's date predicate matched nothing |
| `time_window_2026` | 51 | **73** | 51 | genuine — AGE dropped the date restriction |

No conflict was averaged; no value was served. `time_window_2026` is the
original Phase 6 failure, now *caught* rather than served.

---

## 12. Security

| Property | Result |
|---|---|
| Raw LLM Cypher reached production | **NO** |
| AGE execution targets | `muhafiz.evidence_graph` only |
| `plan_unparseable` / `compile_failed` / `compiler_defect` | 0 / 0 / 0 |
| Production after run | unchanged |

---

## 13. Production integrity

| Object | Before | After |
|---|---|---|
| Case | 73 | **73** |
| Person | 430 | **430** |
| SAME_AS | 4702 | **4702** |
| vertices | 4942 | **4942** |
| edges | 13467 | **13467** |
| documents | 1012 | **1012** |

---

## 14. Performance

| Metric | Value |
|---|---|
| Total runtime | **25.1 min** |
| Mean per case | 35.9 s |
| AGE generation | median 41.2 s, max 91.6 s |
| Retry proxy (>25 s) | **16 of 21** |
| AGE execution | mean 93 ms |
| Structured execution | mean 37 ms, max 135 ms |

Generation dominates by ~440×. The retry rate is high and rose with the
enlarged prompt (schema card + gate catalogue).

---

## 15. Regression tests

**442 passed, 10 skipped** — unchanged from baseline. No new tests were
added in this validation phase; the four defects above are *not* yet
covered by tests, which is itself a gap.

---

## 16. Remaining capability gaps

Unchanged and out of scope here: AGE ratio/percentage, threshold,
comparison, median, and graph-unavailable semantics.

---

## 17. Supervisor decisions required

**A. `VALUE_AGGREGATE` profile.** Value measures (min/max/avg/sum — 6
corpus cases) have no profile and fall through to a counting shape. Adding
one is a **new gate/profile category**, which the escalation rule reserves
for supervisor approval. The alternative — narrowing `_shape_incompatible()`
so a non-grouped/non-ratio query tolerates a mismatched selection — is a
policy adjustment within the approved design and needs no new category.
I did not choose between them.

**B. `COVERAGE_ACCEPTABLE` vs executor behaviour (D4).** The executor
serves INSUFFICIENT-coverage results; my gate fails them. Deciding which is
authoritative changes what the system will serve, so it is not mine to
settle.

---

## 18. Recommended next step

**Fix the four defects before any further validation**, in this order:

1. **D2 first** — make the direct path block on failed gates. Until it
   does, D3 and D4 are invisible and the gate layer provides no actual
   guarantee.
2. **D1** — per decision A.
3. **D3** — reject "stronger" profiles whose gates the chosen execution
   path cannot satisfy, or downgrade to the required profile.
4. **D4** — per decision B.

Add regression tests for each, then re-run the corpus. Do not treat the
current numbers as a baseline for the gate layer; they are a baseline for
multi-path composition only.
