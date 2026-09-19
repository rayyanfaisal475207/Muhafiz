# Phase 8 — Architecture Checkpoint

The aggregate subsystem as it stands at the end of Phase 7D. Descriptive
only: nothing here proposes a change.

Branch `feat/aggregate-engine-phase0` · HEAD `7fdd54c`

---

## Request flow

```
natural-language question
        ↓
typed aggregate interpretation        AggregateSpec (caller-supplied)
        ↓
requirement extraction                gates.requirements_from_spec()
        ↓
authority resolution                  authority.resolve_authority()
        ↓
gate-profile selection                model proposes, code derives
        ↓
profile compatibility validation      gates.validate_selection()
        ↓
DIRECT execution  ──or──  COMPOSITE execution
        ↓
gate evaluation                       gate_evaluator.evaluate_gates()
        ↓
coverage / confidence evidence
        ↓
reconciliation                        reconcile.reconcile()
```

---

## 1. Input layer

Questions reach the aggregate subsystem as an `AggregateRouteRequest`
carrying the question text, a `Scope` (role, user, case allow-list) and —
for the structured route — a typed `AggregateSpec`.

`Scope` is never populated from question text. Cross-case access is gated
on role (`supervisor` / `station-admin` / `platform-admin`) in both
`route_structured` and `route_age`, duplicated deliberately so a security
check does not silently follow another module's refactor.

---

## 2. Planning layer

Two independent planners, by design.

**Structured — `AggregateSpec`** (`spec.py`, 347 lines). A frozen dataclass:
`measure`, `population` (entity + traversals + predicates), `grain`,
`distinct_key`, `group_by`, `ratio`, `time_window`, `scope`,
`canonicalization`. Phase 4 deliberately does not generate specs from
natural language; the harness supplies them.

**AGE — `AgeQueryPlan`** (`age_plan.py`, 262 lines). A pydantic model with
`extra="forbid"`. The model names labels, relationships, direction,
filters, aggregate, grain and one `gate_profile_id`. It cannot express
query text: there is no `cypher`, `raw_where`, `graph_name` or
`custom_gates` field, and 27 forbidden field names are asserted absent.

**Prompt.** `route_age._SYSTEM_PROMPT` plus a schema card generated from
live registry measurements (labels, property presence, relationship
fanout, real example values) and the gate catalogue rendered from
`gates.CATALOGUE` itself — one registry, so prompt and enforcement cannot
drift.

**Strict parsing.** `AgeQueryPlan.model_validate()` rejects unknown fields
outright. An invented field is a parse error, not something dropped.

---

## 3. Authority layer

`authority.py` (174 lines) resolves each constraint to the source the
registry measured as authoritative. The model may state a requirement; it
never selects a backend.

| Constraint | Authority | Basis |
|---|---|---|
| `incident_date` | **PostgreSQL** `cases.incident_date` | present 64/73; the AGE `Case` node has no date at all |
| `Person.age` | **graph** `Person.age` | measured 19/430 |
| relationships | **graph** | edges exist only in the evidence graph |
| `police_station`, `location` | **PostgreSQL** | measured 73/73 |
| unknown field | **UNRESOLVED** | caller refuses rather than guessing |

`registry.LogicalField` carries `authority_basis` so a decision can be read
back without re-deriving it.

**Multi-source composition** (`composite.py`, `populations.py`,
`composite_executor.py`). Each constraint executes at its own source and
yields a population of stable ids; trusted code intersects them. Joins use
`case_id` / `entity_id` / `record_id`, never display names — a
`case_id × entity_id` join raises `PopulationKeyMismatch` rather than
silently returning empty.

---

## 4. Execution layer

**Direct** — `executor.execute()` via the existing validated compiler.
Chosen when every constraint resolves to one source. 30 of 42 corpus cases.

**Composite** — `execute_composite()`. Chosen only when authority
resolution proves more than one source is required. 12 of 42 cases, 9
successful; the 3 "failures" are correct refusals naming their unapplied
constraint. Correctness invariants (`merged_into IS NULL`,
`superseded_by IS NULL`) are injected per triple from the registry.

**AGE** — `age_plan → validator → compiler → guard → age_execution`.
`execute_compiled_age_query()` accepts only a `CompiledAgeQuery`
constructed by the deterministic compiler.

---

## 5. Safety layer

**Gate catalogue** (`gates.py`, 610 lines) — 14 deterministically
measurable gates, 6 closed profiles. No subjective gate exists and none can
be added through model output.

**Profile selection policy:**

| Case | Outcome |
|---|---|
| exact / compatible | accept |
| under-powered | escalate to the derived requirement |
| mislabelled shape | **correct** to the derived requirement |
| unknown id | refuse |
| ratio-vs-count incoherence | refuse |
| stronger, same family | accept, never downgraded |

Invariant: `effective_profile ≥ required_profile_for(typed_spec)`, always —
the requirement is computed before the model's choice is read.

**Gate evaluation** (`gate_evaluator.py`, 422 lines) reads only recorded
evidence. A failed required gate blocks on **both** paths.

**No raw Cypher**: model output cannot carry query text; execution accepts
only compiler-produced objects; `cypher_guard` runs over compiler output as
a regression assertion.

**No silent broadening**: a constraint is applied or the plan refuses,
naming it.

---

## 6. Verification layer

**Reconciliation** (`reconcile.py`) classifies AGREEMENT,
SINGLE_ROUTE_VALID, SEMANTICALLY_DIFFERENT, CONFLICT, REFUSED. No LLM
adjudicates; no conflict is averaged.

**Comparability before values.** `comparable_key()` compares
(interpretation, grain, grouping) first. `canonical.py` proves
`count ≡ count_distinct` only when the computation traverses nothing and
the label's key is measured unique — otherwise the two stay different.

**Semantic route** is evidence-only: `result_shape="evidence"`,
`numeric` always `None`. 485 numeric mentions were recorded across the
corpus and **zero** promoted into an aggregate.

**Normalization** (`confidence.py`) is typed per metric; a count without a
shared denominator, or an average, refuses rather than inventing a scale.

**Consensus** (`consensus.py`) runs comparability → confidence →
normalization and stops at `ELIGIBLE`. Weighted combination raises
`ConsensusPolicyRequired`: no weights are invented.

---

## 7. Module inventory

**New this redesign (14 modules, 4,110 lines):** `age_plan.py`,
`age_plan_validator.py`, `age_compiler.py`, `age_execution.py`,
`age_eval_client.py`, `authority.py`, `populations.py`, `composite.py`,
`composite_executor.py`, `canonical.py`, `confidence.py`, `consensus.py`,
`gates.py`, `gate_evaluator.py`.

**Pre-existing, extended:** `route_structured.py`, `route_age.py`,
`reconcile.py`, `config.py`.

**Pre-existing, untouched:** `spec.py`, `validator.py`, `compiler.py`,
`executor.py`, `registry.py`, `temporal.py`, `coverage.py`, `receipt.py`,
`shapes.py`, `cypher_guard.py`, `routes.py`, `corpus.py`, `shadow.py`,
`route_semantic.py`.
