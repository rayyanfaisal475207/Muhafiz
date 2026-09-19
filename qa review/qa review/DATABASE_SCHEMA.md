# DATABASE_SCHEMA.md

> Sources: [src/database/models.py](../src/database/models.py) (SQLAlchemy 2.0 ORM),
> [migrations/](../migrations/) (plain SQL, applied via `scripts/apply_migration.py`),
> [alembic/versions/](../alembic/versions/) (baseline chain),
> [docs/graph_schema.md](../docs/graph_schema.md) (Apache AGE graph).

There are **three** persistence layers:

| Layer | Technology | Location |
|---|---|---|
| Relational | PostgreSQL | `DATABASE_URL` |
| Property graph | Apache AGE **inside the same Postgres instance** | graphs `evidence_graph`, `evidence_graph_eval` |
| Vectors | ChromaDB | local directory `CHROMA_PERSIST_DIR` |

Plus a **legacy SQLite side-log** (`data/pipeline_logs.db`,
[src/database/db.py](../src/database/db.py)) which is write-only from the app's
perspective — no case, user, or RBAC data is read from it.

> **Schema-source caveat for QA:** some columns exist only in the ORM
> (`Base.metadata.create_all()` runs at startup) and some only in the plain-SQL
> migrations. Where the two differ, both are noted below.

---

## 1. Core identity and access

### `users`
ORM: `User` · Migrations: baseline + 006 (role) + 012 (police_station)

| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | `gen_random_uuid()` default |
| `email` | Text | **UNIQUE, NOT NULL** |
| `password_hash` | Text | NOT NULL — bcrypt |
| `role` | `user_role` enum | NOT NULL, default `'investigator'` |
| `police_station` | Text | Nullable; added by migration 012 **with no backfill** |
| `company_name` | Text | Nullable |
| `plan` | Text | Default `'free'` |
| `created_at` | Timestamp | `now()` |

- Enum `user_role`: `'investigator' | 'supervisor' | 'station-admin' | 'platform-admin'`.
- `is_admin` was **dropped** by migration 006 after backfilling `role`. The API
  still exposes an `is_admin` field, derived as `role == 'platform-admin'`.
- **Relationships:** 1:1 `user_context_profiles`; 1:N `sessions`, `projects`,
  `pipeline_runs`, `documents`, `generated_files`, `case_assignments`.

### `user_context_profiles`
| Column | Type | Notes |
|---|---|---|
| `user_id` | UUID **PK and FK → users.id** | 1:1 with users |
| `context_text` | Text | Default `''`; API caps input at 1000 chars |
| `preferred_language` | Text | Default `'auto'` — **no DB or API allow-list** |
| `llm_mode` | Text | Default `'cloud'` — **no DB or API allow-list** |
| `updated_at` | Timestamp | |

### `case_assignments` (the ABAC anchor)
Migration 006.

| Column | Type | Notes |
|---|---|---|
| `assignment_id` | UUID PK | |
| `case_id` | Text | **FK → cases.case_id ON DELETE CASCADE**, NOT NULL |
| `user_id` | UUID | **FK → users.id ON DELETE CASCADE**, NOT NULL |
| `role` | `user_role` | NOT NULL, default `'investigator'` — this is the **per-case** role |
| `created_at` | Timestamp | |

**Constraint:** `UNIQUE (case_id, user_id)` — one assignment row per user per
case. `assign_user_to_case()` updates the existing row's role rather than
inserting a duplicate.

### `audit_logs`
Migration 006.

| Column | Type | Notes |
|---|---|---|
| `log_id` | UUID PK | |
| `timestamp` | Timestamp | `CURRENT_TIMESTAMP` |
| `event_type` | Text | NOT NULL |
| `user_id` | UUID | FK → users.id **ON DELETE SET NULL** |
| `case_id` | Text | FK → cases.case_id **ON DELETE SET NULL** |
| `details` | JSONB | NOT NULL, default `'{}'` |

`ON DELETE SET NULL` (rather than CASCADE) preserves the audit record when the
referenced user or case is deleted.

Observed `event_type` values in code: `admin_action`,
`graph_review_confirm`, `graph_review_reject`,
`graph_review_citation_confirmed`, `graph_review_citation_rejected`,
`graph_review_consistency_finding_acknowledge`, `ingestion_quality_acknowledge`,
`graph_traversal_cross_case`, `authorization_violation`.

---

## 2. Cases, projects, sessions, messages

### `cases`
Migration 004 + 018 (`conflicts_checked_at`).

| Column | Type | Notes |
|---|---|---|
| `case_id` | **Text PK** — not a UUID | Client-suppliable; validated against `^[A-Za-z0-9._-]+$`; auto-generated as `CASE-<8 hex>` |
| `fir_number`, `crime_category`, `investigation_officer`, `police_station`, `investigation_status`, `location`, `description` | Text | All nullable |
| `incident_date` | Date | Nullable |
| `victim_info`, `suspect_info` | JSONB | Nullable |
| `conflicts_checked_at` | Timestamp | Nullable — distinguishes "detection ran and found nothing" from "never ran" |
| `created_at`, `updated_at` | Timestamp | `updated_at` has `onupdate=func.now()` in the ORM |

**Indexes:** `idx_cases_fir_number`, `idx_cases_status (investigation_status)`.
**RLS:** enabled + forced (migrations 008/010).
**Ownership:** there is **no owner column**. Ownership is expressed entirely
through `case_assignments`.

> **ORM anomaly for QA:** `models.py` declares `conflicts_checked_at` **twice**
> within the `Case` class (lines ~189 and ~207). Python keeps the last binding, so
> this is harmless at runtime but is a latent maintenance hazard.

### `projects`
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `user_id` | UUID | **FK → users.id** — this table *is* owner-scoped |
| `name` | Text | NOT NULL |
| `description`, `domain_context` | Text | Nullable |
| `created_at`, `updated_at` | Timestamp | |

**RLS:** none. `projects` is not covered by migration 008/010, which
[src/api/projects.py](../src/api/projects.py) documents as a deliberate no-op.

### `project_memory`
| Column | Type | Notes |
|---|---|---|
| `id` | UUID PK | |
| `project_id` | UUID | FK → projects.id |
| `summary_text` | Text | NOT NULL |
| `last_updated` | Timestamp | |

### `sessions`
| Column | Type | Notes |
|---|---|---|
| `session_id` | UUID PK | **Generated client-side** by the chat app before the first message |
| `user_id` | UUID | FK → users.id, **nullable** (Phase-1 legacy) |
| `project_id` | UUID | FK → projects.id, nullable |
| `case_id` | Text | FK → cases.case_id, nullable |
| `title` | Text | Default `'New Chat'` |
| `created_at`, `updated_at` | Timestamp | |
| `deleted_at` | Timestamp | Nullable — **soft delete** |

**Indexes:** `idx_sessions_case_id` (created twice — migrations 004 and 010 —
specifically to keep the `messages`/`pipeline_runs` RLS sub-query off a sequential scan).
**RLS:** enabled + forced.
**Ownership:** `user_id`. Every session route compares it to `current_user.id`.

> `user_id` is nullable at the schema level, but the application never creates an
> ownerless session — `chat_endpoint` creates the row *with* its owner up front,
> precisely because ownerless rows could not be listed or opened.

### `messages`
| Column | Type | Notes |
|---|---|---|
| `message_id` | UUID PK | |
| `session_id` | UUID | FK → sessions.session_id, NOT NULL |
| `role` | Text | NOT NULL |
| `content` | Text | NOT NULL |
| `source_type` | Text | Nullable |
| `confidence_score` | Integer | Nullable |
| `citation_validated` | Boolean | Nullable |
| `unverified_citations` | `ARRAY(Text)` | Nullable |
| `degradation_trace` | JSONB | Nullable — migration 019 |
| `created_at` | Timestamp | |

**Constraint:** `CheckConstraint("role IN ('user', 'assistant')", name="ck_messages_role")`.
**RLS:** enabled + forced by migration 010 (008 protected `sessions` but not this
child table holding the actual chat content).

---

## 3. Documents and knowledge base

### `documents`
| Column | Type | Notes |
|---|---|---|
| `doc_id` | **Text PK** | Hash-derived, from `Document._generate_id()` |
| `user_id` | UUID | FK → users.id, nullable |
| `project_id` | UUID | FK → projects.id, nullable |
| `case_id` | Text | FK → cases.case_id, nullable (migration 004) |
| `filename` | Text | NOT NULL |
| `doc_type`, `document_date` | Text | Nullable |
| `chunk_count` | Integer | Nullable |
| `ingested_at` | Timestamp | |
| `effective_from`, `effective_to` | Date | Nullable — temporal validity, checked by the verifier |
| `is_global` | Boolean | Default `false` |

**Index:** `idx_documents_case_id`.
**RLS:** enabled + forced. Its policy additionally passes when `is_global = true`,
so shared knowledge-base documents are visible regardless of case scope.

### `chunk_fulltext` (migration 022)
The persistent BM25 index; replaces rebuilding an in-memory BM25 index per query.

| Column | Type | Notes |
|---|---|---|
| `chunk_id` | Text PK | |
| `doc_id` | Text | NOT NULL |
| `source`, `project_id`, `case_id` | Text | Denormalised, indexed subset of `metadata` |
| `is_global` | Boolean | NOT NULL default `false` |
| `text` | Text | NOT NULL |
| `metadata` | JSONB | NOT NULL default `'{}'` — the **complete** Chroma metadata dict verbatim |
| `tsv` | TSVECTOR | NOT NULL |
| `updated_at` | Timestamptz | |

**Indexes:** `ix_chunk_fulltext_tsv` **GIN** on `tsv`; btree on `project_id`,
`case_id`, `source`.
**Consistency requirement:** must be kept in sync with Chroma on both ingest
(`vector_store.upsert_documents()`) and delete
(`DELETE /api/admin/kb/documents/{source_file}` calls
`fulltext_index.delete_by_source()`). Drift between the two is a QA risk area.

### `police_reference_data`
| Column | Type | Notes |
|---|---|---|
| `ref_id` | UUID PK | |
| `category` | Text | NOT NULL |
| `subject` | Text | NOT NULL |
| `description` | Text | Nullable |
| `fine_amount` | Numeric | Nullable |
| `section_ref`, `source_document` | Text | Nullable |
| `source_type` | Text | NOT NULL |
| `effective_from` | Date | Nullable |
| `created_at` | Timestamp | |

Carries a `CheckConstraint` (see [models.py](../src/database/models.py) line ~461).
This is the **only** table the least-privilege `muhafiz_mcp_readonly` role may
`SELECT` (migration 009), and the only table the SQL route and MCP demo query.
Seeded by `scripts/seed_police_reference_data.py`.

---

## 4. Pipeline observability

### `pipeline_runs`
| Column | Type | Notes |
|---|---|---|
| `run_id` | UUID PK | |
| `session_id` | UUID | FK → sessions.session_id, NOT NULL |
| `user_id` | UUID | FK → users.id, nullable |
| `original_query`, `rewritten_query`, `routed_to`, `final_outcome` | Text | Nullable |
| `retry_count` | Integer | Default 0 |
| `total_duration_ms` | Integer | Nullable |
| `verifier_passed`, `verifier_regenerated` | Boolean | Nullable — migration 007 |
| `created_at` | Timestamp | |

**Index:** `ix_pipeline_runs_created_at` (also created by migration 014).
**RLS:** enabled + forced; the policy joins through `sessions` on `session_id`.

### `pipeline_steps`
| Column | Type | Notes |
|---|---|---|
| `step_id` | Integer PK (identity) | |
| `run_id` | UUID | FK → pipeline_runs.run_id, NOT NULL |
| `step_name` | Text | NOT NULL |
| `step_order` | Integer | NOT NULL |
| `status` | Text | NOT NULL, under a `CheckConstraint` |
| `duration_ms` | Integer | Nullable |
| `input_summary`, `output_summary` | JSONB | Nullable |
| `created_at` | Timestamp | |

**Index:** `ix_pipeline_steps_run_id_created_at (run_id, created_at)` (also migration 014).

### `mcp_tool_calls`
| Column | Type | Notes |
|---|---|---|
| `call_id` | UUID PK | |
| `run_id` | UUID | **FK → pipeline_runs.run_id**, NOT NULL |
| `mcp_server`, `tool_name` | Text | NOT NULL |
| `input_params`, `output_summary` | JSONB | Nullable |
| `status` | Text | NOT NULL, `CheckConstraint` |
| `duration_ms` | Integer | Nullable |
| `rejected_by_role` | Boolean | Default `false` |
| `created_at` | Timestamp | |

Because `run_id` is a real FK to `pipeline_runs` (which itself FKs `sessions`),
the `/api/admin/mcp-demo` endpoint must create both a session and a run before it
can log its call.

### `error_logs` (migration 003)
| Column | Type | Notes |
|---|---|---|
| `error_id` | UUID PK | |
| `occurred_at` | Timestamptz | NOT NULL `now()` |
| `severity` | Text | NOT NULL default `'error'` — `warning \| error \| critical` |
| `error_type`, `module` | Text | Nullable |
| `message` | Text | NOT NULL |
| `stack_trace` | Text | Nullable |
| `run_id`, `session_id`, `user_id` | UUID | Nullable, **not FKs** (deliberately loose — an error must be recordable even if the referenced row never committed) |
| `context` | JSONB | Nullable |

**Indexes:** `idx_error_logs_occurred_at (occurred_at DESC)`,
`idx_error_logs_severity`, `idx_error_logs_module`, plus
`ix_error_logs_occurred_at` (migration 014).

### `ingestion_jobs` (migration 003)
| Column | Type | Notes |
|---|---|---|
| `job_id` | UUID PK | |
| `doc_id` | Text | Nullable, set once the document row exists |
| `filename` | Text | NOT NULL |
| `file_type` | Text, `file_size_bytes` bigint | Nullable |
| `status` | Text | NOT NULL default `'processing'` — `processing \| success \| failed` |
| `chunks_added` | Integer | Default 0 |
| `error_message` | Text | Nullable |
| `uploaded_by` | UUID | Nullable, **not an FK** |
| `started_at` | Timestamptz | NOT NULL `now()` |
| `finished_at`, `duration_ms` | | Nullable |

**Indexes:** `idx_ingestion_jobs_started_at (started_at DESC)`, `idx_ingestion_jobs_status`.
**Note:** this table has **no `case_id`** — the gap migration 028's
`ingestion_run_quality` exists to close.

### `session_attachments` (migration 003)
| Column | Type | Notes |
|---|---|---|
| `attachment_id` | UUID PK | |
| `session_id` | UUID | NOT NULL, **not an FK** in the SQL migration |
| `user_id` | UUID | Nullable — the ownership key the API checks |
| `filename` | Text | NOT NULL |
| `file_type`, `file_size_bytes` | | Nullable |
| `extracted_text` | Text | Nullable — truncated to 12 000 chars by the API |
| `char_count` | Integer | Untruncated length |
| `status` | Text | NOT NULL default `'ready'` — `ready \| failed` |
| `error_message` | Text | Nullable |
| `created_at` | Timestamptz | |

**Index:** `idx_session_attachments_session (session_id)`.
**RLS:** none — protected by the application ownership check only.

### `generated_files`
| Column | Type | Notes |
|---|---|---|
| `file_id` | UUID PK | |
| `session_id` | UUID | FK → sessions.session_id, NOT NULL |
| `user_id` | UUID | FK → users.id, **NOT NULL** — the primary ownership key |
| `message_id` | UUID | FK → messages.message_id, nullable |
| `case_id` | Text | FK → cases.case_id **ON DELETE SET NULL** — migration 013, **no backfill** |
| `file_type`, `file_name`, `storage_path` | Text | NOT NULL |
| `file_size_bytes` | Integer | Nullable |
| `created_at` | Timestamp | |
| `expires_at` | Timestamp | Nullable — **no expiry-enforcement code exists** in the repo |

Because migration 013 did not backfill `case_id`, pre-migration rows keep the
older blanket station-admin/platform-admin download access — an explicit,
documented limitation in [src/main.py](../src/main.py).

---

## 5. Graph-support side tables

These back graph operations with real Postgres indexes instead of unindexed AGE
label scans.

### `identity_index` (migration 021)
`(label, id_key, id_value)` **composite PK** → `entity_id`, plus `updated_at`.
**Index:** `ix_identity_index_label_key_entity`.
Backs entity-resolution hard-block lookups (CNIC, plate, phone, `belt_no`) in O(1).

### `pending_candidate_priority` (migration 027)
`edge_id BIGINT PK` (the AGE edge id), `edge_label` (`SAME_AS`|`CITES`), `tier`,
`a_key`/`b_key` (endpoint keys — `entity_id` for SAME_AS, `case_id` for CITES),
a snapshot of the original scoring signal (`original_confidence`,
`original_basis`, `original_name_similarity`, `original_shared_case`,
`original_shared_structured_id`), and reprioritization-owned fields
(`priority_score`, `why`, `group_id`, `deprioritized`, `last_scored_at`),
`created_at`, `source_doc_id`.
**Indexes:** `(edge_label, deprioritized, priority_score DESC)`, `(group_id)`.
`priority_score` / `why` / `group_id` / `deprioritized` are owned **exclusively**
by `candidate_reprioritization.py`.

### `entity_resolution_consistency_findings` (migration 029)
`finding_id SERIAL PK`, `edge_id BIGINT`, `tier`, `status_at_detection`
(`pending`|`confirmed`), `mention_entity_id`, `candidate_entity_id`, the
original vs. `fresh_*` scoring triple, `finding_reason`, `detected_at`,
`acknowledged`, `acknowledged_by`, `acknowledged_at`.
**Index:** `(acknowledged, detected_at DESC)` — backs the "open findings" read.

### `ingestion_run_quality` (migration 028)
`run_id TEXT PK`, `source` (`ingest_file`|`sync_muhafiz_data`), nullable
`case_id`, `started_at`/`finished_at`, the four tier counters
(`tier_cnic_auto`, `tier_flagged_unverified`, `tier_human_review`, `tier_new`),
`corroboration_gate_rejections`, `extraction_errors`, `flagged_for_review`,
`flagged_reason`.
**Indexes:** `(source, started_at DESC)`, `(case_id)`.

### `same_as_queue_snapshot` (migration 030)
`id BIGSERIAL PK`, `snapshot_at`, nullable `case_id` (**NULL = global rollup**),
`tier`, `status`, `edge_count`.
**Indexes:** `(case_id, snapshot_at DESC)`, `(snapshot_at DESC)`.
Written **only** by `scripts/snapshot_same_as_queue.py` — there is no automatic
writer, so an empty `GET /queue/history` usually means the script never ran.

### Community detection (migration 016 + 017)
- `community_runs` — `run_id TEXT PK`, `computed_at`, `node_count`, `edge_count`,
  `community_count`, `algorithm` (default `'louvain'`), plus `raw_node_count` /
  `raw_edge_count` (017, pre-filter baselines).
- `community_membership` — `entity_id TEXT PK` (one community per entity),
  `community_id`, `level` (default 0), `run_id` **FK → community_runs ON DELETE CASCADE**.
  **Index:** `ix_community_membership_community_id`.
- `community_reports` — `community_id TEXT PK`, `level`, `run_id` FK CASCADE,
  `member_entity_ids TEXT[]`, `case_ids TEXT[]`, `member_count`, `summary_text`,
  `updated_at`.

---

## 6. Row-Level Security summary

| Table | RLS | Policy predicate |
|---|---|---|
| `cases` | ENABLE + FORCE | inactive OR cross_case OR (NULL & `''`) OR `case_id = app.case_id` |
| `documents` | ENABLE + FORCE | as above, **plus `is_global = true`** |
| `sessions` | ENABLE + FORCE | as `cases` |
| `pipeline_runs` | ENABLE + FORCE | `session_id IN (SELECT ... FROM sessions WHERE <case predicate>)` |
| `messages` | ENABLE + FORCE (migration 010) | same join-through-sessions shape |
| Everything else | none | Application-layer checks only |

All are `FOR ALL` policies with **no separate `WITH CHECK`**, so `USING` governs
`INSERT` too. `FORCE` means the table owner is also subject to them — but a
Postgres **superuser** or a `BYPASSRLS` role still bypasses everything
([RUN.md](../RUN.md) §4; migration 015 provides the `muhafiz_app` least-privilege role,
which requires manual password + `DATABASE_URL` follow-up).

---

## 7. Database roles

| Role | Migration | Grants |
|---|---|---|
| `muhafiz_mcp_readonly` | 009 | `CONNECT` on the database, `USAGE` on `public`, `SELECT` on **`police_reference_data` only** |
| `muhafiz_app` | 015 | `CONNECT`, `USAGE` + **`CREATE` on `public`** (needed for the startup `create_all()`), `SELECT/INSERT/UPDATE/DELETE` on the application tables, `USAGE, SELECT` on all sequences |

Both are created `WITH LOGIN` and **no password**; setting a password and
repointing the corresponding connection string is a manual per-deployment step.

---

## 8. Apache AGE graph (`evidence_graph`)

Full detail in [docs/graph_schema.md](../docs/graph_schema.md). AGE enforces no schema —
labels and properties are created lazily on first write, so
*"this document plus app-layer validation in `src/graph/*.py` is the enforcement."*

### Node labels
`Case`, `Person`, `Vehicle`, `PhoneNumber`, `Address`, `Organization`,
`Weapon`, `Incident`, `Document`, `StructuredRecord`, `Date`, `PoliceStation`,
`District`, `Officer`.

### Edge labels
`BELONGS_TO_CASE`, `APPEARS_IN`, `ASSOCIATED_WITH`, `SAME_AS`, `OWNS`,
`REGISTERED_TO` (declared, still unwritten), `LOCATED_AT`, `INVOLVED_IN`,
`PART_OF`, `FILED_AT`, `ASSIGNED_TO`, `RELATED_TO`, `CROSS_VERSION_OF`,
`OCCURRED_ON`, `CONFLICTS_WITH`, `CITES`.

### Structural invariants
- **Every** node except `Case` gets a `BELONGS_TO_CASE` edge at write time,
  making within-case traversal a single filtered hop.
- **No edge exists without provenance** — every edge carries `confidence` (or is
  definitionally 1.0), `source_doc_id`, and `source_chunk_id` where the extraction
  traces to a specific chunk.
- **Append-only.** `versioning.write_edge()` never mutates in place; a superseding
  fact sets `superseded_by = <new edge id>` on the prior edge. Current state =
  `WHERE superseded_by IS NULL`; history = follow the chain.
- **`OCCURRED_ON` locking.** `locked = true` blocks any writer (including a
  re-extraction) from superseding the edge until `unlock_event()` is called.
- **Resolution never physically merges nodes.** CNIC exact match attaches edges
  to the existing node directly (no `SAME_AS`); name-fallback always mints a new
  node and links it with a `pending` `SAME_AS` edge for human review.
- **Labels are pre-created in migrations** (005, 011, 020, 023, 024, 025, 026) to
  avoid a duplicate-key race when two concurrent transactions first write the
  same brand-new label.
- **`scoped_cypher()`** ([src/graph/case_scope.py](../src/graph/case_scope.py)) raises
  `ValueError` for any within-case template that does not literally reference
  `$case_id`. Compliance test 5 enforces that no harness tool bypasses it.

### `SAME_AS` tiers ([src/graph/entity_resolution.py](../src/graph/entity_resolution.py))

| Tier constant | Value | Meaning |
|---|---|---|
| `TIER_CNIC_AUTO` | `cnic_auto` | Exact structured identifier — auto-merge, **never appears in the review queue** |
| `TIER_FLAGGED` | `flagged_unverified` | Scored match, needs review |
| `TIER_REVIEW` | `human_review` | LLM-adjudicated, needs review |
| `TIER_NEW` | `new` | No candidate — a fresh node |

`status` ∈ `pending | confirmed | rejected`.

---

## 9. ChromaDB (vector store)

- Collection: `CHROMA_COLLECTION_NAME` (default `muhafiz_kb`), persisted at
  `CHROMA_PERSIST_DIR` (default `data/chroma_db`).
- **One embedding dimension per collection.** `EXPECTED_EMBEDDING_DIM` is derived
  from `EMBEDDING_PROVIDER` (`e5` 1024 · `gemini` 3072 · `openai` 1536 ·
  `local` 384) and `upsert()` rejects a wrong-dimension write **before** it
  reaches Chroma — because Chroma only raises a dimension-mismatch once the
  collection is non-empty, so a freshly cleared collection would otherwise
  silently adopt whatever dimension arrived first.
- Changing providers requires dropping the collection and re-ingesting
  (`scripts/reingest_kb.py`).
- Chunk metadata mirrors `chunk_fulltext.metadata`: `source`, `doc_id`,
  `project_id`, `case_id`, `is_global`, `doc_type`, `page`, `record_date`,
  `fir_display_code`, and others.

---

## 10. Legacy SQLite (`data/pipeline_logs.db`)

Created by `src/database/db.py::init_db()`. Holds an LLM-call/step side-log
written by `pipeline_logger.py`. It runs **unconditionally**, in both Postgres and
legacy mode — the [src/main.py](../src/main.py) comment notes this is deliberately not the
"legacy SQLite mode" the other branch warns about, because nothing reads
case/user/RBAC data from it. Its schema predates the entire case/auth/RBAC model.
