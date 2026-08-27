# Running Muhafiz from scratch

Everything needed to bring the whole stack up on a fresh machine, and to stop it
cleanly. Written against a live, tested run on Windows (backend on `:8001` —
matches both frontends' Vite proxy target in `frontend/vite.config.ts` and
`admin-frontend/vite.config.ts`; audit finding F-10 caught this doc
previously saying `:8000`, which the proxies never pointed at — chat app on
`:5173`, admin app on `:5174`, Postgres+Apache AGE in Docker on
`:5432`) — including the actual errors hit while bringing it up, not a
theoretical happy path.

- [1. Prerequisites](#1-prerequisites)
- [2. Environment variables](#2-environment-variables)
- [3. Install](#3-install)
- [4. First-run setup (migrations)](#4-first-run-setup-migrations)
- [5. Start everything](#5-start-everything)
- [6. Smoke test](#6-smoke-test)
- [7. Ingesting documents / filling gaps in the knowledge base](#7-ingesting-documents--filling-gaps-in-the-knowledge-base)
- [8. Stop everything](#8-stop-everything)
- [9. Troubleshooting](#9-troubleshooting)

See [`README.md`](README.md) for what the system actually does; this file is
only the mechanics of getting it running.

---

## 1. Prerequisites

| Tool | Version | Notes |
|---|---|---|
| Python | 3.11+ | 3.13 works |
| Node.js | 20+ | with `npm` |
| Docker Desktop | any recent | **must be running** before anything else — see the troubleshooting note below, this alone caused a failed first attempt |
| PostgreSQL | via `docker-compose.yml`, **not** a plain Postgres image | The Phase 4 graph layer requires the Apache AGE extension. `docker-compose.yml` is pinned to `apache/age:release_PG16_1.5.0`; a stock `postgres:16` image will fail `CREATE EXTENSION age`. |
| Git | any | |

This repo runs on a local/self-hosted Postgres only — there is no hosted-DB
fallback path.

**API keys you should have** (cloud fallback path — see §2 for why this is a
fallback, not the primary path):

- **Groq** — https://console.groq.com
- **Google Gemini** — https://aistudio.google.com/app/apikey
- **Tavily** — https://tavily.com (optional; the WEB route degrades without it)

**Local model serving (optional but the intended primary path).** The
finalized stack is Qwen3-14B (reasoning) + Qalb-8B (generation) +
multilingual-e5-large-instruct (embeddings) + bge-reranker-v2-m3 (reranker),
served on any OpenAI-compatible endpoint(s) — vLLM, a bespoke FastAPI wrapper,
an ngrok tunnel to a machine running one, etc. **If you don't have this running,
leave the `LOCAL_*_URL`/`EMBEDDINGS_URL`/`RERANKER_URL` variables empty** (or
point them at a currently-dead endpoint — the app tolerates this: it tries
local first, falls back to Groq/Gemini automatically on failure, and every
call site does this independently, not just generation). Confirmed live in
this session: with those endpoints unreachable, the whole pipeline
(rewriter → router → SQL/RAG/graph route → generation → Verifier) still ran
correctly end to end on the Groq fallback alone.

---

## 2. Environment variables

Copy the template and fill it in:

```bash
cp .env.example .env
```

**`.env.example` is not fully in sync with `src/config.py`** — it predates
several Phase 0/5/7/9 variables. The table below is the actual current set;
cross-check `src/config.py` directly if in doubt.

| Variable | Required | Notes |
|---|---|---|
| `DATABASE_URL` | **yes** | For a real deployment this must point at the least-privilege `muhafiz_app` role (migration 015), **not** the `postgres` superuser — a superuser/`BYPASSRLS` connection silently defeats every RLS policy regardless of correctness. For a quick local dev run against the bundled `docker-compose.yml` before that role is provisioned, `postgresql+asyncpg://postgres:dev@localhost:5432/muhafiz` still works, but see §4's "Least-privilege roles" note before treating that as production-ready. Must start `postgresql+asyncpg://`. |
| `REQUIRE_POSTGRES` | no | `true` (default) refuses to start rather than silently falling back to the legacy pre-Phase-1 SQLite schema when `DATABASE_URL` isn't configured. Only set `false` for narrow legacy/local debugging. |
| `MCP_DATABASE_URL` | no | Least-privilege connection string for the MCP SQL route (`muhafiz_mcp_readonly`, migration 009 — SELECT-only on `police_reference_data`). If unset, the MCP Postgres route raises `RuntimeError` on first use rather than falling back to the app's main `DATABASE_URL`. |
| `JWT_SECRET_KEY` | **yes** | Any long random string — `python -c "import secrets; print(secrets.token_hex(32))"`. **The code's own default (`your-secret-key-for-dev`) is an insecure placeholder — don't ship it.** |
| `ENVIRONMENT` | yes | `development` locally (non-secure cookies, so `http://localhost` works). |
| `LLM_PROVIDER` | yes | `groq` or `gemini` — this is the **cloud fallback** identity, not the primary path (see below). |
| `GROQ_API_KEY` / `GEMINI_API_KEY` | yes (as fallback) | `_1`, `_2`, … suffixes enable auto-rotation on rate-limit. |
| `TAVILY_API_KEY` | no | Enables the WEB route. |
| `EMBEDDING_PROVIDER` | yes | **`e5` is the current default** (1024-dim, local multilingual-e5-large-instruct) — **not** the old 384-dim `local` fallback. The live ChromaDB collection in this repo is already at 1024-dim; don't switch this without a full re-embed (`scripts/reingest_kb.py`). |
| `LOCAL_LLM_URL` / `LOCAL_LLM_MODEL` | no | Reasoning endpoint (Qwen3-14B). Empty = skip straight to cloud fallback for reasoning calls. |
| `LOCAL_GEN_LLM_URL` / `LOCAL_GEN_LLM_MODEL` | no | Generation endpoint (Qalb-8B). Empty = skip straight to cloud fallback for the final answer. |
| `EMBEDDINGS_URL` / `RERANKER_URL` | no | Local e5 / bge-reranker-v2-m3 endpoints. Empty = falls back per `EMBEDDING_PROVIDER`'s own logic (see `src/retrieval/embedder.py`) — there is no cross-encoder reranker cloud fallback, it's simply skipped if unset. |
| `MODEL_SERVER_BASE_URL` | no | Convenience base the four vars above can expand from, e.g. `LOCAL_LLM_URL=${MODEL_SERVER_BASE_URL}/llm`. Standard shell-style `${VAR}` expansion — confirmed `python-dotenv` resolves this correctly. |
| `AIR_GAP_MODE` | no | `true` disables the WEB route entirely **and** refuses the cloud LLM fallback outright (fails closed) instead of silently phoning Groq/Gemini. Leave `false` for a normal dev run. |
| `WEB_ALLOWED_DOMAINS` | no | Comma-separated allowlist for the WEB route; has a sensible default. |
| `MAX_RETRIES` / `TOP_K_RETRIEVAL` / `TOP_K_RERANK` / `CHUNK_SIZE` / `CHUNK_OVERLAP` | no | Pipeline tuning, sensible defaults. |

> **Role, not `is_admin`.** `users.role` (`investigator` / `supervisor` /
> `station-admin` / `platform-admin`) is the current access-control field.
> If your database still has an `is_admin` column and no `role` column, your
> schema predates Phase 7 — see §4, migration `006_rbac.sql` fixes this
> (and safely backfills `role` from the old `is_admin` before dropping it).

---

## 3. Install

**Backend** (from the repo root) — a project virtualenv is the normal path;
this session's actual run used a machine with FastAPI/uvicorn already on the
global Python path, which also works if that's your setup:

```bash
python -m venv .venv
source .venv/bin/activate          # Windows Git Bash: source .venv/Scripts/activate
                                    # Windows PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

**Frontends** (two separate apps):

```bash
cd frontend && npm install && cd ..
cd admin-frontend && npm install && cd ..
```

---

## 4. First-run setup (migrations)

The schema lives in two places: the Alembic chain, and plain SQL for
everything from migration 003 onward (the two mechanisms drifted apart early
on — each new plain-SQL migration documents this rather than pretending it
isn't true). **Apply in this exact order** — each depends on the one before it
(the RBAC migration needs `cases` to exist for its `case_assignments` FK; RLS
needs the RBAC role enum; etc.):

```bash
# 1. The SQLAlchemy migration chain (users, sessions, messages, pipeline_runs,
#    documents, projects, police_reference_data, and other baseline tables)
alembic upgrade head

# 2. Admin dashboard + attachments tables
python scripts/apply_migration.py migrations/003_admin_dashboard_and_attachments.sql

# 3. Case data model (cases, documents.case_id, sessions.case_id)
python scripts/apply_migration.py migrations/004_case_model.sql

# 4. Apache AGE graph setup — requires the AGE-enabled Postgres image (§1)
python scripts/apply_migration.py migrations/005_age_graph.sql

# 5. RBAC — role enum, case_assignments, audit_logs (drops the old is_admin
#    column, safely backfilling role from it first)
python scripts/apply_migration.py migrations/006_rbac.sql

# 6. Verifier fields on pipeline_runs (Phase 6)
python scripts/apply_migration.py migrations/007_verifier_fields.sql

# 7. Row-Level Security policies on cases/documents/sessions/pipeline_runs
python scripts/apply_migration.py migrations/008_rls_policies.sql

# 8. Least-privilege role for the MCP SQL route (Phase 1, Module 1.2) —
#    SELECT-only on police_reference_data, nothing else
python scripts/apply_migration.py migrations/009_mcp_readonly_role.sql

# 9. Fixes migration 008's NULL-vs-NULL RLS bug, adds a messages policy (Phase 2)
python scripts/apply_migration.py migrations/010_rls_null_case_fix.sql

# 10. Second, physically separate AGE graph for the entity-resolution eval
#     harness (Phase 3, Module 3.1) — keeps eval fixtures out of real cases
python scripts/apply_migration.py migrations/011_age_eval_graph.sql

# 11. police_station on users, anchors station-admin case-assignment scoping (Phase 5)
python scripts/apply_migration.py migrations/012_user_station.sql

# 12. case_id on generated_files, for download_file's case-level scoping (Phase 5)
python scripts/apply_migration.py migrations/013_generated_files_case_id.sql

# 13. Indexes for the admin analytics queries (Phase 7)
python scripts/apply_migration.py migrations/014_analytics_indexes.sql

# 14. Least-privilege role for the application's own runtime connection
#     (Phase 11 / issues.md's 13th Critical finding) — see the dedicated
#     note right after this list; this one needs manual follow-up steps,
#     it is not "just run it and move on" like the others.
python scripts/apply_migration.py migrations/015_app_least_privilege_role.sql

# 15. Community detection + summarization storage (GraphRAG-inspired layer,
#     additive on top of the Phase 4 evidence_graph) — community_membership,
#     community_summaries, community_runs
python scripts/apply_migration.py migrations/016_community_detection.sql

# 16. Raw pre-filter node/edge counts on community_runs, so
#     check_community_staleness.py has a genuinely comparable baseline
#     against the live graph's raw counts (not the post-filter ones)
python scripts/apply_migration.py migrations/017_community_runs_raw_counts.sql

# 17. cases.conflicts_checked_at — distinguishes "conflict detection ran and
#     found nothing" from "detection never ran yet" (agent-harness
#     reconciliation, Unit 6)
python scripts/apply_migration.py migrations/018_case_conflicts_checked_at.sql

# 18. Per-message degradation trace on messages (tools_used/degraded_from/
#     caveats), so investigators keep the same "what worked, what failed"
#     transparency the admin Run History page has, durably, in their own chat
python scripts/apply_migration.py migrations/019_message_degradation_trace.sql

# 19. Pre-create the AGE `Date` vlabel and `CITES` elabel — both already
#     written live outside migration 005/011's static list (M8 of the
#     Muhafiz Data API migration); closes the same concurrent-first-write
#     race migration 005 pre-creates labels to prevent
python scripts/apply_migration.py migrations/020_age_date_and_cites_labels.sql

# 20. Identity index — a plain Postgres side table backing entity-resolution
#     hard-block lookups with a real indexed primary-key match instead of an
#     unindexed AGE label scan (Graph Scale & Schema Expansion, Milestone A1)
python scripts/apply_migration.py migrations/021_identity_index.sql

# 21. Persistent full-text index (chunk_fulltext) for BM25 keyword search —
#     replaces rebuilding an in-memory BM25 index over the full scoped
#     corpus on every query (Graph Scale & Schema Expansion, Milestone A2)
python scripts/apply_migration.py migrations/022_chunk_fulltext_index.sql

# 22. Pre-create the AGE `PoliceStation`/`District` vlabels and `FILED_AT`
#     elabel — jurisdiction graph nodes (Graph Scale & Schema Expansion,
#     Milestone B1)
python scripts/apply_migration.py migrations/023_jurisdiction_graph_labels.sql

# 23. Pre-create the AGE `Officer` vlabel and `ASSIGNED_TO` elabel — officer
#     identity resolution (Graph Scale & Schema Expansion, Milestone B2)
python scripts/apply_migration.py migrations/024_officer_graph_labels.sql

# 24. Pre-create the AGE `RELATED_TO` elabel — person-relationship edges
#     (Graph Scale & Schema Expansion, Milestone C1)
python scripts/apply_migration.py migrations/025_related_to_label.sql

# 25. Pre-create the AGE `CROSS_VERSION_OF` elabel — cross-version edge
#     (Graph Scale & Schema Expansion, Milestone C4)
python scripts/apply_migration.py migrations/026_cross_version_of_label.sql

# 26. Pending-candidate priority index — a plain Postgres side table backing
#     graph_review.list_pending()'s hot query with a real index instead of an
#     unindexed AGE edge-label scan (Graph Scale & Schema Expansion,
#     Milestone D1)
python scripts/apply_migration.py migrations/027_pending_candidate_priority.sql

# 27. Ingestion run quality rollup (ingestion_run_quality) — one row per
#     ingestion run, entity-resolution tier counts (TIER_CNIC_AUTO/
#     TIER_FLAGGED/TIER_REVIEW/TIER_NEW), closing the gap where
#     ingestion_jobs has no case_id and bulk ingestion paths never wrote a
#     row (Ingestion Quality Control at Scale, Module G1)
python scripts/apply_migration.py migrations/028_ingestion_run_quality.sql

# 28. Entity-resolution consistency findings — a continuous background check
#     that re-diffs a sample of resolved SAME_AS matches' original scoring
#     signal against a freshly recomputed one, looking for degradation
#     (Ingestion Quality Control at Scale, Module G3)
python scripts/apply_migration.py migrations/029_entity_resolution_consistency_findings.sql
```

All of these are idempotent — safe to re-run. **Check what's actually applied
before assuming**, rather than trusting this list blindly — the live database
in this session had migrations 1–5 already done but 6–8 missing (from an
earlier partial setup), which surfaces as very confusing failures downstream
(the ORM model expects a `role` column that doesn't exist yet, etc.) if you
don't catch it first:

```bash
python -c "
import asyncio, asyncpg, os
from dotenv import load_dotenv
load_dotenv()
url = os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://')
async def main():
    conn = await asyncpg.connect(url)
    cols = await conn.fetch(\"SELECT column_name FROM information_schema.columns WHERE table_name='users'\")
    print('users columns:', [c['column_name'] for c in cols])
    tables = await conn.fetch(\"SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename\")
    print('tables:', [t['tablename'] for t in tables])
    await conn.close()
asyncio.run(main())
"
# Expect: users has 'role' (not 'is_admin'); tables include case_assignments
# and audit_logs. If either is missing, migrations 6+ haven't run.
```

The same blind-trust risk applies at the other end of the list — a setup that
stopped partway through 016-029 fails just as confusingly downstream (e.g.
`community_membership` missing breaks Global Search's community summaries,
`chunk_fulltext` missing breaks BM25 retrieval). Check the full-list end
state too:

```bash
python -c "
import asyncio, asyncpg, os
from dotenv import load_dotenv
load_dotenv()
url = os.environ['DATABASE_URL'].replace('postgresql+asyncpg://', 'postgresql://')
async def main():
    conn = await asyncpg.connect(url)
    tables = await conn.fetch(\"SELECT tablename FROM pg_tables WHERE schemaname='public' ORDER BY tablename\")
    names = {t['tablename'] for t in tables}
    print('entity_resolution_consistency_findings' in names, 'ingestion_run_quality' in names)
    await conn.close()
asyncio.run(main())
"
# Expect: True True. If either is False, migrations 16-29 (or at least 028/029)
# haven't run yet.
```

If `apply_migration.py` fails with a connection error, Postgres isn't
reachable — confirm Docker Desktop is actually running (not just
`docker compose up -d` issued into a dead daemon — see §9) and `DATABASE_URL`
in `.env` points at the right host/port/credentials.

**Least-privilege roles (migrations 009 and 015) need manual follow-up —
applying the SQL alone does not finish the job.** Both create a role with
`LOGIN` but no password, and neither migration repoints your app's
connection string for you:

- **`muhafiz_mcp_readonly`** (migration 009, Phase 1) — the MCP SQL route's
  connection. Set a password
  (`ALTER ROLE muhafiz_mcp_readonly WITH PASSWORD '...'`), then set
  `MCP_DATABASE_URL` in `.env` to a connection string using it (see the
  commented example in `.env.example`). Verify with
  `python scripts/verify_mcp_role.py` (needs live Postgres). Until
  `MCP_DATABASE_URL` is set, the MCP Postgres route raises `RuntimeError` on
  first use — this is deliberate fail-closed behavior, not a bug.
- **`muhafiz_app`** (migration 015, `issues.md`'s 13th Critical finding) —
  the application's own normal runtime connection (`DATABASE_URL` itself).
  **This one matters most**: as long as `DATABASE_URL` still points at the
  `postgres` superuser, every RLS policy from migrations 008/010 is silently
  inert regardless of correctness — a Postgres superuser or `BYPASSRLS` role
  unconditionally bypasses row-level security. Set a password
  (`ALTER ROLE muhafiz_app WITH PASSWORD '...'`), repoint `DATABASE_URL` at
  it (`postgresql+asyncpg://muhafiz_app:<password>@localhost:5432/muhafiz`),
  restart the backend, and confirm it starts cleanly and every route still
  works (this role also needs `CREATE ON SCHEMA public` for
  `init_postgres()`'s startup `create_all()` call — migration 015 grants
  this already). Verify with `python scripts/verify_app_role.py` (needs live
  Postgres). Existing `.env` files pointing at the superuser role do **not**
  auto-update; this is a manual, per-deployment step.

**Vector store.** Embeddings live in ChromaDB, a local persistent collection
at `CHROMA_PERSIST_DIR` (default `./data/chroma_db`) — no separate setup
needed. **If this collection predates the switch to `e5` (1024-dim)**, it's
still at the old 384-dim or 3072-dim shape and needs a full wipe + re-ingest
(`python scripts/reingest_kb.py`) — Chroma pins one embedding dimension per
collection and won't tell you it's silently returning garbage otherwise.

**Create an admin user**, if you don't already have one — check first
(`SELECT email, role FROM users WHERE role = 'platform-admin';`), since a
prior setup on the same database may already have one:

```bash
python scripts/create_admin.py
# Creates a platform-admin account with a default password.
# CHANGE THIS PASSWORD before any real deployment.
```

Or promote an existing account directly in Postgres:

```sql
UPDATE users SET role = 'platform-admin' WHERE email = 'you@example.com';
```

**Populate the knowledge base and cases** — see §7. The bundled synthetic
dataset (`data/memory/`, 119 documents / 34 cases) is more useful for a real
demo than an empty corpus; §7 covers ingesting it (and specifically the one
document confirmed missing from a prior ingestion pass in this session).

---

## 5. Start everything

There is **no separate worker or queue** — background work (memory updates,
graph extraction, conflict detection) runs on the backend's own event loop.
Bring things up in this order: **Docker Desktop → Postgres container →
backend → frontends.** Skipping ahead is the single most common failure mode
here (confirmed directly — starting `docker compose up -d` before Docker
Desktop itself had finished starting produced a
`failed to connect to the docker API at npipe:...` error, not a helpful
"still starting" message).

```bash
# 1. Make sure Docker Desktop is actually running first (see §9 if unsure)

# 2. Start Postgres — only the postgres service, not vllm (see the note below)
docker compose up postgres -d

# Wait for it to report healthy before moving on:
docker inspect --format='{{.State.Health.Status}}' muhafiz-postgres
# (poll until this prints "healthy" — took ~10-15s in this session)
```

> **Don't `docker compose up -d` the whole file unless you mean to.**
> `docker-compose.yml` also defines a `vllm` service — an *example*
> configuration for serving Qwen3-14B via vLLM, not something you want
> starting by accident. It expects a `HUGGING_FACE_HUB_TOKEN`, will try to
> download a multi-GB model checkpoint, and needs a GPU with real VRAM
> headroom. If you're using an already-running local model server (ngrok
> tunnel or otherwise) or pure cloud fallback, `docker compose up postgres -d`
> is what you want.

```bash
# Terminal 1 — API  (http://localhost:8001)
source .venv/bin/activate          # if using a venv
uvicorn src.main:app --host 127.0.0.1 --port 8001 --reload

# Terminal 2 — Chat app  (http://localhost:5173)
cd frontend && npm run dev

# Terminal 3 — Admin app  (http://localhost:5174)
cd admin-frontend && npm run dev
```

**Start the API first** — both frontends proxy `/api` to
`http://127.0.0.1:8001` and will error until it is up. Confirm it's healthy:

```bash
curl http://localhost:8001/health
# {"status":"ok", ..., "documents_in_store": <chunk count>}
```

If starting both frontends **at once** on a memory-constrained machine, see
§9 — this session hit an actual out-of-memory crash doing exactly that on
first run (Vite's dependency pre-bundling step is the spike; starting them a
few seconds apart, or one at a time, avoids it).

**URLs**

| App | URL | Login |
|---|---|---|
| Chat | http://localhost:5173 | register a new account in the UI |
| Admin | http://localhost:5174 | an account with `role = platform-admin` (or `supervisor` for the Review Queue / Audit Logs pages specifically) |

---

## 6. Smoke test

After Postgres + the API are up (frontends optional for this check — the API
alone is enough to confirm the pipeline works):

```bash
python scripts/_smoketest.py
```

This registers a throwaway account, logs in, hits `/api/auth/me` (confirm it
returns `role`/`is_admin` without erroring — this is exactly the check that
would catch a migration-006-not-applied state), and sends a real chat message
through the full SSE pipeline. A working run looks like this (actual output
from this session, cloud-fallback path, no local model server reachable):

```
register: 200 {"id":"...","email":"...","role":"investigator","is_admin":false,...}
login: 200 {"message":"Login successful"}
me: 200 {"id":"...","role":"investigator","is_admin":false,...}
chat status: 200
[query_rewriter] done - Rewritten: 'What PPC section covers mobile phone theft?'
[router] done - Route decided: SQL
[retrieval] done - Extracted: {'category': 'penal_code', 'subject': 'Mobile/Vehicle Theft', ...}
[retrieval] done - Found 3 rows
[citation_validator] done - The reference to Section 379 PPC ...
[response] done - Response generated (203 chars)
[memory] done - Saved to session
```

`[citation_validator] done` is the Verifier's event name in the trace (a
historical label — the module underneath is `src/pipeline/verifier.py`, not
the deleted `citation_validator.py`). If it's absent for a RAG/SQL/GRAPH-style
route, that's a real problem, not a quiet feature.

Then in a browser:

1. Open http://localhost:5173, register, and send *"What PPC section covers
   mobile phone theft?"* — you should see the live status trace, then a
   grounded answer citing Section 379 PPC.
2. Open http://localhost:5174, log in as an admin — the **Dashboard** should
   show a non-zero chunk count, real request/latency charts, and a
   **Grounding pass rate** card.

---

## 7. Ingesting documents / filling gaps in the knowledge base

The bundled synthetic dataset (`data/memory/README.md` has the full design)
has a 96-document POC ingestion subset in `data/documents/` — everything not
`handwritten`-tier (OCR is explicitly out of scope for this build). Bulk-load
it with:

```bash
python scripts/reingest_kb.py
# Full wipe + re-ingest of config.DOCUMENTS_DIR. Use scripts/reingest_kb_resume.py
# instead if a prior run was interrupted partway (it resumes without wiping).
```

**Verify what's actually landed, don't assume the count matches the file
count** — this session found a real, standing gap this way:
`FIR-2026-ARMS-001.pdf` (case `CASE-DRY-001`, "clean" rendering tier, present
on disk) was **not** in the live ChromaDB collection, despite 95 of the other
96 eligible documents being there. Check with:

```bash
python -c "
import chromadb, os
client = chromadb.PersistentClient(path='data/chroma_db')
coll = client.get_collection('muhafiz_kb')
res = coll.get(limit=500, include=['metadatas'])
ingested = set(m.get('source') for m in res['metadatas'] if m)
on_disk = set(f for f in os.listdir('data/documents') if f.endswith('.pdf'))
print('missing from Chroma:', sorted(on_disk - ingested))
"
```

To ingest a single missing file with its `case_id` attached (which also
triggers Phase 4's graph extraction — structured fields, NER, domain
entities, entity resolution — for that document, not just the vector store):

```bash
python scripts/_ingest_arms001.py
# Or adapt it: ingest_file(path, is_global=True, case_id="CASE-XXX")
```

**This specific file failed three times in this session with real,
reproducible memory errors** — not a code bug, an actual resource ceiling on
the machine at the time (§9 has the exact errors and what freed enough memory
to matter and what didn't — as of writing this, it is still not ingested).
If you hit the same thing: close memory-heavy applications (browser tabs,
other IDEs) before retrying, or retry when the machine is otherwise idle —
Docling's PDF conversion (layout + OCR models) is the specific step that's
memory-hungry, confirmed by watching it fail progressively later in its own
pipeline (model download check → weight loading → page preprocessing) as more
memory was freed between attempts, without ever fully succeeding.

`case_id` is **not** enforced anywhere in the ingestion code (confirmed in
`docs/AUDIT_FINDINGS_2026-07-23.md`) — passing it is a discipline, not
something the function requires. Admin-panel uploads
(`POST /api/admin/kb/upload`) stay `is_global=True` with no case attached by
design; case-specific evidence should be ingested with `ingest_file(...,
case_id=...)` directly, since there's no dedicated case-evidence-upload UI
yet (see README's Known Limitations).

---

## 8. Stop everything

In each terminal press **Ctrl-C**. That's the clean shutdown — the backend
runs its lifespan shutdown, and the Vite dev servers stop immediately.

If a process was backgrounded or a port is stuck:

```bash
# macOS / Linux
lsof -ti:8001 | xargs kill      # repeat for 5173, 5174

# Windows (PowerShell)
Get-NetTCPConnection -LocalPort 8001 | Select-Object -Expand OwningProcess | ForEach-Object { Stop-Process -Id $_ -Force }

# Windows (Git Bash) — find the PID, then taskkill
tasklist | grep -i "node\|python"
taskkill //F //PID <pid>
```

If you also want to stop Postgres:

```bash
docker compose down          # stop, keep data
docker compose down -v       # stop and delete the data volume — irreversible, don't do this on the synthetic dataset's live DB by accident
```

---

## 9. Troubleshooting

Issues actually hit bringing this stack up and running it end to end this
session — not theoretical.

**`failed to connect to the docker API at npipe:////./pipe/dockerDesktopLinuxEngine`.**
Docker Desktop itself isn't running (not just "the postgres container isn't
up" — the whole daemon is down). Launch Docker Desktop and wait for it to
finish starting (10-30s) before running any `docker compose`/`docker`
command — a command issued too early fails with exactly this error rather
than a "please wait" message.

**Migrations 6-8 (`role`, `case_assignments`, `audit_logs`, RLS) silently
missing even though the app "works".** A database can have migrations 1-5
applied (from an earlier setup) and be missing 6-8 without any obvious
startup error — the backend starts fine, `/health` returns 200. It breaks
specifically on anything touching `users.role` or RLS session variables. Run
the check query in §4 before assuming the schema is current.

**Ingestion fails with `std::bad_alloc`, `DefaultCPUAllocator: not enough
memory`, or `The paging file is too small for this operation to complete.
(os error 1455)`.** Genuine system memory exhaustion during Docling's PDF
conversion (it loads OCR + layout models) — not a bug in the ingestion code.
Confirmed in this session: closing the two Vite dev servers (freeing a few
hundred MB) let the conversion progress further (further into model-weight
loading, then into page preprocessing) but still didn't free enough to fully
succeed on a machine with Docker Desktop + an IDE + a browser also running.
Close memory-heavy applications or retry when the machine is more idle;
`scripts/reingest_kb_resume.py`'s per-file subprocess isolation is the
existing mitigation for *cumulative* memory pressure across a bulk run, but
won't help a single file that fails on first attempt due to overall system
pressure.

**Both frontend dev servers crash with `Fatal process out of memory: Zone`
or a Rolldown/`oxc_allocator` panic (`out of memory`) when started together.**
Hit directly in this session running `npm run dev` for `frontend/` and
`admin-frontend/` at the same time on a machine with only ~3GB free RAM out
of 16GB total — Vite's dependency pre-bundling step for both projects
competing for memory simultaneously is what triggers it, specifically the
first time (or after a Vite config change forces "re-optimizing
dependencies"). Fix: start them a few seconds apart, or one at a time and
wait for the first to finish its `[vite] ready in ...ms` line before starting
the second. A crashed dev server sometimes leaves the parent `npm` process
alive with the actual `vite` child dead — check with
`curl -o /dev/null -w "%{http_code}" http://localhost:5173/`; a `000` despite
the process still showing in `tasklist`/`ps` means it's dead and needs a
manual `taskkill`/`kill` + restart, not just a wait.

**Admin dashboard shows "instrumentation not applied" / attachments say "not
enabled".**
Migration 003 hasn't been applied. Run §4.

**Chat answers work but always via cloud (Groq/Gemini), never the local
model.**
Expected if `LOCAL_LLM_URL`/`LOCAL_GEN_LLM_URL`/etc. point at a dead endpoint
(e.g. an expired free-tier ngrok tunnel — these rotate on restart) — the
client tries local first, logs a warning, and falls back automatically. Check
the backend log for `"Local LLM failed: ... Falling back to..."` to confirm
this is what's happening rather than a config typo. Update the tunnel URL (or
`MODEL_SERVER_BASE_URL`) once your local server is back up.

**Retrieval returns nothing / RAG always says "no information".**
Either the knowledge base is empty (see §7), or `EMBEDDING_PROVIDER` doesn't
match how the corpus was embedded (must be `e5` for the current DB — a
mismatch silently returns garbage nearest-neighbors, not an error).

**Login works but every API call 401s afterward.**
Cookies aren't being set/sent. Set `ENVIRONMENT=development` in `.env`
(secure cookies require HTTPS and won't stick on `http://localhost`). Use
`localhost`, not `127.0.0.1`, consistently across frontend and API.

**Admin login rejected for a valid account.**
The account needs `role = platform-admin` (or `supervisor` for the
Review Queue / Audit Logs / Entity Eval pages specifically). Confirm via
`/api/auth/me`, which now returns `role` directly.

**`ModuleNotFoundError` / import errors on backend start.**
The venv isn't active (if using one), or `pip install -r requirements.txt`
didn't finish.

**Windows console crashes on a non-ASCII error (`UnicodeEncodeError:
charmap`).**
Some scripts print characters cp1252 can't encode (Urdu text in a log line,
for instance). Run with `PYTHONIOENCODING=utf-8`, or redirect output to a
file and read it back with a UTF-8-aware tool rather than the raw console.
