# ============================================================
# Database Schema — Normalized Relational SQLite
#
# NORMALIZATION LEVEL: Third Normal Form (3NF)
#
# WHY 3NF?
#   1NF: Every cell holds one atomic value — no arrays, no repeating groups.
#   2NF: Every non-key attribute depends on the WHOLE primary key (not part of it).
#   3NF: No transitive dependencies — non-key attributes depend ONLY on the PK,
#        not on other non-key attributes.
#
# SCHEMA OVERVIEW (parent → child relationships):
#
#   sessions         (1) ──→ (M) queries
#   queries          (1) ──→ (M) pipeline_steps
#   queries          (1) ──→ (M) llm_calls
#   queries          (1) ──→ (M) retrieved_documents
#   ingested_documents         (independent — represents knowledge base state)
#
# Each table has a single integer primary key (surrogate key) to avoid
# duplication and allow clean foreign key references.
#
# INDEXES: Added on all foreign keys and commonly filtered columns
# (session_id, query_id, step_name, source_file) for query performance.
# ============================================================

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path

from src import config

logger = logging.getLogger(__name__)

# ── Schema DDL ────────────────────────────────────────────────────────────────

_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ── sessions ──────────────────────────────────────────────────────────────────
-- One row per conversation session (identified by session_id UUID from frontend).
-- Stores lifecycle metadata only. Message content lives in the queries table.
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT        PRIMARY KEY,           -- UUID from frontend
    created_at      TIMESTAMP   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_active     TIMESTAMP   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    message_count   INTEGER     NOT NULL DEFAULT 0
);

-- ── queries ───────────────────────────────────────────────────────────────────
-- One row per user message / pipeline invocation.
-- Contains the full pipeline result: rewritten query, routing decision, response.
-- FK: session_id → sessions.session_id  (CASCADE DELETE cleans up on session removal)
CREATE TABLE IF NOT EXISTS queries (
    query_id            INTEGER     PRIMARY KEY AUTOINCREMENT,
    session_id          TEXT        NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    user_message        TEXT        NOT NULL,       -- original user input
    rewritten_query     TEXT,                       -- output of Query Rewriter (LLM Call 1)
    needs_rag           INTEGER,                    -- 0=NO, 1=YES (Router output)
    retry_count         INTEGER     NOT NULL DEFAULT 0,
    response_type       TEXT,                       -- 'rag' | 'direct' | 'safe' | 'sql' | ...
    final_response      TEXT,                       -- the answer returned to the user
    total_duration_ms   INTEGER,                    -- wall-clock time for full pipeline
    verifier_passed     INTEGER,                    -- 1=passed, 0=failed/abstained (Phase 6)
    verifier_regenerated INTEGER,                   -- 1=answer replaced by abstention (Phase 6)
    created_at          TIMESTAMP   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_queries_session ON queries(session_id);
CREATE INDEX IF NOT EXISTS idx_queries_created ON queries(created_at);

-- ── pipeline_steps ────────────────────────────────────────────────────────────
-- One row per pipeline step per query (and per retry for retried steps).
-- Captures the real-time trace that the frontend pipeline panel displays.
-- FK: query_id → queries.query_id
CREATE TABLE IF NOT EXISTS pipeline_steps (
    step_id         INTEGER     PRIMARY KEY AUTOINCREMENT,
    query_id        INTEGER     NOT NULL REFERENCES queries(query_id) ON DELETE CASCADE,
    step_name       TEXT        NOT NULL,   -- 'query_rewriter'|'router'|'retrieval'|'reranker'|'evaluator'|'response'|'memory'
    status          TEXT        NOT NULL,   -- 'active'|'done'|'skipped'|'error'
    detail          TEXT,                   -- human-readable detail (e.g. "Rewritten: '...'")
    duration_ms     INTEGER,                -- how long this step took
    retry_number    INTEGER     NOT NULL DEFAULT 0,  -- 0 = first attempt, 1+ = retry
    created_at      TIMESTAMP   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_steps_query  ON pipeline_steps(query_id);
CREATE INDEX IF NOT EXISTS idx_steps_name   ON pipeline_steps(step_name);

-- ── llm_calls ─────────────────────────────────────────────────────────────────
-- One row per LLM API call (3-4 calls per full RAG query).
-- Enables cost tracking, latency analysis, and debugging.
-- FK: query_id → queries.query_id
CREATE TABLE IF NOT EXISTS llm_calls (
    call_id                 INTEGER     PRIMARY KEY AUTOINCREMENT,
    query_id                INTEGER     NOT NULL REFERENCES queries(query_id) ON DELETE CASCADE,
    call_type               TEXT        NOT NULL,   -- 'rewriter'|'router'|'evaluator'|'response'|'retry_rewriter'
    provider                TEXT        NOT NULL,   -- 'groq'|'gemini'|'openai'|'anthropic'
    model                   TEXT        NOT NULL,
    system_prompt_preview   TEXT,                   -- first 300 chars (avoid storing full prompts)
    user_input_preview      TEXT,                   -- first 300 chars
    response_preview        TEXT,                   -- first 300 chars
    duration_ms             INTEGER,
    retry_number            INTEGER     NOT NULL DEFAULT 0,
    created_at              TIMESTAMP   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_llm_calls_query    ON llm_calls(query_id);
CREATE INDEX IF NOT EXISTS idx_llm_calls_provider ON llm_calls(provider);
CREATE INDEX IF NOT EXISTS idx_llm_calls_type     ON llm_calls(call_type);

-- ── retrieved_documents ───────────────────────────────────────────────────────
-- One row per document chunk considered during retrieval.
-- Covers both initial retrieval and retry attempts.
-- FK: query_id → queries.query_id
CREATE TABLE IF NOT EXISTS retrieved_documents (
    ret_id              INTEGER     PRIMARY KEY AUTOINCREMENT,
    query_id            INTEGER     NOT NULL REFERENCES queries(query_id) ON DELETE CASCADE,
    doc_chunk_id        TEXT        NOT NULL,   -- matches ChromaDB chunk id
    source_file         TEXT        NOT NULL,
    chunk_text_preview  TEXT,                   -- first 200 chars
    retrieval_method    TEXT,                   -- 'semantic'|'bm25'|'rrf'
    rrf_score           REAL,
    rank_position       INTEGER,
    is_relevant         INTEGER,               -- 1=evaluator said relevant, 0=not, NULL=not evaluated
    retry_number        INTEGER     NOT NULL DEFAULT 0,
    created_at          TIMESTAMP   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_ret_docs_query  ON retrieved_documents(query_id);
CREATE INDEX IF NOT EXISTS idx_ret_docs_source ON retrieved_documents(source_file);

-- ── ingested_documents ────────────────────────────────────────────────────────
-- Registry of every chunk stored in ChromaDB.
-- Independent table (not linked to queries) — represents the knowledge base state.
-- Useful for: listing ingested files, deduplication checks, audit trail.
CREATE TABLE IF NOT EXISTS ingested_documents (
    chunk_id            TEXT        PRIMARY KEY,   -- matches ChromaDB doc_id (deterministic)
    source_file         TEXT        NOT NULL,
    source_path         TEXT,
    file_type           TEXT        NOT NULL,      -- 'pdf'|'txt'|'xlsx'|'csv'|'html'|'docx'|'image'
    chunk_index         INTEGER,                   -- position within parent document
    chunk_total         INTEGER,                   -- total chunks from this file
    chunk_text_preview  TEXT,                      -- first 200 chars
    char_count          INTEGER,
    embedding_model     TEXT,                      -- which embedding model was used
    embedding_dims      INTEGER,                   -- 3072 for gemini-embedding-001
    ingested_at         TIMESTAMP   NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_ingested_source ON ingested_documents(source_file);
CREATE INDEX IF NOT EXISTS idx_ingested_type   ON ingested_documents(file_type);
"""


# ── Connection Management ──────────────────────────────────────────────────────

def get_db_path() -> Path:
    return Path(config.DB_PATH)


def init_db() -> None:
    """
    Create all tables and indexes if they don't exist.
    Safe to call multiple times (all statements use IF NOT EXISTS).
    Called once at application startup.
    """
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)

    with sqlite3.connect(str(db_path)) as conn:
        conn.executescript(_SCHEMA_SQL)
        # Phase 6 migration: add verifier columns to existing databases.
        # SQLite doesn't support ALTER TABLE ADD COLUMN IF NOT EXISTS,
        # so we attempt each and swallow the "duplicate column" error.
        for col_def in (
            "ALTER TABLE queries ADD COLUMN verifier_passed INTEGER",
            "ALTER TABLE queries ADD COLUMN verifier_regenerated INTEGER",
        ):
            try:
                conn.execute(col_def)
            except sqlite3.OperationalError:
                pass  # column already exists — safe to ignore
        conn.commit()

    logger.info("Pipeline DB initialised at %s", db_path)


@contextmanager
def get_connection():
    """
    Context manager that yields a SQLite connection.

    Uses WAL mode (Write-Ahead Logging) for better concurrent read
    performance — readers don't block writers and vice versa.

    Usage:
        with get_connection() as conn:
            conn.execute("INSERT INTO ...")
    """
    conn = sqlite3.connect(
        str(get_db_path()),
        detect_types=sqlite3.PARSE_DECLTYPES | sqlite3.PARSE_COLNAMES,
    )
    conn.row_factory = sqlite3.Row   # allows dict-style column access
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
