"""
[Gold-QA fix — Module 8b] Batched Docling conversion for large PDFs.

Fast, mocked — deliberately NOT sharing test_pdf_loader.py's `slow` module
mark (which runs real Docling models against a real PDF). A single
converter.convert() call over a large (200+ page) PDF corrupts specific
pages' extracted text — confirmed empirically against the real CrPC PDF:
Section 154's body text extracted correctly, byte-for-byte identical, both
in isolation (page_range=(77, 78)) and within a 1-100 page batch, but was
truncated to just its bare heading when the whole 319-page document was
converted in ONE call. load_pdf() now converts in page-range batches
instead of one whole-document call. These tests verify the batching LOGIC
itself (call boundaries, correct page-to-text reassembly) with a fake
converter — no real Docling/PDF work needed for that.
"""
import src.ingestion.loaders.pdf_loader as pdf_loader_module


class _FakePage:
    pass


class _FakeDoclingDoc:
    """Mimics the subset of docling's Document API load_pdf() actually
    calls: .pages (a dict keyed by page number) and
    .export_to_markdown(page_no=...)."""

    def __init__(self, page_numbers, text_by_page):
        self.pages = {n: _FakePage() for n in page_numbers}
        self._text_by_page = text_by_page

    def export_to_markdown(self, page_no):
        return self._text_by_page[page_no]

    def num_pages(self):
        return len(self.pages)


class _FakeConvertResult:
    def __init__(self, document):
        self.document = document


class _FakeConverter:
    """Records every convert() call's page_range and returns a fake
    per-batch document covering exactly that range, with deterministic
    per-page text — enough to verify batching boundaries and correct
    page-to-text reassembly without any real Docling/PDF work."""

    def __init__(self, total_pages):
        self.total_pages = total_pages
        self.calls: list[tuple] = []

    def convert(self, path, page_range=None):
        if page_range is None:
            start, end = 1, self.total_pages
        else:
            start, end = page_range
        self.calls.append((start, end))
        page_numbers = list(range(start, end + 1))
        text_by_page = {
            n: f"PAGE {n} REAL BODY TEXT, more than fifty characters long for sure."
            for n in page_numbers
        }
        return _FakeConvertResult(_FakeDoclingDoc(page_numbers, text_by_page))


def _patch_pdf_loader(monkeypatch, total_pages, batch_size=None):
    fake_converter = _FakeConverter(total_pages)
    monkeypatch.setattr(pdf_loader_module, "_get_converter", lambda: fake_converter)
    monkeypatch.setattr(pdf_loader_module, "_cheap_page_count", lambda file_path: total_pages)
    if batch_size is not None:
        monkeypatch.setattr(pdf_loader_module, "_DOCLING_BATCH_SIZE", batch_size)
    return fake_converter


def test_large_document_is_converted_in_multiple_batches(monkeypatch, tmp_path):
    """A 170-page document with batch_size=80 must be converted in exactly
    3 calls, covering (1,80), (81,160), (161,170) — no gaps, no overlap
    beyond the batch boundaries themselves."""
    fake_converter = _patch_pdf_loader(monkeypatch, total_pages=170, batch_size=80)

    docs = pdf_loader_module.load_pdf(tmp_path / "fake.pdf")

    assert fake_converter.calls == [(1, 80), (81, 160), (161, 170)]
    assert len(docs) == 170
    assert {d.metadata["page"] for d in docs} == set(range(1, 171))


def test_small_document_makes_exactly_one_batch_call(monkeypatch, tmp_path):
    """A document at or under the batch size must still make exactly ONE
    convert() call — identical cost to the pre-fix behavior, just via an
    explicit page_range covering the whole (small) document instead of
    none at all."""
    fake_converter = _patch_pdf_loader(monkeypatch, total_pages=5, batch_size=80)

    docs = pdf_loader_module.load_pdf(tmp_path / "fake.pdf")

    assert fake_converter.calls == [(1, 5)]
    assert len(docs) == 5


def test_page_text_is_not_mixed_up_across_batches(monkeypatch, tmp_path):
    fake_converter = _patch_pdf_loader(monkeypatch, total_pages=170, batch_size=80)

    docs = pdf_loader_module.load_pdf(tmp_path / "fake.pdf")

    doc_by_page = {d.metadata["page"]: d for d in docs}
    # One page from each of the 3 batches.
    assert "PAGE 1 REAL BODY TEXT" in doc_by_page[1].text
    assert "PAGE 80 REAL BODY TEXT" in doc_by_page[80].text
    assert "PAGE 81 REAL BODY TEXT" in doc_by_page[81].text
    assert "PAGE 160 REAL BODY TEXT" in doc_by_page[160].text
    assert "PAGE 161 REAL BODY TEXT" in doc_by_page[161].text
    assert "PAGE 170 REAL BODY TEXT" in doc_by_page[170].text
    # No page's text leaked another page's number into it.
    assert "PAGE 2" not in doc_by_page[1].text


def test_total_pages_metadata_reflects_the_real_document_total_not_a_batch_size(monkeypatch, tmp_path):
    """A batched conversion's own Docling result objects only ever see
    their OWN batch's page count — total_pages metadata must come from
    the cheap whole-document count, not a batch's num_pages()."""
    _patch_pdf_loader(monkeypatch, total_pages=170, batch_size=80)

    docs = pdf_loader_module.load_pdf(tmp_path / "fake.pdf")

    assert all(d.metadata["total_pages"] == 170 for d in docs)


def test_falls_back_to_a_single_conversion_when_page_count_is_unavailable(monkeypatch, tmp_path):
    """If PyMuPDF can't cheaply count pages, fall back to the original
    single whole-document conversion (no page_range) rather than failing
    outright."""
    fake_converter = _FakeConverter(total_pages=5)
    monkeypatch.setattr(pdf_loader_module, "_get_converter", lambda: fake_converter)
    monkeypatch.setattr(pdf_loader_module, "_cheap_page_count", lambda file_path: None)

    docs = pdf_loader_module.load_pdf(tmp_path / "fake.pdf")

    assert fake_converter.calls == [(1, 5)]  # convert() called with page_range=None -> (1, total_pages) in the fake
    assert len(docs) == 5
