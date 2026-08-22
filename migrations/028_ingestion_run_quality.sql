-- ============================================================
-- Migration 028: Ingestion run quality rollup (Ingestion Quality Control
-- at Scale, Module G1 — see INGESTION_QUALITY_AT_SCALE_PLAN.md).
--
-- Closes a real, already-documented gap:
-- src/pipeline/harness/agents/data_quality.py's own module docstring
-- confirms `ingestion_jobs` (migration 003) has NO case_id column, and
-- the bulk case-scoped ingestion paths (ingest_file(case_id=...),
-- scripts/sync_muhafiz_data.py) never write a row there at all --
-- confirmed again while writing this migration, not assumed. Rather than
-- retrofit ingestion_jobs' single-file-upload shape (job_id/filename/
-- chunks_added) to also carry per-run entity-resolution tier counts it
-- was never designed for, this is a new, separate table -- same
-- discipline as migration 027's own header: a plain side table, added
-- when the existing one doesn't cleanly fit, never a forced retrofit.
--
-- One row per ingestion RUN (a single ingest_file() call, or one whole
-- `sync_muhafiz_data.py --full` pass), not per document and not per
-- mention -- src/graph/ingestion_quality.py's start_run()/finish_run()
-- are the two maintenance choke points, mirroring identity_index's own
-- write_node()-choke-point precedent. The four tier_* columns are pure
-- counts of decisions entity_resolution.py already makes deterministically
-- (TIER_CNIC_AUTO/TIER_FLAGGED/TIER_REVIEW/TIER_NEW) -- this table adds
-- visibility, never a new decision.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, safe to re-run.
--
-- HOW TO APPLY
--   python scripts/apply_migration.py migrations/028_ingestion_run_quality.sql

CREATE TABLE IF NOT EXISTS ingestion_run_quality (
    run_id                          TEXT PRIMARY KEY,
    source                          TEXT NOT NULL,  -- 'ingest_file' | 'sync_muhafiz_data' | ...
    case_id                         TEXT,            -- nullable: a multi-case bulk sync run has none

    started_at                      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at                     TIMESTAMPTZ,

    -- Counts of entity_resolution.py's own tier decisions
    -- (TIER_CNIC_AUTO/TIER_FLAGGED/TIER_REVIEW/TIER_NEW) across every
    -- mention resolved during this run -- see that module's own tier
    -- constants, never re-derived or re-named here.
    tier_cnic_auto                  INTEGER NOT NULL DEFAULT 0,
    tier_flagged_unverified         INTEGER NOT NULL DEFAULT 0,
    tier_human_review               INTEGER NOT NULL DEFAULT 0,
    tier_new                        INTEGER NOT NULL DEFAULT 0,

    -- structured_projection.py's _has_corroboration() gate: how many
    -- times a no-CNIC structured mention had a real candidate that the
    -- gate refused to risk (never how many mentions had no candidate at
    -- all -- see ingestion_quality.py's own docstring for the distinction,
    -- which matters for Module G2's baseline).
    corroboration_gate_rejections   INTEGER NOT NULL DEFAULT 0,

    extraction_errors               INTEGER NOT NULL DEFAULT 0,

    -- Module G2's circuit breaker (not built by this migration) sets
    -- this on a run whose ambiguous-match rate spikes above its rolling
    -- baseline -- a plain flag column here so G2 has somewhere to write
    -- to without a second migration; false/unset is this table's entire
    -- behavior until G2 lands.
    flagged_for_review              BOOLEAN NOT NULL DEFAULT false,
    flagged_reason                  TEXT
);

-- Backs "every run for this source, most recent first" (the admin
-- surface's own read shape, Module G4) and a rolling-baseline read over
-- the same source (Module G2).
CREATE INDEX IF NOT EXISTS ix_ingestion_run_quality_source
    ON ingestion_run_quality (source, started_at DESC);

CREATE INDEX IF NOT EXISTS ix_ingestion_run_quality_case
    ON ingestion_run_quality (case_id);
