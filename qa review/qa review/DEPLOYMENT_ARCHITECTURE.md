# DEPLOYMENT_ARCHITECTURE.md

> Sources: [docker-compose.yml](../docker-compose.yml), [RUN.md](../RUN.md), [.env.example](../.env.example),
> [.github/workflows/ci.yml](../.github/workflows/ci.yml), `frontend/vite.config.ts`,
> `admin-frontend/vite.config.ts`, [requirements.txt](../requirements.txt).
>
> **No production Dockerfile, Kubernetes manifest, or hosted-deployment
> configuration exists in this repository.** Everything documented here is the
> local/self-hosted development topology the codebase actually supports; where
> a production topology is not established by the code, it is marked **UNKNOWN**.

---

## 1. Runtime services (as the code actually runs)

| Service | How it runs | Port | Managed by |
|---|---|---|---|
| FastAPI backend | `uvicorn src.main:app` | `config.PORT` (default **8000**) | Manual (`uvicorn ... --reload`) or `python src/main.py` |
| Chat SPA (dev) | Vite dev server | 5173 | `npm run dev` in `frontend/` |
| Admin SPA (dev) | Vite dev server | 5174 | `npm run dev` in `admin-frontend/` |
| PostgreSQL + Apache AGE | Docker container `muhafiz-postgres` | 5432 | `docker compose up postgres -d` |
| Local model server (LLM/embeddings/reranker) | External — an ngrok tunnel to a self-hosted server in the documented dev setup | N/A | **Not managed by this repository** |
| vLLM (example service) | Docker container `muhafiz-vllm` | 8000 (**collides with the backend's own default port**) | `docker-compose.yml`, explicitly **not** started by the documented `docker compose up postgres -d` flow |

**There is no separate worker, queue, or scheduler process.** [RUN.md](../RUN.md) §5
states this directly: *"There is no separate worker or queue — background work
... runs on the backend's own event loop."* See
[ARCHITECTURE.md](ARCHITECTURE.md) §7 for the three in-process mechanisms
(`BackgroundTasks`, fire-and-forget `asyncio.create_task`, manual admin/CLI triggers).

---

## 2. Docker

[docker-compose.yml](../docker-compose.yml) defines two services. **Only `postgres` is
meant to be started routinely** — the file's own header and [RUN.md](../RUN.md) both
warn against `docker compose up -d` (the whole file) by accident.

### 2.1 `postgres`
- Image: `apache/age:release_PG16_1.5.0` — Apache AGE ships as a Postgres
  extension inside the same instance, not a separate graph database.
- Container name: `muhafiz-postgres`, `restart: unless-stopped`.
- Port: `5432:5432`.
- Env: `POSTGRES_DB` (default `muhafiz`), `POSTGRES_USER` (default `postgres`),
  `POSTGRES_PASSWORD` (default `dev` — **a placeholder, not production-safe**).
- Volume: named volume `pgdata`, explicitly **pinned** to `rag-chatbot_pgdata`
  (not the Compose-auto-derived name) so it attaches to a pre-existing volume
  from the project's earlier directory name rather than silently creating a
  second, empty one — a documented gotcha for anyone renaming the project folder.
- TLS: certs mounted read-only from `./certs`, copied to `/tmp/certs` and
  `chmod 600`'d at container start (a workaround because Windows volume mounts
  cannot themselves carry the `0600` permission Postgres requires on the key file).
- Healthcheck: `pg_isready -U postgres -d muhafiz`, `interval: 5s`,
  `timeout: 3s`, `retries: 5`.

### 2.2 `vllm` (example configuration, not part of the routine flow)
- Image: `vllm/vllm-openai:latest`.
- Serves `Qwen/Qwen2.5-14B-Instruct` with TLS via `--ssl-keyfile`/`--ssl-certfile`.
- Requires `HUGGING_FACE_HUB_TOKEN`, a GPU with real VRAM headroom, and
  downloads a multi-GB model checkpoint. `RUN.md` frames this as *"an example
  configuration ... not something you want starting by accident."*
- **Port collision:** exposes `8000:8000`, the same host port the FastAPI
  backend defaults to.

### 2.3 What Docker does *not* cover
No Dockerfile builds the FastAPI backend or either frontend into a container
image. There is no `docker-compose` service for the backend, the frontends, the
local model server, or a reverse proxy. Containerization of the application
tier itself is **UNKNOWN / not present in this repository**.

---

## 3. Databases

| Store | Technology | Where it lives | Config |
|---|---|---|---|
| Relational + graph | PostgreSQL 16 + Apache AGE | Docker container (dev) | `DATABASE_URL` |
| Vector store | ChromaDB | Local filesystem, in-process | `CHROMA_PERSIST_DIR` (default `./data/chroma_db`) |
| Legacy side-log | SQLite | Local filesystem | `DB_PATH` (default `./data/pipeline_logs.db`) |

Two Apache AGE graphs share the one Postgres instance: `evidence_graph`
(production) and `evidence_graph_eval` (an isolated graph for the
entity-resolution eval harness, so eval fixtures never touch real case data).

**Migration application is manual, not automated at deploy time.** `RUN.md` §4
lists ~28 plain-SQL files applied one-by-one via
`python scripts/apply_migration.py migrations/<file>.sql`, plus the Alembic
baseline chain. The document explicitly warns that a partially-applied
migration set produces confusing downstream failures rather than a clear
startup error, and provides verification queries to run before trusting the
schema state.

**Least-privilege database roles require manual follow-up** (see
[AUTH_FLOW.md](AUTH_FLOW.md) §8.3 / [DATABASE_SCHEMA.md](DATABASE_SCHEMA.md) §7): migrations
009 and 015 create `muhafiz_mcp_readonly` and `muhafiz_app` with `LOGIN` and no
password. Setting a password and repointing `DATABASE_URL`/`MCP_DATABASE_URL`
is an operator step the migrations cannot perform themselves. Until
`DATABASE_URL` is repointed off the `postgres` superuser, **every RLS policy is
silently inert**.

---

## 4. Caches

**No dedicated cache service (Redis, Memcached, etc.) exists in this
repository.** The closest analogues:

- `augraphy_cache/` — a local disk cache directory for the Augraphy document
  degradation/OCR-simulation library used in dataset generation, not an
  application-request cache.
- ChromaDB itself acts as the persistent "cache" of embeddings — there is no
  separate embedding-result cache in front of `EMBEDDINGS_URL`.
- No HTTP response caching layer, no query-result cache, no session-store cache
  (sessions are JWTs verified per-request against Postgres, not a cache-backed
  session store).

---

## 5. Workers

Covered fully in [ARCHITECTURE.md](ARCHITECTURE.md) §7. Summary: **there is no
distinct worker process or process pool.** All asynchronous work is either (a)
FastAPI `BackgroundTasks` inside the same uvicorn process, (b)
`asyncio.create_task` fire-and-forget coroutines on the same event loop, or (c)
manually triggered scripts/admin endpoints. A process restart loses any
in-flight background work (no persistence/retry queue for it).

---

## 6. Cloud infrastructure

**No cloud provider (AWS/GCP/Azure) configuration, Terraform, or IaC is present
in this repository.** No hosted database connection string, managed-Postgres
setup, or CDN configuration was found. Deployment target is
**UNKNOWN / not established by the code** beyond the local Docker Compose +
manually-run process topology documented in `RUN.md`.

The one externally-hosted dependency confirmed by code comments is the Muhafiz
Data API itself, hosted on **Render's free tier** (`MUHAFIZ_API_TIMEOUT`'s
comment references "Render free-tier" cold starts) — this is a data-source
integration, not a deployment target for this application.

---

## 7. Production topology (as documented, not as automated)

`RUN.md` §5 gives the only ordering guidance in the repository, framed for
local development:

```
Docker Desktop  →  Postgres container  →  backend  →  frontends
```

with an explicit warning that skipping ahead is *"the single most common
failure mode"* — starting `docker compose up -d` before Docker Desktop itself
finished starting produces a `failed to connect to the docker API` error, not
a helpful "still starting" message.

### 7.1 Environment-driven safety gates at startup

`src/main.py`'s lifespan handler enforces two things that any production
topology must satisfy or the process refuses to serve traffic correctly:

1. **`REQUIRE_POSTGRES=true` (default).** If `DATABASE_URL` is unset, the
   process **raises `RuntimeError` and does not start** — refusing to silently
   fall back to a legacy SQLite schema with no case/user/RBAC tables.
2. **`ENVIRONMENT=production` + a critical config error → refuses to start.**
   Outside of `production`, the same critical errors (default JWT secret,
   unrecognized `ENVIRONMENT` value) are logged as CRITICAL but the server
   starts anyway — meaning a misconfigured **staging** environment will run
   with an insecure default JWT secret unless an operator reads the logs.

### 7.2 CORS

`CORS_ORIGINS` (comma-separated, default
`http://localhost:5173,http://localhost:3000,http://localhost:5174`) must be
set to the real frontend origin(s) in any non-local deployment —
`allow_credentials=True` is paired with `allow_origins`, not a wildcard, since
cookies are the auth transport.

### 7.3 Reverse proxy / TLS

**Not configured in this repository.** The backend's own `certs/` directory and
`scripts/generate_certs.py` produce certificates for the local Postgres/vLLM
TLS setup described above (§2.1), not for terminating TLS on the public-facing
API. No nginx/Caddy/Traefik configuration exists. A production deployment
would need to add its own TLS termination and reverse proxy — **not present
here**.

### 7.4 Frontend build/serve

Both SPAs build via `tsc -b && vite build` into `dist/` (present in the repo as
`frontend/dist/` and `admin-frontend/dist/` build artifacts). **No static-file
server or CDN configuration for serving these build outputs is present** — the
dev-time Vite proxy (`/api` → `127.0.0.1:8001`) is a development-only mechanism
and does not extend to a production build, which would need the frontend to
call the API via an absolute URL (as `frontend/`'s `VITE_API_URL` already
supports) or its own reverse-proxy configuration.

### 7.5 Stopping the stack

`RUN.md` §8: Ctrl-C in each terminal for a clean shutdown (the backend's
lifespan shutdown handler runs); OS-specific commands are documented for
killing a stuck port; `docker compose down` (keep data) vs.
`docker compose down -v` (delete the data volume — the document explicitly
flags this as irreversible against the synthetic dataset's live DB).

---

## 8. Known operational failure modes (documented in `RUN.md` §9, encountered live)

These are recorded here because they bear directly on deployment/runtime
reliability, not because they are hypothetical:

- **Docker daemon not yet ready** produces a raw `npipe` connection error
  rather than a "please wait" message.
- **Partially-applied migrations** (e.g. 1–5 present, 6–8 missing) let the
  backend start and `/health` return 200 while every `users.role`/RLS-dependent
  route fails — not caught by the health check.
- **PDF ingestion memory exhaustion** (`std::bad_alloc`,
  `DefaultCPUAllocator: not enough memory`) during Docling's OCR/layout model
  loading — a genuine system-resource issue, not an ingestion-code bug.
  `scripts/reingest_kb_resume.py` isolates each file in its own subprocess as a
  mitigation for *cumulative* memory pressure across a bulk run, but does not
  help a single file failing on first attempt under overall system pressure.
- **Both Vite dev servers started simultaneously can OOM-crash** on a
  memory-constrained machine (a real out-of-memory crash was hit in the
  documented session); starting them a few seconds apart avoids the
  dependency-pre-bundling spike.

---

## 9. Dependency pinning

- `requirements.txt` — floor-pinned intent file (`>=` bounds), for
  local-dev readability.
- `requirements.lock.txt` — exact pins for every direct + transitive
  dependency, used by CI and (per its header comment) deployments, for
  reproducible builds.
- `package-lock.json` in each frontend app pins Node dependencies;
  `npm ci` (not `npm install`) is used in CI for reproducible installs.
