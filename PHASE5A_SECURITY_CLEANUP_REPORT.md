# Phase 5A — Security Cleanup Report

**Objective:** return the repository and PostgreSQL role state to the
pre-Phase-5 security-design state, without touching the restored evidence
graph.

**Result: complete. No graph or application data was modified.**

Repository `/c/Users/PMLS/Desktop/Muhafiz 2` · branch
`feat/aggregate-engine-phase0` · HEAD `7fdd54c` · container
`muhafiz-postgres` · database `muhafiz`.

---

## 1. Initial state

| Item | State before cleanup |
|---|---|
| HEAD | `7fdd54c` — "Phase 4 evaluation — 42-case sweep…" |
| Modified source | `src/config.py`, `src/graph/age_client.py`, `src/pipeline/aggregate/route_age.py` (+140 / −6) |
| `migrations/033_graph_readonly_role.sql` | present, 6,144 bytes, **untracked — never committed** |
| `.env` | git-ignored; 132 lines; 1× `GRAPH_READONLY_DATABASE_URL`, 1× `MODEL_SERVER_BASE_URL` |
| `muhafiz_graph_readonly` | exists · `rolsuper=f` · `rolbypassrls=f` · **0 table privileges** · **0 routine privileges** · no schema USAGE · `CONNECT=true` · `default_transaction_read_only=on` |
| Active sessions as that role | **0** |
| Running python/node processes | **none** |
| Default-ACL dependencies on the role | **none** |

**No stop condition was triggered.** No unexpected state found.

---

## 2. Files restored (Step 1)

Restored from HEAD via `git restore --source=HEAD`, scoped to three paths.
No global reset, no checkout, no other file touched.

| File | Verification |
|---|---|
| `src/config.py` | `git diff` empty — content identical to HEAD |
| `src/graph/age_client.py` | `git diff` empty — content identical to HEAD |
| `src/pipeline/aggregate/route_age.py` | `git diff` empty — content identical to HEAD |

**Note on `route_age.py` still showing ` M` in `git status`.** This is CRLF
line-ending normalisation on Windows checkout, not a content difference.
Verified explicitly: 302 lines, 302 CR characters, 302-byte delta
(12,782 on disk vs 12,480 in the blob), and **SHA-256 identical after LF
normalisation** (`f4aa12ab…835d7b` on both). `git diff` is empty. The same
behaviour was observed on the Phase 4 JSON restore.

---

## 3. Migration 033 removal (Step 2)

- Confirmed untracked before deletion (`git ls-files --error-unmatch` → not
  known to git).
- Deleted `migrations/033_graph_readonly_role.sql`.
- Migration count **31 → 30**.
- `git status --short migrations/` → clean. **No other migration modified.**

---

## 4. Role cleanup (Step 3)

Pre-conditions confirmed before any destructive statement: 0 table
privileges, 0 routine privileges, 0 active sessions, no default-ACL
dependencies, source no longer references the variable (Step 5), and no
running Muhafiz process.

Executed in the prescribed order:

```sql
-- 4a. revoke (defensive; only CONNECT was actually held)
REVOKE ALL ON ALL TABLES IN SCHEMA evidence_graph FROM muhafiz_graph_readonly;
REVOKE ALL ON SCHEMA evidence_graph FROM muhafiz_graph_readonly;
REVOKE ALL ON SCHEMA ag_catalog FROM muhafiz_graph_readonly;
ALTER DEFAULT PRIVILEGES IN SCHEMA evidence_graph
    REVOKE ALL ON TABLES FROM muhafiz_graph_readonly;
REVOKE ALL ON DATABASE muhafiz FROM muhafiz_graph_readonly;

-- 4b. reset role configuration
ALTER ROLE muhafiz_graph_readonly RESET default_transaction_read_only;

-- 4c. drop
DROP ROLE muhafiz_graph_readonly;          -- rc=0
```

**After cleanup:**

| Role | State |
|---|---|
| `muhafiz_graph_readonly` | **dropped — absent from `pg_roles`** |
| `muhafiz_app` | present · `rolsuper=f` · `rolbypassrls=f` · rolconfig `(none)` — **unaltered** |
| `muhafiz_mcp_readonly` | present · `rolsuper=f` · `rolbypassrls=f` · rolconfig `(none)` — **unaltered** |
| Residual `rolconfig` anywhere | **none** |

No other role altered. No database dropped or recreated. No DROP/TRUNCATE/
DELETE against any graph or application table.

---

## 5. Environment cleanup (Step 4)

`.env` was **not** reverted wholesale. Exactly one variable and its
four-line comment block were removed.

| | Before | After |
|---|---|---|
| Line count | 132 | 126 |
| `GRAPH_READONLY_DATABASE_URL` | 1 | **0** |
| `MODEL_SERVER_BASE_URL` | 1 | **1 — preserved** |

No secret value printed at any point. `.env.phase5.bak` left unchanged. No
other environment variable touched.

---

## 6. Reference search (Step 5)

Searched `src/`, `scripts/`, `tests/`, `migrations/`, `.env` for:
`GRAPH_READONLY_DATABASE_URL`, `execute_cypher_readonly`,
`get_readonly_pool`, `_readonly_pool`, `close_readonly_pool`,
`muhafiz_graph_readonly`.

**Result: zero `.py`, `.sql` or `.env` source hits.**

Every hit was a stale `__pycache__/*.pyc` bytecode artifact
(`age_client.cpython-312.pyc`, `config.cpython-312.pyc`,
`route_age.cpython-312.pyc`). These are build outputs that regenerate from
the restored sources and carry no dependency; they were **not** deleted,
since the brief forbids touching unrelated files.

One documentation mention remains, and is intentionally left alone per the
brief: `PHASE5_AGE_SECURITY_DESIGN_REVIEW.md` (the incident record).

**Conclusion: no production-code dependency on the failed mechanism.**

---

## 7. Phase 4 integrity (Step 6)

All eight verified byte-identical to HEAD:

| File | Status |
|---|---|
| `src/pipeline/aggregate/routes.py` | IDENTICAL |
| `src/pipeline/aggregate/route_structured.py` | IDENTICAL |
| `src/pipeline/aggregate/cypher_guard.py` | IDENTICAL |
| `src/pipeline/aggregate/route_semantic.py` | IDENTICAL |
| `src/pipeline/aggregate/reconcile.py` | IDENTICAL |
| `scripts/run_aggregate_three_way.py` | IDENTICAL |
| `tests/test_aggregate_three_way.py` | IDENTICAL |
| `docs/aggregate-shadow/phase4_three_way_report.json` | IDENTICAL |

---

## 8. Database verification (Step 7) — read-only

| Object | Expected | Actual |
|---|---|---|
| `Case` | 73 | **73** ✅ |
| `Person` | 430 | **430** ✅ |
| `PhoneNumber` | 2 | **2** ✅ |
| `BELONGS_TO_CASE` total | 3605 | **3605** ✅ |
| `BELONGS_TO_CASE` active | 3383 | **3383** ✅ |
| `FILED_AT` | 73 | **73** ✅ |
| `ASSIGNED_TO` | 144 | **144** ✅ |
| `CITES` | 9 | **9** ✅ |
| `PART_OF` | 146 | **146** ✅ |
| `public.cases` | 73 | **73** ✅ |
| `public.documents` | 1012 | **1012** ✅ |
| `public.chunk_fulltext` | 7716 | **7716** ✅ |

**No graph label, edge, or application table changed.** No Cypher mutation
was executed. The failed boundary was **not** re-tested — the previous
incident already established it.

---

## 9. Final git status (Step 8)

```
 M src/pipeline/aggregate/route_age.py      ← CRLF only; content == HEAD (§2)
?? .env.phase5.bak
?? PHASE5_AGE_SECURITY_DESIGN_REVIEW.md
?? SHARE/
?? docker-compose.override.yml
?? "qa review.zip"
?? "qa review/"
```

- `src/config.py` and `src/graph/age_client.py` no longer appear — fully clean.
- `migrations/033_graph_readonly_role.sql` **absent**; 30 migrations remain.
- **Nothing staged. No commit made.**

Remaining entries are pre-existing or legitimate: the `.env` backup, the
Phase 5 design review (deliverable of the prior task), and the untracked
`SHARE/`, `docker-compose.override.yml`, `qa review*` items that predate
this work.

---

## 10. Confirmation of scope

Nothing below was run:

- ❌ aggregate harness / Phase 4 rerun / Phase 5 rerun
- ❌ report regeneration
- ❌ destructive probes or write-permission tests
- ❌ ingestion or re-projection
- ❌ `evidence_graph_eval` creation or modification
- ❌ replacement security architecture
- ❌ migration execution
- ❌ modification of `muhafiz_app`, `muhafiz_mcp_readonly`, or `postgres`
- ❌ database drop/recreate

**Abandoned and now fully removed:** `muhafiz_graph_readonly`,
`GRAPH_READONLY_DATABASE_URL`, `execute_cypher_readonly()`, the read-only
routing in `route_age.py`, and migration 033.
