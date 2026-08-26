<div align="center">

# Muhafiz

**Evidence Intelligence Platform for Islamabad Police.**

_A case-centric, graph-backed investigative assistant — bilingual (Urdu/English) retrieval over case evidence and reference material, entity/relationship traversal across a case's knowledge graph, a hard grounding gate on every generated answer, and role-based access control down to the individual case._

[![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.111%2B-009688?style=for-the-badge&logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-19.2-61DAFB?style=for-the-badge&logo=react&logoColor=black)](https://reactjs.org/)
[![Vite](https://img.shields.io/badge/Vite-8.1-646CFF?style=for-the-badge&logo=vite&logoColor=white)](https://vitejs.dev/)
[![Tailwind CSS](https://img.shields.io/badge/Tailwind_CSS-3.4-06B6D4?style=for-the-badge&logo=tailwindcss&logoColor=white)](https://tailwindcss.com/)
<br/>
[![PostgreSQL](https://img.shields.io/badge/PostgreSQL-Relational_%2B_RLS-4169E1?style=for-the-badge&logo=postgresql&logoColor=white)](https://www.postgresql.org/)
[![Apache AGE](https://img.shields.io/badge/Apache_AGE-Graph_on_Postgres-FF6600?style=for-the-badge)](https://age.apache.org/)
[![ChromaDB](https://img.shields.io/badge/ChromaDB-Vector_Store_1024d-6A5ACD?style=for-the-badge&logoColor=white)](https://www.trychroma.com/)
<br/>
[![Local LLM](https://img.shields.io/badge/Local-Qwen3--14B_%2B_Qalb--8B-8A2BE2?style=for-the-badge)](#technology-stack)

</div>

---

## Table of Contents

- [Overview](#overview)
- [Key Features](#key-features)
- [Architecture & Pipeline](#architecture--pipeline)
- [Documents: Knowledge Base vs. Chat Attachments](#documents-knowledge-base-vs-chat-attachments)
- [The Evidence Graph](#the-evidence-graph)
- [Database Schema](#database-schema)
- [Technology Stack](#technology-stack)
- [Project Structure](#project-structure)
- [Setup & Installation](#setup--installation)
- [Configuration](#configuration)
- [API Reference](#api-reference)
- [Admin Dashboard](#admin-dashboard)
- [Security & Audit Remediation](#security--audit-remediation)
- [Known Limitations & Deferred Scope](#known-limitations--deferred-scope)
- [Design System](#design-system)

---

## Overview

Muhafiz began as a police-reference RAG chatbot and has been rebuilt into a **case-centric evidence intelligence platform**: a Case (not a document, not a chat) is the organizing unit every piece of evidence, every graph entity, and every access-control decision attaches to.

Ask a question — in Urdu or English — about a specific case, the law, a procedure, or how entities connect across cases, and Muhafiz routes it to the right source: case-scoped document retrieval, an entity/relationship graph built from the case's own evidence, a structured reference-data lookup, or (for supervisors and above) a cross-case query. Every answer that draws on retrieved evidence — from any of those sources — passes through a dedicated **Verifier** before it reaches the investigator: a hard grounding gate that checks the answer's claims against its cited sources, rejects off-topic or cross-case-leaked content, and abstains rather than guessing when a claim can't be verified.

**Muhafiz does not guess.** If an answer isn't supported by the retrieved evidence, structured reference data, or a credible web source, it says so rather than fabricating a citation — and now, that guarantee is enforced by a dedicated verification step, not just prompt wording.

Access is controlled per case: an investigator sees only the cases they're assigned to (or created); cross-case queries — which can surface a recurring vehicle, phone number, or person across separate investigations — require a supervisor role or higher, are hard-blocked otherwise, and are audit-logged either way. Postgres Row-Level Security backstops the same rule at the database layer.

---

## Key Features

### Case & evidence model

- **Case as the organizing entity.** A `Case` (FIR number, crime category, investigating officer, station, status, victim/suspect info) is created via the API; evidence documents, chat sessions, and graph entities all carry a `case_id`. Retrieval, RLS, and the Verifier's leakage check all key off it.
- **`case_id` composes with the pre-existing project/global scoping** rather than replacing it — a case-scoped query is `case_id == X AND (project match OR is_global)`, so the original multi-tenant knowledge base is untouched.

### Retrieval, routing & language

- **Nine-way routing.** The router classifies every query into one of `DIRECT`, `RAG`, `WEB`, `SQL`, `GRAPH`, `GRAPH_HYBRID`, `XGRAPH` (cross-case graph), `XAGG` (cross-case aggregate), or `XNETWORK` (cross-case, open-ended network/theme synthesis over precomputed community summaries — see [The Evidence Graph](#the-evidence-graph)) — plus a `case_scope` (`within_case` by default; only `XGRAPH`/`XAGG`/`XNETWORK` are ever `cross_case`, and that guard is unconditional, not something a malformed router response can bypass). A cross-case query naming a police station/district is narrowed to that jurisdiction's own cases before any graph/vector work runs (query-scope preclassification). Short conversational messages still fast-path to `DIRECT` without an LLM call.
- **Hybrid retrieval with RRF + cross-encoder reranking.** Semantic (ChromaDB) and keyword (Postgres `tsvector` / BM25) signals fuse via Reciprocal Rank Fusion (k = 60) to a top-10 candidate set, which a **bge-reranker-v2-m3 cross-encoder** re-scores down to the top-5 before the relevance evaluator sees it.
- **Query expansion, self-correcting retry loop, guarded web search, SQL lookups against `police_reference_data`** — all carried over from the original build; see [Architecture & Pipeline](#architecture--pipeline) for how they now fit alongside the graph routes.
- **Bilingual, everywhere.** `preferred_language` drives generation on every route, including the new graph/cross-case branches, not only RAG — plus a language toggle in the chat UI.
- **Urdu-aware text pipeline.** Sentence splitting on Urdu sentence-final marks (`۔ ؟`), a purpose-built regex tokenizer (chosen over `urduhack`/Stanza after evaluating both — see `docs/URDU_TOKENIZATION_DECISION.md`), NFKC normalization (which fixed a real bug: the corpus was stored in Arabic *presentation-form* codepoints that compared equal to nothing), and a Roman-Urdu detection tag written per chunk.

### Knowledge graph

- **Apache AGE graph, on the same Postgres instance** — no separate graph database to run or back up (see [The Evidence Graph](#the-evidence-graph)).
- **Entity extraction**: regex-only structured fields (CNIC, phone, plate, FIR#, dates — never LLM), a statistical NER pass with LLM fallback on low-confidence spans, and few-shot domain-entity extraction (vehicles, weapons, organizations, aliases, incidents).
- **Entity resolution**: CNIC-first with a **hard, structural block on merging two entities with different non-null CNICs** (not a scored signal that could drift), name-fallback tiers for everything else, LLM adjudication only on medium-confidence pairs, and a review queue (with precision/recall against a labeled ground-truth set) for anything not auto-resolved. A document-scoped resolution cache means a name repeated many times in the same document reuses the same node instead of re-minting one per mention (fixed a real symptom: unreviewed same-document duplicates were distorting community detection's clusters — `scripts/collapse_same_document_duplicate_persons.py` bulk-confirms the backlog this left behind, live-verified against the real corpus). The pending review queue itself is continuously re-scored against fresh corroborating evidence (never auto-confirmed/rejected by this — confirming a match stays a human action), and grouped by shared underlying signal so a reviewer can act on a whole cluster of related candidates at once instead of one-by-one. Optionally (`FEATURE_HEDGED_PENDING_TRAVERSAL`, off by default), a cross-case query can traverse a not-yet-confirmed `SAME_AS` link too — always confidence-capped and disclosed to the Verifier as a hedge, never presented as settled identity.
- **Append-only versioning.** No edge is ever mutated in place — a new fact supersedes the old one via a `superseded_by` pointer, so "what did the system believe, and from what source, at what point" stays answerable. Timeline events (`OCCURRED_ON`) can be locked by an investigator to block further automatic revision.
- **Cross-case graph traversal, aggregates & network synthesis**, gated to `supervisor` role or higher — a hard `PermissionError` (audit-logged as `authorization_violation`) below that role, not a silent downgrade. Unconfirmed identity links are surfaced as caveats, never presented as confirmed fact. Station/district-scoped case enumeration reuses the identical role gate, not a looser tier of its own.
- **Jurisdiction & officer identity.** `PoliceStation`/`District` are real graph nodes (`Case-[FILED_AT]->PoliceStation-[PART_OF]->District`), and officers (investigating + recording) resolve by belt number with the same hard-block discipline CNIC-based person resolution gets, keeping a full reassignment history rather than collapsing to "current officer."
- **Community detection & network summaries.** Louvain clustering over the person graph produces precomputed, LLM-summarized community reports (`XNETWORK`'s evidence source), refreshed automatically on ingest once the graph has drifted past a staleness threshold — not admin/script-invoked only. **Multi-level, not a single flat partition**: every Louvain level from finest to coarsest is persisted (`community_membership.level`), with the finest `MAX_LEVELS_TO_SUMMARIZE=3` levels actually summarized (summarizing every level multiplies LLM cost per level for little benefit at the coarse end) — this is what Global Search (below) reasons over at a chosen hierarchy level instead of one fixed granularity.
- **Conflict detection.** Deterministic timeline conflicts (the same resolved incident with contradictory dates) plus LLM-identified narrative conflicts — the latter only written to the graph when the LLM supplies a verbatim quote that's actually found in the source text; a missing or hallucinated quote is rejected, not passed through.
- **Scale prerequisites.** A real Postgres identity index backs CNIC/plate/belt-number lookup (replacing an O(nodes) graph scan), a persistent `tsvector`/GIN full-text index replaces per-query in-memory BM25 index rebuilds, and embedding calls run with bounded concurrency instead of one-request-at-a-time pacing.
- **Ingestion quality control.** Every tracked ingestion run (one document upload, or one whole bulk sync pass) gets a real rollup — entity-resolution tier counts, corroboration-gate rejections, extraction errors — visible per-run in a dedicated admin **Ingestion Quality** page, not just aggregated into an opaque total. A deterministic circuit breaker (no LLM judgment call) flags a run whose ambiguous-match or rejection rate comes in more than 10 points above the rolling average of the last 10 same-source runs; flagging never auto-remediates, and a flagged run's status propagates forward until a human acknowledges it. Separately, a continuous background sampler periodically re-diffs a sample of already-resolved entity matches against the graph's current state, looking for a match that's quietly gotten *weaker* since it was first decided — surfaced as an open finding for a human to review, not auto-reversed.
- **Legal-code semantic layer.** `cases.crime_category` (e.g. "PPC, Arms Ordinance 1965") is data-driven, act-level reference data, not an opaque label: `scripts/load_legal_code_acts.py` detects every distinct act actually present in the corpus and upserts a `police_reference_data` row (`category="legal_code_act"`) for each, populated **only** with a real, cited description (never fabricated — an uncovered act gets `description=NULL` and shows up in the script's own coverage report) — six real Pakistani statutes covered as of this audit (PPC, Arms Ordinance 1965, CNSA 1997, PECA 2016, Illegal Dispossession Act 2005, Punjab Domestic Violence Act). XAGG's `counts_by_act` re-derives a per-act case count so a multi-act case counts under every act it carries instead of fragmenting by exact `crime_category` string, and a keyword-matched subset of covered acts (`_LEGAL_CODE_ACT_KEYWORDS`) lets a natural-language query like "narcotics cases" resolve to the right act. The SQL route can also look an act up directly via `police_reference_data`.

### Grounding & safety

- **Verifier Agent — a hard gate, not a suggestion.** Every route that answers from retrieved evidence (`RAG`, `SQL`, `WEB`, `GRAPH`, `GRAPH_HYBRID`, `XGRAPH`, `XAGG`, `XNETWORK`, and every web-search fallback path) is checked before delivery: claim-to-citation grounding, off-topic/generic detection, cross-case evidence leakage, confidence-appropriate hedging for low-confidence graph links, and temporal validity. It fails **closed** — a JSON-parse failure or a rejected claim serves a safe abstention, never a best-effort guess. `DIRECT` (no retrieval happened) is the one route it doesn't gate.

### Agent harness (built, flagged off by default)

A second architecture layer sits alongside the orchestrator described in [Architecture & Pipeline](#architecture--pipeline): `src/pipeline/harness/` restructures the same retrieval/generation logic into a `Supervisor` routing to 11 specialized sub-agents — the original eight (semantic search, case summarization, report drafting, investigative analysis, timeline building, cross-case linkage, large-scale aggregate, data quality) plus three added in a later reconciliation sweep (`findings.md` Modules 8-10): **Local Search** (semantic entity-access-point matching against a dedicated entity-description embedding store, for descriptive references like "the investigating officer" that a literal-name graph seed match can't resolve), **Global Search** (whole-dataset map-reduce reasoning over every precomputed community report at a hierarchy level, not a top-k similarity cut), and **Meta-Analysis** (the outermost layer — decomposes a compound question into up to 5 standalone sub-queries, re-enters the Supervisor concurrently for each, then synthesizes across the sub-answers). Each sub-agent composes the same underlying primitives (RAG, GRAPH/GRAPH_HYBRID, XGRAPH, XAGG, XNETWORK, SQL, WEB) as reusable tools rather than a hand-coded `if/elif` chain. It's wired live into `main.py`'s chat endpoint behind a per-route rollout flag (`HARNESS_CUTOVER_ROUTES`, a comma-separated route allowlist, **empty/off by default** — every route still runs through the original orchestrator unless explicitly cut over). Not a parallel product, not experimental scaffolding left unfinished: it's the same contracts, same audit logging, same case/role scoping, built and tested, staged for a route-by-route rollout rather than a single big-bang switch.

### Security & access control

- **Role-based + attribute-based access control.** Four roles (`investigator` / `supervisor` / `station-admin` / `platform-admin`); a `case_assignments` table anchors who can see which case. `station-admin` does **not** get blanket case visibility — need-to-know applies to every role below `platform-admin`.
- **Postgres Row-Level Security as a database-level backstop** on `cases`, `documents`, `sessions`, and `pipeline_runs` (`FORCE`d, so it applies even to the table owner), keyed on a `SET LOCAL app.case_id` session variable set fresh on every request.
- **Append-only audit log** — admin actions, case-assignment changes, graph writes, and every cross-case access attempt (granted *and* denied) are recorded.

### Everything carried over from the original build

Multi-step agentic pipeline with externalized prompts, in-chat file generation (PDF/XLSX/DOCX), per-conversation chat attachments (never enter the shared knowledge base), admin-managed knowledge-base ingestion, multi-format document loaders, Projects with rolling project memory, per-user personalization, and a live per-step pipeline trace over Server-Sent Events. See the sections below for what changed in each.

---

## Architecture & Pipeline

```text
USER QUERY (+ active case_id, if any)
    │
    ▼
Load conversation history + project/case context + chat attachments
    │
    ▼
[LLM] QUERY REWRITER — resolve pronouns, translate to English, make standalone
    │
    ▼
[LLM] ROUTER — { route, case_scope: within_case | cross_case, target_entity, output_format,
    │             station, district }
    │        (short conversational messages fast-path to DIRECT without an LLM call;
    │         every route except XGRAPH/XAGG/XNETWORK is forced back to within_case even if
    │         the router mislabels it — the guard runs after route normalization,
    │         unconditionally; a station/district named in a cross-case query narrows the
    │         candidate case set before any graph/vector/relational work runs)
    │
    ├── DIRECT / NONE → answer from general knowledge (+ user context, language, attachments)
    │                    — no retrieval, so the Verifier does not gate this route
    │
    ├── RAG           → [LLM] QUERY EXPANDER (n=2) → embed variants → ChromaDB (case-scoped)
    │                    + keyword search → persistent Postgres full-text index candidate
    │                    pool → BM25 over that pool → RRF fuse (k=60, top-10)
    │                    → cross-encoder rerank (bge-reranker-v2-m3, top-5)
    │                    → [LLM] RELEVANCE EVALUATOR
    │                         ├─ relevant     → generate
    │                         └─ not relevant → retry loop (feedback-improved query,
    │                               up to MAX_RETRIES) → exhausted → guarded web search
    │                               → safe response
    │
    ├── GRAPH /       → Apache AGE traversal, case-scoped, capped 2-3 hops, confirmed-
    │   GRAPH_HYBRID    SAME_AS-only identity fold (GRAPH_HYBRID also runs vector/BM25 in
    │                    parallel, merged at RRF) → same evaluator gate as RAG → generate
    │                    (falls back to RAG on evaluator rejection)
    │
    ├── XGRAPH        → cross-case graph traversal, optionally jurisdiction-narrowed —
    │                    supervisor role or higher only (hard PermissionError + audit log
    │                    otherwise); unconfirmed identity links surface as caveats, never as
    │                    fact; never falls back into the case-scoped RAG stream, even on
    │                    Verifier rejection
    │
    ├── XAGG          → cross-case aggregate (recurring vehicles/persons across cases,
    │                    station/status/category counts) — same supervisor-or-higher gate
    │
    ├── XNETWORK      → cross-case, open-ended network/theme synthesis over precomputed,
    │                    community-summarized clusters (Louvain over the person graph,
    │                    auto-refreshed on ingest past a staleness threshold) — same
    │                    supervisor-or-higher gate
    │
    ├── WEB           → domain-allowlisted Tavily search → (on failure) Gemini grounded
    │                    search fallback → fully disabled under AIR_GAP_MODE
    │
    └── SQL           → [LLM] parameter extraction → MCP Postgres tool → police_reference_data
                        → no rows / MCP failure → automatic fallback to RAG
    │
    ▼ (every route above that answers from retrieved evidence converges here)
[LLM] RESPONSE GENERATOR (Qalb-8B) — answers only from the evidence it was handed
    │
    ▼
[LLM] VERIFIER — claim-to-citation grounding · off-topic detection · cross-case leakage ·
    │             confidence-appropriate hedging · temporal validity
    │
    ├── grounded      → deliver the answer
    └── not grounded  → serve a safe abstention — never the ungrounded draft
    │
    ▼
Persist: messages, pipeline_runs (incl. verifier_passed / verifier_regenerated),
pipeline_steps, audit_logs · update project memory (background) · background
case-scoped conflict detection (Phase 8)
    │
    ▼
output_format == file_pdf | file_xlsx | file_docx ?
    │ no                                  │ yes
    ▼                                     ▼
Return chat response            [LLM] FILE STRUCTURER → build PDF/XLSX/DOCX
                                          → store + return a download card
```

Most "[LLM]" steps above (rewriter, router, expander, evaluator, Verifier judge, plus the
graph-extraction/adjudication stages) share **one** local Qwen3-14B instance across nine
prompted roles; final response generation is the one step routed to a separate model
(Qalb-8B). See [Technology Stack](#technology-stack).

This diagram is the orchestrator's own control flow. A route named in `HARNESS_CUTOVER_ROUTES`
(empty by default) is instead handled by the agent-harness `Supervisor` described in
[Key Features](#key-features) — same external contracts (SSE trace, Postgres run logging,
conversation/project memory), a coarser per-step trace for now, one route at a time.

---

## Documents: Knowledge Base vs. Chat Attachments

Two ways a document can enter Muhafiz. They are deliberately separate systems, and the separation is **structural, not a filter**.

|                | **Knowledge base**                             | **Chat attachment**              |
| -------------- | ---------------------------------------------- | --------------------------------- |
| Added by       | Admin, from the admin panel                    | Any user, from the chat composer |
| Processing     | Chunked → embedded → indexed                   | Text extracted once               |
| Stored in      | ChromaDB collection (+ Postgres `documents`)   | `session_attachments`             |
| Retrievable by | **Everyone** — it answers all users' questions | Only that one conversation        |
| Lifetime       | Permanent, shared                              | Dies with the conversation        |
| Endpoint       | `POST /api/admin/kb/upload`                    | `POST /api/attachments`           |

An attachment cannot leak into another user's answer because it is never written to the collection retrieval reads. Its text reaches the model only through the prompt of its own conversation, clearly labelled as user-supplied and not part of the knowledge base, and capped so a large PDF cannot crowd out retrieved documents.

**Case evidence is a third path, distinct from both.** `ingest_file()`/`ingest_directory()` accept an optional `case_id` that tags the resulting chunks (Chroma metadata + the `documents` row) for case-scoped retrieval and graph extraction. **This is not currently enforced** — `case_id` is an optional parameter, not a required one, at every call site including the admin bulk-ingest path — so evidence *can* be ingested without a case attached. Treat "ingest case evidence with an explicit `case_id`" as an operational discipline for now, not something the code guarantees.

**A fourth, bulk path exists for real FIR/case data specifically:** `scripts/sync_muhafiz_data.py` pulls records from the Muhafiz Data API (currently a same-schema stand-in, not yet the final production integration — see `docs/decisions/0001-muhafiz-api-migration.md`) and projects them into both Postgres `cases` and the evidence graph in one pass — `PoliceStation`/`District`/`Officer` nodes, structured-field edges, jurisdiction/officer identity, all of it. `--full` is idempotent (safe to re-run; re-projects everything without duplicating edges) but not incremental — every run re-fetches and re-projects the whole dataset. This is how the corpus this system is actually tested against gets populated, not exclusively hand-created cases via `POST /api/cases`.

Full detail: [`docs/INGESTION.md`](docs/INGESTION.md).

---

## The Evidence Graph

A separate graph layer runs **inside the same Postgres instance**, via the [Apache AGE](https://age.apache.org/) extension (`evidence_graph`) — not a standalone graph database to install, back up, or monitor. AGE enforces no schema of its own; `docs/graph_schema.md` plus application-layer validation in `src/graph/*.py` is the enforcement, and every writer goes through one shared versioning primitive (`src/graph/versioning.py`), never raw Cypher from elsewhere in the codebase.

**Node types:** `Case`, `Person`, `Vehicle`, `PhoneNumber`, `Address`, `Organization`/`Gang`, `Weapon`, `Incident`, `Document`, `StructuredRecord`, `PoliceStation`, `District`, `Officer` (the last three from the graph-scale/schema-expansion work — see `docs/decisions/0002-graph-schema-expansion-and-scale.md`).

**Edge types:** `BELONGS_TO_CASE`, `APPEARS_IN` (reused for chalaan name-resolution and typed-recovered-property links), `ASSOCIATED_WITH`, `SAME_AS` (identity — `pending`/`confirmed`/`rejected`, confirmed-only ever treated as the same real-world entity), `OWNS`/`REGISTERED_TO`, `LOCATED_AT` (reused for a witness's home jurisdiction, distinct from the case's own filing station), `INVOLVED_IN`, `PART_OF` (reused for `PoliceStation`→`District`), `OCCURRED_ON` (lockable by an investigator; reused for zimni-entry and position-timeline dated events), `CONFLICTS_WITH` (written by Phase 8's conflict detection), `CITES` (a prose FIR→FIR citation, always `pending`, its own review queue), `FILED_AT` (`Case`→`PoliceStation`), `ASSIGNED_TO` (`Officer`→`Case`, full reassignment history), `RELATED_TO` (person-to-person relationship, e.g. accused→victim), `CROSS_VERSION_OF` (`Case`→`Case`, a confirmed structured cross-FIR link).

Cross-case traversal is bounded by default — a within-case query can no longer fold another case's node into its result through a confirmed identity link without going through the same role gate cross-case queries use.

**Every edge is append-only and carries provenance** (`source_doc_id`, and `source_chunk_id` where the extraction traces to a specific chunk) — nothing is ever mutated in place; a superseding write sets `superseded_by` on the prior edge instead of deleting it. Cypher is always parameterized (`age_client.py`'s `execute_cypher(cypher_query, params, columns)`) — request-derived values are bound parameters, never string-concatenated into the query text.

Full schema, the AGE-specific connection quirks it's built around (statement caching, `$1::agtype` casting, first-write label races), and worked traversal examples: [`docs/graph_schema.md`](docs/graph_schema.md).

---

## Database Schema

PostgreSQL (local/self-hosted, reached via direct SQL only) holds all relational data *and* the AGE graph, with full-text search native via Postgres `tsvector`. Vector search runs separately in ChromaDB, not in Postgres.

![Database schema](docs/database-schema.png)

_Regenerated 2026-08-26 — all 27 real tables, auto-laid-out into zones mirroring [`docs/DATABASE_DESIGN.md`](docs/DATABASE_DESIGN.md)'s own section headers. (The image that shipped here before that date was a leftover artifact from an unrelated product — its own title read "TaxIQ — Database Schema" and its hardcoded table layout referenced tables like `tax_rates`/`document_chunks`/pgvector that don't exist in this schema at all; caught when someone actually looked at the rendered image, not by code review.) Both the diagram (`scripts/build_erd.py`) and the JSON it's built from ([`docs/schema-snapshot.json`](docs/schema-snapshot.json), `scripts/generate_schema_snapshot.py`) are machine-generated straight from live `information_schema` introspection, not hand-maintained — re-run both scripts after a schema change to refresh._

Relationship overview (crow's-foot `<` = the many side):

```text
users ──┬──< sessions ──┬──< messages ──< generated_files
        │               ├──< pipeline_runs ──┬──< pipeline_steps
        │               │                    └──< mcp_tool_calls
        │               └──< generated_files
        ├──< projects ──┬──< sessions (project_id)
        │               ├──< project_memory
        │               └──< documents (project_id)
        ├──< pipeline_runs (user_id)
        ├──< case_assignments ──< cases ──┬──< documents (case_id, nullable — see below)
        │                                 ├──< sessions (case_id)
        │                                 └──< case_assignments
        ├──< audit_logs (user_id, case_id — both nullable FKs, ON DELETE SET NULL)
        └──o user_context_profiles          (1:1 — user_id is the PK)

Decoupled by design (no FK constraint — shown dashed in the diagram):
  session_attachments  ~ sessions/users   -- per-conversation files, never in the KB
  error_logs           ~ runs/sessions/users
  ingestion_jobs       ~ documents

Standalone:
  police_reference_data  -- structured penal-code + legal-code-act reference data,
                             queried on the SQL route via MCP (see Knowledge graph,
                             "Legal-code semantic layer", above)

Not in Postgres tables at all — Apache AGE graph (`evidence_graph`), see
The Evidence Graph section above.
```

Document chunks and their embeddings live in ChromaDB, not Postgres — `documents` only tracks per-file ingestion metadata (status, chunk count, source filename, `case_id`).

**Key tables & indexes**

- **`cases`** — the organizing entity: `case_id` (text primary key, e.g. `CASE-A1B2C3D4` — not a UUID; a human-meaningful code investigators actually search for), FIR number, crime category, investigating officer, station, status, victim/suspect info (JSONB). `documents.case_id`/`sessions.case_id` are nullable, `ON DELETE SET NULL` — deleting a case must not silently delete evidence.
- **`case_assignments`** — `(case_id, user_id, role)`. This, not `cases.investigation_officer` (a free-text field, never checked against a user account), is the *only* mechanism that grants case access — see the caveat under [Known Limitations](#known-limitations--deferred-scope).
- **`audit_logs`** — append-only: `event_type`, `user_id`, `case_id`, `details` (JSONB). Covers admin actions, case-assignment changes, graph writes, and cross-case access attempts, both granted and denied (`authorization_violation`).
- **`users.role`** replaces the original `is_admin` boolean — a Postgres enum (`investigator` / `supervisor` / `station-admin` / `platform-admin`).
- **`pipeline_runs`** gained `verifier_passed` / `verifier_regenerated` (Phase 6) alongside the original per-query columns (routed_to, retry_count, timings) that power the trace panel and every latency chart.
- **ChromaDB collection** (not a Postgres table) — chunk text, 1024-dim embeddings, and metadata (`is_global`, `case_id`, `source_file`, `is_roman_urdu`, etc.) for hybrid retrieval. The vector half never touches Postgres.
- **`chunk_fulltext`** — a persistent, incrementally-maintained `tsvector`/GIN-indexed mirror of every chunk's text, replacing a full in-memory BM25 index rebuild on every single query. Postgres does the keyword half of hybrid retrieval from this table, not by rebuilding an index per request.
- **`identity_index`** — `(label, id_key, id_value) → entity_id`, a real Postgres primary-key lookup backing CNIC/plate/belt-number resolution (`Person`/`Vehicle`/`PhoneNumber`/`Officer`), replacing an O(nodes) Apache AGE label scan. Maintained from the same choke point every graph node write already goes through; the graph scan stays as a fallback on a miss, never removed.
- **`community_runs` / `community_membership` / `community_reports`** — the latest Louvain community-detection pass over the person graph and its LLM-written summaries (`XNETWORK`'s evidence source). Each run replaces the prior one wholesale — a latest-snapshot table, not append-only history the way the graph itself is.
- **`pending_candidate_priority`** — a side table shadowing every currently-pending `SAME_AS`/`CITES` edge, backing the entity-resolution review queue's re-prioritization/grouping view with a real index instead of an unindexed `WHERE status = 'pending'` scan. Never the source of truth — always traceable back to the real graph edge.
- **`session_attachments`** — deliberately has **no** foreign key to `sessions`; the missing constraint is the structural guarantee that attachments can never be joined into shared retrieval.
- **`error_logs` / `ingestion_jobs`** — back the admin error history and ingestion status.

**Retired / superseded** — present in the schema but no longer written by anything live:

- **`messages.citation_validated` / `messages.unverified_citations`** — written by the original `citation_validator.py` background task; Phase 6 replaced that mechanism entirely with the Verifier (`pipeline_runs.verifier_passed`/`verifier_regenerated`, checked synchronously and gating). Treat these two `messages` columns as legacy.

Migrations live in two places, and the two have already drifted apart (documented in each new migration's own header rather than silently papered over):

- `alembic/` — the original SQLAlchemy migration chain.
- `migrations/*.sql` — plain SQL, applied via `python scripts/apply_migration.py <file>`. Everything from migration 003 onward (attachments/errors/ingestion-jobs, the case model, the AGE graph, RBAC, verifier fields, RLS policies) lives here, not in Alembic.

### Source data: the Muhafiz Data API's own schema

Everything above is Muhafiz's **own** relational schema — what this platform's Postgres instance stores. It's a different thing from the schema of the **external system it ingests from**: [`muhafiz_schema.dbml.txt`](muhafiz_schema.dbml.txt) (plus its rendered diagram, [`muhafiz_schema_v11.png`](muhafiz_schema_v11.png)) is the real, reverse-engineered schema of the Muhafiz Data API (`https://muhafiz.onrender.com`, see [`API_CONSUMER_GUIDE.md`](API_CONSUMER_GUIDE.md)) — reconstructed from two genuine filled Islamabad Police case files, revision 11. Four separate real-world systems, each modeled as its own schema block (very likely separate databases in a real deployment): **`psrms`** (Police Station Record Management — `fir` plus its 12 child tables: sections, zimni entries, accused/witness/investigating-officer rows, malkhana/weapon registers, chalaan dispatch/outcome, position timeline, cross-version links, roznamcha), **`cms`** (Complaint Management — pre-FIR complaints), **`pkm`** (citizen-facing service applications — character certificates, driving licenses, tenant/employee registration, vehicle verification, loss reports, women-violence reports), and **`criminal_db`** (criminal records). `src/data_gateway/muhafiz_api/models.py`'s own module docstring documents exactly where the live API's real response shape has already drifted from this DBML (nullable-far-more-often-than-implied fields, an `fir.crime_scene_location` merge that kept the four legacy columns it was meant to replace, slug IDs instead of the UUIDs the DBML declares) — read both together, not the DBML alone. `docs/decisions/0001-muhafiz-api-migration.md` is the full decision record for why this became the evidence source at all, in place of the original synthetic document corpus.

---

## Technology Stack

### Backend

| Layer           | Technology                                                                         |
| --------------- | ------------------------------------------------------------------------------------ |
| Framework       | FastAPI (async), Uvicorn                                                              |
| Database        | PostgreSQL (local/self-hosted, relational + Apache AGE graph) + ChromaDB (vectors)   |
| Graph           | Apache AGE — Postgres extension, parameterized Cypher via a dedicated `asyncpg` pool  |
| Data access     | Async SQLAlchemy + asyncpg (direct SQL — the only access path)                       |
| Keyword search  | Postgres `tsvector` · `rank-bm25` (Urdu-aware tokenizer)                             |
| Migrations      | Alembic · plain SQL (`migrations/`)                                                  |
| Structured data | Model Context Protocol (MCP) Postgres server                                         |
| Auth            | JWT in an HttpOnly cookie + double-submit CSRF, bcrypt, slowapi rate limits, RBAC/ABAC (role enum + `case_assignments`), Postgres RLS backstop |

### AI

| Role                                                                                                                                  | Model                     | Notes                                                              |
| --------------------------------------------------------------------------------------------------------------------------------------- | -------------------------- | -------------------------------------------------------------------- |
| Reasoning — router, rewriter, query expander, relevance evaluator, SQL-param extraction, Verifier judge, doc-type classification, NER fallback, domain-entity extraction, resolution adjudication | **Qwen3-14B**              | One local instance, nine prompted roles, served on an OpenAI-compatible endpoint (`LOCAL_LLM_URL`) |
| Generation — the final answer, on every route                                                                                          | **Qalb-8B**                | Separate local endpoint (`LOCAL_GEN_LLM_URL`), so a heavy generation call never blocks a reasoning call |
| Embeddings                                                                                                                              | **multilingual-e5-large-instruct** | Local endpoint (`EMBEDDINGS_URL`), 1024-dim, instruction-prefixed for queries, unprefixed for stored chunks |
| Reranker                                                                                                                                | **bge-reranker-v2-m3**      | Local endpoint (`RERANKER_URL`), cross-encoder re-score of RRF's fused top-10 down to top-5 |
| Sentence splitting                                                                                                                      | Rule-based regex           | No model — `src/ingestion/sentence_splitter.py`                     |
| Cloud fallback (reasoning + generation)                                                                                                 | Groq (`GROQ_MODEL`, configurable) → Gemini | Only on local-endpoint failure; **fully disabled under `AIR_GAP_MODE`**, which fails closed rather than silently phoning a cloud provider. Groq model catalogs get retired/rotated — verify `GROQ_MODEL` against `client.models.list()` for your account rather than assuming a value in `.env` still resolves; see [Known Limitations](#known-limitations--deferred-scope) |
| Grounded web search                                                                                                                     | Tavily → Gemini grounded-search fallback | Domain-allowlisted; disabled under `AIR_GAP_MODE`                   |

`docker-compose.yml` includes an example `vllm` service definition for serving Qwen3-14B; the e5 embedding and reranker endpoints need their own serving process (e.g. a small FastAPI/sentence-transformers wrapper, or a TEI-style server) — see [Setup & Installation](#setup--installation). API keys for the cloud fallback are rotated automatically on rate-limit (`src/llm/key_manager.py`), so `GROQ_API_KEY_1..n` / `GEMINI_API_KEY_1..n` can be supplied.

### Frontend

| Layer        | Technology                                                        |
| ------------ | -------------------------------------------------------------------- |
| Framework    | React 19 · TypeScript · Vite                                        |
| Styling      | Tailwind CSS, token-driven (see [Design System](#design-system)), light/dark theme toggle |
| State        | Zustand                                                              |
| Realtime     | Server-Sent Events                                                   |
| Admin charts | Recharts                                                              |

---

## Project Structure

```text
muhafiz/
├── admin-frontend/       # Admin dashboard (React, plain CSS, Recharts)
├── alembic/              # SQLAlchemy migration chain
├── archive/              # One-off scripts and superseded docs (gitignored)
├── data/
│   ├── documents/        # Source files for the knowledge base
│   └── generated/        # AI-generated PDF/XLSX/DOCX output
├── docs/                 # Architecture, implementation plan, graph schema, INGESTION.md, DESIGN.md
├── frontend/             # Main chat app (React, Tailwind, Zustand)
│   └── public/brand/     # Logo assets (SVG)
├── mcp-servers/          # MCP server configuration
├── migrations/           # Plain-SQL migrations (case model, AGE graph, RBAC, verifier fields, RLS,
│                         #   identity/full-text indexes, jurisdiction/officer graph labels, community detection)
├── prompts/              # All LLM system prompts, externalized
├── scripts/              # Ingestion, migration, evaluation, and maintenance utilities
├── src/
│   ├── api/              # Routes: sessions, profile, projects, cases, case_assignments,
│   │                     #   graph_review, community_admin, admin, attachments
│   ├── auth/             # JWT, CSRF, password hashing, rate limiting, role-based dependencies
│   ├── data_gateway/      # DataGateway protocol + direct backend + muhafiz_api (external FIR/case sync client)
│   ├── database/         # SQLAlchemy models, Postgres engine (incl. RLS session vars), pipeline logger
│   ├── extraction/       # Structured-field regex, doc-type classifier, NER, domain-entity extraction
│   ├── generation/       # PDF / XLSX / DOCX builders
│   ├── graph/            # Apache AGE client, entity/officer resolution, identity index, versioning,
│   │                     #   structured/cross-silo projection, conflict detection, community detection
│   ├── ingestion/        # Loaders, chunker, sentence splitter, Urdu normalizer, tokenizer, ingestion service,
│   │                     #   background triggers (conflict detection, candidate reprioritization, community refresh)
│   ├── llm/              # Provider clients (local-first + cloud fallback), key rotation
│   ├── mcp/               # MCP client for the SQL route
│   ├── memory/            # Conversation persistence
│   ├── observability/     # Error capture + analytics aggregation
│   ├── pipeline/          # Orchestrator, router, verifier, xagg, xnetwork, and every pipeline stage;
│   │                     #   harness/ — the second, sub-agent-based architecture layer (flagged off by default)
│   └── retrieval/         # Embedder, vector store, BM25, persistent full-text index, RRF,
│                          #   cross-encoder reranker, graph retriever, community vector store
├── tests/                # Pytest suite (no network access)
├── docker-compose.yml     # AGE-enabled Postgres + an example vLLM service
└── requirements.txt
```

---

## Setup & Installation

### Prerequisites

- Python 3.11+
- Node.js 20+
- PostgreSQL, local/self-hosted, running an **Apache AGE-enabled image** (`docker-compose.yml` is pinned to `apache/age:release_PG16_1.5.0`, not plain `postgres`) — the graph layer requires this; a stock Postgres image will fail `CREATE EXTENSION age`
- A local model-serving process for Qwen3-14B, Qalb-8B, multilingual-e5-large-instruct, and bge-reranker-v2-m3 (any OpenAI-compatible endpoint works — vLLM, LM Studio, a small FastAPI wrapper, etc.), **or** cloud-only operation via Groq/Gemini if you leave the `LOCAL_*_URL` variables empty (local-first is the default; cloud is the fallback path)

### 1. Backend

```bash
git clone <repo-url>
cd muhafiz

python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env             # then fill in the values below — note .env.example
                                  # itself predates several of the vars in Configuration;
                                  # cross-check against src/config.py if in doubt
```

### 2. Database

```bash
docker compose up -d             # starts the AGE-enabled Postgres
alembic upgrade head
```

Then apply the SQL migrations that are not in the Alembic chain, **in order** — each is idempotent, but later ones depend on earlier ones (the case model before the graph, the graph before RBAC, RBAC before RLS):

```bash
python scripts/apply_migration.py migrations/003_admin_dashboard_and_attachments.sql
python scripts/apply_migration.py migrations/004_case_model.sql
python scripts/apply_migration.py migrations/005_age_graph.sql
python scripts/apply_migration.py migrations/006_rbac.sql
python scripts/apply_migration.py migrations/007_verifier_fields.sql
python scripts/apply_migration.py migrations/008_rls_policies.sql
```

Until 003 is applied, chat attachments are disabled (with an explicit message) and the admin dashboard shows an "instrumentation not applied" banner. Until 004–008 are applied, the Case model, graph, RBAC, Verifier logging, and RLS respectively won't exist — the app does not currently detect and warn about this the way it does for 003.

### 3. Model serving

Point these at wherever your model-serving process is running (see [Configuration](#configuration)): `LOCAL_LLM_URL` (Qwen3-14B, reasoning), `LOCAL_GEN_LLM_URL` (Qalb-8B, generation), `EMBEDDINGS_URL` (multilingual-e5-large-instruct), `RERANKER_URL` (bge-reranker-v2-m3). Leave any of them empty to fall back to the corresponding cloud provider for that role (Groq/Gemini) — except that this fallback is refused entirely when `AIR_GAP_MODE=true`.

If your ChromaDB collection was created before switching to the 1024-dim e5 embedder, it needs a full wipe + re-ingest (`scripts/reingest_kb.py`) — Chroma pins one embedding dimension per collection.

### 4. Run

```bash
uvicorn src.main:app --reload --port 8001  # API      → http://localhost:8001

cd frontend && npm install && npm run dev        # Chat app  → http://localhost:5173
cd admin-frontend && npm install && npm run dev  # Admin app → http://localhost:5174
```

The admin app proxies `/api` to `http://127.0.0.1:8000`. An account needs `role = platform-admin` to reach most of it (`role = supervisor` for the graph-review queue and audit logs).

### 5. Populate cases and the knowledge base

Create a case via `POST /api/cases`, then ingest evidence with `ingest_file(..., case_id=...)` (case_id is currently optional — pass it explicitly for anything that should be case-scoped). Upload shared reference material from the admin panel (**Knowledge Base → drop files**) — that path stays global/`is_global=True` by design and is not case-scoped.

### 6. Air-gap deployment

Set `AIR_GAP_MODE=true` to disable the web-search route entirely and refuse the LLM client's cloud fallback (Groq/Gemini) on any local-model failure, instead of silently sending query text to a cloud provider. This has not yet been exercised as a full "outbound disabled, confirm everything else still runs" dry run against live infrastructure — see [Known Limitations](#known-limitations--deferred-scope).

---

## Configuration

Key `.env` values, per `src/config.py` (the source of truth — `.env.example` has not been kept fully in sync; see the note in Setup above):

| Variable                                                        | Purpose                                                                          | Default        |
| ------------------------------------------------------------------ | ------------------------------------------------------------------------------------ | ---------------- |
| `LLM_PROVIDER`                                                  | Cloud fallback provider (`groq` \| `gemini` \| `openai` \| `anthropic`)             | `gemini`        |
| `GROQ_API_KEY`, `GEMINI_API_KEY`                                | Fallback provider keys (`_1..n` suffixes enable rotation)                          | —               |
| `LOCAL_LLM_URL` / `LOCAL_LLM_MODEL`                             | Reasoning endpoint (Qwen3-14B), OpenAI-compatible. Empty disables local reasoning   | _(empty)_       |
| `LOCAL_GEN_LLM_URL` / `LOCAL_GEN_LLM_MODEL`                     | Generation endpoint (Qalb-8B). Empty disables local generation                     | _(empty)_       |
| `MODEL_SERVER_BASE_URL`                                         | Shared base URL the `LOCAL_*_URL`/`EMBEDDINGS_URL`/`RERANKER_URL` vars can expand from | _(empty)_       |
| `EMBEDDING_PROVIDER`                                            | `e5` (local, 1024-dim) \| `gemini` \| `openai` \| `local` (384-dim CPU fallback)    | `e5`            |
| `EMBEDDINGS_URL` / `RERANKER_URL`                                | Local e5 / bge-reranker-v2-m3 endpoints                                            | _(empty)_       |
| `AIR_GAP_MODE`                                                  | `true` disables web search entirely and refuses the LLM cloud fallback              | `false`         |
| `WEB_ALLOWED_DOMAINS`                                           | Comma-separated domain allowlist for the guarded WEB route                          | gov.pk / islamabadpolice.gov.pk / nadra.gov.pk / a short list of established news domains |
| `DATABASE_URL`                                                  | Local/self-hosted Postgres connection (asyncpg, direct SQL only, AGE-enabled instance) | —            |
| `TAVILY_API_KEY`                                                | Web-search route                                                                   | —               |
| `JWT_SECRET_KEY`                                                | Signs auth JWTs — **the code default is a placeholder dev value; it must be overridden before any real deployment** | `your-secret-key-for-dev` |
| `MAX_RETRIES`                                                   | Retrieval retry budget                                                             | `1`             |
| `TOP_K_RETRIEVAL` / `TOP_K_RERANK`                              | Candidates retrieved / kept after cross-encoder rerank                             | `10` / `5`      |
| `CHUNK_SIZE` / `CHUNK_OVERLAP`                                  | Chunking (now snapped to Urdu-aware sentence boundaries, not raw offsets)           | `512` / `64`    |
| `EMBEDDING_MAX_CONCURRENCY`                                     | Concurrent in-flight requests to `EMBEDDINGS_URL` (bounded, not sequential/paced)   | `8`             |
| `HARNESS_CUTOVER_ROUTES`                                        | Comma-separated route names (`RAG`, `XGRAPH`, ...) to hand off to the agent harness instead of the orchestrator. Empty = every route stays on the orchestrator | _(empty)_ |
| `FEATURE_HEDGED_PENDING_TRAVERSAL`                              | Cross-case only: traverse a `pending` (not yet human-confirmed) `SAME_AS` link, confidence-capped and disclosed as a hedge, instead of excluding it entirely | `false` |

> Conversation memory (`src/memory/conversation.py`) is entirely Postgres-backed via the data gateway — there is no JSON-file storage path and no `MEMORY_BACKEND` variable anywhere in `src/config.py`.

---

## API Reference

All routes are under `/api`. Authentication is a JWT in an HttpOnly cookie; mutating requests require the `X-CSRF-Token` double-submit header.

### Auth

| Endpoint                                       | Purpose                                      |
| ---------------------------------------------- | --------------------------------------------- |
| `POST /api/auth/register` · `login` · `logout` | Account and session lifecycle (rate-limited)  |
| `GET /api/auth/me`                             | Current user                                  |

### Chat

| Endpoint                                                    | Purpose                                                                    |
| ----------------------------------------------------------- | ------------------------------------------------------------------------------ |
| `POST /api/chat`                                            | Send a message; streams the pipeline trace and answer as SSE. Accepts an optional `case_id` — the caller must have a `case_assignments` row for it (or be `platform-admin`) or the request is rejected with `403` before the pipeline runs |
| `GET /api/sessions` · `GET/PATCH/DELETE /api/sessions/{id}` | Session list, history, rename, soft-delete                                    |
| `GET /api/sessions/{id}/export`                             | Export a chat (`?format=json\|md\|pdf`)                                       |
| `GET /api/files/{file_id}/download`                         | Download a generated file (ownership-checked; admins may fetch any)          |

### Cases & assignments

| Endpoint                                                            | Purpose                                                          |
| --------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `GET/POST/PUT/DELETE /api/cases`                                    | Case CRUD; the creator is auto-assigned as `investigator`         |
| `GET /api/cases/{case_id}/assignments`                              | List a case's assignments (`station-admin` role or higher)       |
| `POST /api/cases/{case_id}/assignments`                             | Assign a user to a case with a role (`station-admin` or higher)  |
| `DELETE /api/cases/{case_id}/assignments/{user_id}`                  | Remove an assignment (`station-admin` or higher)                |

### Attachments (per-conversation)

| Endpoint                                                            | Purpose                                        |
| ------------------------------------------------------------------- | ------------------------------------------------- |
| `POST /api/attachments`                                             | Attach a file to one conversation (multipart)     |
| `GET /api/attachments?session_id=` · `DELETE /api/attachments/{id}` | List / remove                                     |

### Projects & profile

| Endpoint                            | Purpose                                       |
| ------------------------------------ | ------------------------------------------------- |
| `GET/POST/PUT/DELETE /api/projects` | Project CRUD, scoped to the owner                 |
| `GET/PUT /api/profile`              | User context, preferred language, model mode      |

### Graph review (`supervisor` role or higher)

| Endpoint                                              | Purpose                                                                  |
| -------------------------------------------------------- | ---------------------------------------------------------------------------- |
| `GET /api/admin/graph-review/pending`                  | Entity-resolution matches awaiting a human decision (not the auto-merged ones) |
| `GET /api/admin/graph-review/stats`                    | Review-queue counts                                                          |
| `POST /api/admin/graph-review/{edge_id}/confirm`       | Confirm a candidate `SAME_AS` match                                           |
| `POST /api/admin/graph-review/{edge_id}/reject`        | Reject a candidate match                                                      |
| `GET /api/admin/graph-review/citations/pending`        | Pending `CITES` (prose FIR→FIR citation) candidates — a separate queue from `SAME_AS` |
| `POST /api/admin/graph-review/citations/{edge_id}/confirm` · `reject` | Confirm/reject a citation candidate                              |
| `GET /api/admin/graph-review/queue`                    | The `SAME_AS`/`CITES` pending queue re-scored/reordered by fresh corroborating evidence — never auto-confirms/rejects |
| `GET /api/admin/graph-review/queue/groups`             | Pending candidates grouped by shared underlying signal, for one batch action instead of one-by-one |
| `POST /api/admin/graph-review/queue/reprioritize`      | Manual full-queue re-score (there is no cron to run this on a schedule)      |
| `POST /api/admin/graph-review/queue/batches/{group_id}/confirm` · `reject` | Act on a whole group at once — internally still one confirm/reject call per member edge |
| `GET /api/admin/graph-review/consistency-findings`     | Open findings from Module G3's continuous sampling — a previously-resolved match whose corroborating evidence now looks weaker than at write time |
| `POST /api/admin/graph-review/consistency-findings/{finding_id}/acknowledge` | Records that an investigator looked at a finding — never touches the underlying `SAME_AS` edge; use confirm/reject above for that |

Module G3 (Ingestion Quality Control at Scale — `INGESTION_QUALITY_AT_SCALE_PLAN.md`) extends `scripts/eval_entity_resolution.py`'s ground-truth idea into a continuous background job: a fire-and-forget task (same execution model as D1/E3 above), throttled to a 20% chance per ingest since it audits the whole corpus, not the case that was just ingested, periodically samples up to 20 of the most-recently-touched `SAME_AS` candidates (pending *or* already-confirmed — never rejected) and re-diffs each one's original name-similarity/shared-case/shared-structured-id scoring snapshot against the graph's current state, the same diff idiom `candidate_reprioritization.py` already uses for D1's reinforcement check, mirrored to look for *degradation* instead. `src/graph/entity_resolution_sampling.py` never imports `versioning` or any AGE write path — structurally incapable of touching a `SAME_AS`/`CITES` edge; its only writes are to the new `entity_resolution_consistency_findings` Postgres table (migration 029). Scope is stated honestly: `cnic_auto` merges create no `SAME_AS` edge at all and `versioning.write_node()` overwrites properties with no per-write history, so there is nothing to diff a CNIC-auto merge's identity against without a new node-history sidelog — a real, separate capability this module doesn't attempt. Live-verified against real Postgres/AGE: a constructed fixture (two near-identical-name Persons in the same case, then the candidate renamed and detached from the case) correctly surfaced a finding, and — the property tested most aggressively — the `SAME_AS`/`CITES` edge count was confirmed unchanged before/after the sampling pass.

### Community detection (`supervisor` role or higher)

| Endpoint                              | Purpose                                                                       |
| -------------------------------------- | ------------------------------------------------------------------------------ |
| `GET /api/admin/community/staleness`  | The same drift heuristic checked automatically after every ingest — read-only |
| `POST /api/admin/community/refresh`   | Manual full sweep: re-run community detection + summarization regardless of the staleness check (there is no cron to run this on a schedule) |

### Ingestion quality (`supervisor` role or higher)

Ingestion Quality Control at Scale (`INGESTION_QUALITY_AT_SCALE_PLAN.md`), Modules G1/G2 — visibility into ingestion-run outcomes plus a deterministic circuit breaker, not an agentic quality loop (see the plan's own §1 for why an LLM-judged ingestion loop was explicitly rejected).

| Endpoint                                              | Purpose                                                                  |
| -------------------------------------------------------- | ---------------------------------------------------------------------------- |
| `GET /api/admin/ingestion-quality/runs`                | Per-run entity-resolution tier rollup (G1) plus circuit-breaker flags (G2), newest first; optional `source` filter |
| `GET /api/admin/ingestion-quality/flagged`             | Only the runs currently awaiting acknowledgment                              |
| `POST /api/admin/ingestion-quality/{run_id}/acknowledge` | Clears a flagged run so the next same-source run stops inheriting the flag — never re-scores anything, never touches the graph |

The breaker (`src/graph/ingestion_circuit_breaker.py`) flags a run — never auto-remediates — when its ambiguous-match rate (`human_review`+`flagged_unverified` share of resolved mentions) or corroboration-gate rejection rate comes in more than 10 percentage points above the rolling average of the last 10 finished runs *for that same source* (`ingest_file` and `sync_muhafiz_data` are grouped separately — confirmed live that `sync_muhafiz_data` runs always carry `case_id IS NULL`, so per-case grouping isn't meaningful for it, and `ingest_file` is one row per single-document upload with almost never a repeat `case_id` to baseline against). Both the 10-point threshold and the 10-run window are stated as a starting point, same disclosure `community_detection.get_staleness()`'s own drift thresholds already carry — not tuned against real drift-history data yet. A flagged run's status propagates to the next same-source run until a human acknowledges it (`POST .../{run_id}/acknowledge`); live-verified against real Postgres, including that acknowledging a stale (already-superseded) flagged run in a fast-moving chain correctly does *not* unblock a newer flagged run further down the chain.

### Admin (`platform-admin` role required unless noted)

| Endpoint                                                                        | Purpose                                                     |
| ------------------------------------------------------------------------------- | ---------------------------------------------------------------- |
| `GET /api/admin/metrics`                                                        | Totals for the summary cards                                    |
| `GET /api/admin/usage`                                                          | Requests over time + routing breakdown                          |
| `GET /api/admin/verifier/stats`                                                 | Verifier pass/fail rate (Phase 6)                                |
| `GET /api/admin/latency`                                                        | avg / p50 / p95, trend, per-route and per pipeline step         |
| `GET /api/admin/errors` · `GET /api/admin/errors/trend`                         | Filterable error history and trend                              |
| `GET /api/admin/kb/stats` · `GET /api/admin/kb/jobs`                            | Chunks indexed, chunks per document, ingestion status            |
| `GET /api/admin/eval/entity-resolution` (`supervisor` role or higher)          | Entity-resolution precision/recall from the labeled eval harness |
| `GET /api/admin/audit-logs`                                                     | Filterable audit log, with `authorization_violation` events highlighted |
| `POST /api/admin/kb/upload`                                                     | Upload a document into the shared knowledge base                 |
| `DELETE /api/admin/kb/documents/{source_file}`                                  | Remove a document and its chunks                                 |
| `GET /api/admin/instrumentation`                                                | Which observability tables exist                                |
| `GET /api/admin/runs` · `/runs/{id}/steps` · `/mcp-calls` · `/files` · `/users` | Operational views                                                 |

---

## Admin Dashboard

A separate React app (`admin-frontend/`) on the `/api/admin/*` namespace. Every figure is computed from real rows — nothing is placeholder.

| Page                     | Contents                                                                                                                                                                                                                                                                                                                                                                                  |
| ------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **Dashboard**            | Summary cards (requests, median/p95 latency, chunks indexed, errors, slowest step, **grounding pass rate**); requests-over-time area chart; routing donut; requests-by-route trend + table; latency trend; per-step timing table; errors-over-time; largest documents                                                                                                                     |
| **Knowledge Base**       | Drag-and-drop upload into the shared corpus; per-document ingestion status; document list with chunk counts and delete; chunks-per-document chart                                                                                                                                                                                                                                          |
| **Review Queue**         | Every pending entity-resolution `SAME_AS` candidate the graph pipeline flagged (never the auto-merged CNIC matches) — shows the match *basis* ("matched on name + shared case, unverified"), not a bare confidence number, and lets an investigator confirm or reject                                                                                                                       |
| **Ingestion Quality**    | Per-run entity-resolution tier rollup (Module G1) — CNIC-auto/flagged/human-review/new counts, corroboration-gate rejections, extraction errors — plus Module G2's circuit-breaker flags, filterable by source, with an Acknowledge action on a flagged run                                                                                                                                |
| **Entity Eval**          | Precision/recall against the labeled entity-resolution ground-truth set (`scripts/eval_entity_resolution.py`), including per-tier metrics and named test-case pass/fail                                                                                                                                                                                                                     |
| **Audit Logs**           | Filterable append-only audit trail (event type, case, user), with `authorization_violation` events (rejected cross-case attempts) visually highlighted                                                                                                                                                                                                                                      |
| **Errors**               | Filterable history (severity, module, exception type, free-text search) with expandable stack traces and the originating `run_id`; errors-over-time trend by severity                                                                                                                                                                                                                       |
| **Run History**          | Past pipeline runs, expandable into their step-by-step trace                                                                                                                                                                                                                                                                                                                                 |
| **MCP Calls**            | Tool-call log for the SQL route                                                                                                                                                                                                                                                                                                                                                              |
| **Generated Files**      | Audit and delete AI-generated documents                                                                                                                                                                                                                                                                                                                                                      |

**Backend-only, no admin-frontend page yet:** the `/api/admin/graph-review/queue/*` re-prioritization/grouping endpoints (D1), the `/api/admin/community/*` staleness-check/manual-refresh endpoints, and the `/api/admin/graph-review/consistency-findings/*` endpoints (G3) exist and are tested, but `ReviewQueuePage.tsx` still only calls the original `/pending`/`{edge_id}/confirm`/`reject` endpoints — the newer surfaces are API-only for now. (G1/G2's `/api/admin/ingestion-quality/*` endpoints got their own page, **Ingestion Quality**, in Module G4.)
| **Users**                | Registered accounts                                                                                                                                                                                                                                                                                                                                                                          |

All views share a date-range filter (24h / 7d / 30d / 90d), which also switches bucketing between hourly and daily. Percentiles are nearest-rank, so a reported p95 is a latency a real request actually experienced.

---

## Security & Audit Remediation

An independent code audit against the codebase as of 2026-07-27 (`issues.md`) produced 127 findings (13 Critical, 19 High, 56 Medium, 39 Low — one Critical was added 2026-07-28, the first point the audit had live Postgres access, since a superuser-connection RLS bypass isn't findable by static review alone). A 12-phase remediation plan (`solution.md`) triaged every finding; **all 12 phases (0 through 11) are implemented and committed to `main`** — see `IMPLEMENTATION_PROMPT.md` for the full per-module progress log, including what deviated from the plan and what was live-verified against real Postgres/AGE versus confirmed only by code review and the test suite.

Not every finding got a code change, and this README won't claim otherwise:

- **Deliberately deferred, by design** (`solution.md` §10, six items, each with its own stated reason) — never code-fixed, and not scheduled to be: rich sanitized markdown rendering for the chat UI (needs its own XSS review), a full responsive/mobile-tablet redesign, fully reconciling Alembic's migration history with the plain-SQL chain, retroactively repairing already-corrupted pre-fix historical data, a full dependency-compatibility audit beyond pinning, and making `mypy`/`ruff`/ESLint blocking CI gates rather than report-only. See `solution.md` §10 for the reasoning behind each.
- **Product decisions, not code bugs** (`solution.md` §9, four items) — all four now have a confirmed decision and are implemented: the graph-review entity-resolution queue's cross-case visibility is a **confirmed, permanent product exemption** from per-case confidentiality (documented in [`docs/graph_schema.md`](docs/graph_schema.md)'s "Reviewed tradeoff" section); the Audit Logs page's role gate is confirmed **`platform-admin`-only**, matching the backend (implemented in `App.tsx`/`Sidebar.tsx`); the graph-contamination severity correction was folded into Phase 3's fix; and the one failing CI test at the start of this project was confirmed stale (code was correct) and replaced, not a product reversal.

**Current test suite** (re-run 2026-08-26, `python -m pytest`, against a live `muhafiz-postgres`/AGE instance): **1761 collected, 0 failed, 0 errors, 5 skipped** (`tests/test_pdf_loader.py`'s 6 real-Docling tests excluded from this run — see below; counted via `--junit-xml`, not the terminal summary line, which this pytest/environment combination stopped printing at some point, a harmless reporting quirk not investigated further here since per-test pass/fail detail is unaffected) — grown substantially since the 1457-passed count recorded 2026-08-22, as `findings.md`'s Modules 1-11 (Local Search, Global Search, Meta-Analysis, the legal-code semantic layer, and a further round of route/ingestion/documentation fixes) landed. One test (`test_orchestrator.py::test_cross_case_query_result_set_includes_more_than_one_case`) now shows **XPASS** rather than the previously-recorded xfail — its own `xfail` reason documents a real, pre-existing test-design race (asserts on a fire-and-forget background log call that the assertion can race ahead of), confirmed still accurate on inspection; this is a pre-existing, already-documented condition surfacing differently under this pytest version, not a new regression. `tests/test_pdf_loader.py`'s 6 real-Docling tests are excluded from the count above — they fail in this environment on a genuine, memory-pressure-dependent error (`OSError: The paging file is too small...`, see RUN.md §9), reproduced flakily during this same audit (present on some runs, absent on others depending on what else this machine had done since its last restart), not a code defect; run them separately on a machine with more headroom. Note for anyone re-running this locally: this repo's `.venv` (if present) may be missing `pytest-asyncio`, which surfaces as dozens of `'asyncio' not found in markers configuration` collection errors — not a code regression, an environment gap; use whichever interpreter actually has `pytest-asyncio` installed (check with `python -m pip show pytest-asyncio` before trusting a failing run).

This section, plus the "Fixed since the initial audit" note and every claim in [Known Limitations](#known-limitations--deferred-scope) below, is re-verified against the current code rather than carried forward from what the plan intended — if you find a gap between what's claimed here and what the code does, that's a doc bug, report it.

---

## Known Limitations & Deferred Scope

Verified during an independent code audit (not just the phase build reports) — listed here so this README doesn't claim more than what's actually built.

**Explicitly out of scope, by design** (per `docs/IMPLEMENTATION_PLAN.md`): OCR / scanned-handwritten documents, media evidence (audio/video/image), the SOW's Collaboration module, a specialized multi-agent architecture split, 50–100-user growth-stage hardware, and a Policy Agent — all deferred deliberately, not gaps in the current build.

**Partial / not fully enforced, confirmed in code:**

- `case_id` is optional, not required, at every ingestion call site — "no evidence without a case" is an operational discipline, not a code guarantee.
- `cases.investigation_officer` is a free-text field never checked against a user account — case access is exclusively via `case_assignments`; a case's named IO has no access unless separately assigned.
- Organization/address entities resolve via name-fallback only — there's no hard-block primary identifier for them the way CNIC (person), plate (vehicle), phone number, and belt number (officer) have.
- **Correction (2026-08-22): this line was stale/wrong, not just outdated — the code was checked directly, not carried forward from an old claim.** Entity resolution (`src/graph/entity_resolution.py::_name_similarity()`) *does* attempt cross-script matching — a deliberately coarse Urdu-consonant-to-Roman "skeleton" comparison ("ظفر"/"Zafar" both reduce to `zfr`), chosen over a full transliteration library or an LLM call to avoid a heavier dependency and a new failure mode on the merge-decision path. What's still genuinely true: `graph_retriever.py`'s query-time seed lookup (`_find_seed_nodes`, a plain `CONTAINS` match) does **not** use this — a query naming a person only by their Latin-script name won't seed-match a node whose `canonical_name` was stored in Urdu script, even though entity resolution itself would have merged them into one node at write time.
- Genuinely incremental community *detection* (re-clustering only the part of the person graph that changed) is not built — community refresh moved from admin/script-invoked-only to an automatic staleness-gated trigger, but the detection pass itself is still a full recompute every time it runs.
- Phase 9's GPU load test, keyword-search checkpoint, end-to-end eval, and air-gap dry run are built as scripts (`scripts/gpu_load_test.py`, `eval_keyword_search.py`, `eval_end_to_end.py`) but have not yet been executed against live infrastructure, and Go/No-Go pass thresholds are not yet locked in.
- `.env.example` has not been kept in sync with `src/config.py` — several variables in the Configuration table above (the local-model and air-gap settings) aren't reflected in the template file yet.
- The agent harness (`src/pipeline/harness/`) is built and tested but not cut over to live traffic by default (`HARNESS_CUTOVER_ROUTES` empty) — its per-step trace granularity is intentionally coarser than the orchestrator's for whichever route is cut over, a known, accepted first-slice trade-off, not an oversight.
- `GROQ_MODEL` in `.env` needs to be a model your Groq account can currently reach — Groq's catalog rotates, and a model that worked when this was configured can silently stop resolving (`does not exist or you do not have access to it`) with no local warning until the cloud fallback is actually exercised. Confirm against a live `client.models.list()` call periodically, not just once at setup.

**Fixed since the initial audit:** `POST /api/auth/register` and `GET /api/auth/me` used to read/return an `is_admin` field that the Phase 7 `role`-enum migration had removed from the `users` model and from every user dict the data gateway returned, so both endpoints errored at runtime. Fixed by deriving `is_admin` (`role == "platform-admin"`) at the data-gateway boundary (`get_user_by_id`, `get_user_by_email`, `create_user`, `get_all_users` in `src/data_gateway/direct_backend.py`) — restoring the field for the existing frontend consumers that read it (`authStore.ts`, `AuthContext.tsx`, `UsersPage.tsx`) — and by adding the authoritative `role` field alongside it in the `/register`/`/me` response. Covered by new regression tests in `tests/test_api.py`.

**Confirmed live since the initial audit:** `GRAPH`/`XGRAPH`/`XAGG`/`XNETWORK` Cypher traversal, jurisdiction/officer graph writes, and the scale-prerequisite work (identity index, persistent full-text index, bounded-concurrency embeddings) have all been exercised against a real Postgres/AGE instance, not just the in-memory fake-graph unit suite — see `docs/decisions/0002-graph-schema-expansion-and-scale.md`'s per-milestone "live-verified" records.

---

## Design System

Both frontends share one token set (`frontend/src/index.css`, mirrored in `admin-frontend/src/index.css`). Rationale in [`docs/DESIGN.md`](docs/DESIGN.md).

- **Surfaces:** warm off-white (`#F4F2ED` canvas, `#FFFFFF` cards). Warm neutrals, not blue-grays.
- **Accent:** ink navy `#27477D` — the only saturated colour in the system, reserved for primary buttons, active nav/pipeline items, links, focus rings, and in-progress indicators. Roughly 95% of the UI is neutral; the accent means something because it is rare.
- **Dark mode:** warm charcoal; the accent lightens to `#7FA3DC` so it stays legible.
- **Type:** Inter. **Radii:** 6–20px. **Shadows:** soft and warm-tinted.
- **Motion:** phase icons animate the part that carries meaning (a search sweep, a pen stroke), not a generic spinner. All motion is disabled under `prefers-reduced-motion`.
- **Logo:** a four-point spark on a navy tile, with one squared corner suggesting a page. Icon, glyph, and horizontal lockup variants (light and dark) in `frontend/public/brand/`.

No component carries a raw hex value; a theme change is a change to the token file.

---

<div align="center">

Built to prioritize **accuracy over speed** and **transparency over black-box magic.**

</div>
