# QA Failure Analysis — Aggregate Query Pipeline

Observed failures only, with the layer each is attributed to and the
evidence for that attribution. No redesign proposals, no fixes. Evidence
was gathered from the recorded run artefacts and from read-only inspection
of the live registry and graph.

Attribution vocabulary:

| Layer | Component |
|---|---|
| L1 | NL → `AggregateSpec` generation (`nl_spec.py`, prompt, schema card) |
| L2 | Deterministic validation (`validator.py`) |
| L3 | Verification policy (`verification_policy.py`) |
| L4 | Structured execution (`executor.py`, `compiler.py`, `composite_executor.py`) |
| L5 | Reconciliation (`reconcile.py`) |

---

## F-1 — Silent question substitution (5 queries, highest severity)

**Observed.** The generated spec drops a semantically load-bearing part of
the question. The system answers a different question and reports
`coverage: COMPLETE` with no warning.

| Query | Asked | Answered | Confidence markers |
|---|---|---|---|
| QA-008 r1 | distinct CNICs / persons with one | 208 = all distinct persons | FULL, COMPLETE, no warnings |
| QA-013 | persons **accused** in incidents | 206 = all persons in any incident | FULL, AGREEMENT, `independently_verified: True` |
| QA-015 r2 | **accused** persons **under investigation not arrested** | 55 = persons in any StructuredRecord | FULL, COMPLETE, no warnings |
| QA-022 r2 | persons with **risk score > 80** | 208 = every person | FULL, COMPLETE, no warnings |
| QA-024 r2–3 | **licensed** weapons | 32 = all weapons | COMPLETE, no warnings |

**Evidence.** In each case the recorded spec has `predicates: []` (or a
traversal with `role_field: null`) and the executed Cypher carries no
corresponding filter. QA-022 run 2:

```
MATCH (n:Person) WHERE n.merged_into IS NULL
RETURN count(DISTINCT n.entity_id) AS value      -> 208
```

The same question in runs 1 and 3 produced
`predicates: [{field: criminal_risk_score, op: gt, value: 80}]` and was
correctly refused by L2 with
`unknown_field: no field 'criminal_risk_score' is available on Person`.

QA-024 run 1 applied the filter correctly
(`license_status <> "بغیر لائسنس"` → 0, coverage `INSUFFICIENT`), while runs
2–3 emitted `predicates: []` and returned 32. Ground truth on the edge:
`license_status` is `بغیر لائسنس` on 30 weapons and NULL on 2 — there are no
explicitly licensed weapons, so 32 is not a defensible answer under any
reading.

**Suspected layer: L1**, with an architectural amplifier.

L2/L3/L4 each validate the spec that was produced. None of them compares
the spec against the question. When L1 omits the question's substance, the
resulting spec is *simpler*, so fewer triggers fire and every gate passes.
QA-013 shows the end state: two independent routes computed the same
substituted question, agreed, and the answer was labelled
`independently_verified: True`.

QA-022 is the sharpest case because the correct behaviour is proven to
exist — 2 of 3 runs refused the identical question.

---

## F-2 — Schema card omits edge properties (2 queries, one root cause)

**Observed.** Role-qualified questions are either answered without the role
filter (QA-013) or refused with the model stating the schema has no role
information (QA-014).

QA-014 refused 3/3, `spec_generation_failed`, model rationale:

> "The schema does not contain explicit labels or properties to distinguish
> 'witness' or 'victim' roles…"

**Evidence.** The claim is false about the data and true about the card.
The graph does carry roles on the edge:

```
MATCH (p:Person)-[e:INVOLVED_IN]->(i:Incident) RETURN DISTINCT keys(e)
  -> ['role', 'as_of', 'confidence', 'arrest_status', 'source_doc_id']

role distribution: accused 94, complainant 73, witness 37, victim 9
```

The card built by `route_age.build_schema_card` does not mention them:

```
'role' in card:           False
'arrest_status' in card:  False
'accused' in card:        False
'witness' in card:        False
```

It describes the edge only as
`(Person)-[:INVOLVED_IN]->(Incident)  221 edges  FANS forward (up to 3 per Person)`.

`AggregateSpec.Traversal` already has `role_field` / `role_value` fields, so
the role filter is expressible — the planner is simply never told the
values exist.

**Suspected layer: L1 (schema-card construction).** The model's reasoning
was correct given its input. This single omission produces both a false
refusal (QA-014) and a falsely-verified wrong answer (QA-013: 94 accused
existed, 206 returned). QA-015's role/arrest-status component has the same
origin.

---

## F-3 — Traversal direction and relationship selection errors (6 queries)

**Observed.** Answerable multi-hop questions are refused because the spec
describes a path that does not exist.

| Query | Emitted | Registry contains |
|---|---|---|
| QA-007 | `Person -BELONGS_TO_CASE-> Case`, `direction: "in"` | `(Person, BELONGS_TO_CASE, Case)` — outgoing |
| QA-009 | starts at `District`, `FILED_AT -> PoliceStation` | `Case -FILED_AT-> PoliceStation -PART_OF-> District` |
| QA-012 | `Case -BELONGS_TO_CASE-> Case` for the citation hop | `(Case, CITES, Case)` exists |
| QA-015 r1/r3 | `relation_count` on `BELONGS_TO_CASE`, `direction: "in"` | outgoing |
| QA-020 | `FILED_AT` as `"in"` from Case; field `name` on Person | outgoing; field is `canonical_name` |
| QA-025 | `"in"` direction + `caliber_or_bore` on Person + group-by `district.name` | – |

**Evidence that these are planner errors, not schema limits.** QA-023 asked
essentially the same thing as QA-007 and succeeded, using the identical
relationship with `direction: "out"`:

```
QA-023: MATCH (n:Person)-[e0:BELONGS_TO_CASE]->(m0:Case) ... -> 208 (3/3 stable)
QA-007: direction "in"  -> refused 3/3, unknown_relationship
```

QA-010 also used `direction: "in"` **correctly** (Weapon→Case). The model is
inconsistent rather than uniformly wrong, which makes the failures hard to
predict.

For QA-012 the target relationship was available and unused:
`('Case', 'CITES', 'Case')` is in the registry; the planner reused
`BELONGS_TO_CASE` and invented a `Case -BELONGS_TO_CASE-> Case` edge.

**Contributing factor.** The prompt schema in `nl_spec.py` presents
`"direction": "out | in"` with no statement of the convention and no
indication of the direction in which the schema card's triples are written.
`Traversal.direction` defaults to `"out"`; the model explicitly overrode it.

**Suspected layer: L1.** L2 behaved correctly throughout — it refused rather
than guessing, and the refusal messages name the exact problem.

---

## F-4 — Case-link bridge ignores the registry (2 queries)

**Observed.** Every date-filtered Incident question is refused.

```
QA-016 (2026): window_incident_date(incident_date) resolved to 51 case(s)
  but could not be applied to Incident: no declared path from Incident to
  Case, so a case-id restriction cannot be applied to this subject
QA-017 (2024): identical, 13 case(s)
```

**Evidence.** Two paths exist in the registry:

```
('Incident', 'BELONGS_TO_CASE', 'Case')
('Incident', 'PART_OF',         'Case')
```

`composite_executor.plan_link` (`composite_executor.py:392`) resolves the
subject→Case link only from the plan: `plan.subject_case_link`, or a
relationship constraint whose path targets `Case`. It never consults the
registry. QA-016's spec had `predicates: []` and no traversals — only a time
window — so nothing declared a link and the bridge failed.

Separately, `Incident` carries its own `incident_datetime` property, so the
year filter need not have routed through `Case` at all.

**Suspected layer: L4 (`composite_executor.plan_link`).** This is the only
failure in the suite not attributable to L1 — it is deterministic code that
does not use information the registry already holds. QA-017 additionally
dropped the comparison (`compare: null`), reducing "2025 or 2024?" to a
single 2024 window (that part is F-1).

---

## F-5 — Unhandled exception on non-numeric aggregate (QA-018)

**Observed.** QA-018 run 1 crashed. It is the only uncaught exception in
the suite; it escaped route-level error handling and propagated to the
caller.

```
ValueError: invalid literal for int() with base 10: '2024-09-14T22:00:00Z'
  executor.py:273 in _execute_simple
    observed_n = int(value or 0)
```

**Evidence.** The cast is unconditional for any non-grouped spec:

```python
else:
    value = rows[0].get("value") if rows else 0
    observed_n = int(value or 0)
```

The question asked for the earliest incident date — a `min` over a
datetime. `observed_n` is coverage bookkeeping, not the answer: the `min`
query succeeded and returned the correct date, which was then discarded by
the exception.

Runs 2–3 refused earlier at L2 with
`missing_value_field: Measure 'min' needs value_field: which number is
being aggregated?` — the same date-blind assumption expressed one layer up,
since `min` over a date is an ordinary question.

**Suspected layer: L4 (`executor.py`), with a contributing L2 assumption.**

---

## F-6 — Non-determinism (6 queries)

**Observed.** Identical question, identical scope, three runs, materially
different outcomes.

| Query | Run 1 | Run 2 | Run 3 |
|---|---|---|---|
| QA-008 | 208 | 194 | REFUSED |
| QA-015 | REFUSED | 55 | REFUSED |
| QA-018 | **EXCEPTION** | REFUSED | REFUSED |
| QA-019 | 0 | REFUSED | REFUSED |
| QA-022 | REFUSED | **208** | REFUSED |
| QA-024 | 0 | 32 | 32 |

QA-008 diverged three ways in both value and interpretation:

```
r1  count_distinct Person, predicates []                 -> 208
r2  count_distinct Person + cnic exists                  -> 194
r3  count_distinct Person, distinct_key = cnic           -> REFUSED
```

Verification level also varied (FULL / PARTIAL / –), so the assurance story
changes between runs. QA-011 and QA-017 produced stable values with varying
verification levels and route sets.

**Suspected layer: L1.** Spec generation is a sampled LLM call and is the
only non-deterministic component; L2–L5 are deterministic given a spec.

**Consequence.** A user asking the same question twice can receive a
different number, a refusal, or a crash, with nothing indicating the result
is unstable.

---

## F-7 — Compound questions silently half-answered (4 queries)

**Observed.** Two-part questions return a single scalar answering one part.

| Query | Asked | Returned |
|---|---|---|
| QA-008 | distinct CNICs **and** persons with one | one scalar |
| QA-011 | officers assigned **and** cases with an officer | 76 (officers only) |
| QA-014 | witnesses **and** victims | refused |
| QA-025 | accused count **and** districts | refused |

**Evidence.** QA-011's spec is well formed — 76 officers with ≥1
`ASSIGNED_TO` case via a correct `relation_count` predicate — but the
second half is absent from the spec and from the warnings. The only warning
present concerns verification (*"Not independently verified…"*), not the
dropped sub-question.

`AggregateSpec` carries one `measure` and one `population`, so a two-part
question is not representable.

**Suspected layer: L1 (no signal emitted) over a structural limit in the
spec model.** The failure is not that it cannot answer both — it is that
nothing tells the caller only one was answered.

---

## F-8 — Counting distinct values of a non-identity field is unexpressible

**Observed.** QA-008 run 3 attempted the first half of the question —
distinct CNIC *values* — and was refused:

```
wrong_distinct_key: distinct_key 'cnic' does not identify a Person;
the registry measured 'entity_id' as its key.
```

**Evidence.** `distinct_key` is constrained to the registry's measured
identity key for the label. `cnic` is present on 196 of 430 Person nodes but
cannot be used as a distinct key, and there is no other field in the spec
for "count distinct values of X". L2's refusal is correct and protective —
using a non-unique field as an identity key would silently undercount — but
the capability gap is real.

**Suspected layer: spec model (`spec.py`) / L2 constraint.** Recorded as a
capability limit rather than a defect.

---

## Summary of attribution

| Finding | Layer | Queries | Deterministic |
|---|---|---|---|
| F-1 silent substitution | L1 (+ architectural blind spot) | QA-008, 013, 015, 022, 024 | partly |
| F-2 schema card omits edge properties | L1 (card construction) | QA-013, 014, (015) | yes |
| F-3 direction / relationship selection | L1 | QA-007, 009, 012, 015, 020, 025 | yes |
| F-4 case-link bridge ignores registry | L4 `plan_link` | QA-016, 017 | yes |
| F-5 int() cast on datetime aggregate | L4 `executor.py:273` | QA-018 | no |
| F-6 non-determinism | L1 | 6 queries | n/a |
| F-7 compound questions | L1 + spec model | QA-008, 011, 014, 025 | yes |
| F-8 distinct-value counting | spec model / L2 | QA-008 | yes |

Two findings are pure code defects independent of the model (F-4, F-5).
Two are schema/prompt-input defects that make the model's output wrong
while its reasoning is sound (F-2, F-3). The rest originate in
interpretation.

L2 (validation) was correct in every case observed: it refused rather than
guessing, and its messages named the precise problem. L5 (reconciliation)
was correct in mechanism; it cannot detect F-1 because both routes receive
the same question and can substitute it the same way.

---

## Scope note

All findings concern `src/pipeline/aggregate/*`, reached via
`orchestrator.answer_question()`. That path is **not** wired to `/api/chat`,
which uses the legacy `xagg.run_aggregate` engine. These findings therefore
describe the new architecture's current state, not live user-facing
behaviour.
