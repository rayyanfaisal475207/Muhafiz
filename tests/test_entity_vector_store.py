"""
Tests for src/retrieval/entity_vector_store.py (findings.md Module 8, Local
Search) — mirrors community_vector_store.py's own shape and this repo's
`no_network`-guarded pattern: a real (local, on-disk, no-network) Chroma
PersistentClient pointed at a pytest tmp_path, with `embed_text`/`embed_texts`
monkeypatched to a deterministic fake embedder so no model server is ever
called.

Covers:
  (a) upsert + query round-trip, case_id scoping (a match from a different
      case is never returned even when it's a closer semantic match);
  (b) empty collection / no matches for a case -> [] , not an error;
  (c) query_similar_entities(case_id=None) fails closed -> [] with no query
      attempted at all;
  (d) delete_entities prunes stale ids;
  (e) get_all_entity_documents() returns exactly what was upserted, for the
      refresh-diff consumer.
"""
from __future__ import annotations

import pytest

import src.retrieval.entity_vector_store as entity_vector_store_mod
from src.retrieval.entity_vector_store import (
    delete_entities,
    get_all_entity_documents,
    query_similar_entities,
    reset_collection,
    upsert_entities,
)


def _fake_vector(text: str) -> list[float]:
    """Deterministic, cheap stand-in for a real embedding — bucketed by the
    text's own character codes so identical/near-identical strings land
    near each other and distinct strings don't collide. Real semantic
    quality is not what these tests are checking; the store's own
    plumbing (upsert/query/scope/delete) is."""
    h = sum(ord(c) for c in text)
    return [float(h % 97) / 97.0, float(h % 53) / 53.0, float(h % 31) / 31.0]


@pytest.fixture(autouse=True)
def _fake_embedder(monkeypatch):
    async def _embed_text(text, task_type="RETRIEVAL_QUERY"):
        return _fake_vector(text)

    async def _embed_texts(texts, task_type="RETRIEVAL_DOCUMENT"):
        return [_fake_vector(t) for t in texts]

    monkeypatch.setattr(entity_vector_store_mod, "embed_text", _embed_text)
    monkeypatch.setattr(entity_vector_store_mod, "embed_texts", _embed_texts)


@pytest.fixture(autouse=True)
def _isolated_chroma_dir(tmp_path, monkeypatch):
    from src import config

    monkeypatch.setattr(config, "CHROMA_PERSIST_DIR", tmp_path / "chroma_entity_test")
    reset_collection()
    yield
    reset_collection()


def _entity(entity_id, label, case_id, canonical_name, description_text=None):
    return {
        "entity_id": entity_id,
        "label": label,
        "case_id": case_id,
        "canonical_name": canonical_name,
        "description_text": description_text or f"{label} {canonical_name}.",
    }


@pytest.mark.asyncio
async def test_upsert_and_query_round_trip():
    await upsert_entities([
        _entity("e1", "Officer", "CASE-001", "ندیم", "Officer ندیم, belt number GEN-0113, investigating officer for this case."),
        _entity("e2", "Person", "CASE-001", "طارق"),
    ])

    results = await query_similar_entities("Officer ندیم, belt number GEN-0113, investigating officer for this case.", "CASE-001", top_k=3)

    assert results
    assert results[0]["entity_id"] == "e1"
    assert results[0]["label"] == "Officer"
    assert results[0]["case_id"] == "CASE-001"
    assert results[0]["canonical_name"] == "ندیم"


@pytest.mark.asyncio
async def test_case_scoping_excludes_other_cases_even_when_closer_match():
    # e_other is an EXACT text match but scoped to a different case — must
    # never be returned for a CASE-001 query, even though it would win on
    # pure similarity. This is the hard within-case scope entity_vector_
    # store.py's own module docstring commits to.
    await upsert_entities([
        _entity("e_this_case", "Officer", "CASE-001", "ندیم", "Officer ندیم, belt number GEN-0113."),
        _entity("e_other_case", "Officer", "CASE-999", "ندیم", "Officer ندیم, belt number GEN-0113."),
    ])

    results = await query_similar_entities("Officer ندیم, belt number GEN-0113.", "CASE-001", top_k=5)

    assert {r["entity_id"] for r in results} == {"e_this_case"}


@pytest.mark.asyncio
async def test_no_entities_for_case_returns_empty_not_error():
    await upsert_entities([_entity("e1", "Person", "CASE-001", "طارق")])

    results = await query_similar_entities("anything", "CASE-002", top_k=3)

    assert results == []


@pytest.mark.asyncio
async def test_no_case_id_fails_closed_without_querying():
    results = await query_similar_entities("anything", None, top_k=3)
    assert results == []


@pytest.mark.asyncio
async def test_delete_entities_prunes_stale_ids():
    await upsert_entities([
        _entity("e1", "Person", "CASE-001", "طارق"),
        _entity("e2", "Person", "CASE-001", "بلال"),
    ])

    await delete_entities(["e1"])

    documents = await get_all_entity_documents()
    assert "e1" not in documents
    assert "e2" in documents


@pytest.mark.asyncio
async def test_get_all_entity_documents_reflects_upserts():
    await upsert_entities([
        _entity("e1", "Vehicle", "CASE-001", "ICT-LE-309", "Vehicle ICT-LE-309, plate number ICT-LE-309."),
    ])

    documents = await get_all_entity_documents()

    assert documents == {"e1": "Vehicle ICT-LE-309, plate number ICT-LE-309."}


@pytest.mark.asyncio
async def test_reembedding_same_id_overwrites_description():
    await upsert_entities([_entity("e1", "Person", "CASE-001", "طارق", "Person طارق, CNIC old.")])
    await upsert_entities([_entity("e1", "Person", "CASE-001", "طارق", "Person طارق, CNIC new.")])

    documents = await get_all_entity_documents()
    assert documents["e1"] == "Person طارق, CNIC new."
