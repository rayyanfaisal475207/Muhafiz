-- ============================================================
-- 062 — A better binding signal, because no threshold could fix the old one
--
-- WHAT CALIBRATION FOUND
--
-- Thresholds were derived by sampling real stored values, asking the
-- binder where they live, and splitting results into true matches (the
-- column the value actually came from) and false ones:
--
--     true median   0.563      false median  0.200     gap 0.363
--     floor at p95 of false matches: 0.571
--     recall at that floor:          0.444
--     VERDICT: unusable
--
-- A cut-off removing 95% of false matches loses 56% of the true ones. The
-- hand-picked 0.45 was not merely wrong — NO value of that knob is right,
-- because the distributions overlap. Tuning it was never going to work.
--
-- WHY plain similarity() FAILS HERE
--
-- similarity() is symmetric: it compares two strings as wholes. A short
-- term inside a long value scores near zero no matter how exactly it
-- appears:
--
--     similarity('چرس', 'چرس کے پانچ پیکٹ')            ~ low
--     word_similarity('چرس', 'چرس کے پانچ پیکٹ')       = 1.000
--
-- That is the shape of most real questions. "The total quantity of heroin
-- seized last year" needs a drug named INSIDE a description, not equal to
-- a category. Asking for whole-string similarity there is asking the
-- wrong question, and no threshold rescues a wrong question.
--
-- THE SIGNAL ADDED
--
-- word_similarity(term, value) measures the best match of the term against
-- any word sequence within the value. Measured on real values it returns
-- 1.000 for a term that genuinely appears and 0.000 for one that does not,
-- where whole-string similarity returned a muddle in between.
--
-- It is added as its own match kind rather than replacing the existing
-- one, because the two answer different questions:
--
--     value        the whole field IS this term        -> equality filter
--     contains     this term appears INSIDE the field  -> text predicate
--
-- Conflating them is how a filter silently matches nothing.
-- ============================================================

BEGIN;

-- Supports fast word_similarity lookups; the GIN trigram index already
-- present on name columns serves the same operator.
CREATE INDEX IF NOT EXISTS idx_seized_desc_trgm
    ON seized_items USING gin (description_ur gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_cevents_desc_trgm
    ON case_events USING gin (description_ur gin_trgm_ops);

DROP FUNCTION IF EXISTS search_schema(text, integer, numeric, integer, numeric);
CREATE FUNCTION search_schema(
    p_term         text,
    p_max_distinct integer DEFAULT 5000,
    p_min_score    numeric DEFAULT 0.30,
    p_limit        integer DEFAULT 40,
    p_cross_score  numeric DEFAULT 0.25,
    p_word_score   numeric DEFAULT 0.60
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
           round(name_score(tr.table_name, v_skel), 3),
           tr.role, false, 'table name resembles the term'::text
    FROM table_roles tr
    WHERE tr.is_evidence
      AND name_score(tr.table_name, v_skel) >= p_min_score;

    RETURN QUERY
    SELECT DISTINCT ON (cc.table_name, cc.column_name)
           'column'::text, cc.table_name, cc.column_name, NULL::text,
           round(name_score(cc.column_name, v_skel), 3),
           tr.role, false,
           coalesce('foreign key to ' || cc.references_table,
                    'column name resembles the term')::text
    FROM column_catalog cc
    JOIN table_roles tr ON tr.table_name = cc.table_name
    WHERE tr.is_evidence
      AND NOT cc.is_generated
      AND name_score(cc.column_name, v_skel) >= p_min_score;

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
          AND cc.is_text
          AND NOT cc.is_generated
          AND NOT cc.is_primary_key
          AND cc.references_table IS NULL
          AND coalesce(cs.rows_estimate, 0) BETWEEN 1 AND 200000
    LOOP
        BEGIN
            -- 1. The whole field IS the term. Equality is sound here.
            IF r.n_distinct BETWEEN 1 AND p_max_distinct THEN
                RETURN QUERY EXECUTE format($d$
                    SELECT 'value'::text, %L::text, %L::text, v::text,
                           round(CASE WHEN normalize_name(v::text) = $1 THEN 1.0
                                      ELSE similarity(normalize_name(v::text), $1)
                                 END::numeric, 3),
                           %L::text, true,
                           'the whole value; filter with equality'::text
                    FROM (SELECT DISTINCT %I AS v FROM %I WHERE %I IS NOT NULL) s
                    WHERE normalize_name(v::text) = $1
                       OR similarity(normalize_name(v::text), $1) >= $2
                $d$, r.tn, r.cn, r.role, r.cn, r.tn, r.cn)
                USING v_skel, p_min_score;
            END IF;

            -- 2. The term appears INSIDE the field. This is the shape most
            --    real questions take, and the one whole-string similarity
            --    cannot see. Runs on free-text columns too, which the
            --    distinct-count gate excludes from equality matching.
            RETURN QUERY EXECUTE format($d$
                SELECT 'contains'::text, %L::text, %L::text, v::text,
                       round(word_similarity($1, v::text)::numeric, 3),
                       %L::text, true,
                       'the term appears inside this value; match with a text predicate, not equality'::text
                FROM (SELECT DISTINCT %I AS v FROM %I
                       WHERE %I IS NOT NULL AND length(%I::text) > 0) s
                WHERE word_similarity($1, v::text) >= $2
            $d$, r.tn, r.cn, r.role, r.cn, r.tn, r.cn, r.cn)
            USING p_term, p_word_score;

            -- 3. Cross-script candidate. Lossy, labelled, never a filter
            --    on its own.
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
                USING normalize_name(p_term), p_cross_score;
            END IF;
        EXCEPTION WHEN OTHERS THEN CONTINUE;
        END;
    END LOOP;
END;
$fn$;

COMMENT ON FUNCTION search_schema(text, integer, numeric, integer, numeric, numeric) IS
'Where a term lives. Four signals, deliberately distinct: table/column '
'names, whole-value equality, word-level containment (word_similarity, '
'which finds a term inside a longer field), and cross-script candidates. '
'"value" means filter with equality; "contains" means use a text '
'predicate — conflating them is how a filter silently matches nothing.';

COMMIT;
