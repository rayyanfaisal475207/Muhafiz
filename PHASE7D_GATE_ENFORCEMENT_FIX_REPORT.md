# Phase 7D — Gate Enforcement + Phase 7C Regression Fixes

**Status: complete.** All four Phase 7C defects are fixed and verified on
the full 42-case corpus. The structured route is back to **25 correct, 0
wrong**, every previously-lost case is restored, and no result with a
failed required gate is served.

Branch `feat/aggregate-engine-phase0` · run 26.3 min · production unchanged.

---

## 1. Approved supervisor decisions, as implemented

**Value aggregates — no new profile.** `MIN/MAX/AVG/SUM` are calculation
semantics inside the typed spec; profiles describe validation *shape*. The
six existing profiles are reused; no `VALUE_AGGREGATE` was added.

**Coverage — serve with a warning.** INSUFFICIENT results are served
carrying `INSUFFICIENT_DATA_COVERAGE`, and the verdict is preserved in
provenance rather than softened.

---

## 2. D1 — value aggregates wrongly refused

**Root cause.** `min/max/avg/sum` correctly derive `SIMPLE_COUNT`, but the
live planner labels them `GROUPED_AGGREGATE`, and `_shape_incompatible()`
refused any mismatched label. My first attempt changed
`required_profile_for()` — which was already returning the right answer —
and left the code that actually blocked untouched.

**I then defended that as correct policy.** It was not: §2 lists D1 as a
defect and §4 says supported ungrouped value aggregates should use the
appropriate existing single-source profile. Refusing a valid query over a
cosmetic mislabel *is* the defect.

**Fix.** A mislabelled selection is **corrected** to the profile derived
from the typed spec. Restored: `min_person_age` **24**, `max_person_age`
**49**.

---

## 3. D2 — direct-path gates were advisory

**Root cause.** The composite path blocked on a failed gate; the direct
path computed the evaluation, wrote it to provenance, and never checked it.
Five Phase 7C cases were served with failed gates.

**Fix.** The direct path now refuses with `gate_failed`, mirroring the
composite path. The receipt (and its value) stay available for debugging
but are never served as an accepted answer.

**Verified:** 17 gate-failed cases, **17 blocked, 0 served**.

---

## 4. D3 — profile compatibility is not a strength order

**Root cause, in two stages.** More gates is not "stronger" — a
single-source result can never satisfy `STABLE_JOIN_KEYS` or
`SET_COMPOSITION_COMPLETE`. My first fix made shape mismatch symmetric and
**refused** those selections, which caused a *new* regression: it killed
`accused_distinct_through_relationship` (92) and
`active_persons_via_belongs_to_case` (208), both of which Phase 7C had
served correctly. I traded two over-strict gate failures for two lost
answers — strictly worse. I aborted that run at 14/42 rather than spend 25
minutes measuring code I knew was broken.

**Fix.** Two distinct outcomes:

| Condition | Outcome |
|---|---|
| Ratio-vs-count mismatch | **REFUSE** — substituting changes what the answer *means* |
| Grouped-on-flat, multi-source-on-single-source | **CORRECT** to the derived requirement |

Correction cannot weaken enforcement: the replacement *is*
`required_profile_for(typed_spec)`, computed before the model's choice is
read. A legitimately stronger choice in the same family is still accepted
untouched.

**Restored:** 92 and 208.

---

## 5. D4 — coverage blocked servable results

**Root cause.** `COVERAGE_ACCEPTABLE` was a required gate while the
executor was allowed to serve INSUFFICIENT results. A required gate blocks
by definition; both rules cannot hold for one check. Phase 7C measured the
contradiction — three cases recorded `COVERAGE_ACCEPTABLE: failed` and were
served anyway.

**Fix.** Removed from every profile's required set, retained as an
advisory signal. Served results carry:

```
INSUFFICIENT_DATA_COVERAGE: This result is based on incomplete data
coverage and may not represent the full relevant population.
```

**Verified:** 5 cases carry the warning — `min_person_age`,
`max_person_age`, `avg_person_age`, `sum_person_age`,
`threshold_cases_with_more_than_one_weapon`.

---

## 6. Catalogue and compatibility changes

| Change | Effect |
|---|---|
| `COVERAGE_ACCEPTABLE` removed from `_UNIVERSAL` | 4–11 required gates per profile (was 5–12) |
| `_shape_incompatible()` narrowed | ratio incoherence only |
| `_shape_mismatched()` added | grouped/multi-source mislabels → correction |
| `SelectionOutcome.corrected` added | distinct from `escalated` |
| `profile_corrected` in provenance | §21 auditability |
| Harness `corrected` counter | checked before `escalated` |

No new gate and no new profile were added.

---

## 7. Corpus results — Phase 7C → 7D

### Reconciliation

| Classification | 7C | 7D | Δ |
|---|---|---|---|
| AGREEMENT | 6 | **8** | **+2** |
| SINGLE_ROUTE_VALID | 20 | 18 | −2 |
| SEMANTICALLY_DIFFERENT | 2 | 2 | 0 |
| CONFLICT | 3 | 3 | 0 |
| REFUSED | 11 | 11 | 0 |

### Evaluation

| Verdict | 7C | 7D | Δ |
|---|---|---|---|
| ALL_AGREE | 8 | **10** | **+2** |
| STRUCTURED_AND_AGE_AGREE | 1 | 1 | 0 |
| STRUCTURED_CORRECT_AGE_WRONG | 14 | 14 | 0 |
| **AGE_CORRECT_STRUCTURED_WRONG** | 2 | **0** | **−2** |
| CORRECT_REFUSAL | 17 | 17 | 0 |
| INCORRECT_REFUSAL | 0 | 0 | 0 |

`AGE_CORRECT_STRUCTURED_WRONG` returning to zero is the regression signal
clearing.

### Gate profiles

| Outcome | Count |
|---|---|
| Exact | 7 |
| Escalated | 4 |
| **Corrected** | **6** |
| Stronger compatible | 5 |
| Unknown | **0** |
| Incompatible | **0** |
| No selection | 20 |
| Gate failures (all blocked) | 17 |

### Structured route

**25 SUCCESS / 17 REFUSED — 25 correct, 0 wrong.** Matches the Phase 6
baseline exactly.

### Routing

Direct 30 · composite 12 · composite successful 9 (the three "failures"
are correct refusals naming their unapplied constraint).

---

## 8. §19 verifications

| Check | Result |
|---|---|
| `min_person_age` | **24** ✓ |
| `max_person_age` | **49** ✓ |
| `accused_distinct_through_relationship` | **92** ✓ |
| `active_persons_via_belongs_to_case` | **208** ✓ |
| `time_window_2026` | **51** ✓ |
| Required-gate failures served | **0** ✓ |
| Single-source results rejected by composition gates | **0** ✓ |
| INSUFFICIENT coverage carries warning | **5 cases** ✓ |
| Silent constraint drops | **0** ✓ |

---

## 9. AGE route and semantic route

| AGE | Count |
|---|---|
| SUCCESS / REFUSED / ERROR | 21 / 20 / 1 |
| Numeric results | 21 |
| Correct | 11 |
| Wrong | 3 |

The 3 wrong: `accused_distinct_through_relationship` (208 vs 92),
`time_window_2024` (0 vs 13), `time_window_2026` (73 vs 51) — all caught as
CONFLICT, none served.

Of the 14 `STRUCTURED_CORRECT_AGE_WRONG`: **12 approved capability gaps**,
1 model decline, 3 genuine AGE errors.

Semantic: **42/42 SUCCESS**, all `result_shape=evidence`, zero numeric
leaks.

---

## 10. Security and production

| Property | Result |
|---|---|
| Raw LLM Cypher reached production | **NO** |
| AGE execution targets | `muhafiz.evidence_graph` only |
| `plan_unparseable` / `compiler_defect` | 0 / 0 |

| Object | Before | After |
|---|---|---|
| Case / Person / SAME_AS | 73 / 430 / 4702 | **73 / 430 / 4702** |
| vertices / edges / documents | 4942 / 13467 / 1012 | **4942 / 13467 / 1012** |

---

## 11. Tests

| Suite | Result |
|---|---|
| Focused D1–D4 (`test_aggregate_gate_enforcement.py`) | **32 passed** |
| Full aggregate suite | **478 passed, 10 skipped** (from 442/10, +36) |

Three superseded tests were **rewritten, not deleted**, each carrying a note
naming the cases that were lost:
`test_insufficient_coverage_fails` → `..._is_not_a_required_gate_failure`;
`test_multi_source_profile_..._is_incompatible` → `..._is_corrected`;
`test_5_grouped_profile_on_a_flat_query_is_refused` → `..._is_corrected`.

---

## 12. Observation worth recording

One structured refusal code appeared that was absent in 7C:
`compile_failed` ×1. Structured correctness is unaffected (25/25), so this
is noted rather than treated as a defect — but it should be identified
before the next validation phase.

---

## 13. Remaining capability gaps

Unchanged: AGE ratio/percentage, threshold, comparison, median, composite
MIN/MAX/AVG, scalar confidence weighting. All deliberately out of scope.

---

## 14. Supervisor decisions required

**None new.** Both Phase 7C decisions were resolved by this brief and are
implemented as specified.

---

## 15. Recommended next step

Identify the single `compile_failed` case (§12), then proceed to the
confidence-weighting policy decision, which remains the last unimplemented
layer of the consensus design.
