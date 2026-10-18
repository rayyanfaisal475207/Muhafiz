-- ============================================================
-- 072 — The schema, in every language questions arrive in
--
-- THE GAP
--
--   "How many cases are registered?"   -> 73
--   "کتنے مقدمات درج ہیں؟"              -> refuses
--
-- Same question, same data, different language. The table is called
-- `rebuild_cases`; the Urdu word is مقدمات. name_score() folds prefixes,
-- underscores and script variants — it does not translate, so an Urdu
-- question has no path to an English identifier at all.
--
-- Cross-script work so far runs the OTHER way: an English question
-- reaching Urdu VALUES (Karachi -> کراچی). That is why "cases from
-- Lahore" works and "کتنے مقدمات" does not.
--
-- WHY A CATALOG AND NOT TRANSLATION PER QUESTION
--
-- There is an asymmetry worth exploiting. The DATA is large and changes
-- constantly — translating values would be unbounded work that goes stale.
-- The SCHEMA is small and changes only with a migration: 54 tables and 560
-- columns here, and that count does not grow with the corpus.
--
-- So the small stable thing is translated once and kept. At a million
-- cases it is still 54 tables.
--
-- IS THIS THE AUTHORED-LIST MISTAKE?
--
-- It passes the test from L21: can it be thrown away and rebuilt exactly,
-- from the database, with no human input? Yes — scripts/schema_terms.py
-- regenerates it from identifier names and the COMMENTs already in the
-- schema. Nobody writes these by hand, and no corpus VALUE appears here;
-- it describes the schema, which is metadata, not data.
--
-- Terms carry their provenance and confidence, so a bad translation is
-- visible and removable rather than baked in.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS schema_terms (
    term_id      bigserial PRIMARY KEY,
    table_name   text NOT NULL,
    column_name  text,                    -- NULL means the term names the table
    term         text NOT NULL,
    lang         text NOT NULL,           -- 'ur', 'en', 'roman' — observed, not validated
    source       text NOT NULL,           -- 'model' | 'identifier' | 'human'
    confidence   numeric,
    generated_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (table_name, column_name, term)
);

CREATE INDEX IF NOT EXISTS idx_schema_terms_lookup
    ON schema_terms (table_name, column_name);
CREATE INDEX IF NOT EXISTS idx_schema_terms_term
    ON schema_terms (term);

COMMENT ON TABLE schema_terms IS
'What each table and column is CALLED, in every language questions arrive '
'in. Regenerated from identifier names and schema comments; contains no '
'corpus values. Small and stable by construction: it grows with the '
'schema, never with the data.';

-- Skeletons for matching, same normalisation the binder uses elsewhere.
ALTER TABLE schema_terms ADD COLUMN IF NOT EXISTS term_skeleton text
    GENERATED ALWAYS AS (normalize_name(term)) STORED;

CREATE INDEX IF NOT EXISTS idx_schema_terms_skeleton
    ON schema_terms (term_skeleton);

-- ── Identifier matching now consults the translations ───────────────
CREATE OR REPLACE FUNCTION name_score_multilingual(
    p_table text, p_column text, p_skeleton text
) RETURNS numeric LANGUAGE sql STABLE AS $fn$
    SELECT greatest(
        -- The identifier itself, across its readings.
        name_score(coalesce(p_column, p_table), p_skeleton),
        -- Any recorded name for it, in any language.
        coalesce((
            SELECT max(CASE WHEN st.term_skeleton = p_skeleton THEN 1.0
                            ELSE similarity(st.term_skeleton, p_skeleton) END)
            FROM schema_terms st
            WHERE st.table_name = p_table
              AND (st.column_name IS NOT DISTINCT FROM p_column)
        ), 0)
    )::numeric;
$fn$;

COMMENT ON FUNCTION name_score_multilingual(text, text, text) IS
'How well a term matches a table or column, in ANY language it is known '
'by. Without this an Urdu question cannot reach an English identifier, '
'which is most of the schema.';

COMMIT;
