-- ============================================================
-- 070 — Fact identity is derived, never passed in
--
-- WHAT I BROKE
--
--   collapse_duplicate_facts('case_parties', 'role_raw')
--   -> 276 rows became 158; 170 removed
--
-- A case with three accused holds three rows all reading 'ملزم'. They are
-- three PEOPLE, not three copies of one fact. Collapsing on role merged
-- them, and every case was flattened to one person per role.
--
-- The mistake was letting the caller name the identity column. For
-- `weapons` the naming column IS the identity — one pistol per case per
-- type is a reasonable reading. For `case_parties` it is emphatically not.
-- The quality gate's "naming column" was reused as an identity column
-- without checking the two coincide, and for one table they do not.
--
-- THE RULE, READ FROM THE SCHEMA
--
--   a fact referencing a PERSON     -> identity is (case_id, person_id)
--   referencing an OFFICER/SECTION  -> that reference
--   anything else                   -> the column that names the fact
--
-- Derived from the catalog, so a table that gains a person reference is
-- handled without anyone remembering to pass a different argument. The
-- caller can no longer choose wrongly, because the caller no longer
-- chooses.
-- ============================================================

BEGIN;

CREATE OR REPLACE FUNCTION fact_identity_columns(p_table text)
RETURNS text[] LANGUAGE plpgsql STABLE AS $fn$
DECLARE v text;
BEGIN
    -- An entity reference is the identity. Two rows about different people
    -- are different facts however alike they otherwise look.
    IF EXISTS (SELECT 1 FROM column_catalog
                WHERE table_name = p_table AND column_name = 'person_id') THEN
        RETURN ARRAY['person_id'];
    END IF;
    IF EXISTS (SELECT 1 FROM column_catalog
                WHERE table_name = p_table AND column_name = 'officer_id') THEN
        RETURN ARRAY['officer_id'];
    END IF;
    IF EXISTS (SELECT 1 FROM column_catalog
                WHERE table_name = p_table AND column_name = 'section_id') THEN
        RETURN ARRAY['section_id'];
    END IF;

    -- Otherwise the column that NAMES the fact, by the schema convention
    -- that every table stores the document wording in a *_raw column.
    SELECT min(column_name) INTO v FROM column_catalog
     WHERE table_name = p_table AND column_name LIKE '%\_type\_raw';
    IF v IS NOT NULL THEN RETURN ARRAY[v]; END IF;

    SELECT min(column_name) INTO v FROM column_catalog
     WHERE table_name = p_table
       AND column_name IN ('description_ur', 'msisdn', 'registration_no');
    IF v IS NOT NULL THEN RETURN ARRAY[v]; END IF;

    RETURN NULL;
END;
$fn$;

COMMENT ON FUNCTION fact_identity_columns(text) IS
'What makes a fact in this table unique, read from the schema. A table '
'referencing a person is identified by that person; otherwise by the '
'column naming the fact. Callers do not choose: choosing wrongly once '
'merged 170 distinct people away.';

DROP FUNCTION IF EXISTS collapse_duplicate_facts(text, text);

CREATE FUNCTION collapse_duplicate_facts(p_table text)
RETURNS TABLE (kept bigint, merged bigint, removed bigint, identity text)
LANGUAGE plpgsql AS $fn$
DECLARE
    v_pk    text;
    v_ident text;
    v_cols  text[];
    r       record;
    c       text;
    n_kept bigint := 0; n_merged bigint := 0; n_removed bigint := 0;
BEGIN
    SELECT (fact_identity_columns(p_table))[1] INTO v_ident;
    IF v_ident IS NULL THEN
        RAISE EXCEPTION
          'cannot identify facts in %: no person, officer or section '
          'reference and no naming column. Refusing rather than guessing.',
          p_table;
    END IF;

    SELECT column_name INTO v_pk FROM column_catalog
     WHERE table_name = p_table AND is_primary_key LIMIT 1;
    IF v_pk IS NULL THEN
        RAISE EXCEPTION 'no single-column primary key on %', p_table;
    END IF;

    SELECT array_agg(column_name) INTO v_cols FROM column_catalog
     WHERE table_name = p_table
       AND column_name NOT IN (v_pk, 'case_id', v_ident)
       AND NOT is_generated;

    FOR r IN EXECUTE format(
        'SELECT min(%I) AS survivor, array_agg(%I) AS all_ids, count(*) AS n'
        '  FROM %I WHERE %I IS NOT NULL'
        ' GROUP BY case_id, %I HAVING count(*) > 1',
        v_pk, v_pk, p_table, v_ident, v_ident)
    LOOP
        -- Carry detail from the duplicates onto the survivor before
        -- removing them, so a later pass that found more is not discarded.
        FOREACH c IN ARRAY coalesce(v_cols, ARRAY[]::text[]) LOOP
            EXECUTE format(
                'UPDATE %I s SET %I = d.v FROM ('
                '  SELECT %I AS v FROM %I WHERE %I = ANY($1)'
                '   AND %I IS NOT NULL LIMIT 1) d'
                ' WHERE s.%I = $2 AND s.%I IS NULL',
                p_table, c, c, p_table, v_pk, c, v_pk, c)
            USING r.all_ids, r.survivor;
        END LOOP;
        EXECUTE format('DELETE FROM %I WHERE %I = ANY($1) AND %I <> $2',
                       p_table, v_pk, v_pk)
        USING r.all_ids, r.survivor;
        n_kept := n_kept + 1; n_merged := n_merged + 1;
        n_removed := n_removed + (r.n - 1);
    END LOOP;

    kept := n_kept; merged := n_merged; removed := n_removed;
    identity := v_ident;
    RETURN NEXT;
END;
$fn$;

COMMENT ON FUNCTION collapse_duplicate_facts(text) IS
'Collapses rows duplicated by repeated extraction. Derives its own '
'identity key from the schema and raises rather than guessing when it '
'cannot determine one.';

COMMIT;
