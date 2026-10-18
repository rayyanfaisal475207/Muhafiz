-- ============================================================
-- 036 — Documents, structural elements, spans and chunks
--        (rebuild stage 2)
--
-- WHY THIS EXISTS
--
-- Today a chunk is a 512-token window with 64 tokens of overlap and a
-- flat metadata bag. That loses three things that matter for anything
-- beyond lookup:
--
--   1. WHERE the text sat in the document. A sentence under
--      "زمنی نمبر 3" means something different from the same sentence
--      under "برآمدگی". A flat window cannot tell them apart.
--   2. WHAT the text is. A table row, a heading, a signature block and
--      a narrative paragraph all retrieve identically today, so a table
--      gets split mid-row and cited as prose.
--   3. WHAT it points at. A zimni entry refers back to its FIR; a
--      chalaan cites sections. Those references exist in the text and
--      are thrown away at chunk time.
--
-- Docling's contribution is not its PDF parser — this corpus arrives
-- from an API, not PDFs. It is the idea that a document is a TREE of
-- typed elements with provenance, and that a chunk should carry its
-- position in that tree. This schema mimics that:
--
--      documents  -> doc_elements (tree)  -> doc_spans (provenance)
--                                         -> chunks (retrieval unit)
--                                         -> chunk_relations (graph of chunks)
--                                         -> chunk_mentions (chunk -> extracted row)
--
-- `chunk_mentions` is the cross-relationship layer: it links retrieval
-- units to the typed rows extracted from them, in both directions. That
-- is what lets a SQL answer cite text, and a retrieved passage name the
-- structured facts it contains.
--
-- Additive only. Existing `documents` and `chunk_fulltext` are untouched;
-- these are new tables under rebuild_* naming where a name collides.
-- ============================================================

BEGIN;

-- ── Documents ───────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS rebuild_documents (
    doc_id        text PRIMARY KEY,
    case_id       text,                    -- NULL for global statute/reference
    doc_type_id   text NOT NULL REFERENCES document_types,
    source_system text,                    -- 'psrms', 'manual_upload', ...
    external_id   text,                    -- the id in the originating system
    title         text,
    language      text,                    -- 'ur' | 'en' | 'roman_ur' | 'mixed'
    is_global     boolean NOT NULL DEFAULT false,
    -- Version/recency: a Safe City feed will re-send corrected documents.
    -- Superseding rather than overwriting keeps the audit trail intact.
    revision      integer NOT NULL DEFAULT 1,
    supersedes_doc_id text REFERENCES rebuild_documents (doc_id),
    ingested_at   timestamptz NOT NULL DEFAULT now(),
    source_hash   text                     -- dedupe identical re-sends
);

CREATE INDEX IF NOT EXISTS idx_rdocs_case   ON rebuild_documents (case_id);
CREATE INDEX IF NOT EXISTS idx_rdocs_type   ON rebuild_documents (doc_type_id);
CREATE INDEX IF NOT EXISTS idx_rdocs_hash   ON rebuild_documents (source_hash);

-- ── Structural tree (the Docling-shaped part) ───────────────────────
-- One row per structural element, in reading order, with a parent.
-- `section_path` is the materialised breadcrumb so a retrieval filter
-- or a prompt can carry context without walking the tree:
--   'ایف آئی آر > تفصیلِ واقعہ'
--   'زمنی > زمنی نمبر 3 > برآمدگی'
CREATE TABLE IF NOT EXISTS doc_elements (
    element_id    bigserial PRIMARY KEY,
    doc_id        text NOT NULL REFERENCES rebuild_documents (doc_id) ON DELETE CASCADE,
    parent_id     bigint REFERENCES doc_elements (element_id) ON DELETE CASCADE,
    element_type  text NOT NULL,           -- heading|paragraph|list_item|table|table_row|
                                           -- key_value|signature|stamp|caption|footnote
    heading_level integer,                 -- 1..6, NULL for non-headings
    ordinal       integer NOT NULL,        -- reading order within the parent
    section_path  text,                    -- materialised breadcrumb
    label         text,                    -- key for key_value rows ('دفعات', 'تاریخ')
    text          text,                    -- element text, NULL for pure containers
    -- Table structure preserved rather than flattened to prose.
    table_row     integer,
    table_col     integer,
    -- Provenance into the original payload.
    char_start    integer,
    char_end      integer,
    page_no       integer,
    confidence    numeric
);

CREATE INDEX IF NOT EXISTS idx_delem_doc     ON doc_elements (doc_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_delem_parent  ON doc_elements (parent_id);
CREATE INDEX IF NOT EXISTS idx_delem_type    ON doc_elements (element_type);
CREATE INDEX IF NOT EXISTS idx_delem_section ON doc_elements (doc_id, section_path);

-- ── Spans: the provenance anchor every extracted fact points at ─────
-- A span is an exact character range in a document. Structured rows
-- (case_parties, weapons, case_statutes, case_events) carry span_id, so
-- a SQL-computed number can cite the sentence it came from.
CREATE TABLE IF NOT EXISTS doc_spans (
    span_id    bigserial PRIMARY KEY,
    doc_id     text NOT NULL REFERENCES rebuild_documents (doc_id) ON DELETE CASCADE,
    element_id bigint REFERENCES doc_elements (element_id) ON DELETE SET NULL,
    char_start integer NOT NULL,
    char_end   integer NOT NULL,
    text       text NOT NULL,
    CHECK (char_end > char_start)
);

CREATE INDEX IF NOT EXISTS idx_dspans_doc     ON doc_spans (doc_id);
CREATE INDEX IF NOT EXISTS idx_dspans_element ON doc_spans (element_id);

-- ── Chunks: the retrieval unit ──────────────────────────────────────
-- Structure-aware rather than a fixed window: a chunk never crosses a
-- section boundary and never splits a table row. `section_path` and
-- `heading_trail` are denormalised onto the chunk so retrieval can
-- filter and prompts can show context without a join.
CREATE TABLE IF NOT EXISTS rebuild_chunks (
    chunk_id      text PRIMARY KEY,
    doc_id        text NOT NULL REFERENCES rebuild_documents (doc_id) ON DELETE CASCADE,
    span_id       bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL,
    element_id    bigint REFERENCES doc_elements (element_id) ON DELETE SET NULL,
    ordinal       integer NOT NULL,        -- reading order within the document
    text          text NOT NULL,
    -- Context carried WITH the chunk, so an embedded passage knows where
    -- it came from. Measured motivation: a zimni recovery paragraph and
    -- an FIR narrative paragraph are near-identical in isolation.
    section_path  text,
    heading_trail text,
    element_type  text,
    -- Scope and language, used by the retrieval filter and the reranker.
    case_id       text,
    is_global     boolean NOT NULL DEFAULT false,
    language      text,
    is_roman_urdu boolean NOT NULL DEFAULT false,
    -- Retrieval plumbing.
    token_count   integer,
    embedding_ref text,                    -- id in the vector store
    tsv           tsvector,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_rchunks_doc     ON rebuild_chunks (doc_id, ordinal);
CREATE INDEX IF NOT EXISTS idx_rchunks_case    ON rebuild_chunks (case_id);
CREATE INDEX IF NOT EXISTS idx_rchunks_global  ON rebuild_chunks (is_global);
CREATE INDEX IF NOT EXISTS idx_rchunks_section ON rebuild_chunks (doc_id, section_path);
CREATE INDEX IF NOT EXISTS idx_rchunks_tsv     ON rebuild_chunks USING gin (tsv);

-- ── Chunk-to-chunk relationships ────────────────────────────────────
-- The cross-relationship layer. Retrieval returns a chunk; these edges
-- let it expand to the chunks that give it meaning, without a vector
-- search and without guessing.
--
--   NEXT / PREV        reading order, for stitching a truncated passage
--   PARENT / CHILD     section containment
--   CONTINUES          a table or list split across chunks
--   REFERENCES         explicit textual reference (zimni -> its FIR,
--                      chalaan -> a statute section)
--   SAME_TABLE         rows of one table, so a table is retrievable whole
CREATE TABLE IF NOT EXISTS chunk_relations (
    from_chunk_id text NOT NULL REFERENCES rebuild_chunks (chunk_id) ON DELETE CASCADE,
    to_chunk_id   text NOT NULL REFERENCES rebuild_chunks (chunk_id) ON DELETE CASCADE,
    relation      text NOT NULL,
    confidence    numeric,
    PRIMARY KEY (from_chunk_id, to_chunk_id, relation)
);

CREATE INDEX IF NOT EXISTS idx_crel_to ON chunk_relations (to_chunk_id, relation);

-- ── Chunk-to-fact mentions ──────────────────────────────────────────
-- The join between unstructured and structured. Every extracted row
-- records which chunk(s) asserted it, and every chunk can list the facts
-- it contains.
--
-- Deliberately generic (entity_table + entity_pk) rather than one FK
-- column per table: the fact types will grow, and a Safe City feed will
-- add types this schema has not seen. `entity_table` is constrained by
-- CHECK rather than FK so adding a type is a one-line change.
CREATE TABLE IF NOT EXISTS chunk_mentions (
    mention_id   bigserial PRIMARY KEY,
    chunk_id     text NOT NULL REFERENCES rebuild_chunks (chunk_id) ON DELETE CASCADE,
    span_id      bigint REFERENCES doc_spans (span_id) ON DELETE SET NULL,
    entity_table text NOT NULL,            -- 'persons','weapons','case_statutes',...
    entity_pk    text NOT NULL,            -- the row's primary key, as text
    mention_text text,                     -- the surface form as written
    role_hint    text,                     -- how it was mentioned, pre-resolution
    confidence   numeric,
    CHECK (entity_table IN (
        'persons','case_parties','weapons','vehicles','phone_numbers',
        'addresses','seized_items','case_statutes','case_events',
        'officers','police_stations','cases'
    ))
);

CREATE INDEX IF NOT EXISTS idx_cmention_chunk  ON chunk_mentions (chunk_id);
CREATE INDEX IF NOT EXISTS idx_cmention_entity ON chunk_mentions (entity_table, entity_pk);

COMMIT;
