-- Migration 031: AGE graph-schema grants for the muhafiz_app least-
-- privilege role (migration 015).
--
-- Found live, 28 Aug 2026, verifying the cross-lingual graph name-matching
-- fix: every single graph call under muhafiz_app was failing --
-- "type agtype does not exist" / "unhandled cypher(cstring) function
-- call" -- because migration 015 granted DML on the relational tables
-- but never touched the AGE graph schemas at all. muhafiz_app had no
-- USAGE on ag_catalog (where the agtype type and cypher() function
-- itself live) and no USAGE/DML on evidence_graph or
-- evidence_graph_eval (where every vertex/edge label is a real
-- Postgres table). The entire graph feature -- every Person/Vehicle/
-- Officer/etc. read or write, so XGRAPH, GRAPH, GRAPH_HYBRID routes and
-- every ingestion-time entity-resolution write -- has been silently
-- broken for any deployment that completed migration 015's own
-- documented manual step (repointing DATABASE_URL at muhafiz_app)
-- without also running this. src/graph/age_client.py's _load_age()
-- correctly anticipates the LOAD-is-superuser-only half of this problem
-- (see its own docstring) but its assumption that AGE is already
-- preloaded via shared_preload_libraries was also not true of the
-- bundled docker-compose.yml Postgres, which this migration's sibling
-- fix (docker-compose.yml's postgres.entrypoint) addresses separately --
-- both were required together to actually restore graph access.
--
-- UNLIKE migrations 009/015's explicit per-table grants (deliberately
-- avoiding "ALL TABLES"/default-privileges so a future new table needs
-- an explicit decision): the AGE graph schemas are not a human-curated
-- table list the way the relational schema is. Every vertex/edge LABEL
-- is a real table AGE creates dynamically at runtime (create_vlabel/
-- create_elabel), including from application code itself -- migrations
-- 020/023/024/025/026 all exist specifically to pre-create a label
-- before the app's own first concurrent write races AGE's internal
-- catalog. A per-table grant list here would need updating every time a
-- new label is added, and forgetting one would silently break writes to
-- exactly that label with no warning until it's hit live. ALL TABLES +
-- ALTER DEFAULT PRIVILEGES is the correct choice for this specific
-- schema, not a relaxation of migration 009/015's own standard --
-- their reasoning (avoid surprise access to a table nobody decided to
-- grant) does not apply to a schema whose whole contract is "AGE owns
-- what tables exist here, not us".
--
-- Idempotent: safe to re-run.

GRANT USAGE ON SCHEMA ag_catalog TO muhafiz_app;
GRANT USAGE ON SCHEMA evidence_graph TO muhafiz_app;
GRANT USAGE ON SCHEMA evidence_graph_eval TO muhafiz_app;

GRANT SELECT ON ALL TABLES IN SCHEMA ag_catalog TO muhafiz_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA evidence_graph TO muhafiz_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA evidence_graph_eval TO muhafiz_app;

GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA ag_catalog TO muhafiz_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA evidence_graph TO muhafiz_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA evidence_graph_eval TO muhafiz_app;

-- Covers every future label AGE creates in either graph, at runtime,
-- without needing a new migration each time -- see the module comment
-- above for why this schema specifically warrants that, unlike public.
ALTER DEFAULT PRIVILEGES IN SCHEMA evidence_graph
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO muhafiz_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA evidence_graph_eval
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO muhafiz_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA evidence_graph
    GRANT USAGE, SELECT ON SEQUENCES TO muhafiz_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA evidence_graph_eval
    GRANT USAGE, SELECT ON SEQUENCES TO muhafiz_app;

-- ── Manual step required after applying this migration ───────────────────
-- docker-compose.yml's postgres service now sets
-- shared_preload_libraries=age (the other half of this fix) -- that
-- setting only takes effect after a container restart:
--   docker compose up -d postgres --force-recreate
-- Verify both halves together with:
--   python -c "import asyncio; from src.graph import age_client; \
--     asyncio.run(age_client.execute_cypher('MATCH (n) RETURN n LIMIT 1', columns=['n']))"
