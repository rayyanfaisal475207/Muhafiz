-- ============================================================
-- 052 — Only evidence may answer an evidence question
--
-- WHAT THE TEST EXPOSED
--
--   SELECT * FROM search_schema('theft');
--     value | sessions | title | 'PPC Theft Section'   | 0.333
--
-- Chat session titles. The planner was being offered your colleague's
-- own conversation names as a place to find facts about theft. Answer a
-- question from that and the system reports its own history back as
-- evidence — sourced, confident, and completely circular.
--
-- table_roles called sessions an 'entity' because messages references
-- it. In-degree says a table has identity. It says nothing about whether
-- the table holds EVIDENCE.
--
-- THE STRUCTURAL DEFINITION OF EVIDENCE
--
-- Not a list of approved tables. Evidence is what can be traced to
-- source text:
--
--   a table is evidence  <=>  it reaches doc_spans through foreign keys
--
-- doc_spans is the provenance anchor: a row that reaches it can name the
-- characters in the document that assert it. A row that cannot reach it
-- has no source and therefore cannot support a claim about the world,
-- whatever it contains.
--
-- Measured: weapons reaches doc_spans in 1 hop. sessions reaches only
-- the legacy cases table and never doc_spans. The boundary falls exactly
-- where it should, and it was derived rather than drawn.
--
-- A CONSEQUENCE WORTH STATING PLAINLY
--
-- The legacy `cases` table holds the 73 real rows and has no span
-- columns, so it is NOT evidence under this rule. That is correct and
-- also inconvenient: until extraction populates rebuild_cases and its
-- fact tables, the evidence surface is nearly empty.
--
-- That is not a bug to patch by adding `cases` to an exception list —
-- doing so would reintroduce exactly the authored list this whole
-- rebuild exists to remove. An empty evidence surface is an accurate
-- report of the current state, and profile_column() already says
-- 'empty_table' rather than implying zero.
-- ============================================================

BEGIN;

-- ── Reachability to provenance, computed once ───────────────────────
CREATE MATERIALIZED VIEW evidence_tables AS
WITH RECURSIVE walk AS (
    SELECT c.table_name AS tbl,
           c.table_name AS origin,
           0            AS depth,
           c.table_name AS seen
    FROM (SELECT DISTINCT table_name FROM column_catalog) c
    UNION ALL
    SELECT r.to_table, w.origin, w.depth + 1, w.seen || '>' || r.to_table
    FROM walk w
    JOIN schema_relationships r
         ON r.from_table = w.tbl AND r.direction = 'child_to_parent'
    WHERE w.depth < 4
      AND ('>' || w.seen || '>') NOT LIKE ('%>' || r.to_table || '>%')
)
SELECT origin AS table_name,
       min(depth) AS hops_to_provenance
FROM walk
WHERE tbl = 'doc_spans'
GROUP BY origin;

CREATE UNIQUE INDEX idx_evtables ON evidence_tables (table_name);

COMMENT ON MATERIALIZED VIEW evidence_tables IS
'Tables that can trace a row to source text by reaching doc_spans '
'through foreign keys. Only these may answer a question about the '
'world; everything else is operational data.';

-- ── Roles carry the boundary ────────────────────────────────────────
DROP MATERIALIZED VIEW IF EXISTS table_roles CASCADE;

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
       END)::text AS role,
       -- The boundary. doc_spans itself counts: it IS the provenance.
       (c.tn = 'doc_spans'
        OR EXISTS (SELECT 1 FROM evidence_tables e WHERE e.table_name = c.tn))
                                                                   AS is_evidence
FROM cols c JOIN deg d ON d.tn = c.tn;

CREATE UNIQUE INDEX idx_troles_table ON table_roles (table_name);

COMMENT ON MATERIALIZED VIEW table_roles IS
'Structural classification of every table, plus is_evidence: whether a '
'row here can be traced to source text. A planner may only answer from '
'is_evidence tables.';

-- ── Search the evidence surface only ────────────────────────────────
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
    WHERE tr.is_evidence
      AND similarity(normalize_name(tr.table_name), v_skel) >= p_min_score;

    RETURN QUERY
    SELECT 'column'::text, cc.table_name, cc.column_name, NULL::text,
           round(similarity(normalize_name(cc.column_name), v_skel)::numeric, 3),
           tr.role, false,
           coalesce('foreign key to ' || cc.references_table,
                    'column name resembles the term')::text
    FROM column_catalog cc
    JOIN table_roles tr ON tr.table_name = cc.table_name
    WHERE tr.is_evidence
      AND similarity(normalize_name(cc.column_name), v_skel) >= p_min_score;

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
    WHERE tr.is_evidence AND tr.role IN ('fact','dimension','entity')
      AND cc.is_text
      AND (normalize_name(v) = v_skel
           OR similarity(normalize_name(v), v_skel) >= p_min_score);

    FOR r IN
        SELECT cc.table_name AS tn, cc.column_name AS cn, tr.role AS role
        FROM column_catalog cc
        JOIN table_roles tr ON tr.table_name = cc.table_name
        LEFT JOIN column_stats cs ON cs.table_name = cc.table_name
                                 AND cs.column_name = cc.column_name
        WHERE tr.is_evidence AND tr.role IN ('fact','dimension','entity')
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
'Where a term lives within the EVIDENCE surface only — tables that can '
'trace a row to source text. Operational data such as chat sessions is '
'structurally excluded, so the system cannot cite itself.';

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

COMMIT;
