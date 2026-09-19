# Phase 1 — Closure Verification Report

Independent audit of the Phase 1 stabilization work, run against live data.
The purpose was to look for hidden weaknesses, regressions and hardcoded
solutions — not to confirm that the fixes worked.

It found two defects that Phase 1 itself had missed, one of which was a
hardcoded table of the exact shape the brief prohibits, sitting behind a
real correctness hole. Both are fixed. It also found that a determinism
claim made in the Phase 1 report was measured at too small a sample and is
overstated.

---

# Executive Summary

**Phase 1 status: REQUIRES ONE DECISION BEFORE CLOSURE.**

Not "requires additional fixes" — the seven Phase 1 issues are verified
fixed, and the two defects this audit found have been repaired and tested.
The blocker is a measurement, not a bug:

> A question the system can answer correctly is answered correctly on
> **1 run in 6** (and 1 in 20 under load). The other runs refuse. The cause
> is understood, deterministic, and fixable — but fixing it is a change to
> shared infrastructure outside the aggregate package, and that decision is
> not this audit's to make.

Nothing unsafe ships. Every failure observed in this audit was a refusal
with a truthful reason; no silent wrong answers were produced in any test.
The question is whether an answer rate of ~17% on a complex-but-answerable
question is acceptable for manual QA to begin.

| Area | Verdict |
|---|---|
| Silent question substitution | **Fixed and verified** |
| Schema awareness / dynamic discovery | **Fixed and verified** (proven with a novel property) |
| Aggregate result type safety | **Fixed and verified** |
| Registry-based resolution | **Fixed and verified** |
| Traversal direction reconciliation | **Fixed and verified**, including what it refuses to do |
| Hardcoded solutions | **One found and removed** (pre-existing, in the validator) |
| Determinism | **Overstated in the Phase 1 report** — see Finding C |
| Safety under failure | **Verified** — no hallucinated answers in any test |

---

# Verified Improvements

## 1. Silent question substitution

**Problem.** The system answered a broader question than the one asked and
reported full confidence — "how many accused persons" answered as "how many
involved persons" (206), marked `independently_verified: True`.

**Root cause.** Nothing validated the spec against the *question*. Every
check validated it against the registry, and a spec that drops a constraint
is *simpler*, so it passed more gates and fired fewer verification triggers
than the correct spec would have.

**Fix.** A declared constraint inventory from the generator, checked
deterministically against what the spec structurally carries. No keywords,
no question matching, no vocabulary of any kind.

**Verification method.** Five unseen, deliberately adjacent questions. If
constraints were being dropped, these would collapse onto one another.

| Test | Question | Result | Ground truth |
|---|---|---|---|
| SEM-1 | accused persons linked to cases | 92 (1 of 3 runs) | 92 ✓ |
| SEM-2 | witnesses participated | **37 ×3** | 37 ✓ |
| SEM-3 | victims recorded | REFUSED ×3 | 9 |
| SEM-4 | suspects with confirmed arrest status | REFUSED ×3 | not representable |
| SEM-5 | people involved in cases | **208 ×3** | 208 ✓ |

**Result: PASS.** All five produced *different* interpretations, each
carrying the correct role filter (`accused`, `witness`, `victim`). None
returned 206 — the old substitution answer. The distinction the failure was
about is preserved.

Two notes on reading this table. SEM-5 returning 208 rather than 206 is
correct, not a discrepancy: it asks about *cases* (`BELONGS_TO_CASE`, 208
persons), while 206 is persons-in-*incidents*. The system distinguished two
questions that the auditor had initially conflated. And SEM-4 refusing is
the desired outcome — `constraint_lost` fired because "confirmed arrest
status" and "suspect role" are two edge-property filters and a `Traversal`
carries one.

**Residual defect (SEM-3).** The planner expresses the role filter *twice*:
correctly on the traversal, and again as a node predicate `role eq victim`.
`Person` has no `role` field, so the validator refuses. Deterministic 3/3,
isolated to this one question, and it **fails safe**. The validator is
right; the prompt is not clear enough that an edge property must not also
appear in `predicates`.

## 2. Schema awareness

**Problem.** `INVOLVED_IN.role` existed in the graph and was invisible to
the planner, which then reported that the schema could not distinguish
roles — true of the card, false of the graph.

**Fix.** Edge properties measured by `keys(r)` in the registry and rendered
on the card with presence counts and enumerable values, under the same
bounded policy already used for node properties.

**Verification method — the strongest test in this audit.** A property that
has never existed was injected into the live graph, then traced through the
whole chain:

```
SET r.qa_probe_flag = 'zeta'   on 5 of 221 INVOLVED_IN edges

registry  -> {'arrest_status', 'qa_probe_flag', 'role'}   presence 5/221
card      -> "edge property qa_probe_flag  present on 5/221  values: 'zeta'"
```

**Result: PASS.** A novel property reached the planner with its real value
and correct sparse presence, with **no code change**. The probe was then
removed and the graph restored. This is the generalizability requirement
demonstrated rather than asserted.

## 3. Aggregate result type safety

**Problem.** `observed_n = int(value or 0)` crashed on a `min` over a date —
discarding a query that had already succeeded.

**Fix.** Type-aware coverage bookkeeping, generalised over *kinds* of value
(count-like number / any other scalar / nothing), with no date-specific
branch. A follow-up fix distinguished counting measures, whose value *is* a
population size, from summarising measures, whose value is not.

**Verification.**

| Kind | Input | `observed_n` |
|---|---|---|
| count | `73` | 73 |
| zero / none | `0`, `None` | 0 |
| datetime | `'2024-09-14T22:00:00Z'` | 1 |
| categorical | `'accused'` | 1 |
| boolean | `True` | 1 |
| negative | `-5` | 0 |

Live: QA-018 returns `2024-09-14T22:00:00Z` ×3 — confirmed against the graph
— with coverage `CAVEAT` and the message *"Computed over the 64 of 73
record(s) that carry a incident_datetime."* Both figures independently
verified.

**Result: PASS.** No datetime-specific handling exists.

## 4. Registry-based resolution

**Problem.** `plan_link` read `getattr(plan, "subject_case_link", None)` — a
field the plan class does not define, so the branch was dead code — and
refused with "no declared path" for subjects where the registry held two.

**Fix.** `registry_case_link()` resolves from measured relationships, in
both orientations, and applies a strict rule: exactly one candidate → use
it; several → refuse and name them; none → say so.

**Verification.**

| Subject | Result |
|---|---|
| Person / Weapon / Document | resolved automatically (unique) |
| Incident | refused, naming `BELONGS_TO_CASE (out), PART_OF (out)` |
| Officer | refused, naming `ASSIGNED_TO (out), BELONGS_TO_CASE (out)` |
| Date | no path, reported honestly |

**Result: PASS.** It never guesses.

## 5. Traversal direction reconciliation

**Fix.** Deterministic normalisation of an unambiguously-wrong hop
direction, before validation, recorded as a correction and surfaced as a
warning.

**Verification — adversarial, testing what it must NOT do.**

| Case | Corrected? | Correct behaviour |
|---|---|---|
| valid direction | no | untouched |
| unique reverse exists | **yes**, recorded | the intended fix |
| **wrong relationship type** | **no** | a different question, must refuse |
| **wrong target label** | **no** | a different question, must refuse |
| both orientations exist | no | ambiguous, must not choose |
| multi-hop, second hop wrong | only hop 2 | walk tracks the source label |
| role-qualified hop | yes, role preserved | see caveat below |

**Result: PASS**, with one documentation correction. The Phase 1
investigation stated this would not run on role-qualified hops. It does.
The behaviour is safe — the role filter is preserved verbatim and the
requested orientation does not exist, so there is no alternative population
to confuse it with — but the stated guarantee was inaccurate and is
corrected here.

Live evidence that rule 3 holds: QA-012's planner emitted the impossible
`Case -BELONGS_TO_CASE-> Case`; reconciliation left it alone
(`corrections: 0`) and the refusal stood.

---

# Findings

## Finding A — a hardcoded table, and the correctness hole behind it

**Severity: HIGH. Status: FIXED.**

The brief's prohibited pattern was present — not in Phase 1's new code, but
in the validator it depends on:

```python
_ROLE_BEARING_RELATIONSHIPS: dict[str, tuple[str, ...]] = {
    "INVOLVED_IN": ("role",),
    "ASSIGNED_TO": ("role",),
}
```

Two problems. It is a hand-kept list of relationship names, so a new
role-bearing edge needed a code edit. And its own justification — *"the
registry measures node properties, not edge properties"* — **stopped being
true when Phase 1 made the registry measure edge properties.** A Phase 1
change invalidated this code's premise without updating it.

**The hole it was hiding.** Role *values* were never checked. This
validated as `EXECUTABLE`:

```
role_field='role', role_value='NOT_A_REAL_ROLE'   ->  EXECUTABLE
```

It would have returned a clean, confident **0** — the "plausible, entirely
wrong number" failure class that `M3_value_literal` exists to catch.
Pre-existing, but Phase 1 made it far more reachable, because the planner
now actually uses role filters where it previously never did.

**Fix.** The validator checks the field against `info.properties` (measured)
and the value against `snapshot.edge_property_values(...)` (measured, and
only where the value set is small enough to enumerate honestly).

```
role_value='NOT_A_REAL_ROLE' -> UNSUPPORTED
  "INVOLVED_IN.role does not take the value 'NOT_A_REAL_ROLE';
   measured values are 'accused', 'applicant_pkm', 'complainant', ..."
role_value='accused'         -> EXECUTABLE
role_value=None              -> EXECUTABLE
```

An unenumerated property constrains nothing — empty means "not measured",
never "no values exist" — so a high-cardinality field is not falsely
rejected. A guard test now fails if any relationship name reappears in
validator *code* (comments are allowed; they are documentation, not
behaviour).

## Finding B — value_field resolves against the wrong namespace

**Severity: MEDIUM. Status: NOT FIXED — pre-existing, out of Phase 1 scope.**

`incident_date` is a *logical* field (Postgres-authoritative, valid for a
`time_window`) but **not** a node property. `_resolve_field` resolves
logical fields, so `value_field='incident_date'` validates, and the compiler
then emits `max(n.incident_date)` against the graph, where that property
does not exist. The result is NULL and the query refuses with
`null_result` rather than with an accurate diagnosis.

Observed on the blind min/max test. Confirmed pre-existing: `git diff` shows
Phase 1 never touched this code. Fixing it means routing value-field
aggregates by authority, which is a real change and not a closure repair.

**Recommended action:** Phase 2. The current behaviour is a refusal, not a
wrong answer, so it is safe but unhelpful.

## Finding C — the determinism claim was measured at too small a sample

**Severity: HIGH for the claim; MEDIUM for the system. Status: DIAGNOSED.**

The Phase 1 report stated "24 of 25 queries deterministic", measured at
**3 runs each**. At 20 runs, a question that the 3-run sample showed
answering 1-in-3 answers **1-in-20**.

```
STAB-FILTERED  "How many accused persons are linked to cases?"  (20 runs)
   1 ANSWERED (92, correct)     attempts=1
  19 REFUSED                    attempts=2
```

The correlation with `attempts` is perfect and reproduced in a second run
(1 of 6, same pattern). The mechanism:

```
attempts=1  ->  INVOLVED_IN:Incident -> BELONGS_TO_CASE:Case   ->  92  ✓
attempts=2  ->  INVOLVED_IN:Case                               ->  refused
```

**The causal chain.** The correct answer to this question needs a two-hop
spec. The model intermittently omits `grain` on that more complex payload;
`_validate_payload` rejects it; `call_llm_json` fires a corrective retry
that **appends text to the prompt** telling the model not to invent a
"different shape" and to "make your best judgment call anyway". The retried
prompt reliably produces the *simpler* single-hop spec, which is wrong.

So a retry intended to fix malformed JSON systematically degrades
interpretation quality. This is P0.2's diagnosed root cause #3 — prompt
mutation on retry — now observed in the act, and it is more damaging than
the provider-switching cause that was originally emphasised.

**Two corrections to earlier analysis, recorded because both were wrong:**

1. This was first attributed to "hop-target selection" by the planner. It is
   not: 6 of 6 isolated trials produced the correct two-hop path on a single
   attempt.
2. It was then attributed to provider switching (the local model server was
   observed returning HTTP 000 during the 20-run test). That is real and did
   occur, but the second run reproduced the same 1-in-6 pattern with
   `attempts` — not backend — as the discriminator.

**What makes this diagnosable at all** is the P0.2 provenance
instrumentation added in Phase 1. Without `attempts`/`retried` in the
receipt, this would have been read as planner flakiness, which is the wrong
conclusion and would have produced the wrong fix.

**Recommended action — the decision this report is asking for.** The repair
is in shared infrastructure (`json_extract.call_llm_json`), used by many
callers beyond this pipeline. Options, in order of preference:

1. **Make the retry non-mutating for schema-shaped callers** — re-send the
   original prompt rather than an appended correction. Smallest change with
   the largest effect on this failure.
2. **Tighten `_validate_payload` to require `grain`** so the missing field
   is caught as a shape error the model can correct without being pushed
   toward a simpler shape. Narrower, and does not touch shared code.
3. Accept the current answer rate and let manual QA proceed, treating
   refusals as the expected outcome for multi-hop questions.

## Finding D — planner duplicates an edge filter as a node predicate

**Severity: LOW. Status: NOT FIXED.**

SEM-3 (victims) emits the role filter both on the traversal (correct) and as
a node predicate `role eq victim` (invalid — `Person` has no `role` field).
The validator refuses. Deterministic, isolated, fails safe. A prompt-clarity
issue: rule 7b explains where an edge property goes but does not say it must
not *also* appear in `predicates`.

## Finding E — the `Case` coupling in the composite layer

**Severity: INFORMATIONAL. Status: PRE-EXISTING, not a defect.**

`registry_case_link` contains the literal `"Case"`. This is structural to
the layer rather than a hardcoded special case: the temporal authority
resolves a window to *case ids* (`Constraint.key = "case_id"`), so the
subject→Case bridge is inherent to the design, and the pre-existing
`plan_link` already carried the same literal. Recorded for transparency
rather than repaired.

---

# Regression Testing

Every Phase 1 issue, re-verified in this audit.

| Issue | Original failure | Expected | Current | Status | Evidence |
|---|---|---|---|---|---|
| P0.1 silent substitution | QA-013 answered 206, marked verified | correct value or refusal | **92**, `independently_verified: False` | **FIXED** | confirmed against graph; SEM-1..5 |
| P0.2 non-determinism | 6 queries varied | stable or explained | 1 query varies; cause diagnosed | **PARTIAL** | Finding C |
| P0.3 schema card | `role` invisible | dynamic discovery | novel property reached planner | **FIXED** | injection test |
| P1.1 direction | QA-007 refused 3/3 | corrected or refused | answers 208 ×3 | **FIXED** | adversarial suite |
| P1.2 compound | QA-011 answered 76 silently | refuse, name both | `multiple_outputs_requested` | **FIXED** | QA-008, QA-011, QA-014 |
| P2.1 datetime crash | `ValueError` | no crash | correct date ×3 | **FIXED** | 0 crashes in 75+ runs |
| P2.2 case link | false "no path" | true message or resolve | names both candidates | **FIXED** | 4-branch test |
| P3.1 distinct values | unexpressible | deferred | still refuses | **DEFERRED** | as planned |

---

# Blind Query Testing

Seven unseen questions across the required categories.

| Category | Question | Result |
|---|---|---|
| Counting | "How many cases exist?" | **73 ×3** ✓ |
| Relationship-constrained | "How many people have role witness?" | **37 ×3** ✓ correct |
| Time-based | "How many incidents occurred after 2024?" | REFUSED — ambiguous Incident→Case path |
| Multi-hop | "How many people are connected through these cases?" | REFUSED — 3 hops > 2 supported |
| **Unsupported** | "How many people are left-handed?" | **REFUSED ×3** ✓ |
| Categorical | "Break down weapons by their licence status" | REFUSED — 50% NULL group keys |
| Min/max | "What is the most recent incident date?" | REFUSED — Finding B |

**The hallucination test passed cleanly.** "Left-handed" produced an honest
self-report — *"The schema does not include a 'handedness' property for the
Person node label"* — and `constraint_lost` refused it. Repeated 20 times in
the stability run: **20 of 20 refused**, never an invented number.

Every refusal above carries a specific, truthful reason. None is a generic
failure.

---

# Determinism / Stability Testing

```
Query:  "How many accused persons are linked to cases?"
Runs:   20 (plus 6 in a second session)
Plan consistency:    2 distinct specs; 1 correct, 19 wrong
Result consistency:  1 ANSWERED (92, correct), 19 REFUSED
Discriminator:       attempts=1 -> correct; attempts=2 -> wrong (perfect correlation)
Issues:              Finding C
```

```
Query:  "How many people are left-handed?"      (unsupported)
Runs:   20
Plan consistency:    2 specs, both leading to refusal
Result consistency:  20/20 REFUSED
Refusal stability:   19 constraint_lost, 1 spec_invalid
Issues:              none — unsupported input fails safe every time
```

The asymmetry is the important result: **unsafe input is refused with
complete reliability; a hard-but-answerable question is answered
unreliably.** The system errs toward refusing, which is the correct
direction to err, and the cost is answer rate rather than correctness.

---

# Code Quality Review

| Check | Result |
|---|---|
| TODO / FIXME / HACK markers in Phase 1 code | none |
| debug prints, breakpoints | none |
| domain values (`accused`, `cnic`, …) in logic | none |
| entity/relationship names in new logic | only `"Case"` — Finding E |
| question-text keyword inspection | none |
| broad `except` clauses | 2, both logged and both degrade rather than crash |
| malformed-input robustness | `fidelity.check` survives `where=None`, `requested_outputs=None`, missing generation |
| duplicated logic | `direction._resolves` duplicates `validator._lookup_traversal` **deliberately**, documented: if the two disagreed, corrections would be made against a rule the validator does not apply |
| tests | 46 in `test_aggregate_fidelity.py`; full project suite green |

One pre-existing test was updated: `test_age_is_visible_on_the_card`
asserted `".age" in card`. Its subject — a sparse property staying visible —
still holds; the assertion had encoded the dotted rendering that was removed
because the planner copied it into specs verbatim.

---

# Architecture Assessment

**1. Are the fixes generic?** Yes, with one exception now removed. Every
Phase 1 fix is driven by measurement or structure. The novel-property
injection test demonstrates this end to end. The one hardcoded table found
in the supporting layer (Finding A) has been replaced with registry lookups.

**2. Are the fixes metadata-driven?** Yes. Edge properties, edge values,
relationship paths and traversal directions all resolve from the registry
snapshot. Adding a relationship, an edge property or a role value to the
graph requires no code change — which is now proven rather than claimed.

**3. Are there hardcoded cases?** One was found and removed (Finding A).
One literal remains (`"Case"`, Finding E) and is structural to the composite
layer rather than a special case. No question-specific conditions, QA-ID
checks, keyword matching or hardcoded roles exist anywhere in the Phase 1
code.

**4. Is the LLM trusted for deterministic logic?** No, and this audit
strengthens that claim. The LLM interprets and declares; deterministic code
validates the field, the value, the direction, the path, the coverage and
the fidelity of the spec to the declaration. Finding C is the proof: when
the LLM produced a degraded spec, the deterministic layer refused it rather
than executing it. **The system failed safe under a condition that
previously produced confident wrong answers.**

**5. Is the architecture scalable?** Structurally yes. The measured limits
are: one edge-property filter per hop, two relationship hops, no
`distinct_field`, and the AGE route cannot express edge filters at all —
which means role-qualified questions cannot be independently verified. That
last one is the most significant architectural gap and is honestly reported
by `independently_verified: False` rather than hidden.

**6. Is the system ready for manual QA?** Yes for safety, with a caveat on
throughput. No silent wrong answers were produced in any test in this audit.
Every failure was a refusal with a truthful, specific reason. But manual QA
on multi-hop questions will see refusals far more often than answers until
Finding C is addressed, and testers should be told that a refusal is the
expected outcome rather than a bug to re-file.

---

# Final Recommendation

## Phase 1 Requires One Additional Fix Before Closure

The seven Phase 1 issues are verified fixed and the two defects this audit
found are repaired. Nothing unsafe ships, and the safety properties are
stronger than the Phase 1 report claimed — the system refused every
degraded spec it was handed, including under a live backend failure.

Closure is held on **Finding C** alone: a prompt-mutating retry in shared
infrastructure systematically converts a correct multi-hop interpretation
into a wrong single-hop one, reducing the answer rate on answerable complex
questions to roughly 1 in 6. The cause is understood and the correlation is
perfect across 26 runs in two sessions.

**Recommended:** apply option 2 from Finding C — require `grain` in
`_validate_payload` so the shape error is caught without pushing the model
toward a simpler spec. It is contained within `nl_spec.py`, touches no
shared code, and is testable in isolation. Re-run the 20-repetition
stability test; if the answer rate rises materially, close Phase 1 and begin
manual QA.

**If instead the decision is to proceed now:** that is defensible, because
the failure mode is a refusal rather than a wrong answer. It should be an
explicit decision recorded against this report, and manual QA should be
briefed that multi-hop questions will frequently refuse.

**Do not close on the Phase 1 report's determinism figure.** "24 of 25
deterministic" was measured at 3 runs and does not survive 20. This report
supersedes it.
