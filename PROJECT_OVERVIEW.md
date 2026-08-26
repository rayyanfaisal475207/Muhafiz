# Project Overview

Orientation for a new contributor: what this is, how it's put together, and where to
look next. For "how do I get it running," see [`README.md`](README.md) and
[`RUN.md`](RUN.md). For the full architectural rationale behind design decisions
summarized here, see the deeper docs linked throughout — this file exists so you don't
have to read all of them before your first commit.

---

## 1. What this is

**Muhafiz** ("guardian," Urdu) is a case-centric evidence-intelligence platform for
Islamabad Police: a bilingual (Urdu/English) investigative assistant where a **Case** —
not a document, not a chat thread — is the entity every piece of evidence, every graph
node, and every access-control decision attaches to.

An investigator asks a question, in Urdu or English, about a specific case, the law, a
procedure, or how entities connect across cases. The system routes the query to the
right evidence source (case-scoped document retrieval, a knowledge graph built from that
case's own evidence, structured reference data, or — for supervisors and above — a
cross-case query), generates an answer, and runs that answer through a hard grounding
gate before it ever reaches the user. If the evidence doesn't support an answer, the
system says so rather than fabricating a citation.

It began as a simple police-reference RAG chatbot and was rebuilt around the case-centric
model described above; a lot of the codebase's structure (why retrieval is filtered by
`case_id`, why access control has five independent checkpoints instead of one) makes
sense once you know that history.

---

## 2. Tech stack

| Layer | Technology |
|---|---|
| Backend framework | FastAPI (async) + Uvicorn |
| Relational DB | PostgreSQL, local/self-hosted, reached via direct SQL (SQLAlchemy async + asyncpg) |
| Graph DB | Apache AGE — a graph extension **inside the same Postgres instance**, not a separate service |
| Vector store | ChromaDB (1024-dim embeddings) |
| Keyword search | Postgres `tsvector`/GIN (persistent, incrementally maintained) |
| Auth | JWT in an HttpOnly cookie + double-submit CSRF, bcrypt, slowapi rate limiting, RBAC + Postgres Row-Level Security |
| Migrations | Alembic (baseline chain) **+** plain SQL files in `migrations/` (everything since — the two chains are deliberately separate, see §7) |
| Frontend (chat) | React 19 + TypeScript + Vite 8, Tailwind CSS, Zustand, Server-Sent Events, `react-markdown`/`remark-gfm` |
| Frontend (admin) | React 19 + TypeScript + Vite 8, plain CSS (no Tailwind) with its own design-token file, Recharts |
| AI — reasoning | **Qwen3-14B**, one local instance serving nine prompted roles (router, rewriter, evaluator, Verifier judge, NER fallback, etc.) |
| AI — generation | **Qalb-8B**, a separate local endpoint, so a heavy generation call never blocks a reasoning call |
| AI — embeddings | **multilingual-e5-large-instruct**, local, 1024-dim, asymmetric (query vs. document embedding differ) |
| AI — reranking | **bge-reranker-v2-m3**, local cross-encoder |
| AI — cloud fallback | Groq → Gemini, only on local-endpoint failure; fully disabled under `AIR_GAP_MODE` |
| Testing | pytest (backend, no live network by design) + Vitest (both frontends) |
| CI | GitHub Actions — see §8 |

Local-first is the deliberate default: every `LOCAL_*_URL` can be left empty to run
purely on cloud fallback, but the system is built and tuned to serve traffic entirely
on-prem, in an air-gapped deployment mode.

---

## 3. Architecture — the shape of the system

Two parallel answer-producing pipelines currently coexist in `src/pipeline/`:

1. **The orchestrator** (`src/pipeline/orchestrator.py`) — the original, hand-coded
   `if/elif` control flow. This is what serves every route today.
2. **The agent harness** (`src/pipeline/harness/`) — a newer supervisor → sub-agent →
   tool restructuring of the same logic, built and tested, but only live for whichever
   routes are named in `HARNESS_CUTOVER_ROUTES` (empty by default — nothing is cut over
   yet). See §5.

Both share the same underlying primitives (retrieval, graph traversal, generation,
verification) — the harness composes them through sub-agents; the orchestrator calls
them directly in sequence.

### Request flow (orchestrator path — what actually serves traffic today)

```
User query (+ active case_id, if any)
    │
    ▼
Query rewriter (LLM) — resolve pronouns, translate to English, make standalone
    │
    ▼
Router (LLM) — classify into one of 9 routes + case_scope + target_entity + output_format
    │   (a deterministic regex layer intercepts known misclassification patterns before
    │    the LLM call runs; case_scope is forced back to within_case for every route
    │    except XGRAPH/XAGG/XNETWORK, unconditionally, even if the router disagrees)
    │
    ├── DIRECT           → answer from general knowledge, no retrieval, no Verifier gate
    ├── RAG               → expand query → embed + keyword search → RRF fuse →
    │                        cross-encoder rerank → relevance evaluator → generate
    │                        (retries on rejection, falls back to guarded web search)
    ├── GRAPH/GRAPH_HYBRID → Apache AGE traversal, case-scoped (HYBRID also runs RAG
    │                        in parallel and RRF-merges); falls back to RAG on failure
    ├── XGRAPH            → cross-case graph traversal — supervisor role or higher only;
    │                        never falls back into case-scoped RAG (structural separation)
    ├── XAGG              → cross-case aggregate (counts, recurring entities) — same
    │                        role gate; a Verifier rejection falls back to the raw
    │                        computed table, since that evidence is correct by construction
    ├── XNETWORK          → cross-case thematic synthesis over precomputed community
    │                        summaries — same role gate, never falls back
    ├── WEB               → domain-allowlisted search, disabled under AIR_GAP_MODE
    └── SQL               → structured lookup against police_reference_data via MCP
    │
    ▼ (every evidence-answering route converges here)
Response generator (Qalb-8B) — answers only from the evidence it was handed
    │
    ▼
Verifier (LLM + deterministic checks) — claim-to-citation grounding, off-topic
detection, cross-case leakage, confidence-appropriate hedging, temporal validity
    │
    ├── grounded      → deliver the answer
    └── not grounded  → serve a safe abstention, never the ungrounded draft
```

The full diagram with exact module names is in [`README.md`](README.md#architecture--pipeline).

### Why a Verifier, structurally

The Verifier is a hard gate on every route that answers from retrieved evidence, not a
prompt instruction hoping the model behaves. It runs **after** generation and checks the
generated text itself — citation markers resolve to real chunks, claims are attributable
to what those chunks say, no cross-case evidence leaked into a case-scoped answer, and a
low-confidence graph link is hedged rather than stated flatly. It fails **closed**: a
JSON-parse failure or an unresolvable claim produces an abstention, never a best-effort
guess. This is the mechanism behind the project's core promise — "Muhafiz does not
guess."

---

## 4. Key components, by directory

```
src/
├── main.py           Entry point. FastAPI app, /api/chat SSE endpoint, dispatches to
│                      the orchestrator or the harness per HARNESS_CUTOVER_ROUTES.
├── config.py          Single source of truth for every env var (see .env.example, which
│                      is NOT fully in sync with this file — config.py wins on conflict).
├── api/                Route modules: sessions, cases, case_assignments, admin,
│                      attachments, graph_review, community_admin, ingestion_quality_admin,
│                      profile, projects.
├── auth/               JWT issuance/verification, CSRF double-submit, password hashing,
│                      rate limiting, role-based FastAPI dependencies.
├── pipeline/            The orchestrator (legacy, live) + router + verifier + xagg +
│                      xnetwork + every pipeline stage.
│   └── harness/         The newer supervisor/sub-agent architecture (see §5).
├── retrieval/           Embedder, ChromaDB vector store, persistent full-text index,
│                      RRF fusion, cross-encoder reranker, graph retriever.
├── graph/                Apache AGE client, entity/officer resolution, identity index,
│                      append-only versioning, conflict detection, community detection.
├── ingestion/            Document loaders' orchestration, chunker, Urdu-aware sentence
│                      splitter/tokenizer/normalizer, background triggers.
├── extraction/           Structured-field regex (CNIC/phone/plate — never LLM), NER,
│                      domain-entity extraction (few-shot LLM).
├── generation/           PDF/XLSX/DOCX builders for in-chat file output.
├── data_gateway/          A `DataGateway` protocol abstraction over Postgres — the layer
│                      every route/pipeline stage calls instead of touching SQL directly.
├── llm/                  Provider clients (local-first + Groq/Gemini cloud fallback),
│                      API-key rotation on rate-limit.
├── database/              SQLAlchemy models, the Postgres engine (including RLS session
│                      variable arming).
└── memory/                Conversation/project memory persistence (Postgres-backed only
                          — no JSON-file storage path despite what an old var name implies).
```

`frontend/` (investigator chat) and `admin-frontend/` (platform-admin dashboard) are two
genuinely separate React apps, not one app with role-gated views — see §6.

---

## 5. The agent harness — why it exists, and its current status

`src/pipeline/harness/` restructures the orchestrator's logic into:

```
Supervisor (routes to one of 8 sub-agents)
   ├── Semantic Search          ├── Timeline Building
   ├── Case Summarization       ├── Cross-Case Linkage
   ├── Report Drafting          ├── Large-Scale Aggregate
   ├── Investigative Analysis   └── Data Quality
         │
         └── compose one or more tools: RAG · GRAPH/GRAPH_HYBRID · XGRAPH ·
                                          XAGG · XNETWORK · SQL · WEB
```

A tool is never independently routable — only a sub-agent is. This is deliberate: it's
what keeps case/role scoping enforceable without re-deriving every enforcement point at
the supervisor level.

**Status: built and tested, not serving live traffic by default.** Cutover is per-route
via `HARNESS_CUTOVER_ROUTES` (a comma-separated allowlist, empty by default). A "shadow
mode" exists to run the harness silently alongside the orchestrator on sampled real
traffic — comparing outputs without ever affecting what a user sees — as the mechanism
for deciding when a route is ready to cut over.

If you're picking up harness work: read
[`docs/EVIDENCE_INTELLIGENCE_PLATFORM_ARCHITECTURE.md`](docs/EVIDENCE_INTELLIGENCE_PLATFORM_ARCHITECTURE.md)
and `docs/decisions/` for the design reasoning already settled — a surprising number of
"why isn't this simpler" questions are answered there, with the tradeoff already
considered and rejected for a stated reason.

---

## 6. The two frontends

| | `frontend/` | `admin-frontend/` |
|---|---|---|
| Audience | Any authenticated user | `platform-admin` (most pages), `supervisor` (review queue, audit logs, entity eval) |
| Styling | Tailwind CSS | Plain CSS, its own `index.css` token file |
| State | Zustand | — |
| Charts | — | Recharts |
| Dev port | 5173 | 5174 |

Both proxy `/api/*` to the backend in dev (see each `vite.config.ts`) — **as of this
writing both proxy targets are hardcoded to `http://127.0.0.1:8001`, while the backend's
actual default port (`src/config.py`, `.env`, `RUN.md`) is `8000`.** This is a live
mismatch, not just a documentation gap — worth fixing in `vite.config.ts` on either side
before relying on the dev proxy. See `README.md`'s Known Limitations for the current
disclosure of this.

Role is never client-supplied or inferred from which app you're using — both apps read
`role` from the authenticated session, and the backend is the only source of truth for
what a given role can do.

---

## 7. Data & migrations

Two migration mechanisms exist side by side, and they've been documented as
**deliberately separate**, not accidentally drifted:

- **`alembic/`** — the original SQLAlchemy-generated baseline (users, sessions,
  messages, pipeline_runs, documents, projects, `police_reference_data`).
- **`migrations/003_*.sql` through `migrations/029_*.sql`** (plain SQL, applied via
  `python scripts/apply_migration.py <file>`) — every schema change since: the case
  model, the Apache AGE graph, RBAC, RLS policies, community detection, the identity
  index, jurisdiction/officer graph labels, ingestion-quality tracking, and more. Apply
  in numeric order — later migrations assume earlier ones ran (RBAC needs `cases` to
  exist for its `case_assignments` FK; RLS needs the RBAC role enum; etc.).

Both are idempotent and safe to re-run. `docs/DATABASE_DESIGN.md` and
`docs/graph_schema.md` describe the relational schema and the graph schema
respectively; `docs/schema-snapshot.json` is regenerated directly from a live database
and is more current than the checked-in ER diagram image.

**Two data-population paths exist:**
1. Hand-created cases via `POST /api/cases` + `ingest_file(..., case_id=...)`.
2. `scripts/sync_muhafiz_data.py` — pulls real FIR/case records from an external Muhafiz
   Data API and projects them into both Postgres `cases` and the evidence graph in one
   pass (structured fields, jurisdiction, officer identity, all of it). This is how the
   corpus the system is actually tested against gets populated at scale, not primarily
   via hand-created cases. See `docs/decisions/0001-muhafiz-api-migration.md`.

---

## 8. CI

`.github/workflows/ci.yml` runs two jobs on every push/PR:

- **Backend**: Python 3.13, `pip install -r requirements.lock.txt` (the exact-pinned
  lockfile — `requirements.txt` itself is the floor-pinned, human-readable intent file),
  then `pytest --cov=src --cov-report=term-missing --cov-report=xml` (coverage is
  reported, not enforced as a gate), then the harness compliance suite
  (`pytest src/pipeline/harness/compliance/` — this one **is** blocking), then a
  non-blocking `pip-audit`.
- **Frontend**: matrixed over `[frontend, admin-frontend]` on Node 20 — `npm ci`,
  `npm run build`, non-blocking `npm audit`.

Note: no `pyproject.toml`/`.python-version` pins a Python floor anywhere in the repo —
"Python 3.11+" in the README is an unenforced convention; CI actually runs 3.13.

---

## 9. Conventions worth knowing before your first PR

- **Every LLM prompt is externalized** to a `.txt` file under `prompts/` — never inline
  in Python. If you're changing model behavior, you're almost always editing a prompt
  file, not a function.
- **Cypher is always parameterized.** `age_client.py`'s `execute_cypher(query, params,
  columns)` — request-derived values are bound parameters, never string-concatenated.
  Graph writes go through one shared versioning primitive
  (`src/graph/versioning.py`), never raw Cypher from elsewhere.
- **The graph is append-only.** No edge is mutated in place; a superseding fact sets
  `superseded_by` on the prior edge. This is why some fixes (a bad entity merge, a wrong
  extracted date) need a deliberate correction path rather than "just update the row."
- **Access control has five independent checkpoints, not one chokepoint** — the API
  boundary's role check, RLS session-variable arming, per-tool cross-case role checks,
  `user_role` provenance (always from the authenticated session, never from a
  user-profile dict), and `scoped_cypher()`'s structural guard on within-case templates.
  A refactor that collapses these into "one auth layer" is a behavior change, not a
  cleanup — each exists because of a specific historical bug.
- **Case scoping composes, it doesn't replace.** A case-scoped retrieval query is
  `case_id == X AND (project match OR is_global)` — the original multi-tenant knowledge
  base logic is still underneath it.
- **Chat attachments and the knowledge base are structurally separate stores**, not a
  filtered view of one store — an attachment is never written to the collection
  retrieval reads, so it cannot leak into another user's answer by construction.
- **`docs/decisions/`** holds ADR-style records for major design calls (e.g. why the
  Muhafiz Data API integration is shaped the way it is, why the graph schema expanded
  the way it did) — check there before re-litigating a decision that was already made
  for a stated reason.
- Two audit passes (`docs/AUDIT_FINDINGS_2026-07-23.md`,
  `docs/AUDIT_FINDINGS_2026-08-04.md`) and their remediation plans (`solution.md`,
  `docs/FIX_PLAN_2026-08-04.md`) are worth reading if you're touching retrieval,
  routing, or the graph — a lot of the current code's shape is a direct response to a
  specific live failure documented there, not a hypothetical concern.

---

## 10. Where to go next

| I want to... | Start here |
|---|---|
| Get the app running locally | [`README.md`](README.md), [`RUN.md`](RUN.md) |
| Understand the deep architecture | [`docs/EVIDENCE_INTELLIGENCE_PLATFORM_ARCHITECTURE.md`](docs/EVIDENCE_INTELLIGENCE_PLATFORM_ARCHITECTURE.md) |
| Understand the graph schema | [`docs/graph_schema.md`](docs/graph_schema.md) |
| Understand the relational schema | [`docs/DATABASE_DESIGN.md`](docs/DATABASE_DESIGN.md), `docs/schema-snapshot.json` |
| Work on the agent harness | `src/pipeline/harness/`, `docs/decisions/` |
| Understand ingestion end to end | [`docs/INGESTION.md`](docs/INGESTION.md) |
| See what's deliberately out of scope | `README.md`'s Known Limitations section |
| Understand a past design tradeoff | `docs/decisions/000*.md` |

This file and `README.md` are living documents. If you find a gap between what's
claimed here and what the code does, that's a doc bug — fix it in the same PR as your
code change if you're the one who caused the drift.
