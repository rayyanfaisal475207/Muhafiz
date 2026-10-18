-- ============================================================
-- 074 — A shared term belongs to the table it is ABOUT
--
-- MEASURED after generating the multilingual catalog:
--
--   مقدمات claimed by rebuild_cases, case_statutes, case_station_history
--
-- All three contain the word "case", so all three were named "cases". But
-- `case_statutes` is about STATUTES of a case and `case_station_history`
-- about STATION HISTORY of a case. Only `rebuild_cases` is about cases.
--
-- Binding then picked arbitrarily among three perfect scores, and a
-- question about مقدمات reached a history table holding no rows.
--
-- THE RULE, DERIVED FROM THE IDENTIFIER
--
-- An identifier is a compound of concepts: case_station_history is three,
-- rebuild_cases is two (one of them a namespace prefix). A term naming the
-- table's whole subject belongs to the table with the FEWEST additional
-- concepts. The rest keep the term at reduced confidence — they are still
-- related, just not what the word means on its own.
--
-- No vocabulary: the rule counts underscore-separated parts and compares.
-- It holds for any naming convention that composes words.
-- ============================================================

BEGIN;

CREATE OR REPLACE VIEW schema_term_conflicts AS
WITH claims AS (
    SELECT st.term_skeleton, st.table_name, st.term, st.lang,
           -- Concepts in the identifier, ignoring a leading namespace.
           array_length(
               string_to_array(
                   regexp_replace(st.table_name, '^(rebuild|tbl|dim|fact)_', ''),
                   '_'), 1) AS concept_count
    FROM schema_terms st
    WHERE st.column_name IS NULL
)
SELECT term_skeleton,
       count(*)                                   AS claimed_by,
       min(concept_count)                          AS fewest_concepts,
       array_agg(table_name ORDER BY concept_count, table_name) AS tables
FROM claims
GROUP BY term_skeleton
HAVING count(*) > 1;

COMMENT ON VIEW schema_term_conflicts IS
'Terms claimed by several tables. The table with the fewest concepts in '
'its identifier is the one the term is ABOUT; the others merely contain '
'the word.';

CREATE OR REPLACE FUNCTION resolve_term_specificity()
RETURNS TABLE (term text, kept_for text, demoted_for text[])
LANGUAGE plpgsql AS $fn$
DECLARE r record;
BEGIN
    FOR r IN SELECT * FROM schema_term_conflicts LOOP
        -- The most specific claimant keeps full confidence; the rest are
        -- demoted rather than deleted, so a question that really does mean
        -- the related table can still reach it.
        UPDATE schema_terms st
           SET confidence = 0.3
         WHERE st.term_skeleton = r.term_skeleton
           AND st.column_name IS NULL
           AND st.table_name <> r.tables[1];

        term := (SELECT s.term FROM schema_terms s
                  WHERE s.term_skeleton = r.term_skeleton LIMIT 1);
        kept_for := r.tables[1];
        demoted_for := r.tables[2:];
        RETURN NEXT;
    END LOOP;
END;
$fn$;

-- Scoring respects it: a demoted claim scores proportionally lower, so the
-- table the term is about wins without the others being unreachable.
CREATE OR REPLACE FUNCTION name_score_multilingual(
    p_table text, p_column text, p_skeleton text
) RETURNS numeric LANGUAGE sql STABLE AS $fn$
    SELECT greatest(
        name_score(coalesce(p_column, p_table), p_skeleton),
        coalesce((
            SELECT max(
                CASE WHEN st.term_skeleton = p_skeleton THEN 1.0
                     ELSE similarity(st.term_skeleton, p_skeleton) END
                * coalesce(st.confidence, 0.8) / 0.8)
            FROM schema_terms st
            WHERE st.table_name = p_table
              AND (st.column_name IS NOT DISTINCT FROM p_column)
        ), 0)
    )::numeric;
$fn$;

COMMIT;
