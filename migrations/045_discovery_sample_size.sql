-- ============================================================
-- 045 — Separate "can I enumerate" from "is my list complete"
--
-- THE REGRESSION 044 INTRODUCED
--
-- 044 checked small-sample BEFORE ratio:
--
--     IF non_null < 2 * distinct  -> insufficient_evidence
--
-- For cases.location (64 distinct in 73 rows) that fires, because
-- 73 < 128. So a column that 039 classified correctly as free_text was
-- downgraded to insufficient_evidence. But 73 draws producing 64
-- distinct values is not weak evidence — it is STRONG evidence of free
-- text. The test was backwards: it treated a high distinct count as a
-- reason to distrust the ratio, when a high distinct count is precisely
-- what the ratio is supposed to detect.
--
-- Caught only because cases.location had a known-correct answer from
-- earlier measurement and was re-tested. A new column would have carried
-- the wrong verdict silently.
--
-- THE REAL DISCRIMINATOR IS ABSOLUTE SAMPLE SIZE
--
-- What makes places.kind_raw (5 distinct in 7 rows) unjudgeable is not
-- its ratio, it is that SEVEN ROWS cannot establish anything. What makes
-- cases.location judgeable is 73 rows. The threshold belongs on the
-- sample, not on the relationship between sample and distinct count.
--
-- n >= 30 is a convention borrowed from sampling practice, not a
-- derivation from this data. Stating that plainly: it is the one number
-- here that is a judgement call rather than a measurement, it is exposed
-- as a parameter, and every verdict reports the n it was computed from
-- so a caller can disagree.
--
-- TWO DIFFERENT QUESTIONS, CONFLATED UNTIL NOW
--
--   1. Can I enumerate this column?      -> is distinct count small?
--   2. Is that enumeration complete?     -> did values repeat, over
--                                           enough rows to tell?
--
-- 039 and 044 answered only the first and returned nothing when the
-- answer was no. But a planner offered 20 observed values with
-- "this list may not be complete" is far better served than a planner
-- offered nothing at all. So values are now returned whenever they are
-- few enough to be useful, and `exhaustive` answers the second question
-- separately.
-- ============================================================

BEGIN;

DROP FUNCTION IF EXISTS profile_column(text, text);
DROP FUNCTION IF EXISTS discover_values(text, text, integer, numeric);
DROP FUNCTION IF EXISTS profile_all_columns(text);

CREATE FUNCTION profile_column(
    p_table      text,
    p_column     text,
    p_min_sample integer DEFAULT 30,
    p_max_ratio  numeric DEFAULT 0.40,
    p_max_enum   integer DEFAULT 500
) RETURNS TABLE (
    distinct_count bigint,
    row_count      bigint,
    non_null_count bigint,
    null_rate      numeric,
    distinct_ratio numeric,
    enumerable     boolean,
    exhaustive     boolean,
    verdict        text,
    reason         text
) LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_distinct bigint;
    v_rows     bigint;
    v_nonnull  bigint;
    v_ratio    numeric;
BEGIN
    EXECUTE format(
        'SELECT count(DISTINCT %I), count(*), count(%I) FROM %I',
        p_column, p_column, p_table
    ) INTO v_distinct, v_rows, v_nonnull;

    v_ratio := CASE WHEN v_nonnull > 0
                    THEN round(v_distinct::numeric / v_nonnull, 4) END;

    enumerable := v_distinct > 0 AND v_distinct <= p_max_enum;
    exhaustive := false;

    IF v_rows = 0 THEN
        verdict := 'empty_table';
        reason  := 'the table holds no rows; a count over it is not zero '
                   'evidence but no evidence';
    ELSIF v_nonnull = 0 THEN
        verdict := 'all_null';
        reason  := format('all %s rows are null here; a filter on this '
                          'column discards the entire population', v_rows);
    ELSIF v_distinct > p_max_enum THEN
        verdict := 'high_cardinality';
        reason  := format('%s distinct values exceeds the enumeration '
                          'limit of %s; match by search, not equality',
                          v_distinct, p_max_enum);
    -- Sample size is checked BEFORE the ratio, but only against an
    -- absolute floor. A high distinct count no longer suppresses the
    -- ratio test — that was the 044 regression.
    ELSIF v_nonnull < p_min_sample THEN
        verdict := 'insufficient_evidence';
        reason  := format('%s distinct values in only %s non-null rows: '
                          'below the %s-row floor, the distinct ratio '
                          'cannot separate a category set from free text',
                          v_distinct, v_nonnull, p_min_sample);
    ELSIF v_ratio > p_max_ratio THEN
        verdict := 'free_text';
        reason  := format('%s distinct across %s non-null rows (ratio %s): '
                          'values seldom repeat over an adequate sample, '
                          'which is how free text behaves',
                          v_distinct, v_nonnull, v_ratio);
    ELSE
        verdict := 'categorical';
        exhaustive := true;
        reason  := format('%s distinct across %s non-null rows (ratio %s): '
                          'values repeat over an adequate sample, so '
                          'equality filtering is sound and this list is '
                          'complete', v_distinct, v_nonnull, v_ratio);
    END IF;

    distinct_count := v_distinct;
    row_count      := v_rows;
    non_null_count := v_nonnull;
    null_rate      := CASE WHEN v_rows = 0 THEN NULL
                           ELSE round((v_rows - v_nonnull)::numeric / v_rows, 4) END;
    distinct_ratio := v_ratio;
    RETURN NEXT;
END;
$fn$;

COMMENT ON FUNCTION profile_column(text, text, integer, numeric, integer) IS
'Live column profile. enumerable answers "are there few enough values to '
'list"; exhaustive answers "is that list complete", which needs an '
'adequate sample. Both are reported with the n behind them so a caller '
'can disagree with the thresholds.';

-- ── Values, with honest limits ──────────────────────────────────────
CREATE FUNCTION discover_values(
    p_table        text,
    p_column       text,
    p_max_distinct integer DEFAULT 500,
    p_max_ratio    numeric DEFAULT 0.40,
    p_min_sample   integer DEFAULT 30,
    p_limit        integer DEFAULT 50
) RETURNS TABLE (
    value_raw   text,
    frequency   bigint,
    script      text,
    verdict     text,
    exhaustive  boolean
) LANGUAGE plpgsql STABLE AS $fn$
DECLARE p record;
BEGIN
    SELECT * INTO p FROM profile_column(
        p_table, p_column, p_min_sample, p_max_ratio, p_max_distinct);

    -- Nothing to enumerate. Return a labelled marker rather than a bare
    -- empty set, so "never loaded" is distinguishable from "none exist".
    IF NOT p.enumerable THEN
        value_raw := NULL; frequency := NULL; script := NULL;
        verdict := p.verdict; exhaustive := false;
        RETURN NEXT;
        RETURN;
    END IF;

    -- Values are returned for every enumerable column, including
    -- free_text and insufficient_evidence ones. They are real observed
    -- values and useful to a planner; `exhaustive` carries the caveat.
    -- Capped at p_limit so a 400-value column cannot flood a prompt —
    -- and when capped, exhaustive is false by construction.
    RETURN QUERY EXECUTE format($f$
        SELECT %I::text,
               count(*)::bigint,
               CASE WHEN %I::text ~ '[؀-ۿ]'         THEN 'arabic'
                    WHEN %I::text ~ '^[0-9\s\-/]+$' THEN 'digit'
                    WHEN %I::text ~ '[A-Za-z]'      THEN 'latin'
                    ELSE 'mixed' END,
               %L::text,
               (%L::boolean AND %s >= (SELECT count(DISTINCT %I) FROM %I))
        FROM %I
        WHERE %I IS NOT NULL AND btrim(%I::text) <> ''
        GROUP BY 1 ORDER BY 2 DESC, 1
        LIMIT %s
    $f$, p_column, p_column, p_column, p_column,
         p.verdict, p.exhaustive, p_limit, p_column, p_table,
         p_table, p_column, p_column, p_limit);
END;
$fn$;

COMMENT ON FUNCTION discover_values(text, text, integer, numeric, integer, integer) IS
'Live enumeration of observed values, capped and labelled. Returns '
'values even when the list is not provably complete — a planner given '
'20 real values plus a caveat beats a planner given nothing — and '
'exhaustive=true only when the sample supports it and nothing was cut.';

-- ── Whole-schema profile, updated for the new signature ─────────────
CREATE FUNCTION profile_all_columns(
    p_schema text DEFAULT 'public'
) RETURNS TABLE (
    table_name     text,
    column_name    text,
    data_type      text,
    distinct_count bigint,
    row_count      bigint,
    null_rate      numeric,
    enumerable     boolean,
    exhaustive     boolean,
    verdict        text
) LANGUAGE plpgsql STABLE AS $fn$
DECLARE r record; p record;
BEGIN
    FOR r IN
        SELECT c.table_name AS tn, c.column_name AS cn, c.data_type AS dt
        FROM information_schema.columns c
        JOIN information_schema.tables t
             ON t.table_schema = c.table_schema
            AND t.table_name   = c.table_name
            AND t.table_type   = 'BASE TABLE'
        WHERE c.table_schema = p_schema
        ORDER BY c.table_name, c.ordinal_position
    LOOP
        BEGIN
            SELECT * INTO p FROM profile_column(r.tn, r.cn);
            table_name := r.tn; column_name := r.cn; data_type := r.dt;
            distinct_count := p.distinct_count; row_count := p.row_count;
            null_rate := p.null_rate; enumerable := p.enumerable;
            exhaustive := p.exhaustive; verdict := p.verdict;
            RETURN NEXT;
        EXCEPTION WHEN OTHERS THEN
            table_name := r.tn; column_name := r.cn; data_type := r.dt;
            distinct_count := NULL; row_count := NULL; null_rate := NULL;
            enumerable := NULL; exhaustive := NULL;
            verdict := 'unprofilable';
            RETURN NEXT;
        END;
    END LOOP;
END;
$fn$;

COMMENT ON FUNCTION profile_all_columns(text) IS
'Profiles every column of every base table live, storing nothing. '
'Build-order stage 1.';

COMMIT;
