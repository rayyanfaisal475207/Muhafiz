-- ============================================================
-- 058 — Bind an English term to an Urdu value
--
-- THE MEASURED GAP
--
--   bind_terms(ARRAY['Karachi'])  ->  no rows
--
-- The corpus stores 'کراچی'. normalize_name() folds ORTHOGRAPHY — case,
-- diacritics, Arabic letter variants — and that is all it claims to do.
-- It cannot cross scripts, so an English question never reaches Urdu data.
--
-- This is L1 reappearing in the retrieval layer: the model filtering
-- "unlicensed" against 'بغیر لائسنس' and returning 0 where the truth was
-- 30. Same failure, one layer up.
--
-- WHAT WAS MEASURED, INCLUDING WHERE IT FAILS
--
-- Consonant-skeleton romanisation, against the real city names:
--
--   لاہور      -> lahor      vs lahore       0.63
--   اسلام آباد -> aslamabad  vs islamabad    0.54
--   کراچی      -> krachi     vs karachi      0.50
--   ملتان      -> mltan      vs multan       0.44
--   فیصل آباد  -> fislabad   vs faisalabad   0.33
--   راولپنڈی   -> raolpndi   vs rawalpindi   0.25
--
-- A threshold that catches Rawalpindi catches almost anything. So this is
-- NOT a matcher. Urdu script omits short vowels, so the skeleton is
-- lossy in a way trigram similarity cannot recover reliably.
--
-- HOW IT IS USED INSTEAD
--
-- As a CANDIDATE GENERATOR at a deliberately low threshold, with its
-- matches labelled 'cross_script' and scored below same-script matches.
-- Measured at 0.25 against the live corpus, each of Faisalabad, Multan
-- and Rawalpindi matched exactly one place and nothing else — precision
-- holds while the candidate set is small.
--
-- That will degrade as places grow, which is why the label matters: a
-- cross_script binding is weaker evidence, and a planner seeing more than
-- one of them for a term must confirm rather than choose. Confirmation is
-- world knowledge — an LLM or web lookup knows Karachi is کراچی — and is
-- the same path already reserved for "what does CNSA 1997 mean".
--
-- This is writing-system knowledge, like normalize_name, not a corpus
-- vocabulary: no place name, weapon type or status appears anywhere here.
-- ============================================================

BEGIN;

DROP FUNCTION IF EXISTS roman_skeleton(text);
CREATE FUNCTION roman_skeleton(p_text text)
RETURNS text LANGUAGE sql IMMUTABLE AS $fn$
    SELECT nullif(regexp_replace(
        translate(
            -- Digraphs first: these are single Urdu letters that romanise
            -- to two Latin characters, so translate() cannot do them.
            regexp_replace(
                regexp_replace(
                    regexp_replace(
                        regexp_replace(
                            regexp_replace(lower(coalesce(p_text, '')),
                                           'چ', 'ch', 'g'),
                            'خ', 'kh', 'g'),
                        'ش', 'sh', 'g'),
                    'غ', 'gh', 'g'),
                'ژ', 'zh', 'g'),
            -- Remaining letters, one Latin character each. Emphatic and
            -- retroflex pairs collapse together (ٹ/ت, ڈ/د, ڑ/ر), which is
            -- what makes this a skeleton rather than a transliteration.
            'ابپتٹثجحدڈذرڑزسصضطظعفقکگلمنںوہھءیےآأإة',
            'abptts'  ||
            'jhddzrrzsszzzafqkglmnnohh' ||
            'iiaaah'),
        '[^a-z]+', '', 'g'), '');
$fn$;

COMMENT ON FUNCTION roman_skeleton(text) IS
'Consonant skeleton of Arabic-script text, for generating cross-script '
'CANDIDATES. Lossy by construction — Urdu omits short vowels — so its '
'matches are labelled cross_script and are never treated as confirmation.';

-- ── search_schema gains a cross-script channel ──────────────────────
DROP FUNCTION IF EXISTS search_schema(text, integer, numeric, integer);
CREATE FUNCTION search_schema(
    p_term         text,
    p_max_distinct integer DEFAULT 500,
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
            -- Same-script: exact, then trigram.
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

            -- Cross-script: only for a Latin term against Arabic-script
            -- data, and scored strictly below same-script so exact always
            -- wins. Labelled so a planner knows to confirm it.
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
'Where a term lives within the evidence surface: table name, column name, '
'stored value, or cross-script candidate. A cross_script match is a '
'CANDIDATE only — romanisation is lossy and must be confirmed before it '
'becomes a filter.';

COMMIT;
