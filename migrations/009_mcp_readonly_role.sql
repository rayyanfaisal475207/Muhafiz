-- Migration 009: Least-privilege Postgres role for the MCP SQL route
--
-- Before this migration, src/mcp/client.py connected the MCP Postgres server
-- (a spawned `npx @modelcontextprotocol/server-postgres` process) using the
-- exact same DATABASE_URL as the rest of the application — i.e. as the
-- superuser role, with full read/write access to every table: users,
-- password_hash, audit_logs, victim_info, suspect_info, everything. The MCP
-- SQL route only needs to read police_reference_data.
--
-- This role is granted SELECT-only on police_reference_data and nothing
-- else. No password is set here (no secrets belong in version control) —
-- CREATE ROLE ... WITH LOGIN with no PASSWORD clause leaves rolpassword
-- NULL, so password authentication fails for this role until an operator
-- explicitly sets one. See the manual steps below.
--
-- Idempotent: safe to re-run against an environment that already has this
-- role/grants (matches the CREATE ... IF NOT EXISTS convention used by the
-- rest of this migration chain, worked around here since Postgres has no
-- native `CREATE ROLE IF NOT EXISTS`).

DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'muhafiz_mcp_readonly') THEN
        CREATE ROLE muhafiz_mcp_readonly WITH LOGIN;
    END IF;
END
$$;

-- GRANT CONNECT ON DATABASE needs the actual database name, which isn't
-- known at migration-authoring time (this file is applied against whatever
-- database DATABASE_URL points at) — resolve it dynamically.
DO $$
BEGIN
    EXECUTE format('GRANT CONNECT ON DATABASE %I TO muhafiz_mcp_readonly', current_database());
END
$$;

GRANT USAGE ON SCHEMA public TO muhafiz_mcp_readonly;
GRANT SELECT ON police_reference_data TO muhafiz_mcp_readonly;

-- Deliberately no ALTER DEFAULT PRIVILEGES grant here: a future new table
-- must be explicitly, individually granted to this role if the MCP route
-- ever needs to read it. Silent inheritance of SELECT on every new table
-- would slowly recreate the exact over-broad-access problem this migration
-- exists to close.

-- ── Manual steps required after applying this migration ─────────────────
-- An operator with live DB access must, before MCP_DATABASE_URL is set to
-- point at this role in any real deployment:
--
--   1. Set a real password:
--        ALTER ROLE muhafiz_mcp_readonly WITH PASSWORD '<a-real-secret>';
--   2. Set MCP_DATABASE_URL in .env to a connection string using this role,
--      e.g.:
--        MCP_DATABASE_URL=postgresql://muhafiz_mcp_readonly:<password>@localhost:5432/muhafiz
--   3. Verify with scripts/verify_mcp_role.py (requires a live Postgres
--      connection — not runnable in this environment).
--
-- Until MCP_DATABASE_URL is set, src/mcp/client.py falls back to
-- DATABASE_URL (the superuser connection) with a loud startup warning —
-- this migration alone does not close the privilege gap; the env var must
-- actually be set.
