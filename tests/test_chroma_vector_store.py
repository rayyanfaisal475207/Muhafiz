"""
ChromaVectorStore — project-scope isolation and upsert-dedup verification.

This is the Phase 1 guarantee named in the upgrade plan and the current
README: a document scoped to one project must never surface in another
project's (or no project's) retrieval results. Uses a real Chroma
PersistentClient against a temp directory — no mocking of Chroma itself,
since the isolation logic lives in the `where` filter translation, not in
a fake.
"""
import pytest

from src import config
from src.retrieval.vector_store import ChromaVectorStore

PROJECT_A = "11111111-1111-1111-1111-111111111111"
PROJECT_B = "22222222-2222-2222-2222-222222222222"


@pytest.fixture(autouse=True)
def _small_expected_dim(monkeypatch):
    """
    This file's fixtures use 8-dim toy vectors — isolation/upsert-dedup
    logic doesn't care about real embedding dimensions. Module 4.2 added a
    dimension guard to ChromaVectorStore.upsert() checked against
    config.EXPECTED_EMBEDDING_DIM (1024 by default, for the real e5
    provider); without this, every upsert() call here would raise
    EmbeddingDimensionMismatch for reasons unrelated to what these tests
    actually verify.
    """
    monkeypatch.setattr(config, "EXPECTED_EMBEDDING_DIM", 8)


@pytest.fixture
def store(tmp_path):
    ChromaVectorStore.reset_instance()
    s = ChromaVectorStore(persist_dir=tmp_path / "chroma")
    yield s
    ChromaVectorStore.reset_instance()


def _chunk(chunk_id, project_id=None, is_global=False, text="some chunk text", case_id=None):
    return {
        "id": chunk_id,
        "text": text,
        "embedding": [0.1] * 8,
        "metadata": {
            "doc_id": f"doc_{chunk_id}",
            "source": f"{chunk_id}.pdf",
            "chunk_index": 0,
            **({"project_id": project_id} if project_id else {}),
            **({"case_id": case_id} if case_id else {}),
            "is_global": is_global,
        },
    }


@pytest.fixture
def populated_store(store):
    store.upsert([
        _chunk("a1", project_id=PROJECT_A, text="Project A confidential content"),
        _chunk("b1", project_id=PROJECT_B, text="Project B confidential content"),
        _chunk("g1", is_global=True, text="Shared knowledge-base content"),
        _chunk("n1", text="Unscoped legacy content, neither project-tagged nor global"),
    ])
    return store


def _ids(results):
    return {r["id"] for r in results}


class TestProjectScopedIsolation:
    def test_project_a_never_sees_project_b_content(self, populated_store):
        results = populated_store.search([0.1] * 8, top_k=10, metadata_filter={"project_id": PROJECT_A})
        assert "b1" not in _ids(results), "Project B's document leaked into Project A's retrieval"

    def test_project_b_never_sees_project_a_content(self, populated_store):
        results = populated_store.search([0.1] * 8, top_k=10, metadata_filter={"project_id": PROJECT_B})
        assert "a1" not in _ids(results), "Project A's document leaked into Project B's retrieval"

    def test_project_scoped_search_includes_its_own_and_global_docs(self, populated_store):
        results = populated_store.search([0.1] * 8, top_k=10, metadata_filter={"project_id": PROJECT_A})
        assert _ids(results) == {"a1", "g1"}

    def test_project_scoped_search_excludes_unscoped_legacy_docs(self, populated_store):
        """
        A chunk with no project_id and is_global=False (the shape every
        existing production row currently has) must not appear in a
        project-scoped search — matches the original pgvector SQL's
        `d.project_id = :pid OR d.is_global = true` filter exactly.
        """
        results = populated_store.search([0.1] * 8, top_k=10, metadata_filter={"project_id": PROJECT_A})
        assert "n1" not in _ids(results)

    def test_non_project_chat_sees_only_global_docs(self, populated_store):
        """
        Phase 8, Bug 1: a non-project chat passes an explicit
        {"is_global": True} filter and must retrieve ONLY global
        knowledge-base docs — never any project's scoped documents. This
        is the corrected behavior; the earlier "no filter searches
        everything" contract leaked every project's docs to any user.
        """
        results = populated_store.search([0.1] * 8, top_k=10, metadata_filter={"is_global": True})
        assert _ids(results) == {"g1"}, "Non-project chat must see only global docs"
        assert "a1" not in _ids(results), "Project A's document leaked into a non-project chat"
        assert "b1" not in _ids(results), "Project B's document leaked into a non-project chat"

    def test_falsy_filter_is_unfiltered_primitive(self, populated_store):
        """
        The low-level store still treats a falsy filter as "search
        everything" — this is the internal/test primitive. Production
        isolation is enforced by the orchestrator always passing an
        explicit project or global filter, never None (see orchestrator's
        where_clause construction).
        """
        results = populated_store.search([0.1] * 8, top_k=10, metadata_filter=None)
        assert _ids(results) == {"a1", "b1", "g1", "n1"}


CASE_1 = "CASE-001"
CASE_2 = "CASE-002"


@pytest.fixture
def case_populated_store(store):
    store.upsert([
        _chunk("c1", is_global=True, case_id=CASE_1, text="Case 1 evidence, global corpus"),
        _chunk("c2", is_global=True, case_id=CASE_2, text="Case 2 evidence, global corpus"),
        _chunk("c3", is_global=True, text="Global evidence with no case attached (pre-Phase-1 corpus)"),
        _chunk("c4", project_id=PROJECT_A, case_id=CASE_1, text="Case 1 evidence, project A"),
    ])
    return store


class TestCaseScopedIsolation:
    """
    Phase 1: case_id is an independent filter ANDed on top of the existing
    project/global isolation guarantee (see vector_store._build_where's
    docstring for why the two dimensions don't collapse into one).
    """

    def test_case_scoped_search_returns_only_that_cases_evidence(self, case_populated_store):
        results = case_populated_store.search([0.1] * 8, top_k=10, metadata_filter={"is_global": True, "case_id": CASE_1})
        assert _ids(results) == {"c1"}

    def test_case_scoped_search_excludes_other_cases(self, case_populated_store):
        results = case_populated_store.search([0.1] * 8, top_k=10, metadata_filter={"is_global": True, "case_id": CASE_1})
        assert "c2" not in _ids(results), "Case 2's evidence leaked into Case 1's retrieval"

    def test_case_scoped_search_excludes_caseless_evidence(self, case_populated_store):
        """A case-scoped query must not pull in evidence that belongs to no case."""
        results = case_populated_store.search([0.1] * 8, top_k=10, metadata_filter={"is_global": True, "case_id": CASE_1})
        assert "c3" not in _ids(results)

    def test_no_case_filter_preserves_pre_phase1_behavior(self, case_populated_store):
        """
        Omitting case_id (as every pre-Phase-1 caller does) must behave
        exactly as before Phase 1 — is_global alone still returns every
        global chunk, case-tagged or not.
        """
        results = case_populated_store.search([0.1] * 8, top_k=10, metadata_filter={"is_global": True})
        assert _ids(results) == {"c1", "c2", "c3"}

    def test_case_filter_composes_with_project_filter(self, case_populated_store):
        """
        case_id AND project scoping together: Case 1's project-A chunk
        surfaces for project A + Case 1, alongside Case 1's global chunk
        (global docs remain visible to every project chat) — but the
        project-A-scoped Case 1 chunk must not leak into a different case
        filter, even within the same project.
        """
        results = case_populated_store.search(
            [0.1] * 8, top_k=10, metadata_filter={"project_id": PROJECT_A, "case_id": CASE_1}
        )
        assert _ids(results) == {"c1", "c4"}

        results_other_case = case_populated_store.search(
            [0.1] * 8, top_k=10, metadata_filter={"project_id": PROJECT_A, "case_id": CASE_2}
        )
        assert "c4" not in _ids(results_other_case)


class TestGetAllScopedForBm25:
    """
    RETRIEVAL_DIVERSITY_FIX_PROMPT.md, Fix 1: BM25 needs the full scoped
    candidate pool (id/text/metadata), not just a vector-search top-k.
    `get_all()` must apply the EXACT same `_build_where` scoping `search()`
    does — widening BM25's pool must never widen access control.
    """

    def test_get_all_returns_full_text_not_just_metadata(self, populated_store):
        results = populated_store.get_all(metadata_filter={"is_global": True})
        assert {"g1"} == _ids(results)
        assert results[0]["text"] == "Shared knowledge-base content"

    def test_get_all_respects_project_isolation(self, populated_store):
        results = populated_store.get_all(metadata_filter={"project_id": PROJECT_A})
        assert _ids(results) == {"a1", "g1"}
        assert "b1" not in _ids(results), "Project B's document leaked into Project A's BM25 pool"

    def test_get_all_respects_non_project_is_global_scope(self, populated_store):
        results = populated_store.get_all(metadata_filter={"is_global": True})
        assert _ids(results) == {"g1"}
        assert "a1" not in _ids(results) and "b1" not in _ids(results)

    def test_get_all_respects_case_scoping(self, case_populated_store):
        results = case_populated_store.get_all(metadata_filter={"is_global": True, "case_id": CASE_1})
        assert _ids(results) == {"c1"}
        assert "c2" not in _ids(results), "Case 2's evidence leaked into Case 1's BM25 pool"

    def test_get_all_is_not_limited_to_a_small_top_k(self, store):
        """
        The actual bug this method exists to fix: unlike `search()` (bounded
        by top_k / nearest-neighbor distance), `get_all()` must return every
        matching chunk in scope, however many there are — BM25's whole point
        is to see documents vector search's top-k would never surface.
        """
        store.upsert([
            _chunk(f"doc{i}", is_global=True, text=f"chunk number {i}")
            for i in range(25)
        ])
        results = store.get_all(metadata_filter={"is_global": True})
        assert len(results) == 25


class TestUpsertSemantics:
    def test_upsert_same_id_overwrites_not_duplicates(self, store):
        store.upsert([_chunk("x1", text="original text")])
        store.upsert([_chunk("x1", text="updated text")])

        assert store.count() == 1
        results = store.search([0.1] * 8, top_k=10)
        assert results[0]["text"] == "updated text"

    def test_upsert_empty_list_is_a_noop(self, store):
        store.upsert([])
        assert store.count() == 0


class TestDeleteBySource:
    def test_delete_by_source_removes_only_that_documents_chunks(self, populated_store):
        deleted = populated_store.delete_by_source("a1.pdf")

        assert deleted == 1
        remaining = _ids(populated_store.search([0.1] * 8, top_k=10))
        assert "a1" not in remaining
        assert {"b1", "g1", "n1"} <= remaining
