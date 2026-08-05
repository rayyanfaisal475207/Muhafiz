-- Migration 016: Community detection + summarization storage
-- (GraphRAG-inspired layer, additive on top of the Phase 4 evidence_graph —
-- see docs/AUDIT_FINDINGS_2026-08-04.md / FIX_PLAN_2026-08-04.md for the
-- verified graph state this builds on, and the Section 2 design plan this
-- migration implements).
--
-- Deliberately NOT stored as Apache AGE node properties. community_id is a
-- derived analytical snapshot recomputed from scratch by
-- src/graph/community_detection.py each run, not a document-sourced fact —
-- forcing it through versioning.write_node() would require fabricating a
-- fake source_doc_id and misrepresent what that field means for every other
-- node property write in the system. A plain Postgres side table lets a
-- full recompute cleanly replace the whole partition in one transaction
-- instead of issuing a Cypher SET per node.
--
-- Three tables:
--   community_membership — latest Louvain partition only (replaced wholesale
--     each run via TRUNCATE + INSERT in one transaction, not versioned/
--     append-only — a community assignment isn't a fact with provenance the
--     way graph edges are, it's a current snapshot of an algorithm's output).
--   community_runs — one row per detection run, for observability (real
--     community counts/sizes in the Stage 3 closeout report instead of
--     asserted ones).
--   community_reports — LLM-generated summaries per community, keyed to the
--     latest run's community_id values via FK.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, safe to re-run.

CREATE TABLE IF NOT EXISTS community_runs (
    run_id           TEXT PRIMARY KEY,
    computed_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    node_count       INT NOT NULL,
    edge_count       INT NOT NULL,
    community_count  INT NOT NULL,
    algorithm        TEXT NOT NULL DEFAULT 'louvain'
);

CREATE TABLE IF NOT EXISTS community_membership (
    entity_id     TEXT NOT NULL,       -- canonicalized Person entity_id (post SAME_AS collapse)
    community_id  TEXT NOT NULL,       -- e.g. "C-20260805-014"
    level         INT NOT NULL DEFAULT 0,  -- reserved for future hierarchy; 0 = flat Louvain partition
    run_id        TEXT NOT NULL REFERENCES community_runs (run_id) ON DELETE CASCADE,
    PRIMARY KEY (entity_id)
);
CREATE INDEX IF NOT EXISTS ix_community_membership_community_id ON community_membership (community_id);

-- No FK from community_reports to community_membership: both tables are
-- replaced together in the same TRUNCATE+INSERT transaction each run
-- (src/graph/community_detection.py), so a same-transaction FK would only
-- add ordering constraints without adding real safety. community_id values
-- are correlated by the application, not the database, across these two
-- tables — consistent with community_membership's own "latest snapshot,
-- not versioned history" model above.
CREATE TABLE IF NOT EXISTS community_reports (
    community_id       TEXT PRIMARY KEY,
    level               INT NOT NULL DEFAULT 0,
    run_id              TEXT NOT NULL REFERENCES community_runs (run_id) ON DELETE CASCADE,
    member_entity_ids   TEXT[] NOT NULL,
    case_ids            TEXT[] NOT NULL,
    member_count        INT NOT NULL,
    summary_text        TEXT NOT NULL,
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
