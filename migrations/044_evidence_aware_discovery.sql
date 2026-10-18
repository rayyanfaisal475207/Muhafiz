-- ============================================================
-- 044 — Discovery reports its evidence (fixes two defects in 039 and 043)
--
-- DEFECT 1: A RATIO OVER NINE ROWS CARRIES NO INFORMATION
--
-- 039 gated enumeration on distinct/rows > 0.40. Testing 043 against a
-- freshly built place hierarchy:
--
--     discover_values('places','kind_raw')  -> 0 rows
--
-- Nine places, nine distinct kinds, ratio 1.0, so the gate called a
-- perfectly categorical column free text. It is not — it holds
-- 'province', 'city', 'police_station'. It simply had one row per kind
-- so far.
--
-- The ratio measures whether values REPEAT. Until a column has enough
-- rows for repetition to be expected, it measures nothing. The gate was
-- reading sampling noise as a property of the column.
--
-- DEFECT 2: EMPTY IS NOT THE SAME AS FREE TEXT
--
--     profile_column('case_events','event_type_raw')
--       -> distinct 0, rows 0, is_categorical TRUE
--
-- An empty table was reported as confidently categorical. And
-- discover_values returned zero rows for THREE different situations the
-- caller must distinguish:
--
--     * the column is free text        -> use search, not equality
--     * the table is empty             -> the question is unanswerable
--     * every value is null            -> the field exists but is unused
--
-- A planner that cannot tell these apart will report "no results" for a
-- table that was never loaded, which reads to a user as "there are none".
-- That is the confident-wrong-number failure in its quietest form.
--
-- THE FIX: RETURN THE EVIDENCE, NOT A VERDICT
--
-- Same principle as 042's ranked paths and 043's resolve_place. Do not
-- make a silent binary choice on the caller's behalf. Return what was
-- measured, labelled, and let the caller decide — including the label
-- 'insufficient_evidence', which is a legitimate and common answer that
-- the old gate had no way to express.
-- ============================================================

BEGIN;

-- Return types change, so the old signatures must go first.
DROP FUNCTION IF EXISTS profile_column(text, text);
DROP FUNCTION IF EXISTS discover_values(text, text, integer, numeric);

-- ── Column profile, with its own reliability attached ───────────────
CREATE FUNCTION profile_column(
    p_table text, p_column text
) RETURNS TABLE (
    distinct_count bigint,
    row_count      bigint,
    non_null_count bigint,
    null_rate      numeric,
    distinct_ratio numeric,
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

    -- Order matters: the cheapest explanations for "no values" are
    -- checked before any judgement about the column's nature.
    IF v_rows = 0 THEN
        verdict := 'empty_table';
        reason  := 'the table holds no rows; nothing can be concluded '
                   'about this column, and a count over it is not zero '
                   'evidence but no evidence';
    ELSIF v_nonnull = 0 THEN
        verdict := 'all_null';
        reason  := format('all %s rows are null in this column; a filter '
                          'on it discards the entire population', v_rows);
    -- A ratio needs repetition to mean anything. With fewer non-null
    -- rows than twice the distinct count, a categorical column has not
    -- had the CHANCE to repeat, so the ratio is noise rather than signal.
    ELSIF v_nonnull < 2 * v_distinct AND v_distinct <= 500 THEN
        verdict := 'insufficient_evidence';
        reason  := format('%s distinct values in %s non-null rows: too '
                          'few rows for repetition to be expected, so the '
                          'distinct ratio is not yet informative',
                          v_distinct, v_nonnull);
    ELSIF v_distinct > 500 THEN
        verdict := 'high_cardinality';
        reason  := format('%s distinct values exceeds the enumeration '
                          'limit; match by search, not equality', v_distinct);
    ELSIF v_ratio > 0.40 THEN
        verdict := 'free_text';
        reason  := format('%s distinct values across %s non-null rows '
                          '(ratio %s): values rarely repeat, which is how '
                          'free text behaves', v_distinct, v_nonnull, v_ratio);
    ELSE
        verdict := 'categorical';
        reason  := format('%s distinct values across %s non-null rows '
                          '(ratio %s): values repeat, so equality '
                          'filtering is sound', v_distinct, v_nonnull, v_ratio);
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

COMMENT ON FUNCTION profile_column(text, text) IS
'Live column profile carrying its own reliability. verdict separates '
'empty_table, all_null and insufficient_evidence from a real '
'free_text/categorical judgement, so a planner never reads an unloaded '
'table as an empty result.';

-- ── Discovery that says why, and never silently returns nothing ─────
-- Every row carries the verdict, so a caller reading only the first row
-- still learns whether these values can be trusted as a complete
-- enumeration. Values ARE returned under insufficient_evidence — they
-- are real observed values and useful — but labelled so the caller knows
-- the list may not be exhaustive.
CREATE FUNCTION discover_values(
    p_table        text,
    p_column       text,
    p_max_distinct integer DEFAULT 500,
    p_max_ratio    numeric DEFAULT 0.40
) RETURNS TABLE (
    value_raw  text,
    frequency  bigint,
    script     text,
    verdict    text,
    exhaustive boolean
) LANGUAGE plpgsql STABLE AS $fn$
DECLARE
    v_verdict text;
    v_rows    bigint;
BEGIN
    SELECT p.verdict, p.row_count INTO v_verdict, v_rows
    FROM profile_column(p_table, p_column) p;

    -- No values to enumerate, and the caller is told which kind of
    -- nothing this is rather than receiving a bare empty set.
    IF v_verdict IN ('empty_table', 'all_null') THEN
        value_raw := NULL; frequency := 0; script := NULL;
        verdict := v_verdict; exhaustive := false;
        RETURN NEXT;
        RETURN;
    END IF;

    -- Genuinely unbounded: enumerating is the wrong operation, and
    -- saying so is more useful than returning a truncated list that
    -- looks complete.
    IF v_verdict IN ('high_cardinality', 'free_text') THEN
        value_raw := NULL; frequency := NULL; script := NULL;
        verdict := v_verdict; exhaustive := false;
        RETURN NEXT;
        RETURN;
    END IF;

    RETURN QUERY EXECUTE format($f$
        SELECT %I::text,
               count(*)::bigint,
               CASE WHEN %I::text ~ '[؀-ۿ]'      THEN 'arabic'
                    WHEN %I::text ~ '^[0-9\s\-/]+$' THEN 'digit'
                    WHEN %I::text ~ '[A-Za-z]'      THEN 'latin'
                    ELSE 'mixed' END,
               %L::text,
               %L::boolean
        FROM %I
        WHERE %I IS NOT NULL AND btrim(%I::text) <> ''
        GROUP BY 1 ORDER BY 2 DESC
    $f$, p_column, p_column, p_column, p_column,
         v_verdict, (v_verdict = 'categorical'),
         p_table, p_column, p_column);
END;
$fn$;

COMMENT ON FUNCTION discover_values(text, text, integer, numeric) IS
'Live enumeration carrying its verdict. Returns a labelled marker row '
'rather than a bare empty set when there is nothing to enumerate, so '
'"this table was never loaded" is never mistaken for "there are none". '
'exhaustive=false means the values are real but the list may grow.';

-- ── Profile every column at once ────────────────────────────────────
-- Stage 1 of the build order is "profile every column, live, no
-- storage". This is that, as one query over the catalog. It stores
-- nothing and is always current.
CREATE OR REPLACE FUNCTION profile_all_columns(
    p_schema text DEFAULT 'public'
) RETURNS TABLE (
    table_name     text,
    column_name    text,
    data_type      text,
    distinct_count bigint,
    row_count      bigint,
    null_rate      numeric,
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
            null_rate := p.null_rate; verdict := p.verdict;
            RETURN NEXT;
        EXCEPTION WHEN OTHERS THEN
            -- A column type that cannot be counted DISTINCT (json, and
            -- some extension types) is reported, not skipped silently.
            table_name := r.tn; column_name := r.cn; data_type := r.dt;
            distinct_count := NULL; row_count := NULL; null_rate := NULL;
            verdict := 'unprofilable';
            RETURN NEXT;
        END;
    END LOOP;
END;
$fn$;

COMMENT ON FUNCTION profile_all_columns(text) IS
'Profiles every column of every base table live. Build-order stage 1, '
'with nothing stored: re-running it always reflects the corpus as it is.';

-- ── Fix the duplicated ancestor in the display path ─────────────────
-- place_full_path showed 'سندھ > سندھ' because a place reachable through
-- both the police and civil hierarchies was listed once per hierarchy.
-- Correct for the ancestor QUERY, wrong for a displayed path.
DROP VIEW IF EXISTS place_full_path;
CREATE VIEW place_full_path AS
SELECT p.place_id, p.kind_raw, p.name_raw,
       (SELECT string_agg(x.name_raw, ' > ' ORDER BY x.depth DESC)
          FROM (SELECT DISTINCT ON (a.place_id) a.place_id, a.name_raw, a.depth
                  FROM place_ancestors(p.place_id) a
                 ORDER BY a.place_id, a.depth) x)   AS ancestry,
       (SELECT count(DISTINCT h.hierarchy_raw) FROM place_hierarchy h
         WHERE h.child_id = p.place_id)             AS hierarchy_count,
       (SELECT count(*) FROM place_hierarchy h
         WHERE h.child_id = p.place_id)             AS parent_count
FROM places p;

COMMENT ON VIEW place_full_path IS
'Human-readable ancestry per place, one entry per ancestor even when it '
'is reachable through several hierarchies. hierarchy_count > 1 is '
'expected, not an error.';

COMMIT;
