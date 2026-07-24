-- ============================================================
-- Muhafiz — Migration 006
-- Phase 7: Security & Access Control (RBAC, Case Assignments, Audit Logs)
--
-- HOW TO APPLY
--   python scripts/apply_migration.py migrations/006_rbac.sql
-- ============================================================

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_type WHERE typname = 'user_role') THEN
        CREATE TYPE user_role AS ENUM ('investigator', 'supervisor', 'station-admin', 'platform-admin');
    END IF;
END$$;

-- Add role column and backfill from is_admin
ALTER TABLE users ADD COLUMN IF NOT EXISTS role user_role;

UPDATE users SET role = 'platform-admin' WHERE is_admin = true AND role IS NULL;
UPDATE users SET role = 'investigator' WHERE is_admin = false AND role IS NULL;
UPDATE users SET role = 'investigator' WHERE role IS NULL;

ALTER TABLE users ALTER COLUMN role SET NOT NULL;
ALTER TABLE users ALTER COLUMN role SET DEFAULT 'investigator';

-- Drop the old is_admin column per the plan
ALTER TABLE users DROP COLUMN IF EXISTS is_admin;

-- Create case_assignments table (ABAC anchor)
CREATE TABLE IF NOT EXISTS case_assignments (
    assignment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id TEXT NOT NULL REFERENCES cases(case_id) ON DELETE CASCADE,
    user_id UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role user_role NOT NULL DEFAULT 'investigator',
    created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (case_id, user_id)
);

-- Create audit_logs table
CREATE TABLE IF NOT EXISTS audit_logs (
    log_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    timestamp TIMESTAMP WITHOUT TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT NOT NULL,
    user_id UUID REFERENCES users(id) ON DELETE SET NULL,
    case_id TEXT REFERENCES cases(case_id) ON DELETE SET NULL,
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);
