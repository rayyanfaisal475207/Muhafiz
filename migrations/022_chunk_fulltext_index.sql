-- ============================================================
-- Migration 022: Persistent full-text index for BM25 keyword search
-- (Graph Scale & Schema Expansion, Milestone A2 —
-- docs/decisions/0002-graph-schema-expansion-and-scale.md)
--
-- Replaces bm25_retriever.py's actual scaling problem: retrieve_bm25()
-- builds a fresh BM25Okapi index (full tokenization + term-frequency
-- stats) over the ENTIRE scoped candidate pool on every single query —
-- confirmed in this codebase's own comments (orchestrator.py: "this
-- rebuilds an in-memory BM25 index over the full scoped corpus on every
-- retrieval... at real production scale this tokenize+index pass becomes
-- the dominant cost per query. No caching/persistent-index layer is
-- added here"). Swapping where the chunk TEXT comes from (Chroma vs.
-- Postgres) would not fix that on its own — the candidate POOL itself has
-- to shrink from "every chunk in scope" to "chunks that actually share a
-- token with the query", which is what a real inverted index is for.
--
-- One row per chunk (chunk_id is the exact same id Chroma stores it
-- under — src/retrieval/vector_store.py's upsert_documents()), maintained
-- incrementally at ingest time (src/retrieval/fulltext_index.py's
-- maintain()/delete_by_ids()/delete_by_source()), never rebuilt from
-- scratch per query.
--
-- `tsv` is populated from ALREADY-TOKENIZED text (src/ingestion/
-- tokenizer.py's Urdu-aware `tokenize()`, space-joined) rather than
-- Postgres's own to_tsvector('simple', ...) tokenizing the raw text —
-- bm25_retriever.py's own comment is explicit that "CORPUS AND QUERY MUST
-- USE THE SAME TOKENIZER" (Arabic/Urdu codepoint variants, Urdu
-- punctuation): letting Postgres's built-in tokenizer diverge from the
-- one BM25 scoring already depends on would silently under/over-match
-- Urdu content. 'simple' text search config still normalizes casing
-- (a no-op on Urdu) and provides GIN indexing — it is not asked to do any
-- linguistic tokenization here, `tokenize()` already did that.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, safe to re-run.
--
-- HOW TO APPLY
--   python scripts/apply_migration.py migrations/022_chunk_fulltext_index.sql

-- `metadata` carries the chunk's COMPLETE Chroma metadata dict (doc_type,
-- page, record_date, fir_display_code, ...) verbatim, as JSONB — not just
-- the columns below. Those columns (source/project_id/case_id/is_global)
-- are a denormalized, indexed subset of the SAME metadata, kept only so
-- candidate_pool()'s scope filter can use real btree indexes instead of
-- a JSONB expression index; `metadata` is what actually gets handed back
-- to callers, so a downstream consumer that reads e.g. `record_date` for
-- reranker.py's recency boost sees the identical shape it would have
-- gotten from Chroma's get_all(), not a thinned-down projection of it.
CREATE TABLE IF NOT EXISTS chunk_fulltext (
    chunk_id    TEXT PRIMARY KEY,
    doc_id      TEXT NOT NULL,
    source      TEXT,
    project_id  TEXT,
    case_id     TEXT,
    is_global   BOOLEAN NOT NULL DEFAULT false,
    text        TEXT NOT NULL,
    metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
    tsv         TSVECTOR NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS ix_chunk_fulltext_tsv ON chunk_fulltext USING GIN (tsv);
-- Supports candidate_pool()'s scope filter (project_id/is_global OR,
-- case_id AND — the exact same two-dimensional scoping
-- vector_store.py's _build_where() already enforces for Chroma).
CREATE INDEX IF NOT EXISTS ix_chunk_fulltext_project ON chunk_fulltext (project_id);
CREATE INDEX IF NOT EXISTS ix_chunk_fulltext_case ON chunk_fulltext (case_id);
CREATE INDEX IF NOT EXISTS ix_chunk_fulltext_source ON chunk_fulltext (source);
