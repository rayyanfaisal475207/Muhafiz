-- Semantic binding: let the embedding model say what a question is ABOUT.
--
-- WHY THIS EXISTS
--
-- bind_terms() matches a question's words against identifiers and stored
-- values with trigram and exact comparison. That is string matching, and
-- it works for English only by luck: English questions use English words,
-- and the identifiers are in English.
--
-- An Urdu question has no such luck. Its words collide with stored VALUES,
-- because the values are the part of this database that is in Urdu.
-- MEASURED, on "کتنے ہتھیار برآمد ہوئے؟" (how many weapons were recovered):
--
--   'ہتھیار'  -> weapons                          table  1.000
--   'برآمد'   -> case_events.event_type_raw        value  1.000
--
-- A tie. The agent took the event type and answered 7; the truth is 96.
--
-- The multilingual embedding model already running for passage retrieval
-- has no such difficulty. Asked to rank tables against the same question:
--
--   0.8348  weapons        <- correct, first
--   0.8213  seized_items
--   0.8066  vehicles       (case_events does not reach the top five)
--
-- It was never consulted. This table is where its answer lives.
--
-- WHAT IS EMBEDDED
--
-- The schema, never the data: an object's identifier, its COMMENT, and
-- the multilingual terms in schema_terms. Small (tens of tables, hundreds
-- of columns), stable, and it does not grow with the corpus — so this is
-- bounded work that stays bounded at a million cases.
--
-- WHY float8[] AND NOT A VECTOR TYPE
--
-- pgvector is not installed on this server. 1024 floats per row over a
-- few hundred rows is small enough to load once into the process and
-- score there; the ANN index pgvector would provide buys nothing at this
-- cardinality. The column is plain float8[] so that installing pgvector
-- later is a type change, not a rebuild.

CREATE TABLE IF NOT EXISTS schema_embeddings (
    table_name   text        NOT NULL,
    column_name  text,                       -- NULL = the table itself
    doc          text        NOT NULL,       -- exactly what was embedded
    embedding    float8[]    NOT NULL,
    dims         int         NOT NULL,
    model        text        NOT NULL,
    built_at     timestamptz NOT NULL DEFAULT now()
);

-- NULL column_name means "the table itself", the same convention
-- schema_terms uses. A composite PRIMARY KEY cannot express that -- it
-- forces NOT NULL -- and in SQL two NULLs are not equal, so a plain
-- UNIQUE constraint would let one table accumulate duplicate rows.
-- Keying on coalesce() keeps NULL meaning what it means everywhere else
-- and still gives ON CONFLICT something to match.
CREATE UNIQUE INDEX IF NOT EXISTS schema_embeddings_object_idx
    ON schema_embeddings (table_name, (coalesce(column_name, '')));

COMMENT ON TABLE schema_embeddings IS
  'One embedding per table and column, of its name, purpose and '
  'multilingual terms. Lets a question be matched to the schema by '
  'meaning rather than by spelling.';

COMMENT ON COLUMN schema_embeddings.doc IS
  'The exact text embedded. Kept so a bad match can be explained and '
  'so a change of source text is visible without re-embedding.';

CREATE INDEX IF NOT EXISTS schema_embeddings_table_idx
    ON schema_embeddings (table_name);

-- Whether the semantic index still covers the schema. A stale or partial
-- index must be visible: semantic binding that silently covers half the
-- tables degrades every Urdu question and looks like nothing at all.
CREATE OR REPLACE VIEW schema_embedding_freshness AS
SELECT
    (SELECT count(*) FROM schema_embeddings WHERE column_name IS NULL)
        AS tables_embedded,
    (SELECT count(*) FROM table_roles
      WHERE is_evidence AND role IN ('fact','dimension','entity'))
        AS tables_expected,
    (SELECT count(*) FROM schema_embeddings WHERE column_name IS NOT NULL)
        AS columns_embedded,
    (SELECT count(DISTINCT model) FROM schema_embeddings) AS models,
    (SELECT max(built_at) FROM schema_embeddings)          AS built_at;

GRANT SELECT ON schema_embeddings, schema_embedding_freshness TO PUBLIC;
