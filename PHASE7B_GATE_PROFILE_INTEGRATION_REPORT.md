# Phase 7B — Gate Profiles + Live Multi-Path Integration

**Status: complete.** A trusted gate catalogue and closed profile registry
are implemented, rendered into the planner prompt from the same registry the
runtime enforces, and `execute_composite()` is wired into
`route_structured`. Scalar confidence weighting remains deliberately
unimplemented.

Branch `feat/aggregate-engine-phase0` · HEAD `7fdd54c` · production
unchanged.

---

## 1. Executive summary

The model may now select one approved validation profile per question. It
cannot define gates, weights or thresholds, and it cannot weaken
enforcement: trusted code derives the **minimum required profile from the
typed spec before the model's choice is read**, so a weak selection is
escalated rather than honoured.

Demonstrated end to end on live data: a multi-source question
(`Person.age ≥ 20` + `BELONGS_TO_CASE` + `incident_date` in 2024) returns
**1**, matching independently computed ground truth — and returns the same
**1** under a deliberately weak `SIMPLE_COUNT` selection, with the
escalation recorded in provenance.

Regression **374 → 442 passed**, 10 skipped.

---

## 2. Compatibility note — what was reused, not rebuilt

| Existing abstraction | Location | Reused for |
|---|---|---|
| `CoverageVerdict` (`COMPLETE/CAVEAT/INSUFFICIENT`) | `coverage.py:66` | `COVERAGE_ACCEPTABLE` reads it directly |
| `ValidationIssue(code, message, field)` | `validator.py:92` | every gate/profile refusal and escalation |
| `receipt.filters_applied` / `filters_dropped` | `receipt.py:101` | `ALL_REQUESTED_CONSTRAINTS_APPLIED` |
| `receipt.injected_predicates`, `excluded_merged_n`, `excluded_superseded_n` | `receipt.py` | `VERSION_RULES_APPLIED` |
| `CompositeResult.applied/unapplied_constraints` | `composite.py` | constraint-completeness gates |
| `temporal.resolve_window()` | `temporal.py:141` | Postgres temporal authority |
| `route_age` prompt seam (`call_llm_json`) | `route_age.py:307` | catalogue injection |

**`CAVEAT` passes.** Nothing in `executor.py` refuses on it today — CAVEAT
results are served with `caveat_text()`. `COVERAGE_ACCEPTABLE` therefore
fails **only** on `INSUFFICIENT`; making it fail on CAVEAT would silently
tighten established behaviour.

**Executor invariants are stable identifiers** (`all_group_keys_null`,
`duplicate_group_keys`, `grouping_collapsed`, `null_result`,
`negative_count`, `numerator_le_denominator`, …) — safe to assert on. But
they record grouping and range checks, **never version resolution**, so
`VERSION_RULES_APPLIED` reads `injected_predicates` and the `excluded_*`
counters instead. Asserting on `invariants_checked` would have passed
vacuously; a test pins that distinction.

**No existing `profile` concept** (0 hits repo-wide). `Eligibility`,
`GuardReport` and `ConsensusPolicyRequired` exist but mean different things,
so `GateProfile` introduces no clash.

---

## 3. Gate catalogue (14 gates)

Each is deterministically measurable from recorded evidence:

| Gate | Evidence read |
|---|---|
| `SOURCE_AUTHORITY_RESOLVED` | `AuthorityDecision.resolved` per constraint |
| `ALL_REQUESTED_CONSTRAINTS_APPLIED` | `unapplied_constraints` / `filters_dropped` |
| `NO_SILENT_BROADENING` | applied vs requested constraint counts |
| `POPULATION_IDENTIFIED` | `population_definition` / `subject_label` |
| `GRAIN_VERIFIED` | `receipt.grain` / `composite.grain` |
| `DISTINCT_KEY_VERIFIED` | `receipt.distinct_key` / `final_population.key` |
| `VERSION_RULES_APPLIED` | `injected_predicates`, `excluded_merged/superseded_n` |
| `STABLE_JOIN_KEYS` | `Population.key` across subplans |
| `SET_COMPOSITION_COMPLETE` | composition steps vs population count |
| `GROUPING_FIELD_VALID` | `receipt.group_keys` |
| `DENOMINATOR_VERIFIED` | `denominator_n` / `effective_denominator_n` |
| `EXECUTION_SUCCESS` | execution status |
| `COVERAGE_ACCEPTABLE` | `coverage_verdict` (fails only on INSUFFICIENT) |
| `DATA_COMPLETENESS_KNOWN` | coverage absent/observed counts |

No subjective gate exists, and a test asserts none can be added.

---

## 4. Gate profiles (6, closed)

| Profile | Rank | Gates | Shape |
|---|---|---|---|
| `SIMPLE_COUNT` | 0 | 5 | one population, no filters |
| `FILTERED_COUNT` | 1 | 8 | filters from one source |
| `GROUPED_AGGREGATE` | 2 | 9 | one grouping dimension |
| `MULTI_SOURCE_FILTERED_COUNT` | 3 | 10 | constraints span sources |
| `RATIO_OR_PERCENTAGE` | 3 | 9 | numerator/denominator |
| `MULTI_SOURCE_FILTERED_DISTINCT_COUNT` | 4 | 12 | + distinct key + version rules |

---

## 5. Prompt integration — one registry, no drift

`gates.render_catalogue_for_prompt()` generates the catalogue from
`CATALOGUE` itself and is injected at `route_age.run()` between the schema
card and the question. 1,037 chars — compact beside the ~9.4k schema card.
A test asserts every profile id appears in the rendered text and that the
renderer reads `CATALOGUE`, so prompt and enforcement cannot diverge.

---

## 6. Selection validation — the approved policy

| Case | Behaviour | Verified |
|---|---|---|
| exact/compatible match | accept | ✓ |
| valid but **under-powered** | **escalate** to required, record `gate_profile_escalated` | ✓ |
| stronger but compatible | accept, never downgraded | ✓ |
| unknown id | **REFUSE** (`unknown_gate_profile`) | ✓ |
| incompatible **shape** | **REFUSE** (`incompatible_gate_profile`) | ✓ |

Shape incompatibility is checked **before** rank, because
`RATIO_OR_PERCENTAGE` and `MULTI_SOURCE_FILTERED_COUNT` share rank 3 — a
proportion and a count are different computations, and escalating between
them would change what the answer *means*.

**Invariant:** `effective_profile ≥ required_profile_for(typed_query)`,
asserted across every profile × every spec shape.

---

## 7. Requirement extraction — typed spec only

`requirements_from_spec()` reads `population.predicates`, `traversals`,
`time_window`, `group_by`, `ratio`, `measure`, `grain`, `scope`, and
resolves source multiplicity through the authority registry. It reads no
model output, asserted by test.

**Defect found and fixed during implementation.** The first version set
`requires_distinct_entity = (measure == "count_distinct" or grain ==
"ENTITY")`. Nearly every spec is ENTITY-grained, so this fired universally
— a plain windowed count derived `MULTI_SOURCE_FILTERED_DISTINCT_COUNT` and
dragged in `DISTINCT_KEY_VERIFIED` and `VERSION_RULES_APPLIED` gates it
does not need. That is the "stronger gates wrongly refusing valid queries"
hazard. Now: explicit `count_distinct`, **or** ENTITY grain with a
traversal that can multiply rows (the 73→449 case).

---

## 8. Composite route integration

`route_structured.run()` chooses the path from trusted authority logic, not
model preference:

```
requires_multiple_sources?  ── no ──► existing trusted direct executor
         │ yes
         ▼
_composite_plan_for() ──► execute_composite() ──► gate evaluation
```

Measured routing decisions:

| Spec | Path |
|---|---|
| simple count | direct |
| single-source property filter | direct |
| grouped | direct |
| time window | **composite** |
| age + traversal + window | **composite** |
| ratio + window | direct *(composite has no ratio emitter)* |

Ratio falling back to the route that supports it is correct — the fallback
is to a **stronger** capability, never a broader query.

---

## 9. No silent broadening

Verified live: a window on `report_datetime` (no executable temporal
authority) refuses with `constraint_not_applied` naming the constraint, and
five gates fail. The composite failure branch returns **before** reaching
`execute(`, asserted by a source-level test — a failed composite can never
re-run the question as an unrestricted single-source query.

---

## 10. Gates vs confidence

Gates are binary eligibility; confidence evidence describes quality
afterwards. A failed required gate **blocks** the result — it is never
downgraded to a caveat or offset by confidence. Tests assert
`gate_evaluator` never imports `ResultConfidenceEvidence` or
`combine_weights`, that every check is a `bool`, and that `GateEvaluation`
has no `score`/`confidence`/`weight` field.

**Scalar confidence weighting remains unimplemented.**
`confidence.combine_weights()` and `consensus.combine()` still raise
`ConsensusPolicyRequired`. No weights were invented; equal weighting is
itself a policy and was not adopted as a default.

---

## 11. Security

| Property | Result |
|---|---|
| Raw LLM Cypher reaching production | **NO** |
| `AgeQueryPlan` forbidden fields | none present |
| Policy-invention fields rejected | `custom_gates`, `gate_expression`, `gate_code`, `custom_weights`, `custom_thresholds`, `gates`, `gate_definition`, `confidence_weight` |
| Model's only policy field | `gate_profile_id` (validated against catalogue) |
| Production graph constant | `evidence_graph` |

---

## 12. Live test packaging

The six Phase 7 composite tests remain **skipped**. `conftest.py` rewrites
`DATABASE_URL` *and* wraps `asyncpg.create_pool` to refuse any DSN naming
`muhafiz`. I did not disable that guard.

**The Phase 5C evaluator was assessed and rejected on technical grounds:**
its `public` schema contains only `age_eval_snapshot` — **no `cases`
table**, so no Postgres temporal authority. The composite tests exist
precisely to prove Postgres+graph composition, so the evaluator cannot
exercise the intended code path. Production has 73 cases / 64 dated.

Behaviour is verified regardless: every assertion was executed against the
live database via standalone scripts this phase (73 direct, 13 windowed, 1
multi-constraint, refusals as expected). Only the pytest packaging is
unverified. **SUPERVISOR DECISION REQUIRED** (§15).

---

## 13. Secret redaction

A `muhafiz_app` password appeared in a Phase 7 traceback. No credential was
written into any Phase 7 or 7B source/test file (verified by scan), `.env`
remains git-ignored, and no new test prints a credential-bearing URL — the
live fixture now skips before constructing any DSN. **Credential rotation
remains advisable.**

---

## 14. Tests

```bash
pytest tests/test_aggregate_gate_profiles.py     # 68 passed
pytest <full aggregate suite>                    # 442 passed, 10 skipped
```

Baseline 374/10 → **442/10** (+68).

| Area | Tests |
|---|---|
| Catalogue integrity | 9 |
| Selection policy (all five outcomes) | 15 |
| Requirement derivation | 6 |
| Gate evaluation | 10 |
| Gates ≠ confidence | 3 |
| Model cannot invent policy | 11 |
| Composite routing | 7 |
| No silent fallback | 1 |

### Implementation defects found and fixed

1. **`requires_distinct_entity` fired universally** (§7).
2. **`routes.refusal()` takes no `execution_metadata`** — a TypeError in my
   new composite-refusal path; timing now rides in provenance.
3. **`DISTINCT_KEY_VERIFIED` refused valid composite results.** It read
   `composite.subject_key`, a field `CompositeResult` does not have, so a
   correct multi-source distinct count was blocked by its own gate. Now
   reads `final_population.key` — the key the composition actually joined
   on. This was the §3 hazard occurring for real.

Two of my own test assertions were also too literal (scanning docstrings
rather than executable lines) and were narrowed, the same correction made
in Phase 6.

---

## 15. Production integrity

| Object | Before | After |
|---|---|---|
| `Case` | 73 | **73** |
| `Person` | 430 | **430** |
| `SAME_AS` | 4702 | **4702** |
| vertices | 4942 | **4942** |
| edges | 13467 | **13467** |
| documents | 1012 | **1012** |

---

## 16. Files changed

**New:** `gates.py` (catalogue, profiles, requirements, selection policy),
`gate_evaluator.py` (per-gate measurement),
`tests/test_aggregate_gate_profiles.py` (68 tests).

**Modified:** `route_structured.py` (composite routing + gate evaluation +
provenance), `route_age.py` (catalogue injection, `gate_profile_id` in the
output contract), `age_plan.py` (`gate_profile_id` field, 8 new forbidden
fields).

---

## 17. Remaining blockers and gaps

- Composite metrics remain COUNT / COUNT_DISTINCT; ratios, groupings and
  value measures stay on the direct route.
- AGE ratio/percentage unsupported (unchanged, per approved decision).
- Six live composite tests skipped pending §12.
- Scalar confidence weighting unimplemented by design.

---

## 18. Architectural decisions requiring supervisor approval

**A. Live composite test packaging.** The evaluator DB cannot host these
tests (no `cases` table). Options: a narrowly scoped opt-out of the conftest
guard for `requires_postgres` read-only tests, or seed a disposable copy
with a `cases` table. I did not choose — the guard is the last line of
defence between the suite and production.

*(The gate-profile escalation policy previously flagged has since been
approved and is implemented as specified.)*

---

## 19. Next step

Fresh 42-case corpus validation through the integrated gate-profile and
composite route, measuring profile-selection quality, live multi-path
coverage, time-window improvements and reconciliation changes. **Not run
here**, per the stop condition.
