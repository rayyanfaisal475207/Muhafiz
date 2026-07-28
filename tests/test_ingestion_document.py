"""
Module 4.1 — case-scoped doc_id generation (src/ingestion/document.py,
src/ingestion/chunker.py) and retrieval-filter consistency
(src/retrieval/vector_store._build_where).

Guards the fix for: two different cases ingesting a same-named file
(identical filename/page/first-200-chars) used to hash to the SAME doc_id,
so Case B's chunks silently overwrote Case A's in Chroma (unconditional
upsert-by-id). Folding case_id/project_id into the hash seed makes that
structurally impossible going forward.
"""
from src.ingestion.chunker import chunk_documents
from src.ingestion.document import Document
from src.retrieval.vector_store import _build_where


def _make(case_id=None, project_id=None, text="Same opening text across both cases."):
    metadata = {"source": "scan001.pdf", "page": "1"}
    if case_id:
        metadata["case_id"] = case_id
    if project_id:
        metadata["project_id"] = project_id
    return Document(text=text, metadata=metadata)


class TestGenerateIdCaseScoping:
    def test_same_file_different_case_ids_produce_different_doc_ids(self):
        doc_a = _make(case_id="CASE-001")
        doc_b = _make(case_id="CASE-002")
        assert doc_a.doc_id != doc_b.doc_id

    def test_same_file_same_case_id_produces_same_doc_id(self):
        doc_a = _make(case_id="CASE-001")
        doc_b = _make(case_id="CASE-001")
        assert doc_a.doc_id == doc_b.doc_id

    def test_case_id_and_project_id_scope_independently(self):
        doc_case = _make(case_id="CASE-001")
        doc_project = _make(project_id="PROJ-001")
        assert doc_case.doc_id != doc_project.doc_id

    def test_no_scope_falls_back_to_global(self):
        doc_a = _make()
        doc_b = _make()
        assert doc_a.doc_id == doc_b.doc_id


class TestChunkerTagsCaseBeforeRederivingId:
    def test_chunk_documents_retags_doc_id_once_case_id_is_known(self):
        """
        The loader has no notion of case_id, so the parent Document's
        doc_id is generated once (untagged) at load time. chunk_documents
        must re-tag metadata and re-derive doc_id before deriving chunk
        ids, or the case dimension never actually reaches Chroma/Postgres.
        """
        untagged = Document(text="x" * 50, metadata={"source": "a.pdf", "page": "1"})
        pre_id = untagged.doc_id

        chunks = chunk_documents([untagged], chunk_size=1000, chunk_overlap=0, case_id="CASE-001")

        assert chunks, "expected at least one chunk"
        assert chunks[0].metadata["case_id"] == "CASE-001"
        assert not chunks[0].doc_id.startswith(pre_id), (
            "chunk id must derive from the re-tagged (case-scoped) doc_id, not the original untagged one"
        )

    def test_two_cases_ingesting_the_same_file_get_disjoint_chunk_ids(self):
        doc_case_a = Document(text="Same content, both cases.", metadata={"source": "scan001.pdf", "page": "1"})
        doc_case_b = Document(text="Same content, both cases.", metadata={"source": "scan001.pdf", "page": "1"})

        chunks_a = chunk_documents([doc_case_a], chunk_size=1000, chunk_overlap=0, case_id="CASE-001")
        chunks_b = chunk_documents([doc_case_b], chunk_size=1000, chunk_overlap=0, case_id="CASE-002")

        ids_a = {c.doc_id for c in chunks_a}
        ids_b = {c.doc_id for c in chunks_b}
        assert ids_a.isdisjoint(ids_b), "Case A and Case B chunk ids must never collide"


class TestBuildWhereCaseAlone:
    """
    The orchestrator's where_clause construction (not _build_where itself)
    was the actual bug: a case-scoped query with no project_id passed
    {"is_global": True, "case_id": X}, and real case evidence (is_global
    is always False) failed the is_global half. This guards _build_where's
    own contract, which the fixed orchestrator now relies on: passing
    case_id alone yields a bare case_id filter, not an is_global AND.
    """

    def test_case_id_alone_produces_bare_case_filter(self):
        assert _build_where({"case_id": "CASE-001"}) == {"case_id": {"$eq": "CASE-001"}}

    def test_case_id_alone_does_not_and_is_global(self):
        result = _build_where({"case_id": "CASE-001"})
        assert result != {"$and": [{"is_global": {"$eq": True}}, {"case_id": {"$eq": "CASE-001"}}]}
