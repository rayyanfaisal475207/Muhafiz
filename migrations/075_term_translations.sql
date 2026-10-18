-- ============================================================
-- 075 — Every query term, in both languages
--
-- THE PROBLEM
--
-- The CORPUS is mixed. Some values are English ('charas', 'weapon'), some
-- Urdu (چرس, ہتھیار), and which one a given row uses depends on who typed
-- it. So a question in either language reaches only part of its own data:
--
--   "how many weapons"  matches 'weapon' rows, misses ہتھیار rows
--   "کتنے ہتھیار"        matches ہتھیار rows, misses 'weapon' rows
--
-- Neither is wrong. Both are half-answers, and neither says so.
--
-- Cross-script romanisation helps only where the same WORD is written in
-- two scripts (Karachi / کراچی). It cannot connect different words for the
-- same thing, which is most of the vocabulary.
--
-- THE FIX
--
-- Translate the query term, not the data. Then search for both forms.
-- Translating the data would be unbounded and would go stale on every
-- ingestion; translating query terms is bounded by the vocabulary people
-- actually use, and each one is paid for once.
--
-- This table is that cache. It holds no corpus values — only words that
-- appeared in questions and what they turned out to mean — with the source
-- that supplied each, so a bad translation is visible and removable.
-- ============================================================

BEGIN;

CREATE TABLE IF NOT EXISTS term_translations (
    term_skeleton text NOT NULL,
    term          text NOT NULL,
    lang          text NOT NULL,
    translation   text NOT NULL,
    target_lang   text NOT NULL,
    source        text NOT NULL,          -- 'model' | 'web' | 'human'
    confidence    numeric,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (term_skeleton, target_lang, translation)
);

CREATE INDEX IF NOT EXISTS idx_term_tr_lookup
    ON term_translations (term_skeleton, target_lang);

COMMENT ON TABLE term_translations IS
'Query vocabulary in both languages, cached so each word is paid for '
'once. Grows with what people ask, never with the corpus. Holds no data '
'values — only question words and their equivalents.';

-- Every form of a term worth searching with: the term itself plus any
-- recorded translation. One call, so the enhancer does not have to know
-- whether a translation exists.
CREATE OR REPLACE FUNCTION term_forms(p_term text)
RETURNS TABLE (form text, lang text, source text) LANGUAGE sql STABLE AS $fn$
    SELECT p_term, 'original'::text, 'question'::text
    UNION
    SELECT tt.translation, tt.target_lang, tt.source
    FROM term_translations tt
    WHERE tt.term_skeleton = normalize_name(p_term)
      AND coalesce(tt.confidence, 1) >= 0.5
    UNION
    -- A term the schema already knows in another language: if a question
    -- word is a recorded name for a table or column, every other name for
    -- that same object is an equivalent worth searching with.
    SELECT other.term, other.lang, 'schema'::text
    FROM schema_terms st
    JOIN schema_terms other
      ON other.table_name = st.table_name
     AND other.column_name IS NOT DISTINCT FROM st.column_name
     AND other.term_skeleton <> st.term_skeleton
    WHERE st.term_skeleton = normalize_name(p_term)
      AND coalesce(other.confidence, 1) >= 0.5;
$fn$;

COMMENT ON FUNCTION term_forms(text) IS
'Every form of a query term worth searching with — the word itself, its '
'translations, and any other name the schema knows the same thing by. '
'Used so a question in one language still reaches data written in the '
'other.';

COMMIT;
