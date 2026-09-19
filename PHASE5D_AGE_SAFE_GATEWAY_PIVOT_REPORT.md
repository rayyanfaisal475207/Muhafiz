# Phase 5D — AGE Safe Query Gateway Pivot

**Status: complete.** The model no longer writes Cypher. It emits a typed
`AgeQueryPlan`; trusted code validates it against the live registry and
compiles it into read-only Cypher executed against production
`evidence_graph`. The Phase 5C evaluator database is retained as security-
test infrastructure and is no longer a runtime dependency.

Branch `feat/aggregate-engine-phase0` · production unchanged at
73 / 430 / 4702, 4942 vertices / 13467 edges.

---

## 1. Executive summary

**Before (Phase 5C).** The model wrote executable Cypher. Production was
protected by moving that text somewhere disposable — an isolated database
holding a synchronised copy of the graph. Containment worked (proven
destructively: evaluator `Case` 73 → 0, production untouched), but it
required maintaining a copy of production purely so untrusted text had
somewhere safe to run, plus snapshot freshness and reconciliation across
two data states.

**After (Phase 5D).**

```
model → AgeQueryPlan → validator → compiler → guard → production evidence_graph
```

The model chooses *semantics* (labels, relationships, direction, filters,
grain, grouping). Trusted code chooses *syntax*. Mutation is not filtered
out — it is **unrepresentable**: the plan schema has no field that can
carry a clause, `extra="forbid"` rejects an invented one, and the compiler
contains no mutation emitter to call.

---

## 2. Phase 5C component disposition

| Component | Previous purpose | Disposition | Reason |
|---|---|---|---|
| `muhafiz_age_eval` database | Ran untrusted Cypher away from production | **TEST-ONLY** | Still the only place destructive tests can run on realistic data |
| `muhafiz_age_eval_app` role | Isolated execution identity | **TEST-ONLY** | Kept; no production path uses it |
| `evidence_graph_eval` graph | Production-derived snapshot | **TEST-ONLY** | Adversarial/compiler verification target |
| `scripts/rebuild_age_eval.py` | Runtime snapshot refresh | **TEST-ONLY** | Docstring reframed; not needed to answer a question |
| `age_eval_client.py` | Runtime executor | **TEST-ONLY** | Zero runtime importers remain |
| Snapshot metadata + `verified` | Runtime comparability gate | **REMOVED FROM RUNTIME** | Both routes now read the same production state |
| Snapshot-aware reconciliation | Demoted un-snapshotted AGE results | **REMOVED (surgical)** | Asymmetry gone; gate would suppress genuine agreement |
| Fixed graph/database target | Model cannot select target | **KEEP / ADAPT** | Still true, now for production |
| `cypher_guard` | Primary safety boundary | **KEEP — demoted** | Now a regression assertion over trusted output |
| Resource limits | Bounded evaluator cost | **KEEP / ADAPT** | Re-pointed at production as `AGE_QUERY_*` |
| Production CONNECT hardening | Isolation | **UNRELATED — UNTOUCHED** | Both app roles verified working |

---

## 3. Files changed

**New**

| File | Lines |
|---|---|
| `src/pipeline/aggregate/age_plan.py` | 245 |
| `src/pipeline/aggregate/age_plan_validator.py` | 325 |
| `src/pipeline/aggregate/age_compiler.py` | 341 |
| `src/pipeline/aggregate/age_execution.py` | 204 |
| `tests/test_age_safe_gateway.py` | 731 |

**Modified**

| File | Change |
|---|---|
| `src/pipeline/aggregate/route_age.py` | +288/−102 — model emits a plan; three gates; production execution |
| `src/config.py` | +107 — `AGE_QUERY_*` limits; `AGE_EVAL_*` demoted to test-only |
| `src/pipeline/aggregate/reconcile.py` | +17 — snapshot gate removed, semantics comparison retained |
| `.env.example` | +47 — new limits documented; evaluator marked test-only |
| `tests/test_age_eval_containment.py` | 2 tests updated to the new contract |

---

## 4. Runtime dependencies removed

- `route_age.py` no longer imports `age_eval_client` (verified: zero
  runtime importers in `src/`).
- No runtime path reads `AGE_EVAL_DATABASE_URL`.
- **Proof:** with `AGE_EVAL_*` unset, the aggregate path still answers
  `Case = 73`. No snapshot, no rebuild, no evaluator required.
- `validate_config()` no longer warns when the evaluator is unconfigured.

---

## 5. `AgeQueryPlan` grammar

```
root_alias, root_label                  the entity being measured
patterns[]    from_alias/from_label, rel_type, direction(OUT|IN|ANY),
              to_alias/to_label, optional
filters[]     alias, property, op, value
aggregate     COUNT | COUNT_DISTINCT | SUM | AVG | MIN | MAX
aggregate_alias, aggregate_property
group_by      alias, property            (single dimension)
limit         1..1000
grain         ENTITY|RELATIONSHIP|ROLE_PAIR|EVENT|RECORD
population_description, reasoning
```

Aliases are a closed set (`a`–`d`). Operators, aggregates, directions and
grains are closed enums. Fields derived from the 42-case corpus's own axis
literals; `median`, `comparison`, `threshold`, `top_n` are deliberately
absent because the corpus marks them unimplemented and the structured route
refuses them.

---

## 6. Validation

**Rejected:** unknown label / relationship type / property; a relationship
triple that exists as a type but not between those labels; alias bound to
two labels; filter or aggregate on an unbound alias; nullary operator with
a value; `IN` without a list; value aggregate without a property;
`COUNT_DISTINCT` with no key; **plain `COUNT` downstream of a fanning
traversal** (the 73→449 inflation); grouping on a property present on
<50% of nodes; >3 patterns, >8 filters, LIMIT >1000.

**No repair, ever.** An invalid plan is refused with its reasons and never
rewritten toward the structured route's choice — asserted by a test that
`dataclasses.replace` and `model_copy` do not appear in the validator.

---

## 7. Compiler

Emits only: `MATCH`, `OPTIONAL MATCH`, `WHERE`, `WITH`, `RETURN`, `LIMIT`.

Injects correctness invariants the model is not asked to remember:
`merged_into IS NULL` where the label carries tombstones,
`superseded_by IS NULL` where the relationship is versioned. Phase 0
measured the cost of omitting them: 429 persons instead of 208.

**No `ORDER BY`.** The first version copied the structured compiler's
`ORDER BY value DESC`; the guard refused it (`age_order_by_alias` —
AGE raises `UndefinedColumnError`). The emitter now does not express
ordering at all rather than carry a dialect hazard for cosmetics. This was
caught by the defence-in-depth layer doing exactly its job.

---

## 8. Security properties

Mutation is unrepresentable at three independent levels:

1. **Schema** — no `cypher`/`raw_where`/`function_name` field exists;
   `extra="forbid"` makes inventing one a parse error (14 smuggling
   attempts tested).
2. **Enums** — `aggregate="DELETE"` is not a member, so it cannot parse.
3. **Compiler** — no emitter exists for CREATE, MERGE, SET, REMOVE,
   DELETE, DETACH DELETE, DROP or CALL. Asserted by source inspection,
   not merely by output scanning.

Every compiled output is additionally checked against `cypher_guard`'s
`_WRITE_CLAUSES` — **imported, never retyped**, so guard and tests cannot
drift.

---

## 9. Injection safety

Values go through `_ParamBag` and appear only as `$p0`, `$p1`, … Tested
with `'; MATCH (n) DETACH DELETE n; --`, `$cypher$ DROP GRAPH …$cypher$`,
`Ali' OR '1'='1`, embedded quotes/backslashes/newlines, zero-width
characters and Urdu text: in every case the value appears **only** in
`params`, never in the query text, and the emitted text carries
`a.name = $pN`.

Identifiers cannot be parameterised in Cypher and so are interpolated —
safe only because the validator cleared each against the live registry, and
`_safe_identifier()` re-asserts the character class at the point of
interpolation.

---

## 10. AGE independence

Preserved deliberately. The planner still chooses labels, relationships,
direction, filters, grain and grouping, and may choose differently from the
structured route. Pinned by test using the **recorded** Phase 4 case
`ratio_cases_multi_officer_entity_grain`, where AGE traversed
`BELONGS_TO_CASE` while the structured route used `ASSIGNED_TO` and
reconciliation classified it `CONFLICT`. The objective is not to force
agreement — it is that AGE can choose, the choice is explicit in plan
provenance (`chosen_relationships`, `chosen_labels`, `plan_hash`), and
reconciliation can see it.

---

## 11. Cypher guard

Retained, demoted to **secondary**. It now runs over trusted compiler
output as a regression assertion. A SAFETY violation means the trusted
compiler emitted something forbidden — logged `CRITICAL` as
`CompilerDefect` and **not executed**. This is expected never to fire in
normal operation; it fired once during development, on the `ORDER BY`
defect above, and correctly blocked production execution.

```
Primary:   typed plan + validator + deterministic compiler
Secondary: Cypher guard · fixed graph/database · timeouts · row limits
```

---

## 12. Evaluator database

Retained, **not dropped**: `muhafiz_age_eval`, `muhafiz_age_eval_app`,
`evidence_graph_eval`, snapshot `20260916T090855Z-d88ce528` (verified).

New purpose: adversarial and security-regression testing — the only place
destructive queries can run on realistic data. Production runtime does not
depend on it; security/CI testing may use it. Phase 5C's work is therefore
repurposed, not wasted.

---

## 13. Reconciliation changes

**Removed:** the gate demoting AGE results lacking `snapshot_id` to
`SINGLE_ROUTE_VALID`, and the now-dead `ROUTE_AGE_NAME` constant.

**Retained:** `comparable_key()` — interpretation, grain, grouping — which
asks whether the routes answered the *same question* before asking whether
they got the same answer. That is what keeps "92 distinct accused" from
being compared against "94 accused involvements", and is unrelated to
evaluator staleness.

**Verified:** the 4-vs-70 case still classifies `CONFLICT` with no value
served; equal values still classify `AGREEMENT` — including when carrying
`executed_against` provenance, which previously demoted them.

---

## 14. Tests

```bash
pytest tests/test_age_safe_gateway.py                    # 106 passed
pytest tests/test_aggregate_*.py tests/test_age_*.py     # 212 passed, 4 skipped
RUN_POSTGRES_TESTS=1 pytest tests/test_age_eval_containment.py   # 26 passed
```

Baseline was **190 aggregate tests**; now **212 passed, 4 skipped** with no
regressions. New gateway tests: **106**.

| Area | Tests |
|---|---|
| Plan contract / no escape hatch | 26 |
| Adversarial inputs (mutation, evasion, target selection) | 13 |
| Validator — unknown identifiers | 6 |
| Validator — counting correctness | 4 |
| Validator — cost limits | 2 |
| Compiler invariants (read-only, aliasing, dialect) | 32 |
| Compiler — injected correctness predicates | 3 |
| Injection — values stay data | 9 |
| Execution boundary | 5 |
| Semantic independence | 3 |
| Reconciliation cleanup | 3 |

Counts confirmed two ways: pytest collection (106) and AST analysis (106).

**Integration order followed (§32):** unit → adversarial → isolated
evaluator → read-only verification → production. Evaluator integration
returned Case 73, Person distinct 208, traversal 208, 19 station groups.

---

## 15. Production integrity

| Object | Before | After |
|---|---|---|
| `Case` | 73 | **73** |
| `Person` | 430 | **430** |
| `SAME_AS` | 4702 | **4702** |
| vertices | 4942 | **4942** |
| edges | 13467 | **13467** |
| `public.documents` | 1012 | **1012** |

Four compiled queries executed against production `evidence_graph`,
guard clean on every one, counts identical before and after.

---

## 16. Remaining limitations

1. **The planner is untested against a live model.** Every plan in this
   phase was constructed directly. Whether the LLM reliably produces valid
   plans from the prompt is exactly what Phase 6 measures.
2. **Ratio/percentage is not in the plan grammar.** The corpus contains
   ratio cases; the structured route computes them. AGE currently cannot,
   so those cases will reconcile as single-route rather than two-route.
3. **No `ORDER BY` / `top_n`** — a grouped result comes back unordered.
4. **Grouping is single-dimension**, matching the structured compiler.
5. **Cost limits are crude** (pattern count, arity, LIMIT) — not a cost
   model. Adequate for corpus shapes; a pathological 3-pattern query over
   a larger graph could still be slow, bounded only by the statement
   timeout.
6. **Snapshot metadata remains writable by the evaluator role** (Phase 5C
   limitation, unchanged) — irrelevant to production now that the
   evaluator is test-only.

---

## 17. Exact next step

**Phase 6 — full three-way validation through the new safe-plan route.**

```bash
python scripts/run_aggregate_three_way.py       # 42-case corpus
```

This measures what Phase 5D could not: whether the LLM produces valid plans
from real questions, how often the validator refuses them and why, and
whether the AGE route remains correct and independent enough to justify
continued development. Refresh the evaluator first only if adversarial
tests need current data — the corpus run itself does not require it.

**Not run in this phase**, per the brief.
