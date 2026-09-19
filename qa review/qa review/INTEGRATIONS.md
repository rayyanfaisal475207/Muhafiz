# INTEGRATIONS.md

> Sources: [src/config.py](../src/config.py), [src/llm/client.py](../src/llm/client.py),
> [src/retrieval/embedder.py](../src/retrieval/embedder.py),
> [src/retrieval/cross_reranker.py](../src/retrieval/cross_reranker.py),
> [src/retrieval/web_search.py](../src/retrieval/web_search.py),
> [src/data_gateway/muhafiz_api/client.py](../src/data_gateway/muhafiz_api/client.py),
> [src/mcp/client.py](../src/mcp/client.py).

---

## 1. Local model server (LLM + embeddings + reranker)

**Purpose:** primary, first-choice provider for every LLM call and for
embeddings/reranking, served over a local/self-hosted OpenAI-compatible HTTP
endpoint (an ngrok tunnel to a free-tier server in the documented dev setup).

| Concern | Reasoning slot | Generation slot | Embeddings | Reranker |
|---|---|---|---|---|
| Env var | `LOCAL_LLM_URL` | `LOCAL_GEN_LLM_URL` | `EMBEDDINGS_URL` | `RERANKER_URL` |
| Model | `LOCAL_LLM_MODEL` (default `qwen3.5-2b`) | `LOCAL_GEN_LLM_MODEL` | multilingual-e5-large-instruct | bge-reranker-v2-m3 |
| Timeout | `LOCAL_LLM_TIMEOUT` (default 20 s) | `LOCAL_GEN_LLM_TIMEOUT` (default 20 s) | not independently configured (httpx default) | not independently configured |
| Failure behaviour | Falls back to the configured cloud provider (`_call_groq`/`_call_gemini`/etc.), unless `force_cloud`/`AIR_GAP_MODE` change that | Same | **No documented fallback** in `embedder.py` beyond raising | **No documented fallback** — a reranker failure propagates |
| Retries | None visible at the `httpx.AsyncClient` level — a single attempt per role, then the local→cloud fallback in `call_llm`/`stream_llm` | | | |

`MODEL_SERVER_BASE_URL` is the shared base behind all four endpoints (falls back
to `LOCAL_LLM_URL` if unset). Ingestion-time extraction modules
(`doc_classifier`, `ner`, `domain_entities`, entity-resolution's adjudicator)
call `{MODEL_SERVER_BASE_URL}/health` **before** a batch run, so a dead tunnel
fails one health check instead of hundreds of individual calls
([src/extraction/llm_health.py](../src/extraction/llm_health.py)).

**Air-gap interaction:** `AIR_GAP_MODE=true` with no `LOCAL_LLM_URL` configured
is flagged as a **critical config error** by `validate_config()` — every LLM
call would refuse the cloud fallback and simply fail.

---

## 2. Cloud LLM providers (Groq / Gemini / OpenAI / Anthropic)

**Purpose:** fallback for local-model failure, and the sole path when
`force_cloud=True` (e.g. a JSON-classification call where a "successful" local
reply isn't structurally usable).

| Provider | Env var(s) | Model default |
|---|---|---|
| Groq | `GROQ_API_KEY` (+ `GROQ_API_KEY_1..4` for rotation) | `openai/gpt-oss-120b` |
| Gemini | `GEMINI_API_KEY` (+ `_1..4`) | `gemini-2.5-flash` |
| OpenAI | `OPENAI_API_KEY` | `gpt-4o` |
| Anthropic | `ANTHROPIC_API_KEY` | `claude-3-5-sonnet-20241022` |

**Failure behaviour:**
- `_is_rate_limit(exc)` matches `"429"`, `"RESOURCE_EXHAUSTED"`, or
  `"rate limit"` in the exception string. `key_manager` rotates to the next
  configured key on this condition.
- `observability/errors.py` deliberately downgrades a rate-limit exhaustion to
  `warning` severity with a stable `error_type`, so the admin dashboard's error
  view can filter it separately from genuine bugs — described as an *expected
  operational condition under load*, not an application defect.
- **Timeouts/retries:** **UNKNOWN at the HTTP-client level** — no explicit
  per-call timeout or retry/backoff policy was found for the Groq/Gemini SDK
  calls in `client.py`; failures surface as whatever exception the underlying
  SDK raises, caught by the broader local→cloud fallback logic in `call_llm`.
- `AIR_GAP_MODE=true` **disables cloud LLM calls entirely** — the single
  documented data-sovereignty boundary a caller cannot override.

---

## 3. Tavily web search

**Purpose:** the WEB route's live web search, and the guarded fallback source
for general-knowledge questions outside the police corpus.

- **Endpoint:** `POST https://api.tavily.com/search`
  ([src/retrieval/web_search.py](../src/retrieval/web_search.py)).
- **Auth:** `TAVILY_API_KEY` (read via `os.getenv`, not through `config.py`).
- **Domain restriction:** `include_domains: config.WEB_ALLOWED_DOMAINS`
  (default: `gov.pk, islamabadpolice.gov.pk, nadra.gov.pk, pakistantoday.com.pk,
  dawn.com, tribune.com.pk, thenews.com.pk, geo.tv, app.com.pk`) — framed in the
  code as a relevance/reliability control, not only a safety one.
- **Failure behaviour:**
  - `AIR_GAP_MODE=true` → the HTTP call is **never issued**; returns `[]` immediately.
  - Missing `TAVILY_API_KEY` → returns a single mock result explaining the key
    is missing, rather than an empty list or an exception — a dev-mode fallback.
  - Non-200 response → logs the error text and returns `[]`.
  - Any exception → caught, logged, returns `[]`.
- **Timeouts/retries:** no explicit timeout is set on the `aiohttp.ClientSession`
  call — **UNKNOWN**/unbounded at the code level; no retry logic present.
- Requested with `search_depth: "basic"`, `time_range: "year"`,
  `include_answer: false`, `max_results` (caller-supplied, default 5).

---

## 4. Muhafiz Data API (external evidence source)

**Purpose:** a REST API fronting real-schema PSRMS/CMS/PKM/criminal-record data
(documented in [API_CONSUMER_GUIDE.md](../API_CONSUMER_GUIDE.md)), the source of
structured evidence records for graph projection. Explicitly confirmed in
[docs/decisions/0001-muhafiz-api-migration.md](../docs/decisions/0001-muhafiz-api-migration.md)
as a **same-schema stand-in**, not the real police system.

- **Client:** [src/data_gateway/muhafiz_api/client.py](../src/data_gateway/muhafiz_api/client.py)
  (`httpx.AsyncClient`).
- **Gating:** entirely opt-in via `MUHAFIZ_API_BASE_URL` — empty disables the
  source. `validate_config()` flags a half-configured pair (base URL set, key
  not, or vice versa) as an error, since one without the other is always a
  mistake.
- **Timeout:** `MUHAFIZ_API_TIMEOUT` (default **60 s**) — generous, because the
  live API (hosted on Render's free tier) is occasionally slow on cold start.
- **Retries:** `tenacity` — `stop_after_attempt(4)`,
  `wait_exponential(multiplier=2, min=2, max=30)`, retried **only** on
  `httpx.TimeoutException` / `httpx.TransportError` (`reraise=True` on final failure).
- **Pagination:** `page_size` capped at `MUHAFIZ_API_PAGE_SIZE` (default 100,
  the documented API maximum). `iter_all()` pages until at least
  `meta.total` records have been seen.
- **Typed errors** ([src/data_gateway/muhafiz_api/errors.py](../src/data_gateway/muhafiz_api/errors.py)):
  `MuhafizApiAuthError` (401), `MuhafizApiNotFoundError` (404),
  `MuhafizApiValidationError` (422), `MuhafizApiUnavailableError` (5xx after
  retries, connection/timeout failure, or a non-{data,meta}-shaped response).
- **No air-gap awareness by design** — a plain internet REST call, gated
  entirely by whether `MUHAFIZ_API_BASE_URL` is set.
- **Consumption:** `src/ingestion/muhafiz_records.py` (M3),
  `src/ingestion/muhafiz_cases.py` (M4), driven manually via
  `scripts/sync_muhafiz_data.py` — **not** part of the live request path.

---

## 5. MCP Postgres server

**Purpose:** demonstrates/serves the SQL retrieval route through the Model
Context Protocol, as an alternative to the direct-SQL fast path the ordinary
chat pipeline uses.

- **Mechanism:** [src/mcp/client.py](../src/mcp/client.py) spawns
  `npx -y @modelcontextprotocol/server-postgres <connection-string>` as a **local
  subprocess** and communicates over stdio (`mcp.client.stdio.stdio_client`).
- **Connection:** `MCP_DATABASE_URL`, expected to point at the least-privilege
  `muhafiz_mcp_readonly` role (`SELECT` on `police_reference_data` only —
  migration 009). **No superuser fallback** — removed once the readonly role
  was verified end-to-end.
- **Failure behaviour:**
  - `MCP_DATABASE_URL` unset → `RuntimeError` raised immediately on first use
    (deliberate fail-closed; a startup warning from `validate_config()` gives
    advance notice).
  - A network block on outbound TCP 5432 (documented in code comments —
    `WinError 64`/`ECONNRESET`) causes the spawned `npx` process to
    crash/fail to connect.
  - `result.isError` from the MCP tool call → the error text is logged and
    re-raised as a generic `Exception`.
  - A non-JSON tool response → raised as an `Exception` with the raw text.
  - `POST /api/admin/mcp-demo` catches any failure and returns **502**
    `"MCP query failed: <exc>"`, always logging an `mcp_tool_calls` row
    (status `success`/`failed`) in a `finally` block regardless of outcome.
- **Timeouts/retries:** **UNKNOWN** — no explicit timeout wraps the stdio
  session lifecycle in this client; the subprocess spawn itself has no retry.
- **Requires Node.js and network access to `npx`'s package resolution** at
  runtime — an offline/air-gapped host without a pre-cached package would fail here.

---

## 6. ChromaDB (embedded, not a network integration)

Not a remote service — an in-process, local-disk persistent client. Included
here only to record its persistence-layer nature: `CHROMA_PERSIST_DIR` (default
`data/chroma_db`), no network calls, no timeout/retry concerns of its own.
Failure modes are disk-I/O and dimension-mismatch errors (see
[DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) §9).

---

## 7. PostgreSQL + Apache AGE

Not a third-party API, but the platform's primary datastore *and* graph store
in one Docker Compose service (`apache/age:release_PG16_1.5.0`).

- **Connection pools:** SQLAlchemy async engine (`pool_size=15,
  max_overflow=20, pool_recycle=1800`) and a separate asyncpg pool for AGE
  (`min_size=1, max_size=10, statement_cache_size=0`).
- **Failure behaviour at startup:** a failed `init_postgres()` does **not**
  crash the process — it is caught, logged as a WARNING, and the server starts
  anyway so `/health` can report the outage rather than crash-loop. Every
  DB-backed route then fails per-request until Postgres is reachable.
- **Health check:** `GET /health` probes Postgres with `SELECT 1` under a hard
  **3-second** `asyncio.wait_for` timeout, specifically so a stalled/unreachable
  host cannot block the liveness endpoint for the OS TCP timeout.

---

## 8. Vite dev proxies (frontend → backend)

Not an external service, but the mechanism that stitches the two SPAs to the
API in local development. Both `frontend/vite.config.ts` and
`admin-frontend/vite.config.ts` proxy `/api/*` to `http://127.0.0.1:8001`,
**changeOrigin: true**, no path rewrite. `allowedHosts` additionally permits
`.ngrok-free.app` / `.ngrok-free.dev` (Vite 5+ rejects unrecognized `Host`
headers by default, which would otherwise block ngrok-tunneled access).

> **QA flag:** the proxy target (`8001`) does not match the backend's own
> default port (`config.PORT = 8000`, and `RUN.md`'s documented
> `uvicorn ... --port 8000`). The admin app, which routes every request through
> this proxy, will fail against a backend started on the documented default
> port. The chat app is unaffected because it calls an absolute `VITE_API_URL`.
