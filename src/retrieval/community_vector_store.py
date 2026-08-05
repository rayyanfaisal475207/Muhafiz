# ============================================================
# Community report vector store — GraphRAG-inspired layer, Section 2.
#
# A second, separate Chroma collection ("muhafiz_community_reports"),
# distinct from the document-chunk collection (COLLECTION_NAME="muhafiz_kb"
# in vector_store.py) — tens of summaries, not thousands of chunks, so this
# is a deliberately lean wrapper rather than a reuse of ChromaVectorStore's
# full machinery (dimension-mismatch guard, metadata allowlist sanitizer,
# singleton pooling) built for the much larger, higher-write-volume
# document collection. Same persist_dir, same embed_text/embed_texts
# functions from src/retrieval/embedder.py, same EMBEDDING_PROVIDER config
# — no new embedding integration surface.
# ============================================================

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import chromadb

from src import config
from src.retrieval.embedder import embed_text, embed_texts

logger = logging.getLogger(__name__)

COMMUNITY_COLLECTION_NAME = "muhafiz_community_reports"

_client: Optional[chromadb.ClientAPI] = None
_collection = None


def _get_collection():
    global _client, _collection
    if _collection is None:
        persist_dir: Path = config.CHROMA_PERSIST_DIR
        persist_dir.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(persist_dir))
        _collection = _client.get_or_create_collection(
            name=COMMUNITY_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def reset_collection() -> None:
    """Test-only: drop the cached collection handle so the next call reopens it."""
    global _client, _collection
    _client = None
    _collection = None


async def upsert_community_reports(reports: list[dict]) -> None:
    """
    reports: list of {community_id, summary_text, case_ids, member_count}.
    Embeds summary_text with RETRIEVAL_DOCUMENT task type (ingestion-side,
    matching vector_store.py's own document/query asymmetry for the "e5"
    provider) and upserts by community_id — re-running summarization for
    the same community_id overwrites its embedding, same as Chroma's
    native upsert semantics elsewhere in this codebase.
    """
    if not reports:
        return
    vectors = await embed_texts(
        [r["summary_text"] for r in reports], task_type="RETRIEVAL_DOCUMENT"
    )
    collection = _get_collection()
    collection.upsert(
        ids=[r["community_id"] for r in reports],
        documents=[r["summary_text"] for r in reports],
        embeddings=vectors,
        metadatas=[
            {
                "community_id": r["community_id"],
                "case_ids": ",".join(r.get("case_ids") or []),
                "member_count": r.get("member_count", 0),
            }
            for r in reports
        ],
    )
    logger.info("Upserted %d community report embeddings.", len(reports))


async def query_similar_communities(query: str, top_k: int = 5) -> list[dict]:
    """
    Top-k community reports by embedding similarity to `query`.
    RETRIEVAL_QUERY task type (query-side, matching embedder.py's
    documented asymmetry). Returns
    [{community_id, summary_text, case_ids, member_count, distance}, ...].
    """
    vector = await embed_text(query, task_type="RETRIEVAL_QUERY")
    collection = _get_collection()
    if collection.count() == 0:
        return []
    result = collection.query(
        query_embeddings=[vector],
        n_results=min(top_k, collection.count()),
        include=["documents", "metadatas", "distances"],
    )
    out = []
    for doc, meta, dist in zip(
        result["documents"][0], result["metadatas"][0], result["distances"][0]
    ):
        case_ids = meta.get("case_ids", "")
        out.append({
            "community_id": meta.get("community_id"),
            "summary_text": doc,
            "case_ids": case_ids.split(",") if case_ids else [],
            "member_count": meta.get("member_count", 0),
            "distance": dist,
        })
    return out
