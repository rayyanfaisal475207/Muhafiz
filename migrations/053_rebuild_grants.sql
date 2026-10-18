-- ============================================================
-- 053 — Grants for the rebuilt schema
--
-- Ingestion failed with:
--
--   asyncpg.exceptions.InsufficientPrivilegeError:
--     permission denied for table places
--
-- The application connects as muhafiz_app, which was granted table by
-- table in 031 and 032. Every table added since — the whole rebuild —
-- is invisible to it.
--
-- Granting by DISCOVERY rather than by list, so a table added by a later
-- migration is not silently unreachable the same way. The grant covers
-- the schema, and default privileges cover what does not exist yet.
--
-- Sequences matter as much as tables: places, doc_elements and doc_spans
-- use bigserial, and an INSERT fails on the sequence even when the table
-- is granted.
-- ============================================================

BEGIN;

GRANT USAGE ON SCHEMA public TO muhafiz_app;

GRANT SELECT, INSERT, UPDATE, DELETE
    ON ALL TABLES IN SCHEMA public TO muhafiz_app;

GRANT USAGE, SELECT
    ON ALL SEQUENCES IN SCHEMA public TO muhafiz_app;

GRANT EXECUTE
    ON ALL FUNCTIONS IN SCHEMA public TO muhafiz_app;

-- Tables created by future migrations, so this problem does not recur.
-- Applies to objects created by the role running this (postgres).
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO muhafiz_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO muhafiz_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT EXECUTE ON FUNCTIONS TO muhafiz_app;

-- Materialised views need SELECT explicitly; REFRESH additionally needs
-- ownership, so refresh_catalog() is left to the migration role and the
-- application only reads the catalog.
GRANT SELECT ON schema_relationships, column_catalog,
                table_roles, evidence_tables TO muhafiz_app;

COMMIT;
