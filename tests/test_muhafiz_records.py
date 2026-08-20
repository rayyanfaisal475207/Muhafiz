"""
M3 of the Muhafiz Data API migration (docs/decisions/0001-muhafiz-api-migration.md) —
src/ingestion/muhafiz_records.py: record -> Document rendering.

Two kinds of coverage:
  1. Unit tests against small hand-built records, exercising the null/blank
     handling and stable-source-id rules directly.
  2. Tests against the real recorded snapshot (tests/fixtures/muhafiz_api_snapshot.json,
     scripts/fetch_muhafiz_snapshot.py) — the actual measured data this
     migration is built against, not invented shapes.
"""
import json
from pathlib import Path

import pytest

from src.data_gateway.muhafiz_api.models import (
    CmsComplaint, FirRecord, PkmApplication, RoznamchaEntry,
)
from src.ingestion import muhafiz_records as mr

FIXTURE = Path(__file__).parent / "fixtures" / "muhafiz_api_snapshot.json"


@pytest.fixture(scope="module")
def snapshot():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def firs(snapshot):
    return [FirRecord(r) for r in snapshot["endpoints"]["fir"]]


@pytest.fixture(scope="module")
def cms_complaints(snapshot):
    return [CmsComplaint(r) for r in snapshot["endpoints"]["cms"]]


@pytest.fixture(scope="module")
def pkm_applications(snapshot):
    return [PkmApplication(r) for r in snapshot["endpoints"]["pkm"]]


@pytest.fixture(scope="module")
def roznamcha_entries(snapshot):
    return [RoznamchaEntry(r) for r in snapshot["endpoints"]["roznamcha"]]


# ── unit tests: null/blank handling ─────────────────────────────────────────

class TestFirRendering:
    def test_narrative_produces_one_document(self):
        fir = FirRecord({
            "fir_id": "fir-1-26", "fir_display_code": "1/26",
            "narrative_text": "کچھ بیانیہ متن یہاں لکھا گیا ہے۔",
            "police_station": {"name": "Test Station", "code": "PS-T", "district": {"name": "Test District"}},
        })
        docs = mr.render_fir(fir)
        narrative_docs = [d for d in docs if d.metadata["source"].endswith("#narrative")]
        assert len(narrative_docs) == 1
        assert "بیانیہ" in narrative_docs[0].text
        assert "PS-T" not in narrative_docs[0].text  # header uses station NAME, not code
        assert "Test Station" in narrative_docs[0].text

    def test_blank_narrative_produces_no_document(self):
        fir = FirRecord({"fir_id": "fir-2-26", "narrative_text": "   "})
        docs = mr.render_fir(fir)
        assert not any(d.metadata["source"].endswith("#narrative") for d in docs)

    def test_null_fields_do_not_crash_and_produce_no_document(self):
        fir = FirRecord({"fir_id": "fir-3-26"})  # everything else absent
        docs = mr.render_fir(fir)
        assert docs == []

    def test_zimni_entries_each_get_their_own_document(self):
        fir = FirRecord({
            "fir_id": "fir-4-26",
            "fir_zimni": [
                {"id": "z1", "entry_number": 1, "entry_text": "پہلی انٹری"},
                {"id": "z2", "entry_number": 2, "entry_text": ""},  # blank, skipped
                {"id": "z3", "entry_number": 3, "entry_text": "تیسری انٹری"},
            ],
        })
        docs = mr.render_fir(fir)
        zimni_sources = sorted(d.metadata["source"] for d in docs if "zimni" in d.metadata["source"])
        assert zimni_sources == ["psrms/fir/fir-4-26#zimni_z1", "psrms/fir/fir-4-26#zimni_z3"]

    def test_record_date_carries_the_incident_datetime(self):
        """M8: lets reranker.py's recency boost read a real date instead
        of regexing a source string that has no year in it."""
        fir = FirRecord({"fir_id": "fir-8-26", "narrative_text": "متن",
                          "incident_datetime": "2026-08-18T15:10:00Z"})
        docs = mr.render_fir(fir)
        assert docs[0].metadata["record_date"] == "2026-08-18T15:10:00Z"

    def test_fir_display_code_carried_for_orchestrator_auto_scope(self):
        """M11: lets orchestrator.py's FIR-based query auto-scope match a
        real display code — the slug `source` string never contains it."""
        fir = FirRecord({"fir_id": "fir-10-26", "fir_display_code": "891/24", "narrative_text": "متن"})
        docs = mr.render_fir(fir)
        assert docs[0].metadata["fir_display_code"] == "891/24"

    def test_record_date_absent_when_no_incident_datetime(self):
        fir = FirRecord({"fir_id": "fir-9-26", "narrative_text": "متن"})
        docs = mr.render_fir(fir)
        assert docs[0].metadata["record_date"] is None

    def test_content_provenance_carries_the_synthetic_tag(self):
        fir = FirRecord({"fir_id": "fir-5-26", "narrative_text": "متن", "source": "synthetic"})
        docs = mr.render_fir(fir)
        assert docs[0].metadata["content_provenance"] == "synthetic"

    def test_record_type_and_source_system_are_always_set(self):
        fir = FirRecord({"fir_id": "fir-6-26", "narrative_text": "متن"})
        docs = mr.render_fir(fir)
        assert docs[0].metadata["record_type"] == "fir_narrative"
        assert docs[0].metadata["source_system"] == "muhafiz_api"
        assert docs[0].metadata["external_id"] == "fir-6-26"

    def test_source_id_is_stable_across_re_renders(self):
        """Constraint 3 from the decision record: re-fetching + re-rendering
        the same record must produce the SAME source string, or every sync
        run orphans the previous run's chunks/graph edges."""
        raw = {"fir_id": "fir-7-26", "narrative_text": "متن", "updated_at": "2026-08-18T00:00:00Z"}
        docs_a = mr.render_fir(FirRecord(raw))
        raw_refetched = {**raw, "updated_at": "2026-08-19T00:00:00Z"}  # only updated_at changed
        docs_b = mr.render_fir(FirRecord(raw_refetched))
        assert docs_a[0].metadata["source"] == docs_b[0].metadata["source"]
        assert docs_a[0].doc_id == docs_b[0].doc_id


class TestCmsRendering:
    def test_summary_produces_one_document(self):
        cms = CmsComplaint({
            "complaint_id": "CMSC-1", "one_line_summary": "کچھ شکایت کا خلاصہ",
            "case_tag_number": "CMS-ISB-2026-0001",
        })
        docs = mr.render_cms(cms)
        assert len(docs) == 1
        assert docs[0].metadata["source"] == "cms/complaint/CMSC-1#summary"
        assert "CMS-ISB-2026-0001" in docs[0].text

    def test_blank_summary_produces_no_document(self):
        cms = CmsComplaint({"complaint_id": "CMSC-2", "one_line_summary": None})
        assert mr.render_cms(cms) == []


class TestPkmRendering:
    def test_loss_report_produces_a_document(self):
        pkm = PkmApplication({
            "application_id": "PKM-1", "service_type": "loss_report",
            "loss_report": {"lost_item_description": "شناختی کارڈ گم ہو گیا"},
        })
        docs = mr.render_pkm(pkm)
        assert len(docs) == 1
        assert docs[0].metadata["source"] == "pkm/application/PKM-1#lost_item_description"

    def test_vehicle_verification_produces_no_document(self):
        """Purely structured (no free text worth embedding) — measured live,
        see _PKM_FREE_TEXT_FIELDS's own comment."""
        pkm = PkmApplication({
            "application_id": "PKM-2", "service_type": "vehicle_verification",
            "vehicle_verification": {"vehicle_registration_no": "ABC-123", "vehicle_make": "Honda"},
        })
        assert mr.render_pkm(pkm) == []

    def test_no_service_record_produces_no_document(self):
        pkm = PkmApplication({"application_id": "PKM-3", "service_type": "driving_license"})
        assert mr.render_pkm(pkm) == []


class TestRoznamchaRendering:
    def test_entry_text_produces_a_document(self):
        entry = RoznamchaEntry({"id": "RZ-1", "entry_text": "روزنامچہ اندراج", "entry_date": "2026-08-01"})
        docs = mr.render_roznamcha(entry)
        assert len(docs) == 1
        assert docs[0].metadata["source"] == "psrms/roznamcha/RZ-1"
        # No case_id anywhere in metadata — roznamcha stays case-less, no
        # date/station inference toward a same-day FIR (decision record #2).
        assert "case_id" not in docs[0].metadata

    def test_blank_entry_produces_no_document(self):
        entry = RoznamchaEntry({"id": "RZ-2", "entry_text": ""})
        assert mr.render_roznamcha(entry) == []


# ── against the real snapshot ────────────────────────────────────────────────

class TestAgainstRealSnapshot:
    def test_every_fir_renders_without_raising(self, firs):
        for fir in firs:
            mr.render_fir(fir)  # must not raise for any of the 73 real records

    def test_most_firs_produce_at_least_one_document(self, firs):
        # Measured: every real FIR has a non-blank narrative_text.
        counts = [len(mr.render_fir(fir)) for fir in firs]
        assert all(c >= 1 for c in counts)

    def test_every_cms_renders_without_raising(self, cms_complaints):
        for cms in cms_complaints:
            mr.render_cms(cms)

    def test_every_pkm_renders_without_raising(self, pkm_applications):
        for pkm in pkm_applications:
            mr.render_pkm(pkm)

    def test_every_roznamcha_renders_without_raising(self, roznamcha_entries):
        for entry in roznamcha_entries:
            mr.render_roznamcha(entry)

    def test_pkm_only_loss_report_and_women_violence_report_produce_documents(self, pkm_applications):
        """Locks in the measured finding: of the 7 PKM service types, only
        these 2 carry free text (10 women_violence_report + loss_report
        instances observed live vs. 0 from the other 5 types)."""
        rendered_types = set()
        for pkm in pkm_applications:
            docs = mr.render_pkm(pkm)
            if docs:
                rec = pkm.service_record()
                rendered_types.add(rec["service_type"])
        assert rendered_types <= {"loss_report", "women_violence_report"}

    def test_source_ids_are_globally_unique_across_all_firs(self, firs):
        all_sources = [d.metadata["source"] for fir in firs for d in mr.render_fir(fir)]
        assert len(all_sources) == len(set(all_sources)), "duplicate source id across different FIRs"

    def test_doc_ids_are_globally_unique_across_all_firs(self, firs):
        all_ids = [d.doc_id for fir in firs for d in mr.render_fir(fir)]
        assert len(all_ids) == len(set(all_ids))


# ── end-to-end through the real ingestion pipeline ──────────────────────────
#
# Not just render_fir() in isolation: this drives a real FIR through
# render_fir() -> service.ingest_documents() -> the REAL chunker + a REAL
# (temp-dir) Chroma instance, stubbing only the embedder (no network) and
# Postgres (no live DB in this suite). Proves the new metadata keys
# actually survive vector_store.py's two independent allowlists
# (constraint 4 in the decision record) instead of being silently dropped.

class TestEndToEndThroughIngestDocuments:
    async def test_fir_narrative_chunk_carries_muhafiz_metadata_into_chroma(
        self, monkeypatch, tmp_path, firs,
    ):
        from src import config
        from src.ingestion import service
        from src.retrieval import vector_store
        from src.retrieval.vector_store import ChromaVectorStore

        fir = next(f for f in firs if f.narrative_text and f.narrative_text.strip())
        docs = mr.render_fir(fir)
        assert docs, "fixture FIR must render at least one Document"

        monkeypatch.setattr(config, "EXPECTED_EMBEDDING_DIM", 4)
        ChromaVectorStore.reset_instance()
        store = ChromaVectorStore(persist_dir=tmp_path / "chroma")
        monkeypatch.setattr(vector_store, "_get_store", lambda: store)

        class _FakeGateway:
            async def insert_documents(self, documents):
                pass
            async def log_document(self, **kwargs):
                pass

        async def fake_get_gateway():
            return _FakeGateway()

        async def fake_embed_texts(texts, task_type="RETRIEVAL_DOCUMENT"):
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

        monkeypatch.setattr(service, "embed_texts", fake_embed_texts)
        monkeypatch.setattr("src.data_gateway.get_gateway", fake_get_gateway)
        monkeypatch.setattr(vector_store, "get_gateway", fake_get_gateway)

        result = await service.ingest_documents(
            docs, source_name=fir.fir_id, case_id=fir.fir_id, is_global=False,
        )
        assert result["chunks_added"] >= 1
        assert "error" not in result

        stored = store.get_all()
        assert stored, "chunks must actually have landed in Chroma"
        narrative_chunk = next(c for c in stored if c["metadata"].get("record_type") == "fir_narrative")
        meta = narrative_chunk["metadata"]
        assert meta["source_system"] == "muhafiz_api"
        assert meta["external_id"] == fir.fir_id
        assert meta["case_id"] == fir.fir_id
        # None-valued optional fields (e.g. district when the station has
        # none) must be absent, not present-as-null — _sanitize_metadata's
        # contract, unchanged by this migration.
        assert all(v is not None for v in meta.values())

        ChromaVectorStore.reset_instance()
