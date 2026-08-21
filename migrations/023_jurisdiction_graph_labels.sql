-- ============================================================
-- Muhafiz — Migration 023
-- Pre-create the `PoliceStation`/`District` vlabels and `FILED_AT` elabel
-- (Milestone B1, GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md — jurisdiction graph
-- nodes, docs/decisions/0002-graph-schema-expansion-and-scale.md).
--
-- `PART_OF` is NOT added here — it already exists (migration 005), first
-- written for Incident->Case. This module reuses the same edge label for
-- a second, semantically-consistent pair (PoliceStation->District, "is
-- part of"), the same way `docs/graph_schema.md` already treats edge
-- labels as reusable across node-type pairs rather than one label per pair.
--
-- Mirrors 005/011/020's own guarded, idempotent, re-runnable pattern —
-- see 020's header for why labels get their own follow-on migration
-- instead of a retroactive edit to 005's static list (labels written by
-- code that ships in the same change as the label's own pre-creation).
--
-- HOW TO APPLY
--   python scripts/apply_migration.py migrations/023_jurisdiction_graph_labels.sql
--   Idempotent — safe to re-run.
-- ============================================================

LOAD 'age';
SET search_path = ag_catalog, "$user", public;

DO $$
DECLARE
    graph_oid oid;
    graph_name text;
BEGIN
    -- Both graphs, same reasoning as migration 020: an eval run exercising
    -- B1 writes (scripts/eval_entity_resolution.py et al.) gets the same
    -- race-free pre-creation the production graph does.
    FOR graph_oid, graph_name IN
        SELECT graphid, name FROM ag_catalog.ag_graph WHERE name IN ('evidence_graph', 'evidence_graph_eval')
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM ag_catalog.ag_label WHERE graph = graph_oid AND name = 'PoliceStation'
        ) THEN
            PERFORM create_vlabel(graph_name, 'PoliceStation');
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM ag_catalog.ag_label WHERE graph = graph_oid AND name = 'District'
        ) THEN
            PERFORM create_vlabel(graph_name, 'District');
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM ag_catalog.ag_label WHERE graph = graph_oid AND name = 'FILED_AT'
        ) THEN
            PERFORM create_elabel(graph_name, 'FILED_AT');
        END IF;
    END LOOP;
END
$$;
