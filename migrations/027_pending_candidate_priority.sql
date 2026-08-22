-- ============================================================
-- Migration 027: Pending-candidate priority index (Graph Scale & Schema
-- Expansion, Milestone D1 — see GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md's
-- "Open points to resolve first", point 4, and
-- docs/decisions/0002-graph-schema-expansion-and-scale.md's Milestone D
-- section).
--
-- D1's own hot query needs the same treatment A1 gave identity lookups:
-- `graph_review.list_pending()`'s `WHERE r.status = 'pending'` runs over
-- AGE's edge storage with no property index behind it — a full label
-- scan under the hood, same confirmed diagnosis as migration 021's
-- header, now for SAME_AS/CITES edges instead of node identifiers.
--
-- Same design discipline as migration 021 (identity_index) / migration
-- 016 (community_membership): a plain Postgres side table shadowing
-- derived/lookup state alongside AGE, never a replacement for it, never
-- the sole source of truth (a row here always traces back to a real
-- pending edge in AGE, checked by edge_id). Apache AGE stays the graph
-- store (§5 of the plan, confirmed decision).
--
-- One row per PENDING SAME_AS/CITES edge (CROSS_VERSION_OF is written
-- directly, never pending — C4 — so it never appears here). The row is
-- inserted when the edge is first written with status='pending'
-- (src/graph/versioning.py's write_edge(), the single maintenance choke
-- point — same pattern identity_index.maintain() already uses from
-- write_node()) and DELETED the moment a human confirms/rejects it
-- (write_edge()'s supersedes_edge_id path) — so "every row in this
-- table" is definitionally "every currently-pending candidate", with no
-- separate status column to drift out of sync.
--
-- priority_score/why/group_id/deprioritized are owned exclusively by
-- src/graph/candidate_reprioritization.py's re-scoring job (Milestone
-- D1) — that job NEVER calls versioning.write_edge() and therefore
-- structurally cannot confirm/reject/create/supersede a graph edge; it
-- only ever UPDATEs these four columns on an existing row. This is what
-- keeps "D1 never auto-decides" a property of the code, not just a
-- convention: the reprioritization job has no code path that touches
-- SAME_AS/CITES status at all.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, safe to re-run.
--
-- HOW TO APPLY
--   python scripts/apply_migration.py migrations/027_pending_candidate_priority.sql

CREATE TABLE IF NOT EXISTS pending_candidate_priority (
    edge_id                       BIGINT PRIMARY KEY,
    edge_label                    TEXT NOT NULL,  -- 'SAME_AS' | 'CITES'
    tier                          TEXT,           -- SAME_AS only; NULL for CITES

    -- Generic "left"/"right" endpoint keys — entity_id for SAME_AS
    -- (mention -> candidate), case_id for CITES (citing -> cited case).
    -- Never a graph write target itself, just enough to re-fetch the
    -- live nodes at re-scoring time and to drive grouping (point 2).
    a_key                         TEXT NOT NULL,
    b_key                         TEXT NOT NULL,

    -- Snapshot of the ORIGINAL scoring signal at write time (Milestone
    -- D1's point 1: entity_resolution.Candidate's own fields, persisted
    -- structurally on the edge by entity_resolution.resolve_and_write —
    -- see that module's ResolutionDecision — and mirrored here so the
    -- re-scoring job can diff "then vs now" without re-parsing the
    -- free-text `basis` string).
    original_confidence           DOUBLE PRECISION,
    original_basis                TEXT,
    original_name_similarity      DOUBLE PRECISION,
    original_shared_case          BOOLEAN,
    original_shared_structured_id BOOLEAN,

    -- Owned by candidate_reprioritization.py only (see header above).
    priority_score                DOUBLE PRECISION NOT NULL DEFAULT 0,
    why                           TEXT,
    group_id                      TEXT,
    deprioritized                 BOOLEAN NOT NULL DEFAULT false,
    last_scored_at                TIMESTAMPTZ,

    created_at                    TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_doc_id                 TEXT
);

-- Backs the reordered queue read: "every pending candidate, most
-- actionable first" — exactly the query graph_review.py's new /queue
-- endpoint runs, replacing an AGE label scan with a Postgres index scan.
CREATE INDEX IF NOT EXISTS ix_pending_candidate_priority_queue
    ON pending_candidate_priority (edge_label, deprioritized, priority_score DESC);

-- Backs the "list every member of this batch" read for the grouped
-- batch-review endpoint.
CREATE INDEX IF NOT EXISTS ix_pending_candidate_priority_group
    ON pending_candidate_priority (group_id);
