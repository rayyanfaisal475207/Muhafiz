-- Migration 017: raw pre-filter node/edge counts on community_runs
--
-- Background: scripts/check_community_staleness.py needs to compare the
-- LIVE graph's current raw Person node/edge counts against what they were
-- at the last detection run, to decide whether a re-run is worth doing.
-- community_runs.node_count/edge_count (migration 016) are POST-filter
-- counts from the actual clustered networkx graph (after
-- community_detection.py's implausible-name/known-station exclusions) —
-- comparing those against the live graph's raw counts is an apples-to-
-- oranges comparison that will always show large fake "drift" regardless
-- of whether anything real has changed. This migration adds the raw
-- counts alongside the existing filtered ones so the staleness check has
-- a genuinely comparable baseline.
--
-- Nullable: existing community_runs rows (from before this migration)
-- won't have these populated — the staleness check treats a NULL here as
-- "no comparable baseline, treat as stale" rather than erroring.

ALTER TABLE community_runs ADD COLUMN IF NOT EXISTS raw_node_count INT;
ALTER TABLE community_runs ADD COLUMN IF NOT EXISTS raw_edge_count INT;
