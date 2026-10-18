-- ============================================================
-- 061 — A table's prefix is not part of its meaning
--
-- THE DEFECT, MEASURED
--
--   search_schema('cases')  ->  rebuild_cases   score 0.429
--
-- Below the 0.45 binding threshold, so the word "cases" bound to NOTHING
-- and every question about cases escalated and refused:
--
--   "Which police station handles the most cases?"  -> REFUSED
--   "How many cases have a weapon linked to them?"  -> REFUSED
--
-- The cause is a naming artefact of this rebuild. `rebuild_cases` exists
-- only because the legacy `cases` table still occupies the name. Trigram
-- similarity between "cases" and "rebuild_cases" is diluted by eight
-- characters that carry no meaning about the data.
--
-- THE FIX, DERIVED NOT NAMED
--
-- Score a table name both whole and with its leading `word_` segment
-- removed, and keep the better score. Nothing names "rebuild" here: the
-- rule is that a leading underscore-delimited segment is a NAMESPACE, and
-- a namespace is not what a table is about. The same rule handles tbl_,
-- dim_, fact_, stg_ and any other convention a future source arrives with.
--
-- The full name still scores too, so a table genuinely called "rebuild"
-- would still match that word.
-- ============================================================

BEGIN;

CREATE OR REPLACE FUNCTION name_variants(p_name text)
RETURNS TABLE (variant text) LANGUAGE sql IMMUTABLE AS $fn$
    SELECT DISTINCT v FROM (
        VALUES
            (p_name),
            -- Leading namespace segment removed: rebuild_cases -> cases
            (nullif(regexp_replace(p_name, '^[a-z]+_', ''), p_name)),
            -- Underscores as spaces, so "police_station" matches the
            -- two-word phrase a person actually types.
            (replace(p_name, '_', ' ')),
            (replace(nullif(regexp_replace(p_name, '^[a-z]+_', ''), p_name),
                     '_', ' '))
    ) AS t(v)
    WHERE v IS NOT NULL AND btrim(v) <> '';
$fn$;

COMMENT ON FUNCTION name_variants(text) IS
'Readings of an identifier that a person might type: the name itself, the '
'name without its leading namespace segment, and underscores as spaces. A '
'prefix is a namespace, not a statement about what the table holds.';

-- Best similarity across all readings of a name.
CREATE OR REPLACE FUNCTION name_score(p_name text, p_skeleton text)
RETURNS numeric LANGUAGE sql IMMUTABLE AS $fn$
    SELECT coalesce(max(
        CASE WHEN normalize_name(v.variant) = p_skeleton THEN 1.0
             ELSE similarity(normalize_name(v.variant), p_skeleton) END
    ), 0)::numeric
    FROM name_variants(p_name) v;
$fn$;

COMMENT ON FUNCTION name_score(text, text) IS
'How well an identifier matches a term, scored across every reasonable '
'reading of that identifier rather than its literal spelling alone.';

-- ── search_schema uses the variant-aware score for names ────────────
DROP FUNCTION IF EXISTS search_schema(text, integer, numeric, integer, numeric);
CREATE FUNCTION search_schema(
    p_term         text,
    p_max_distinct integer DEFAULT 5000,
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
        SELECT cc.table_name AS tn, cc.column_name AS cn, tr.role AS role
        FROM column_catalog cc
        JOIN table_roles tr ON tr.table_name = cc.table_name
        LEFT JOIN column_stats cs ON cs.table_name = cc.table_name
                                 AND cs.column_name = cc.column_name
        WHERE tr.is_evidence AND tr.role IN ('fact','dimension','entity')
          AND cc.is_text
          AND NOT cc.is_generated
          AND NOT cc.is_primary_key
          AND cc.references_table IS NULL
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
'Where a term lives within the evidence surface. Identifier matching is '
'namespace-insensitive, so "cases" reaches rebuild_cases and "police '
'station" reaches police_station without either being named here.';

COMMIT;
