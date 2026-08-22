# Database Design

This document details the PostgreSQL (local/self-hosted) database schema
powering the platform. It covers relational data only — vector embeddings
live in ChromaDB (`data/chroma_db`), and the entity/relationship graph lives
in Apache AGE on this same Postgres instance (see `docs/graph_schema.md` for
that half; not duplicated here).

**M12 of the Muhafiz Data API migration** (`docs/decisions/0001-muhafiz-api-migration.md`)
rewrote this document from scratch — it previously listed 5 tables and
predated `cases`, `case_assignments`, `audit_logs`, the community-detection
tables, and RLS entirely. `docs/schema-snapshot.json` (the "machine-generated
dump this document is checked against") is **also stale** in the same way —
missing the same tables — and nothing in this migration regenerates it (no
live Postgres instance was available this session); treat it as informational
only until it's regenerated against a live database, not as the source of
truth `src/database/models.py` is.

## Schema Overview

### Mermaid ERD (core relational tables only — see prose below for the rest)

```mermaid
erDiagram
    users ||--o{ sessions : "creates"
    users ||--o{ case_assignments : "is assigned"
    sessions ||--o{ messages : "contains"
    sessions ||--o{ generated_files : "owns"
    cases ||--o{ case_assignments : "has"
    cases ||--o{ documents : "owns evidence"
    cases ||--o{ sessions : "scopes"

    users {
        uuid id PK
        string email
        string role
    }

    cases {
        string case_id PK
        string fir_number
        string police_station
        date incident_date
        string investigation_status
        timestamptz conflicts_checked_at
    }

    case_assignments {
        uuid assignment_id PK
        string case_id FK
        uuid user_id FK
        string role
    }

    sessions {
        uuid session_id PK
        uuid user_id FK
        string case_id FK
        string title
        timestamp created_at
        timestamp updated_at
        timestamp deleted_at
    }

    messages {
        uuid message_id PK
        uuid session_id FK
        string role
        text content
        timestamp created_at
    }

    documents {
        string doc_id PK
        string case_id FK
        string filename
        string doc_type
        int chunk_count
        bool is_global
    }

    generated_files {
        uuid file_id PK
        uuid session_id FK
        uuid user_id FK
        string file_name
        string storage_path
        string file_type
        timestamp created_at
    }

    police_reference_data {
        uuid ref_id PK
        string category
        string subject
        text description
        numeric fine_amount
        string section_ref
        string source_type
    }
```

## Table definitions

### Identity & access

**`users`** — `id` (UUID PK), `email`, `password_hash`, `role`, `company_name`,
`plan`, `police_station`. Authenticated via JWT in an HttpOnly cookie (see
`src/auth/`).

**`user_context_profiles`** — one row per user, free-text conversational
context/preferences carried across sessions.

**`case_assignments`** — `assignment_id` (UUID PK), `case_id` FK → `cases`
(CASCADE), `user_id` FK → `users` (CASCADE), `role` (default
`investigator`). Not a data-ownership table — it's what
`DirectGateway.get_cases()` INNER JOINs against for any non-platform-admin
caller. **A case with no assignment row is invisible in the UI even though
the case, its documents, and its evidence all exist** — every case-provisioning
script in this migration (`scripts/sync_muhafiz_cases.py`) supports an
opt-in `--assign-to` for exactly this reason.

**`audit_logs`** — `log_id` (UUID PK), `timestamp`, `event_type`, `user_id`
FK → `users` (SET NULL), `case_id` FK → `cases` (SET NULL), `details`
(JSONB). Every graph write (`src/graph/versioning.py`) and every cross-case
graph traversal logs here.

### Cases & evidence

**`cases`** — `case_id` (Text PK, human-meaningful, e.g. `"fir-891-24"` under
this migration's "Case = FIR" decision — not a generated UUID), `fir_number`,
`crime_category`, `investigation_officer`, `police_station`, `incident_date`,
`investigation_status`, `location`, `description`, `victim_info`/`suspect_info`
(JSONB), `conflicts_checked_at` (when conflict detection last *completed* for
this case — NULL means "not known to have been checked," never "checked,
clean"; see `docs/graph_schema.md`'s `OCCURRED_ON` row and
`migrations/018_case_conflicts_checked_at.sql`). Provisioned from real FIRs by
`scripts/sync_muhafiz_cases.py`/`src/ingestion/muhafiz_cases.py` (M4 of this
migration).

**`documents`** — `doc_id` (Text PK, deterministic — see
`src/ingestion/document.py`), `user_id`, `project_id`, `case_id` FK → `cases`
(SET NULL, nullable — evidence ingested before the Case model existed, and
some shared knowledge-base uploads, have no case), `filename`, `doc_type`,
`chunk_count`, `ingested_at`, `is_global`. The relational record of a chunk
family stored in Chroma — chunk text/embeddings/retrieval metadata live there,
not here.

**`ingestion_jobs`** — per-file admin-upload progress tracking
(`POST /api/admin/kb/upload`). **Confirmed gap, not fixed by this
migration:** this table has no `case_id` column at all, and the one live
path that writes it always ingests `is_global=True` — case-scoped ingestion
(offline scripts, and `scripts/sync_muhafiz_data.py`, M9) never creates a
row here. `src/pipeline/harness/agents/data_quality.py` reports this
honestly via a caveat rather than fabricating a case-scoped count.

**`session_attachments`** — one-off chat-composer file uploads. Text
extracted once, injected into that single conversation's prompt, **never
embedded, never reaches Chroma or the graph** — structurally separate from
the knowledge-base ingestion path (`docs/INGESTION.md`).

### Reference data

**`police_reference_data`** — `ref_id` (UUID PK), `category` (`penal_code` is
the only category with real backing), `subject`, `description`, `fine_amount`,
`section_ref`, `source_document`, `source_type` (`scraped` | `synthetic`).
Two independent sources, both additive, neither replacing the other:
`scripts/seed_police_reference_data.py` (6 hand-curated rows with
descriptive text) and `scripts/load_real_offense_sections.py` (M7 of this
migration — the 36 distinct real section/act pairs measured across the live
FIR dataset, `description` left NULL since the API supplies no offense-text
per section).

### Conversation & pipeline

**`sessions`** — `session_id` (UUID PK), `user_id`, `case_id`, `title`,
`created_at`/`updated_at`/`deleted_at` (soft-delete).

**`messages`** — `message_id` (UUID PK), `session_id`, `role`
(`user`/`assistant`), `content`, `created_at`.

**`generated_files`** — exported PDF/DOCX/XLSX artifacts from a chat
session. `file_id` (UUID PK), `session_id`, `user_id` (access control on
download), `storage_path`.

**`project_memory`** / **`projects`** — project-scoped conversational memory
and project metadata, independent of the case model.

**`pipeline_runs`** / **`pipeline_steps`** — per-query orchestrator
observability (route taken, retry count, latency per stage).

**`mcp_tool_calls`** — audit trail of MCP tool invocations (the SQL route's
read-only Postgres role, `migrations/009_mcp_readonly_role.sql`).

**`error_logs`** — unhandled exception capture for the admin dashboard.

### Community detection (`migrations/016_community_detection.sql`, `017`)

**`community_runs`** — one row per Louvain community-detection run
(`run_id` PK, `computed_at`, `node_count`/`edge_count`/`community_count`,
`raw_node_count`/`raw_edge_count`). Every run **replaces** the prior one
(`DELETE FROM community_runs` before insert) — this is a "latest snapshot,"
never a versioned history, unlike the graph's own append-only discipline.

**`community_membership`** — `entity_id` (PK, canonicalized Person entity_id
post-`SAME_AS` collapse) → `community_id`, FK → `community_runs` (CASCADE).

**`community_reports`** — `community_id` (PK) → `run_id` FK (CASCADE),
`member_entity_ids`/`case_ids` (arrays), `summary_text`. No FK to
`community_membership` — both tables are replaced together in the same
detection run, so a same-transaction FK would only add an ordering
constraint without adding real safety.

### Pending-candidate priority (`migrations/027_pending_candidate_priority.sql`)

Graph Scale & Schema Expansion, Milestone D1 (see
`docs/decisions/0002-graph-schema-expansion-and-scale.md`) — a Postgres
side table shadowing every currently-PENDING `SAME_AS`/`CITES` edge in
AGE, same design discipline as `community_membership` above and A1's
`identity_index`: never the source of truth, always traceable back to a
real edge in AGE by `edge_id`.

**`pending_candidate_priority`** — `edge_id` (PK, the AGE edge's own
internal id), `edge_label` (`SAME_AS`/`CITES`), `tier`, `a_key`/`b_key`
(entity_id or case_id, whichever the edge connects), the write-time
scoring snapshot (`original_confidence`/`original_basis`/
`original_name_similarity`/`original_shared_case`/
`original_shared_structured_id`), and the re-scoring-owned columns
`priority_score`/`why`/`group_id`/`deprioritized`/`last_scored_at`.

Maintained from one choke point — `src/graph/versioning.py`'s
`write_edge()` — mirroring exactly how `identity_index` is maintained
from `write_node()`: a row is inserted the moment a pending edge is
written, and deleted the moment a human confirms/rejects it (never
mutated to reflect a status change — status changes are what deletes the
row). The re-scoring columns are written exclusively by
`src/graph/candidate_reprioritization.py`, which never calls
`write_edge()` — structurally incapable of confirming/rejecting a match.

## Row-Level Security

`migrations/008_rls_policies.sql`/`010` arm RLS on `documents`/`sessions`/
`cases`/`messages`, gated through `src/database/postgres.py`'s
`current_rls_active`/`current_cross_case` session variables. Apache AGE has
**no native RLS equivalent** — the graph's own case-isolation story
(`src/graph/case_scope.py`) is a structural chokepoint, not a database-level
guarantee; see `docs/graph_schema.md`'s "Case isolation for the graph" section
for the full, honestly-stated distinction.
