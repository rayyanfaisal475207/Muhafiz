-- ============================================================
-- Migration 030: SAME_AS review-queue backlog snapshot history
-- (GRAPH_QUALITY_VISIBILITY_FIX_PROMPT.md, Feature A).
--
-- src/api/graph_review.py's GET /stats already computes a live tier x
-- status snapshot of every SAME_AS edge -- but it's read-time only,
-- nothing persists it. There is no way to answer "is the pending backlog
-- actually shrinking" without manually running that same query on two
-- different days and diffing the numbers by hand. This table is what a
-- periodic snapshot writer (src/graph/same_as_queue_history.py) inserts
-- into, so the SAME query's shape becomes a queryable time series instead
-- of a single point-in-time read.
--
-- One row per (snapshot_at, case_id, tier, status) tuple -- the same
-- nested tier -> status -> count shape /stats already returns, just
-- flattened and persisted, plus a case_id dimension /stats doesn't have
-- (it only ever reports the global cross-case rollup; per-case rows let
-- one case's cleanup -- e.g. fir-1001-26 -- be watched on its own).
-- case_id NULL is the global rollup row, same nullable convention
-- migration 028's ingestion_run_quality.case_id already uses for "a
-- multi-case bulk run has none".
--
-- Same "adds visibility, never a new judgment call" discipline
-- ingestion_quality.py's own header states for Module G1 -- this table
-- is never written to by anything that also touches a SAME_AS edge.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, safe to re-run.
--
-- HOW TO APPLY
--   python scripts/apply_migration.py migrations/030_same_as_queue_snapshot.sql

CREATE TABLE IF NOT EXISTS same_as_queue_snapshot (
    id              BIGSERIAL PRIMARY KEY,
    snapshot_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    case_id         TEXT,            -- NULL = global (cross-case) rollup row
    tier            TEXT NOT NULL,   -- entity_resolution.py's own tier constants
    status          TEXT NOT NULL,   -- 'pending' | 'confirmed' | 'rejected'
    edge_count      INTEGER NOT NULL DEFAULT 0
);

-- Backs "history for one case, most recent first" and "global history,
-- most recent first" -- the two read shapes GET /queue/history serves.
CREATE INDEX IF NOT EXISTS ix_same_as_queue_snapshot_case
    ON same_as_queue_snapshot (case_id, snapshot_at DESC);

CREATE INDEX IF NOT EXISTS ix_same_as_queue_snapshot_time
    ON same_as_queue_snapshot (snapshot_at DESC);
