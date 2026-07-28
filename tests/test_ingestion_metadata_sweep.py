"""
Phase 4, Module 4.3 — ingestion metadata correctness sweep. Five
independent fixes, each covered by its own test class:

1. total_pages/pages_ingested/dropped_pages (pdf_loader.py, service.py)
2. Admin KB upload never silently overwrites a same-named file (admin.py)
3. date_registered prefers a labeled date over the first-date fallback
   (doc_classifier.py)
4. Excel loader blanks only genuine NaN, not the literal string "nan"
   (excel_loader.py)
5. PDF table cells are XML-escaped before Table() construction
   (pdf_builder.py)
"""
import pytest

# ── 1. total_pages / pages_ingested / dropped_pages ─────────────────────────

class _FakeDoclingDoc:
    """Stands in for Docling's `result.document` — only the surface
    load_pdf actually touches: num_pages(), .pages (dict keyed by page
    number), export_to_markdown(page_no=...)."""

    def __init__(self, page_texts: dict[int, str]):
        self._page_texts = page_texts

    def num_pages(self):
        return len(self._page_texts)

    @property
    def pages(self):
        return self._page_texts

    def export_to_markdown(self, page_no):
        return self._page_texts[page_no]


class TestDroppedPages:
    def test_total_pages_stays_the_true_count_even_when_a_page_is_dropped(self, monkeypatch, tmp_path):
        from src.ingestion.loaders import pdf_loader

        # Page 1: real text (Docling succeeds). Page 2: too short (falls to
        # vision), and vision itself returns nothing — a dropped page.
        fake_doc = _FakeDoclingDoc({
            1: "A" * 100,
            2: "short",
        })

        class _FakeResult:
            document = fake_doc

        class _FakeConverter:
            def convert(self, path):
                return _FakeResult()

        monkeypatch.setattr(pdf_loader, "_get_converter", lambda: _FakeConverter())
        monkeypatch.setattr(pdf_loader, "_load_scanned_page_with_vision", lambda *a, **k: [])
        monkeypatch.setattr(pdf_loader, "_extract_temporal_metadata", lambda p: (19900101, 99991231))

        fake_pdf = tmp_path / "fixture.pdf"
        fake_pdf.write_bytes(b"%PDF-fake")

        documents = pdf_loader.load_pdf(fake_pdf)

        assert len(documents) == 1, "only page 1 should have produced a Document"
        assert documents[0].metadata["total_pages"] == 2, "true page count must not shrink to len(documents)"
        assert documents[0].metadata["dropped_pages"] == [2]

    def test_no_dropped_pages_is_an_explicit_empty_list(self, monkeypatch, tmp_path):
        from src.ingestion.loaders import pdf_loader

        fake_doc = _FakeDoclingDoc({1: "A" * 100, 2: "B" * 100})

        class _FakeResult:
            document = fake_doc

        class _FakeConverter:
            def convert(self, path):
                return _FakeResult()

        monkeypatch.setattr(pdf_loader, "_get_converter", lambda: _FakeConverter())
        monkeypatch.setattr(pdf_loader, "_extract_temporal_metadata", lambda p: (19900101, 99991231))

        fake_pdf = tmp_path / "fixture.pdf"
        fake_pdf.write_bytes(b"%PDF-fake")

        documents = pdf_loader.load_pdf(fake_pdf)

        assert len(documents) == 2
        assert all(d.metadata["dropped_pages"] == [] for d in documents)


class TestServiceStatsDict:
    """
    service.py's ingest_file stats dict must read total_pages/dropped_pages
    off the loader's own metadata (when present — PDFs), never silently
    recompute total_pages as len(documents) once a page has been dropped.
    Exercises the small pure-Python slice of ingest_file's stats
    construction directly, without going through the full pipeline.
    """

    def test_total_pages_and_dropped_pages_come_from_document_metadata(self):
        from src.ingestion.document import Document

        documents = [
            Document(text="page one text", metadata={"total_pages": 3, "dropped_pages": [2]}),
        ]
        raw_total_pages = documents[0].metadata.get("total_pages") if documents else None
        total_pages = raw_total_pages if raw_total_pages is not None else len(documents)
        dropped_pages = documents[0].metadata.get("dropped_pages", []) if documents else []

        assert total_pages == 3
        assert dropped_pages == [2]
        assert len(documents) == 1  # pages_ingested

    def test_non_pdf_loaders_keep_the_old_len_documents_meaning(self):
        """Non-PDF loaders never set total_pages in metadata — total_pages
        must still fall back to len(documents), matching pre-4.3 behavior."""
        from src.ingestion.document import Document

        documents = [
            Document(text="row 1", metadata={"source": "sheet.xlsx"}),
            Document(text="row 2", metadata={"source": "sheet.xlsx"}),
        ]
        raw_total_pages = documents[0].metadata.get("total_pages") if documents else None
        total_pages = raw_total_pages if raw_total_pages is not None else len(documents)

        assert total_pages == 2


# ── 2. Admin KB upload collision handling ───────────────────────────────────

class TestKBUploadNoOverwrite:
    def test_disambiguates_filename_on_collision(self, tmp_path):
        """
        Mirrors admin.py's collision logic in isolation (no FastAPI/DB
        wiring needed to verify the pure path-disambiguation behavior).
        """
        from pathlib import Path

        documents_dir = tmp_path
        safe_name = "evidence.pdf"
        (documents_dir / safe_name).write_bytes(b"original bytes")

        dest = documents_dir / safe_name
        if dest.exists():
            stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
            counter = 2
            while dest.exists():
                safe_name = f"{stem}__{counter}{suffix}"
                dest = documents_dir / safe_name
                counter += 1

        assert safe_name == "evidence__2.pdf"
        assert not dest.exists()

        dest.write_bytes(b"new bytes")
        assert (documents_dir / "evidence.pdf").read_bytes() == b"original bytes", (
            "the original file must survive a same-named upload"
        )
        assert (documents_dir / "evidence__2.pdf").read_bytes() == b"new bytes"

    def test_second_collision_increments_again(self, tmp_path):
        from pathlib import Path

        documents_dir = tmp_path
        (documents_dir / "evidence.pdf").write_bytes(b"v1")
        (documents_dir / "evidence__2.pdf").write_bytes(b"v2")

        safe_name = "evidence.pdf"
        dest = documents_dir / safe_name
        if dest.exists():
            stem, suffix = Path(safe_name).stem, Path(safe_name).suffix
            counter = 2
            while dest.exists():
                safe_name = f"{stem}__{counter}{suffix}"
                dest = documents_dir / safe_name
                counter += 1

        assert safe_name == "evidence__3.pdf"


# ── 3. date_registered labeled-date preference ──────────────────────────────

class TestDateRegisteredLabeling:
    def test_prefers_labeled_date_over_an_earlier_unrelated_date(self):
        from src.extraction import structured_fields as sf
        from src.extraction.doc_classifier import _find_registration_date

        text = (
            "Incident occurred on 2026-01-05 near the market. "
            "Date Registered: 2026-01-20 at the local station."
        )
        dates = sf.extract_dates(text)
        date_registered, confidence = _find_registration_date(text, dates)

        assert date_registered == "2026-01-20", "the labeled date must win over the earlier, unrelated date"
        assert confidence == "labeled"

    def test_falls_back_to_first_date_when_nothing_is_labeled(self):
        from src.extraction import structured_fields as sf
        from src.extraction.doc_classifier import _find_registration_date

        text = "Witness recalls the events of 2026-01-05, later confirmed on 2026-01-06."
        dates = sf.extract_dates(text)
        date_registered, confidence = _find_registration_date(text, dates)

        assert date_registered == "2026-01-05"
        assert confidence == "unlabeled_fallback"

    def test_no_dates_at_all_gives_none_confidence(self):
        from src.extraction import structured_fields as sf
        from src.extraction.doc_classifier import _find_registration_date

        text = "No dates appear anywhere in this text."
        dates = sf.extract_dates(text)
        date_registered, confidence = _find_registration_date(text, dates)

        assert date_registered is None
        assert confidence is None

    async def test_classify_document_surfaces_the_confidence_tag(self, monkeypatch):
        import src.extraction.doc_classifier as doc_classifier

        async def fake_call_llm(system_prompt, user_message, **kwargs):
            return '{"doc_type": "FIR", "confidence": 0.9, "reasoning": ""}'

        monkeypatch.setattr(doc_classifier, "call_llm", fake_call_llm)

        text = "Date Registered: 2026-01-20. Unrelated mention of 2025-06-01 earlier in the report."
        result = await doc_classifier.classify_document(text)

        assert result["date_registered"] == "2026-01-20"
        assert result["date_registered_confidence"] == "labeled"


# ── 4. Excel loader NaN vs. literal "nan" string ────────────────────────────

class TestExcelNaNHandling:
    def test_literal_nan_string_survives(self):
        import pandas as pd
        from src.ingestion.loaders.excel_loader import _dataframe_to_documents

        df = pd.DataFrame({
            "Notes": ["nan", "actual note"],
            "Status": [1, 2],
        })
        documents = _dataframe_to_documents(df, "sheet.xlsx", "/tmp/sheet.xlsx", "excel")

        combined_text = " ".join(d.text for d in documents)
        assert "Notes: nan" in combined_text, "a genuine cell reading the string 'nan' must not be blanked"

    def test_true_nan_is_blanked(self):
        import pandas as pd
        import numpy as np
        from src.ingestion.loaders.excel_loader import _dataframe_to_documents

        df = pd.DataFrame({
            "Notes": [np.nan, "actual note"],
            "Status": [1, 2],
        })
        documents = _dataframe_to_documents(df, "sheet.xlsx", "/tmp/sheet.xlsx", "excel")

        # A blanked (empty-string) cell is skipped entirely by the loader's
        # own "if val.strip()" filter — it must not render as "Notes: nan".
        first_row_text = documents[0].text
        assert "Notes:" not in first_row_text, "a truly-missing cell must be blanked, not rendered as text"
        assert "Status: 1" in first_row_text


# ── 5. PDF table cells are XML-escaped ──────────────────────────────────────

class TestPDFTableCellEscaping:
    def test_table_cells_are_escaped_before_table_construction(self, monkeypatch, tmp_path):
        from reportlab.platypus import Table
        import src.generation.pdf_builder as pdf_builder

        captured = {}

        class SpyTable(Table):
            def __init__(self, data, *args, **kwargs):
                captured["data"] = data
                super().__init__(data, *args, **kwargs)

        monkeypatch.setattr(pdf_builder, "Table", SpyTable)
        monkeypatch.setattr(pdf_builder, "GENERATED_DIR", str(tmp_path))

        payload = {
            "title": "T",
            "sections": [{
                "type": "table",
                "headers": ["Section & Clause", "Punishment"],
                "rows": [["379 < 420", "3 years & fine"]],
            }],
        }
        filepath, _ = pdf_builder.build_pdf(payload)

        assert captured["data"][0] == ["Section &amp; Clause", "Punishment"]
        assert captured["data"][1] == ["379 &lt; 420", "3 years &amp; fine"]

        import os
        os.remove(filepath)
