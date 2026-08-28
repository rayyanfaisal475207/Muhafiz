# ============================================================
# Persistent full-text index (Graph Scale & Schema Expansion, Milestone
# A2) — a real Postgres tsvector/GIN index, maintained incrementally at
# ingest, replacing bm25_retriever.py's actual per-query cost: retrieve_
# bm25() building a fresh BM25Okapi index (full tokenization + term-
# frequency stats) over the ENTIRE scoped candidate pool, every query.
#
# See migrations/022_chunk_fulltext_index.sql for the table's own design
# rationale, including why `tsv` is built from already-tokenized text
# (src/ingestion/tokenizer.py's Urdu-aware `tokenize()`) rather than
# Postgres's own to_tsvector tokenizing the raw text.
#
# candidate_pool() is the actual scale fix: it narrows the pool BM25Okapi
# has to tokenize/score down to chunks that share at least one token with
# the query (a GIN lookup, index-backed), instead of every chunk in
# scope. retrieve_bm25()'s own ranking math is completely unchanged — this
# module only changes what candidate list it's handed.
# ============================================================

from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import text

from src.database.postgres import get_session
from src.ingestion.tokenizer import tokenize

logger = logging.getLogger(__name__)


def _tokenized_text(raw_text: str) -> str:
    """Space-joined tokens, via the SAME tokenizer bm25_retriever.py uses
    to build its corpus — kept as one call site so the two can never
    silently diverge (see this module's docstring)."""
    return " ".join(tokenize(raw_text))


async def maintain(chunk_id: str, raw_text: str, metadata: dict) -> None:
    """
    Upsert this chunk's full-text index row — called from
    src/retrieval/vector_store.py's upsert_documents() for every chunk
    written to Chroma, so the index is updated incrementally on ingest,
    never rebuilt wholesale.

    Same resilience contract as src/graph/identity_index.py's maintain():
    a failure here must never fail the ingest it's riding alongside. Any
    exception is logged and swallowed — retrieve_bm25() simply gets a
    stale/incomplete candidate pool for this chunk until the next
    successful write, which is a recall gap, not a correctness one (the
    chunk is still fully retrievable via vector search).
    """
    if not raw_text or not raw_text.strip():
        return
    try:
        async with get_session() as db:
            await db.execute(
                text(
                    """
                    INSERT INTO chunk_fulltext
                        (chunk_id, doc_id, source, project_id, case_id, is_global, text, metadata, tsv, updated_at)
                    VALUES
                        (:chunk_id, :doc_id, :source, :project_id, :case_id, :is_global, :raw_text,
                         CAST(:metadata_json AS JSONB), to_tsvector('simple', :tokenized_text), now())
                    ON CONFLICT (chunk_id) DO UPDATE SET
                        doc_id = EXCLUDED.doc_id, source = EXCLUDED.source,
                        project_id = EXCLUDED.project_id, case_id = EXCLUDED.case_id,
                        is_global = EXCLUDED.is_global, text = EXCLUDED.text,
                        metadata = EXCLUDED.metadata, tsv = EXCLUDED.tsv, updated_at = now()
                    """
                ),
                {
                    "chunk_id": chunk_id,
                    "doc_id": str(metadata.get("doc_id") or ""),
                    "source": metadata.get("source"),
                    "project_id": str(metadata["project_id"]) if metadata.get("project_id") else None,
                    "case_id": metadata.get("case_id"),
                    "is_global": bool(metadata.get("is_global", False)),
                    "raw_text": raw_text,
                    "metadata_json": json.dumps(metadata, default=str),
                    "tokenized_text": _tokenized_text(raw_text),
                },
            )
    except Exception as exc:
        logger.warning("fulltext_index.maintain: failed to index chunk %s (%s)", chunk_id, exc)


async def delete_by_ids(chunk_ids: list[str]) -> None:
    """Mirrors ChromaVectorStore.delete_by_ids() — used as the same
    compensating action when a downstream write fails after this store's
    own write already succeeded."""
    if not chunk_ids:
        return
    try:
        async with get_session() as db:
            await db.execute(
                text("DELETE FROM chunk_fulltext WHERE chunk_id = ANY(:chunk_ids)"),
                {"chunk_ids": chunk_ids},
            )
    except Exception as exc:
        logger.warning("fulltext_index.delete_by_ids: failed for %d id(s) (%s)", len(chunk_ids), exc)


async def delete_by_source(source_file: str) -> None:
    """Mirrors ChromaVectorStore.delete_by_source() — called alongside it
    (src/api/admin.py's document-delete route) so a deleted document's
    chunks disappear from the full-text index too, not just Chroma."""
    try:
        async with get_session() as db:
            await db.execute(
                text("DELETE FROM chunk_fulltext WHERE source = :source"),
                {"source": source_file},
            )
    except Exception as exc:
        logger.warning("fulltext_index.delete_by_source: failed for %r (%s)", source_file, exc)


def _scope_where(where: Optional[dict]) -> tuple[str, dict]:
    """
    Same two-dimensional scoping src/retrieval/vector_store.py's
    _build_where() applies to Chroma — project/global OR, case AND — built
    here as a plain SQL WHERE fragment against chunk_fulltext's own
    columns. A falsy `where` applies no scope filter (matches
    _build_where()'s "None applies no filter" default for internal/test
    callers).
    """
    if not where:
        return "TRUE", {}

    clauses = []
    params: dict = {}
    project_id = where.get("project_id")
    if project_id:
        clauses.append("(project_id = :project_id OR is_global = TRUE)")
        params["project_id"] = str(project_id)
    elif where.get("all_cases") is True:
        # Mirrors vector_store._build_where()'s "All Cases" scope: every
        # case's evidence plus global reference material, never a private
        # caseless project upload. A real, nullable Postgres column here
        # (unlike Chroma's sparse metadata), so `case_id IS NOT NULL` is
        # exact -- no need for vector_store's has_case workaround. Getting
        # this branch wrong (i.e. leaving `all_cases` unhandled) would fall
        # through to the `if not clauses: return "TRUE", {}` case below --
        # completely unscoped, leaking every project's chunks into BM25.
        clauses.append("(is_global = TRUE OR case_id IS NOT NULL)")
    elif where.get("is_global") is True:
        clauses.append("is_global = TRUE")
    case_id = where.get("case_id")
    if case_id:
        clauses.append("case_id = :case_id")
        params["case_id"] = case_id
    source = where.get("source")
    if source:
        clauses.append("source = :source")
        params["source"] = source

    if not clauses:
        return "TRUE", {}
    return " AND ".join(clauses), params


async def candidate_pool(query_text: str, where: Optional[dict] = None, limit: int = 2000) -> list[dict]:
    """
    The actual Milestone A2 scale fix: a GIN-index-backed lookup for every
    chunk (within `where`'s scope) that shares AT LEAST ONE token with
    `query_text` — a real inverted-index candidate generation step,
    replacing "every chunk in scope, unfiltered" as retrieve_bm25()'s
    input pool. retrieve_bm25() still does its own exact BM25 scoring over
    whatever this returns; this function only shrinks what it has to
    tokenize and score.

    `limit` bounds a pathological very-common-term query from returning
    the entire scoped corpus anyway — 2000 is comfortably above
    config.TOP_K_RETRIEVAL, so it never starves a real query of true
    positives in this codebase's actual corpus sizes.

    Returns [] on a query with no tokens (nothing to match against) or on
    any Postgres failure — callers already treat an empty/failed candidate
    pool as "fall back to semantic-only results" (see orchestrator.py's
    own try/except around this call).
    """
    tokens = tokenize(query_text)
    if not tokens:
        return []

    scope_sql, scope_params = _scope_where(where)
    # `|` (OR) across query tokens — recall-oriented candidate generation.
    # Precision is retrieve_bm25()'s job once it scores this pool; a
    # chunk sharing just one rare term with a multi-term query is exactly
    # the kind of partial match BM25's own IDF weighting is designed to
    # rank appropriately, not something this step should pre-emptively
    # exclude.
    tsquery = " | ".join(tokens)

    try:
        async with get_session() as db:
            res = await db.execute(
                text(
                    f"""
                    SELECT chunk_id, text, metadata
                    FROM chunk_fulltext
                    WHERE {scope_sql} AND tsv @@ to_tsquery('simple', :tsquery)
                    LIMIT :limit
                    """
                ),
                {**scope_params, "tsquery": tsquery, "limit": limit},
            )
            rows = res.fetchall()
    except Exception as exc:
        logger.warning("fulltext_index.candidate_pool: query failed (%s) — returning no candidates", exc)
        return []

    def _as_dict(raw_metadata) -> dict:
        # asyncpg/SQLAlchemy decodes a JSONB column to a Python dict for
        # typed ORM columns, but a raw text() SELECT (as above) can come
        # back as either a dict or the raw JSON string depending on
        # driver/version — handle both rather than assume one.
        if isinstance(raw_metadata, str):
            return json.loads(raw_metadata) if raw_metadata else {}
        return dict(raw_metadata or {})

    return [
        {"id": row.chunk_id, "text": row.text, "metadata": _as_dict(row.metadata)}
        for row in rows
    ]
