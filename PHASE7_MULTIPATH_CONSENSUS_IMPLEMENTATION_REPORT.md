# Phase 7 — Multi-Path Aggregate Composition + Confidence/Normalization Consensus

**Status: complete.** Multi-path composition, per-constraint authority
routing, deterministic population algebra, provable semantic
canonicalization, typed numeric normalization, and the consensus gate are
implemented. The confidence-weighting policy is deliberately **not**
invented — it is an isolated, testable boundary awaiting approval.

Branch `feat/aggregate-engine-phase0` · HEAD `7fdd54c` · production
unchanged.

---

## 1. Executive summary

Phase 6's worst failure was an AGE plan that could not express a date
restriction, silently dropped it, and answered **73** where the truth was
**51**. Phase 7 removes the conditions that made that possible.

A question's constraints no longer have to belong to one route. Each
constraint resolves to the source that the registry measured as
authoritative — `incident_date` to Postgres, `Person.age` to the graph,
relationships to the graph — each is executed there, and the resulting
populations of **stable ids** are intersected by trusted code. Verified
live: age 20-30 **∩** linked-to-a-case **∩** 2024 incident dates = **1**,
matching independently computed ground truth.

Crucially, a constraint that cannot be applied now **refuses**. There is no
representation for "ignored".

---

## 2. Multi-path architecture

```
question
   ↓
constraints
   ↓  authority.resolve_authority()      ← registry measurement, not a model
   ├── incident_date  → postgres  cases.incident_date   (64/73)
   ├── Person.age     → graph     Person.age            (19/430)
   └── BELONGS_TO_CASE→ graph     relationship
   ↓  execute each at its own source
   ↓  populations of stable ids
   ↓  populations.intersect_all()        ← trusted code, never the LLM
   ↓  aggregate
result
```

The model may state a semantic requirement; it never selects a backend.
Authority comes from `LogicalField`, which Phase 0 already populated with
live measurements and an `authority_basis` explaining each decision.

---

## 3. Composite plan

`composite.py` — `CompositeAggregatePlan`:

| Field | Meaning |
|---|---|
| `subject_label` / `subject_key` | what is measured, and the id that counts one |
| `grain` | ENTITY / RELATIONSHIP / RECORD / … |
| `metric` | COUNT, COUNT_DISTINCT |
| `constraints` | each with `field`, `kind`, `spec`, `key` |
| `combine` | INTERSECT (default), UNION, DIFFERENCE |

`ConstraintKind` is TEMPORAL, PROPERTY, or RELATIONSHIP. Value measures
(sum/avg/min/max) are deliberately absent — they aggregate a property over
a population rather than counting one, and the structured route already
computes them correctly from a single source.

---

## 4. Population operations

`populations.py` implements INTERSECT, UNION and DIFFERENCE over
insertion-ordered, de-duplicated id tuples, each recording a
`CompositionStep` for the receipt.

Two properties matter more than the algebra:

- **Joining different identifiers raises.** A `case_id` × `entity_id`
  intersection would silently return empty — a type error dressed as an
  answer. `PopulationKeyMismatch` makes it loud.
- **An empty population is a real answer.** "No cases in 2025" yields an
  empty set that propagates through the intersection; it is never mistaken
  for "restriction unavailable".

---

## 5. Time-window solution

`incident_date` is resolved by the existing `temporal.resolve_window()`
against `cases.incident_date`, producing a case-id allow-list. Because the
subject is usually a Person, `_subject_ids_for_cases()` translates that
allow-list across the graph edge into subject ids — with the case ids bound
as `$ids`, never interpolated.

Measured, live:

| Constraints | Result |
|---|---|
| linked to a case | 208 |
| + 2024 incident dates | **28** |
| age 20-30 + linked | 9 |
| age 20-30 + linked + 2024 | **1** |

The Phase 6 failure mode — a date restriction quietly disappearing and the
answer broadening to the full population — cannot occur here: an
unresolvable date produces `constraint_not_applied`, not a wider answer.

---

## 6. Constraint completeness

`_refuse_unapplied()` runs before any value is produced. Every constraint
is APPLIED or the plan REFUSES, naming the constraint and the reason.
`CompositeResult` exposes `applied_constraints` and
`unapplied_constraints`; there is no third field, because there is no third
state.

---

## 7. Provenance

`CompositeResult.provenance()` records, per constraint: name, field, kind,
**source**, physical path, authority basis, whether it applied, how many
ids it matched, and any refusal reason — plus every composition step and
the final population size. The computation is reproducible from the receipt
alone.

---

## 8. Confidence evidence

`confidence.ResultConfidenceEvidence` carries only measured facts: source
and authority, observed/expected population and coverage verdict,
constraint completeness with the applied/unapplied lists, grain and
population definition, denominator, execution status, row count, guard
findings, and excluded null/merged/superseded counts.

There is **no dimension for how confident the model sounded.** A test
asserts that no such field exists — prose must not be able to outvote
measurement.

`combinable` is a hard admissibility gate (incomplete constraints or failed
execution ⇒ not combinable). It is not a quality score.

---

## 9. Confidence scalar policy — **SUPERVISOR DECISION REQUIRED**

`confidence.combine_weights()` and `consensus.combine()` raise
`ConsensusPolicyRequired`. No weights are invented.

Collapsing the evidence vector into one number requires choosing fixed
per-route weights, dimension weights, or minimum thresholds. None is
approved for this project. **Unweighted averaging is not a neutral
default** — equal weighting is itself a policy, and adopting it silently
would be exactly the unreviewed decision this boundary exists to prevent.

The gap is loud and unit-tested rather than papered over.

---

## 10. Semantic normalization

`canonical.py` proves — never assumes — that `count` and `count_distinct`
are equal for a given population. Both conditions must hold:

1. the computation traverses **nothing** (one row per node), and
2. the label's distinct key is measured present on every node and unique.

Applied to the four Phase 6 same-number cases:

| Case | Verdict |
|---|---|
| `count_distinct_active_persons` (208) | **canonicalised** — Person.entity_id 430/430, no traversal |
| `weapons_unlicensed_property_filter` (30) | **canonicalised** — Weapon.entity_id 32/32 |
| `malkhana_records` (45) | **not canonicalised** — grain differs (RECORD vs ENTITY) |
| `time_window_2024` (13) | **not canonicalised** — one traversal; and the routes filtered different fields from different authorities (`Incident.report_datetime` vs `cases.incident_date`), agreeing at 13 by coincidence |

Grain and grouping pass through untouched. `reconcile.reconcile()` takes an
optional `snapshot=` to enable this; without it, behaviour is byte-identical
to before, so every existing caller is unaffected.

---

## 11. Numeric normalization

Typed per metric, never one blind formula:

| Metric | Rule |
|---|---|
| percentage | ÷ 100 → 0.64 |
| ratio / proportion | already normalized |
| count / count_distinct | ÷ shared denominator (51/73 → 0.6986) |
| **count with no denominator** | **refused** |
| **avg / sum / min / max** | **refused** — 31.84 must not become 0.31 |

Original value and unit are preserved; the normalized form exists only for
comparison and never becomes the user-facing figure.

---

## 12. Consensus

`consensus.evaluate()` runs three gates **in this order**:

1. **Comparability** (with provable canonicalization) → `NOT_COMPARABLE`
2. **Confidence admissibility** → `NOT_COMBINABLE`
3. **Legitimate common scale** → `NO_COMMON_SCALE`
4. all pass → `ELIGIBLE`, `awaiting_policy=True`

Reversing any pair produces a known failure: normalizing first lets two
different questions be averaged because both are numbers; weighting first
lets a confident answer to the wrong question outvote a correct one.

`ASSIGNED_TO` vs `BELONGS_TO_CASE`, all-cases vs 2026-cases, and raw
records vs distinct persons remain different populations. No scaling or
weighting can override gate 1.

---

## 13. Phase 6 regressions addressed

- **`time_window_2026`** — the class of failure is eliminated for composite
  execution: the date resolves against Postgres authority, and an
  unresolvable date refuses instead of broadening. *(The independent AGE
  planner still cannot express dates; that remains architectural decision
  A from Phase 6.)*
- **Same-number SEMANTICALLY_DIFFERENT** — two of four now reach
  AGREEMENT when a snapshot is supplied; the other two remain different for
  documented, measured reasons (§10).

---

## 14. Security

Unchanged and re-verified:

| Property | Result |
|---|---|
| `route_age.run` uses the compiled executor | **yes** |
| `route_age.run` reaches raw `age_client` | **no** |
| Forbidden fields on `AgeQueryPlan` | **none** |
| Production graph constant | `evidence_graph` |
| Raw LLM Cypher reaching production | **NO** |

The composite executor writes nothing. It emits read-only `MATCH … RETURN`
with bound parameters, re-asserting the identifier character class at every
interpolation point.

---

## 15. Tests

```bash
pytest tests/test_aggregate_multipath.py                 # 44 passed, 6 skipped
pytest <full aggregate suite>                            # 374 passed, 10 skipped
```

Baseline was **330 passed / 4 skipped**; now **374 / 10** (+44 executed).

| Area | Tests |
|---|---|
| Authority resolution | 5 |
| Population algebra | 6 |
| Constraint completeness | 4 |
| Semantic canonicalization | 8 |
| Numeric normalization | 5 |
| Confidence evidence | 5 |
| Consensus gates | 7 |
| Reconciliation canonicalization | 4 |
| Live multi-path (skipped — §17) | 6 |

---

## 16. Production integrity

| Object | Before | After |
|---|---|---|
| `Case` | 73 | **73** |
| `Person` | 430 | **430** |
| `SAME_AS` | 4702 | **4702** |
| vertices | 4942 | **4942** |
| edges | 13467 | **13467** |
| documents | 1012 | **1012** |

---

## 17. Implementation bug found and fixed during this phase

**The composite executor initially omitted the correctness invariants.**
Its first version returned **429** persons for "linked to a case" where the
correct figure is **208**, and **249** for the 2024 population where the
truth is **28** — because it did not inject `merged_into IS NULL` and
`superseded_by IS NULL`. That is precisely the 429-vs-208 defect Phase 0
measured, reappearing in new code that had not inherited the structured
compiler's habit.

Fixed by injecting both from the registry, resolved **per triple** (
`BELONGS_TO_CASE` is versioned only on Person→Case; the eight other triples
are not). All five live figures now match ground truth, and a regression
test asserts the injection sites exist.

---

## 18. Files changed

**New:** `authority.py` (174), `populations.py` (154), `canonical.py` (171),
`composite.py` (201), `composite_executor.py` (510), `confidence.py` (267),
`consensus.py` (222), `tests/test_aggregate_multipath.py` (659).

**Modified:** `reconcile.py` — optional `snapshot=` parameter enabling
provable canonicalization; two helpers for subject/traversal extraction.
No other source file changed.

---

## 19. Remaining capability gaps

- Composite metrics are COUNT / COUNT_DISTINCT only; value measures stay
  with the structured route.
- Ratio/percentage remains unsupported in the AGE planner (unchanged, per
  the approved Phase 6 decision).
- `threshold`, `comparison`, `median` remain out of scope.
- The composite plan is **not yet wired into `route_structured`** — it is a
  complete, tested, independently callable layer. Integrating it into the
  live request path is the next step (§21).

---

## 20. Architectural decisions requiring supervisor approval

### A. Confidence-weighting policy — **SUPERVISOR DECISION REQUIRED**
Evidence dimensions and the gate are implemented; the scalar weighting is
not. Options: fixed per-route weights, dimension weights, or minimum
thresholds. Equal weighting is a policy, not a default.

### B. Live integration tests vs the production guard — **SUPERVISOR DECISION REQUIRED**
Six composite integration tests need the real database. `conftest.py`
rewrites `DATABASE_URL` **and** wraps `asyncpg.create_pool` to refuse any
DSN naming `muhafiz`. I did not disable that guard: it is the last line of
defence between the suite and the production graph, and this project has
already lost production data once when a safety boundary was bypassed for
convenience. The six assertions were verified live via a standalone script
(208 / 28 / 9 / 1 / refused); only their pytest packaging is unverified.
Options: a narrowly scoped opt-out for `requires_postgres` tests, or point
them at the existing Phase 5C evaluator database.

*(Note: a `muhafiz_app` password was printed into this session's test
traceback. It was not written into any Phase 7 file — verified — and `.env`
remains git-ignored. Rotation is advisable.)*

---

## 21. Recommended next step

Wire `execute_composite()` into the structured route so multi-constraint
questions use it in the live request path, then re-run the 42-case corpus
to measure the effect. That run is **not** started here, per the stop
condition.
