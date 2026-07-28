"""
Module 4.2 — ingestion transactional consistency (src/retrieval/vector_store.py:
upsert_documents, ChromaVectorStore.upsert/delete_by_ids).

Guards two related fixes:

1. Reordering: Chroma is written BEFORE Postgres. A Chroma-write failure
   (e.g. a dimension mismatch) must leave no Postgres `documents` row behind
   — the orphaned-row class this closes.
2. Compensating delete: if Chroma succeeds but the subsequent Postgres
   write fails, the just-written Chroma chunks are deleted before
   re-raising, so a Postgres failure doesn't leave a Chroma-only orphan
   either.
3. EmbeddingDimensionMismatch: a wrong-dimension embedding is rejected
   before any write, even against an empty/freshly-created collection
   (previously the only guard was Chroma's own error, which doesn't fire
   until the collection already has vectors of some dimension in it).
"""
import pytest

from src import config
from src.retrieval import vector_store
from src.retrieval.vector_store import ChromaVectorStore, EmbeddingDimensionMismatch


@pytest.fixture(autouse=True)
def _small_expected_dim(monkeypatch):
    monkeypatch.setattr(config, "EXPECTED_EMBEDDING_DIM", 8)


@pytest.fixture
def store(tmp_path):
    ChromaVectorStore.reset_instance()
    s = ChromaVectorStore(persist_dir=tmp_path / "chroma")
    yield s
    ChromaVectorStore.reset_instance()


def _args(chunk_id="c1", dim=8):
    return dict(
        ids=[chunk_id],
        texts=["some chunk text"],
        embeddings=[[0.1] * dim],
        metadatas=[{"doc_id": f"doc_{chunk_id}", "source": "a.pdf", "chunk_index": 0, "is_global": True}],
    )


class _FakeGateway:
    def __init__(self, insert_documents=None):
        self.insert_calls = []
        self._insert_documents = insert_documents

    async def insert_documents(self, documents):
        self.insert_calls.append(documents)
        if self._insert_documents:
            await self._insert_documents(documents)


class TestChromaFirstOrdering:
    async def test_chroma_failure_never_reaches_postgres(self, store, monkeypatch):
        """A Chroma-write failure (dimension mismatch here) must prevent the
        Postgres insert from ever being attempted — no orphaned documents row."""
        monkeypatch.setattr(vector_store, "_get_store", lambda: store)
        gateway = _FakeGateway()

        async def fake_get_gateway():
            return gateway

        monkeypatch.setattr(vector_store, "get_gateway", fake_get_gateway)

        with pytest.raises(EmbeddingDimensionMismatch):
            await vector_store.upsert_documents(**_args(dim=999))  # wrong dim

        assert gateway.insert_calls == [], "Postgres insert must not run after a Chroma failure"
        assert store.count() == 0

    async def test_chroma_and_postgres_both_succeed_normally(self, store, monkeypatch):
        monkeypatch.setattr(vector_store, "_get_store", lambda: store)
        gateway = _FakeGateway()

        async def fake_get_gateway():
            return gateway

        monkeypatch.setattr(vector_store, "get_gateway", fake_get_gateway)

        await vector_store.upsert_documents(**_args())

        assert store.count() == 1
        assert len(gateway.insert_calls) == 1


class TestCompensatingDelete:
    async def test_postgres_failure_rolls_back_chroma_write(self, store, monkeypatch):
        """
        Chroma succeeds, then Postgres fails (e.g. a case_id FK violation).
        The just-written Chroma chunk must be deleted (compensating action)
        and the original exception re-raised — no Chroma-only orphan left
        behind.
        """
        monkeypatch.setattr(vector_store, "_get_store", lambda: store)

        async def failing_insert(documents):
            raise RuntimeError("simulated FK violation on case_id")

        gateway = _FakeGateway(insert_documents=failing_insert)

        async def fake_get_gateway():
            return gateway

        monkeypatch.setattr(vector_store, "get_gateway", fake_get_gateway)

        with pytest.raises(RuntimeError, match="simulated FK violation"):
            await vector_store.upsert_documents(**_args(chunk_id="orphan1"))

        assert store.count() == 0, "Chroma chunk must be rolled back after the Postgres failure"

    async def test_compensating_delete_only_removes_this_calls_ids(self, store, monkeypatch):
        """A failed second upsert must not delete an unrelated, already-committed chunk."""
        monkeypatch.setattr(vector_store, "_get_store", lambda: store)

        gateway_ok = _FakeGateway()

        async def fake_get_gateway_ok():
            return gateway_ok

        monkeypatch.setattr(vector_store, "get_gateway", fake_get_gateway_ok)
        await vector_store.upsert_documents(**_args(chunk_id="good1"))
        assert store.count() == 1

        async def failing_insert(documents):
            raise RuntimeError("simulated failure")

        gateway_fail = _FakeGateway(insert_documents=failing_insert)

        async def fake_get_gateway_fail():
            return gateway_fail

        monkeypatch.setattr(vector_store, "get_gateway", fake_get_gateway_fail)
        with pytest.raises(RuntimeError):
            await vector_store.upsert_documents(**_args(chunk_id="bad1"))

        assert store.count() == 1, "Only the failed call's own chunk should be rolled back"
        assert store.get_by_ids(["good1"]), "The earlier, successfully-committed chunk must survive"


class TestEmbeddingDimensionGuard:
    def test_mismatched_dimension_raises_before_any_write(self, store):
        with pytest.raises(EmbeddingDimensionMismatch):
            store.upsert([{
                "id": "x1", "text": "t",
                "embedding": [0.1] * 4,  # config.EXPECTED_EMBEDDING_DIM is patched to 8
                "metadata": {"doc_id": "doc_x1", "source": "a.pdf", "chunk_index": 0},
            }])
        assert store.count() == 0

    def test_matching_dimension_succeeds(self, store):
        store.upsert([{
            "id": "x1", "text": "t",
            "embedding": [0.1] * 8,
            "metadata": {"doc_id": "doc_x1", "source": "a.pdf", "chunk_index": 0},
        }])
        assert store.count() == 1

    def test_mismatch_fires_even_against_an_empty_freshly_created_collection(self, store):
        """
        The specific gap this closes: Chroma's own dimension error only
        fires once a collection already has vectors of some dimension.
        A brand-new, empty collection (exactly `store`'s state here) must
        still be protected by this guard, not just non-empty ones.
        """
        assert store.count() == 0
        with pytest.raises(EmbeddingDimensionMismatch):
            store.upsert([{
                "id": "x1", "text": "t",
                "embedding": [0.1] * 3,
                "metadata": {"doc_id": "doc_x1", "source": "a.pdf", "chunk_index": 0},
            }])
