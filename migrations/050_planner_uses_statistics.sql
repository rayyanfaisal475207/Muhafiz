-- ============================================================
-- 050 — Plan from statistics, answer from data
--
-- THE DEFECT
--
-- search_schema() timed out. To find where a term lives it profiled
-- every text column with count(DISTINCT) — including audit_logs at
-- 167,000 rows — then ran similarity over every distinct value. Correct,
-- and useless: a planner calls this on EVERY question, and a routing
-- step that costs full table scans is not a routing step.
--
-- WHAT WAS BEING REBUILT BY HAND
--
-- Postgres already maintains this. ANALYZE writes, per column:
--
--   n_distinct        7        (positive: an absolute count)
--   n_distinct       -0.2603   (negative: a FRACTION of rows)
--   null_frac
--   most_common_vals  the frequent values, already materialised
--
-- Measured against the profiling built in 039-045:
--
--   cases.police_station   pg_stats -0.2603   measured ratio 0.2603
--   cases.location         pg_stats -0.8767   measured ratio 0.8767
--
-- Identical. The distinct ratio the cardinality gate is built on is a
-- number Postgres computes on its own schedule, and most_common_vals
-- answers "what values occur here" with no scan at all.
--
-- THE DISTINCTION THAT MATTERS
--
-- pg_stats is a SAMPLE. It is an estimate, and estimates are exactly
-- right for choosing which table to look at and exactly wrong for
-- telling a user a number. Presenting a sampled n_distinct as "there are
-- 19 police stations" would be the confident-wrong-number failure with
-- extra steps.
--
--   ROUTING   -> statistics. Fast, approximate, never shown to a user.
--   ANSWERING -> profile_column() and real SQL. Exact, slower, cited.
--
-- Both layers stay. What changes is that the fast one runs first and
-- narrows the field, so the exact one runs on three columns instead of
-- four hundred.
-- ============================================================

BEGIN;

-- ── Postgres's own measurements, in usable form ─────────────────────
CREATE OR REPLACE VIEW column_stats AS
SELECT s.tablename::text  AS table_name,
       s.attname::text    AS column_name,
       s.null_frac,
       s.n_distinct,
       -- Normalise the sign convention into one comparable number.
       CASE WHEN s.n_distinct < 0 THEN NULL
            ELSE s.n_distinct::bigint END                   AS distinct_estimate,
       CASE WHEN s.n_distinct < 0 THEN abs(s.n_distinct)::numeric
            WHEN c.reltuples > 0 THEN (s.n_distinct / c.reltuples)::numeric
            ELSE NULL END                                   AS distinct_ratio_est,
       c.reltuples::bigint                                  AS rows_estimate,
       s.most_common_vals::text::text[]                     AS common_values,
       s.most_common_freqs                                  AS common_freqs
FROM pg_stats s
JOIN pg_class c     ON c.relname   = s.tablename
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = s.schemaname
WHERE s.schemaname = 'public' AND c.relkind = 'r';

COMMENT ON VIEW column_stats IS
'Postgres''s own per-column statistics, sign convention normalised. '
'ESTIMATES from a sample: use for routing a query, never as an answer. '
'Refreshed by ANALYZE, at no query cost.';

-- ── Where does this term live: statistics first ─────────────────────
-- Same contract as before, but the value scan now reads most_common_vals
-- rather than the tables. Columns whose stats say they are small enough
-- are additionally scanned exactly, so a rare value is still found —
-- bounded by estimated distinct count and estimated table size, both
-- free to check.
DROP FUNCTION IF EXISTS search_schema(text, integer, numeric, integer);
CREATE FUNCTION search_schema(
    p_term         text,
    p_max_distinct integer DEFAULT 500,
    p_min_score    numeric DEFAULT 0.30,
    p_limit        integer DEFAULT 40
) RETURNS TABLE (
    match_kind   text,
    table_name   text,
    column_name  text,
    value_raw    text,
    score        numeric,
    table_role   text,
    exact_scan   boolean,
    note         text
) LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_skel text := normalize_name(p_term);
    r      record;
BEGIN
    -- 1. Names. Free.
    RETURN QUERY
    SELECT 'table'::text, tr.table_name, NULL::text, NULL::text,
           round(similarity(normalize_name(tr.table_name), v_skel)::numeric, 3),
           tr.role, false,
           'table name resembles the term'::text
    FROM table_roles tr
    WHERE similarity(normalize_name(tr.table_name), v_skel) >= p_min_score;

    RETURN QUERY
    SELECT 'column'::text, cc.table_name, cc.column_name, NULL::text,
           round(similarity(normalize_name(cc.column_name), v_skel)::numeric, 3),
           cc.table_role, false,
           coalesce('foreign key to ' || cc.references_table,
                    'column name resembles the term')::text
    FROM column_catalog cc
    WHERE similarity(normalize_name(cc.column_name), v_skel) >= p_min_score;

    -- 2. Values already materialised by ANALYZE. Still free.
    RETURN QUERY
    SELECT 'value'::text, cs.table_name, cs.column_name, v,
           round(CASE WHEN normalize_name(v) = v_skel THEN 1.0
                      ELSE similarity(normalize_name(v), v_skel) END::numeric, 3),
           tr.role, false,
           'frequent stored value; filter this column by this exact string'::text
    FROM column_stats cs
    JOIN table_roles tr ON tr.table_name = cs.table_name
    CROSS JOIN LATERAL unnest(coalesce(cs.common_values, '{}')) AS v
    WHERE tr.role IN ('fact','dimension','entity')
      AND (normalize_name(v) = v_skel
           OR similarity(normalize_name(v), v_skel) >= p_min_score);

    -- 3. Exact scan, only where statistics say it is cheap. This is what
    --    finds a value too rare to appear in most_common_vals.
    FOR r IN
        SELECT cs.table_name AS tn, cs.column_name AS cn
        FROM column_stats cs
        JOIN table_roles tr ON tr.table_name = cs.table_name
        JOIN column_catalog cc ON cc.table_name = cs.table_name
                              AND cc.column_name = cs.column_name
        WHERE tr.role IN ('fact','dimension','entity')
          AND cc.data_type IN ('text','character varying','character')
          AND NOT cc.is_primary_key
          AND cc.references_table IS NULL
          AND cs.rows_estimate <= 50000
          AND (cs.distinct_estimate IS NULL
               OR cs.distinct_estimate <= p_max_distinct)
          AND coalesce(cs.distinct_ratio_est, 1) <= 0.60
    LOOP
        BEGIN
            RETURN QUERY EXECUTE format($d$
                SELECT 'value'::text, %L::text, %L::text, v::text,
                       round(CASE WHEN normalize_name(v::text) = $1 THEN 1.0
                                  ELSE similarity(normalize_name(v::text), $1)
                             END::numeric, 3),
                       %L::text, true,
                       'stored value found by exact scan'::text
                FROM (SELECT DISTINCT %I AS v FROM %I WHERE %I IS NOT NULL) s
                WHERE normalize_name(v::text) = $1
                   OR similarity(normalize_name(v::text), $1) >= $2
            $d$, r.tn, r.cn,
                 (SELECT role FROM table_roles WHERE table_name = r.tn),
                 r.cn, r.tn, r.cn)
            USING v_skel, p_min_score;
        EXCEPTION WHEN OTHERS THEN
            CONTINUE;
        END;
    END LOOP;
END;
$fn$;

COMMENT ON FUNCTION search_schema(text, integer, numeric, integer) IS
'Where a term lives: table name, column name, or stored value, across '
'scripts. Routes through pg_stats first and scans exactly only where '
'statistics say it is cheap, so it is fast enough to call per question.';

-- ── Stats freshness, because a stale estimate routes wrongly ────────
-- An estimate is only as good as the last ANALYZE. After a bulk load,
-- statistics describe the previous corpus, which would route a planner
-- to the wrong columns with complete confidence.
CREATE OR REPLACE VIEW stats_freshness AS
SELECT c.relname::text AS table_name,
       c.reltuples::bigint AS rows_estimate,
       s.n_live_tup       AS rows_live,
       s.last_analyze,
       s.last_autoanalyze,
       greatest(coalesce(s.last_analyze,   '-infinity'::timestamptz),
                coalesce(s.last_autoanalyze,'-infinity'::timestamptz)) AS analyzed_at,
       (s.n_live_tup > 0
        AND abs(c.reltuples - s.n_live_tup) / greatest(s.n_live_tup,1)::numeric > 0.20)
                          AS stats_stale
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
LEFT JOIN pg_stat_user_tables s ON s.relid = c.oid
WHERE c.relkind = 'r';

COMMENT ON VIEW stats_freshness IS
'Whether pg_stats still describes the current data. stats_stale means '
'routing decisions based on statistics are unreliable and ANALYZE '
'should run — after every bulk load, before any planning.';

COMMIT;
