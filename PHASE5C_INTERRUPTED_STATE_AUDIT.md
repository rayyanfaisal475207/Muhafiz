# Phase 5C — Interrupted State Audit

**Status: COMPLETE.** Phase 5C was stopped after infrastructure provisioning
and **before any source code was written**. Production is intact.

Repository `/c/Users/PMLS/Desktop/Muhafiz 2` · container `muhafiz-postgres`
(healthy, up 21h) · audit is **read-only** except one privilege restore noted
in §K, which was not performed.

**The single most important finding:** the interrupted run changed **zero
tracked files**. All of its effects are *external PostgreSQL state*. A git
rollback would therefore remove nothing, and leave everything.

---

## A. Git checkpoint

| Item | Value |
|---|---|
| Branch | `feat/aggregate-engine-phase0` |
| HEAD | `7fdd54c5b67d63ea49a7c1cd7e59050a6cdf09bd` |
| `git diff --stat` | **empty** |
| `git diff --cached --stat` | **empty** |
| `git diff --name-only HEAD` | **empty** |
| `git stash list` | **empty** |

```text
 M src/pipeline/aggregate/route_age.py      ← CRLF-only, proven below
?? .env.phase5.bak
?? PHASE5A_SECURITY_CLEANUP_REPORT.md
?? PHASE5B_ISOLATED_AGE_DESIGN_REVIEW.md
?? PHASE5_AGE_SECURITY_DESIGN_REVIEW.md
?? SHARE/
?? docker-compose.override.yml
?? "qa review.zip"
?? "qa review/"
```

This is **byte-identical to the Phase 5B checkpoint status**. No entry is new.

### `route_age.py` re-proven CRLF-only (not re-used from the 5A report)

| Check | Result |
|---|---|
| `git diff` | empty |
| Disk vs blob size | 12,782 vs 12,480 = **302-byte delta** |
| CR chars / lines | 302 / 302 — **exactly one CR per line** |
| SHA-256 after LF normalisation | `f4aa12ab3ea12883` **on both** |

Content is identical to HEAD. This is a Windows checkout artifact, **not** a
Phase 5C edit.

---

## B. File-by-file change inventory

| Path | Status | What changed | Likely origin | Safe to preserve | Secrets |
|---|---|---|---|---|---|
| `src/pipeline/aggregate/route_age.py` | ` M` | **Nothing** — CRLF only | Windows checkout (pre-5C) | Yes | No |
| `.env` | untracked, ignored | 1 line vs backup: `MODEL_SERVER_BASE_URL` | **Phase 5 tunnel fix — authorised, must remain** | Yes | **YES** |
| `.env.phase5.bak` | `??` | unchanged | Phase 5A | Yes | **YES** |
| `PHASE5_AGE_SECURITY_DESIGN_REVIEW.md` | `??` | unchanged | Phase 5 | Yes | No |
| `PHASE5A_SECURITY_CLEANUP_REPORT.md` | `??` | unchanged | Phase 5A | Yes | No |
| `PHASE5B_ISOLATED_AGE_DESIGN_REVIEW.md` | `??` | unchanged | Phase 5B | Yes | No |
| `SHARE/` | `??` | unchanged | Pre-existing hand-off | Yes | **YES** (dumps) |
| `docker-compose.override.yml` | `??` | unchanged, mtime **2026-08-27** | Pre-existing | Yes | No |
| `qa review/`, `qa review.zip` | `??` | unchanged | Pre-existing | Yes | No |

**Category roll-up**

- **PRE-EXISTING BEFORE PHASE 5C** — every item above.
- **CREATED/MODIFIED BY INTERRUPTED PHASE 5C** — **none.**
- **UNRELATED / ORIGIN UNCERTAIN** — none.
- **GENERATED / TEMPORARY** — `__pycache__/*.pyc` only (ignored).
- **SECRET / LOCAL CONFIGURATION** — `.env`, `.env.phase5.bak`, `SHARE/`; all
  git-ignored, none committed, no value printed in this audit.

### Files the brief flagged for special attention

| File | Result |
|---|---|
| `src/config.py` | Identical to HEAD. **No** `age_eval` / `eval_database` / `eval_graph` reference. |
| `src/graph/age_client.py` | Identical to HEAD. `GRAPH_NAME = "evidence_graph"`; `_dsn()` reads `config.DATABASE_URL`. |
| `src/pipeline/aggregate/route_age.py` | Identical to HEAD (CRLF only). |
| `src/pipeline/aggregate/reconcile.py` | Identical to HEAD; mtime 20:11 (pre-5C). |
| `src/pipeline/aggregate/cypher_guard.py` | Identical to HEAD; mtime 20:08 (pre-5C). |
| `docker-compose.yml` | Identical; mtime **2026-08-28**. |
| `docker-compose.override.yml` | Untracked, mtime **2026-08-27**; declares only a `pgdata` volume, **no eval service**. |
| `.env` / `.env.example` | See §E. |
| `migrations/` | **30 files**, unchanged; latest `032`. No new migration, none applied. |
| `scripts/` | No untracked files. No `rebuild_age_eval_snapshot.py`. |
| `tests/` | No untracked files. No containment tests. |

Source grep for `muhafiz_age_eval|AGE_EVAL|execute_eval_cypher|eval_snapshot|
rebuild_age_eval` returned **only pre-existing hits** referencing the *old*
migration `011_age_eval_graph.sql` (`scripts/eval_entity_resolution.py`,
`scripts/purge_eval_contamination.py`). **No new source wiring exists.**

---

## C. External PostgreSQL changes (this is where Phase 5C actually got to)

### Created

| Object | Detail |
|---|---|
| Database `muhafiz_age_eval` | owner `muhafiz_age_eval_app`; `datacl` default |
| Role `muhafiz_age_eval_app` | LOGIN, NOSUPERUSER, NOCREATEDB, NOCREATEROLE, **NOBYPASSRLS** |
| Extension `age` **1.5.0** in eval DB | plus `plpgsql` |
| Graph `evidence_graph_eval` in eval DB | registered; **0 vertices / 0 edges — EMPTY** |
| Grants in eval DB | `ag_catalog` USAGE; `evidence_graph_eval` USAGE+CREATE; default privileges `arwd` |

### Modified — **on the production database**

Two database-level privilege statements were executed against `muhafiz`:

```sql
REVOKE CONNECT ON DATABASE muhafiz FROM PUBLIC;
REVOKE ALL      ON DATABASE muhafiz FROM muhafiz_age_eval_app;
GRANT  CONNECT  ON DATABASE muhafiz TO muhafiz_app;
GRANT  CONNECT  ON DATABASE muhafiz TO muhafiz_mcp_readonly;
```

Resulting `datacl` on `muhafiz`:
`{=T/postgres,postgres=CTc/postgres,muhafiz_mcp_readonly=c/postgres,muhafiz_app=c/postgres}`

A third statement was executed against the **`postgres` maintenance
database** — this one was my own addition, not part of the brief:

```sql
REVOKE CONNECT ON DATABASE postgres FROM PUBLIC;
```

**No data, no graph, no table, and no schema ACL in production was changed.**
Production schema ACLs are unchanged (`evidence_graph`, `evidence_graph_eval`,
`ag_catalog`, `public` all still `muhafiz_app=U/postgres` as before).
The eval role holds **0 table privileges and 0 routine privileges** in
production. Active sessions as the eval role: **0**.

### Isolation actually achieved (empirically verified)

| From → To | Result |
|---|---|
| `muhafiz_age_eval_app` → `muhafiz` | **DENIED** — `FATAL: permission denied for database "muhafiz"` |
| `muhafiz_age_eval_app` → `postgres` | **DENIED** |
| `muhafiz_age_eval_app` → `muhafiz_age_eval` | allowed |
| `muhafiz_app` → `muhafiz` | allowed (verified through the real app path) |
| `muhafiz_mcp_readonly` → `muhafiz` | allowed |

---

## D. Docker / infrastructure changes

**None.**

| Item | State |
|---|---|
| Containers | `muhafiz-postgres` only — `apache/age:release_PG16_1.5.0`, **healthy** |
| Volumes | `muhafiz2_pgdata`, `muhafiz_pgdata`, `rag-chatbot_pgdata` — all pre-existing |
| Networks | `bridge`, `host`, `none`, `muhafiz2_default`, `muhafiz-main_default`, `muhafiz_default` — all pre-existing |
| New services / compose edits | none (`docker-compose.yml` mtime 2026-08-28) |

The eval database is a **logical database inside the existing container**, not
new infrastructure. Healthcheck targets `-d muhafiz` and still passes.

---

## E. Environment / configuration changes

**Phase 5C added no environment variable.** Verified by comparing variable
*names only* against `.env.phase5.bak` (no values read or printed):

| Check | Result |
|---|---|
| Variable names in `.env` | 52 |
| Variable names in `.env.phase5.bak` | 52 |
| Names added since backup | **none** |
| Names removed since backup | **none** |
| Lines differing | **1** — `MODEL_SERVER_BASE_URL` (the authorised tunnel fix) |
| `muhafiz_age_eval` mentioned in `.env` | **no** |
| `.env` tracked? | No — ignored at `.gitignore:26` |

| Variable | Present | Points to | Tracked |
|---|---|---|---|
| `AGE_EVAL_DATABASE_URL` | **NOT PRESENT** | — | — |
| `AGE_EVAL_GRAPH_NAME` | **NOT PRESENT** | — | — |
| any `AGE_EVAL_*` / `EVAL_*` / `SNAPSHOT*` | **NOT PRESENT** | — | — |
| `GRAPH_READONLY_DATABASE_URL` | **NOT PRESENT** (removed in 5A) | — | — |
| `DATABASE_URL` | present | **production `muhafiz`** | untracked |

### Can partial config misdirect the AGE route? — **No.**

The route's target is unchanged and fully determined by committed code:

- `route_age.py:253` calls `execute_cypher(query, params=..., columns=...)`
  with **no `graph=` argument** → defaults to `GRAPH_NAME`.
- `age_client.py:59` → `GRAPH_NAME = "evidence_graph"`.
- `age_client._dsn()` → `config.DATABASE_URL` → production.

**The AGE route still targets production `evidence_graph`, exactly as it did
at the Phase 5B checkpoint.** There is no half-wired evaluator path, and no
configuration that could send it to the empty evaluation database. The
evaluation database is, at present, entirely inert with respect to the
application.

---

## F. Phase 5C implementation progress matrix

| Component | Status |
|---|---|
| Separate evaluation database | **IMPLEMENTED** (`muhafiz_age_eval`) |
| Evaluation role / credentials | **IMPLEMENTED** (`muhafiz_age_eval_app`) |
| AGE extension in evaluator | **IMPLEMENTED** (1.5.0) |
| Database-level isolation from production | **IMPLEMENTED & VERIFIED** |
| Evaluation graph | **IMPLEMENTED BUT UNVERIFIED** — registered, but **empty (0/0)** and never queried through application code |
| Production-derived snapshot | **NOT STARTED** |
| Snapshot rebuild script | **NOT STARTED** |
| Snapshot metadata | **NOT STARTED** |
| Snapshot validation | **NOT STARTED** |
| Fixed AGE executor (`execute_eval_cypher`) | **NOT STARTED** |
| No-production-fallback behaviour | **NOT STARTED** |
| Resource limits (timeout / max rows / pool) | **NOT STARTED** |
| Snapshot-aware reconciliation | **NOT STARTED** |
| Containment tests | **NOT STARTED** |
| Phase 5C report | **NOT STARTED** |

**Reached: infrastructure only (4 of 15).** No application integration of any
kind. The run stopped at the boundary between provisioning and coding —
the cleanest possible place to be interrupted.

---

## G. Production integrity verification (read-only)

| Object | Phase 5B baseline | Now | |
|---|---|---|---|
| `Case` | 73 | **73** | ✅ |
| `Person` | 430 | **430** | ✅ |
| `SAME_AS` | 4702 | **4702** | ✅ |
| `Incident` | 73 | **73** | ✅ |
| `Officer` | 1155 | **1155** | ✅ |
| `BELONGS_TO_CASE` | 3605 | **3605** | ✅ |
| `public.cases` | 73 | **73** | ✅ |
| `public.documents` | 1012 | **1012** | ✅ |

Graph registrations in `muhafiz`: `evidence_graph`, `evidence_graph_eval` —
both still present. Production `evidence_graph_eval` remains the **29-vertex
/ 57-edge fixture** Phase 5B identified; it was **not** touched, cleared, or
repopulated.

Application path re-verified end-to-end: `muhafiz_app` connects, reads
`cases` = 73, and executes Cypher returning `Case` = 73.

**PRODUCTION IS UNCHANGED.**

---

## H. Risks introduced by partial state

| # | Risk | Severity | Note |
|---|---|---|---|
| 1 | `REVOKE CONNECT ON DATABASE postgres FROM PUBLIC` — my own addition, outside the brief; also stripped a PUBLIC-derived connect right from `muhafiz_app` and `muhafiz_mcp_readonly` | **Low, but unrequested** | No source connects to the maintenance DB; healthcheck uses `-d muhafiz` and is healthy. Reversible in one statement (§K-1). |
| 2 | Empty `evidence_graph_eval` in the eval DB could later be mistaken for a valid snapshot | Low | Nothing references it. Mitigated by this document. |
| 3 | Two graphs now named `evidence_graph_eval` in two databases (29-vertex fixture in prod, empty in eval DB) | Low–Medium | Naming ambiguity for future work; **no runtime effect today**. |
| 4 | Eval role credential exists in a shell-history/session context, not in `.env` | Low | Role has zero production privileges; 0 active sessions. Dropped in §K-2 if we abandon this path. |
| 5 | `REVOKE CONNECT ... FROM PUBLIC` on `muhafiz` | **None — beneficial** | Explicit grants for both app roles verified working. Recommend keeping. |

**No risk requires immediate destructive action.** Nothing was removed or
disabled during this audit.

---

## I. How Phase 5C partial work was preserved

**There was no source work to preserve.** `git diff --name-only HEAD` is
empty, the stash is empty, and no `*phase5c*` file exists anywhere in the
repo. Creating `wip/phase5c-interrupted` would produce an **empty commit**
that falsely implies code exists, so I did not create one — a misleading
checkpoint is worse than none.

Preservation is therefore this document, which records the exact, re-runnable
provisioning sequence:

```sql
-- Evaluator role and database
CREATE ROLE muhafiz_age_eval_app WITH LOGIN PASSWORD '<redacted>'
  NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
CREATE DATABASE muhafiz_age_eval OWNER muhafiz_age_eval_app;

-- Database-level isolation (executed against production)
REVOKE CONNECT ON DATABASE muhafiz FROM PUBLIC;
REVOKE ALL      ON DATABASE muhafiz FROM muhafiz_age_eval_app;
GRANT  CONNECT  ON DATABASE muhafiz TO muhafiz_app;
GRANT  CONNECT  ON DATABASE muhafiz TO muhafiz_mcp_readonly;

-- AGE in the evaluator (in muhafiz_age_eval)
CREATE EXTENSION IF NOT EXISTS age;
GRANT USAGE  ON SCHEMA ag_catalog TO muhafiz_age_eval_app;
GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO muhafiz_age_eval_app;
SELECT create_graph('evidence_graph_eval');
GRANT USAGE, CREATE ON SCHEMA evidence_graph_eval TO muhafiz_age_eval_app;
GRANT SELECT, INSERT, UPDATE, DELETE
  ON ALL TABLES IN SCHEMA evidence_graph_eval TO muhafiz_age_eval_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA evidence_graph_eval
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO muhafiz_age_eval_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA evidence_graph_eval
  TO muhafiz_age_eval_app;
```

No password is recorded here. **Git checkpoint ≠ database checkpoint** — the
above is the database checkpoint.

### External resource disposition

| Resource | Classification |
|---|---|
| Database `muhafiz_age_eval` | **SAFE TO LEAVE TEMPORARILY** — inert, unreferenced; likely reusable by a replica design |
| Role `muhafiz_age_eval_app` | **SAFE TO LEAVE TEMPORARILY** — zero production privileges |
| AGE extension in eval DB | **SAFE TO LEAVE TEMPORARILY** |
| Empty `evidence_graph_eval` (eval DB) | **SAFE TO LEAVE TEMPORARILY** — but see §K-3 if the replica design supersedes it |
| `REVOKE CONNECT` on `muhafiz` | **SHOULD REMAIN** — genuine security improvement, verified non-breaking |
| `REVOKE CONNECT` on `postgres` | **SHOULD BE REVERTED** — unrequested scope creep (§K-1) |
| Production `evidence_graph_eval` (29-vertex fixture) | **UNKNOWN — NEEDS REVIEW** — pre-dates Phase 5C; Phase 5B already questioned its value |
| Docker containers/volumes/networks | **SAFE TO LEAVE** — untouched |

---

## J. Exact recommended state before Phase 5B.1

**Recommended option: C** — keep the neutral evaluation infrastructure,
revert the one unrequested privilege change, and add no application
integration.

Rationale. Option B (return source/config to Phase 5B state) is a **no-op**:
source and config are *already* byte-identical to the Phase 5B checkpoint.
Option A is nearly right but would silently carry my unrequested `postgres`
revoke into the next phase. The only real decision is what to do with the
external objects, and they are provably inert — no source references them, no
environment variable names them, and the AGE route's target is fixed in
committed code to production. They cannot confound a replica-feasibility
investigation.

Keeping them is also positively useful: a read-replica design will need a
separate database and a non-production role, and the isolation property
already demonstrated (`FATAL: permission denied` at connection time, before
any Cypher parses) is exactly the property a replica must also provide. It is
evidence for the 5B.1 investigation, not noise.

**Target state before Phase 5B.1**

1. Source tree at `7fdd54c`, no diffs — **already true.**
2. `.env` with the tunnel fix, no eval variables — **already true.**
3. AGE route targeting production `evidence_graph` — **already true**, and
   should stay that way until a containment design is agreed.
4. `postgres` maintenance-DB CONNECT restored — **one statement, §K-1.**
5. Eval DB/role left in place, documented, unreferenced — **already true.**

---

## K. Exact cleanup steps

**Nothing below has been executed.** Step 1 is the only one I recommend now.

**K-1 — Revert my unrequested maintenance-DB revoke (recommended):**

```sql
GRANT CONNECT ON DATABASE postgres TO PUBLIC;
```

**K-2 — Only if the evaluation-database path is abandoned** (defer until
after Phase 5B.1 decides):

```sql
DROP DATABASE muhafiz_age_eval;   -- verify 0 sessions first
DROP ROLE     muhafiz_age_eval_app;
```

**K-3 — Only if the replica design supersedes the eval graph** (defer):

```sql
-- in muhafiz_age_eval
SELECT drop_graph('evidence_graph_eval', true);
```

**K-4 — Do NOT revert** `REVOKE CONNECT ON DATABASE muhafiz FROM PUBLIC`.
It is a real security improvement and both app roles hold verified explicit
grants.

Production `evidence_graph_eval` (29-vertex fixture) is **left untouched**
pending the Phase 5B.1 decision.
