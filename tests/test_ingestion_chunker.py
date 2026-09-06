"""
Tests for src/ingestion/chunker.py.

Module 8 (Gold-QA fix): PDF loaders return one Document per PAGE
(loaders/pdf_loader.py) — chunk_documents() used to chunk each page
independently, so every chunk boundary was silently also a page boundary,
regardless of chunk_size. A section/sentence spanning a page break got its
two halves permanently split into unrelated chunks with no shared context —
confirmed live against the CrPC PDF (2,360 chunks, only 5 mentioned "154";
the real Section 154 chunk was just its bare heading, with the substantive
body severed onto a different page's chunk entirely).
"""
import pytest

from src.ingestion.chunker import chunk_documents, split_text_into_chunks
from src.ingestion.document import Document


def _pdf_page(source, page, text):
    return Document(text=text, metadata={"source": source, "type": "pdf", "page": page})


# ── The regression this module exists to fix ────────────────────────────────

def test_a_sentence_spanning_a_page_break_is_not_split_across_chunks():
    """The exact CrPC bug: a section heading at the bottom of one page and
    its body text at the top of the next must land in the SAME chunk (or at
    least a chunk that has both), not be silently severed at the page
    boundary the way independent per-page chunking used to guarantee."""
    page1 = _pdf_page("crpc.pdf", 1, "Some preceding content.\n\n154. Information in cognizable cases.")
    page2 = _pdf_page(
        "crpc.pdf", 2,
        "Every information relating to the commission of a cognizable offence "
        "shall be reduced to writing by the officer in charge, read over to "
        "the informant, and signed by him.",
    )

    chunks = chunk_documents([page1, page2], chunk_size=512, chunk_overlap=50)
    combined = " ".join(c.text for c in chunks)

    assert "154. Information in cognizable cases." in combined
    # Find the chunk containing the heading and confirm it ALSO contains
    # real body text, not just the bare heading the old per-page chunking
    # produced.
    heading_chunk = next(c for c in chunks if "154. Information in cognizable cases." in c.text)
    assert "reduced to writing" in heading_chunk.text


def test_pdf_pages_of_the_same_source_are_merged_before_chunking():
    """Two short pages that together fit in one chunk_size window must
    produce ONE chunk, not two independently-chunked fragments."""
    page1 = _pdf_page("doc.pdf", 1, "First page short text.")
    page2 = _pdf_page("doc.pdf", 2, "Second page short text.")

    chunks = chunk_documents([page1, page2], chunk_size=200, chunk_overlap=20)

    assert len(chunks) == 1
    assert "First page short text." in chunks[0].text
    assert "Second page short text." in chunks[0].text


def test_pdf_pages_of_different_sources_are_not_merged_together():
    """Grouping is scoped to consecutive pages sharing the SAME source —
    two different PDFs' pages must never be concatenated into one chunk."""
    page_a = _pdf_page("a.pdf", 1, "Document A content.")
    page_b = _pdf_page("b.pdf", 1, "Document B content.")

    chunks = chunk_documents([page_a, page_b], chunk_size=200, chunk_overlap=20)

    assert len(chunks) == 2
    sources = {c.metadata["source"] for c in chunks}
    assert sources == {"a.pdf", "b.pdf"}
    for c in chunks:
        if c.metadata["source"] == "a.pdf":
            assert "Document B content." not in c.text
        else:
            assert "Document A content." not in c.text


def test_non_pdf_documents_are_never_merged_across_documents():
    """Excel per-sheet / docx per-section Documents are semantically
    distinct splits, not a pagination artifact — concatenating them would
    be wrong. Grouping must be gated on type == "pdf", not just "shares a
    source"."""
    sheet1 = Document(text="Sheet 1 data.", metadata={"source": "book.xlsx", "type": "excel", "sheet": "Sheet1"})
    sheet2 = Document(text="Sheet 2 data.", metadata={"source": "book.xlsx", "type": "excel", "sheet": "Sheet2"})

    chunks = chunk_documents([sheet1, sheet2], chunk_size=200, chunk_overlap=20)

    assert len(chunks) == 2
    for c in chunks:
        assert "Sheet 1 data." not in c.text or "Sheet 2 data." not in c.text


def test_chunk_page_metadata_reflects_the_page_the_chunk_actually_starts_on():
    """A merged multi-page PDF's chunk must carry the PAGE it starts on,
    not always page 1 or a stale value from the first page in the group."""
    page1 = _pdf_page("doc.pdf", 1, "A" * 400)
    page2 = _pdf_page("doc.pdf", 2, "B" * 400)

    chunks = chunk_documents([page1, page2], chunk_size=300, chunk_overlap=20)

    pages_seen = {c.metadata.get("page") for c in chunks}
    # With 800+ chars split into 300-char windows, at least one chunk must
    # start on page 2 — a bug that always reported page 1 (the group's
    # first page) would fail this.
    assert 2 in pages_seen


def test_single_page_pdf_is_unaffected():
    """A lone PDF page (no adjacent same-source page) must behave exactly
    as before — this is a no-op path for the common single-page case."""
    page = _pdf_page("single.pdf", 1, "Just one page of content, nothing to merge.")

    chunks = chunk_documents([page], chunk_size=200, chunk_overlap=20)

    assert len(chunks) == 1
    assert chunks[0].metadata["page"] == 1


# ── Table-of-contents tagging ────────────────────────────────────────────────

def test_table_of_contents_style_chunk_is_tagged():
    toc_text = "\n".join(f"{n}. Some Section Title Here." for n in range(1, 10))
    page = _pdf_page("code.pdf", 1, toc_text)

    chunks = chunk_documents([page], chunk_size=2000, chunk_overlap=0)

    assert len(chunks) == 1
    assert chunks[0].metadata.get("section") == "table_of_contents"


def test_real_prose_chunk_is_not_tagged_as_table_of_contents():
    prose = _pdf_page(
        "code.pdf", 1,
        "154. Information in cognizable cases. Every information relating to "
        "the commission of a cognizable offence shall be reduced to writing "
        "by the officer in charge of a police station, read over to the "
        "informant, and signed by the person giving it, and the substance "
        "thereof shall be entered in a book kept by such officer in such "
        "form as the Provincial Government may prescribe in this behalf.",
    )

    chunks = chunk_documents([prose], chunk_size=2000, chunk_overlap=0)

    assert len(chunks) == 1
    assert chunks[0].metadata.get("section") != "table_of_contents"


# ── split_text_into_chunks() itself must be unaffected by the refactor ──────

def test_split_text_into_chunks_public_api_is_unchanged():
    """The offset-carrying refactor must not change split_text_into_chunks()'s
    own behavior or return type for its existing callers."""
    text = "First sentence here. Second sentence here. Third sentence here."
    result = split_text_into_chunks(text, chunk_size=40, chunk_overlap=5)

    assert isinstance(result, list)
    assert all(isinstance(c, str) for c in result)
    assert "".join(result).replace(" ", "") != ""  # produced real content


# ── [Gold-QA fix — Module 8d] no-gap coverage guarantee ──
# The splitter must never DROP a span of text: every character of the input
# must appear in at least one chunk. A short sentence-boundary chunk used to
# trigger a full-chunk_size stride that leapt over the un-emitted tail,
# silently dropping ~448-char spans (live-confirmed: CrPC §154's body).

from src.ingestion.chunker import _split_text_into_chunks_with_offsets
from src import config


def _covers_all_chars(text, chunk_size, overlap):
    spans = _split_text_into_chunks_with_offsets(text, chunk_size, overlap)
    covered = [False] * len(text)
    for start, chunk in spans:
        # find the chunk's actual span in text (chunk is stripped, so locate it)
        idx = text.find(chunk[:40], max(0, start - 5)) if chunk else -1
        if idx < 0:
            idx = start
        for j in range(idx, min(len(text), idx + len(chunk))):
            covered[j] = True
    # allow uncovered runs only where the char is whitespace (strip artifacts)
    gaps = [i for i, c in enumerate(covered) if not c and not text[i].isspace()]
    return gaps


def test_splitter_covers_every_non_whitespace_char_dense_text():
    # Dense text with many short sentences (each ends well before chunk_size),
    # the exact shape that triggered the drop.
    text = " ".join(
        f"{n}. A short section heading. Then a body sentence that ends here."
        for n in range(140, 170)
    )
    gaps = _covers_all_chars(text, config.CHUNK_SIZE, config.CHUNK_OVERLAP)
    assert gaps == [], f"splitter dropped non-whitespace chars at indices {gaps[:10]}"


def test_splitter_no_gap_with_short_boundary_chunks():
    # A pathological case: very short sentences so each chunk snaps early.
    text = ". ".join("word " * 3 for _ in range(200))
    gaps = _covers_all_chars(text, 512, 64)
    assert gaps == [], f"dropped chars at {gaps[:10]}"
