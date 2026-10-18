-- ============================================================
-- 064 — Full-text search over rebuilt chunks
--
-- rebuild_chunks holds 712 chunks, every one carrying a span, and none of
-- them searchable: tsv was an empty column nothing filled.
--
-- WHY 'simple' AND NOT 'english'
--
-- The corpus is Urdu. An English text-search configuration stems and
-- stop-words English, and does neither usefully for Arabic script — it
-- would silently drop tokens it does not recognise. 'simple' tokenises on
-- whitespace and punctuation and lower-cases, which is exactly right for a
-- corpus whose language Postgres has no dictionary for.
--
-- Stemming is a loss here, not a gain: 'گرفتاری' and 'گرفتار' are better
-- matched by trigram similarity, which the GIN index below also serves.
--
-- GENERATED, NOT TRIGGERED
--
-- A generated column cannot drift from the text it indexes. A trigger can,
-- if a bulk load bypasses it — and ingestion writes these rows in bulk.
-- ============================================================

BEGIN;

ALTER TABLE rebuild_chunks DROP COLUMN IF EXISTS tsv;
ALTER TABLE rebuild_chunks
    ADD COLUMN tsv tsvector
    GENERATED ALWAYS AS (to_tsvector('simple', coalesce(text, ''))) STORED;

CREATE INDEX IF NOT EXISTS idx_rchunks_tsv_gin
    ON rebuild_chunks USING gin (tsv);

-- Trigram, for the morphological variation 'simple' cannot handle and for
-- cross-script candidates.
CREATE INDEX IF NOT EXISTS idx_rchunks_text_trgm
    ON rebuild_chunks USING gin (text gin_trgm_ops);

COMMENT ON COLUMN rebuild_chunks.tsv IS
'Full-text vector over the chunk, built with the simple configuration '
'because the corpus is Urdu and an English dictionary would drop tokens '
'it cannot stem. Generated, so it cannot drift from the text.';

COMMIT;
