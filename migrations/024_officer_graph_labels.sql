-- ============================================================
-- Muhafiz — Migration 024
-- Pre-create the `Officer` vlabel and `ASSIGNED_TO` elabel (Milestone B2,
-- GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — officer identity resolution,
-- docs/decisions/0002-graph-schema-expansion-and-scale.md).
--
-- Mirrors 005/011/020/023's own guarded, idempotent, re-runnable pattern.
--
-- HOW TO APPLY
--   python scripts/apply_migration.py migrations/024_officer_graph_labels.sql
--   Idempotent — safe to re-run.
-- ============================================================

LOAD 'age';
SET search_path = ag_catalog, "$user", public;

DO $$
DECLARE
    graph_oid oid;
    graph_name text;
BEGIN
    FOR graph_oid, graph_name IN
        SELECT graphid, name FROM ag_catalog.ag_graph WHERE name IN ('evidence_graph', 'evidence_graph_eval')
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM ag_catalog.ag_label WHERE graph = graph_oid AND name = 'Officer'
        ) THEN
            PERFORM create_vlabel(graph_name, 'Officer');
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM ag_catalog.ag_label WHERE graph = graph_oid AND name = 'ASSIGNED_TO'
        ) THEN
            PERFORM create_elabel(graph_name, 'ASSIGNED_TO');
        END IF;
    END LOOP;
END
$$;
