# ARCHITECTURE.md

> Every statement is traced to a source file. Unestablished facts are marked **UNKNOWN**.

---

## 1. High-level shape

```
┌──────────────────┐        ┌────────────────────┐
│  Chat SPA :5173  │        │  Admin SPA :5174   │     React 19 + Vite
│  (frontend/)     │        │  (admin-frontend/) │
└────────┬─────────┘        └─────────┬──────────┘
         │  cookies + X-CSRF-Token    │
         └──────────────┬─────────────┘
                        ▼
             ┌─────────────────────────┐
             │  FastAPI  (src/main.py) │  uvicorn, single process
             │  CORS · slowapi · SSE   │
             └──────────┬──────────────┘
                        │
    ┌───────────────────┼─────────────────────────────┐
    ▼                   ▼                             ▼
REST routers      /api/chat pipeline           BackgroundTasks
(src/api/*)       ├─ orchestrator.py           + asyncio.create_task
                  └─ harness/supervisor.py     (no external worker)
                        │
    ┌───────────┬───────┴────────┬───────────────┬──────────────┐
    ▼           ▼                ▼               ▼              ▼
DataGateway  ChromaDB      Apache AGE      Local model      External APIs
(Postgres)   (vectors)     (graph, in      server           Groq/Gemini/
             + chunk_       the same       (LLM, embed,     OpenAI/Anthropic
             fulltext       Postgres)      rerank)          Tavily, Muhafiz API,
             (Postgres)                                     MCP postgres server
```

**One process, no separate worker.** [RUN.md](../RUN.md) §5 states this explicitly:
*"There is no separate worker or queue — background work (memory updates, graph
extraction, conflict detection) runs on the backend's own event loop."*

---

## 2. Frontend architecture

### 2.1 Chat app — [frontend/](../frontend/)

- **Entry:** `main.tsx` → `App.tsx` (`BrowserRouter` + `AppRoutes`).
- **Routing:** `react-router-dom` v7. Public `/login`, `/register`; everything
  else nested under `<ProtectedRoute>` → `<AppLayout>` (see
  [FRONTEND_FEATURES.md](FRONTEND_FEATURES.md) §1).
- **State:** zustand, one store per domain —
  `authStore`, `chatStore`, `sessionStore`, `caseStore`, `projectStore`,
  `profileStore`, `themeStore`. `authStore.logout()` calls `reset()` on the
  chat/case/project/session stores so a shared workstation cannot leak state
  across logins.
- **Transport:** [src/lib/api.ts](../frontend/src/lib/api.ts) — one axios instance
  (`baseURL = VITE_API_URL || 'http://localhost:8000/api'`, `withCredentials: true`)
  with a request interceptor that attaches `X-CSRF-Token` on mutating methods and a
  response interceptor that dispatches a global `auth:unauthorized` window event
  on 401.
- **Streaming:** `streamChat()` uses `fetch` (not axios or `EventSource`, because
  the endpoint is a `POST`), reads `response.body.getReader()`, splits on `\n\n`,
  and parses each `data: ` frame. A malformed frame is logged and dropped, not
  thrown. A **90-second stall timeout** (`STREAM_STALL_TIMEOUT_MS`) races each
  `reader.read()`; on stall the reader is cancelled and a `StreamStallError` is raised.
- **Layout:** `ChatPage` is a two-column split — `ChatPanel` (~60%) and a right
  pane that shows either `PipelinePanel` (live step trace) or `CitationPanel`
  (the clicked source).

### 2.2 Admin app — [admin-frontend/](../admin-frontend/)

- **Entry:** `main.tsx` → `App.tsx` with `AuthProvider` outside `BrowserRouter`.
- **Routing:** two-layer guard — `ProtectedLayout` (authenticated?) then
  `RequireRole` per route, redirecting to a per-role home.
- **State:** React Context (`AuthContext`) persisted in `localStorage`; page data
  is fetched into local `useState` per page.
- **Transport:** two axios instances — `api` (`baseURL: '/api/admin'`) and
  `casesApi` (`baseURL: '/api'`), both with the same CSRF interceptor. Both rely
  on the **Vite dev proxy** (`/api` → `http://127.0.0.1:8001`) rather than an
  absolute URL.
- **Charts:** `recharts` via `components/charts.tsx`.

> **Port discrepancy for QA:** both `vite.config.ts` files proxy `/api` to
> `http://127.0.0.1:8001`, while [RUN.md](../RUN.md) and `config.PORT` default the backend
> to **8000**. The chat app is unaffected (it calls an absolute `VITE_API_URL`),
> but the **admin app depends entirely on this proxy** and will fail against a
> backend on 8000.

---

## 3. Backend architecture

### 3.1 Application assembly — [src/main.py](../src/main.py)

1. `logging.basicConfig(level=INFO, ...)` before anything else.
2. `FastAPI(..., lifespan=lifespan)`.
3. `CORSMiddleware` with `allow_origins=config.CORS_ORIGINS`,
   `allow_credentials=True`, `allow_methods=["*"]`, `allow_headers=["*"]`.
4. slowapi limiter registered on `app.state.limiter` +
   `RateLimitExceeded` exception handler.
5. Routers mounted (prefix in `include_router` unless the router sets its own):

| Router | Mount |
|---|---|
| `auth_router` | `/api/auth` |
| `sessions_router` | `/api/sessions` |
| `profile_router` | `/api/profile` |
| `admin_router` | `/api/admin` (self-prefixed) |
| `projects_router` | `/api/projects` |
| `cases_router` | `/api/cases` |
| `case_assignments_router` | `/api/cases/{case_id}/assignments` (self-prefixed) |
| `attachments_router` | `/api/attachments` |
| `graph_review_router` | `/api/admin/graph-review` (self-prefixed) |
| `community_admin_router` | `/api/admin/community` (self-prefixed) |
| `ingestion_quality_admin_router` | `/api/admin/ingestion-quality` (self-prefixed) |

Plus three routes defined inline: `GET /health`, `POST /api/chat`,
`GET /api/files/{file_id}/download`.

### 3.2 Startup (lifespan)

In order:
1. `ensure_directories()` — creates `CHROMA_PERSIST_DIR`, `DOCUMENTS_DIR`, `DB_PATH.parent`.
2. `error_capture.install()` — mirrors every `ERROR`/`CRITICAL` log into `error_logs`.
3. Database init:
   - If `DATABASE_URL` is set: `await init_postgres()`. A `MissingSchemaError`
     is swallowed (already logged with a specific CRITICAL); any other exception
     is logged as a WARNING and **startup continues**, so `/health` can report the
     failure rather than crash-looping. A legacy `pipeline_logs.db` is archived to
     `.db.archived`. `init_db()` (the SQLite side-log schema) runs unconditionally.
   - Else if `REQUIRE_POSTGRES` (default `true`): **`RuntimeError` — refuses to start.**
   - Else: legacy SQLite mode with a `CRITICAL` warning that cases/users/RBAC will not work.
4. `validate_config()` — warnings are logged; **critical** errors raise
   `RuntimeError` only when `ENVIRONMENT == "production"`, otherwise they log
   CRITICAL and the server starts anyway.

### 3.3 Data access layer

`src/data_gateway/` defines a `DataGateway` Protocol (~90 async methods) with a
single implementation, `DirectGateway` (async SQLAlchemy over asyncpg).
`get_gateway()` memoises one module-level instance. The docstring states plainly:
*"This repo has one backend, one connection path — no Supabase REST fallback, no
mode probing."* (Note: a stale `.mcp.json` still references a Supabase MCP server
URL; nothing in `src/` reads it.)

**Two independent connection pools to the same Postgres instance:**

| Pool | Purpose | Config |
|---|---|---|
| SQLAlchemy async engine ([src/database/postgres.py](../src/database/postgres.py)) | All relational tables | `pool_size=15`, `max_overflow=20`, `pool_recycle=1800` |
| asyncpg pool ([src/graph/age_client.py](../src/graph/age_client.py)) | Apache AGE Cypher | `min_size=1`, `max_size=10`, `statement_cache_size=0` |

`age_client` re-runs `LOAD 'age'; SET search_path = ag_catalog, "$user", public;`
at the **start of every `execute_cypher()` call**, because AGE's session-level
catalog registration was empirically found not to survive pooled-connection reuse.

### 3.4 Two answering engines

| | Legacy orchestrator | Agent harness |
|---|---|---|
| Module | [src/pipeline/orchestrator.py](../src/pipeline/orchestrator.py) (~2 850 lines) | [src/pipeline/harness/](../src/pipeline/harness/) |
| Shape | One `process_query()` async generator branching on route | Supervisor → 1 sub-agent → tools |
| Selection | Default for every route | Only when the classified route is in `config.HARNESS_CUTOVER_ROUTES` |
| Default state | **Active** | **Inactive** (`HARNESS_CUTOVER_ROUTES` defaults to empty) |

The harness has 11 registered sub-agents (`Semantic Search`, `Large-Scale
Aggregate`, `Case Summarization`, `Timeline Building`, `Cross-Case Linkage`,
`Investigative Analysis`, `Report Drafting`, `Data-Quality`, `Local Search`,
`Global Search`, `Meta-Analysis`) and 9 tool wrappers (`rag`, `graph`,
`local_search`, `global_search`, `sql`, `web`, `xagg`, `xgraph`, `xnetwork`).
Details in [AI_RAG_ARCHITECTURE.md](AI_RAG_ARCHITECTURE.md).

---

## 4. Request lifecycle

### 4.1 A plain authenticated REST request

```
1. CORS preflight (if cross-origin)
2. slowapi rate-limit check (if the route is decorated)
3. Router-level dependency: cross_case_rls_dependency / case_rls_dependency
      -> sets ContextVars app.rls_active / app.case_id / app.cross_case
4. get_current_user  ->  CSRF (mutating only) -> JWT decode -> DB user lookup
5. require_role(...) / require_case_access(...) where declared
6. Handler body -> gateway call
      -> get_session() opens an AsyncSession and emits
         SET LOCAL app.rls_active / set_config('app.case_id', ...) / app.cross_case
      -> query executes under the RLS policies
      -> commit on success, rollback on exception, always close
7. Optional gateway.log_audit_event(...)
8. Response serialised by the declared response_model (if any)
```

### 4.2 `POST /api/chat` (the SSE path)

```
1. rate limit 60/min
2. get_current_user (CSRF + JWT)
3. asyncio.gather(get_user_context_profile, get_session)      # concurrent reads
4. resolve project_id / case_id  (request body wins over the session row)
5. if case_id: check_case_access(...)      -> 403 "Not assigned to this case"
6. set_case_scope(case_id)                 # arm RLS ONCE, at this chokepoint
7. if the session row is absent: create_session(id, owner, provisional title, ...)
8. if HARNESS_CUTOVER_ROUTES is non-empty:
       route_query(message) once
       cutover only if route in the set AND output_format == "chat"
9. return StreamingResponse(event_generator(), media_type="text/event-stream")
10. inside event_generator():
       stream = run_cutover_query(...)  or  process_query(...)
       for each event: yield f"data: {json.dumps(event)}\n\n"
       an event carrying delegate_to_legacy switches to process_query() mid-stream
       any exception -> yield {"step":"system","status":"error","detail": str(e)}
```

**Arming RLS is the caller's job, once.** `process_query()`'s docstring is
explicit that it no longer self-arms, and any new caller must call
`set_case_scope()` first. Compliance test
`test_enforcement_2_rls_arming.py` enforces at CI time that no harness tool
wrapper arms RLS itself.

### 4.3 Legacy pipeline stages (`process_query`)

Emitted step names, in typical order:
`query_rewriter → router → attachments → retrieval → reranker → cross_reranker →
evaluator → (web_search) → citation_validator → (cross_case_finding) → response →
(file_generation) → (title_generation) → memory`.

Route branching in `process_query`:

| Route | Behaviour |
|---|---|
| `DIRECT` / `NONE` | Answer from general knowledge; no retrieval, no verifier |
| `RAG` | Hybrid retrieval + RRF + cross-encoder + evaluator + retry loop |
| `SQL` | `extract_sql_params()` → direct SQL over `police_reference_data` |
| `WEB` | Guarded web search (or an air-gap notice when `AIR_GAP_MODE`) |
| `GRAPH` | Case-scoped graph traversal |
| `GRAPH_HYBRID` | Graph traversal blended with vector retrieval |
| `XGRAPH` | Cross-case graph traversal (role-gated) |
| `XAGG` | Cross-case aggregate/count (role-gated) |
| `XNETWORK` | Cross-case community-report synthesis (role-gated) |

`MAX_RETRIES` (default **1**) bounds the evaluator retry loop. An exhausted RAG
retry **abstains** (`_SAFE_RESPONSE`) — it explicitly does *not* silently fall
back to a web search the user did not request.

---

## 5. Services (internal modules)

| Service | Module | Responsibility |
|---|---|---|
| Query rewriter | `pipeline/query_rewriter.py` | Rewrites the message using conversation history; separate retry-rewrite path |
| Router | `pipeline/router.py` | Classifies into 9 routes + `case_scope`, `target_entity`, `output_format`, `target_year`, `confidence`, `station`, `district`, `secondary_methods`. Deterministic regex overrides run **before** the LLM for XAGG/XGRAPH/XNETWORK |
| Evaluator | `pipeline/evaluator.py` | "Are these chunks relevant?" → drives the retry loop |
| Verifier | `pipeline/verifier.py` | Hard grounding gate: 3 deterministic pre-checks (temporal validity, cross-case leakage, confidence hedging) + an LLM claim-by-claim judge. **Fails closed** |
| Validation | `pipeline/validation.py` | Second-opinion entailment check that runs *after* the verifier passes. **Caveat-only, never blocking; fails open** |
| Citation consistency | `pipeline/citation_consistency.py` | Deterministic `[Document N]` index arithmetic; Report Drafting only |
| Memory | `memory/conversation.py` + `pipeline/memory_updater.py` | Loads/saves history within `MAX_HISTORY_TOKENS` (default 2000), dropping oldest first; updates project memory |
| Title generator | `pipeline/title_generator.py` | Replaces the provisional session title |
| File structurer + builders | `pipeline/file_structurer.py`, `generation/*` | Converts an answer into PDF/XLSX/DOCX under `data/generated/` |
| Entity resolution | `graph/entity_resolution.py` | Four tiers: `cnic_auto` (exact identifier, auto-merge), `flagged_unverified`, `human_review`, `new` |
| Versioning | `graph/versioning.py` | The only graph writer. Append-only: `write_edge()` sets `superseded_by` on the prior edge, never mutates in place |
| Case scope | `graph/case_scope.py` | `scoped_cypher()` refuses any within-case template that does not literally reference `$case_id` |
| Analytics | `observability/analytics.py` | Time-series/rollups over `pipeline_runs` / `pipeline_steps` / `error_logs` |
| Error capture | `observability/errors.py` | Bounded in-memory queue drained by one background task into `error_logs`; never raises, never recurses, drops on overflow |

---

## 6. External integrations

Summarised here; full failure/timeout detail in [INTEGRATIONS.md](INTEGRATIONS.md).

| Integration | Direction | Gate |
|---|---|---|
| Local model server (LLM reasoning + generation, embeddings, reranker) | Outbound HTTP | `LOCAL_LLM_URL`, `LOCAL_GEN_LLM_URL`, `EMBEDDINGS_URL`, `RERANKER_URL` |
| Groq / Gemini / OpenAI / Anthropic | Outbound HTTPS | API keys; blocked entirely by `AIR_GAP_MODE` |
| Tavily web search | Outbound HTTPS | `TAVILY_API_KEY`, restricted to `WEB_ALLOWED_DOMAINS`, disabled by `AIR_GAP_MODE` |
| Muhafiz Data REST API | Outbound HTTPS | `MUHAFIZ_API_BASE_URL` + `MUHAFIZ_API_KEY` (both or neither) |
| MCP Postgres server | Local subprocess (`npx`) | `MCP_DATABASE_URL`; **no superuser fallback** |
| ChromaDB | In-process, local disk | `CHROMA_PERSIST_DIR` |
| PostgreSQL + Apache AGE | Network (Docker in dev) | `DATABASE_URL` |

---

## 7. Background work, queues and workers

**There is no Celery, RQ, Arq, cron, or scheduler anywhere in this repository.**
Three mechanisms cover all asynchronous work:

### 7.1 FastAPI `BackgroundTasks`
Used by `POST /api/admin/kb/upload` → `_ingest_uploaded_file(path, job_id)`.
Runs after the response is sent, in the same process. Outcome (`success`/`failed`,
`chunks_added`, `duration_ms`, `error_message`) is written to the
`ingestion_jobs` row — the caller never learns of a background failure from the
HTTP response.

### 7.2 Fire-and-forget `asyncio.create_task`
Scheduled by [src/ingestion/service.py](../src/ingestion/service.py) after each ingest:

| Task | Module | Trigger condition |
|---|---|---|
| Conflict detection | `ingestion/conflict_bg.py` | Every ingest; on success stamps `cases.conflicts_checked_at` |
| Candidate reprioritization | `ingestion/reprioritization_bg.py` | Every ingest (incremental re-score of the pending queue) |
| Community refresh | `ingestion/community_refresh_bg.py` | Every ingest, but only recomputes if the ~10 % drift staleness heuristic fires |
| Entity-resolution consistency sampling | `ingestion/entity_resolution_sampling_bg.py` | Probabilistic — gated by `SAMPLE_TRIGGER_PROBABILITY` |
| Entity embedding refresh | `ingestion/entity_embedding_refresh_bg.py` | Every ingest; plain incremental diff, no staleness gate |

All five are **best-effort**: each catches its own exceptions so a failure never
fails the ingestion job it rides alongside. Because they are event-loop tasks,
a process restart loses any in-flight work with no retry.

`community_refresh_bg.refresh_if_stale()` exists as an awaitable core precisely
because a one-shot CLI script must `await` it — a `create_task` there would race
the process's own connection-pool teardown and likely never finish.

### 7.3 Manual admin/CLI triggers (the substitute for a scheduler)
Because there is no cron, several sweeps are exposed as explicit actions:

- `POST /api/admin/community/refresh` — full detect + summarize sweep.
- `POST /api/admin/graph-review/queue/reprioritize` — full queue re-score.
- `scripts/sync_muhafiz_data.py --full` — external-data sync + projection.
- `scripts/snapshot_same_as_queue.py` — writes the `same_as_queue_snapshot`
  rows that `GET /queue/history` reads. **No automatic writer exists**, so an
  empty history most likely means the script has never been run.
- `scripts/eval_entity_resolution.py` — writes
  `data/eval/resolution_metrics.json`, served verbatim by
  `GET /api/admin/eval/entity-resolution`. The script wipes the evidence graph,
  so it is run offline only.

### 7.4 Error-capture queue
`observability/errors.py` maintains an in-memory `asyncio.Queue` drained by one
background task. On overflow, records are **dropped** (and the drop is reported)
rather than applying backpressure to a user request. Groq/Gemini rate-limit
exhaustion is downgraded to `warning` severity with a stable `error_type` so it
does not bury genuine bugs.

---

## 8. Data flow

### 8.1 Ingestion (knowledge base)

```
upload / CLI
  -> loader_router.route_and_load()
        -> validate_file()          magic bytes, size, zip-bomb guard
        -> per-format loader        pdf / docx / excel / html / image / text
  -> text_normalizer (whitespace, Urdu) + script_detector (Roman-Urdu)
  -> chunker.split_text_into_chunks()   CHUNK_SIZE 512 / CHUNK_OVERLAP 64,
                                        snapped to sentence boundaries
  -> embedder.embed_texts(task_type="RETRIEVAL_DOCUMENT")   bounded concurrency
  -> vector_store.upsert_documents()  -> ChromaDB
        (also writes chunk_fulltext rows for BM25)
  -> documents row in Postgres
  -> graph extraction (doc_classifier -> ner -> domain_entities ->
        relationship_extraction) -> entity_resolution -> versioning.write_*()
  -> 5 fire-and-forget background tasks (§7.2)
```

### 8.2 Retrieval (RAG route)

```
query
  -> embed_text(task_type="RETRIEVAL_QUERY")
  -> ChromaDB vector search   (top-k; widened by CROSS_CASE_RETRIEVAL_MULTIPLIER
                               and capped per case by CROSS_CASE_PER_CASE_CAP
                               when the query is NOT case-scoped)
  -> BM25 over the chunk_fulltext candidate pool
  -> RRF fusion (k = 60) + a semantic confidence floor of 0.85
  -> cross-encoder rerank (RERANKER_URL) -> TOP_K_RERANK
  -> evaluator: relevant?  no -> rewrite_for_retry -> loop (bounded by MAX_RETRIES)
  -> generation (local generation slot first, cloud fallback)
  -> verifier.verify_grounding()  -> fail: regenerate or abstain
  -> validation (caveat-only)
  -> SSE stream to the client
  -> persist: messages, pipeline_runs, pipeline_steps, project memory
```

### 8.3 Structured / graph flow

```
Muhafiz Data API  ->  muhafiz_records.py / muhafiz_cases.py
                  ->  structured_projection.py  (FIR fields -> nodes/edges)
                  ->  cross_silo_projection.py  (CMS/PKM/criminal records, CITES)
                  ->  entity_resolution.py      (cnic_auto | flagged | review | new)
                  ->  versioning.write_node/write_edge   (append-only, MERGE nodes)
                  ->  pending_candidate_priority (Postgres side table)
                  ->  /api/admin/graph-review    (human confirm/reject)
```

### 8.4 Answer persistence

Each chat turn writes: a `sessions` row (first turn), `messages` rows (user +
assistant, the assistant row carrying `citation_validated`,
`unverified_citations`, `confidence_score`, `degradation_trace`), a
`pipeline_runs` row (`routed_to`, `retry_count`, `final_outcome`,
`total_duration_ms`, `verifier_passed`, `verifier_regenerated`), N
`pipeline_steps` rows, optional `generated_files` and `mcp_tool_calls` rows, and
`audit_logs` entries for privileged actions.
