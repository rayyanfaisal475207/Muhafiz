-- ============================================================
-- 039 — Remove stored vocabularies entirely (supersedes 034 and 038)
--
-- WHY 038 WAS STILL WRONG
--
-- 038 replaced authored lists with HARVESTED lists. Better, but it kept
-- a stored list, and a stored list has two failure modes that do not go
-- away by changing who wrote it:
--
--   1. UNBOUNDED GROWTH. harvest_observed_values() had no cardinality
--      gate. Pointed at `cases.location` (64 distinct in 73 rows) or a
--      person-name column, it would store a row per value forever. At
--      Safe City scale that is millions of rows, and resolving a term
--      becomes a search problem over a table we invented.
--   2. STALENESS. A stored list is a snapshot. The corpus moves; the
--      snapshot does not, until something remembers to refresh it.
--
-- THE FIX: DISCOVER AT QUERY TIME, STORE NOTHING
--
-- A low-cardinality column can be enumerated live, in under a
-- millisecond, from an index. There is no reason to persist that.
--   SELECT DISTINCT police_station FROM cases  -> 19 rows, always current
--
-- So: no vocabulary tables, no harvest job, no refresh discipline, no
-- growth. The resolver asks the database what values exist at the moment
-- the question is asked, and the LLM chooses among them.
--
-- THE CARDINALITY GATE
--
-- Measured on the live corpus (73 cases):
--   crime_category         7 distinct   ratio 0.10  -> categorical
--   police_station        19 distinct   ratio 0.26  -> categorical
--   investigation_status  19 distinct   ratio 0.26  -> categorical
--   investigation_officer 41 distinct   ratio 0.56  -> free text
--   location              64 distinct   ratio 0.88  -> free text
--
-- Distinct-count AND distinct/rows ratio separate the two cleanly. A
-- column that fails the gate is never enumerated — it is matched by
-- full-text or vector search instead, which is what free text needs.
--
-- WHAT THIS DOES NOT SOLVE
--
-- Enumeration tells the model what values EXIST. It does not tell it
-- what an unfamiliar term MEANS — 'CNSA 1997', 'zimni', 'challan'.
-- That is world knowledge, not private data, and no amount of querying
-- this database produces it. That gap is filled by a web-search tool,
-- which is a separate capability and not a substitute for this one:
-- the open web cannot tell you that THIS corpus stores 'بغیر لائسنس'.
-- ============================================================

BEGIN;

-- ── Remove the stored vocabularies ──────────────────────────────────
DROP FUNCTION IF EXISTS harvest_observed_values(text, text);

DROP TABLE IF EXISTS value_aliases   CASCADE;
DROP TABLE IF EXISTS observed_values CASCADE;
DROP TABLE IF EXISTS value_clusters  CASCADE;

-- The authored dimension tables from 034 go too. Their resolved-key
-- columns on the fact tables stay (nullable), so a future resolver can
-- still write a stable key without anything having to pre-exist.
ALTER TABLE case_parties    DROP CONSTRAINT IF EXISTS case_parties_role_id_fkey;
ALTER TABLE case_events     DROP CONSTRAINT IF EXISTS case_events_event_type_id_fkey;
ALTER TABLE police_stations DROP CONSTRAINT IF EXISTS police_stations_station_type_id_fkey;
ALTER TABLE rebuild_cases   DROP CONSTRAINT IF EXISTS rebuild_cases_category_id_fkey;
ALTER TABLE rebuild_cases   DROP CONSTRAINT IF EXISTS rebuild_cases_status_id_fkey;
ALTER TABLE weapons         DROP CONSTRAINT IF EXISTS weapons_license_state_id_fkey;
ALTER TABLE weapons         DROP CONSTRAINT IF EXISTS weapons_weapon_type_id_fkey;
ALTER TABLE persons         DROP CONSTRAINT IF EXISTS persons_gender_id_fkey;
ALTER TABLE officers        DROP CONSTRAINT IF EXISTS officers_rank_id_fkey;
ALTER TABLE rebuild_documents DROP CONSTRAINT IF EXISTS rebuild_documents_doc_type_id_fkey;

DROP TABLE IF EXISTS party_roles      CASCADE;
DROP TABLE IF EXISTS case_statuses    CASCADE;
DROP TABLE IF EXISTS crime_categories CASCADE;
DROP TABLE IF EXISTS license_states   CASCADE;
DROP TABLE IF EXISTS weapon_types     CASCADE;
DROP TABLE IF EXISTS station_types    CASCADE;
DROP TABLE IF EXISTS event_types      CASCADE;
DROP TABLE IF EXISTS ranks            CASCADE;
DROP TABLE IF EXISTS document_types   CASCADE;
DROP TABLE IF EXISTS genders          CASCADE;

-- ── Query-time discovery ────────────────────────────────────────────
-- Returns the distinct values of a column, with frequencies, ONLY when
-- the column passes the cardinality gate. Nothing is stored. Every call
-- reflects the corpus as it is right now.
--
-- Returns zero rows for a free-text column — that is the signal to the
-- planner to use full-text or vector matching instead of an equality
-- filter, rather than inventing a value.
CREATE OR REPLACE FUNCTION discover_values(
    p_table       text,
    p_column      text,
    p_max_distinct integer DEFAULT 500,
    p_max_ratio    numeric DEFAULT 0.40
) RETURNS TABLE (value_raw text, frequency bigint, script text)
LANGUAGE plpgsql STABLE AS $$
DECLARE
    v_distinct bigint;
    v_rows     bigint;
BEGIN
    EXECUTE format('SELECT count(DISTINCT %I), count(*) FROM %I', p_column, p_table)
        INTO v_distinct, v_rows;

    -- Gate: too many values, or too close to unique, means free text.
    IF v_distinct > p_max_distinct
       OR (v_rows > 0 AND v_distinct::numeric / v_rows > p_max_ratio) THEN
        RETURN;                                  -- deliberately empty
    END IF;

    RETURN QUERY EXECUTE format($f$
        SELECT %I::text,
               count(*)::bigint,
               CASE WHEN %I::text ~ '[؀-ۿ]' THEN 'arabic'
                    WHEN %I::text ~ '^[0-9\s\-/]+$'   THEN 'digit'
                    WHEN %I::text ~ '[A-Za-z]'        THEN 'latin'
                    ELSE 'mixed' END
        FROM %I
        WHERE %I IS NOT NULL AND btrim(%I::text) <> ''
        GROUP BY 1 ORDER BY 2 DESC
    $f$, p_column, p_column, p_column, p_column, p_table, p_column, p_column);
END;
$$;

COMMENT ON FUNCTION discover_values(text, text, integer, numeric) IS
'Live enumeration of a low-cardinality column. Stores nothing; always '
'current. Returns no rows when the column is free text, which tells the '
'planner to match by search rather than equality.';

-- Column profile, also computed live, so the planner can decide HOW to
-- match before it decides WHAT to match.
CREATE OR REPLACE FUNCTION profile_column(
    p_table text, p_column text
) RETURNS TABLE (
    distinct_count bigint, row_count bigint, null_rate numeric, is_categorical boolean
) LANGUAGE plpgsql STABLE AS $$
BEGIN
    RETURN QUERY EXECUTE format($f$
        SELECT count(DISTINCT %I)::bigint,
               count(*)::bigint,
               CASE WHEN count(*) = 0 THEN 0
                    ELSE round(count(*) FILTER (WHERE %I IS NULL)::numeric / count(*), 4)
               END,
               (count(DISTINCT %I) <= 500
                AND (count(*) = 0
                     OR count(DISTINCT %I)::numeric / greatest(count(*),1) <= 0.40))
        FROM %I
    $f$, p_column, p_column, p_column, p_column, p_table);
END;
$$;

COMMENT ON FUNCTION profile_column(text, text) IS
'Live cardinality and null-rate profile. null_rate matters as much as the '
'values: a filter on a column that is mostly NULL silently discards most '
'of the population and returns a plausible small number.';

COMMIT;
