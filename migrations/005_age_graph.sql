-- ============================================================
-- Muhafiz — Migration 005
-- Apache AGE graph setup (Phase 4.1 of the Evidence Intelligence
-- Platform build — docs/IMPLEMENTATION_PLAN.md Phase 4)
--
-- HOW TO APPLY
--   python scripts/apply_migration.py migrations/005_age_graph.sql
--   Requires the Postgres container to be running the AGE-enabled
--   image (see docker-compose.yml) — CREATE EXTENSION age fails
--   against a plain postgres:16-alpine image, the extension .so
--   isn't installed there.
--   It is idempotent.
--
-- Continues migration 004's raw-SQL/apply_migration.py pattern rather
-- than an Alembic revision — see 004's own header for why a third
-- migration mechanism isn't introduced here.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS age;

LOAD 'age';
SET search_path = ag_catalog, "$user", public;

-- create_graph() has no IF NOT EXISTS form, and errors if the graph
-- already exists — guard it explicitly so this file is safe to re-run.
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'evidence_graph'
    ) THEN
        PERFORM create_graph('evidence_graph');
    END IF;
END
$$;

-- Pre-create every node/edge label from docs/graph_schema.md's catalog,
-- rather than leaving them to AGE's default lazy-on-first-write creation.
-- This is not just tidiness: AGE creates a label's underlying table+
-- sequence the first time it's written, and when two concurrent
-- transactions both try to write the SAME brand-new label for the first
-- time, they race to create the same catalog objects and one loses with
-- `duplicate key value violates unique constraint "pg_class_relname_nsp_
-- index"` — reproduced empirically while building src/graph/age_client.py
-- (Phase 4.7). Pre-creating every label here, once, before any concurrent
-- ingestion traffic exists, removes the race entirely. AGE still enforces
-- no property schema — this only pre-creates the label (the table), not
-- any property shape; docs/graph_schema.md plus app-layer validation
-- remains the actual schema enforcement.
DO $$
DECLARE
    graph_oid oid := (SELECT graphid FROM ag_catalog.ag_graph WHERE name = 'evidence_graph');
    vlabel text;
    elabel text;
BEGIN
    FOREACH vlabel IN ARRAY ARRAY[
        'Case', 'Person', 'Vehicle', 'PhoneNumber', 'Address', 'Organization',
        'Weapon', 'Incident', 'Document', 'StructuredRecord'
    ]
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM ag_catalog.ag_label WHERE graph = graph_oid AND name = vlabel
        ) THEN
            PERFORM create_vlabel('evidence_graph', vlabel);
        END IF;
    END LOOP;

    FOREACH elabel IN ARRAY ARRAY[
        'BELONGS_TO_CASE', 'APPEARS_IN', 'ASSOCIATED_WITH', 'SAME_AS', 'OWNS',
        'REGISTERED_TO', 'LOCATED_AT', 'INVOLVED_IN', 'PART_OF', 'OCCURRED_ON',
        'CONFLICTS_WITH'
    ]
    LOOP
        IF NOT EXISTS (
            SELECT 1 FROM ag_catalog.ag_label WHERE graph = graph_oid AND name = elabel
        ) THEN
            PERFORM create_elabel('evidence_graph', elabel);
        END IF;
    END LOOP;
END
$$;
