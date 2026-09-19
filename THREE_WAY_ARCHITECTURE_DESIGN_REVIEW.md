# Three-Way Architecture Integration — Design Review

**Analysis only. No source code was modified.**

The two edits I had started before this review was requested
(`AGGREGATE_ENGINE` in `config.py`, a `live_bridge.py` module) have been
reverted; the working tree is back at the Phase 8 checkpoint.

---

## 0. Headline finding

**The three-way architecture is built, validated, and already shares a
common envelope. What is missing is not reconciliation — it is an input.**

`reconcile.reconcile(structured, age, semantic)` already consumes all three
routes and returns a classification. All three already return the same
`RouteResult` type. The integration gap is narrower and more specific than
"wire the routes together":

1. **Nothing generates an `AggregateSpec` from natural language.** The
   structured route refuses without one (`no_spec_supplied`). Phase 4 left
   this deliberately unbuilt. This is *the* blocker.
2. **The harness result contract carries one result, not three.**
3. **No latency policy exists** for a live request that makes a 41–92 s
   model call.

---

## 1. Current route architecture

```
scripts/run_aggregate_three_way.py          ← the ONLY caller today
        │
        ├── route_structured.run(snapshot, request, spec=…)
        ├── route_age.run(snapshot, request, schema_card=…)
        └── route_semantic.run(snapshot, request)
                    │
                    ▼
        reconcile.reconcile(structured, age, semantic, snapshot=…)
```

The live path is entirely separate:

```
/api/chat → harness → tools/xagg.py → xagg.run_aggregate()   ← legacy engine
```

**Zero external modules import** `gates`, `gate_evaluator`,
`composite_executor`, `authority`, or `consensus`. `route_structured` has
exactly one importer: the evaluation harness.

---

## 2. Route output contracts

### Structured route

```
Input:    RegistrySnapshot, AggregateRouteRequest, spec: AggregateSpec (REQUIRED)
Output:   RouteResult
Success:  status=SUCCESS, result_shape="scalar"|"ratio"|"grouped",
          result=NumericResult(value, interpretation, grain, population, grouping, unit)
Failure:  status=REFUSED|EXECUTION_ERROR|UNSUPPORTED, refusal_code + refusal_reason
Metadata: elapsed_ms, spec_hash, registry_as_of, queries[]
Provenance: engine, gate_profile{selected/required/effective/escalated/corrected},
          gate_evaluation{per-gate pass/fail + evidence}, coverage_verdict,
          population_definition, grain, distinct_key, injected_predicates,
          filters_applied, composite{constraints, sources, composition, final_n}
Warnings: INSUFFICIENT_DATA_COVERAGE + receipt caveat
Native:   AggregateReceipt (full provenance) or CompositeResult
```

**Without a spec it returns `UNSUPPORTED/no_spec_supplied`.**

### AGE route

```
Input:    RegistrySnapshot, AggregateRouteRequest, schema_card (optional)
Output:   RouteResult
Success:  status=SUCCESS, result_shape="scalar"|"grouped",
          result=NumericResult(value, interpretation, grain, population)
Failure:  REFUSED (model_declined, plan_invalid, plan_unparseable,
          compile_failed, compiler_defect, scope_denied, empty_result)
          | EXECUTION_ERROR (generation_unparseable)
Numeric:  result.value
Provenance: planner, plan (full AgeQueryPlan), plan_hash, chosen_labels,
          chosen_relationships, model_interpretation, model_grain,
          model_reasoning, gate_profile_id (selection only),
          plan_violations, compiled_cypher, guard_violations, executed_against
Metadata: generation_ms (median 41 s, max 92 s), execution_ms (~93 ms), row_count
Coverage: NONE — the AGE route produces no CoverageReport
Gates:    NONE — gate profiles are structured-route only
```

### Semantic route

```
Input:    RegistrySnapshot, AggregateRouteRequest
Output:   RouteResult
Success:  status=SUCCESS, result_shape="evidence",
          result=EvidenceResult(evidence_count, supporting[], contradicting[],
          relevant_entities, relevant_relationships, interpretation, numeric_mentions[])
Failure:  REFUSED/retrieval_failed
Numeric:  NONE BY CONSTRUCTION — RouteResult.numeric returns None for
          EvidenceResult even when numeric_mentions is populated
Metadata: elapsed_ms (~3.2 s)
```

---

## 3. Missing contracts

### 3.1 Common result envelope — **ALREADY EXISTS**

`RouteResult` is the shared envelope, and `reconcile()` already consumes
all three. **No `ThreeWayResult` class is needed for the engine.** What is
missing is a *transport* contract for the harness (§3.3).

### 3.2 Common status model — **ALREADY UNIFIED**

`RouteStatus = SUCCESS | REFUSED | EXECUTION_ERROR | UNSUPPORTED | CONFLICT`,
shared by all three. There is no `PARTIAL`; `PARTIAL_AGREEMENT` exists at
the *reconciliation* level, which is the right layer.

**No normalization required.**

### 3.3 Harness transport — **MISSING**

`XAggToolResult` carries `aggregate_kind` and `raw_summary_text` — one
result. Three-way produces three `RouteResult`s plus a
`ReconciliationResult`. The class is marked `[PRESERVE — design §2.4]` and
mirrors `SUBAGENT_INTERFACES.md`, so extending it touches the shared
harness type system.

### 3.4 Natural-language → `AggregateSpec` — **MISSING, AND THE REAL BLOCKER**

Only `corpus.py` (hand-written fixtures) and `shadow.py` construct specs.
Nothing derives one from a question.

Without it the structured route cannot answer a chat question at all, which
means two of three routes are unavailable and "three-way" degenerates to
AGE-only.

### 3.5 Numeric compatibility — **SOLVED**

`NumericResult.comparable_key()` = (interpretation, grain, grouping), and
`canonical.py` proves `count ≡ count_distinct` only where provable. Same
number / different meaning is detected, not assumed (§A2 of the conflict
catalogue).

---

## 4. Reconciliation compatibility

```
Inputs:   structured, age, semantic (all Optional[RouteResult]), snapshot (optional)
Logic:    1. no numeric route            → REFUSED
          2. invariant violations        → CONFLICT (requires_investigation)
          3. exactly one numeric route   → SINGLE_ROUTE_VALID
          4. comparable_key mismatch     → SEMANTICALLY_DIFFERENT
          5. values match                → AGREEMENT
          6. otherwise                   → CONFLICT (no value served)
Output:   ReconciliationResult(classification, value, interpretation, grain,
          agreeing/disagreeing/refused routes, semantic_support,
          invariant_violations, explanation, diagnosis, requires_investigation)
```

**It already handles all three routes and needs no change.**

**Limitations:** semantic influences only `semantic_support` and
`requires_investigation`, never the value (by design). No coincidental-
agreement detection. No confidence weighting — `consensus.combine()` raises
`ConsensusPolicyRequired` deliberately.

---

## 5. Conflict cases

Catalogued separately in **`THREE_WAY_CONFLICT_CASES.md`** — five
categories (A numeric disagreement, B route unavailable, C semantic,
D gates/coverage, E failures), each marked settled or open, all drawn from
measured corpus runs.

---

## 6. Unresolved architecture policies

### A. Route authority — **ALREADY SETTLED, needs confirmation only**

*Decision needed:* structured = answer authority, AGE = independent
verifier, semantic = evidence only?
*Why it matters:* determines whether AGE can ever override structured.
*Current assumption:* Option 1. `reconcile.py` serves no value on CONFLICT,
never prefers a route, and `RouteResult.numeric` returns None for evidence.
Phase 4B's docstring explicitly refuses a "trust marker" for structured.
*Risk if wrong:* Option 2 (equal contribution) would require confidence
weighting, which is unapproved and unimplemented.
**Recommendation: confirm Option 1. It is already implemented.**

### B. Execution strategy — **OPEN**

*Decision needed:* always run all three, or select by query?
*Why it matters:* AGE costs 41–92 s per question. Always-on makes every
aggregate answer slow.
*Current assumption:* the harness always runs all three; the live path runs
none.
*Options:* (1) always three; (2) structured-first, AGE only for
high-risk/ambiguous; (3) structured synchronous + AGE asynchronous, with
verification arriving after the answer.
*Risk:* (1) makes chat unusable at 40–90 s; (2) needs a risk classifier
nobody has specified; (3) needs a UI that can revise a displayed answer.

### C. Conflict handling — **PARTIALLY SETTLED**

*Settled:* no value is served on CONFLICT; no averaging.
*Open:* what the user sees. A refusal with both figures? The structured
figure flagged for review?
*Risk:* showing both numbers invites the reader to pick one, which defeats
the purpose of withholding.

### D. Missing-route behaviour — **PARTIALLY SETTLED**

*Settled:* `SINGLE_ROUTE_VALID` when one route computes.
*Open:* (i) should an unverified answer be visibly marked? (ii) **B2 is
arguably mislabelled** — when structured *correctly refuses* and AGE
answers, `SINGLE_ROUTE_VALID` implies the lone result is valid when the
refusal was the correct outcome.
*Risk:* serving AGE's number in a B2 case serves a known-unsound figure.

### E. Semantic role — **SETTLED**

Evidence only. 485 mentions, 0 promoted, enforced structurally.
*Open (minor):* whether `CONTRADICTS` should do more than set a flag.

### F. Spec generation — **OPEN, AND BLOCKING**

*Decision needed:* build an LLM planner emitting a typed `AggregateSpec`?
*Why it matters:* without it the structured route cannot serve chat.
*Options:* (1) LLM → `AggregateSpec`, validated like `AgeQueryPlan`;
(2) pattern-match a narrow set of shapes; (3) keep legacy for chat and
three-way for evaluation only.
*Risk:* (2) recreates the keyword-dispatch engine the redesign replaced —
I started down that path and it already failed on "distinct persons".

---

## 7. Implementation risks

| # | Risk | Impact | Severity | Mitigation |
|---|---|---|---|---|
| 1 | **Latency** — AGE planner 41–92 s per question | chat unusable | **HIGH** | policy B; async verification or selective execution |
| 2 | **No spec generation** — structured unavailable in chat | "three-way" becomes AGE-only | **HIGH** | policy F before any wiring |
| 3 | **Harness contract** — one result slot | cannot transport three results | **HIGH** | extend `XAggToolResult`; touches `SUBAGENT_INTERFACES.md` |
| 4 | **Rewiring `run_aggregate()`** would break `shadow.py`, which uses it as the evaluation baseline | destroys shadow evaluation silently | **HIGH** | never modify it; adapt at `tools/xagg.py` only |
| 5 | **No timeout policy** for a live AGE call | a hung model call blocks a user response | MEDIUM | define timeout + degrade-to-structured |
| 6 | **Duplicate execution** — composite already queries Postgres + graph; adding AGE re-queries | 2–3× database load per question | MEDIUM | measure; acceptable at this corpus size |
| 7 | **Coverage/gate asymmetry** — AGE has neither | comparing unlike things | MEDIUM | document; do not fabricate AGE coverage |
| 8 | **Semantic numeric leak** | evidence becomes an answer | LOW | already structurally prevented; keep the test |
| 9 | **Role gate divergence** — duplicated in three places | a refactor could desynchronise | LOW | already deliberate; keep duplicated |

---

## 8. Recommended implementation approach

Staged, each stage independently verifiable:

**Stage 1 — spec generation (policy F).** An LLM planner emitting a typed
`AggregateSpec`, parsed strictly and validated by the existing
`validator.py`. Mirrors `AgeQueryPlan` exactly, so the security argument
transfers. *Nothing else is possible without this.*

**Stage 2 — structured-only cutover.** Route chat through the new
structured engine alone: gate profiles, composite execution, coverage
warnings, ~40 ms. Fast, and it exercises most of the architecture.

**Stage 3 — harness contract.** Extend `XAggToolResult` to carry three
results plus a reconciliation verdict.

**Stage 4 — AGE verification, per policy B.** Most likely asynchronous or
selective, given the latency measurement.

**Do not** rewire `xagg.run_aggregate()` at any stage (risk 4).

---

## 9. Supervisor decisions required

| # | Decision | Status |
|---|---|---|
| **1** | **Build NL → `AggregateSpec` generation?** (policy F) | **blocking** |
| **2** | **Execution strategy** — always three / selective / async? (policy B) | **blocking for three-way** |
| 3 | User-facing CONFLICT behaviour (policy C) | needed before serving |
| 4 | B2 relabel — structured-refuses-but-AGE-answers | correctness concern |
| 5 | Unverified-answer marker (policy D) | presentation |
| 6 | Live AGE timeout policy | needed for Stage 4 |
| 7 | Confirm route authority Option 1 | confirmation only |

Decisions 1 and 2 gate everything else.
