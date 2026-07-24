# ============================================================
# Pipeline Logger — Writes Every Pipeline Event to SQLite
#
# This module is the ONLY place that writes to the database.
# All other modules call these functions; none touch SQLite directly.
# This keeps DB logic centralized and makes testing easier.
#
# DESIGN:
#   - All functions are synchronous (SQLite writes are fast — <1ms)
#   - Each function maps to one table or one logical operation
#   - Functions never raise — they log errors and continue, so a DB
#     failure never crashes the pipeline
#   - Previews (first N chars) are stored, not full content, to keep
#     the DB small and fast
# ============================================================

import logging
import time

from src.database.db import get_connection

logger = logging.getLogger(__name__)

PREVIEW_LEN = 300   # chars stored in *_preview columns


# ── Session Operations ─────────────────────────────────────────────────────────

def upsert_session(session_id: str) -> None:
    """
    Insert a new session or update last_active if it already exists.
    Called at the start of every process_query() invocation.
    """
    try:
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO sessions (session_id, last_active)
                VALUES (?, strftime('%Y-%m-%dT%H:%M:%fZ','now'))
                ON CONFLICT(session_id) DO UPDATE SET
                    last_active   = excluded.last_active,
                    message_count = message_count + 1
            """, (session_id,))
    except Exception as exc:
        logger.error("DB upsert_session failed: %s", exc)


# ── Query Operations ───────────────────────────────────────────────────────────

def create_query(session_id: str, user_message: str) -> int:
    """
    Insert a new query row and return its query_id.
    Called at the start of process_query() before any pipeline steps run.

    Returns:
        The new query_id (used to link all subsequent log entries).
        Returns -1 on failure so the pipeline continues without crashing.
    """
    try:
        with get_connection() as conn:
            cur = conn.execute("""
                INSERT INTO queries (session_id, user_message)
                VALUES (?, ?)
            """, (session_id, user_message))
            return cur.lastrowid
    except Exception as exc:
        logger.error("DB create_query failed: %s", exc)
        return -1


def update_query(
    query_id: int,
    *,
    rewritten_query: str = None,
    needs_rag: bool = None,
    retry_count: int = None,
    response_type: str = None,
    final_response: str = None,
    total_duration_ms: int = None,
    verifier_passed: bool = None,
    verifier_regenerated: bool = None,
) -> None:
    """
    Update query fields as the pipeline progresses.
    Uses keyword-only arguments so callers only specify what changed.
    """
    if query_id < 0:
        return

    # Build SET clause dynamically based on non-None kwargs
    fields, values = [], []
    if rewritten_query is not None:
        fields.append("rewritten_query = ?");    values.append(rewritten_query)
    if needs_rag is not None:
        fields.append("needs_rag = ?");          values.append(int(needs_rag))
    if retry_count is not None:
        fields.append("retry_count = ?");        values.append(retry_count)
    if response_type is not None:
        fields.append("response_type = ?");      values.append(response_type)
    if final_response is not None:
        fields.append("final_response = ?");     values.append(final_response)
    if total_duration_ms is not None:
        fields.append("total_duration_ms = ?");  values.append(total_duration_ms)
    if verifier_passed is not None:
        fields.append("verifier_passed = ?");    values.append(int(verifier_passed))
    if verifier_regenerated is not None:
        fields.append("verifier_regenerated = ?"); values.append(int(verifier_regenerated))

    if not fields:
        return

    try:
        with get_connection() as conn:
            conn.execute(
                f"UPDATE queries SET {', '.join(fields)} WHERE query_id = ?",
                (*values, query_id),
            )
    except Exception as exc:
        logger.error("DB update_query failed: %s", exc)


# ── Pipeline Step Operations ───────────────────────────────────────────────────

def log_step(
    query_id: int,
    step_name: str,
    status: str,
    detail: str = "",
    duration_ms: int = None,
    retry_number: int = 0,
) -> int:
    """
    Insert one pipeline step log row.

    Args:
        query_id:     The parent query (from create_query).
        step_name:    One of: query_rewriter, router, retrieval, reranker,
                      evaluator, response, memory.
        status:       One of: active, done, skipped, error.
        detail:       Human-readable description (matches SSE event detail).
        duration_ms:  How long this step took (None for 'active' entries).
        retry_number: 0 = first attempt, 1+ = retry.

    Returns:
        The new step_id (or -1 on failure).
    """
    if query_id < 0:
        return -1
    try:
        with get_connection() as conn:
            cur = conn.execute("""
                INSERT INTO pipeline_steps
                    (query_id, step_name, status, detail, duration_ms, retry_number)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (query_id, step_name, status, detail[:PREVIEW_LEN], duration_ms, retry_number))
            return cur.lastrowid
    except Exception as exc:
        logger.error("DB log_step failed: %s", exc)
        return -1


# ── LLM Call Operations ────────────────────────────────────────────────────────

def log_llm_call(
    query_id: int,
    call_type: str,
    provider: str,
    model: str,
    system_prompt: str = "",
    user_input: str = "",
    response: str = "",
    duration_ms: int = None,
    retry_number: int = 0,
) -> None:
    """
    Log one LLM API call with previews of the prompt and response.

    call_type values:
        'rewriter'       → Query Rewriter (LLM Call 1, initial)
        'router'         → Router (LLM Call 2)
        'evaluator'      → Relevance Evaluator (LLM Call 3)
        'response'       → Final Response (LLM Call 4)
        'retry_rewriter' → Query Rewriter during retry loop
        'direct_response'→ Direct response (no RAG path)
    """
    if query_id < 0:
        return
    try:
        with get_connection() as conn:
            conn.execute("""
                INSERT INTO llm_calls
                    (query_id, call_type, provider, model,
                     system_prompt_preview, user_input_preview, response_preview,
                     duration_ms, retry_number)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                query_id, call_type, provider, model,
                system_prompt[:PREVIEW_LEN],
                user_input[:PREVIEW_LEN],
                response[:PREVIEW_LEN],
                duration_ms,
                retry_number,
            ))
    except Exception as exc:
        logger.error("DB log_llm_call failed: %s", exc)


# ── Retrieved Documents Operations ────────────────────────────────────────────

def log_retrieved_docs(
    query_id: int,
    chunks: list[dict],
    retrieval_method: str = "rrf",
    is_relevant: bool = None,
    retry_number: int = 0,
) -> None:
    """
    Log all retrieved document chunks for one retrieval attempt.

    Args:
        query_id:         Parent query.
        chunks:           List of chunk dicts from vector_store / reranker.
                          Each dict must have: id, text, metadata, optionally rrf_score.
        retrieval_method: 'semantic' | 'bm25' | 'rrf'
        is_relevant:      Set by the evaluator after this retrieval. None = not yet evaluated.
        retry_number:     Which retry attempt this retrieval belongs to.
    """
    if query_id < 0 or not chunks:
        return
    try:
        rows = [
            (
                query_id,
                chunk.get("id", ""),
                chunk.get("metadata", {}).get("source", "unknown"),
                chunk.get("text", "")[:PREVIEW_LEN],
                retrieval_method,
                chunk.get("rrf_score"),
                rank + 1,
                (int(is_relevant) if is_relevant is not None else None),
                retry_number,
            )
            for rank, chunk in enumerate(chunks)
        ]
        with get_connection() as conn:
            conn.executemany("""
                INSERT INTO retrieved_documents
                    (query_id, doc_chunk_id, source_file, chunk_text_preview,
                     retrieval_method, rrf_score, rank_position, is_relevant, retry_number)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, rows)
    except Exception as exc:
        logger.error("DB log_retrieved_docs failed: %s", exc)


def update_retrieved_docs_relevance(
    query_id: int,
    is_relevant: bool,
    retry_number: int = 0,
) -> None:
    """
    Update the is_relevant flag for all chunks from a specific retrieval attempt.
    Called after the evaluator returns its verdict.
    """
    if query_id < 0:
        return
    try:
        with get_connection() as conn:
            conn.execute("""
                UPDATE retrieved_documents
                SET is_relevant = ?
                WHERE query_id = ? AND retry_number = ?
            """, (int(is_relevant), query_id, retry_number))
    except Exception as exc:
        logger.error("DB update_retrieved_docs_relevance failed: %s", exc)


# ── Ingested Documents Operations ──────────────────────────────────────────────

def log_ingested_chunk(
    chunk_id: str,
    source_file: str,
    source_path: str,
    file_type: str,
    chunk_index: int,
    chunk_total: int,
    chunk_text: str,
    embedding_model: str,
    embedding_dims: int,
) -> None:
    """
    Record one ingested chunk in the knowledge base registry.
    Uses INSERT OR REPLACE so re-ingestion updates existing records (matches upsert in ChromaDB).
    """
    try:
        with get_connection() as conn:
            conn.execute("""
                INSERT OR REPLACE INTO ingested_documents
                    (chunk_id, source_file, source_path, file_type,
                     chunk_index, chunk_total, chunk_text_preview, char_count,
                     embedding_model, embedding_dims)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                chunk_id, source_file, source_path, file_type,
                chunk_index, chunk_total,
                chunk_text[:PREVIEW_LEN], len(chunk_text),
                embedding_model, embedding_dims,
            ))
    except Exception as exc:
        logger.error("DB log_ingested_chunk failed: %s", exc)


# ── Query Functions (for API endpoints) ───────────────────────────────────────

def get_session_stats(session_id: str) -> dict:
    """Return aggregate stats for a session."""
    try:
        with get_connection() as conn:
            row = conn.execute("""
                SELECT
                    s.message_count,
                    s.created_at,
                    s.last_active,
                    COUNT(q.query_id)         AS total_queries,
                    AVG(q.total_duration_ms)  AS avg_duration_ms,
                    SUM(q.needs_rag)          AS rag_queries,
                    SUM(q.retry_count)        AS total_retries
                FROM sessions s
                LEFT JOIN queries q ON q.session_id = s.session_id
                WHERE s.session_id = ?
                GROUP BY s.session_id
            """, (session_id,)).fetchone()
            return dict(row) if row else {}
    except Exception as exc:
        logger.error("DB get_session_stats failed: %s", exc)
        return {}


def get_ingested_files_summary() -> list[dict]:
    """Return one row per source file with chunk count and ingestion time."""
    try:
        with get_connection() as conn:
            rows = conn.execute("""
                SELECT
                    source_file,
                    file_type,
                    COUNT(*)        AS chunk_count,
                    SUM(char_count) AS total_chars,
                    MIN(ingested_at) AS first_ingested,
                    MAX(ingested_at) AS last_ingested
                FROM ingested_documents
                GROUP BY source_file, file_type
                ORDER BY last_ingested DESC
            """).fetchall()
            return [dict(r) for r in rows]
    except Exception as exc:
        logger.error("DB get_ingested_files_summary failed: %s", exc)
        return []


def delete_ingested_file(source_file: str) -> None:
    """Delete all chunks for a source file from the ingested_documents table."""
    try:
        with get_connection() as conn:
            conn.execute("""
                DELETE FROM ingested_documents
                WHERE source_file = ?
            """, (source_file,))
    except Exception as exc:
        logger.error("DB delete_ingested_file failed: %s", exc)


# ============================================================
# Async PostgreSQL Pipeline Logger
#
# Provides async equivalents of the synchronous SQLite functions
# above, writing to PostgreSQL via SQLAlchemy ORM models instead
# of raw SQL.
#
# All methods follow the same "never crash" pattern: errors are
# logged and silently swallowed so a DB failure cannot bring
# down the pipeline.
#
# Status mapping:
#   The existing SQLite logger uses statuses:
#       active, done, skipped, error
#   The Postgres schema (via CheckConstraint) expects:
#       success, skipped, retry, failed
#   The helper _map_status() bridges the two vocabularies.
# ============================================================

import uuid as _uuid  # avoid shadowing the existing `import uuid` if any

from sqlalchemy import select


class PgPipelineLogger:
    """
    Async pipeline logger that writes to PostgreSQL.

    Each public method mirrors one of the module-level sync functions
    (upsert_session, create_query → create_run, etc.) but targets the
    ORM models defined in ``src.database.models``.
    """

    # ── Status Mapping ─────────────────────────────────────────────────────

    _STATUS_MAP = {
        "done":    "success",
        "active":  "success",
        "error":   "failed",
        "retry":   "retry",
        "skipped": "skipped",
    }

    @staticmethod
    def _map_status(status: str) -> str:
        """
        Convert a SQLite-style status to the Postgres CHECK constraint
        vocabulary.  Unknown statuses pass through unchanged (the DB
        constraint will reject them, but the try/except will catch that).
        """
        return PgPipelineLogger._STATUS_MAP.get(status, status)

    # ── Session Operations ─────────────────────────────────────────────────

    async def upsert_session(self, session_id: str, user_id: str = None) -> None:
        """Create or touch a Session row in PostgreSQL.

        Sessions must always carry an owner: creating a row without user_id
        makes it invisible to /api/sessions (which filters by owner) and
        causes 403s when the frontend tries to reopen it. We therefore set
        user_id on insert AND backfill it on legacy rows where it is NULL.
        """
        try:
            from src.database.postgres import get_session as _pg_session
            from src.database.models import Session as SessionModel
            from datetime import datetime

            async with _pg_session() as session:
                sid = _uuid.UUID(session_id)
                uid = _uuid.UUID(str(user_id)) if user_id else None
                result = await session.execute(
                    select(SessionModel).where(SessionModel.session_id == sid)
                )
                existing = result.scalars().first()
                if existing is not None:
                    existing.updated_at = datetime.utcnow()
                    if uid is not None and existing.user_id is None:
                        existing.user_id = uid
                else:
                    session.add(SessionModel(session_id=sid, user_id=uid))
        except Exception as exc:
            logger.error("PG upsert_session failed: %s", exc)

    # ── Run Operations ─────────────────────────────────────────────────────

    async def create_run(self, session_id: str, user_message: str) -> str:
        """
        Create a new PipelineRun and return its run_id as a string.

        Returns an empty string on failure so the pipeline can continue.
        """
        try:
            from src.database.postgres import get_session as _pg_session
            from src.database.models import PipelineRun

            run_id = _uuid.uuid4()
            async with _pg_session() as session:
                session.add(PipelineRun(
                    run_id=run_id,
                    session_id=_uuid.UUID(session_id),
                    original_query=user_message,
                ))
            return str(run_id)
        except Exception as exc:
            logger.error("PG create_run failed: %s", exc)
            return ""

    async def update_run(self, run_id: str, **kwargs) -> None:
        """
        Update fields on an existing PipelineRun.

        Accepted keyword arguments:
            rewritten_query, routed_to, retry_count,
            final_outcome, total_duration_ms
        """
        if not run_id:
            return
        try:
            from src.database.postgres import get_session as _pg_session
            from src.database.models import PipelineRun

            allowed = {
                "rewritten_query", "routed_to", "retry_count",
                "final_outcome", "total_duration_ms",
            }
            updates = {k: v for k, v in kwargs.items() if k in allowed and v is not None}
            if not updates:
                return

            async with _pg_session() as session:
                result = await session.execute(
                    select(PipelineRun).where(
                        PipelineRun.run_id == _uuid.UUID(run_id)
                    )
                )
                run = result.scalars().first()
                if run is not None:
                    for key, value in updates.items():
                        setattr(run, key, value)
        except Exception as exc:
            logger.error("PG update_run failed: %s", exc)

    # ── Step Operations ────────────────────────────────────────────────────

    async def log_step(
        self,
        run_id: str,
        step_name: str,
        step_order: int,
        status: str,
        duration_ms: int = None,
        input_summary: dict = None,
        output_summary: dict = None,
    ) -> None:
        """Insert one pipeline step log row into PostgreSQL."""
        if not run_id:
            return
        try:
            from src.database.postgres import get_session as _pg_session
            from src.database.models import PipelineStep

            async with _pg_session() as session:
                session.add(PipelineStep(
                    run_id=_uuid.UUID(run_id),
                    step_name=step_name,
                    step_order=step_order,
                    status=self._map_status(status),
                    duration_ms=duration_ms,
                    input_summary=input_summary,
                    output_summary=output_summary,
                ))
        except Exception as exc:
            logger.error("PG log_step failed: %s", exc)

    # ── Document Operations ────────────────────────────────────────────────

    async def log_document(
        self,
        doc_id: str,
        filename: str,
        doc_type: str = None,
        chunk_count: int = None,
        is_global: bool = False,
    ) -> None:
        """
        Insert or update a Document row in PostgreSQL.

        Uses merge semantics: if a row with the same doc_id already
        exists its fields are updated, otherwise a new row is inserted.
        """
        try:
            from src.database.postgres import get_session as _pg_session
            from src.database.models import Document

            async with _pg_session() as session:
                result = await session.execute(
                    select(Document).where(Document.doc_id == doc_id)
                )
                existing = result.scalars().first()
                if existing is not None:
                    existing.filename = filename
                    if doc_type is not None:
                        existing.doc_type = doc_type
                    if chunk_count is not None:
                        existing.chunk_count = chunk_count
                    existing.is_global = is_global
                else:
                    session.add(Document(
                        doc_id=doc_id,
                        filename=filename,
                        doc_type=doc_type,
                        chunk_count=chunk_count,
                        is_global=is_global,
                    ))
        except Exception as exc:
            logger.error("PG log_document failed: %s", exc)

    async def log_mcp_tool_call(
        self,
        run_id: str,
        mcp_server: str,
        tool_name: str,
        status: str,
        input_params: dict = None,
        output_summary: dict = None,
        duration_ms: int = None,
        rejected_by_role: bool = False,
    ) -> None:
        """Insert an MCP tool call log into PostgreSQL."""
        if not run_id:
            return
        try:
            from src.database.postgres import get_session as _pg_session
            from src.database.models import McpToolCall
            import uuid

            async with _pg_session() as session:
                session.add(McpToolCall(
                    run_id=uuid.UUID(run_id),
                    mcp_server=mcp_server,
                    tool_name=tool_name,
                    input_params=input_params,
                    output_summary=output_summary,
                    status=status,
                    duration_ms=duration_ms,
                    rejected_by_role=rejected_by_role,
                ))
        except Exception as exc:
            logger.error("PG log_mcp_tool_call failed: %s", exc)

    async def log_generated_file(
        self,
        session_id: str,
        user_id: str,
        file_type: str,
        file_name: str,
        storage_path: str,
        file_size_bytes: int = None,
        message_id: str = None
    ) -> str:
        """Log a generated file to PostgreSQL. Returns the new file_id."""
        try:
            from src.database.postgres import get_session as _pg_session
            from src.database.models import GeneratedFile
            import uuid

            file_id = uuid.uuid4()
            async with _pg_session() as session:
                session.add(
                    GeneratedFile(
                        file_id=file_id,
                        session_id=_uuid.UUID(session_id),
                        user_id=_uuid.UUID(user_id),
                        message_id=_uuid.UUID(message_id) if message_id else None,
                        file_type=file_type,
                        file_name=file_name,
                        storage_path=storage_path,
                        file_size_bytes=file_size_bytes
                    )
                )
            return str(file_id)
        except Exception as exc:
            logger.error("PG log_generated_file failed: %s", exc)
            return None


# ── Module-level Instance ─────────────────────────────────────────────────────
# Created only when PostgreSQL is configured so that importing this module
# without a running Postgres instance has zero side-effects.

def _make_pg_logger():
    """Lazily import postgres helpers to avoid circular imports."""
    try:
        from src.database.postgres import is_postgres_configured
        if is_postgres_configured():
            return PgPipelineLogger()
    except Exception:
        pass
    return None

pg_pipeline_logger = _make_pg_logger()
