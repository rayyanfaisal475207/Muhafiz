-- ============================================================
-- 063 — Thresholds the data chose, and one channel the data deleted
--
-- WHAT CALIBRATION MEASURED
--
-- Real stored values were run through the real binder, and every match was
-- VERIFIED against the database — does this term actually occur in that
-- column — rather than assumed correct only in the column it was drawn
-- from. Per match kind:
--
--   contains   floor 0.750   recall 0.807   true med 1.000  false med 0.651
--              -> clean separation, stored
--
--   value      floor 0.364   recall 0.412   true med 0.267  false med 0.214
--              -> gap of 0.053. UNUSABLE.
--
-- THE FUZZY VALUE CHANNEL IS NOISE, SO IT GOES
--
-- A gap of 0.053 between true and false means whole-string trigram
-- similarity carries almost no information about whether a term belongs to
-- a column. Every cut-off trades one error for the other. The honest
-- response is not a cleverer threshold — it is to stop asking a question
-- the signal cannot answer.
--
-- So `value` becomes EXACT ONLY. A value match now means the field equals
-- the term, normalised for script and case, and nothing else. Partial
-- matching moves entirely to `contains`, which measures the right thing
-- (word_similarity: does this term appear as a word inside the field) and
-- separates cleanly at 0.750.
--
-- This removes a whole class of silent failure. A fuzzy value match at
-- 0.4 previously compiled into `column = 'term'` — an equality filter
-- built on a guess, which returns zero rows and looks like an answer.
--
-- WHY PER-KIND AND NOT GLOBAL
--
-- A single global floor was measured twice and failed twice: removing 95%
-- of false matches cost 40% of true ones. Exact equality at 1.0 and a
-- name resemblance at 0.5 are not points on one axis. They are different
-- kinds of evidence and need different bars.
--
-- Thresholds are read from derived_thresholds at query time, so
-- re-running the calibration changes behaviour with no code edit. The
-- defaults below are the measured values, used when that table is absent.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS derived_thresholds (
    name          text PRIMARY KEY,
    value         numeric NOT NULL,
    measured_at   timestamptz NOT NULL DEFAULT now(),
    sample_size   integer,
    recall        numeric,
    method        text,
    notes         text
);

COMMENT ON TABLE derived_thresholds IS
'Thresholds measured from the data, with the sample behind each. A '
'threshold derived on 73 cases is not evidence about 73,000 — re-run '
'scripts/calibrate_thresholds.py when the corpus changes size.';

CREATE OR REPLACE FUNCTION threshold(p_name text, p_default numeric)
RETURNS numeric LANGUAGE sql STABLE AS $fn$
    SELECT coalesce(
        (SELECT value FROM derived_thresholds WHERE name = p_name),
        p_default);
$fn$;

COMMENT ON FUNCTION threshold(text, numeric) IS
'A measured threshold, falling back to the value measured at the time this '
'migration was written. Re-calibration changes behaviour without a code '
'change.';

DROP FUNCTION IF EXISTS search_schema(text, integer, numeric, integer, numeric, numeric);
CREATE FUNCTION search_schema(
    p_term         text,
    p_max_distinct integer DEFAULT 5000,
    p_min_score    numeric DEFAULT NULL,   -- name matching
    p_limit        integer DEFAULT 40,
    p_cross_score  numeric DEFAULT NULL,
    p_word_score   numeric DEFAULT NULL
) RETURNS TABLE (
    match_kind text, table_name text, column_name text, value_raw text,
    score numeric, table_role text, exact_scan boolean, note text
) LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_skel  text := normalize_name(p_term);
    v_roman text := roman_skeleton(p_term);
    v_name  numeric := coalesce(p_min_score,  threshold('min_score.name', 0.30));
    v_word  numeric := coalesce(p_word_score, threshold('min_score.contains', 0.75));
    v_cross numeric := coalesce(p_cross_score, threshold('min_score.cross_script', 0.25));
    r record;
BEGIN
    RETURN QUERY
    SELECT DISTINCT ON (tr.table_name)
           'table'::text, tr.table_name, NULL::text, NULL::text,
           round(name_score(tr.table_name, v_skel), 3),
           tr.role, false, 'table name resembles the term'::text
    FROM table_roles tr
    WHERE tr.is_evidence AND name_score(tr.table_name, v_skel) >= v_name;

    RETURN QUERY
    SELECT DISTINCT ON (cc.table_name, cc.column_name)
           'column'::text, cc.table_name, cc.column_name, NULL::text,
           round(name_score(cc.column_name, v_skel), 3),
           tr.role, false,
           coalesce('foreign key to ' || cc.references_table,
                    'column name resembles the term')::text
    FROM column_catalog cc
    JOIN table_roles tr ON tr.table_name = cc.table_name
    WHERE tr.is_evidence AND NOT cc.is_generated
      AND name_score(cc.column_name, v_skel) >= v_name;

    FOR r IN
        SELECT cc.table_name AS tn, cc.column_name AS cn, tr.role AS role,
               coalesce(
                   cs.distinct_estimate,
                   CASE WHEN cs.n_distinct < 0
                        THEN ceil(abs(cs.n_distinct) * greatest(cs.rows_estimate, 0))
                        ELSE cs.rows_estimate END, 0) AS n_distinct
        FROM column_catalog cc
        JOIN table_roles tr ON tr.table_name = cc.table_name
        LEFT JOIN column_stats cs ON cs.table_name = cc.table_name
                                 AND cs.column_name = cc.column_name
        WHERE tr.is_evidence AND tr.role IN ('fact','dimension','entity')
          AND cc.is_text AND NOT cc.is_generated AND NOT cc.is_primary_key
          AND cc.references_table IS NULL
          AND coalesce(cs.rows_estimate, 0) BETWEEN 1 AND 200000
    LOOP
        BEGIN
            -- 1. EXACT only. Measured gap between true and false fuzzy
            --    value matches was 0.053 — no information. A fuzzy match
            --    here used to compile into an equality filter built on a
            --    guess, which returns zero rows and looks like an answer.
            IF r.n_distinct BETWEEN 1 AND p_max_distinct THEN
                RETURN QUERY EXECUTE format($d$
                    SELECT 'value'::text, %L::text, %L::text, v::text,
                           1.0::numeric, %L::text, true,
                           'the field equals this term; filter with equality'::text
                    FROM (SELECT DISTINCT %I AS v FROM %I WHERE %I IS NOT NULL) s
                    WHERE normalize_name(v::text) = $1
                $d$, r.tn, r.cn, r.role, r.cn, r.tn, r.cn)
                USING v_skel;
            END IF;

            -- 2. The term appears inside the field. Separates cleanly at
            --    the measured floor, and works on free text, which the
            --    distinct-count gate excludes from equality matching.
            RETURN QUERY EXECUTE format($d$
                SELECT 'contains'::text, %L::text, %L::text, v::text,
                       round(word_similarity($1, v::text)::numeric, 3),
                       %L::text, true,
                       'the term appears inside this value; use a text predicate'::text
                FROM (SELECT DISTINCT %I AS v FROM %I
                       WHERE %I IS NOT NULL AND length(%I::text) > 0) s
                WHERE word_similarity($1, v::text) >= $2
            $d$, r.tn, r.cn, r.role, r.cn, r.tn, r.cn, r.cn)
            USING p_term, v_word;

            IF v_roman IS NOT NULL AND p_term ~ '[A-Za-z]'
               AND r.n_distinct BETWEEN 1 AND p_max_distinct THEN
                RETURN QUERY EXECUTE format($d$
                    SELECT 'cross_script'::text, %L::text, %L::text, v::text,
                           round(similarity(roman_skeleton(v::text), $1)::numeric, 3),
                           %L::text, true,
                           'cross-script candidate; confirm before filtering'::text
                    FROM (SELECT DISTINCT %I AS v FROM %I WHERE %I IS NOT NULL) s
                    WHERE v::text ~ '[؀-ۿ]'
                      AND similarity(roman_skeleton(v::text), $1) >= $2
                $d$, r.tn, r.cn, r.role, r.cn, r.tn, r.cn)
                USING normalize_name(p_term), v_cross;
            END IF;
        EXCEPTION WHEN OTHERS THEN CONTINUE;
        END;
    END LOOP;
END;
$fn$;

COMMENT ON FUNCTION search_schema(text, integer, numeric, integer, numeric, numeric) IS
'Where a term lives. Four kinds of evidence, each with its own measured '
'bar: exact value equality, word-level containment (floor 0.75, measured), '
'identifier resemblance, and cross-script candidates. Fuzzy whole-value '
'matching was removed — measured separation was 0.053, which is noise.';

COMMIT;
