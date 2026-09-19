# Phase 5 — AGE Read-Only Security Design Review

**Status: investigation only. Nothing implemented, nothing changed.**
All findings below come from read-only inspection of the restored database
and the repository. No mutation, no migration, no probe was executed.

Repository `/c/Users/PMLS/Desktop/Muhafiz 2` · branch
`feat/aggregate-engine-phase0` · HEAD `7fdd54c` · Apache AGE **1.5.0** on
PostgreSQL 16.3.

---

## 1. Current execution path

```
route_age.py::run()
  └─ cypher_guard.guard(query, snapshot)        ← MANDATORY, refuses or passes
  └─ cypher_guard.expected_columns(query)       ← refuses unaliased RETURN
  └─ age_client.execute_cypher_readonly(...)    ← working-tree change
       └─ get_readonly_pool()                   ← asyncpg pool
            DSN = config.GRAPH_READONLY_DATABASE_URL  (no fallback; raises)
       └─ pool.acquire()  →  _load_age(conn)
            LOAD 'age'                  (best-effort; swallows InsufficientPrivilege)
            SET search_path = ag_catalog, "$user", public   ← NOT guarded
       └─ conn.fetch(sql, json.dumps(params))
            sql = SELECT * FROM cypher('evidence_graph',
                     $cypher$<GENERATED TEXT>$cypher$, $1::agtype) AS (col agtype)
```

**Role executing the query:** `muhafiz_graph_readonly`, via
`GRAPH_READONLY_DATABASE_URL`. The writable `muhafiz_app` pool is untouched
and still serves every other graph caller.

**Transaction:** none explicit. `conn.fetch()` runs in asyncpg's implicit
single-statement transaction. There is no `BEGIN READ ONLY`.

**RLS / cross-case bypass:** the working-tree change *removed* the
`current_rls_active` / `current_cross_case` arming that the Phase 4 version
performed. Those context vars are now never set on this path.

**Mechanism:** ordinary SQL execution. `cypher()` is a normal set-returning
function invoked through a normal `SELECT`. There is no separate protocol,
no sandbox, no special channel.

**Can generated Cypher execute arbitrary AGE mutations?** Yes — subject only
to the guard. The query text is interpolated into the SQL string (AGE's
`cypher()` takes a `cstring`, which libpq cannot bind), so `execute_cypher`'s
own docstring rule — "written in this codebase, never built from
request/user input" — is deliberately violated on this one path. The guard is
what is supposed to earn that exception.

---

## 2. Existing security controls

| Layer | Mechanism | Status |
|---|---|---|
| 1 | `cypher_guard.guard()` — regex/token validation | Active, mandatory |
| 2 | `expected_columns()` — refuses unaliased RETURN | Active |
| 3 | `muhafiz_graph_readonly` DB role | **Ineffective — see §3** |
| 4 | `default_transaction_read_only = on` | **Ineffective — see §3** |
| 5 | Role gate (`supervisor`+) in `route_age.run()` | Active |

---

## 3. Proven failure of the read-only-role approach

Measured during Phase 5, before the restore. With `muhafiz_graph_readonly`
holding **only** `SELECT` on `evidence_graph` tables, and with
`default_transaction_read_only = on` set on the role:

| Operation | Result |
|---|---|
| `CREATE (x:Case {...})` | DB denied — `permission denied for table Case` |
| `MERGE (x:Case {...})` | DB denied — `permission denied for table Case` |
| `MATCH (c:Case) SET c.x=1` | **ALLOWED — 73 rows affected** |
| `MATCH (c:Case) REMOVE c.case_id` | **ALLOWED** |
| `MATCH (c:Case) DETACH DELETE c` | **ALLOWED — destroyed all 73 Case vertices** |

This is how the incident occurred. The damage was subsequently repaired by
full restore; the restored graph is verified against baseline.

### Root cause

Three facts, all confirmed read-only on the restored database:

1. **`cypher()` is `prosecdef = false`** with the default `PUBLIC EXECUTE`.
   It executes with the *calling* role's privileges — so it is not a
   privilege-escalation vector, and equally not a privilege *barrier*.

2. **Label tables inherit from base tables.** Every label is a child of
   `evidence_graph._ag_label_vertex` / `_ag_label_edge`, all owned by
   `postgres`. `GRANT SELECT ON ALL TABLES IN SCHEMA evidence_graph` covered
   parents *and* children — so the grant surface was uniform, and uniform
   SELECT was still not a write barrier.

3. **`CREATE`/`MERGE` were denied but `SET`/`REMOVE`/`DETACH DELETE` were
   not.** That asymmetry is the finding. Operations creating *new* rows hit
   a genuine INSERT check; operations modifying or deleting *existing* rows
   did not surface an equivalent UPDATE/DELETE check through the `cypher()`
   execution path in AGE 1.5.0.

**Therefore: PostgreSQL table-level DML grants are not a reliable control
surface for AGE Cypher mutation, and `default_transaction_read_only` did not
constrain it either.** Migration 033's security claim is false as written and
must not be revived in that form.

**Current state (post-restore):** the role exists, has **0 grants** on
`evidence_graph`, **no** USAGE on `ag_catalog` or `evidence_graph`, and
**still carries** `default_transaction_read_only = on`. Migration 033 was
deliberately not re-applied. In this state the route cannot execute at all —
it would fail at `cypher(unknown, unknown) does not exist`.

---

## 4. AGE privilege / security analysis (AGE 1.5.0)

| Question | Finding |
|---|---|
| Graph / label / function ownership | All `postgres` |
| `cypher()` security definer? | **No** (`prosecdef=f`), `PUBLIC EXECUTE` |
| AGE-native read-only mechanism? | **None.** Of 336 functions in `ag_catalog`, zero match read/permission/grant/role |
| Graph-level ACL? | None. `ag_graph` has no permission column |
| Isolation unit available | A **separate graph** (`evidence_graph_eval` exists, registered, 32 tables) or a separate database |
| Does `SELECT` constrain Cypher writes? | **Demonstrably not** for SET/REMOVE/DETACH DELETE |

AGE 1.5.0 offers **no graph-scoped authorization model**. Access control is
whatever PostgreSQL provides at the table level, and §3 shows that is not a
dependable write boundary through `cypher()`.

---

## 5. Cypher guard analysis — what it actually enforces

`_WRITE_CLAUSES` = create, merge, delete, detach, set, remove, drop, load,
foreach, call, using, grant, revoke, alter, insert, update, truncate, copy —
matched as `\b<word>\b` against the query **after string literals are
blanked** and lowercased.

Tested in-process (no database contact):

| Construct | Guard verdict |
|---|---|
| `SET` | ❌ refused (`write_operation`) |
| `REMOVE` | ❌ refused |
| `CREATE` | ❌ refused (+ `bad_opener`) |
| `MERGE` | ❌ refused (+ `bad_opener`) |
| `DELETE` / `DETACH DELETE` | ❌ refused |
| `CALL` procedure | ❌ refused (+ `bad_opener`) |
| Multi-statement (`;`) | ❌ refused |
| `$cypher$` delimiter escape | ❌ refused |
| Inline string literal | ❌ refused |
| Backtick-quoted `` `SET` `` | ❌ refused |
| Comment-hidden `/*x*/ SET` | ❌ refused |
| Legitimate read | ✅ passes |

### Two confirmed gaps

| Evasion | Bytes | Guard |
|---|---|---|
| `S​ET` (zero-width space inside SET) | `53 200b 45 54` | ✅ **PASSES** |
| `ЅET` (U+0405 Cyrillic Ѕ homoglyph) | `405 45 54` | ✅ **PASSES** |

Both defeat `\bset\b` because the string is not ASCII and NFKC normalisation
does **not** fold either form. The guard has no Unicode normalisation, no
confusable-character folding, and no ASCII-only precondition.

**Honest qualification — these are guard gaps, not proven exploits.** AGE's
lexer would very likely reject both as syntax errors, so they probably do not
*execute* a write. I did not test that, because testing would require running
a candidate mutation against a live graph, which is forbidden and is exactly
what caused the incident. What is proven is that **a token-based guard over
Unicode text is not a sound basis for a security boundary** — it is a
correctness aid that happens to catch the common cases.

**Structural weaknesses independent of the two evasions:**
- Regex over text, not a parse tree — it cannot know where a token sits.
- `_ALLOWED_OPENERS` permits `MATCH`/`WITH`, so a query may open legally and
  carry a mutation later; only the keyword scan stops it.
- `_strip_strings` handles `'...'` and `"..."` but not backtick identifiers
  or `$$`-style quoting.
- Blanket `\bcall\b` refusal blocks procedures, but also blocks legitimate
  read-only procedure use — a usability cost, not a security hole.

---

## 6. Architecture comparison

| | Approach | Actually prevents mutation? | Complexity | Failure mode | Safely testable? |
|---|---|---|---|---|---|
| **A** | PG `SELECT`-only role | **NO — disproven** | Low | Silent write | Only in isolation |
| **B** | `default_transaction_read_only` | **NO — disproven** | Trivial | Silent write | Only in isolation |
| **C** | Separate graph / database | **Yes**, for the real graph | Medium | Stale data | Yes |
| **D** | Cypher AST parser validation | Strong, not absolute | High | Parser divergence | Yes (offline) |
| **E** | SQL wrapper around `cypher()` | Partial — wrapper still calls `cypher()` | Medium | Same as A/B | Needs isolation |
| **F** | Read-only replica | **Yes** (physically read-only) | High (infra) | Replica lag | Yes |
| **G** | No arbitrary LLM Cypher; compile from a constrained IR | **Yes — by construction** | Already built | Reduced flexibility | Yes (offline) |

Notes on the weaker options:

- **E** is the seductive one and it does not hold. A SQL wrapper function
  still ultimately invokes `cypher()` with the caller's privileges; unless
  it is `SECURITY DEFINER` owned by a role that itself cannot write, it
  changes nothing — and a `SECURITY DEFINER` wrapper owned by a *writable*
  role makes things strictly worse.
- **F** is genuinely strong but is infrastructure work disproportionate to
  an evaluation-stage route, and introduces replica lag into a corpus whose
  whole value is exact agreement with independently-measured figures.

---

## 7. Recommended design

**Primary: G + C — compile from the constrained IR, and confine any
remaining generated Cypher to a separate graph.**

The project already possesses G. The structured route's
`spec → validator → compiler → executor` chain emits parameterised Cypher
from a closed algebra; it is deterministic, tested, and **cannot express a
mutation at all** because the compiler has no emitter for one. That is a
boundary by construction rather than by inspection, and it is the only
option on the table with that property.

Concretely:

1. **The AGE text-to-Cypher route keeps its research value only if it stays
   independent.** Its purpose is to be a *second, differently-derived*
   computation for reconciliation. Replacing it with the structured
   compiler would collapse the three-way architecture into one route wearing
   two hats — explicitly forbidden by the Phase 4/5 briefs, and it would
   invalidate every agreement result.

2. **So confine it, rather than trusting it.** Point the AGE route at a
   dedicated graph (`evidence_graph_eval`, which already exists and is
   registered) populated as a read-copy of the evidence graph. A generated
   mutation then damages a disposable copy, and the real graph is
   unreachable from that connection because the role has no privileges on
   it at all.

3. **Keep `cypher_guard` mandatory** — as a correctness and cost filter, not
   as the security boundary. Add Unicode normalisation + an ASCII-only
   precondition to close the two confirmed gaps, but do not claim the guard
   *is* the boundary.

4. **Withdraw the role-based claim entirely.** Migration 033 should not be
   revived; a role that cannot write is not achievable through table grants
   in AGE 1.5.0, and pretending otherwise is worse than having no second
   layer, because it invites reliance.

### Why this is stronger

A and B are empirically disproven on this exact installation. D reduces but
does not eliminate risk, and a parser that diverges from AGE's own lexer
fails open. E does not change the privilege calculus. F works but is
disproportionate. **G is the only option where mutation is unrepresentable
rather than merely refused**, and **C is the only containment that survives
a guard bypass.**

---

## 8. Safe validation plan

Nothing below may run against `evidence_graph`.

**Offline (no database):** extend the guard's own test suite with the two
Unicode evasions and a confusables corpus. Pure function calls.

**Isolated (requires approval, and a throwaway environment):** to establish
whether AGE 1.5.0 *executes* the Unicode forms, or whether any role
configuration blocks SET/REMOVE/DETACH DELETE, the experiment needs:

- a **separate Docker container** with its own volume, not `muhafiz-postgres`;
- a **throwaway database** restored from the dump, discarded afterwards;
- an explicit written list of the exact statements to be run, approved in
  advance;
- verification that the target is the throwaway container by
  `current_database()` **before** each statement.

I will not run that experiment in the current environment under any
circumstances, and I would want the container identity check automated
rather than trusted to my own care — the incident happened precisely because
I treated a destructive probe as routine.

---

## 9. Phase 4 compatibility impact

**Unchanged and must stay unchanged:** `routes.py`, `route_structured.py`,
`cypher_guard.py`, `route_semantic.py`, `reconcile.py`,
`run_aggregate_three_way.py`, `tests/test_aggregate_three_way.py` — all
verified byte-identical to `7fdd54c`.

**The three-way architecture is unaffected by this recommendation.** Moving
the AGE route to a separate graph changes *which data it reads*, not how it
reasons or how reconciliation classifies it. The Phase 4 results remain valid
for the graph they were measured against; a re-run against a copy would need
its own baseline.

---

## 10. Exact next implementation steps (not performed)

1. **Revert** the three working-tree source changes — `src/config.py`,
   `src/graph/age_client.py`, `src/pipeline/aggregate/route_age.py`. They
   implement a boundary that does not exist, and their comments assert
   security properties that are false. Leaving them is worse than removing
   them.
2. **Delete** `migrations/033_graph_readonly_role.sql` (untracked, never
   committed) — or retain it renamed as incident evidence with its claim
   struck through.
3. **Clean up the role** (requires approval): `REVOKE` then `DROP`
   `muhafiz_graph_readonly`, and reset `default_transaction_read_only`.
   Revoke must precede drop — a dependent-privileges error otherwise.
4. **Keep** the `.env` `MODEL_SERVER_BASE_URL` tunnel fix. It is unrelated to
   the failed experiment and is required for the semantic route.
5. **Then**, as separate work: guard Unicode hardening (offline, testable),
   and a design for the `evidence_graph_eval` read-copy.

### Verdict on the current Phase 5 source changes

**Completely revert** all three, plus migration 033. Not partially kept: the
read-only pool's entire justification is a security property that does not
hold, and `route_age.py`'s comment block states that property as fact. The
tunnel fix in `.env` is the one Phase 5 change worth keeping.

---

## Appendix — evidence index

| Claim | Source |
|---|---|
| `cypher()` not SECURITY DEFINER, PUBLIC EXECUTE | `pg_proc` |
| Graph/label/function owned by `postgres` | `pg_namespace`, `pg_tables` |
| Label tables inherit base tables | `pg_inherits` |
| AGE exposes no permission function | 336 `ag_catalog` functions, 0 matches |
| `evidence_graph_eval` registered, 32 tables | `ag_catalog.ag_graph`, `pg_tables` |
| Role has 0 grants post-restore | `information_schema.table_privileges` |
| `default_transaction_read_only` persists | `pg_roles.rolconfig` |
| Guard refuses 11 of 13 constructs | in-process `cypher_guard.guard()` |
| Two Unicode evasions pass | in-process, codepoints verified |
