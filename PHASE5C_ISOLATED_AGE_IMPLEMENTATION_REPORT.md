# Phase 5C — Isolated AGE Containment Implementation

**Status: complete.** Model-generated Cypher now executes only against a
disposable, production-derived snapshot in a separate database whose role
cannot connect to production. Proven destructively: a `DETACH DELETE`
issued through the evaluator's own client destroyed all 73 evaluator `Case`
vertices while production stayed at 73.

Branch `feat/aggregate-engine-phase0` · container `muhafiz-postgres`
(AGE 1.5.0 / PG16).

---

## 1. Architecture implemented

```
STRUCTURED ROUTE ──► muhafiz (muhafiz_app) ──► evidence_graph        [authoritative]

AGE ROUTE ──► cypher_guard ──► age_eval_client ──► muhafiz_age_eval  [disposable]
                                (muhafiz_age_eval_app)  evidence_graph_eval
```

The boundary is the **connection**, not the query. Phase 5 established that
AGE 1.5.0 has no read-only mode — `SET`, `REMOVE` and `DETACH DELETE` all
succeed under SELECT-only grants *and* `default_transaction_read_only=on`
— so no query-level check can be the boundary. `muhafiz_age_eval_app` has
no `CONNECT` privilege on `muhafiz`; PostgreSQL refuses before any Cypher
is parsed. The guard is retained unchanged as defence in depth.

**No production fallback.** Every evaluator failure raises
`EvaluatorUnavailable`, which the route turns into a refusal. There is no
branch that retries against `DATABASE_URL`, and a test asserts the module
never references it.

---

## 2. Files changed

| File | Change |
|---|---|
| `src/pipeline/aggregate/age_eval_client.py` | **new** — fixed-target executor, snapshot reader, isolation assertion |
| `scripts/rebuild_age_eval.py` | **new** — deterministic rebuild with direction guards |
| `tests/test_age_eval_containment.py` | **new** — 26 tests (22 offline, 4 live) |
| `src/config.py` | +92 — five `AGE_EVAL_*` settings, two `validate_config()` checks |
| `src/pipeline/aggregate/route_age.py` | +68/−6 — executes via evaluator; snapshot provenance; refusals |
| `src/pipeline/aggregate/reconcile.py` | +43 — snapshot-aware comparison |
| `.env.example` | +34 — documents the evaluator variables |

`.env` gained five variables (git-ignored, no secret in this report).

---

## 3. Database objects used

| Object | Detail |
|---|---|
| Database `muhafiz_age_eval` | owner `muhafiz_age_eval_app` |
| Role `muhafiz_age_eval_app` | LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, **NOBYPASSRLS** |
| Graph `evidence_graph_eval` | 32 labels, 4,942 vertices, 13,467 edges |
| Table `public.age_eval_snapshot` | snapshot identity + verification |

The evaluator role owns the graph objects it must drop and recreate — it
creates them itself, so ownership follows creation. Production's
`ag_catalog` and `evidence_graph` remain `postgres`-owned and unmodified.

---

## 4. Rebuild command

```bash
python scripts/rebuild_age_eval.py            # dry run
python scripts/rebuild_age_eval.py --execute  # real rebuild
```

**Direction guards** (all verified by test): source must be `muhafiz`,
target must be `muhafiz_age_eval`, the two must differ, and identity is
re-confirmed from `current_database()` after connecting rather than trusted
from the DSN string. Source opens `default_transaction_read_only = on`. The
script contains no statement that can name `evidence_graph`. Seven
dangerous combinations — including reversed source/target — are refused.

**How the copy works.** AGE stores each label as a table of
`(id graphid, properties agtype)`, and a graphid encodes
`label_id << 48 | sequence`. A copied vertex id therefore only resolves if
the target assigns the *same* label id, so labels are created in
production's exact id order — including the three zero-row labels
(`Organization`, `CONFLICTS_WITH`, `CROSS_VERSION_OF`) whose absence would
shift every later id. Alignment is verified before any data moves. Rows
move as `COPY` text; sequences are then advanced past production's.

---

## 5. Snapshot metadata

```
snapshot_id     20260916T090855Z-d88ce528
created_at      2026-09-16 09:08:55+00
source          muhafiz.evidence_graph
Case 73 · Person 430 · SAME_AS 4702 · vertices 4942 · edges 13467
verified        true
```

`require_verified_snapshot()` refuses when the record is missing or
`verified = false`. An unverified snapshot is worse than none: it looks
like data.

---

## 6. Isolation proof

| Check | Result |
|---|---|
| `muhafiz_age_eval_app` → `muhafiz` | **FATAL: permission denied for database "muhafiz"** |
| `muhafiz_age_eval_app` → `postgres` | permitted (K-1 restored; carries no Muhafiz data) |
| `muhafiz_age_eval_app` → `muhafiz_age_eval` | allowed |
| CONNECT privilege on `muhafiz` | **false** |
| Table privileges in `muhafiz` | **0** |
| Routine privileges in `muhafiz` | **0** |
| `muhafiz_app` / `muhafiz_mcp_readonly` → `muhafiz` | both still allowed |

**Destructive proof (brief §14).** Target identity confirmed first
(`assert_isolated()` → `muhafiz_age_eval`), then a `DETACH DELETE` was
executed through the evaluator client:

```
evaluator Case before : 73
evaluator Case after  : 0        ← evaluation data destroyed
production Case       : 73       ← unchanged (Person 430, SAME_AS 4702)
```

The evaluator was then rebuilt to the verified snapshot above. This is the
Phase 5C requirement demonstrated end to end: generated Cypher **can**
damage the evaluator and **cannot** reach production.

---

## 7. Tests executed

| Suite | Result |
|---|---|
| `test_age_eval_containment.py` (default) | **22 passed, 4 skipped** |
| `test_age_eval_containment.py` (`RUN_POSTGRES_TESTS=1`) | **26 passed** |
| Full aggregate suite (6 files) | **190 passed** |

Coverage: config fails closed (unset / production-pointed / unknown DB);
executor exposes no `graph`/`database`/`role` parameter; no production
fallback (asserted structurally against module source); resource limits
configured; rebuild direction guards; snapshot-aware reconciliation; and
live isolation, snapshot fidelity, and edge resolution.

The live tier follows `test_rls_integration.py`'s pattern — module-scope
`requires_postgres` plus a `RUN_POSTGRES_TESTS` skipif — so the default run
stays offline and fast.

**Edge resolution is checked, not assumed.** A row-count match would not
catch dangling graphids, so a traversal asserts copied edges resolve
against copied vertices: `(p:Person)-[:BELONGS_TO_CASE]->(c:Case)` returns
429 distinct persons.

---

## 8. Production integrity

| Object | Before Phase 5C | After |
|---|---|---|
| `Case` | 73 | **73** |
| `Person` | 430 | **430** |
| `SAME_AS` | 4702 | **4702** |
| `Incident` | 73 | **73** |
| `Officer` | 1155 | **1155** |
| `BELONGS_TO_CASE` | 3605 | **3605** |
| total vertices | 4942 | **4942** |
| total edges | 13467 | **13467** |
| `public.cases` | 73 | **73** |
| `public.documents` | 1012 | **1012** |

Production was read-only throughout except two database-level privilege
statements (§9). No graph, table, or schema ACL in `muhafiz` was modified.

---

## 9. Known limitations

1. **Snapshot metadata is not tamper-proof against the evaluator role.**
   Ideally that role would hold `SELECT` only, so a runaway evaluation
   could not forge `verified = true`. That requires separate operator
   credentials for the rebuild; here the rebuild runs *as* the evaluator
   role (it owns the graph objects it must drop), so revoking write would
   only break the rebuild. `AGE_EVAL_ADMIN_DATABASE_URL` exists as the seam
   — point it at a distinct operator role and restore the `REVOKE` to
   close this. Integrity is *checked*, not *enforced*.

2. **The snapshot is a point-in-time copy.** It goes stale the moment
   production changes; nothing detects that automatically. Reconciliation
   surfaces snapshot identity so a human can judge, and Phase 6 should
   rebuild immediately beforehand.

3. **`K-1` and one extra revoke.** `GRANT CONNECT ON DATABASE postgres TO
   PUBLIC` was restored as instructed. `REVOKE CONNECT ON DATABASE muhafiz
   FROM PUBLIC` remains in force (intentional; both app roles hold explicit
   grants).

4. **On loopback the evaluator password is not a boundary.** `pg_hba`
   grants `trust` to `127.0.0.1`/`::1` and `scram-sha-256` elsewhere. The
   CONNECT denial is what contains the evaluator, and it holds under either.

5. **Production still contains a 29-vertex `evidence_graph_eval` fixture**
   from migration 011, untouched and now redundant. Left in place pending a
   decision; removing it is unrelated to this containment.

---

## 10. Exact Phase 6 next step

```bash
python scripts/rebuild_age_eval.py --execute     # fresh snapshot
python scripts/run_aggregate_three_way.py        # 42-case corpus
```

Rebuild first so the AGE route's figures describe current production, then
run the corpus to measure whether the AGE route remains correct and
valuable enough to justify further development. Phase 6 was **not** run as
part of Phase 5C.
