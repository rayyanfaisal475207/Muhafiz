-- ============================================================
-- Muhafiz — Migration 011
-- Apache AGE eval/dev graph (Phase 3, Module 3.1 — see solution.md and
-- issues.md's Critical "Apache AGE graph contains synthetic eval-harness
-- test fixtures permanently written into real cases" finding).
--
-- HOW TO APPLY
--   python scripts/apply_migration.py migrations/011_age_eval_graph.sql
--   Requires the Postgres container to be running the AGE-enabled
--   image, same as 005_age_graph.sql. Idempotent.
--
-- WHY A SECOND GRAPH, NOT A NAMESPACED LABEL/PROPERTY WITHIN
-- evidence_graph
--   scripts/eval_entity_resolution.py runs `MATCH (n) DETACH DELETE n`
--   unconditionally to give itself a clean, reproducible starting state
--   every run — that wipe must be structurally incapable of touching
--   evidence_graph's real case data, not just conventionally scoped to
--   avoid it. A second, physically separate AGE graph is the only way a
--   wipe-everything query aimed at "the eval graph" cannot, even by
--   future accident, reach real data — a shared graph with an
--   is_eval-style property filter would still let one dropped filter
--   clause repeat exactly the incident this migration exists to prevent.
--
-- Mirrors 005_age_graph.sql's pattern exactly (same vlabel/elabel
-- catalog, same guarded create_graph()/create_vlabel()/create_elabel()
-- idempotency pattern, same concurrent-first-write race rationale for
-- pre-creating labels) — this is deliberately not a divergent schema for
-- the eval graph, since eval fixtures are meant to exercise the same
-- node/edge shapes real ingestion produces.
-- ============================================================

CREATE EXTENSION IF NOT EXISTS age;

LOAD 'age';
SET search_path = ag_catalog, "$user", public;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM ag_catalog.ag_graph WHERE name = 'evidence_graph_eval'
    ) THEN
        PERFORM create_graph('evidence_graph_eval');
    END IF;
END
$$;

DO $$
DECLARE
    graph_oid oid := (SELECT graphid FROM ag_catalog.ag_graph WHERE name = 'evidence_graph_eval');
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
            PERFORM create_vlabel('evidence_graph_eval', vlabel);
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
            PERFORM create_elabel('evidence_graph_eval', elabel);
        END IF;
    END LOOP;
END
$$;
