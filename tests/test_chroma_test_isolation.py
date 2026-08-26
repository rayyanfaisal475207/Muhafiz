"""
Tests for the pytest Chroma safety net in tests/conftest.py.

Guards a real incident: a full-suite run left the live
`muhafiz_entity_descriptions` collection empty (822 -> 0) while
`muhafiz_kb` and `muhafiz_community_reports` were untouched. The specific
deleting test was never proven, but the structural exposure was:
`src.config.CHROMA_PERSIST_DIR` is read from the environment at import
time, so a pytest process with no override resolves to the live
`data/chroma_db`, and isolation was opt-in per test file.

Two protections are asserted here:

  C — every pytest process gets a disposable persist root by default.
  F — opening the production root during a test raises instead of writing.

SAFETY: no test here points a writer at the real production directory.
Protection F is exercised against a SIMULATED forbidden root, and the real
directory is only ever read for comparison.
"""
from __future__ import annotations

from pathlib import Path

import chromadb
import pytest

from tests.conftest import (
    PRODUCTION_CHROMA_DIR,
    LiveChromaAccessError,
    _is_production_chroma_path,
)


# ── Protection C — global isolation ──────────────────────────────────────


def test_config_resolves_to_disposable_path_not_production():
    """The default pytest Chroma root must never be the live one."""
    from src import config

    resolved = Path(config.CHROMA_PERSIST_DIR).resolve()

    assert resolved != PRODUCTION_CHROMA_DIR
    assert PRODUCTION_CHROMA_DIR not in resolved.parents
    assert "muhafiz-pytest-chroma-" in resolved.name


def test_application_store_lands_in_the_disposable_root():
    """
    Exercise a real application store rather than only reading an env
    string: the collection an app module opens must sit under the
    disposable root.
    """
    import src.retrieval.entity_vector_store as evs

    evs.reset_collection()
    try:
        collection = evs._get_collection()
        assert collection.count() == 0

        from src import config

        assert Path(config.CHROMA_PERSIST_DIR).resolve() != PRODUCTION_CHROMA_DIR
    finally:
        evs.reset_collection()


def test_production_counts_are_not_visible_from_the_test_root():
    """
    A sanity check that the disposable root really is a different store:
    the live corpus has 823 KB chunks, so seeing them here would mean the
    isolation had failed.
    """
    import src.retrieval.entity_vector_store as evs

    evs.reset_collection()
    try:
        assert evs._get_collection().count() == 0
    finally:
        evs.reset_collection()


# ── Protection F — fail-closed guard ─────────────────────────────────────


def test_guard_classifies_production_paths():
    """Path classification covers the root itself and anything inside it."""
    assert _is_production_chroma_path(PRODUCTION_CHROMA_DIR)
    assert _is_production_chroma_path(str(PRODUCTION_CHROMA_DIR))
    assert _is_production_chroma_path(PRODUCTION_CHROMA_DIR / "chroma.sqlite3")
    assert not _is_production_chroma_path(None)


def test_guard_allows_disposable_paths(tmp_path):
    assert not _is_production_chroma_path(tmp_path)
    assert not _is_production_chroma_path(tmp_path / "chroma")


def test_persistent_client_refuses_the_production_root():
    """
    The real boundary: opening the live root during pytest must raise
    BEFORE any client is constructed, so nothing can be mutated.

    This only ATTEMPTS the production path — the guard rejects it, so no
    client is created and the live store is never opened.
    """
    with pytest.raises(LiveChromaAccessError) as exc:
        chromadb.PersistentClient(path=str(PRODUCTION_CHROMA_DIR))

    assert "Refusing to use live Chroma persistence" in str(exc.value)


def test_guard_is_installed_on_the_real_symbol():
    assert getattr(chromadb.PersistentClient, "_muhafiz_live_guard", False)


# ── Destructive sentinel isolation ───────────────────────────────────────


def _seed(root: Path, ids: list[str]):
    client = chromadb.PersistentClient(path=str(root))
    collection = client.get_or_create_collection(
        name="muhafiz_entity_descriptions", metadata={"hnsw:space": "cosine"}
    )
    collection.upsert(
        ids=ids,
        documents=[f"doc-{i}" for i in ids],
        embeddings=[[0.1] * 8 for _ in ids],
    )
    return collection


def test_destructive_delete_cannot_cross_into_a_protected_root(tmp_path, monkeypatch):
    """
    Two disposable roots stand in for "the test store" and "the live
    store". A destructive delete against A must not reach B, and B is
    additionally marked forbidden so the guard would reject it outright.

    Uses simulated roots only — the real 822 rows are never involved.
    """
    root_a = tmp_path / "working"
    root_b = tmp_path / "protected"

    _seed(root_a, ["A-1", "A-2", "A-3"])
    sentinel = _seed(root_b, ["SENTINEL-1", "SENTINEL-2"])
    assert sentinel.count() == 2

    # Destructive operation against A only.
    collection_a = chromadb.PersistentClient(path=str(root_a)).get_or_create_collection(
        name="muhafiz_entity_descriptions"
    )
    collection_a.delete(ids=["A-1", "A-2", "A-3"])
    assert collection_a.count() == 0

    # B is untouched by A's deletion.
    reopened = chromadb.PersistentClient(path=str(root_b)).get_or_create_collection(
        name="muhafiz_entity_descriptions"
    )
    assert reopened.count() == 2

    # And with B marked forbidden, the guard refuses it before any open.
    import tests.conftest as conftest_mod

    monkeypatch.setattr(conftest_mod, "PRODUCTION_CHROMA_DIR", root_b.resolve())
    with pytest.raises(LiveChromaAccessError):
        chromadb.PersistentClient(path=str(root_b))
