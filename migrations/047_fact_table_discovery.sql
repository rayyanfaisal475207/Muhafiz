-- ============================================================
-- 047 — Discover fact tables precisely, and keep one temporal truth
--
-- THREE DEFECTS IN 046, FOUND BY CHECKING WHAT IT ACTUALLY TOUCHED
--
-- 046 defined a case-fact table as "has case_id and span_id". Reading
-- back the 11 tables it altered:
--
--   OVER-INCLUDED  rebuild_chunks, case_documents
--                  Both carry doc_id. They hold or index TEXT; they are
--                  the source of assertions, not assertions themselves.
--                  A chunk is not true or false at a point in time.
--
--   UNDER-INCLUDED case_officers
--                  Genuinely a case fact — who investigated, in what
--                  role, over what period — but it has no span_id, so
--                  the rule missed it. An officer assignment changes
--                  over a case's life; it is exactly what needs valid
--                  time.
--
--   DOUBLE TEMPORAL  case_station_history
--                  Already had from_ts/to_ts. 046 added valid_from/
--                  valid_to alongside, leaving two competing intervals
--                  in one table and no rule for which a query should
--                  use. That is a latent wrong-answer generator: two
--                  queries, both reasonable, disagreeing.
--
-- THE PRECISE RULE, STILL DISCOVERED
--
--   a case-fact table  =  has a FOREIGN KEY to rebuild_cases
--                         AND has no doc_id column
--
-- The foreign key is the real signal: it says this table makes
-- statements ABOUT a case. A bare case_id column, as rebuild_chunks
-- carries, is a convenience for filtering, not a claim of aboutness —
-- and the catalog distinguishes them exactly.
--
-- Yielding the nine: case_events, case_officers, case_parties,
-- case_station_history, case_statutes, phone_numbers, seized_items,
-- vehicles, weapons.
--
-- ONE TEMPORAL REPRESENTATION, EVERYWHERE
--
-- from_ts/to_ts is consolidated into valid_from/valid_to so that
-- facts_as_of() works uniformly and no table carries two answers to the
-- same question. All affected tables are empty, so this is a definition
-- change, not a data migration.
-- ============================================================

BEGIN;

-- ── Undo the over-inclusion ─────────────────────────────────────────
-- Text carriers do not hold assertions and must not look as if they do.
-- A planner seeing valid_to on rebuild_chunks would reasonably filter on
-- it and silently drop every chunk once anything populated the column.
ALTER TABLE rebuild_chunks
    DROP COLUMN IF EXISTS asserted_by_doc_id,
    DROP COLUMN IF EXISTS asserted_at,
    DROP COLUMN IF EXISTS valid_from,
    DROP COLUMN IF EXISTS valid_to,
    DROP COLUMN IF EXISTS superseded_by_doc_id;
DROP INDEX IF EXISTS idx_rebuild_chunks_current;

ALTER TABLE case_documents
    DROP COLUMN IF EXISTS asserted_by_doc_id,
    DROP COLUMN IF EXISTS asserted_at,
    DROP COLUMN IF EXISTS valid_from,
    DROP COLUMN IF EXISTS valid_to,
    DROP COLUMN IF EXISTS superseded_by_doc_id;
DROP INDEX IF EXISTS idx_case_documents_current;

-- rebuild_cases is the case itself, not a fact about a case. Its
-- changing attributes belong to the fact tables and to case_progress.
ALTER TABLE rebuild_cases
    DROP COLUMN IF EXISTS asserted_by_doc_id,
    DROP COLUMN IF EXISTS asserted_at,
    DROP COLUMN IF EXISTS valid_from,
    DROP COLUMN IF EXISTS valid_to,
    DROP COLUMN IF EXISTS superseded_by_doc_id;
DROP INDEX IF EXISTS idx_rebuild_cases_current;

-- ── The corrected discovery rule ────────────────────────────────────
CREATE OR REPLACE VIEW case_fact_tables AS
SELECT DISTINCT r.from_table AS table_name
FROM schema_relationships r
WHERE r.direction = 'child_to_parent'
  AND r.to_table  = 'rebuild_cases'
  AND NOT EXISTS (
      SELECT 1 FROM information_schema.columns c
      WHERE c.table_schema = 'public'
        AND c.table_name   = r.from_table
        AND c.column_name  = 'doc_id'
  );

COMMENT ON VIEW case_fact_tables IS
'Tables that make assertions about a case: a foreign key to '
'rebuild_cases, and no doc_id (which marks a text carrier). Discovered '
'from the catalog, so a fact table added later appears here with no edit.';

CREATE OR REPLACE FUNCTION apply_temporal_columns(p_schema text DEFAULT 'public')
RETURNS TABLE (table_name text, action text)
LANGUAGE plpgsql AS $fn$
DECLARE r record;
BEGIN
    FOR r IN SELECT f.table_name AS tn FROM case_fact_tables f ORDER BY 1
    LOOP
        EXECUTE format($d$
            ALTER TABLE %I
              ADD COLUMN IF NOT EXISTS asserted_by_doc_id text
                  REFERENCES rebuild_documents (doc_id),
              ADD COLUMN IF NOT EXISTS asserted_at  timestamptz,
              ADD COLUMN IF NOT EXISTS valid_from   timestamptz,
              ADD COLUMN IF NOT EXISTS valid_to     timestamptz,
              ADD COLUMN IF NOT EXISTS superseded_by_doc_id text
                  REFERENCES rebuild_documents (doc_id)
        $d$, r.tn);

        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %I (case_id) WHERE valid_to IS NULL',
            'idx_' || left(r.tn, 40) || '_current', r.tn);

        table_name := r.tn; action := 'temporal columns applied';
        RETURN NEXT;
    END LOOP;
END;
$fn$;

COMMENT ON FUNCTION apply_temporal_columns(text) IS
'Adds bitemporal assertion columns to every table in case_fact_tables. '
'Idempotent and self-extending.';

SELECT * FROM apply_temporal_columns();

-- ── One interval per table ──────────────────────────────────────────
-- from_ts/to_ts meant exactly what valid_from/valid_to mean. Two names
-- for one concept in the same schema guarantees that two correct-looking
-- queries eventually disagree. Both tables are empty, so the older pair
-- is simply removed rather than migrated.
ALTER TABLE case_station_history DROP COLUMN IF EXISTS from_ts;
ALTER TABLE case_station_history DROP COLUMN IF EXISTS to_ts;
ALTER TABLE case_officers        DROP COLUMN IF EXISTS from_ts;
ALTER TABLE case_officers        DROP COLUMN IF EXISTS to_ts;

-- case_station_history was keyed on (case_id, from_ts), which that drop
-- removes. valid_from replaces it in the same role.
ALTER TABLE case_station_history
    ADD PRIMARY KEY (case_id, valid_from);

COMMENT ON COLUMN case_station_history.valid_from IS
'When the case was actually at this station, not when a document said '
'so. Replaces from_ts so every fact table reads the same way.';

-- ── Guard: a fact table must not invent its own temporal columns ────
-- Catches the 046 mistake class at review time rather than at query
-- time, by reporting any fact table carrying an interval pair that
-- facts_as_of() will not see.
CREATE OR REPLACE VIEW temporal_model_violations AS
SELECT f.table_name,
       string_agg(c.column_name, ', ' ORDER BY c.column_name) AS shadow_columns,
       'this fact table carries its own interval columns; facts_as_of() '
       'reads valid_from/valid_to and would ignore them' AS problem
FROM case_fact_tables f
JOIN information_schema.columns c
     ON c.table_schema = 'public' AND c.table_name = f.table_name
WHERE c.column_name IN ('from_ts','to_ts','effective_from','effective_to',
                        'start_ts','end_ts','valid_start','valid_end')
GROUP BY f.table_name;

COMMENT ON VIEW temporal_model_violations IS
'Should always be empty. Non-empty means a fact table has a second, '
'competing notion of time that the as-of functions cannot see.';

COMMIT;
