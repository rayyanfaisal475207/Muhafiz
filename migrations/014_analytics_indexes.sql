-- Migration 014: Indexes for the admin analytics queries (Phase 7,
-- Module 7.4).
--
-- Background (see issues.md's "Analytics queries against pipeline_runs/
-- pipeline_steps/error_logs are unbounded and the underlying tables have
-- zero indexes" finding): get_runs_since()/get_step_latencies_since()/
-- get_errors_since() (src/data_gateway/direct_backend.py) each run a
-- WHERE <date column> >= $since full scan with no supporting index —
-- fine on today's dev-scale data, a growing full-table scan on every
-- /api/admin/usage, /latency, /errors/trend request as these tables
-- accumulate real traffic.
--
-- Columns chosen to match each query's actual WHERE clause:
--   pipeline_runs.created_at   — get_runs_since()'s only filter
--   pipeline_steps(run_id, created_at) — get_step_latencies_since()
--       filters on created_at; run_id leads the composite since
--       PipelineStep.steps's relationship join (case.py cascade delete,
--       and any future per-run step lookup) also filters on run_id —
--       a composite index serves both without needing two indexes.
--   error_logs.occurred_at     — get_errors_since()'s only filter
--
--   python scripts/apply_migration.py migrations/014_analytics_indexes.sql

CREATE INDEX IF NOT EXISTS ix_pipeline_runs_created_at ON pipeline_runs (created_at);
CREATE INDEX IF NOT EXISTS ix_pipeline_steps_run_id_created_at ON pipeline_steps (run_id, created_at);
CREATE INDEX IF NOT EXISTS ix_error_logs_occurred_at ON error_logs (occurred_at);
