# Phase 8 — Remaining Items

Everything outstanding, split by what it actually is. The distinction
matters: a bug should be fixed, a capability gap should not be fixed
without a decision, and a product decision is not mine to take.

---

## A. Implementation bugs

**None confirmed.**

The four Phase 7C defects (D1 value-aggregate refusal, D2 advisory gates,
D3 profile-family compatibility, D4 coverage blocking) were fixed and
verified in Phase 7D. The current corpus run shows structured **25 correct,
0 wrong**, zero gate failures served, zero silent constraint drops.

### Watch items — not bugs, but worth knowing

**1. AGE route wrong on 3 of 21 numeric results.**

| Case | AGE | Truth | Classification |
|---|---|---|---|
| `accused_distinct_through_relationship` | 208 | 92 | CONFLICT |
| `time_window_2024` | 0 | 13 | CONFLICT |
| `time_window_2026` | 73 | 51 | CONFLICT |

All three were **caught by reconciliation and never served**. These are
independent-planner errors, which is what the independent route exists to
surface — not defects in the safety machinery. `time_window_2026` is the
original Phase 6 failure, now detected rather than served.

**2. Planner omits `gate_profile_id` on 20 of 42 cases.**

Trusted derivation supplies the profile, so nothing is weakened. But
selection coverage is under half, which means the model-selection path is
less exercised than the counts suggest. Prompt-level, not architectural.

**3. High planner retry rate.** 16 of 21 generations exceeded 25s,
implying ≥2 attempts. Cost and latency only; correctness unaffected.

---

## B. Capability gaps — deliberately unsupported

These are **not** to be fixed in this phase.

**CORRECTION (Phase 8 review).** Earlier reports — including this one —
listed "ratio, threshold, comparison, median" together as one block of
capability gaps. That grouping was imprecise and overstated what is
missing. Verified against the code, the four have entirely different
statuses:

| Item | Real status | Actual blocker |
|---|---|---|
| **Ratio / percentage** | **SUPPORTED** on the structured route | none — `executor.py:206` dispatches to `_execute_ratio()`; the corpus computes 5.479% and 95.890% correctly. Only the *AGE planner* lacks it, by approved scope decision |
| **Threshold** | **Effectively supported** under a different name | none technical — `RelationCountPredicate` IS implemented and emitted (`compiler.py:205`), producing the 4-vs-70 grain distinction. The `threshold` field on `AggregateSpec` is a REDUNDANT second spelling that was declared but never wired; the validator refuses it and points at the predicate that works |
| **Median** | **Genuinely unsupported** | **real technical wall** — Apache AGE 1.5.0 has no percentile function, so there is no Cypher to emit. The alternative is fetching all values into Python and sorting, which pulls raw rows out of the database to compute an aggregate and is unbounded in memory. `validator.py:838` refuses explicitly so it surfaces as a stated refusal, not a CompilerError |
| **Comparison** | **Genuinely unsupported** | **real missing feature** — bucketed comparison has no emitter at all. Roughly "compile two populations and present both" |

So: one real technical wall (median), one genuine missing feature
(comparison), one redundant spec field that arguably should be deleted
rather than implemented (threshold), and one that is not a gap at all
(ratio).

### Remaining genuine gaps

| Gap | Corpus cases | Status |
|---|---|---|
| Median | 1 | AGE has no percentile function |
| Comparison (bucketed) | 1 | no emitter |
| AGE-side ratio | 3 | approved structured-only |
| AGE-side `time_window` | 10 | `incident_date` is Postgres-authoritative |
| Composite MIN/MAX/AVG | — | composite executor supports COUNT / COUNT_DISTINCT only |
| Scalar confidence weighting | — | **awaiting policy** |

The AGE route declining these is **correct behaviour**, not failure. Of the
14 `STRUCTURED_CORRECT_AGE_WRONG` verdicts, 12 are approved capability
gaps, 1 a model decline, and only 3 genuine AGE errors.

---

## C. Product decisions

**1. Confidence-weighting policy.** `consensus.evaluate()` reaches
`ELIGIBLE` and stops; `combine()` raises `ConsensusPolicyRequired`. Needs
fixed per-route weights, dimension weights, or minimum thresholds. Equal
weighting is itself a policy and was not adopted as a default.

**2. Live composite integration tests.** Six tests remain skipped. The
Phase 5C evaluator cannot host them — its `public` schema has no `cases`
table, so no Postgres temporal authority. Options: a narrowly scoped
opt-out of the conftest production guard for read-only
`requires_postgres` tests, or seed a disposable copy. I did not weaken the
guard.

**3. Whether AGE should declare `count_distinct`** when it emits
`count(DISTINCT …)`. Two same-number cases remain SEMANTICALLY_DIFFERENT
because the declared metric differs; canonicalization proves equivalence
only where no traversal exists.

**4. Spec generation from natural language.** The structured route still
requires a caller-supplied `AggregateSpec`. Nothing generates one from a
question.

---

## D. Housekeeping

**`.env.phase5.bak`** — a Phase 5A backup holding 52 variables including
credentials, **not git-ignored**. It should be removed or ignored before
QA. I have not deleted it: it is not mine to discard unasked, and it may
still be wanted as a rollback reference.

**Credential rotation** remains advisable — a `muhafiz_app` password
appeared in a Phase 7 test traceback. It was never written to any source
file and `.env` is git-ignored.
