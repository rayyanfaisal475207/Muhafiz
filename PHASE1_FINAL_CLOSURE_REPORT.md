# Phase 1 — Final Closure Report

Final stabilization and pre-QA verification of the aggregate architecture.
This report closes the development phase: it completes the one outstanding
item from closure verification (retry degradation), re-verifies the Phase 1
fixes, and states plainly what is proven, what is inferred, and what is not
yet known.

---

# Executive Summary

**Development phase: COMPLETE. Recommendation: A — move to manual QA.**

The blocking issue identified in closure verification is fixed. The query
that previously answered **1 run in 20** now answers **20 of 20**, with a
single spec hash, a single value, and zero retries.

```
                     answered   retries   distinct specs
before fix (20)        1/20        19           2
before fix  (6)         1/6         5           2
after  fix (20)       20/20         0           1
```

Two qualifications a reviewer should weigh, both stated in full below:

1. **The causal attribution is partial.** Zero retries fired in the
   post-fix run, so the repair path was never exercised in production
   there. A forced-retry experiment did not reproduce the degradation, so
   the improvement is *consistent with* the fix rather than isolated to it.
   The fix is correct by construction and unit-proven; the field evidence is
   correlational.

2. **Live re-verification is incomplete.** Docker Desktop stopped partway
   through this phase, taking the Postgres/AGE instance down, so the final
   semantic regression re-run could not complete. The full offline suite is
   green and every earlier live result stands, but the semantic set was last
   measured *before* the retry fix.

Neither qualification indicates a defect. Both are stated so that the
decision to freeze is made on what is actually known.

---

# Completed Improvements

## 1. Retry degradation (the closure blocker)

**Problem.** A valid complex interpretation, when a retry fired, came back
simplified and was refused:

```
attempt 1:  Person -INVOLVED_IN-> Incident -BELONGS_TO_CASE-> Case   -> 92
attempt 2:  Person -BELONGS_TO_CASE-> Case                           -> refused
```

**Root cause — established by reading the code, not inferred.**
`call_llm_json` preserves only `user_message` across attempts. The model's
previous output is **discarded entirely**, so a retry is not a formatting
repair: the model re-reads the question and re-decides the answer's shape.
The appended correction then tells it not to invent "a different shape" and
to "make your best judgment call anyway", which biases it toward the
simplest shape that validates. For a payload that encodes an
*interpretation*, that is a semantic regeneration wearing a formatting
fix's clothes.

**Why the fix is opt-in.** `call_llm_json` has **64 call sites across 12+
modules** — extraction, summarization, evaluation, routing. Changing the
correction globally during a closure phase would be exactly the blast
radius this phase forbids. So the behaviour is selected by the caller:

```python
preserve_interpretation=True     # nl_spec.generate_spec only
```

All 63 other call sites are byte-identical to before.

**The fix.** In repair mode the previous reply is quoted back and the model
is asked to repair it:

```
YOUR PREVIOUS REPLY:
{"measure":"count_distinct","entity":"Person","traversals":[...]}

Return that SAME interpretation as valid JSON. Keep every entity,
relationship, traversal, filter and value you already chose — do not
simplify the query, do not drop a constraint, and do not change what is
being counted. Repair only what was malformed or missing.
```

It names no entity, relationship, question or corpus. It improves any
complex interpretation that hits a retry.

**Verification evidence.**

*Unit-proven (deterministic, offline):* the retry prompt carries
`INVOLVED_IN` and `BELONGS_TO_CASE` forward in repair mode and does not in
default mode; a first attempt that validates never triggers a second call;
the echoed reply is length-bounded so a runaway output cannot push the retry
past a request cap. 7 tests.

*Measured live:* 20/20 answered, one spec hash, value `92` every run —
against the same cloud backend and the same ~35s latency profile that
previously produced 19 failures, so a healthier environment does not explain
it. The winning spec hash is **identical** to the one that used to win once
in twenty.

*Not established:* a forced-retry experiment against the live model produced
the correct two-hop path in **both** modes, so it did not reproduce the
degradation and cannot isolate the fix as the cause. Recorded as a known
limit of the evidence.

## 2. Hardcoded role table removed (found during closure verification)

**Problem.** The validator carried a hand-kept table:

```python
_ROLE_BEARING_RELATIONSHIPS = {"INVOLVED_IN": ("role",), "ASSIGNED_TO": ("role",)}
```

**Root cause.** Its own justification — *"the registry measures node
properties, not edge properties"* — stopped being true when Phase 1 made the
registry measure edge properties. A Phase 1 change invalidated this code's
premise without updating it. Behind the table sat a real hole: role
*values* were never checked, so `role_value='NOT_A_REAL_ROLE'` validated as
`EXECUTABLE` and would have returned a confident **0**.

**Solution.** The field is checked against measured `info.properties`; the
value against `snapshot.edge_property_values(...)`, populated by a bounded
registry measurement. An unenumerated property constrains nothing — empty
means "not measured", never "no values exist" — so high-cardinality fields
are not falsely rejected.

**Verification evidence.**

```
role_value='NOT_A_REAL_ROLE' -> UNSUPPORTED, naming the six measured values
role_value='accused'         -> EXECUTABLE
role_value=None              -> EXECUTABLE
unenumerated property        -> EXECUTABLE (constrains nothing)
```

A guard test now fails if any relationship name reappears in validator
*code* (comments are allowed — documentation is not behaviour).

## 3. Earlier Phase 1 fixes, re-verified

| Fix | Evidence | Status |
|---|---|---|
| Silent substitution (P0.1) | QA-013: 206 → **92**, confirmed by direct graph query; five adjacent semantic questions produced five different interpretations | **VERIFIED** |
| Schema awareness (P0.3) | a property that never existed (`qa_probe_flag='zeta'`) was injected live and reached the planner with correct sparse presence, **no code change**, then removed | **VERIFIED** |
| Result type safety (P2.1) | count / zero / None / datetime / categorical / bool / negative all handled; QA-018 returns the correct date with coverage *"Computed over the 64 of 73 record(s)…"* | **VERIFIED** |
| Registry resolution (P2.2) | unique → resolves (Person, Weapon, Document); ambiguous → refuses naming candidates (Incident, Officer); none → honest | **VERIFIED** |
| Direction reconciliation (P1.1) | corrects a unique reverse; refuses to touch wrong relationship type, wrong target label, or an ambiguous pair — confirmed live on QA-012 | **VERIFIED** |
| Compound detection (P1.2) | `multiple_outputs_requested` naming every figure asked for | **VERIFIED** |
| Provenance (P0.2) | `attempts`/`retried` in every receipt — **this instrumentation is what made the retry defect diagnosable at all** | **VERIFIED** |

---

# Architecture Assessment

**1. Is the system generalized?** Yes. The grammar is 15 `AggregateSpec`
fields. A new question is a new combination of values, not new code. Static
scan confirms no `question_kind`, no intent classifier, no per-question
dispatch anywhere in the package — the only matches for those terms are
docstrings stating their deliberate absence.

**2. Are fixes metadata-driven?** Yes. Edge properties, edge values,
relationship paths, traversal directions and role validity all resolve from
the registry snapshot. The novel-property injection test demonstrates this
end to end rather than asserting it.

**3. Are there hardcoded solutions?** One was found during closure
verification and removed (item 2 above). A static scan of the new modules —
`fidelity.py`, `direction.py`, `json_extract.py` — returns **zero** entity
names, relationship names, domain values, QA identifiers or corpus
references in code. One literal remains: `"Case"` in `composite_executor`,
which is structural to that layer (the temporal authority resolves windows
to *case ids*) and pre-dates Phase 1.

**4. Can new aggregate query types be added through reusable
capabilities?** Yes, with measured limits. A new relationship, edge
property or role value requires no code change — proven by injection. What
would require code: a second edge-property filter on one hop, more than two
hops, `distinct_field`, and edge filters in the AGE verification route.

**5. Are failures handled safely?** Yes, and this is the strongest result
of the phase. Across every test in closure verification and this phase, no
silent wrong answer was produced. Every failure was a refusal with a
specific, truthful reason. The unsupported-question control refused **20 of
20** without inventing a number. When the model produced a degraded spec
under a failing backend, the deterministic layer refused it rather than
executing it — the architecture failed safe under precisely the condition
that used to produce confident wrong answers.

---

# Testing Evidence

| | |
|---|---|
| Full project suite | **green, 0 failures** (run after every change in this phase) |
| Aggregate-specific tests | 46 in `test_aggregate_fidelity.py` |
| Retry-semantics tests | 7 new, in `test_json_extract.py` (20 total) |
| Stability | 20 runs × 2 queries, post-fix |
| Blind categories | 7 unseen questions across counting / relational / temporal / multi-hop / unsupported / categorical / min-max |
| Semantic adjacency | 5 unseen questions (accused / witness / victim / suspect / involved) |

**Stability results.**

```
Query:            "How many accused persons are linked to cases?"
Runs:             20
Spec consistency: 1 distinct spec hash (was 2)
Answer consistency: 20/20 = 92 (correct; confirmed against the graph)
Unexpected:       none

Query:            "How many people are left-handed?"   (unsupported)
Runs:             20
Spec consistency: 2 specs, both refusing
Answer consistency: 20/20 REFUSED
Unexpected:       none — never hallucinated a number
```

**Previously failing cases.** All Phase 1 regressions re-verified: QA-013
(206 → 92, correct), QA-018 (crash → correct date), QA-007 (refused →
answered), QA-011/QA-008 (silent partial → named refusal), QA-022/QA-024
(silent wrong → refusal).

---

# Remaining Limitations

**Unsupported capabilities**

1. **One edge-property filter per hop.** `Traversal` carries a single
   `role_field`/`role_value`. A question needing a role *and* an arrest
   status is unrepresentable and refuses.
2. **Two relationship hops maximum.**
3. **No `distinct_field`.** Counting distinct values of a non-identity
   property (distinct CNICs) is unexpressible; the validator refuses rather
   than undercounting. Deferred by design.
4. **The AGE route cannot express edge filters.** `MatchPattern` has node
   aliases only. Role-qualified questions therefore **cannot be
   independently verified**, and the system reports
   `independently_verified: False` rather than hiding it. This is the most
   significant architectural gap.

**Known constraints**

5. **Hop anchoring.** The planner sometimes reaches the right relationship
   and anchors it to the wrong entity. It refuses safely. Deterministic
   auto-correction is possible — the registry often has exactly one
   candidate target — but changing a hop's *target* alters which entities
   are traversed, which is a capability change, not a stabilisation fix.
6. **`value_field` resolves against logical fields, not node properties.**
   `incident_date` is valid for a `time_window` but is not a node property,
   so `max(n.incident_date)` returns NULL and refuses with `null_result`
   rather than an accurate diagnosis. Pre-existing; confirmed untouched by
   Phase 1.
7. **Edge-property duplication.** The planner occasionally emits a role
   filter both on the traversal and as a node predicate; the second is
   invalid and the spec refuses. Prompt clarity, fails safe.
8. **Determinism is mitigated, not guaranteed.** Provider fallback remains
   reachable and no seed is sent to the local endpoint. Provenance now makes
   a backend switch visible after the fact.
9. **The pipeline is not wired to `/api/chat`.** Live traffic still uses the
   legacy `xagg.run_aggregate` engine. QA against this architecture measures
   something users cannot currently reach; QA against the product measures
   the legacy engine. This should be an explicit decision.

**Environment note.** Docker Desktop stopped during this phase, taking
Postgres/AGE down. The final semantic regression re-run could not complete,
so that set was last measured before the retry fix. The offline suite is
green and no code depends on the outcome, but it is outstanding evidence.

---

# Final Recommendation

## A — Phase 1 complete. Move to manual QA / gold-set testing.

**Reasoning.**

The closure blocker is resolved: 1/20 → 20/20, deterministic in plan,
execution and answer. Every Phase 1 fix is re-verified, and the one
hardcoded artefact found during verification has been removed along with the
correctness hole behind it. The full project suite is green after every
change.

The decisive argument is not the answer rate but the **failure mode**.
Across every test in this phase and the last, the system did not once
produce a silent wrong answer. It refused, with a specific and truthful
reason, including under a live backend failure that fed it a degraded spec.
That is the property that makes manual QA productive: a tester can trust
that a number is either right or absent, and a refusal message names what
was missing.

**Two things QA should be briefed on.**

- **A refusal is often the correct outcome**, not a bug to re-file.
  Multi-hop questions, questions needing two edge filters, and questions
  naming properties the schema lacks will all refuse by design. The refusal
  text states which.
- **`independently_verified: False` on role-qualified questions is
  expected**, because the verification route cannot express edge filters. It
  is honesty, not failure.

**Two items to schedule before or alongside QA.**

- Re-run the semantic regression set once the database is back, to close the
  one piece of outstanding evidence.
- Decide explicitly whether gold-set QA runs against this pipeline or the
  legacy engine that `/api/chat` actually uses. Running it against this
  architecture measures work users cannot yet reach.
