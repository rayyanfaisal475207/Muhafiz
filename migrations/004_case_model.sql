-- ============================================================
-- Muhafiz — Migration 004
-- Case data model (Phase 1 of the Evidence Intelligence Platform build)
--
-- HOW TO APPLY
--   psql "$DATABASE_URL" -f migrations/004_case_model.sql
--   or: python scripts/apply_migration.py migrations/004_case_model.sql
--   It is idempotent.
--
-- This follows migration 003's raw-SQL/apply_migration.py pattern rather
-- than an Alembic revision. Alembic's own history never picked up 003's
-- tables (error_logs, ingestion_jobs, session_attachments) even though
-- they were added to src/database/models.py, so the two migration
-- mechanisms have already drifted apart. Adding a third pattern (a fresh
-- Alembic revision) here would make that drift worse, not better — this
-- keeps every post-002 schema change in the one place it actually lives.
-- ============================================================

-- ── 1. Cases ─────────────────────────────────────────────────
-- The organizing entity every piece of evidence attaches to (architecture
-- doc §3.1). case_id is TEXT, not a generated UUID: the synthetic dataset
-- (data/memory/case_index.csv, structured_records/*.csv) already refers to
-- cases by human-meaningful codes like "CASE-003", and FIR numbers/case
-- codes are what an investigator actually types and searches for — an
-- opaque UUID would just be a translation layer nobody asked for. Callers
-- that don't have a real code yet may still generate one client-side.
CREATE TABLE IF NOT EXISTS cases (
    case_id               text PRIMARY KEY,
    fir_number            text,
    crime_category        text,
    investigation_officer text,
    police_station        text,
    incident_date         date,
    investigation_status  text,
    location              text,
    description           text,
    victim_info           jsonb,
    suspect_info          jsonb,
    created_at            timestamptz NOT NULL DEFAULT now(),
    updated_at            timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_cases_fir_number ON cases (fir_number);
CREATE INDEX IF NOT EXISTS idx_cases_status     ON cases (investigation_status);

-- ── 2. documents.case_id ─────────────────────────────────────
-- Nullable: the 96 documents ingested pre-Phase-1 have no case and must
-- keep working as global/project-scoped evidence exactly as before.
-- ON DELETE SET NULL, not CASCADE — deleting a case must not silently
-- delete evidence; it should become unassigned and visible for re-triage.
ALTER TABLE documents
    ADD COLUMN IF NOT EXISTS case_id text REFERENCES cases(case_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_documents_case_id ON documents (case_id);

-- ── 3. sessions.case_id ──────────────────────────────────────
-- Mirrors sessions.project_id: a chat opened against a case remembers it,
-- the same way project-scoped chats already do (see 002/fe8c28ae62b3).
ALTER TABLE sessions
    ADD COLUMN IF NOT EXISTS case_id text REFERENCES cases(case_id) ON DELETE SET NULL;

CREATE INDEX IF NOT EXISTS idx_sessions_case_id ON sessions (case_id);

-- ── Note: structured-record evidence ──────────────────────────
-- The case-management-export/property-tag/court-listing structured-record
-- evidence type (architecture §3.2) is deliberately NOT modeled here. A
-- Phase 1 Case needs only `documents.case_id` to satisfy "ingest a
-- document under a case, retrieve it case-scoped" — the structured-record
-- container is a distinct evidence type (typed rows written straight into
-- Postgres, no chunking/embedding) that belongs with the rest of the
-- typed-evidence work in Phase 4, not bolted on early as an empty table
-- nothing writes to yet.
