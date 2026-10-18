-- ============================================================
-- 051 — Materialise the catalog so the planner can afford to read it
--
-- THE MEASUREMENT
--
--   schema_relationships   138 ms      (200 rows)
--   table_roles             13 ms      (54 rows)
--   column_catalog         459 ms      (522 rows)
--   column_stats             4 ms      (241 rows)
--
-- Each is tolerable alone. Joined — column_stats to table_roles with a
-- lateral unnest — the query exceeds 20 seconds, because every one of
-- these is a view over information_schema, which is itself a view over
-- pg_catalog with security filters that Postgres cannot push a join
-- through. The planner re-evaluates the whole catalog per outer row.
--
-- A planner that must read its own schema before every question cannot
-- pay half a second for the schema, let alone twenty.
--
-- CACHING A DERIVATION IS NOT AUTHORING A LIST
--
-- This is worth stating precisely, because it looks superficially like
-- the thing 039 removed. It is not:
--
--   an authored list   is written by a human, is wrong on contact with
--                      data, and cannot be regenerated
--   a stored list      grows without bound and goes stale silently
--   this               is DERIVED from the catalog, regenerated in full
--                      by one command, never edited, and reports its own
--                      staleness
--
-- The test is: can it be thrown away and rebuilt exactly, from the
-- database, with no human input. This can. A vocabulary list cannot.
--
-- Values are NOT cached here. discover_values() still reads live data,
-- because values change with every ingestion while the SHAPE of the
-- schema changes only with a migration.
-- ============================================================

BEGIN;

DROP VIEW IF EXISTS column_catalog;
DROP VIEW IF EXISTS table_roles CASCADE;
DROP VIEW IF EXISTS schema_relationships CASCADE;

-- ── Edges, from pg_catalog directly ─────────────────────────────────
-- pg_constraint holds foreign keys as arrays of column numbers. Reading
-- them here rather than through information_schema removes the security
-- barrier that blocked join pushdown.
CREATE MATERIALIZED VIEW schema_relationships AS
WITH fk AS (
    SELECT con.conname::text        AS constraint_name,
           src.relname::text        AS from_table,
           srcatt.attname::text     AS from_column,
           tgt.relname::text        AS to_table,
           tgtatt.attname::text     AS to_column
    FROM pg_constraint con
    JOIN pg_class src     ON src.oid = con.conrelid
    JOIN pg_class tgt     ON tgt.oid = con.confrelid
    JOIN pg_namespace n   ON n.oid = src.relnamespace AND n.nspname = 'public'
    CROSS JOIN LATERAL generate_subscripts(con.conkey, 1) AS i
    JOIN pg_attribute srcatt ON srcatt.attrelid = con.conrelid
                            AND srcatt.attnum  = con.conkey[i]
    JOIN pg_attribute tgtatt ON tgtatt.attrelid = con.confrelid
                            AND tgtatt.attnum  = con.confkey[i]
    WHERE con.contype = 'f'
)
SELECT from_table, from_column, to_table, to_column,
       'child_to_parent'::text AS direction, constraint_name FROM fk
UNION ALL
SELECT to_table, to_column, from_table, from_column,
       'parent_to_child'::text, constraint_name FROM fk;

CREATE INDEX idx_screl_from ON schema_relationships (from_table, direction);
CREATE INDEX idx_screl_to   ON schema_relationships (to_table, direction);

COMMENT ON MATERIALIZED VIEW schema_relationships IS
'Every foreign key as a directed edge, both ways, read from pg_catalog. '
'This IS the join graph. Rebuilt by refresh_catalog().';

-- ── Hub cost ────────────────────────────────────────────────────────
CREATE OR REPLACE VIEW table_hub_cost AS
WITH indeg AS (
    SELECT to_table AS tbl, count(DISTINCT from_table) AS in_degree
    FROM schema_relationships WHERE direction = 'child_to_parent'
    GROUP BY to_table
)
SELECT tbl AS table_name, in_degree,
       (in_degree * (in_degree - 1))::bigint AS spurious_pairs,
       (1 + in_degree * in_degree)::bigint   AS enter_cost
FROM indeg;

-- ── Columns, from pg_catalog ────────────────────────────────────────
CREATE MATERIALIZED VIEW column_catalog AS
SELECT c.relname::text                        AS table_name,
       a.attname::text                        AS column_name,
       format_type(a.atttypid, a.atttypmod)   AS data_type,
       CASE WHEN a.atttypid IN ('text'::regtype, 'varchar'::regtype,
                                'bpchar'::regtype) THEN true ELSE false END
                                              AS is_text,
       NOT a.attnotnull                       AS nullable,
       a.attnum                               AS ordinal,
       fk.to_table                            AS references_table,
       fk.to_column                           AS references_column,
       coalesce(pk.is_pk, false)              AS is_primary_key
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
LEFT JOIN LATERAL (
    SELECT r.to_table, r.to_column FROM schema_relationships r
    WHERE r.direction = 'child_to_parent'
      AND r.from_table = c.relname::text
      AND r.from_column = a.attname::text
    LIMIT 1
) fk ON true
LEFT JOIN LATERAL (
    SELECT true AS is_pk FROM pg_constraint con
    WHERE con.conrelid = c.oid AND con.contype = 'p'
      AND a.attnum = ANY(con.conkey)
    LIMIT 1
) pk ON true
WHERE c.relkind = 'r';

CREATE INDEX idx_colcat_table ON column_catalog (table_name);
CREATE INDEX idx_colcat_col   ON column_catalog (column_name);

COMMENT ON MATERIALIZED VIEW column_catalog IS
'Every column with type, key status and foreign-key target, from '
'pg_catalog. The structural half of the planning surface.';

-- ── Fact tables and roles, now over cheap sources ───────────────────
CREATE OR REPLACE VIEW case_fact_tables AS
SELECT DISTINCT r.from_table AS table_name
FROM schema_relationships r
WHERE r.direction = 'child_to_parent'
  AND r.to_table  = 'rebuild_cases'
  AND NOT EXISTS (
      SELECT 1 FROM column_catalog c
      WHERE c.table_name = r.from_table AND c.column_name = 'doc_id');

CREATE MATERIALIZED VIEW table_roles AS
WITH cols AS (
    SELECT cc.table_name AS tn,
           count(*)                                   AS n_cols,
           bool_or(cc.column_name = 'doc_id')         AS has_doc_id,
           bool_or(cc.column_name = 'span_id')        AS has_span_id,
           bool_or(cc.column_name = 'case_id')        AS has_case_id,
           bool_or(cc.column_name IN ('content','text','chunk_text',
                                      'body','description_ur')) AS has_text
    FROM column_catalog cc GROUP BY cc.table_name
),
deg AS (
    SELECT t.tn,
           coalesce((SELECT count(DISTINCT r.from_table) FROM schema_relationships r
                      WHERE r.direction='child_to_parent' AND r.to_table = t.tn),0) AS in_deg,
           coalesce((SELECT count(DISTINCT r.to_table) FROM schema_relationships r
                      WHERE r.direction='child_to_parent' AND r.from_table = t.tn),0) AS out_deg
    FROM cols t
),
facts AS (SELECT table_name FROM case_fact_tables)
SELECT c.tn AS table_name, d.in_deg, d.out_deg, c.n_cols,
       (CASE
         WHEN d.in_deg >= 5 AND c.has_doc_id AND NOT c.has_case_id THEN 'provenance'
         WHEN c.has_doc_id AND c.has_text                          THEN 'text'
         WHEN c.tn IN (SELECT table_name FROM facts)               THEN 'fact'
         WHEN c.has_doc_id                                         THEN 'text'
         WHEN d.in_deg >= 2 AND d.out_deg <= 1                     THEN 'dimension'
         WHEN d.in_deg >= 1                                        THEN 'entity'
         WHEN c.has_span_id                                        THEN 'fact'
         ELSE 'app'
       END)::text AS role
FROM cols c JOIN deg d ON d.tn = c.tn;

CREATE UNIQUE INDEX idx_troles_table ON table_roles (table_name);

COMMENT ON MATERIALIZED VIEW table_roles IS
'Structural classification of every table. Facts are counted, dimensions '
'group, text is retrieved, provenance is cited but never joined through.';

CREATE OR REPLACE VIEW temporal_model_violations AS
SELECT f.table_name,
       string_agg(c.column_name, ', ' ORDER BY c.column_name) AS shadow_columns,
       'this fact table carries its own interval columns; facts_as_of() '
       'reads valid_from/valid_to and would ignore them' AS problem
FROM case_fact_tables f
JOIN column_catalog c ON c.table_name = f.table_name
WHERE c.column_name IN ('from_ts','to_ts','effective_from','effective_to',
                        'start_ts','end_ts','valid_start','valid_end')
GROUP BY f.table_name;

-- ── One command rebuilds everything ─────────────────────────────────
-- Must run after any migration. ANALYZE is included because routing also
-- depends on statistics, and stale statistics misroute with confidence.
CREATE OR REPLACE FUNCTION refresh_catalog()
RETURNS TABLE (step text, detail text) LANGUAGE plpgsql AS $fn$
BEGIN
    REFRESH MATERIALIZED VIEW schema_relationships;
    step := 'schema_relationships';
    detail := (SELECT count(*)::text || ' edges' FROM schema_relationships);
    RETURN NEXT;

    REFRESH MATERIALIZED VIEW column_catalog;
    step := 'column_catalog';
    detail := (SELECT count(*)::text || ' columns' FROM column_catalog);
    RETURN NEXT;

    REFRESH MATERIALIZED VIEW table_roles;
    step := 'table_roles';
    detail := (SELECT string_agg(role || '=' || n, ', ' ORDER BY role)
                 FROM (SELECT role, count(*)::text n FROM table_roles
                        GROUP BY role) x);
    RETURN NEXT;

    ANALYZE;
    step := 'analyze';
    detail := 'statistics refreshed';
    RETURN NEXT;
END;
$fn$;

COMMENT ON FUNCTION refresh_catalog() IS
'Rebuilds the derived catalog and statistics from the database. Run '
'after every migration and every bulk load. The catalog is a cache of a '
'derivation, never hand-edited: this command reproduces it exactly.';

-- ── Term lookup, now affordable ─────────────────────────────────────
DROP FUNCTION IF EXISTS search_schema(text, integer, numeric, integer);
CREATE FUNCTION search_schema(
    p_term         text,
    p_max_distinct integer DEFAULT 500,
    p_min_score    numeric DEFAULT 0.30,
    p_limit        integer DEFAULT 40
) RETURNS TABLE (
    match_kind text, table_name text, column_name text, value_raw text,
    score numeric, table_role text, exact_scan boolean, note text
) LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_skel text := normalize_name(p_term);
    r record;
BEGIN
    RETURN QUERY
    SELECT 'table'::text, tr.table_name, NULL::text, NULL::text,
           round(similarity(normalize_name(tr.table_name), v_skel)::numeric, 3),
           tr.role, false, 'table name resembles the term'::text
    FROM table_roles tr
    WHERE similarity(normalize_name(tr.table_name), v_skel) >= p_min_score;

    RETURN QUERY
    SELECT 'column'::text, cc.table_name, cc.column_name, NULL::text,
           round(similarity(normalize_name(cc.column_name), v_skel)::numeric, 3),
           tr.role, false,
           coalesce('foreign key to ' || cc.references_table,
                    'column name resembles the term')::text
    FROM column_catalog cc
    JOIN table_roles tr ON tr.table_name = cc.table_name
    WHERE similarity(normalize_name(cc.column_name), v_skel) >= p_min_score;

    -- Frequent values, straight from ANALYZE's sample. Restricted to
    -- text columns: most_common_vals on a numeric or jsonb column casts
    -- to an array that is expensive and meaningless to match a word
    -- against, which is what made the previous version time out.
    RETURN QUERY
    SELECT 'value'::text, cs.table_name, cs.column_name, v,
           round(CASE WHEN normalize_name(v) = v_skel THEN 1.0
                      ELSE similarity(normalize_name(v), v_skel) END::numeric, 3),
           tr.role, false,
           'frequent stored value; filter by this exact string'::text
    FROM column_stats cs
    JOIN table_roles tr    ON tr.table_name = cs.table_name
    JOIN column_catalog cc ON cc.table_name = cs.table_name
                          AND cc.column_name = cs.column_name
    CROSS JOIN LATERAL unnest(coalesce(cs.common_values, '{}')) AS v
    WHERE tr.role IN ('fact','dimension','entity')
      AND cc.is_text
      AND (normalize_name(v) = v_skel
           OR similarity(normalize_name(v), v_skel) >= p_min_score);

    -- Exact scan only where statistics say it is small, to catch values
    -- too rare for the sample.
    FOR r IN
        SELECT cc.table_name AS tn, cc.column_name AS cn, tr.role AS role
        FROM column_catalog cc
        JOIN table_roles tr ON tr.table_name = cc.table_name
        LEFT JOIN column_stats cs ON cs.table_name = cc.table_name
                                 AND cs.column_name = cc.column_name
        WHERE tr.role IN ('fact','dimension','entity')
          AND cc.is_text
          AND NOT cc.is_primary_key
          AND cc.references_table IS NULL
          AND coalesce(cs.rows_estimate, 0) BETWEEN 1 AND 50000
          AND coalesce(cs.distinct_ratio_est, 1) <= 0.60
    LOOP
        BEGIN
            RETURN QUERY EXECUTE format($d$
                SELECT 'value'::text, %L::text, %L::text, v::text,
                       round(CASE WHEN normalize_name(v::text) = $1 THEN 1.0
                                  ELSE similarity(normalize_name(v::text), $1)
                             END::numeric, 3),
                       %L::text, true, 'stored value, exact scan'::text
                FROM (SELECT DISTINCT %I AS v FROM %I WHERE %I IS NOT NULL) s
                WHERE normalize_name(v::text) = $1
                   OR similarity(normalize_name(v::text), $1) >= $2
            $d$, r.tn, r.cn, r.role, r.cn, r.tn, r.cn)
            USING v_skel, p_min_score;
        EXCEPTION WHEN OTHERS THEN CONTINUE;
        END;
    END LOOP;
END;
$fn$;

COMMENT ON FUNCTION search_schema(text, integer, numeric, integer) IS
'Where a term lives: table name, column name, or stored value, across '
'scripts. Reads the materialised catalog and ANALYZE''s sample first, '
'scanning exactly only where statistics say it is cheap.';

-- ── Catalog staleness ───────────────────────────────────────────────
CREATE OR REPLACE VIEW catalog_freshness AS
SELECT (SELECT count(*) FROM schema_relationships)                AS cached_edges,
       (SELECT count(*) FROM pg_constraint con
          JOIN pg_class c ON c.oid = con.conrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE con.contype='f' AND n.nspname='public') * 2        AS live_edges,
       (SELECT count(*) FROM column_catalog)                      AS cached_columns,
       (SELECT count(*) FROM pg_attribute a
          JOIN pg_class c ON c.oid = a.attrelid
          JOIN pg_namespace n ON n.oid = c.relnamespace
         WHERE n.nspname='public' AND c.relkind='r'
           AND a.attnum > 0 AND NOT a.attisdropped)               AS live_columns;

COMMENT ON VIEW catalog_freshness IS
'Cached catalog size against live catalog size. A mismatch means a '
'migration ran without refresh_catalog(), and the planner is reasoning '
'about a schema that no longer exists.';

COMMIT;
