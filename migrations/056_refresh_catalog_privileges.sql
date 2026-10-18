-- ============================================================
-- 056 — Let the application refresh the derived catalog
--
-- Ingestion failed on its verify step:
--
--   InsufficientPrivilegeError:
--     must be owner of materialized view schema_relationships
--
-- REFRESH MATERIALIZED VIEW requires ownership, which muhafiz_app does not
-- and should not have. 053 noted this and left refresh to the migration
-- role, but that leaves the catalog stale after every ingestion run — and
-- a stale catalog routes the planner through a schema that no longer
-- matches the data, confidently.
--
-- SECURITY DEFINER runs the function as its owner, so the application can
-- ask for a refresh without being able to alter the views. search_path is
-- pinned inside the function, which is required for a SECURITY DEFINER
-- function: without it a caller could shadow a table name and have the
-- definer execute it with elevated rights.
-- ============================================================

BEGIN;

CREATE OR REPLACE FUNCTION refresh_catalog()
RETURNS TABLE (step text, detail text)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public, pg_temp
AS $fn$
BEGIN
    REFRESH MATERIALIZED VIEW schema_relationships;
    step := 'schema_relationships';
    detail := (SELECT count(*)::text || ' edges' FROM schema_relationships);
    RETURN NEXT;

    REFRESH MATERIALIZED VIEW column_catalog;
    step := 'column_catalog';
    detail := (SELECT count(*)::text || ' columns' FROM column_catalog);
    RETURN NEXT;

    REFRESH MATERIALIZED VIEW evidence_tables;
    step := 'evidence_tables';
    detail := (SELECT count(*)::text || ' tables trace to provenance'
                 FROM evidence_tables);
    RETURN NEXT;

    REFRESH MATERIALIZED VIEW table_roles;
    step := 'table_roles';
    detail := (SELECT string_agg(role || '=' || n, ', ' ORDER BY role)
                 FROM (SELECT role, count(*)::text n FROM table_roles
                        GROUP BY role) x);
    RETURN NEXT;

    ANALYZE;
    step := 'analyze'; detail := 'statistics refreshed';
    RETURN NEXT;
END;
$fn$;

COMMENT ON FUNCTION refresh_catalog() IS
'Rebuilds the derived catalog and statistics. SECURITY DEFINER so the '
'application can refresh after ingestion without owning the views. Run '
'after every migration and every bulk load.';

GRANT EXECUTE ON FUNCTION refresh_catalog() TO muhafiz_app;

COMMIT;
