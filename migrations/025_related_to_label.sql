-- ============================================================
-- Muhafiz — Migration 025
-- Pre-create the `RELATED_TO` elabel (Milestone C1,
-- GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — person-relationship edges,
-- docs/decisions/0002-graph-schema-expansion-and-scale.md).
--
-- Mirrors 005/020/023/024's own guarded, idempotent, re-runnable pattern.
--
-- HOW TO APPLY
--   python scripts/apply_migration.py migrations/025_related_to_label.sql
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
            SELECT 1 FROM ag_catalog.ag_label WHERE graph = graph_oid AND name = 'RELATED_TO'
        ) THEN
            PERFORM create_elabel(graph_name, 'RELATED_TO');
        END IF;
    END LOOP;
END
$$;
