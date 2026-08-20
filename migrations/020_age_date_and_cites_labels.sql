-- ============================================================
-- Muhafiz — Migration 020
-- Pre-create the `Date` vlabel and `CITES` elabel (M8 of the Muhafiz Data
-- API migration, docs/decisions/0001-muhafiz-api-migration.md).
--
-- Both labels are already written live, outside migration 005/011's
-- static list:
--   - `Date` — src/graph/entity_resolution.py's incident-resolution path
--     has written it since before this migration existed
--     (OCCURRED_ON -> Date), and src/graph/structured_projection.py (M6a)
--     writes many more from typed FIR timestamps. Left off 005's list by
--     omission, not by design — it was exposed to exactly the
--     concurrent-first-write race 005's own header describes pre-creating
--     labels to prevent.
--   - `CITES` — introduced by src/graph/cross_silo_projection.py (M6b),
--     a Case->Case prose-citation edge, never declared in a migration
--     until now.
--
-- HOW TO APPLY
--   python scripts/apply_migration.py migrations/020_age_date_and_cites_labels.sql
--   Idempotent — safe to re-run, same guarded-loop pattern as 005/011.
-- ============================================================

LOAD 'age';
SET search_path = ag_catalog, "$user", public;

DO $$
DECLARE
    graph_oid oid;
BEGIN
    -- Mirrors both graphs 005/011 create — evidence_graph (production)
    -- and evidence_graph_eval (Phase 3's isolated eval graph) — so an
    -- eval run exercising Date/CITES writes gets the same race-free
    -- pre-creation the production graph does.
    FOR graph_oid IN
        SELECT graphid FROM ag_catalog.ag_graph WHERE name IN ('evidence_graph', 'evidence_graph_eval')
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM ag_catalog.ag_label WHERE graph = graph_oid AND name = 'Date'
        ) THEN
            PERFORM create_vlabel(
                (SELECT name FROM ag_catalog.ag_graph WHERE graphid = graph_oid), 'Date'
            );
        END IF;

        IF NOT EXISTS (
            SELECT 1 FROM ag_catalog.ag_label WHERE graph = graph_oid AND name = 'CITES'
        ) THEN
            PERFORM create_elabel(
                (SELECT name FROM ag_catalog.ag_graph WHERE graphid = graph_oid), 'CITES'
            );
        END IF;
    END LOOP;
END
$$;
