# ============================================================
# Entity description vector store — GraphRAG-inspired Local Search layer,
# findings.md Module 8.
#
# A third, separate Chroma collection ("muhafiz_entity_descriptions"),
# distinct from the document-chunk collection (COLLECTION_NAME="muhafiz_kb"
# in vector_store.py) and the community-report collection
# ("muhafiz_community_reports" in community_vector_store.py) — one short
# description per graph entity (Person/Vehicle/PhoneNumber/Organization/
# Officer), not thousands of document chunks. Mirrors
# community_vector_store.py's shape closely and deliberately: same
# persist_dir, same embed_text/embed_texts functions from
# src/retrieval/embedder.py, same EMBEDDING_PROVIDER config, same lean
# get_or_create_collection wrapper rather than ChromaVectorStore's full
# machinery (dimension-mismatch guard, metadata allowlist sanitizer,
# singleton pooling) built for the much larger document collection.
#
# WHY THIS EXISTS: no entity is embedded anywhere else in this codebase.
# Entity seeding (_find_seed_nodes(), src/retrieval/graph_retriever.py) is
# 100% literal `CONTAINS` substring matching against
# canonical_name/cnic/phone/plate/belt_no — it cannot resolve a descriptive
# or role-based reference with no literal identifier in the query ("the
# investigating officer in this case"). This collection is the semantic
# "access point" layer MS GraphRAG's Local Search calls for: embed each
# entity's own short description, then match the QUERY's embedding against
# it to find candidate seed entities no literal match could ever find.
#
# HARD WITHIN-CASE SCOPE. Every entity node belongs to exactly one case via
# BELONGS_TO_CASE (entity resolution mints a new node per case-mention,
# never merges across cases except through a confirmed SAME_AS edge — see
# graph_retriever.py's own module docstring) — so a single case_id per
# embedded row is correct, not a simplification. `query_similar_entities()`
# ALWAYS filters by case_id server-side (Chroma `where`); there is no
# unscoped query path. This is a within-case tool (same discipline as GRAPH
# — SUBAGENT_INTERFACES.md §2.2), not a role-gated cross-case one: any
# caller who can see the case can search its entities.
# ============================================================

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import chromadb

from src import config
from src.retrieval.embedder import embed_text, embed_texts

logger = logging.getLogger(__name__)

ENTITY_COLLECTION_NAME = "muhafiz_entity_descriptions"

_client: Optional[chromadb.ClientAPI] = None
_collection = None


def _get_collection():
    global _client, _collection
    if _collection is None:
        persist_dir: Path = config.CHROMA_PERSIST_DIR
        persist_dir.mkdir(parents=True, exist_ok=True)
        _client = chromadb.PersistentClient(path=str(persist_dir))
        _collection = _client.get_or_create_collection(
            name=ENTITY_COLLECTION_NAME,
            metadata={"hnsw:space": "cosine"},
        )
    return _collection


def reset_collection() -> None:
    """Test-only: drop the cached collection handle so the next call reopens it."""
    global _client, _collection
    _client = None
    _collection = None


async def upsert_entities(entities: list[dict]) -> None:
    """
    entities: list of {entity_id, label, case_id, canonical_name, description_text}.
    Embeds description_text with RETRIEVAL_DOCUMENT task type (ingestion-side,
    matching vector_store.py's/community_vector_store.py's own document/query
    asymmetry for the "e5" provider) and upserts by entity_id — re-embedding
    the same entity_id overwrites its prior embedding, same as Chroma's
    native upsert semantics elsewhere in this codebase.
    """
    if not entities:
        return
    vectors = await embed_texts(
        [e["description_text"] for e in entities], task_type="RETRIEVAL_DOCUMENT"
    )
    collection = _get_collection()
    collection.upsert(
        ids=[e["entity_id"] for e in entities],
        documents=[e["description_text"] for e in entities],
        embeddings=vectors,
        metadatas=[
            {
                "entity_id": e["entity_id"],
                "label": e["label"],
                "case_id": e["case_id"],
                "canonical_name": e.get("canonical_name") or "",
            }
            for e in entities
        ],
    )
    logger.info("Upserted %d entity description embedding(s).", len(entities))


async def delete_entities(entity_ids: list[str]) -> None:
    """
    Remove embeddings for entity_ids no longer present in the graph (deleted/
    cleaned-up nodes — see cleanup_orphaned_person_nodes.py — or entities
    whose description changed and were re-upserted under the same id do NOT
    need this; only genuinely-gone ids do). Mirrors the same "prune stale
    entries" discipline clear_all_reports()'s own docstring documents as a
    REAL confirmed bug class for the community-report collection (a Chroma
    collection only ever upserts by default — nothing prunes on its own).
    """
    if not entity_ids:
        return
    collection = _get_collection()
    collection.delete(ids=entity_ids)
    logger.info("Deleted %d stale entity description embedding(s).", len(entity_ids))


async def query_similar_entities(query: str, case_id: str, top_k: int = 3) -> list[dict]:
    """
    Top-k entities by embedding similarity to `query`, scoped to `case_id`
    (server-side Chroma `where` filter — never an unscoped query). RETRIEVAL_QUERY
    task type (query-side, matching embedder.py's documented asymmetry).

    Returns [{entity_id, label, case_id, canonical_name, distance}, ...].
    Empty list (not an error) when the collection has nothing for this case —
    a legitimate "no semantic access point found" outcome, same shape
    query_similar_communities() already establishes for the sibling
    community-report collection.
    """
    if not case_id:
        # Fail closed — no case in scope means nothing to search. Mirrors
        # retrieve_graph()'s own "within-case query with no case_id ->
        # empty" rule; this function is never meant to run unscoped.
        return []

    collection = _get_collection()
    matching = collection.get(where={"case_id": case_id}, include=[])
    if not matching["ids"]:
        return []

    vector = await embed_text(query, task_type="RETRIEVAL_QUERY")
    result = collection.query(
        query_embeddings=[vector],
        n_results=min(top_k, len(matching["ids"])),
        where={"case_id": case_id},
        include=["documents", "metadatas", "distances"],
    )
    out = []
    ids = result.get("ids", [[]])[0]
    metadatas = result["metadatas"][0]
    distances = result["distances"][0]
    for entity_id, meta, dist in zip(ids, metadatas, distances):
        out.append({
            "entity_id": meta.get("entity_id") or entity_id,
            "label": meta.get("label"),
            "case_id": meta.get("case_id"),
            "canonical_name": meta.get("canonical_name"),
            "distance": dist,
        })
    return out


async def get_all_entity_documents() -> dict[str, str]:
    """
    Every currently-embedded entity_id -> its stored description_text, for
    the refresh diff in src/graph/entity_embedding_refresh.py. A plain
    collection.get() over the whole (small — thousands, not millions of
    rows) collection; no separate Postgres hash table needed to detect
    "changed since last embed run."
    """
    collection = _get_collection()
    data = collection.get(include=["documents"])
    ids = data.get("ids", [])
    documents = data.get("documents", [])
    return dict(zip(ids, documents))
