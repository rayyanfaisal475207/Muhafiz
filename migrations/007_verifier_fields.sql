-- 007_verifier_fields.sql
-- Add Verifier Phase 6 fields to pipeline_runs table.
-- Postgres-compatible migration script.

ALTER TABLE pipeline_runs
ADD COLUMN IF NOT EXISTS verifier_passed BOOLEAN,
ADD COLUMN IF NOT EXISTS verifier_regenerated BOOLEAN;
