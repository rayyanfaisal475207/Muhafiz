-- ============================================================
-- 059 — Gate a value SEARCH on size, not on repetition
--
-- THE DEFECT
--
--   bind_terms(ARRAY['Karachi'])
--     -> cross_script  rebuild_cases.location_text  'کراچی ایسٹ'
--     -> nothing from places.name_raw
--
-- places holds the city کراچی, and it was not searched. Why:
--
--   places.name_raw   28 distinct / 28 rows   ratio 1.000   EXCLUDED
--   places.kind_raw    2 distinct / 28 rows   ratio 0.071   scanned
--
-- search_schema only scanned columns with distinct_ratio <= 0.60. A NAME
-- column in a dimension table is unique by nature — that is what makes it
-- a name — so the gate excluded precisely the columns most worth
-- searching, and kept the ones least worth searching.
--
-- THE CONFLATION, AGAIN
--
-- This is L15 and L28 in a third place. The ratio answers ONE question:
--
--   "may I enumerate this column as a list of categories?"
--       -> ratio matters. 64 locations in 73 rows is not a category set.
--
--   "may I search this column for a term?"
--       -> ratio is irrelevant. What matters is how many distinct values
--          must be compared, in absolute terms. Twenty-eight is free
--          whether they repeat or not.
--
-- Two different questions were sharing one threshold, so fixing either
-- broke the other. They are now separate gates, as they should have been
-- in 045 when enumerable and exhaustive were split.
--
-- THE SEARCH GATE
--
-- Scan when the number of DISTINCT values is small enough to compare
-- cheaply, whatever the table size. A 500-row lookup of unique names is
-- scanned; a 200,000-row free-text column is not, and belongs to the
-- vector and full-text layers.
--
-- Where the estimate is missing — a freshly loaded table that ANALYZE has
-- not reached — the row count stands in, because a small table is cheap
-- to scan regardless. Ingestion writes and queries run concurrently here,
-- so missing statistics are normal, not exceptional.
-- ============================================================

BEGIN;

DROP FUNCTION IF EXISTS search_schema(text, integer, numeric, integer, numeric);
CREATE FUNCTION search_schema(
    p_term         text,
    p_max_distinct integer DEFAULT 5000,   -- search gate, absolute
    p_min_score    numeric DEFAULT 0.30,
    p_limit        integer DEFAULT 40,
    p_cross_score  numeric DEFAULT 0.25
) RETURNS TABLE (
    match_kind text, table_name text, column_name text, value_raw text,
    score numeric, table_role text, exact_scan boolean, note text
) LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_skel  text := normalize_name(p_term);
    v_roman text := roman_skeleton(p_term);
    r record;
BEGIN
    RETURN QUERY
    SELECT DISTINCT ON (tr.table_name)
           'table'::text, tr.table_name, NULL::text, NULL::text,
           round(similarity(normalize_name(tr.table_name), v_skel)::numeric, 3),
           tr.role, false, 'table name resembles the term'::text
    FROM table_roles tr
    WHERE tr.is_evidence
      AND similarity(normalize_name(tr.table_name), v_skel) >= p_min_score;

    RETURN QUERY
    SELECT DISTINCT ON (cc.table_name, cc.column_name)
           'column'::text, cc.table_name, cc.column_name, NULL::text,
           round(similarity(normalize_name(cc.column_name), v_skel)::numeric, 3),
           tr.role, false,
           coalesce('foreign key to ' || cc.references_table,
                    'column name resembles the term')::text
    FROM column_catalog cc
    JOIN table_roles tr ON tr.table_name = cc.table_name
    WHERE tr.is_evidence
      AND similarity(normalize_name(cc.column_name), v_skel) >= p_min_score;

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
          -- The search gate: how many distinct values must be compared.
          -- A negative n_distinct is a fraction of rows, so it is resolved
          -- against the row estimate. A missing estimate falls back to the
          -- row count, which is what a freshly loaded table has.
          AND coalesce(
                cs.distinct_estimate,
                CASE WHEN cs.n_distinct < 0
                     THEN ceil(abs(cs.n_distinct) * greatest(cs.rows_estimate, 0))
                     ELSE cs.rows_estimate END,
                0
              ) BETWEEN 1 AND p_max_distinct
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

            IF v_roman IS NOT NULL AND p_term ~ '[A-Za-z]' THEN
                RETURN QUERY EXECUTE format($d$
                    SELECT 'cross_script'::text, %L::text, %L::text, v::text,
                           round(similarity(roman_skeleton(v::text), $1)::numeric, 3),
                           %L::text, true,
                           'cross-script candidate; confirm before filtering'::text
                    FROM (SELECT DISTINCT %I AS v FROM %I WHERE %I IS NOT NULL) s
                    WHERE v::text ~ '[؀-ۿ]'
                      AND similarity(roman_skeleton(v::text), $1) >= $2
                $d$, r.tn, r.cn, r.role, r.cn, r.tn, r.cn)
                USING normalize_name(p_term), p_cross_score;
            END IF;
        EXCEPTION WHEN OTHERS THEN CONTINUE;
        END;
    END LOOP;
END;
$fn$;

COMMENT ON FUNCTION search_schema(text, integer, numeric, integer, numeric) IS
'Where a term lives within the evidence surface. The value scan is gated '
'on DISTINCT COUNT, not on repetition: a column of unique names is the '
'most searchable thing in the schema, and the ratio gate had excluded '
'exactly those. Enumeration is gated separately, by profile_column.';

-- ── Deduplicate bindings ────────────────────────────────────────────
-- The same value can arrive from the statistics sample and from the exact
-- scan. One stored value is one binding.
CREATE OR REPLACE FUNCTION bind_terms(
    p_terms     text[],
    p_min_score numeric DEFAULT 0.45,
    p_per_term  integer DEFAULT 4
) RETURNS TABLE (
    term        text,
    rank        integer,
    match_kind  text,
    table_name  text,
    column_name text,
    value_raw   text,
    score       numeric,
    table_role  text,
    ambiguous   boolean
) LANGUAGE sql STABLE AS $fn$
    WITH raw AS (
        SELECT DISTINCT ON (t.term, s.table_name, s.column_name,
                            coalesce(s.value_raw, ''))
               t.term, s.match_kind, s.table_name, s.column_name,
               s.value_raw, s.score, s.table_role
        FROM unnest(p_terms) AS t(term)
        CROSS JOIN LATERAL search_schema(t.term, 5000, p_min_score, 20) s
        ORDER BY t.term, s.table_name, s.column_name,
                 coalesce(s.value_raw, ''), s.score DESC
    ),
    hits AS (
        SELECT r.*,
               row_number() OVER (PARTITION BY r.term
                                  ORDER BY r.score DESC,
                                           CASE r.match_kind
                                                WHEN 'value'        THEN 0
                                                WHEN 'column'       THEN 1
                                                WHEN 'cross_script' THEN 2
                                                ELSE 3 END,
                                           r.table_name)             AS rn,
               max(r.score) OVER (PARTITION BY r.term)               AS best,
               nth_value(r.score, 2) OVER (
                   PARTITION BY r.term ORDER BY r.score DESC
                   ROWS BETWEEN UNBOUNDED PRECEDING AND UNBOUNDED FOLLOWING
               )                                                     AS runner_up
        FROM raw r
    )
    SELECT term, rn::integer, match_kind, table_name, column_name,
           value_raw, score, table_role,
           (runner_up IS NOT NULL AND best > 0
            AND runner_up >= best * 0.90)                            AS ambiguous
    FROM hits
    WHERE rn <= p_per_term
    ORDER BY term, rn;
$fn$;

COMMIT;
