# Phase 2 — Broad empirical shadow evaluation

**Status: complete. Production unchanged.** No routing change, no deletions,
no modification to `router.py`, `supervisor.py`, `xagg.py` dispatch or the
RAG path. Measured live, 2026-09-15.

---

## 1. Headline finding

Phase 2 found a **silent-ignore defect in the new engine that is the exact
bug the engine was built to prevent**.

`AggregateSpec` declared `compare`, `time_window` and `threshold`, and
`MAX_GROUP_DIMENSIONS = 2` permitted a cross-tab. The compiler consumed
**none** of the first three and only `group_by[0]`. So this spec —

> count cases, restricted to 2024

— validated as `EXECUTABLE`, compiled to

```
MATCH (n:Case) RETURN count(DISTINCT n.case_id) AS value
```

byte-identical to the spec with no time window at all, and returned **73**,
the grand total — **while the receipt asserted a filter had been applied**.

That is Module 144's defect (*"How many FIRs were registered in 2019 or
earlier?"* → 73) reproduced inside the engine designed to make it
impossible, and strictly worse, because the receipt lent it false
provenance.

**Fixed at the validator layer**, not the compiler's: a field the compiler
cannot emit is a *specification that cannot be honoured*, and refusing
those is the validator's entire purpose. Silently dropping a filter is the
one failure mode this package exists to eliminate.

---

## 2. Corpus results — 30/30

| Verdict | Count |
|---|---|
| `PASS` (value matched independent ground truth) | **16** |
| `NEW_REFUSED_CORRECTLY` (guard fired, with the right code) | **14** |
| `WRONG_VALUE` | 0 |
| `NEW_REFUSED_INCORRECTLY` | 0 |
| `REFUSED_WRONG_REASON` | 0 |
| `UNRESOLVED` | 0 |

Refusals are counted **separately** from values throughout. A refusal
proves a guard; it does not prove a capability, and the report does not let
one stand in for the other.

---

## 3. Independent ground truth

No expected value was derived from this package's compiler. Three routes
were used and, where they overlap, they agree:

| Route | Used for | Example |
|---|---|---|
| **SQL** | direct Postgres over `cases` | 73 cases, 19 stations, 64 dated, 13 in 2024, 51 in 2026 |
| **GRAPH** | hand-written Cypher, traversal shapes deliberately unlike the compiler's | 92 distinct accused vs 94 accused edges; 30 unlicensed weapons; 45 malkhana records |
| **PYTHON** | raw rows fetched with *no aggregation in Cypher*, counted in Python | ages n=19 min 24 max 49 mean 31.8421 median 31 sum 605; 4 of 73 cases with >1 distinct officer; 430 Persons = 208 active + 222 tombstoned |

`GroundTruth.route` and `.note` travel with every case, so any figure can be
re-derived without trusting the corpus.

---

## 4. Coverage — proven, by axis

**Measures** — `count` 16, `count_distinct` 8, `avg` 2, `min` 1, `max` 1,
`sum` 1, `median` 1 (refused).
`min`/`max`/`avg`/`sum` verified against Python over raw rows: 24 / 49 /
31.842105263157894 / 605.

**Population shapes** — direct 17, fanning 4, relationship-grain 2,
through-relationship 2, property-filter 2, filtered 2,
relationship-existence 1.

**Grouping** — none 26, via-traversal 1 (19 station groups, top = 7),
sparse 1 (refused), invalid 1 (refused), multi 1 (refused).

**Derived** — ratio 3, threshold 1, comparison 1 (refused).

**Guards** — fanout 6, sparse-property 5, invariant 4, unimplemented 3,
tombstone 2, scope 2, null-grouping 1, zero-denominator 1.

### The mandated grain case, both readings

| Reading | Result | Route |
|---|---|---|
| ENTITY grain (distinct officers) | **5.47945205479452 %** (4/73) | PYTHON |
| RELATIONSHIP grain (assignment edges) | **95.89041095890411 %** (70/73) | GRAPH |

Both are reachable **only by asking for them** — `count_grain` is declared
in the spec and compiles to different Cypher. The 67 duplicate
`(case, officer)` pairs are one officer in two roles.

---

## 5. Compositionality

Every case is **bindings over the shared operators**. No case required a
new emitter, a question-specific function, or a template. Concretely, the
same tree answered:

- *% of cases with >1 officer* (`ASSIGNED_TO`, `Officer`)
- *cases with >1 weapon* (`BELONGS_TO_CASE`, `Weapon`) → 0, correctly
- *distinct accused* vs *accused involvements* (grain alone)

`test_specs_use_only_the_shared_operator_algebra` is where a regression
would surface.

---

## 6. Bugs found and fixed

| # | Defect | Layer | Evidence |
|---|---|---|---|
| 1 | `compare` / `time_window` / `threshold` silently ignored | validator | compiled byte-identical to baseline; returned 73 for a 2024-filtered spec |
| 2 | Second `group_by` dimension silently dropped | validator | cross-tab collapsed to one dimension |
| 3 | `median` raised `CompilerError` at execution instead of refusing | validator | now a stated refusal |
| 4 | **Corpus fixture** held `round(100*4/73, 4)` = 5.4795 against a correct engine result of 5.47945205479452 | test fixture | would most easily have been "fixed" by widening tolerance, masking real drift |

Bug 4 is worth naming: the engine was right and my fixture was lossy. It is
pinned by `test_ratio_ground_truth_is_exact_not_rounded` precisely because
the tempting fix was the wrong one.

---

## 7. Proven / suggested / not covered

### Proven (executed and independently verified)
`count`, `count_distinct`, `min`, `max`, `avg`, `sum`; direct, filtered,
through-relationship, relationship-existence, fanning and relationship-grain
populations; single-dimension grouping incl. via-traversal; ratios;
thresholds over relation counts; tombstone and supersession exclusion;
fanout refusal; scope enforcement; zero-denominator refusal.

### Suggested but unverified (architectural proposals, not validated)
Coverage thresholds (5 % / 50 %); `MIN_GROUPING_FIELD_PRESENCE = 0.50`;
`MIN_RATIO_DENOMINATOR = 5`; `MATERIAL_DIFFERENCE_RATIO = 0.05`;
`MAX_CANON_COMPONENT`; `BOTH_AND_COMPARE` as the Person default. All are
policy choices with stated reasons, none empirically tuned.

### Not yet covered
`median`; comparisons; time windows; standalone thresholds; multi-dimension
grouping; `top_n` (declared and compiled but no corpus case); `ROLE_PAIR`
grain end-to-end; `date_diff` / `date_bucket` / `numeric_bucket` /
`count_absent` / `set_difference`; the 10 Phase 0 specialists; Postgres as
an execution backend (all cases ran against AGE).

---

## 8. Regression status

**Full suite: 3,523 tests — 3,518 passed, 0 failed, 0 errors, 5 skipped**
(RLS integration needing a non-superuser role).

| File | Tests |
|---|---|
| `test_xagg.py` | **527 — unchanged** |
| `test_aggregate_engine.py` | 68 |
| `test_aggregate_shadow.py` | 17 |
| `test_aggregate_corpus.py` | **16 new** |

One existing test changed: `test_disjoint_buckets_allowed` asserted that a
`compare` spec *validates*. That assertion **encoded the defect** — it is
now `test_disjoint_buckets_still_refused_because_compare_is_not_emitted`,
and it flips back automatically when `compare` is implemented. No test was
weakened to make new code pass; no gold expectation was touched.

---

## 9. Stop-condition answers

**1. Which semantics are proven?** The six measures above, six population
shapes, single-dimension grouping, ratios, thresholds — each against an
independent route, plus fourteen guards firing with the correct code.

**2. Which remain unsupported?** Section 7's "not yet covered" list.
Critically, these are now **refused rather than silently ignored**.

**3. What correctness bugs were found?** Four (§6): three silent-ignore
paths in the engine, one lossy fixture.

**4. What kind of problems are the remaining failures?** None are
abstraction failures. `compare`/`time_window`/multi-dim grouping are
**compiler gaps** — the algebra expresses them, the emitter does not.
`median` is a **backend limitation** (AGE cannot compute it). The
specialists are **semantic**: schema-shape claims and narrative chains are
not counts. No case revealed a missing operator.

**5. Is the algebra sufficient for NL→Spec?** **For the proven subset,
yes** — 30/30 with independent verification, and composition demonstrated
across three unseen question shapes. **But I would not start NL→Spec yet.**
A spec generator will emit `time_window` on its first date question; today
that refuses cleanly, which is safe but useless. The gap is in the emitter,
not the representation.

**6. Smallest next architectural step?** **Implement `time_window` in the
compiler**, using the logical-field authority already in the registry
(`incident_date` → Postgres, 64/73). It is the most commonly-asked filter,
it is fully specified, it has independent ground truth ready (13 cases in
2024, 51 in 2026), and it removes the largest single refusal class before
any LLM is involved.

---

## 10. Open items unchanged from Phase 0/1

1. The **222 SAME_AS confirmations** (139/85 components, all
   `flagged_unverified`, 156 by one account) — still blocking a
   canonicalization decision. `BOTH_AND_COMPARE` retained; nothing
   pre-empted.
2. **Legacy misroutes** — two of six Phase 1 probe questions misroute on the
   *production* path (Module 173). Unaffected by this phase, still worth
   filing.
3. **Coverage thresholds** remain proposed, not validated.
