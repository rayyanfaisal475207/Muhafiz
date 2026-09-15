# Phase 1 — Shadow execution and equivalence validation

**Status: analysis complete. Production unchanged.** No cutover, no
deletions, no modification to `router.py`, `supervisor.py`, `xagg.py`'s
dispatch, or the RAG path. The new engine runs only from the harness.

Measured live against the production database, 2026-09-15.

---

## 1. Headline finding

The shadow run found **four wrong-number bugs in the NEW engine** and
**two misroutes plus one identity-grain divergence in the OLD engine**. It
found them by running both against live data and disbelieving the output —
not by testing that the two agree. Had the harness been written to make NEW
match OLD, three of the four NEW bugs would have passed silently, because
each produced a plausible integer.

That is the argument for this phase existing, and the reason the brief's
"do not treat OLD as ground truth" rule mattered in practice rather than in
principle.

---

## 2. Shadow harness files

| File | Role |
|---|---|
| `src/pipeline/aggregate/executor.py` | Runs a validated spec: compile → execute → **shape-check → coverage → receipt**. Refuses rather than repairs. |
| `src/pipeline/aggregate/shadow.py` | Specs per kind, legacy invocation, deterministic classification. |
| `scripts/run_aggregate_shadow.py` | Runner; writes `docs/aggregate-shadow/phase1_shadow_report.json`. |
| `tests/test_aggregate_shadow.py` | Regressions for every bug below. |

The harness calls `xagg.run_aggregate()` as an ordinary caller would. It
imports nothing from `router.py` or `supervisor.py` — asserted by a test.

---

## 3. Bugs found in the NEW engine (all fixed, all pinned)

### BUG 1 — counted entity was the traversal's terminal, not the population's subject

A traversal **filters** the subject; it does not change it. "Persons who
belong to a case" counts Persons. The compiler was emitting
`count(DISTINCT m0.entity_id)` over the *Case* nodes the hop landed on.
Case carries no `entity_id`, so the query returned **0** — valid Cypher, no
error, a confident wrong answer. Correct: **208**.

The validator had the same error mirrored, and was refusing every correct
spec of this shape (`"entity_id does not identify a Case"`). Both now agree
that the measured entity is `population.entity`.

### BUG 2 — value measures compiled as counts

`avg` specs were reaching the grain branch first and emitting
`count(DISTINCT n.entity_id)` → **68**. Right rows, wrong arithmetic.
Measure is now honoured before grain: `avg(n.age)` → **31.84**.

Related, and deliberately strict: `avg` over a **fanning** population is
now refused. A fanning hop repeats each value once per edge, silently
weighting the mean by degree.

### BUG 3 — grouping dimension used the wrong alias

Grouping cases by station produced 19 groups each with **count 0**. A
dimension reached `via` a hop lives on the terminal node; the measure stays
on the base. Now: 19 groups, top = 7 cases.

### BUG 4 — coverage mixed two populations

Printed `"411 of 73 record(s) carry no Person.age (563%)"`. The registry
measures presence over the whole label (430 Persons); the query's
population was 73. Absence is now a **rate** scaled to the observed
population, and `absent_share` is clamped to ≤ 1.

---

## 4. Old-vs-new comparison (6 generic kinds)

| Kind | OLD | NEW | Classification |
|---|---|---|---|
| `total_count` | 73 | 73 | **AGREE** |
| `station_total_count` | 19 | 19 | **AGREE** |
| `graph_recurrence_person` | 4 | 4 | **AGREE** |
| `total_accused_count` | 4 | 92 | **SEMANTICALLY_DIFFERENT** (legacy misroute) |
| `placeholder_officer_count` | None | 1155 | **SEMANTICALLY_DIFFERENT** (legacy misroute) |
| `graph_recurrence_weapon` | 1 | 0 | **SEMANTICALLY_DIFFERENT** (identity grain) |

**AGREE 3 · SEMANTICALLY_DIFFERENT 3 · UNRESOLVED 0 · NEW_CORRECT_OLD_WRONG 0 · OLD_CORRECT_NEW_WRONG 0 · BOTH_WRONG 0**

No divergence required asserting that either engine was arithmetically
wrong. Every one resolved to a *routing* or *grain-of-identity*
difference — which is a stronger result than a win for either side.

### 4.1 Legacy misroutes — Module 173 observed live

`resolve_aggregate_kind()` is first-match-wins and a match ends dispatch:

| Question | Intended | Actual |
|---|---|---|
| "How many accused persons are there in total across all cases?" | `total_accused_count` | **`graph_recurrence_person`** |
| "How many officers are recorded?" | an officer count | **`station_or_category_counts`** |

The first returns **4** (people appearing in more than one case) for a
question asking for a **headcount of 92**. The legacy arithmetic is
correct; it is answering a different question. This is exactly the
false-positive side your teammate filed as Module 173 and left unfixed, now
reproduced with live numbers.

### 4.2 Weapon recurrence — both right, different questions

`xagg._top_recurring_weapon_types()`'s own docstring resolves this without
my judgement: weapon `entity_id`s are FIR-scoped by construction
(`WEAPON-{id}-{fir_id}`), weapons never go through the cross-case merge
tier, so "every real Weapon node belongs to exactly one case, permanently"
and per-entity grouping "would always return an empty list — confirmed
against the live graph".

Verified here: 32 nodes, **32 distinct entity_ids, 0 in more than one
case**, 5 distinct `canonical_name`s. NEW's **0** answers "which weapon
*object* recurs". OLD's **1** answers "which weapon *type* recurs" (after
normalising the `بمعہ N گولیاں` suffix).

---

## 5. Adversarial cases

| Case | Result |
|---|---|
| Officer grain, ENTITY | **5.5 %** (4/73) — the mandated figure |
| Officer grain, RELATIONSHIP | **95.9 %** (70/73) — the edge reading, reachable only by asking for it |
| Person via `BELONGS_TO_CASE` | **208** (tombstones excluded) |
| Case→Person, no distinct key | **REFUSED** `missing_distinct_key` + fanout |
| Case→Address, no distinct key | **REFUSED** (reverse fanout 2,100) |
| Case→Person, keyed | **73** |
| Case+Person+Weapon | **REFUSED** `unknown_relationship` (correctly: no Person→Weapon edge) |
| Null grouping | **19 correct groups** (was the `{None: 73}` failure) |
| `avg` over sparse age, fanning | **REFUSED** `compile_failed` |
| Non-supervisor cross-case | **REFUSED** `scope_denied` |

The 4-vs-70 distinction is **declared in the spec**, not hidden in an
implementation: the two readings are separate `count_grain` values and
compile to different Cypher.

---

## 6. Tombstones, supersession, canonicalization

- **Tombstones.** 222 of 430 Person nodes carry `merged_into`. NEW injects
  `merged_into IS NULL` and cannot be asked not to. 429 → **208**.
- **Supersession.** NEW injects `superseded_by IS NULL` on versioned
  relationships. OLD applies it at **1 of 71** Cypher sites, with 41
  traversing versioned edges.
- **Canonicalization.** Policy remains **`BOTH_AND_COMPARE`**. The 139- and
  85-node components stay visible as unresolved provenance; nothing here
  trusts them. No supervisor decision was pre-empted.

---

## 7. Gold-32 control

The gold set is **not an aggregate benchmark**: 31 of 32 answers are prose
(median length 385 chars); exactly **one**, D1, is a bare number.

| ID | Expected | NEW | Match |
|---|---|---|---|
| D1 | 73 | **73** | ✅ exact |

The other 31 are Complex Reasoning (8), Knowledge Base (8), Contextual
Summarization (5), Creative Generation (5) and prose Fact Retrieval (5) —
multi-element narrative answers that no single aggregate produces. Forcing
them through the engine would measure nothing. **No gold expectation was
modified, and no gold test was touched.**

---

## 8. Latency baseline

| Stage | Measured |
|---|---|
| Registry build (cached per process) | ~11 s |
| NEW compile | **< 1 ms** |
| NEW execute, simple count | 51–96 ms |
| NEW execute, relation-count predicate | ~1,050 ms |
| NEW ratio (two queries) | 114–122 ms |
| OLD execute | 8–532 ms |
| Receipt generation | below measurement noise |

NEW is comparable to OLD on simple counts and slower on the recurrence
predicate. The registry build dominates and is amortised across a process;
no optimisation attempted, per the brief.

---

## 9. Tests

- **`tests/test_aggregate_shadow.py` — 17 new tests**, one per bug plus the
  harness-isolation and classification-vocabulary guards.
- `tests/test_aggregate_engine.py` — 68, unchanged.
- **Full suite: 3,507 tests, 0 failed, 0 errors, 5 skipped.**
- `test_xagg.py`'s 527 untouched. Gold-32 control untouched. Nothing
  weakened.

---

## 10. Open items for supervisor review

1. **The 222 SAME_AS confirmations** (139/85 components, all
   `flagged_unverified`, 156 by one account) — unchanged from Phase 0 and
   still blocking a canonicalization decision.
2. **Legacy misroutes.** Two of six probe questions misroute today. This is
   Module 173 and it affects the *production* path, not just the new
   engine. Worth filing regardless of what happens to this project.
3. **Coverage thresholds** (5 % / 50 %) remain proposed, not validated.

## 11. What this phase did NOT establish

- That an LLM can produce these specs from natural language. Not attempted;
  every spec here is hand-written.
- That the remaining 37 kinds agree. Six were compared; the rest need specs
  written, and the GENERIC+OP ones need their operators first.
- That NEW is correct in general. It is correct on these cases, with
  receipts. Four bugs in one session is the relevant base rate.
