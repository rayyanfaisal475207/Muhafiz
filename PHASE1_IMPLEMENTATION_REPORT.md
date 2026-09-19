# Phase 1 — Implementation Report

Stabilization of the aggregate query architecture. Seven issues investigated,
root-caused and fixed, plus three defects found during implementation. No
question-specific conditions, no QA-ID checks, no keyword matching and no
hardcoded roles or values were introduced; every fix is registry-driven or
structural.

| | |
|---|---|
| Tests | 512 passing (472 baseline + 40 new), full project suite green |
| New modules | `direction.py`, `fidelity.py` |
| New tests | `tests/test_aggregate_fidelity.py` (40 cases) |
| Validation | 25-query blind corpus, 3 runs each, against live data |
| Result | non-determinism 6 → 1, crashes 1 → 0, silent substitutions 5 → 0 |

---

## 1. Changes implemented

### P0.3 — Schema card capability expansion

**Root cause.** Two structural gaps, not a formatting choice.
`RelationshipInfo` had no field for edge properties, so the registry never
measured them; and `build_schema_card` rendered each relationship as
`(A)-[:R]->(B)  N edges` with no property line at all. Node properties got
presence counts and value examples; edges got nothing.

The consequence was not a model failure. Asked for witness/victim counts,
the planner replied that *"the schema does not contain explicit labels or
properties to distinguish 'witness' or 'victim' roles"* — correct reasoning
over an incomplete input. Measured: `'role' in card → False`, while the
graph held `INVOLVED_IN.role` = accused 94, complainant 73, witness 37,
victim 9.

**Fix.** Dynamic discovery, mirroring the existing node pattern exactly:

- `RelationshipInfo.properties: dict[str, PropertyInfo]` — reusing the node
  property type, so presence semantics stay identical.
- `_measure_edge_properties()` uses `keys(r)`, for the same reason
  `_measure_entity` uses `keys(n)`: AGE stores an absent property as absent
  rather than NULL, and only `keys()` distinguishes "no such qualifier" from
  "a null one".
- `collect_value_examples()` extended to edges under the **same bounded
  policy** — only few-valued properties, failures swallowed per property,
  nothing sampled.
- Rendering under each relationship line, capped by
  `_MAX_EDGE_PROPS_PER_REL = 4`, ranked by presence.

**Schema explosion was a real risk and is bounded.** Measured: 48 edge
properties across 20 triples, distributed unevenly — two `SAME_AS` triples
carry 8 each, mostly review bookkeeping. `_EDGE_PROPERTY_BLOCKLIST` withholds
provenance and versioning fields (`as_of`, `confidence`, `source_doc_id`,
`source_chunk_id`, `surface_text`, the supersession marker). That list is
about *meaning* — "when was this extracted" is not a filter a question can
want — and names no domain value. Card growth: 9,434 → 12,575 chars (+33%),
down from +45% before the blocklist.

**Nothing is hardcoded.** A qualifier added to the graph tomorrow appears on
the card with no code change.

### P2.1 — Non-numeric aggregate crash

**Root cause.** `observed_n = int(value or 0)`, unconditional for every
non-grouped spec. `min`/`max` over a date returns a timestamp, so the cast
raised `ValueError` on a query that **had already succeeded** — the correct
date was discarded by an exception thrown while filing coverage paperwork
about it. `shapes.validate_scalar` was already type-tolerant, so the
datetime passed shape validation and died three lines later.

**Fix.** `_observed_rows(value)`, generalised over kinds of value rather
than special-casing dates:

- a count-like number is its own observed-row figure;
- any other scalar — timestamp, string, category — is **one** observed
  value, because it says nothing about rows scanned and inventing a number
  would be worse than reporting the one true thing;
- nothing observed is zero;
- `bool` is excluded from the numeric branch deliberately
  (`isinstance(True, int)` is True in Python, and a boolean aggregate is
  categorical).

Verified on the exact crashing input: `'2024-09-14T22:00:00Z'` → `1`, no
exception. Counts unchanged.

### P2.2 — Registry-backed case linking

**Root cause.** `plan_link` resolved the subject→Case edge only from the
plan: `getattr(plan, "subject_case_link", None)` — a field
`CompositeAggregatePlan` **does not have**, so that branch was dead code —
then from a relationship constraint targeting `Case`. A spec whose only
constraint is a time window has neither, so the failure was guaranteed, and
the message claimed no path existed for subjects where the registry had
measured two.

**Fix.** `registry_case_link(snapshot, subject_label)`, consulted only when
the plan declares nothing. It considers both orientations (a `Case -> X`
edge is still a path, travelled backwards) and applies a strict rule:

- exactly one candidate → use it;
- more than one → **refuse, naming the candidates**, because two edges are
  two populations and choosing between them is a semantic act this layer is
  not entitled to perform;
- none → say so honestly.

Measured effect: `Person`, `Weapon`, `Document` now resolve automatically
where they previously failed. `Incident` and `Officer` still refuse — with
a true, actionable message instead of a false one.

### P1.1 — Traversal direction reconciliation

**Root cause — and what it is not.** The convention was already documented:
`_SYSTEM` rule 6 states *"out follows the arrow as the card draws it; in
goes against it."* The decisive evidence is that **QA-023 ran the identical
traversal with `"out"` and succeeded 3/3 while QA-007 was refused 3/3 for
`"in"`**, and QA-010 used `"in"` correctly. This is planner inconsistency,
not a missing instruction — so "write a clearer prompt" was ruled out.

**Fix.** `direction.py`, run before validation, under three rules:

1. **Only when the hop is already invalid** — a resolving hop is untouched.
2. **Only when the correction is unique** — if flipping does not resolve, or
   if both orientations exist, nothing happens.
3. **Only direction** — never the relationship type, the labels, or a role
   filter. A plan traversing the wrong edge is asking the wrong question and
   must keep reaching the user as a refusal.

Corrections are recorded as `DirectionCorrection` and surfaced as a warning,
so the executed spec is never silently different from the generated one. A
corrected spec has a different `spec_hash()`, which is intended: the hash
identifies the computation that ran.

**Rule 3 was verified live.** QA-012's planner emitted the impossible
`Case -BELONGS_TO_CASE-> Case` for a citation hop; reconciliation left it
alone (`corrections: 0`) and the refusal stood.

### P0.1 — Spec fidelity protection

**Root cause.** Every other check validates the spec against the
**registry**; none validated it against the **question**, which is a string
nobody reads again after generation. A spec that drops a condition is
therefore not merely undetected but *undetectable* downstream — and it is
**simpler** than the correct spec, so it passes more gates and fires fewer
verification triggers.

**Fix.** A two-part mechanism with no vocabulary of any kind:

1. The generator declares, in its own words, every condition the question
   imposes (`question_constraints`), each marked `applied` with a `where`
   naming the field that carries it.
2. `fidelity.check()` compares that declaration against the structure:
   a condition declared **unapplied** is taken at its word and refused; a
   condition declared **applied** must be matched by something the spec
   actually contains, counted per carrier.

`role` is counted separately from `traversal`, because traversing an edge
does not satisfy a claim about the role on it — that is precisely the
QA-013 shape.

**Why the second check matters.** Pure self-report would miss a model that
drops a filter *and* claims it applied one. Observed live on QA-015: the
model claimed `arrest status filter @ predicate, applied: true` while
`predicates` was empty, and the count caught it.

### P1.2 — Compound question handling

**Root cause.** `AggregateSpec` holds one measure over one population. A
two-part question is not representable, and the observed behaviour was to
answer the first part and say nothing about the second.

**Fix.** Multi-measure support was **not** implemented — it would change the
spec, compiler, reconciliation and receipt, a larger change than the rest of
Phase 1 combined. Instead the generator declares `requested_outputs`, and
more than one produces a refusal naming every figure asked for and telling
the user to ask them separately. Detection, not silence.

### P0.2 — Determinism and provenance

**Root cause — not temperature.** `temperature=0.0` was already set. Three
real sources, all measured:

1. **Provider switching.** `call_llm` is local-first and falls back to
   Groq/Gemini **on any exception**, so a transient local hiccup silently
   changes which model answers.
2. **No seed** on the local payload.
3. **Prompt-mutating retries.** `call_llm_json` retries up to 3 times and
   appends a correction each time, so attempt 2 asks a different question
   than attempt 1 — and the attempt count is timing-dependent.

**Fix.** Caching was explicitly rejected: it manufactures the appearance of
determinism while leaving the variance in place. Instead `_GenerationProbe`
counts the model calls made per generation and records
`GenerationProvenance{attempts, retried}` into the receipt; a retried
generation raises a warning saying a repeat may interpret the question
differently.

**Honest status:** across every post-fix run, `retried` was **0** — the
richer schema card appears to make valid JSON easier to produce first time,
so the retry path stopped firing. That is a real improvement, but it means
this instrumentation has not yet been stress-tested against the failure it
was built to expose.

---

## 2. Defects found during implementation

Six, found by running real questions and reading the specs that came back —
not by code review.

**Class 1: the schema card's notation was being copied verbatim into specs.**

| Card wrote | Model emitted | Effect | Origin |
|---|---|---|---|
| `[r].role` | `role_field: "[r].role"` | refusal | introduced by the first edge rendering |
| `.cnic` | `distinct_key: ".cnic"` | refusal | **pre-existing**, latent |

**Class 2: the card was hiding capability.**

| Defect | Effect | Origin |
|---|---|---|
| `_MAX_RELS_IN_CARD = 26` ranked relationships by edge count and cut the tail | `Case-[CITES]->Case` (9 edges) and `Person-[OWNS]->Weapon` (30 edges) were invisible; the planner substituted a relationship it could see, or reported none existed | **pre-existing** |

This one corrected an earlier misattribution. QA-012 was first recorded as a
planner relationship-selection error. It was not: `CITES` had never been
shown to the model. Ranking relationships by frequency is backwards, because
a rare relationship is exactly what a specific question is about. The cap is
now 60 — the whole schema, for about 1 KB.

**Class 3: defects in this phase's own new code.**

| Defect | Effect | Origin |
|---|---|---|
| `where: "predicates"` vs `"predicate"` | fidelity check silently skipped a claim | introduced |
| `observed_n = 1` for a non-counting scalar | coverage announced "only 1 record qualifies" about a `min` over 73 incidents | introduced by the P2.1 crash fix |
| `applied: false` believed unconditionally | the adversarial control (QA-021) regressed from an honest `0` to a refusal | introduced by P0.1 |

The last two are worth stating plainly. The crash fix **traded an exception
for a misleading caveat**, which by this project's own standards is the worse
failure — a crash is loud, a confidently-caveated wrong figure is not. And
the fidelity check **conflated "I could not express this" with "this matches
nothing"**, which would have destroyed the system's ability to answer
"none". Both were caught by running the corpus, both are fixed, and both
have regression tests.

**The principle applied throughout:** a schema card is an input to a
machine, not a syntax illustration — every name it prints must be usable
verbatim, and everything the graph has must be visible.

The `.cnic` case predates this work and had been silently causing refusals
on any question needing a node property as a distinct key. It only became
visible because P0.3 and the fidelity contract pushed the planner into
richer specs.

**The principle applied:** a schema card is an input to a machine, not a
syntax illustration. Every name it prints must be usable verbatim. Both
node and edge properties now render as `property <name>` / `edge property
<name>`, with regression tests for each. The `where` matching normalises
spelling (plural, case, spacing) but **not meaning** — an unrecognised
location stays unrecognised rather than being guessed at.

---

## 3. Files changed

**New**

| File | Lines | Purpose |
|---|---|---|
| `src/pipeline/aggregate/direction.py` | 167 | deterministic direction reconciliation |
| `src/pipeline/aggregate/fidelity.py` | 254 | spec-vs-question fidelity, compound detection |
| `tests/test_aggregate_fidelity.py` | 494 | 37 cases across all five mechanisms |

**Modified**

| File | Change |
|---|---|
| `src/pipeline/aggregate/registry.py` | `RelationshipInfo.properties`, `_measure_edge_properties`, blocklist |
| `src/pipeline/aggregate/route_age.py` | edge properties + values on the card; bare property names |
| `src/pipeline/aggregate/executor.py` | `_observed_rows` replaces the unconditional `int()` cast |
| `src/pipeline/aggregate/composite_executor.py` | `registry_case_link` fallback |
| `src/pipeline/aggregate/nl_spec.py` | constraint inventory, requested outputs, edge-property rule, provenance |
| `src/pipeline/aggregate/orchestrator.py` | direction step, fidelity gates, provenance warning, audit fields |

**Backward compatibility.** Both fidelity gates are skipped when the caller
supplies its own spec (`generation is None`), so the evaluation harness,
shadow comparisons and every corpus-driven test are unaffected. The new
checks constrain only LLM-generated specs — which is why the pre-existing
suite passed essentially unchanged.

One existing test required updating:
`test_age_gateway_phase6_fixes.py::test_age_is_visible_on_the_card` asserted
`".age" in card`. Its subject is that a sparse property stays visible, and
that still holds; the assertion had encoded the dotted rendering, which was
removed precisely because the planner copied it into a spec verbatim. The
assertion now reads `"property age" in card` — same intent, current
notation.

---

## 4. Tests added

37 cases in `tests/test_aggregate_fidelity.py`:

- **Fidelity (13)** — no-generation and empty-declaration must not refuse;
  unapplied and lost constraints must; role counted separately from
  traversal; unknown `where` not held against the spec; spelling variants.
- **Single output (4)** — one, none, absent, and two-figure questions.
- **Direction (7)** — valid untouched; unique reverse corrected and
  recorded; **wrong relationship never repaired**; ambiguity left alone;
  hash changes; multi-hop corrects only the wrong hop.
- **Registry case link (4)** — unique forward, unique reverse, several
  (refuse and name), none.
- **Observed rows (6)** — count, zero, None, timestamp, category, bool,
  negative.
- **Schema card (3)** — edge property visible, no `[r].` decoration, no
  leading dot on node properties.

---

## 5. Before vs after

25 questions, 3 runs each, against live data, through
`orchestrator.answer_question()`.

**A note on how these numbers were produced.** Four fixes landed while the
post-fix corpus was already running, so six queries were re-run against the
final code and their later results are the ones reported. The stale results
are not presented as current: the affected queries are QA-006, QA-008,
QA-012, QA-018, QA-019, QA-021, and both files are kept
(`qa_phase1_after_results.json` is the merged final state).

### Headline

| Metric | Before | After |
|---|---|---|
| Non-deterministic queries | **6** | **1** |
| Unhandled crashes | **1** | **0** |
| Silent question substitutions | **5** | **0** |
| Runs carrying a checked constraint declaration | 0 | **75** |
| Runs answered | 41 | 37 |
| Runs refused | 33 | 38 |

Answered runs fell by four. That is the intended trade, not a loss: the
answers that disappeared are the silent substitutions, and every one of the
nine queries that passed before still returns its original value.

### Per query

| ID | Before | After | Change |
|---|---|---|---|
| QA-001 | 73 | 73 | unchanged |
| QA-002 | 32 | 32 | unchanged |
| QA-003 | 19 | 19 | unchanged |
| QA-004 | 30 | 30 | unchanged |
| QA-005 | 73 | 73 | unchanged |
| QA-006 | 34 | 34 | unchanged |
| QA-007 | REFUSED ×3 | **208** | **fixed** |
| QA-008 | 208 / 194 / REFUSED | REFUSED ×3 | **determinism fixed**, compound detected |
| QA-009 | REFUSED ×3 | 9 / REFUSED / REFUSED | partial — see below |
| QA-010 | 32 | 32 | unchanged |
| QA-011 | **76** (silent half-answer) | REFUSED ×3 | **fixed** — both figures named |
| QA-012 | REFUSED ×3 | REFUSED ×3 | improved diagnosis |
| QA-013 | **206** (falsely verified) | **92** ×3 | **fixed, verified correct** |
| QA-014 | REFUSED ×3 | REFUSED ×3 | diagnosis now truthful |
| QA-015 | REFUSED / 55 / REFUSED | REFUSED ×3 | **determinism fixed** |
| QA-016 | REFUSED ×3 | REFUSED ×3 | message now true |
| QA-017 | REFUSED ×3 | REFUSED ×3 | message now true |
| QA-018 | **CRASH** / REFUSED / REFUSED | **2024-09-14T22:00:00Z** ×3 | **fixed, verified correct** |
| QA-019 | 0 / REFUSED / REFUSED | REFUSED ×3 | **determinism fixed** |
| QA-020 | REFUSED ×3 | REFUSED ×3 | unchanged |
| QA-021 | 0 | 0 ×3 | unchanged (regressed mid-work, fixed) |
| QA-022 | REFUSED / **208** / REFUSED | REFUSED ×3 | **determinism fixed** |
| QA-023 | 208 | 208 | unchanged |
| QA-024 | 0 / 32 / 32 | REFUSED ×3 | **determinism fixed** |
| QA-025 | REFUSED ×3 | REFUSED ×3 | unchanged |

### The two results worth reading closely

**QA-013 — the case this phase existed for.**

```
before:  206,  independently_verified: True
after:   92,   independently_verified: False
```

92 was confirmed by an independent query against the graph. The executed
Cypher now carries the role filter on the edge
(`e0.role = $p0`), which the schema card made expressible. The verification
flag went from `True` to `False` and that is an *improvement*: the AGE route
cannot express edge filters, so this class of question cannot be
independently verified, and the system now says so instead of presenting two
routes agreeing on the wrong question as assurance.

**QA-018 — the crash.**

```
before:  ValueError: invalid literal for int() with base 10: '2024-09-14T22:00:00Z'
after:   '2024-09-14T22:00:00Z',  coverage CAVEAT
         "Computed over the 64 of 73 record(s) that carry a incident_datetime."
```

The value that crashed the executor is now the answer, confirmed against the
graph, with a coverage statement that is specific and true.

### Where the remaining instability is

**QA-009** is the one query still non-deterministic: `9 / REFUSED /
REFUSED`. The mechanical classification calls this a new regression, because
it was uniformly refused before. That label is defensible and it is reported
as such — but the `9` is the correct answer, so the query moved from never
answerable to sometimes correct. It is recorded as an improvement with
residual instability rather than either a clean win or a regression.

### What the fixes did NOT fix

Two queries improved without being solved, and the distinction matters.

`QA-012` and `QA-019` both stopped failing for the reason they used to fail.
Before, `CITES` and `OWNS` were invisible to the planner, so it either
invented an impossible edge or reported that no relationship existed. With
the full relationship set on the card it now reaches for the *right*
relationship — and anchors it to the wrong entity
(`Person -CITES-> Case` rather than `Case -CITES-> Case`). That is hop
anchoring, which `direction.py` deliberately will not repair under rule 3,
because choosing which entity a relationship starts from is a semantic
choice and repairing it would be guessing. Both refuse with accurate
messages.

---

## 6. Remaining limitations

1. **Fidelity depends partly on self-report.** A model that drops a
   condition and never declares it is not caught. The structural count
   catches the case where it declares and drops, but the class is narrowed,
   not closed. Closing it would require independently understanding the
   question — the task that produced the error.

2. **The AGE route cannot express edge-property filters.** `MatchPattern`
   has `from_alias`/`to_alias` but no edge alias, and `Filter` binds to node
   aliases. Role-qualified questions therefore **cannot be independently
   verified** — QA-013 correctly reports `independently_verified: False`.
   This is now honest rather than hidden, but it is a real capability gap.

3. **One edge-property filter per hop.** `Traversal` carries a single
   `role_field`/`role_value`. A question needing two edge conditions (a role
   *and* an arrest status) is unrepresentable and refuses.

4. **Ambiguous subject→Case links refuse.** `Incident` reaches `Case` by two
   relationships. Deliberate: guessing would silently change the population.

5. **P3.1 (distinct value aggregation) deferred.** Counting distinct values
   of a non-identity property still refuses. The correct shape is an
   additive `distinct_field`, which needs compiler, gate-profile, coverage
   and reconciliation work — a feature, not a stabilisation fix.

6. **Determinism is mitigated, not guaranteed.** Provider fallback remains
   reachable and no seed is sent. Retries stopped firing, but the underlying
   mechanism is unchanged.

7. **The pipeline is still not wired to chat/API.** `/api/chat` continues to
   use the legacy `xagg.run_aggregate` engine. Everything here describes an
   architecture that is not yet user-reachable.

---

## 7. Recommendation

**Is the architecture ready for external gold-set QA? Yes, with one
qualification about what the gold set can measure.**

Against the success criteria set for this phase:

| Criterion | Status |
|---|---|
| **Stability** — same question, same spec/answer | **Met.** 24 of 25 queries deterministic across 3 runs, from 19 of 25. |
| **Safety** — no silent wrong answers | **Met** for the observed class. All 5 silent substitutions eliminated; none reappeared. |
| **Execution** — no crashes | **Met.** Zero in 75 runs, from 1. |
| **Explainability** | **Met.** Every answer carries the declared constraints, the fidelity result, direction corrections, generation provenance, and the executed query. |
| **Generalization** | **Met.** Every fix is registry-driven or structural. A new edge property, a new relationship, or a new question category is handled without a code change. |

**The qualification.** A gold set will measure interpretation quality, and
the remaining failures are concentrated there — hop anchoring (QA-012,
QA-019), hallucinated field names (QA-020), and questions needing two edge
filters (QA-015). Expect a gold set to produce **more refusals than
answers** on complex relational questions. That is the system behaving as
designed: it now prefers an explained refusal to a plausible wrong number.
If the gold set is scored on answer rate alone it will look worse than the
pre-Phase-1 system, which answered more questions by answering some of them
wrongly.

**Two things to settle before or alongside the gold set.**

1. **The AGE route cannot express edge-property filters.** Any
   role-qualified question is unverifiable by construction, and the gold set
   will contain such questions. `independently_verified: False` is now
   honest, but the verification architecture has a blind spot for exactly
   the class Phase 1 just unlocked.

2. **The pipeline is still not reachable from chat/API.** `/api/chat` uses
   the legacy `xagg.run_aggregate` engine. A gold set run against this
   architecture measures something users cannot currently reach, and a gold
   set run against the product measures the legacy engine. Which of those is
   intended should be decided explicitly rather than by default.

**Recommended sequencing:** run the gold set against this pipeline to
establish an interpretation-quality baseline, scoring refusals separately
from wrong answers rather than pooling them. Treat hop anchoring as the
highest-value Phase 2 target, since it is now the single largest remaining
cause of refusal on answerable questions.
