-- ============================================================
-- 069 — Collapse facts duplicated by repeated extraction
--
-- MEASURED
--
--   weapons: 145 rows, 81 distinct (case_id, weapon_type_raw)
--   fir-430-26 held FIVE identical 'پستول' rows
--
-- The extractor deduplicated on every field matching. A non-deterministic
-- model writes the same weapon with quantity on one pass and without it on
-- the next; neither row equals the other, so both survive. Every
-- re-extraction therefore inflated every count, and "how many weapons"
-- grew each time the pipeline ran.
--
-- The extractor now deduplicates on identity — the case plus the column
-- that NAMES the fact — and fills blanks on the existing row. This cleans
-- up what the old rule already let through.
--
-- MERGE, NOT DELETE
--
-- The survivor is the earliest row, enriched with any non-null values its
-- duplicates carried, so detail found on a later pass is kept rather than
-- discarded. Only then are the rivals removed.
-- ============================================================

BEGIN;

CREATE OR REPLACE FUNCTION collapse_duplicate_facts(
    p_table text, p_ident_col text
) RETURNS TABLE (kept bigint, merged bigint, removed bigint)
LANGUAGE plpgsql AS $fn$
DECLARE
    v_pk   text;
    v_cols text[];
    r      record;
    c      text;
    n_kept bigint := 0;
    n_merged bigint := 0;
    n_removed bigint := 0;
BEGIN
    SELECT column_name INTO v_pk FROM column_catalog
     WHERE table_name = p_table AND is_primary_key LIMIT 1;
    IF v_pk IS NULL THEN
        RAISE EXCEPTION 'no single-column primary key on %', p_table;
    END IF;

    SELECT array_agg(column_name) INTO v_cols FROM column_catalog
     WHERE table_name = p_table
       AND column_name NOT IN (v_pk, 'case_id', p_ident_col)
       AND NOT is_generated;

    FOR r IN EXECUTE format(
        'SELECT case_id, %I AS ident, min(%I) AS survivor,'
        '       array_agg(%I) AS all_ids, count(*) AS n'
        '  FROM %I WHERE %I IS NOT NULL'
        ' GROUP BY case_id, %I HAVING count(*) > 1',
        p_ident_col, v_pk, v_pk, p_table, p_ident_col, p_ident_col)
    LOOP
        -- Carry any detail the duplicates hold onto the survivor first.
        FOREACH c IN ARRAY coalesce(v_cols, ARRAY[]::text[]) LOOP
            EXECUTE format(
                'UPDATE %I s SET %I = d.v FROM ('
                '  SELECT %I AS v FROM %I'
                '   WHERE %I = ANY($1) AND %I IS NOT NULL LIMIT 1) d'
                ' WHERE s.%I = $2 AND s.%I IS NULL',
                p_table, c, c, p_table, v_pk, c, v_pk, c)
            USING r.all_ids, r.survivor;
        END LOOP;
        n_merged := n_merged + 1;

        EXECUTE format('DELETE FROM %I WHERE %I = ANY($1) AND %I <> $2',
                       p_table, v_pk, v_pk)
        USING r.all_ids, r.survivor;
        n_removed := n_removed + (r.n - 1);
        n_kept := n_kept + 1;
    END LOOP;

    kept := n_kept; merged := n_merged; removed := n_removed;
    RETURN NEXT;
END;
$fn$;

COMMENT ON FUNCTION collapse_duplicate_facts(text, text) IS
'Collapses rows duplicated by repeated extraction, keeping the earliest '
'and carrying any detail its duplicates held. Identity is the case plus '
'the naming column, matching how the extractor now deduplicates.';

COMMIT;
