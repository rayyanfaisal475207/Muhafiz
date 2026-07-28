-- Migration 015: Least-privilege Postgres role for the application's
-- normal runtime connection (DATABASE_URL)
--
-- Background (issues.md's 13th Critical finding, added 2026-07-28 during
-- the Phase 0-3 closeout — the first point this codebase had live Postgres
-- access): the application's DATABASE_URL has always connected as the
-- `postgres` superuser (rolsuper=true, rolbypassrls=true). Postgres
-- superusers and BYPASSRLS roles unconditionally bypass row-level security
-- regardless of FORCE ROW LEVEL SECURITY (migrations 008/010) and
-- regardless of whether the application correctly calls
-- set_config('app.case_id', ...). Every one of Phase 2's RLS verification
-- checks only ever passed because they were deliberately run as a
-- separate, purpose-built non-superuser role created solely for that
-- verification -- never as the role the application itself connects with.
-- Phase 2's entire RLS backstop has therefore had zero actual effect in
-- any deployment using the superuser DATABASE_URL, though app-layer
-- authorization checks are unaffected and remain today's real protection.
--
-- This mirrors Module 1.2's muhafiz_mcp_readonly split (migration 009),
-- but scoped to full application DML instead of a single read-only table:
-- muhafiz_app gets SELECT/INSERT/UPDATE/DELETE on every table the
-- application's models/gateway actually touch, and nothing else -- no
-- superuser, no BYPASSRLS, no ownership of any table.
--
-- One necessary exception to "DML only": src/database/postgres.py's
-- init_postgres() runs Base.metadata.create_all() (CREATE TABLE IF NOT
-- EXISTS) using this same connection on every application startup, so
-- muhafiz_app also needs CREATE ON SCHEMA public. This does not defeat
-- the fix's purpose (RLS bypass), it's a schema-design privilege, not a
-- data-access one -- but it is real and worth naming explicitly rather
-- than silently narrowing "least privilege" to mean less than it does.
--
-- No password is set here (no secrets belong in version control) -- same
-- convention as migration 009. See the manual steps at the bottom.
--
-- Idempotent: safe to re-run.

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'muhafiz_app') THEN
        CREATE ROLE muhafiz_app WITH LOGIN;
    END IF;
END
$$;

DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO muhafiz_app', current_database());
END
$$;

GRANT USAGE ON SCHEMA public TO muhafiz_app;

-- See the module docstring above: required for init_postgres()'s
-- create_all() call, which runs on every startup using this same role.
GRANT CREATE ON SCHEMA public TO muhafiz_app;

-- Full DML on every table the application's SQLAlchemy models/gateway
-- read and write. Deliberately explicit and per-table, not
-- "ALL TABLES IN SCHEMA public" or an ALTER DEFAULT PRIVILEGES grant --
-- same reasoning as migration 009: a future new table must be explicitly
-- added here, not silently inherited.
GRANT SELECT, INSERT, UPDATE, DELETE ON
    users,
    user_context_profiles,
    projects,
    cases,
    project_memory,
    sessions,
    messages,
    pipeline_runs,
    pipeline_steps,
    documents,
    police_reference_data,
    mcp_tool_calls,
    generated_files,
    error_logs,
    ingestion_jobs,
    session_attachments,
    case_assignments,
    audit_logs
TO muhafiz_app;

-- pipeline_steps.step_id is the one autoincrement (SERIAL-backed) primary
-- key in the schema -- every other table's id is a client-generated UUID.
-- Sequences carry no sensitive data (just a counter), so granting broadly
-- on all current sequences is low-risk and avoids missing one; still
-- explicit to "current sequences", not a default-privileges grant.
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO muhafiz_app;

-- Deliberately NOT granted: alembic_version (a migration-tooling table,
-- never read/written by the running application itself).

-- ── Manual steps required after applying this migration ─────────────────
-- An operator with live DB access must, before DATABASE_URL is repointed
-- at this role in any real deployment:
--
--   1. Set a real password:
--        ALTER ROLE muhafiz_app WITH PASSWORD '<a-real-secret>';
--   2. Set DATABASE_URL in .env to a connection string using this role,
--      e.g.:
--        DATABASE_URL=postgresql+asyncpg://muhafiz_app:<password>@localhost:5432/muhafiz?ssl=require
--   3. Verify with scripts/verify_app_role.py (requires a live Postgres
--      connection).
--   4. Restart the application and confirm it starts cleanly (init_postgres()
--      succeeds) and every REST route still works end-to-end.
--
-- Until DATABASE_URL is repointed, the application keeps connecting as
-- the superuser -- this migration alone does not close the privilege gap;
-- the env var must actually be changed, exactly like Module 1.2's
-- MCP_DATABASE_URL.
