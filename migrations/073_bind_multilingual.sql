-- ============================================================
-- 073 — Bind terms in any language
--
-- 072 built the multilingual schema catalog; nothing consulted it. This
-- makes search_schema score identifiers with name_score_multilingual, so
-- an Urdu question reaches an English table name:
--
--   'مقدمات'  ->  rebuild_cases     (was: no match at all)
--   'ہتھیار'   ->  weapons
--   'مقامات'  ->  places
--
-- The reverse direction already worked — an English question reaching
-- Urdu VALUES — which is why "cases from Lahore" answered while
-- "کتنے مقدمات" refused. Both directions now work, which is the point of
-- the system being multilingual rather than translated.
-- ============================================================

BEGIN;

DROP FUNCTION IF EXISTS search_schema(text, integer, numeric, integer, numeric, numeric);
CREATE FUNCTION search_schema(
    p_term         text,
    p_max_distinct integer DEFAULT 5000,
    p_min_score    numeric DEFAULT NULL,
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
    -- Table names, in every language the schema is known by.
    RETURN QUERY
    SELECT DISTINCT ON (tr.table_name)
           'table'::text, tr.table_name, NULL::text, NULL::text,
           round(name_score_multilingual(tr.table_name, NULL, v_skel), 3),
           tr.role, false, 'table name resembles the term'::text
    FROM table_roles tr
    WHERE tr.is_evidence
      AND name_score_multilingual(tr.table_name, NULL, v_skel) >= v_name;

    RETURN QUERY
    SELECT DISTINCT ON (cc.table_name, cc.column_name)
           'column'::text, cc.table_name, cc.column_name, NULL::text,
           round(name_score_multilingual(cc.table_name, cc.column_name, v_skel), 3),
           tr.role, false,
           coalesce('foreign key to ' || cc.references_table,
                    'column name resembles the term')::text
    FROM column_catalog cc
    JOIN table_roles tr ON tr.table_name = cc.table_name
    WHERE tr.is_evidence AND NOT cc.is_generated
      AND name_score_multilingual(cc.table_name, cc.column_name, v_skel) >= v_name;

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
'Where a term lives, in any language. Identifiers are matched through '
'schema_terms so an Urdu question reaches an English table name, and '
'values are matched across scripts so an English question reaches Urdu '
'data. Both directions, because the corpus and its users are bilingual.';

COMMIT;
