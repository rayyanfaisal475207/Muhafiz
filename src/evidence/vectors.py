"""Rebuildable Chroma projection using only the configured local E5 server."""
from __future__ import annotations

import asyncio
import math
import os
from pathlib import Path
from uuid import UUID

import httpx

COLLECTION = "evidence_spans_v1"


def client():
    import chromadb
    directory = os.getenv("EVIDENCE_VECTOR_DIR") or str(Path(__file__).resolve().parents[2]/"data"/"evidence_chroma")
    return chromadb.PersistentClient(path=directory)


async def embed(text: str, *, query: bool):
    from src import config
    if not config.EMBEDDINGS_URL:
        raise RuntimeError("Local embeddings endpoint is unavailable")
    async with httpx.AsyncClient(timeout=12) as session:
        response = await session.post(config.EMBEDDINGS_URL,json={"text":text,"is_query":query})
        response.raise_for_status()
        vector = response.json()["embedding"]
    if len(vector) != config.EXPECTED_EMBEDDING_DIM or any(not isinstance(v,(int,float)) or not math.isfinite(v) for v in vector):
        raise ValueError("Local embedding dimensions or values are invalid")
    return vector


async def index_spans(conn,document_id):
    rows = await conn.fetch("SELECT span_id,text FROM evidence.spans WHERE document_id=$1 ORDER BY char_start",document_id)
    collection = await asyncio.to_thread(lambda:client().get_or_create_collection(COLLECTION,embedding_function=None))
    for row in rows:
        vector = await embed(row["text"],query=False)
        await asyncio.to_thread(collection.upsert,ids=[str(row["span_id"])],embeddings=[vector],
                               metadatas=[{"document_id":str(document_id)}])
    return len(rows)


async def search(conn,query: str,limit: int, *, document_ids=None):
    if os.getenv("EVIDENCE_VECTOR_ENABLED","false").lower() != "true":
        return [],"Semantic search is not enabled; lexical retrieval was used."
    docs = await conn.fetch("""SELECT document_id FROM evidence.documents d
       WHERE NOT EXISTS(SELECT 1 FROM evidence.documents n WHERE n.supersedes=d.document_id)""")
    if document_ids is not None:
        allowed=set(document_ids)
        docs=[r for r in docs if r["document_id"] in allowed]
    if not docs:
        return [],None
    try:
        vector = await embed(query,query=True)
        collection = await asyncio.to_thread(lambda:client().get_collection(COLLECTION,embedding_function=None))
        found = await asyncio.to_thread(collection.query,query_embeddings=[vector],n_results=limit,
                     where={"document_id":{"$in":[str(r["document_id"]) for r in docs]}},include=["distances"])
        return [UUID(value) for value in found["ids"][0]],None
    except Exception as exc:
        return [],f"Semantic search unavailable ({type(exc).__name__}); lexical retrieval was used."
