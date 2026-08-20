"""
ChromaDB Vector Store Wrapper

Replaces the PostgreSQL + pgvector implementation with a local ChromaDB
collection for semantic search. Postgres (local/self-hosted, direct SQL) is unchanged for every
relational table (`documents`, `users`, `projects`, ...) — only chunk
text + embeddings + retrieval metadata, formerly in `document_chunks`,
now live in Chroma. See Muhafiz_Upgrade_Plan_v1.md §Phase 1.

Keyword search (Postgres `ts_rank`) does not have a Chroma equivalent, so
it is dropped from this layer. That is not a regression: the orchestrator
already runs an independent BM25 + RRF pass in Python
(`src/retrieval/bm25_retriever.py`, `src/retrieval/reranker.py`) on top of
whatever this module returns, and that step is untouched by this swap —
it now supplies the entire keyword-relevance half of hybrid search
instead of duplicating what Postgres used to do in SQL.
"""
import asyncio
import logging
import threading
from pathlib import Path
from typing import Any, Optional

import chromadb

from src import config
from src.data_gateway import get_gateway

logger = logging.getLogger(__name__)

COLLECTION_NAME = "muhafiz_kb"

# Metadata keys Chroma is allowed to store (str/int/float/bool only — None
# values are rejected, so callers must sanitize before upsert).
_METADATA_KEYS = (
    "doc_id", "project_id", "is_global", "case_id", "doc_type", "source",
    "chunk_index", "source_type", "page", "section", "category",
    # Phase 2.6 — Roman-Urdu tag, set per chunk in ingestion/service.py.
    # This allowlist is a silent filter: a key that is not listed here is
    # dropped on upsert with no error, so anything added to chunk
    # metadata must be added here too or it simply never lands.
    "is_roman_urdu",
    # Muhafiz Data API migration (docs/decisions/0001-muhafiz-api-migration.md,
    # M3) — set by src/ingestion/muhafiz_records.py for API-sourced chunks,
    # absent/None (dropped by _sanitize_metadata below) for file-sourced
    # ones. `content_provenance` is deliberately NOT named `source` — that
    # key already means "which file/record this chunk came from"; this one
    # carries the record's own real/synthetic origin tag instead.
    "record_type", "source_system", "external_id", "station_code",
    "district", "content_provenance",
    # M8 — a real ISO date/timestamp (FIR incident date, CMS/PKM submission
    # date, roznamcha entry date) so src/retrieval/reranker.py's recency
    # boost can read an actual field instead of regexing a filename that,
    # for API-sourced chunks, carries no year at all.
    "record_date",
    # M11 — the human-readable FIR display code ("891/24"), FIR-only. Lets
    # orchestrator.py's FIR-based query auto-scope match a real display
    # code the way it already matches synthetic-corpus filenames.
    "fir_display_code",
)


def _sanitize_metadata(meta: dict) -> dict:
    """Drop None values and keep only fields Chroma's schema recognizes."""
    return {
        k: v for k, v in meta.items()
        if k in _METADATA_KEYS and v is not None
    }


class EmbeddingDimensionMismatch(Exception):
    """
    Raised when a chunk's embedding vector doesn't match
    config.EXPECTED_EMBEDDING_DIM for the currently-configured
    EMBEDDING_PROVIDER.

    Chroma itself only raises its own dimension-mismatch error once a
    collection is non-empty — a freshly-created or manually-cleared
    collection silently adopts whatever dimension the first-written vector
    happens to have, with no warning (Phase 4, Module 4.2). This check fires
    before every write, regardless of collection state.
    """


class ChromaVectorStore:
    """
    Local-persistent Chroma collection wrapper.

    Interface (`upsert` / `search`) matches Muhafiz_Upgrade_Plan_v1.md
    §Phase 1's named contract. Module-level functions below adapt this to
    the exact call signatures `orchestrator.py` and `ingestion/service.py`
    already use, so neither needs to change.
    """

    _instance: Optional["ChromaVectorStore"] = None
    _lock = threading.Lock()

    def __init__(self, persist_dir: Optional[Path] = None):
        persist_dir = persist_dir or config.CHROMA_PERSIST_DIR
        persist_dir.mkdir(parents=True, exist_ok=True)
        self._client = chromadb.PersistentClient(path=str(persist_dir))
        # Cosine space matches the pgvector `<=>` operator this replaces.
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    @classmethod
    def get_instance(cls) -> "ChromaVectorStore":
        """Process-wide singleton — avoids re-opening the sqlite file per call."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance

    @classmethod
    def reset_instance(cls) -> None:
        """Test-only: force the next get_instance() to open a fresh client."""
        cls._instance = None

    def drop_and_recreate(self) -> None:
        """
        Delete this collection entirely and recreate it empty.

        Required whenever the embedding model/dimension changes (e.g. the
        Phase 0.4 switch to multilingual-e5-large-instruct, 1024-dim) —
        Chroma stores one fixed dimension per collection and hard-errors on
        a query whose embedding size doesn't match what's already stored.
        Upserting new-dimension vectors into an old-dimension collection
        would either error identically on write or, worse, silently mix
        dimensions if the collection were empty-but-uninitialized — drop
        and recreate is the only safe path, not an append.
        """
        try:
            self._client.delete_collection(name=COLLECTION_NAME)
        except Exception as exc:
            logger.warning("delete_collection(%s) failed (may not exist yet): %s", COLLECTION_NAME, exc)
        self._collection = self._client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )

    def upsert(self, chunks: list[dict]) -> None:
        """
        chunks: list of {id, text, embedding, metadata} dicts.
        Chroma's native upsert overwrites existing ids and inserts new
        ones in a single call — the duplicate-prevention Postgres got via
        `ON CONFLICT ... DO UPDATE`.
        """
        if not chunks:
            return
        first_dim = len(chunks[0]["embedding"])
        if first_dim != config.EXPECTED_EMBEDDING_DIM:
            raise EmbeddingDimensionMismatch(
                f"Embedding dimension mismatch: got {first_dim}, expected "
                f"{config.EXPECTED_EMBEDDING_DIM} for EMBEDDING_PROVIDER="
                f"{config.EMBEDDING_PROVIDER!r}. Refusing to write — this would "
                "otherwise succeed silently against an empty/freshly-created "
                "Chroma collection."
            )
        self._collection.upsert(
            ids=[c["id"] for c in chunks],
            documents=[c["text"] for c in chunks],
            embeddings=[c["embedding"] for c in chunks],
            metadatas=[_sanitize_metadata(c.get("metadata") or {}) for c in chunks],
        )

    def delete_by_ids(self, ids: list[str]) -> None:
        """Remove chunks by their exact ids. Used as a compensating action
        when a downstream write (Postgres) fails after this store's write
        already succeeded (Module 4.2)."""
        if ids:
            self._collection.delete(ids=ids)

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 10,
        metadata_filter: Optional[dict] = None,
    ) -> list[dict]:
        """
        Pure vector similarity search (no keyword component — see module
        docstring). Returns the same {id, text, metadata, rrf_score} shape
        the Postgres backend returned, so bm25_retriever/reranker/
        pipeline_logger need no changes.
        """
        where = _build_where(metadata_filter)
        result = self._collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        ids = result.get("ids") or [[]]
        docs = result.get("documents") or [[]]
        metas = result.get("metadatas") or [[]]
        dists = result.get("distances") or [[]]

        out: list[dict] = []
        for chunk_id, text, meta, dist in zip(ids[0], docs[0], metas[0], dists[0]):
            meta = dict(meta or {})
            # cosine space: distance = 1 - cosine_similarity. This is a raw
            # semantic score, not a true RRF fusion score (Postgres used to
            # blend in ts_rank here) — the Python RRF step downstream
            # overwrites "rrf_score" with the real fused value before it
            # reaches the evaluator/response generator.
            out.append({
                "id": chunk_id,
                "text": text,
                "metadata": meta,
                "rrf_score": max(0.0, 1.0 - dist),
            })
        return out

    def get_all_metadata(self) -> list[dict]:
        """Used by pipeline tests/evaluation to fetch all documents."""
        result = self._collection.get(include=["metadatas"])
        return result.get("metadatas") or []

    def get_all(self, metadata_filter: Optional[dict] = None) -> list[dict]:
        """
        Fetch every chunk (id + text + metadata) matching the given scope
        filter — the full candidate pool for BM25, not just a vector-search
        top-k (RETRIEVAL_DIVERSITY_FIX_PROMPT.md, Fix 1).

        Runs the SAME `_build_where` translation `search()` uses, so this
        can never return chunks outside the caller's project/global/case
        access-control scope — it only ever widens the pool BM25 sees
        within whatever scope the caller already passed to vector search,
        never beyond it.
        """
        where = _build_where(metadata_filter)
        result = self._collection.get(where=where, include=["documents", "metadatas"])
        out: list[dict] = []
        for chunk_id, text, meta in zip(
            result.get("ids") or [], result.get("documents") or [], result.get("metadatas") or []
        ):
            out.append({"id": chunk_id, "text": text, "metadata": dict(meta or {})})
        return out

    def get_by_ids(self, ids: list[str]) -> list[dict]:
        """
        Fetch specific chunks by id — the provenance-lookup primitive
        `graph_retriever.py` uses. A graph edge's `source_chunk_id` (Phase 4,
        `src/ingestion/service.py`) is written as literally the same id this
        chunk is stored under here, so this is a direct id lookup, not a
        join. Returns the same {id, text, metadata} shape `search()` does
        (no `rrf_score` — the caller assigns its own score), skipping any id
        Chroma doesn't have (a graph edge whose document was never ingested,
        or since deleted) rather than erroring.
        """
        if not ids:
            return []
        result = self._collection.get(ids=ids, include=["documents", "metadatas"])
        out: list[dict] = []
        for chunk_id, text, meta in zip(
            result.get("ids") or [], result.get("documents") or [], result.get("metadatas") or []
        ):
            out.append({"id": chunk_id, "text": text, "metadata": dict(meta or {})})
        return out

    def delete_by_source(self, source_file: str) -> int:
        """Remove every chunk tagged with this source filename. Returns count deleted."""
        existing = self._collection.get(where={"source": source_file}, include=[])
        ids = existing.get("ids") or []
        if ids:
            self._collection.delete(ids=ids)
        return len(ids)

    def count(self) -> int:
        return self._collection.count()


def _build_where(where: Optional[dict]) -> Optional[dict]:
    """
    Translate the pipeline's `where` dict into a Chroma `where` filter.

    Two independent scoping dimensions compose with AND:

    1. Project/global isolation (unchanged) — enforces that a project's
       documents must never leak into another project's (or a
       non-project) chat:
       - `{"project_id": <id>}` — a project-scoped search matches chunks
         tagged with that project_id OR chunks tagged is_global. Mirrors
         the old pgvector `d.project_id = :pid OR d.is_global = true`
         filter.
       - `{"is_global": True}` — a non-project chat matches ONLY global
         (shared knowledge-base) chunks. This is what the orchestrator
         passes when there is no project. It is deliberately NOT "no
         filter": an unfiltered search returns every project's scoped
         documents to any user, which is the leak this branch exists to
         prevent.

    2. Case scoping (Phase 1, additive) — `{"case_id": <id>}` restricts
       results to that case's evidence only. This is deliberately an
       independent AND'd condition, not a replacement for #1: `project_id`
       /`is_global` is the pre-existing multi-tenant "whose knowledge base
       is this" boundary (a personal/workspace grouping, unrelated to
       police case management), while `case_id` is the investigative
       "which case is this evidence for" boundary. A case-scoped query
       still must not cross the project/global boundary, and a
       project-scoped query with no case given still searches that
       project's whole corpus (including documents never assigned to any
       case, e.g. the pre-Phase-1 96-document corpus) — the two filters
       are orthogonal, so neither can subsume the other.

    A falsy `where` (None / {}) applies no filter and searches everything.
    This remains the low-level primitive default for internal/test callers
    that legitimately want the whole collection; the isolation guarantee is
    enforced by the orchestrator always passing an explicit project/global
    filter, never None, for a user-facing retrieval.
    """
    if not where:
        return None

    conditions: list[dict] = []
    project_id = where.get("project_id")
    if project_id:
        conditions.append({
            "$or": [
                {"project_id": {"$eq": project_id}},
                {"is_global": {"$eq": True}},
            ]
        })
    elif where.get("is_global") is True:
        # Non-project chat: shared knowledge base only, no project's docs.
        conditions.append({"is_global": {"$eq": True}})
    case_id = where.get("case_id")
    if case_id:
        conditions.append({"case_id": {"$eq": case_id}})
    source = where.get("source")
    if source:
        conditions.append({"source": {"$eq": source}})

    if not conditions:
        return None
    if len(conditions) == 1:
        return conditions[0]
    return {"$and": conditions}


def _get_store() -> ChromaVectorStore:
    return ChromaVectorStore.get_instance()


def reset_collection() -> None:
    """Drop and recreate the Chroma collection empty. See drop_and_recreate()."""
    _get_store().drop_and_recreate()


async def upsert_documents(
    ids: list[str],
    texts: list[str],
    embeddings: list[list[float]],
    metadatas: list[dict],
) -> None:
    """
    Store or update document chunks.

    The parent `documents` row stays in Postgres via DataGateway (relational
    metadata, unchanged per the upgrade plan). Chunk text + embedding +
    retrieval metadata goes to Chroma instead of the old `document_chunks`
    table.
    """
    if not ids:
        logger.warning("upsert_documents called with empty list.")
        return

    doc_params = []
    chroma_chunks = []

    for i in range(len(ids)):
        meta = metadatas[i] or {}
        doc_id = meta.get("doc_id", f"unknown_{ids[i]}")
        chunk_index = meta.get("chunk_index", 0)
        source_file = meta.get("source", "unknown")
        project_id = meta.get("project_id")
        case_id = meta.get("case_id")
        is_global = bool(meta.get("is_global", False))
        emb_list = embeddings[i].tolist() if hasattr(embeddings[i], "tolist") else embeddings[i]

        doc_params.append({
            "doc_id": str(doc_id),
            "filename": str(source_file),
            "doc_type": meta.get("doc_type") or "document",
            "is_global": is_global,
            "project_id": project_id,
            "case_id": case_id,
        })
        chroma_chunks.append({
            "id": ids[i],
            "text": texts[i],
            "embedding": emb_list,
            "metadata": {
                "doc_id": str(doc_id),
                "chunk_index": int(chunk_index),
                "source": str(source_file),
                "project_id": project_id,
                "case_id": case_id,
                "is_global": is_global,
                "doc_type": meta.get("doc_type"),
                "source_type": meta.get("source_type"),
                "page": meta.get("page") or meta.get("page_number"),
                "section": meta.get("section"),
                "category": meta.get("category"),
                # Phase 2.6. NOTE: this dict is a second, independent
                # filter on top of _METADATA_KEYS — a key added to the
                # allowlist but not listed here is still dropped, just as
                # silently. Both have to be updated together.
                "is_roman_urdu": meta.get("is_roman_urdu"),
                # M3 (Muhafiz Data API migration) — same "both places"
                # requirement as the comment above; None on any
                # file-sourced chunk, sanitized away by _sanitize_metadata.
                "record_type": meta.get("record_type"),
                "source_system": meta.get("source_system"),
                "external_id": meta.get("external_id"),
                "station_code": meta.get("station_code"),
                "district": meta.get("district"),
                "content_provenance": meta.get("content_provenance"),
                # M8 — see _METADATA_KEYS' comment above.
                "record_date": meta.get("record_date"),
                "fir_display_code": meta.get("fir_display_code"),
            },
        })

    unique_docs = list({d["doc_id"]: d for d in doc_params}.values())

    # Chroma first, Postgres second (Module 4.2). Eliminates the orphaned-
    # documents-row class: if the Chroma write fails (dimension mismatch,
    # disk error, process kill), nothing is committed to Postgres either, so
    # a `documents` row can no longer exist with zero retrievable chunks
    # behind it. If Chroma succeeds but the Postgres write then fails, the
    # just-written Chroma chunks are deleted (compensating action) before
    # re-raising, so the operation is effectively atomic from the caller's
    # perspective in either failure direction.
    store = _get_store()
    await asyncio.to_thread(store.upsert, chroma_chunks)

    gateway = await get_gateway()
    try:
        await gateway.insert_documents(unique_docs)
    except Exception:
        chroma_ids = [c["id"] for c in chroma_chunks]
        logger.error(
            "Postgres insert_documents failed after Chroma write succeeded — "
            "rolling back %d Chroma chunk(s) to avoid a Chroma-only orphan.",
            len(chroma_ids),
        )
        await asyncio.to_thread(store.delete_by_ids, chroma_ids)
        raise

    logger.info("Upserted %d chunks into Chroma", len(ids))


async def query_similar(
    query_text: str,
    query_embedding: list[float],
    top_k: int = 10,
    where: Optional[dict] = None,
    target_date: Optional[int] = None,
) -> list[dict]:
    """
    Find the top-k most similar document chunks via Chroma vector search.

    `query_text` and `target_date` are accepted for call-site compatibility
    but unused here — same as the pgvector-era `target_date`, and now
    `query_text` too, since Chroma has no native keyword-search component
    (see module docstring).
    """
    emb_list = query_embedding.tolist() if hasattr(query_embedding, "tolist") else query_embedding
    store = _get_store()
    return await asyncio.to_thread(store.search, emb_list, top_k, where)


def get_all_documents_metadata() -> list[dict]:
    """Used by pipeline tests/evaluation to fetch all documents."""
    return _get_store().get_all_metadata()


async def get_all_chunks(where: Optional[dict] = None) -> list[dict]:
    """
    Fetch the full scoped candidate pool (id/text/metadata) that BM25 should
    search over, instead of being limited to whatever vector search's top-k
    already returned (RETRIEVAL_DIVERSITY_FIX_PROMPT.md, Fix 1: "BM25 never
    sees the whole corpus").

    `where` uses the exact same shape the orchestrator already builds for
    `query_similar` (project_id / case_id / is_global) and is translated by
    the same `_build_where` used for vector search, so BM25's pool is scoped
    identically — it can never leak chunks outside the access-control
    boundary the vector query already enforces.
    """
    return await asyncio.to_thread(_get_store().get_all, where)


def cap_case_diversity(
    chunks: list[dict],
    per_case_cap: int,
    total_cap: int,
    score_key: str = "rrf_score",
) -> list[dict]:
    """
    Cross-case diversity cap for vector-search results (RETRIEVAL_DIVERSITY_
    FIX_PROMPT.md, Fix 2).

    Pure nearest-neighbor top-k has no notion of "case" — whichever single
    case's chunks happen to sit closest in embedding space for a given
    query's exact phrasing can fill the entire candidate window, starving
    out other cases that also have relevant content (this is the mechanism
    behind the Urdu-vs-English divergence: cross-script embedding drift
    changes which case's chunks are nearest for the same underlying
    question). This only matters for queries that aren't already scoped to
    one case — a `case_id`-filtered query has nothing to diversify across
    and must not call this function at all (see orchestrator.py's RAG
    route).

    Algorithm: bucket the (already vector-search-ranked) candidates by
    `metadata.case_id`, take each case's top `per_case_cap` chunks by
    `score_key`, then re-sort the union by `score_key` and trim to
    `total_cap`. This bounds how much of the final pool any single case can
    occupy without discarding a genuinely stronger match from a case that
    only contributed one or two chunks — the highest-scoring chunks overall
    still win the final trim, diversity capping only prevents one case's
    chunks from crowding out every slot before that trim happens.

    Chunks with no case_id (e.g. pre-case-model corpus, is_global content)
    are bucketed under the key `None`, same as any other case — they are
    not exempted from the per-case cap.
    """
    if total_cap <= 0 or not chunks:
        return []

    buckets: dict[Any, list[dict]] = {}
    order: list[Any] = []
    for chunk in sorted(chunks, key=lambda c: c.get(score_key, 0.0), reverse=True):
        cid = (chunk.get("metadata") or {}).get("case_id")
        if cid not in buckets:
            buckets[cid] = []
            order.append(cid)
        buckets[cid].append(chunk)

    capped: list[dict] = []
    for cid in order:
        capped.extend(buckets[cid][:per_case_cap])

    capped.sort(key=lambda c: c.get(score_key, 0.0), reverse=True)
    return capped[:total_cap]


async def get_chunks_by_ids(ids: list[str]) -> list[dict]:
    """Fetch chunks by id — see ChromaVectorStore.get_by_ids for why this is a direct lookup."""
    if not ids:
        return []
    return await asyncio.to_thread(_get_store().get_by_ids, ids)
