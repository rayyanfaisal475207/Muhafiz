"""
M2 of the Muhafiz Data API migration (docs/decisions/0001-muhafiz-api-migration.md) —
extraction of ingest_documents() out of ingest_file(), so a non-file source
(e.g. src/ingestion/muhafiz_records.py, M3) can chunk/embed/store/graph-extract
a list of already-loaded Document objects directly, without ever touching a
Path or going through route_and_load().

Two things are guarded here:
  1. ingest_documents() itself works given only `documents` + `source_name` —
     no filesystem access anywhere in it.
  2. ingest_file() still behaves exactly as before: loads via route_and_load(),
     then delegates to ingest_documents() with the file's name/extension.
"""
import pytest

from src.ingestion import service
from src.ingestion.document import Document


class _FakeGateway:
    def __init__(self):
        self.logged: list[dict] = []

    async def log_document(self, doc_id, filename, doc_type=None, chunk_count=None,
                            is_global=False, case_id=None):
        self.logged.append({
            "doc_id": doc_id, "filename": filename, "doc_type": doc_type,
            "chunk_count": chunk_count, "is_global": is_global, "case_id": case_id,
        })


@pytest.fixture
def stub_pipeline(monkeypatch):
    """Stub embedding + storage + gateway, leaving real chunking/normalization."""
    upserts = []
    gateway = _FakeGateway()

    async def fake_embed_texts(texts, task_type="RETRIEVAL_DOCUMENT"):
        return [[0.1, 0.2, 0.3] for _ in texts]

    async def fake_upsert_documents(ids, texts, embeddings, metadatas):
        upserts.append({"ids": ids, "texts": texts, "embeddings": embeddings, "metadatas": metadatas})

    async def fake_get_gateway():
        return gateway

    monkeypatch.setattr(service, "embed_texts", fake_embed_texts)
    monkeypatch.setattr(service, "upsert_documents", fake_upsert_documents)
    monkeypatch.setattr("src.data_gateway.get_gateway", fake_get_gateway)

    return {"upserts": upserts, "gateway": gateway}


class TestIngestDocumentsIsSourceAgnostic:
    async def test_ingests_plain_documents_with_no_path_involved(self, stub_pipeline):
        """The whole point of M2: this must work from a list of Document
        objects built by something other than a file loader — no Path
        object appears anywhere in this test."""
        documents = [Document(
            text="Some evidence narrative long enough to survive chunking without issue here.",
            metadata={"source": "psrms/fir/fir-891-24"},
        )]

        result = await service.ingest_documents(
            documents, source_name="psrms/fir/fir-891-24",
            is_global=True, doc_type="fir_narrative",
        )

        assert result["chunks_added"] >= 1
        assert "error" not in result
        assert stub_pipeline["upserts"], "upsert_documents must have been called"

    async def test_doc_type_reaches_chunk_metadata_when_given(self, stub_pipeline):
        documents = [Document(text="Narrative text of sufficient length for one chunk.",
                               metadata={"source": "psrms/fir/fir-1-26"})]

        await service.ingest_documents(
            documents, source_name="psrms/fir/fir-1-26", doc_type="fir_narrative",
        )

        metadatas = stub_pipeline["upserts"][0]["metadatas"]
        assert all(m["doc_type"] == "fir_narrative" for m in metadatas)

    async def test_doc_type_omitted_leaves_chunk_metadata_key_unset(self, stub_pipeline):
        """A non-file caller that has no meaningful doc_type must not have
        one silently invented for it."""
        documents = [Document(text="Narrative text of sufficient length for one chunk.",
                               metadata={"source": "cms/complaint/CMSC-1"})]

        await service.ingest_documents(documents, source_name="cms/complaint/CMSC-1")

        metadatas = stub_pipeline["upserts"][0]["metadatas"]
        assert all("doc_type" not in m for m in metadatas)

    async def test_source_name_reaches_gateway_log_document_as_filename(self, stub_pipeline):
        documents = [Document(text="Narrative text of sufficient length for one chunk.",
                               metadata={"source": "pkm/application/PKM-1"})]

        await service.ingest_documents(documents, source_name="pkm/application/PKM-1")

        assert stub_pipeline["gateway"].logged
        assert stub_pipeline["gateway"].logged[0]["filename"] == "pkm/application/PKM-1"

    async def test_case_id_triggers_graph_extraction_call(self, monkeypatch, stub_pipeline):
        calls = []

        async def fake_run_graph_extraction(source_name, documents, chunks, case_id, doc_id):
            calls.append((source_name, case_id, doc_id))
            return {"errors": []}

        async def fake_conflict_bg(case_id, doc_id):
            return None

        monkeypatch.setattr(service, "_run_graph_extraction", fake_run_graph_extraction)
        monkeypatch.setattr(service, "_run_conflict_detection_bg", fake_conflict_bg)

        documents = [Document(text="Narrative text of sufficient length for one chunk.",
                               metadata={"source": "psrms/fir/fir-2-26"})]

        result = await service.ingest_documents(
            documents, source_name="psrms/fir/fir-2-26", case_id="fir-2-26",
        )

        assert calls, "graph extraction must run when case_id is given"
        assert calls[0][0] == "psrms/fir/fir-2-26", "source_name must reach _run_graph_extraction, not a Path"
        assert result["graph"] == {"errors": []}

    async def test_no_case_id_skips_graph_extraction(self, monkeypatch, stub_pipeline):
        called = []
        monkeypatch.setattr(service, "_run_graph_extraction",
                             lambda *a, **k: called.append(1))

        documents = [Document(text="Narrative text of sufficient length for one chunk.",
                               metadata={"source": "roznamcha/entry-1"})]
        result = await service.ingest_documents(documents, source_name="roznamcha/entry-1")

        assert not called
        assert result["graph"] is None

    async def test_run_graph_extraction_false_skips_extraction_despite_case_id(
        self, monkeypatch, stub_pipeline,
    ):
        """
        M9 (docs/decisions/0001-muhafiz-api-migration.md): a caller with its
        own deterministic graph writer (structured_projection.py) opts out
        of the legacy LLM/NER pass entirely — case_id still scopes chunks
        for retrieval, but no _run_graph_extraction() call happens.
        """
        called = []
        monkeypatch.setattr(service, "_run_graph_extraction",
                             lambda *a, **k: called.append(1))

        documents = [Document(text="Narrative text of sufficient length for one chunk.",
                               metadata={"source": "psrms/fir/fir-1-26#narrative"})]
        result = await service.ingest_documents(
            documents, source_name="psrms/fir/fir-1-26#narrative",
            case_id="fir-1-26", run_graph_extraction=False,
        )

        assert not called
        assert result["graph"] is None
        # case_id still reached chunk metadata for retrieval scoping.
        assert stub_pipeline["upserts"][0]["metadatas"][0]["case_id"] == "fir-1-26"

    async def test_run_graph_extraction_default_true_preserves_existing_behavior(
        self, monkeypatch, stub_pipeline,
    ):
        called = []

        async def fake_run_graph_extraction(*a, **k):
            called.append(1)
            return {"errors": []}
        monkeypatch.setattr(service, "_run_graph_extraction", fake_run_graph_extraction)
        monkeypatch.setattr(service, "_run_conflict_detection_bg",
                             lambda *a, **k: None)

        documents = [Document(text="Narrative text of sufficient length for one chunk.",
                               metadata={"source": "file.pdf"})]
        await service.ingest_documents(documents, source_name="file.pdf", case_id="CASE-1")

        assert called, "omitting run_graph_extraction must default to True, unchanged"

    async def test_empty_chunk_result_returns_zero_with_error(self, stub_pipeline):
        """Mirrors ingest_file's pre-existing 'no chunks generated' path —
        must survive unchanged in the extracted function."""
        documents = [Document(text="", metadata={"source": "empty"})]
        result = await service.ingest_documents(documents, source_name="empty")
        assert result["chunks_added"] == 0
        assert "error" in result


class TestIngestFileDelegatesUnchanged:
    async def test_ingest_file_calls_ingest_documents_with_filename_and_extension(
        self, monkeypatch, stub_pipeline, tmp_path,
    ):
        """Regression guard for the M2 split itself: ingest_file's public
        contract (doc_type = file suffix, source_name = file name) must be
        byte-for-byte what it was before the extraction."""
        captured = {}

        async def fake_ingest_documents(documents, source_name, **kwargs):
            captured["source_name"] = source_name
            captured["kwargs"] = kwargs
            return {"chunks_added": 1, "doc_id": "d1"}

        def fake_route_and_load(file_path):
            return [Document(text="loaded text", metadata={"source": file_path.name})]

        monkeypatch.setattr(service, "route_and_load", fake_route_and_load)
        monkeypatch.setattr(service, "ingest_documents", fake_ingest_documents)

        fake_file = tmp_path / "evidence.PDF"
        fake_file.write_bytes(b"%PDF-fake")

        result = await service.ingest_file(fake_file, is_global=True, case_id="CASE-1")

        assert captured["source_name"] == "evidence.PDF"
        assert captured["kwargs"]["doc_type"] == "pdf", "extension must be lowercased, dot-stripped"
        assert captured["kwargs"]["is_global"] is True
        assert captured["kwargs"]["case_id"] == "CASE-1"
        assert result == {"chunks_added": 1, "doc_id": "d1"}

    async def test_ingest_file_load_failure_still_returns_error_dict(self, monkeypatch, tmp_path):
        """The load-step try/except that used to wrap the whole function
        must still catch a route_and_load exception after the split."""
        def boom(file_path):
            raise RuntimeError("docling exploded")

        monkeypatch.setattr(service, "route_and_load", boom)

        fake_file = tmp_path / "bad.pdf"
        fake_file.write_bytes(b"%PDF-fake")
        result = await service.ingest_file(fake_file)

        assert result["chunks_added"] == 0
        assert "docling exploded" in result["error"]

    async def test_ingest_file_no_documents_short_circuits_before_delegating(
        self, monkeypatch, stub_pipeline, tmp_path,
    ):
        called = []
        monkeypatch.setattr(service, "route_and_load", lambda p: [])
        monkeypatch.setattr(service, "ingest_documents",
                             lambda *a, **k: called.append(1))

        fake_file = tmp_path / "blank.pdf"
        fake_file.write_bytes(b"%PDF-fake")
        result = await service.ingest_file(fake_file)

        assert result == {"chunks_added": 0, "error": "No text could be extracted from this file."}
        assert not called, "ingest_documents must not be called when there's nothing to ingest"
