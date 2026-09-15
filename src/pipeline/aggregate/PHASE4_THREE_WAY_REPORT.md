# Phase 4 — Three-way aggregate reasoning architecture

**Status: architecture proven, evaluation-capable. Production untouched.**
No change to `router.py`, `supervisor.py`, `xagg.py` dispatch or the RAG
path. Measured live, 2026-09-15.

---

## 1. Files added / modified

| File | Role |
|---|---|
| `src/pipeline/aggregate/routes.py` | **new** — 4A common contract |
| `src/pipeline/aggregate/route_structured.py` | **new** — 4B adapter |
| `src/pipeline/aggregate/cypher_guard.py` | **new** — 4C deterministic guards |
| `src/pipeline/aggregate/route_age.py` | **new** — 4C text-to-Cypher route |
| `src/pipeline/aggregate/route_semantic.py` | **new** — 4D evidence route |
| `src/pipeline/aggregate/reconcile.py` | **new** — 4E reconciliation |
| `scripts/run_aggregate_three_way.py` | **new** — 4F harness |
| `tests/test_aggregate_three_way.py` | **new** — 44 tests |

No existing aggregate-engine module was modified. The structured route is
reached by calling `executor.execute()` unchanged.

---

## 2. Common contract (4A)

`NumericResult` carries `value` **plus** `interpretation`, `grain`,
`population`, `grouping`. `comparable_key()` — interpretation + grain +
grouping — is what reconciliation checks *before* comparing values, because
92 distinct accused and 94 accused involvements are both correct answers to
different questions.

`EvidenceResult` has **no `value` field**, and `RouteResult.numeric`
returns `None` for it. That is the structural reason retrieved text cannot
become an aggregate answer — not a convention, an absence.

---

## 3. AGE route and its guards (4C)

`execute_cypher()`'s own contract forbids caller-built query text (AGE's
`cypher()` takes a `cstring` that cannot be bound, so the text is
interpolated into SQL). The guard is what earns the exception:

- **SAFETY** — read-only, single statement, no `$cypher$` delimiter escape,
  no inline literals, explicit RETURN aliases, allowed openers.
- **DIALECT** — the three constructions AGE actually rejects (`EXISTS {}`,
  negated patterns, `ORDER BY <alias>`).
- **SEMANTIC** — registry-measured: non-DISTINCT counts across fanning
  traversals, unexcluded tombstones, unexcluded superseded edges,
  properties the label does not carry.

A flagged query is **REFUSED, never repaired**. `GuardReport` has no
repaired-query field.

---

## 4. Corpus results — 42 cases

### Evaluation (routes vs **independent** ground truth)

| Classification | Count |
|---|---|
| `CORRECT_REFUSAL` | **17** |
| `STRUCTURED_CORRECT_AGE_WRONG` | **22** |
| `STRUCTURED_AND_AGE_AGREE` | **3** |
| `INCORRECT_REFUSAL` | **0** |
| `AGE_CORRECT_STRUCTURED_WRONG` | 0 |
| `BOTH_WRONG` | 0 |

### Reconciliation (routes vs each other)

| Classification | Count |
|---|---|
| `SINGLE_ROUTE_VALID` | 22 |
| `REFUSED` | 13 |
| `AGREEMENT` | 3 |
| `CONFLICT` | 3 |
| `SEMANTICALLY_DIFFERENT` | 1 |

### The three genuine agreements

| Case | Structured | AGE | Truth |
|---|---|---|---|
| `count_cases` | 73 | 73 | 73 (SQL) |
| `count_distinct_active_persons` | 208 | 208 | 208 (Python) |
| `active_persons_via_belongs_to_case` | 208 | 208 | 208 (graph) |

Two structurally independent routes — a typed compiler and a generated
query — reached the same figure, and that figure matches a third
independent route. That is the architecture's positive case.

---

## 5. The critical experiment — disagreement detected

**`ratio_cases_multi_officer_entity_grain`**

| Route | Value |
|---|---|
| Structured | **5.47945205479452 %** (4/73) |
| AGE | **24.65753424657534 %** (18/73) |
| Ground truth (Python over raw rows) | **5.47945205479452 %** |

The model wrote `OPTIONAL MATCH (o:Officer)-[:BELONGS_TO_CASE]->(c)` —
traversing the **wrong relationship** (`BELONGS_TO_CASE`, 206 edges) rather
than `ASSIGNED_TO` (144 edges). Plausible Cypher, valid execution, wrong
number.

Reconciliation returned **CONFLICT**, served **no value**, and produced a
structural diagnosis naming the missing tombstone and supersession
exclusions. It did not pick the deterministic route because it is
deterministic; it refused.

**`ratio_cases_multi_officer_relationship_grain`** is the mirror image:
structured 95.89 %, AGE 5.48 % — the model produced the *entity*-grain
answer to the *relationship*-grain question. Again CONFLICT, no value.

**`time_window_narrow_sept_2024`**: structured 13, AGE 0. The model filtered
on `c.as_of` — a graph bookkeeping timestamp — instead of resolving
`incident_date` through Postgres authority. CONFLICT.

**`malkhana_records`**: structured 45 (RECORD grain), AGE 713 (all
StructuredRecords). Classified **SEMANTICALLY_DIFFERENT**, not CONFLICT,
because the grains differ — the routes answered different questions.

---

## 6. Route-level findings

### AGE route (42 cases)

| Outcome | Count |
|---|---|
| `model_declined` | 19 |
| `SUCCESS` | 12 |
| `guard_semantic` (refused) | 6 |
| `execution_failed` | 2 |
| `scope_denied` | 2 |
| `unparseable_result_shape` | 1 |

Of 12 successes, **3 matched ground truth**. The guard refused 6 queries
before execution on semantic grounds. Nineteen declines are the model
correctly recognising it could not answer safely.

### Semantic route — **not proven**

All 42 returned `EXECUTION_ERROR`. Cause is environmental, not a code
defect: the model server is down (`404` on `/embed` and `/llm` at the ngrok
host), so `embed_text` cannot produce a query vector. The route degraded to
a stated error rather than fabricating evidence — the designed behaviour —
but **its retrieval path is unexercised and I am not claiming it works.**

Its contract *was* verified: across all 42 cases the semantic route
produced **zero** numeric values.

---

## 7. Bug found and fixed — in the harness, not a route

The first sweep reported one `INCORRECT_REFUSAL`:
`cases_by_station_via_traversal`. Investigation showed the structured route
returned **SUCCESS with 19 correct groups**; the classifier read only
`.numeric`, which is `None` for a grouped result, and fell through to the
catch-all.

A harness defect grading a *correct* answer as wrong is the more dangerous
direction — it sends someone hunting a bug that does not exist. Fixed by
comparing group count for grouped shapes, and pinned by
`test_grouped_results_are_comparable_by_group_count`. Corrected tally:
**0 incorrect refusals**.

---

## 8. Security findings

- Guard refuses writes, multi-statement, `$cypher$` delimiter escape,
  inline literals, bad openers, missing RETURN.
- Generated values travel as bound parameters; unaliased RETURN is refused
  rather than shape-guessed.
- `scope_denied` fired correctly for non-supervisor callers (2 cases).
- **No** read-only database role is used for generated queries — they run
  through the same connection as everything else, with the cross-case RLS
  bypass armed. The guard is the only barrier. Acceptable for an
  evaluation harness; **not acceptable for production** without a
  least-privilege role (migration 009's `muhafiz_mcp_readonly` exists and
  is unused here).

---

## 9. Tests

**Full suite: 3,600 — 3,595 passed, 0 failed, 0 errors, 5 skipped.**

| File | Tests |
|---|---|
| `test_xagg.py` | **527 — unchanged** |
| `test_aggregate_three_way.py` | **44 new** |
| others | 68 / 17 / 16 / 33 |

`git diff main..HEAD` touches no production file.

---

## 10. What is now proven

1. Three routes coexist behind one contract and normalise comparably.
2. Generated Cypher is deterministically guarded before execution; unsafe
   and semantically-suspect queries are refused, never repaired.
3. Reconciliation classifies agreement and conflict **deterministically**,
   with no LLM adjudication anywhere.
4. Conflicting numbers are never averaged and no route is preferred —
   CONFLICT serves no value, in all 3 occurrences.
5. Disagreement is observable and diagnosable: each conflict carried a
   structural account of the difference.
6. Independent agreement is achievable — 3 cases where a typed compiler and
   a generated query converged on a figure a third route confirms.
7. The semantic route cannot emit a numeric aggregate (0 of 42).

## 11. What remains unproven

1. **The semantic route's retrieval path** — never exercised; model server
   down.
2. **AGE route accuracy** — 3 correct of 42 is not a capability claim. Most
   cases were declines or guard refusals, which is safe but uninformative.
3. **Scale** — 42 cases, one corpus, one schema.
4. **Cost/latency** — ~18 s per case, entirely model-bound, cloud-dependent.
5. **Guard completeness** — the semantic guards catch measured failure
   modes; they are not a proof system, and a wrong query that violates none
   of them will execute.

---

## 12. Recommended next phase

**Restore the model server, then re-run this harness unchanged.** Two of
three routes currently depend on a host returning 404; the semantic route's
entire evaluation and a large share of the AGE route's declines may be
artefacts of that. Re-running is cheap and would materially change what
section 11 says.

After that, and before any NL→AggregateSpec work: **route generated queries
through a least-privilege read-only role.** The guard is sound but it is a
single barrier in front of a connection with the cross-case bypass armed,
and defence in depth is cheap here because the role already exists.

I would **not** start NL→AggregateSpec next. The architecture is proven;
the AGE route's accuracy is not, and adding a second generative component
before the first is characterised would make failures harder to attribute.
