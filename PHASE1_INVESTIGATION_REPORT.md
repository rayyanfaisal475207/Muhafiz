# Phase 1 — Investigation Report (pre-implementation)

Root-cause investigation for the seven Phase 1 issues. No code has been
modified. Every claim below was verified against the source or the live
registry/graph; the evidence is quoted inline.

Architecture layers referenced: **L1** NL→AggregateSpec, **L2** validation,
**L3** verification policy, **L4** structured execution, **L5**
reconciliation.

---

## Cross-cutting finding (read first)

Three of the headline issues share one cause, and fixing them in the wrong
order wastes effort:

```
P0.3 schema card omits edge properties
        │
        ├──► P0.1 silent substitution of role-qualified questions (QA-013, QA-015)
        └──► false refusals for role questions (QA-014)
```

Evidence that the prompt is *not* the weak link: `_SYSTEM` rule 1 already
uses the exact failing example —

> `"How many people were accused" is ENTITY with count_distinct.`

— and QA-013 still dropped the role filter. The model was told what to do
and could not do it, because `role` never appeared on the schema card:

```
'role' in card:           False
'arrest_status' in card:  False
```

while the graph holds `INVOLVED_IN.role` = accused 94, complainant 73,
witness 37, victim 9.

**Consequence for sequencing:** P0.3 must land before P0.1 is judged.
Adding a semantic-alignment check first would convert silent wrong answers
into refusals — safer, but it would mask the fact that the questions are
answerable. P0.3 first, then P0.1 catches what remains.

---

## P0.1 — Silent question substitution

**Severity:** CRITICAL
**Affected:** QA-008, QA-013, QA-015, QA-022, QA-024

### Current behaviour
The generated spec omits a load-bearing constraint. The system answers a
different, simpler question and reports `coverage: COMPLETE`, verification
`FULL`, and no warnings.

| Query | Asked | Answered | Markers |
|---|---|---|---|
| QA-013 | persons **accused** in incidents | 206 = all persons in any incident | FULL, AGREEMENT, `independently_verified: True` |
| QA-022 r2 | risk score **> 80** | 208 = every person | FULL, COMPLETE, no warnings |
| QA-015 r2 | **accused**, **not arrested** | 55 = persons in any StructuredRecord | FULL, COMPLETE, no warnings |
| QA-024 r2–3 | **licensed** weapons | 32 = all weapons | COMPLETE, no warnings |
| QA-008 r1 | distinct CNICs | 208 = all distinct persons | FULL, COMPLETE, no warnings |

In every case the recorded spec has `predicates: []` (or `role_field: null`)
and the executed Cypher carries no corresponding filter.

### Root cause
Two independent contributors:

1. **Missing capability in the card (P0.3)** for QA-013/QA-015 — the filter
   was not expressible from what the model was shown.
2. **No fidelity check anywhere in the pipeline.** L2 validates the spec
   against the *registry*; nothing validates the spec against the
   *question*. A spec that drops a constraint is structurally valid and
   *simpler*, so it passes every gate and fires fewer L3 triggers.

QA-022 isolates contributor 2 cleanly: runs 1 and 3 emitted the predicate
and were correctly refused (`unknown_field: criminal_risk_score`); run 2
omitted it and sailed through. The correct behaviour exists and is not
reliably reached.

### Affected layer
**L1** produces the defect; **L2/L3** lack the check that would catch it.
This is an architectural blind spot, not a bug in any one function.

### Proposed solution
Add a deterministic **spec-fidelity check** as a new L2 concern, operating
on evidence L1 already has, without keyword rules:

1. **Require L1 to declare its own constraint inventory.** Extend the JSON
   contract with `question_constraints`: a list of the constraints the model
   believes the question asks for, each tagged `applied: true|false` with a
   reason when false. This is self-report, so it is corroborating evidence,
   not proof — but a model that drops a filter usually *knows* it dropped
   it, and QA-014's rationale shows it already volunteers this.
2. **Deterministically detect unexpressed constraints.** Compare the
   declared inventory against what the spec actually contains
   (`predicates`, `traversal.role_field`, `time_window`, `threshold`). Any
   declared-but-absent constraint becomes a `ValidationIssue`.
3. **Make the outcome a refusal, not a silent answer.** An unapplied
   constraint is `UNSUPPORTED` with a message naming what was dropped —
   the same treatment `unknown_field` already gets.

No keyword matching, no question-specific rules; the mechanism is generic
over any constraint the model can name.

### Risks
- **Self-report is not ground truth.** A model that silently drops a filter
  *and* fails to declare it is not caught. This reduces the failure rate,
  it does not eliminate the class. Stated as a residual limitation.
- **New false refusals.** A model over-declaring constraints could refuse
  answerable questions. Mitigated by refusing only on declared-and-absent,
  never on inferred-and-absent.
- Adds one field to the LLM contract — a prompt change, so P0.2's
  determinism work must be measured after this, not before.

### Testing
Re-run the 25-query corpus ×3; QA-013/015/022/024 must become refusals or
correct answers, never silent substitutions. Confirm QA-001/003/005/010/023
still answer identically (no new false refusals).

---

## P0.2 — Non-deterministic spec generation

**Severity:** CRITICAL
**Affected:** QA-008, QA-015, QA-018, QA-019, QA-022, QA-024

### Current behaviour
Identical question, identical data, three runs, materially different
outcomes. QA-008: `208 / 194 / REFUSED`, with three different specs.

### Root cause — **it is not temperature**
`temperature=0.0` is already passed ([nl_spec.py:506](src/pipeline/aggregate/nl_spec.py#L506)). Three real sources:

1. **Provider non-determinism.** `call_llm` is local-first
   ([client.py:119](src/llm/client.py#L119)): it calls `LOCAL_LLM_URL` and
   falls back to Groq/Gemini **on any exception**. Across 75 runs the same
   question was served by different models depending on transient endpoint
   health. Different model → different spec, at any temperature.
2. **No seed.** `_post_local` sends `temperature` and `max_tokens` but no
   `seed` ([client.py:270](src/llm/client.py#L270)). Temperature 0 is
   near-greedy, not reproducible, on a sampling backend.
3. **Prompt-mutating retries.** `call_llm_json` retries up to
   `max_attempts=3` and on each failure sets
   `message = user_message + _json_only_correction(schema_hint)`
   ([json_extract.py:229](src/pipeline/json_extract.py#L229)). Attempt 2
   asks a *different* prompt than attempt 1, so the number of attempts —
   itself timing-dependent — changes the result. `_call_local` has a second
   empty-response retry that changes `max_tokens`.

### Affected layer
**L1**, plus shared infrastructure (`client.py`, `json_extract.py`). Note
L2–L5 are already deterministic given a spec.

### Proposed solution
Determinism cannot be *guaranteed* across heterogeneous providers, so the
goal is the brief's second option — **controlled variation with
explanation** — plus removing the avoidable sources:

1. **Send a seed** on the local path when the caller asks for determinism.
   Additive parameter, default unchanged for other callers.
2. **Record the provenance of every generation** — which backend served it,
   how many attempts, whether a corrective retry fired — into
   `SpecGeneration` and out through `to_dict()`. Today an answer cannot be
   explained because we cannot see which model produced it.
3. **Surface instability rather than hide it.** Where provenance shows a
   fallback or a corrective retry occurred, that belongs in the answer's
   warnings, because it is exactly the condition under which a repeat query
   may differ.

Explicitly **not** proposed: caching specs by question hash. That would
manufacture the appearance of determinism while leaving the underlying
variance in place, and would go stale against a changing registry.

### Risks
- A seed parameter an endpoint ignores is silently ineffective — hence
  recording provenance, so we can tell.
- Full determinism remains impossible while cloud fallback is reachable;
  this is a mitigation plus honesty, not a cure.

### Testing
Run a subset ×5 and record spec-hash stability per backend. Success is
stable specs *when served by one backend*, and a visible warning when not.

---

## P0.3 — Schema card omits graph capabilities

**Severity:** HIGH
**Affected:** QA-013, QA-014, QA-015

### Current behaviour
QA-014 refused 3/3 with the model stating the schema cannot distinguish
witness/victim roles. True of the card, false of the data.

### Root cause
Structural, in two places:

1. **`RelationshipInfo` has no field for edge properties.** It measures
   `edge_n`, fanout, cardinality, versioning — and nothing about
   properties. The registry never looks.
2. **`build_schema_card` emits relationships with no property line**
   ([route_age.py:161](src/pipeline/aggregate/route_age.py#L161)):
   `f"({source})-[:{rel}]->({target})  {edge_n} edges{fan}{ver}"`.

Node properties get presence counts and value examples; edges get nothing.

### Affected layer
**L1** input construction (registry + card), not the model.

### Proposed solution
Extend the measurement, and let the card render what is measured — the same
dynamic pattern already used for node value examples:

1. Add `properties: Mapping[str, EdgePropertyInfo]` to `RelationshipInfo`,
   populated by `build_registry()` with presence counts.
2. Collect enumerable edge-property values with the **existing** bounded
   policy in `collect_value_examples` (only few-valued properties, failures
   swallowed per property, nothing sampled).
3. Render them under each relationship line.

This exposes whatever the graph holds. It does not name `role`,
`arrest_status`, `accused` or any observed value in code — a future edge
property appears automatically.

### Risks
- **Card size.** The card is ~9.4 KB and rule 3 depends on the model
  reading it; unbounded growth could push a cloud request past a token cap
  (a failure mode this repo has hit before — see the `cloud_system_prompt`
  comment). Mitigation: reuse the existing per-label/property caps.
- **Extra registry queries** at startup. Bounded by the same limits as node
  examples.

### Testing
Assert `'role' in card` and that QA-013 applies a role filter or refuses;
QA-014 produces a spec instead of `spec_generation_failed`.

---

## P1.1 — Traversal direction errors

**Severity:** HIGH
**Affected:** QA-007, QA-009, QA-012, QA-015, QA-020, QA-025

### Current behaviour
Answerable questions refused because the spec names a path that does not
exist: QA-007 emitted `direction: "in"` for `Person -BELONGS_TO_CASE->
Case`.

### Root cause — **the convention is already documented**
`_SYSTEM` rule 6 states: *"out follows the arrow as the card draws it; in
goes against it."* The card draws
`(Person)-[:BELONGS_TO_CASE]->(Case)`. The model still chose `"in"`.

Decisive evidence that this is planner inconsistency, not a schema or
documentation gap: **QA-023 ran the identical traversal with
`direction: "out"` and returned 208, stable 3/3**, while QA-007 was refused
3/3. QA-010 also used `"in"` *correctly* for Weapon→Case.

So "write clearer instructions" is not the fix — the instruction exists.

### Affected layer
**L1** generates it; **L2** currently refuses it. The remedy belongs in L2,
because L2 has the registry and can be deterministic.

### Proposed solution
**Deterministic direction reconciliation in the validator.** When a hop
`(source, rel, target, direction)` is unknown, check whether the *reverse*
orientation is a uniquely-known triple. If exactly one orientation exists
in the registry, the direction is not ambiguous — it is determined — and
the validator can normalise the hop and record that it did so.

Properties that keep this honest:
- **Deterministic**, registry-driven, no model in the loop.
- **Only when unambiguous.** If both orientations exist, or neither, behave
  exactly as today and refuse.
- **Recorded**, so a corrected spec is visible in the receipt rather than
  silently rewritten.

This is the narrow claim "an impossible direction with exactly one possible
alternative is a typo, not a semantic choice". It does not touch relationship
*type* selection — QA-012 chose `BELONGS_TO_CASE` where `CITES` was correct,
which is a different error this fix must not paper over, and will still be
refused.

### Risks
- **Over-correction.** A genuinely different intended traversal could be
  normalised into an executable one. Bounded by the uniqueness condition
  and by recording the correction. This is the main thing to scrutinise in
  review.
- Must not run for role-qualified hops where reversing changes meaning.

### Testing
QA-007, QA-020, QA-025 should answer or refuse for their *other* reasons
(QA-020 also hallucinates `name`; QA-025 also hallucinates
`caliber_or_bore`). QA-012 must still refuse. QA-010/QA-023 must be
unchanged.

---

## P1.2 — Compound questions silently half-answered

**Severity:** HIGH
**Affected:** QA-008, QA-011, QA-014, QA-025

### Current behaviour
QA-011 ("how many officers … and how many cases have an officer") returns a
single scalar 76 — officers only. The dropped half produces no warning; the
only warning present concerns verification.

### Root cause
`AggregateSpec` holds exactly one `measure` and one `population`. A
two-part question is **not representable**. Nothing detects or reports the
truncation.

### Affected layer
Spec model + **L1** (no signal emitted).

### Proposed solution
Take the brief's option 2 — **detect and communicate** — rather than
multi-measure support. Rationale: multi-measure changes `AggregateSpec`,
the compiler, reconciliation and the receipt, and would be a larger change
than the rest of Phase 1 combined, with more regression surface. Detection
is cheap, safe, and removes the unacceptable behaviour (silent partial
answers) immediately.

Mechanism: reuse P0.1's `question_constraints` inventory by adding a
sibling declaration for *requested outputs*. When L1 reports more than one
and the spec expresses one, L2 refuses with a message naming what was not
answered.

Multi-measure support is recorded as deferred, not rejected.

### Risks
- Converts three currently-"answered" queries into refusals. That is the
  intended trade: a refusal is correct where the alternative is a silent
  partial answer.

---

## P2.1 — Datetime aggregate crash

**Severity:** HIGH
**Affected:** QA-018

### Current behaviour
```
ValueError: invalid literal for int() with base 10: '2024-09-14T22:00:00Z'
  executor.py:273 → observed_n = int(value or 0)
```
The only uncaught exception in 75 runs; it escaped route-level handling.

### Root cause
The cast is unconditional for every non-grouped spec. `observed_n` is
**coverage bookkeeping**, not the answer — the `min` query had already
succeeded and its correct result was discarded by the exception.

Note `shapes.validate_scalar` is already type-tolerant (it applies numeric
checks only when `isinstance(value, (int, float))`), so the datetime passes
shape validation and dies three lines later. The assumption is isolated to
this one statement.

A second instance of the same assumption sits one layer up in L2:
`missing_value_field: "which number is being aggregated?"` — `min` over a
date is an ordinary question.

### Affected layer
**L4** (`executor.py`), with a contributing **L2** message.

### Proposed solution
Make the observed-row bookkeeping type-aware rather than date-aware, per
the brief's "do not only patch date handling": derive `observed_n` from a
numeric value when one is available, and otherwise fall back to the row
count (which is what the field actually means — how many rows were seen).
No parsing of dates, no type sniffing beyond "is this a number".

### Risks
- Low. `observed_n` feeds coverage reporting; making it correct for
  non-numeric measures cannot make a numeric measure wrong.

### Testing
QA-018 must return a date or refuse cleanly — never raise. Coverage on
count queries must be byte-identical to today.

---

## P2.2 — Case-link bridge ignores the registry

**Severity:** HIGH
**Affected:** QA-016, QA-017

### Current behaviour
Every date-filtered Incident question refuses:
`window_incident_date resolved to 51 case(s) but could not be applied to
Incident: no declared path from Incident to Case`.

### Root cause
`plan_link` ([composite_executor.py:392](src/pipeline/aggregate/composite_executor.py#L392)) resolves the subject→Case link
from the plan only:

```python
link = getattr(plan, "subject_case_link", None)   # dead: field does not exist
...
for c in plan.constraints:                        # only RELATIONSHIP constraints
```

**`CompositeAggregatePlan` has no `subject_case_link` field**, so the first
branch always returns `None`. A pure time-window spec has no relationship
constraint, so the second finds nothing — guaranteed failure.

The registry holds the answer: `(Incident, BELONGS_TO_CASE, Case)` and
`(Incident, PART_OF, Case)`. Separately, `Incident.incident_datetime`
exists, so this need not route through Case at all.

### Affected layer
**L4** (`composite_executor`). The only Phase 1 failure with no LLM
involvement.

### Proposed solution
Add a registry-backed fallback: when the plan declares no link, ask the
snapshot for relationships from the subject label to `Case`. Apply it only
when the choice is **unambiguous** (exactly one such relationship); if
several exist, refuse as today with a message naming the candidates rather
than picking arbitrarily.

`Incident` currently has two (`BELONGS_TO_CASE`, `PART_OF`), so this alone
leaves QA-016/017 refusing — correctly and explicably, instead of falsely
claiming no path exists. Choosing between them is a semantic question this
layer should not answer silently.

### Risks
- Picking the wrong edge would silently change a population. The
  unambiguity condition is what prevents it; a deliberately conservative
  choice.

---

## P3.1 — Distinct value aggregation

**Severity:** MEDIUM (capability gap)
**Affected:** QA-008

### Current behaviour
Counting distinct values of a non-identity property is unexpressible:
```
wrong_distinct_key: distinct_key 'cnic' does not identify a Person;
the registry measured 'entity_id' as its key.
```

### Root cause
`distinct_key` means *identity* — which property makes one unit — and L2
correctly pins it to the registry-measured key. There is no separate way to
say "count distinct values of field X".

### Affected layer
Spec model (`spec.py`) + **L2**.

### Proposed solution
Deferred to Phase 2, with reasoning. The brief's own constraint is "do not
weaken validation", and the correct shape is an **additive** `distinct_field`
distinct from `distinct_key`, which requires changes in the spec, the
compiler, the gate profiles, coverage (a 54%-absent field needs a
caveat) and reconciliation. That is a feature, not a stabilisation fix, and
Phase 1's remaining changes should be measured before adding it.

Interim behaviour is already safe: L2 refuses with an accurate message.
P0.1's work will additionally ensure the *other* half of such a question is
not silently answered instead.

---

## Implementation order

Dependency-driven:

1. **P0.3** schema card — unblocks judgement of P0.1
2. **P2.1**, **P2.2** — isolated code fixes, no LLM contract change
3. **P1.1** — deterministic direction reconciliation in L2
4. **P0.1 + P1.2** — one LLM contract change, measured together
5. **P0.2** — provenance and seed, measured last since steps 1 and 4 change the prompt
6. **P3.1** — deferred to Phase 2

## What will not be attempted

- No keyword rules, no question-ID special cases, no hardcoded roles or values.
- No spec caching to fake determinism.
- No multi-measure spec model in Phase 1.
- No weakening of `wrong_distinct_key` or any existing validation.
