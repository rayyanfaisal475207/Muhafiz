"""
PDF loader (Docling) — Document contract verification.

Marked `slow`: unlike the rest of the suite (no network, no real
anything), this genuinely runs Docling's layout/table-structure models
against a real PDF. The first Docling call in a process pays a one-time
model warm-up cost (~minutes, not the sub-second budget the rest of the
suite holds itself to) — see tests/README.md's "No network, ever"
principle, which this test is a deliberate, marked exception to.

Run everything except this: `pytest -m "not slow"`
"""
from pathlib import Path

import pytest

from src.ingestion.document import Document
from src.ingestion.loaders.pdf_loader import load_pdf

pytestmark = pytest.mark.slow

PAGE_1_TEXT = "PAGE ONE"
PAGE_2_TEXT = "PAGE TWO"


def _make_two_page_pdf(path: Path) -> None:
    """
    A single drawString() line per page isn't representative of a real
    document — Docling's layout model mis-attributed both pages' text to
    page 1 on input that bare-bones (verified separately; does not
    reproduce on any real 40-doc corpus file, including a genuine 2-page
    one). Multiple paragraphs per page is enough real structure for
    Docling's layout clustering to resolve pages correctly, matching what
    every real document in this dataset already looks like.
    """
    from reportlab.pdfgen import canvas
    from reportlab.lib.pagesizes import letter

    c = canvas.Canvas(str(path), pagesize=letter)
    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 720, f"MUHAFIZ TEST FIXTURE — {PAGE_1_TEXT}")
    c.setFont("Helvetica", 11)
    c.drawString(72, 690, "Traffic accident report heading — Islamabad Capital Territory Police.")
    c.drawString(72, 670, "Report No.: TEST-2026-001    Traffic Sector: Test Sector I")
    c.drawString(72, 650, "Date/Time: 2026-01-08 21:53    Location: Test Highway near Test Junction")
    c.drawString(72, 630, "Vehicles Involved: 1) Test Motorcycle; 2) Test Car")
    c.drawString(72, 610, "This paragraph exists purely to give the layout model enough real")
    c.drawString(72, 595, "document structure to resolve page boundaries correctly, the same way")
    c.drawString(72, 580, "every genuine document in the 40-doc corpus already does.")
    c.showPage()

    c.setFont("Helvetica-Bold", 14)
    c.drawString(72, 720, f"MUHAFIZ TEST FIXTURE — {PAGE_2_TEXT}")
    c.setFont("Helvetica", 11)
    c.drawString(72, 690, "Witness statement continuation — Islamabad Capital Territory Police.")
    c.drawString(72, 670, "Witness Name: Test Witness    CNIC: 00000-0000000-0")
    c.drawString(72, 650, "Statement: The witness described the sequence of events leading up to")
    c.drawString(72, 630, "the incident in detail, consistent with the account given by both parties")
    c.drawString(72, 610, "involved. This paragraph exists purely to give the layout model enough")
    c.drawString(72, 595, "real document structure to resolve page boundaries correctly.")
    c.showPage()
    c.save()


@pytest.fixture(scope="module")
def loaded_documents(tmp_path_factory) -> list[Document]:
    """Generate a small 2-page PDF fixture and run the real loader against it once."""
    pdf_dir = tmp_path_factory.mktemp("pdf_loader_fixture")
    pdf_path = pdf_dir / "fixture.pdf"
    _make_two_page_pdf(pdf_path)
    return load_pdf(pdf_path)


def test_returns_one_document_per_page(loaded_documents):
    assert len(loaded_documents) == 2


def test_every_result_satisfies_the_document_contract(loaded_documents):
    for doc in loaded_documents:
        assert isinstance(doc, Document)
        assert isinstance(doc.text, str) and doc.text
        assert isinstance(doc.metadata, dict)
        assert isinstance(doc.doc_id, str) and doc.doc_id


def test_metadata_carries_required_fields(loaded_documents):
    for doc in loaded_documents:
        assert doc.metadata["source"] == "fixture.pdf"
        assert doc.metadata["type"] == "pdf"
        assert doc.metadata["total_pages"] == 2
        assert doc.metadata["extraction_method"] in ("docling", "vision_llm")


def test_pages_are_returned_in_order_and_1_indexed(loaded_documents):
    pages = [doc.metadata["page"] for doc in loaded_documents]
    assert pages == [1, 2]


def test_page_text_matches_the_source_page(loaded_documents):
    """
    Regression guard for the page_no= plumbing: page 1's Document must
    contain page 1's text, not page 2's (and vice versa) — a mixed-up
    page_no would silently return the whole document's text on every page.
    """
    page_1_doc = next(d for d in loaded_documents if d.metadata["page"] == 1)
    page_2_doc = next(d for d in loaded_documents if d.metadata["page"] == 2)

    assert "PAGE ONE" in page_1_doc.text
    assert "PAGE TWO" not in page_1_doc.text

    assert "PAGE TWO" in page_2_doc.text
    assert "PAGE ONE" not in page_2_doc.text


def test_doc_ids_are_unique_across_pages(loaded_documents):
    ids = [doc.doc_id for doc in loaded_documents]
    assert len(ids) == len(set(ids))
