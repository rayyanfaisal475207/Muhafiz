# Phase 5B — Isolated AGE Execution Design Review

**Investigation only. Nothing implemented, nothing modified.** No source,
migration, `.env`, graph, or data change. No Cypher mutation executed. No
harness run.

Repository `/c/Users/PMLS/Desktop/Muhafiz 2` · branch
`feat/aggregate-engine-phase0` · HEAD `7fdd54c` · Apache AGE 1.5.0 ·
PostgreSQL 16.3 · container `muhafiz-postgres` · database `muhafiz`.

---

## 1. `evidence_graph_eval` — current state

**It exists and is registered**, but it is **not a copy of production**.

| | `evidence_graph` | `evidence_graph_eval` |
|---|---|---|
| Registered in `ag_catalog.ag_graph` | ✅ | ✅ |
| Namespace | `evidence_graph` | `evidence_graph_eval` |
| Owner | `postgres` | `postgres` |
| Label tables | 32 | 32 |
| **Total vertices** | **4,942** | **29** |
| **Total edges** | **13,467** | **57** |

### Vertex counts, measured

| Label | Production | Eval | Delta |
|---|---|---|---|
| Case | 73 | **1** | −72 |
| Person | 430 | **2** | −428 |
| Incident | 73 | **1** | −72 |
| Officer | 1,155 | **1** | −1,154 |
| Weapon | 32 | **1** | −31 |
| StructuredRecord | 713 | **12** | −701 |
| PoliceStation | 19 | **1** | −18 |
| District | 9 | **1** | −8 |
| Address | 2,171 | **1** | −2,170 |
| Document | 126 | **1** | −125 |
| Date | 137 | **7** | −130 |
| Vehicle | 2 | **0** | −2 |
| PhoneNumber | 2 | **0** | −2 |

### Edge counts, measured

| Label | Production | Eval | Delta |
|---|---|---|---|
| BELONGS_TO_CASE | 3,605 | **19** | −3,586 |
| FILED_AT | 73 | **1** | −72 |
| ASSIGNED_TO | 144 | **2** | −142 |
| CITES | 9 | **0** | −9 |
| PART_OF | 146 | **2** | −144 |
| APPEARS_IN | 3,591 | **18** | −3,573 |
| SAME_AS | 4,702 | **0** | −4,702 |
| INVOLVED_IN | 221 | **2** | −219 |
| OCCURRED_ON | 568 | **10** | −558 |

### Classification — stated with evidence, not inferred

**It is an evaluation fixture.** Not a read-copy, not stale, not partial in
the sense of a truncated copy. The structure is identical (32 label tables,
same names) because migrations create labels in both graphs; the *content*
is a handful of hand-made test rows — one Case, two Persons, zero SAME_AS
against production's 4,702.

`SAME_AS = 0` is decisive on its own: entity resolution is the single
largest edge population in production, and a copy of any vintage would
carry some of it.

---

## 2. How it is created and populated

**Created by `migrations/011_age_eval_graph.sql`** — `create_graph()` plus
`create_vlabel()`/`create_elabel()` loops over a fixed label list.
Subsequent migrations (020, 023, 024, 025, 026) extend both graphs in
lockstep, and 031 grants `muhafiz_app` DML on both.

**It creates structure only. No migration inserts data into it** — verified:
the sole data-adjacent match is 031's `GRANT SELECT, INSERT, UPDATE,
DELETE`, which is a privilege, not a row.

**It is populated incidentally, by tests and eval scripts** that pass
`graph="evidence_graph_eval"`:

| Consumer | Purpose |
|---|---|
| `scripts/eval_entity_resolution.py` | `EVAL_GRAPH = "evidence_graph_eval"`; entity-resolution scoring |
| `scripts/purge_eval_contamination.py` | cleans eval rows that leaked into production |
| `scripts/backfill_missing_belongs_to_case.py` | writes to eval, "never" production |
| `tests/test_entity_resolution.py` | isolation assertions |
| `tests/test_graph_eval_isolation.py` | 11 tests that `graph=` threads correctly |
| `src/graph/{versioning,entity_resolution,case_scope}.py` | accept and forward a `graph` parameter |

**There is no refresh mechanism, no sync from production, and nothing
automatic.** It drifts by construction — its contents are whatever the last
eval run left behind.

---

## 3. Cross-graph attack surface

> **If the AGE route receives arbitrary Cypher text, what prevents that text
> from targeting `evidence_graph` instead of `evidence_graph_eval`?**

**Today: nothing but the guard.** And more importantly, a separate graph is
**not currently a boundary at all**:

| Check | Result |
|---|---|
| `muhafiz_app` USAGE on `evidence_graph` | **true** |
| `muhafiz_app` USAGE on `evidence_graph_eval` | **true** |
| `muhafiz_app` UPDATE on `evidence_graph."Case"` | **true** |
| `muhafiz_app` UPDATE on `evidence_graph_eval."Case"` | **true** |
| Table grants to `muhafiz_app` | **128 on each schema** |

`muhafiz_app` is now the **only** role with graph access
(`muhafiz_graph_readonly` was dropped in Phase 5A; `muhafiz_mcp_readonly`
cannot reach `ag_catalog`). A connection able to query the eval graph is
the same connection able to write production.

### AGE function surface

**All 336 `ag_catalog` functions have default `PUBLIC EXECUTE`** — including
`create_graph`, `drop_graph`, `create_vlabel`, `create_elabel`,
`load_labels_from_file`, and `cypher` itself. None carries a restrictive
ACL.

So the attack surface, assuming hostile generated text, includes:

- **Graph switching** — the graph name is a literal in the SQL string
  (`cypher('{graph}', …)`), *not* inside the Cypher the model writes. The
  model therefore cannot name a graph **through Cypher syntax**. This is
  the one structural point in our favour, and §4 develops it.
- **Guard bypass to mutation** — proven in Phase 5: `SET`, `REMOVE`,
  `DETACH DELETE` execute under any role holding DML, which `muhafiz_app`
  does on both schemas.
- **`$cypher$` delimiter escape** — the guard checks for it, but a bypass
  would inject arbitrary SQL, at which point graph choice is irrelevant.
- **`CALL` into AGE procedures** — guard-blocked by keyword today; all 336
  functions are PUBLIC-executable if that check were evaded.
- **Expensive/unbounded queries** — no statement timeout, no row cap on the
  current AGE path.

---

## 4. Executor boundary — what can be made a hard invariant

`src/graph/age_client.py`:

```python
GRAPH_NAME = "evidence_graph"                    # line 59, module constant

async def execute_cypher(cypher_query, params=None,
                         columns=("result",),
                         graph: str = GRAPH_NAME):   # line 166, OVERRIDABLE
    sql = f"SELECT * FROM cypher('{graph}', $cypher${cypher_query}$cypher$, …)"
```

**Current state of the AGE route:** `route_age.py` line 253 calls
`execute_cypher(query, params=params, columns=list(columns))` with **no
`graph=` argument** — so it targets **production by default**.

**The strongest enforceable application-layer boundary:**

The graph name is **interpolated by our code, from a Python value**. It
never passes through the model's text. A dedicated executor that hard-codes
its target — no `graph` parameter in its signature at all — makes
"generated Cypher cannot choose the graph" a **structural property of the
function**, not a prompt convention or a validation rule.

That is genuinely strong, and it is available today. Its limit is equally
clear: it constrains **which graph** the query hits, not **what the query
does to that graph**. Containment therefore requires the target graph to be
one we can afford to lose.

---

## 5. Containment design comparison

| | Mutation containment | Cross-graph reach | Complexity | Freshness | Failure mode if guard bypassed |
|---|---|---|---|---|---|
| **A. Separate graph, same DB** | ❌ **none today** — `muhafiz_app` has DML on both | Same role, same connection | Low | No mechanism exists | Production mutated |
| **B. Separate database** | ✅ real — cross-database queries need FDW/dblink | Blocked by DB boundary | Medium | Needs a copy pipeline | Eval DB damaged only |
| **C. Separate container** | ✅ strongest — separate process, volume, network | No shared catalog at all | High | Needs copy + orchestration | Eval container damaged only |
| **D. Read-only replica** | ✅ physically read-only | Blocked | High (infra) | Replica lag | Writes rejected by recovery |
| **E. Fixed graph identifier** | ❌ alone — targets a graph, doesn't protect it | ✅ removes graph choice | **Trivial** | n/a | Damage confined to the fixed graph |
| **F. Combined (A/B + E + guard + limits)** | ✅ layered | ✅ | Medium | Design required | Damage confined; guard is filter, not boundary |

Notes that matter:

- **A alone is disproven for this installation.** Phase 5 established that
  DML grants don't gate `cypher()` writes, and `muhafiz_app` holds DML on
  both schemas anyway. A separate graph under the same role is a naming
  convention, not a boundary.
- **E is necessary but never sufficient.** It is also the cheapest thing on
  this table and should be adopted regardless of which containment is
  chosen.
- **D** was included for comparison and is unsuitable here: replica lag
  would make the AGE route's population differ from the structured route's
  by an unbounded amount, which reconciliation would classify as
  `SEMANTICALLY_DIFFERENT` — burying real disagreements in noise.

---

## 6. Threat model

Assume generated Cypher is hostile. It may attempt mutation, procedure
calls, catalog access, SQL escape, unbounded traversal, or huge result sets.

**Security goal:** `evidence_graph` unreachable from the untrusted route.

**The guard is a correctness and cost filter, not a boundary.** Phase 5A
recorded two confirmed evasions of its `\bset\b` scan — `S​ET`
(zero-width space) and `ЅET` (U+0405 homoglyph) — neither folded by NFKC.
Whether AGE's lexer would *execute* them is untested and must not be
assumed either way. Containment has to survive guard failure.

---

## 7. Phase 4 compatibility

The AGE route exists to produce a **second, differently-derived** result for
reconciliation. Nothing here changes that: pointing it at a different graph
alters *which data it reads*, not how it reasons. Its independence — a model
writing Cypher, versus a compiler emitting from a typed spec — is preserved.

**But there is a real cost, and it must be stated.** If the AGE route reads
a copy while the structured route reads production, any divergence between
the two populations becomes indistinguishable from a computational
disagreement. Phase 4's value rests on `AGREEMENT` meaning "two independent
derivations agree"; a stale copy silently converts that into "two different
datasets disagree."

This is why §8 is a prerequisite, not a refinement.

---

## 8. Freshness and snapshot strategy

For the AGE route to read a copy without poisoning reconciliation:

1. **Snapshot identity must be explicit.** Both routes record which snapshot
   they read; the registry already carries `as_of` and every receipt records
   it. Extend the same idea to the graph copy.
2. **Reconciliation must compare snapshot ids before values.** Different
   snapshots ⇒ `INSUFFICIENT_EVIDENCE`, never `CONFLICT`. This mirrors the
   existing `comparable_key()` rule: establish that both sides answered the
   same question before comparing answers.
3. **Refresh must be atomic** — a half-copied graph is worse than a stale
   one, because it looks current.
4. **A partial refresh must fail closed**, leaving the previous complete
   snapshot in place.
5. **Cadence:** on demand before an evaluation run, plus after any
   ingestion. Continuous sync is unnecessary — the corpus is static between
   ingests.

**None of this exists today.** `evidence_graph_eval` has no refresh
mechanism at all.

---

## 9. Recommendation

> **Can `evidence_graph_eval` safely become the execution target for the
> independent AGE route, and under what hard guarantees?**

**Not yet — and not in its current form.** Two blockers, both measured:

1. **It is not a copy.** 29 vertices against 4,942; one Case against 73;
   zero SAME_AS against 4,702. Every aggregate would be wrong-but-plausible
   — precisely the failure class this architecture exists to detect.
2. **It is not isolated.** `muhafiz_app` holds 128 grants with `UPDATE` on
   *both* schemas. Pointing the route at the eval graph while using the same
   role leaves production one guard bypass away.

### Recommended target design

**B + E + guard + limits** — a separate *database* (not merely a separate
graph), reached through a dedicated executor with a hard-coded graph name.

- **Separate database** gives real containment: PostgreSQL blocks
  cross-database access without FDW/dblink, so a bypass damages a
  disposable copy. A separate *graph* does not, because one role spans both.
- **Fixed graph identifier** (a dedicated executor with no `graph`
  parameter) removes graph choice from generated text structurally. Cheap,
  and correct regardless of the containment chosen.
- **Guard retained** as a correctness/cost filter, with Unicode
  normalisation added — never described as the boundary.
- **Resource limits** — statement timeout and row cap, neither present on
  the AGE path today.

**If a separate database is judged too heavy**, the honest fallback is *not*
a separate graph — it is to leave the AGE route pointed at production **with
its execution disabled**, exactly as Phase 5A left it, until containment
exists. A route that can write production is not made safe by reading a
different graph through the same role.

### Implementation plan (not performed)

1. Provision a separate `muhafiz_eval` database with AGE and the migration
   set (no data).
2. Build an atomic copy mechanism, production → eval DB, stamping a snapshot
   id.
3. Add a dedicated executor with the graph name hard-coded and no `graph`
   parameter.
4. Point `route_age.py` at that executor; keep the role gate.
5. Add statement timeout and row cap.
6. Extend reconciliation to compare snapshot ids before values.
7. Harden the guard's Unicode handling.

### Safe verification plan

Every isolation claim must be proven in the **separate database**, never
against `muhafiz`. The experiment needs: its own container or database, a
pre-approved explicit statement list, and an automated `current_database()`
assertion before each statement. I will not run write probes against the
restored graph under any circumstances.

---

## 10. What is NOT implemented

Nothing in this document. No database, migration, `.env`, or source change;
no graph created, copied, or dropped; no Cypher mutation; no harness run; no
Phase 4 file touched. The AGE route remains as Phase 5A left it —
`execute_cypher` against production, reachable only through the guard.
