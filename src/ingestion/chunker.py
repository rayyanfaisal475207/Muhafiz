# ============================================================
# Text Chunker — Splitting Long Text into Manageable Pieces
#
# WHY DO WE CHUNK?
# Language models and embedding models have a maximum input size (context window).
# ChromaDB retrieves chunks, not whole documents — smaller, focused chunks
# produce more precise retrieval results because a chunk about "aspirin dosage"
# will score higher for that query than a chunk mixing dosage + side effects +
# contraindications all together.
#
# THE OVERLAP TRICK:
# Imagine cutting a book into pages, but each page includes the last 2 sentences
# of the previous page. This prevents information from being split mid-sentence
# across chunks. With overlap, every sentence appears in at least one chunk in
# full context.
#
# CHUNK SIZE CHOICE:
# - Too small (< 100 chars): chunks lack context, embeddings are meaningless
# - Too large (> 2000 chars): too much noise per chunk, retrieval becomes imprecise
# - Sweet spot: 400–700 characters (about 2–4 paragraphs)
# ============================================================

import logging
import re
from typing import Generator, Optional

from src.ingestion.document import Document
from src.ingestion.sentence_splitter import boundary_at_or_before, sentence_boundaries
from src import config

logger = logging.getLogger(__name__)


def split_text_into_chunks(
    text: str,
    chunk_size: int = config.CHUNK_SIZE,
    chunk_overlap: int = config.CHUNK_OVERLAP,
) -> list[str]:
    """
    Thin wrapper over `_split_text_into_chunks_with_offsets()` — same
    behavior and signature as always, for every existing caller. Exists
    separately from that function only so `chunk_documents()` (below) can
    also get each chunk's starting character offset, needed to derive
    per-chunk `page` metadata after Module 8's page-concatenation fix
    (`_group_pdf_pages()`) — offsets are meaningless to any caller that
    only wants the chunk text itself.

    Split a long text string into overlapping fixed-size chunks.

    Strategy:
    1. Snap the break point to the last SENTENCE boundary that fits in
       the window (see src/ingestion/sentence_splitter.py). Paragraph
       breaks are sentence boundaries too, so this subsumes the old
       "split on \\n\\n first" rule while filling chunks fuller.
    2. If no sentence boundary fits — a single sentence longer than
       chunk_size, or unpunctuated text — fall back to the nearest
       whitespace before the limit (never cut mid-word).
    3. Each new chunk starts (chunk_overlap) characters before the previous
       chunk ended, ensuring boundary sentences appear in full context.

    WHY SENTENCE BOUNDARIES (Phase 2.3):
    The previous version broke on raw character offsets and whitespace.
    In English that lands mid-sentence often enough to hurt; in Urdu it
    is worse, because the search had no notion of ۔ (U+06D4) at all — it
    could only ever find spaces, so every Urdu chunk boundary was
    arbitrary. 87 of this corpus's 96 documents are Urdu-script. A chunk
    that starts mid-clause embeds badly and reads badly as a citation.

    Args:
        text:          The full text to split.
        chunk_size:    Maximum characters per chunk (from config).
        chunk_overlap: Characters of overlap between adjacent chunks (from config).

    Returns:
        List of text strings, each <= chunk_size characters.
    """
    return [chunk for _, chunk in _split_text_into_chunks_with_offsets(text, chunk_size, chunk_overlap)]


def _split_text_into_chunks_with_offsets(
    text: str,
    chunk_size: int = config.CHUNK_SIZE,
    chunk_overlap: int = config.CHUNK_OVERLAP,
) -> list[tuple[int, str]]:
    """
    Identical splitting logic to `split_text_into_chunks()` (this WAS that
    function's body — see its own docstring for the full rationale), except
    each result also carries the chunk's starting character offset in
    `text`. Needed only by `chunk_documents()`, to derive a merged-PDF
    chunk's `page` metadata after Module 8's page-concatenation fix — every
    other caller wants `split_text_into_chunks()`'s plain `list[str]`.
    """
    if not text or not text.strip():
        return []

    # Normalise whitespace: collapse triple+ newlines to double
    text = "\n\n".join(
        block.strip()
        for block in text.split("\n\n")
        if block.strip()
    )

    chunks: list[tuple[int, str]] = []
    start = 0
    text_len = len(text)

    # A break-point search that lands close to `start` (a short table row,
    # a short bulleted-list line, or the same distant paragraph break being
    # rediscovered every iteration as `start` creeps toward it) can make
    # (end - start) - chunk_overlap collapse to near zero or negative. The
    # old floor of max(1, ...) still terminated, but by advancing 1
    # character at a time — emitting a near-duplicate micro-chunk at every
    # step until the window finally cleared the trap. Guaranteeing a
    # minimum real advance, independent of where the break-point search
    # lands, fixes that regardless of what text triggers it.
    min_advance = max(1, chunk_size // 4)

    # Computed once for the whole text, not per iteration: the old
    # rfind-per-window search rescanned the same span on every step.
    boundaries = sentence_boundaries(text)

    while start < text_len:
        end = start + chunk_size

        if end >= text_len:
            # Last chunk: take whatever is left
            chunk = text[start:]
        else:
            # Snap to the last sentence end that fits in the window.
            sentence_break = boundary_at_or_before(boundaries, end, start)
            if sentence_break is not None:
                end = sentence_break
            else:
                # No sentence boundary in range (one very long sentence,
                # or unpunctuated text). Fall back: break at the last
                # whitespace so we still never cut a word in half.
                ws_break = text.rfind(" ", start, end)
                if ws_break != -1 and ws_break > start:
                    end = ws_break

            chunk = text[start:end].strip()

        chunk = chunk.strip()
        if chunk:
            # `start` (not the post-.strip() content start) is precise
            # enough here — this offset is only ever used to pick which
            # PAGE a chunk begins on (spans of hundreds+ characters), and
            # .strip() removes at most a few leading whitespace chars.
            chunks.append((start, chunk))

        # Advance start with overlap, but never below min_advance: if the
        # break point found was too close to `start` to make real progress
        # after applying overlap, skip the overlap for this one step and
        # take a full chunk_size stride instead, rather than crawl.
        advance = (end - start) - chunk_overlap
        if advance < min_advance:
            advance = min(chunk_size, text_len - start)
        start += advance

    logger.debug("Split text into %d chunks (size=%d, overlap=%d)",
                 len(chunks), chunk_size, chunk_overlap)
    return chunks


def _group_pdf_pages(documents: list[Document]) -> list[tuple[Document, list[tuple[int, Optional[int]]]]]:
    """
    [Gold-QA fix — Module 8] PDF loaders (`loaders/pdf_loader.py`) return
    one `Document` per PAGE. Before this fix, `chunk_documents()` chunked
    each page's Document independently — every chunk boundary was
    therefore ALSO silently a page boundary, regardless of `chunk_size`.
    Any section/sentence whose text spans a page break — the common case,
    not the exception, for a dense multi-page document like a legal code —
    got its two halves permanently split into unrelated chunks sharing no
    context window. Confirmed live: the CrPC PDF's actual Section 154 text
    existed as just its bare heading ("154. Information in cognizable
    cases.") in one chunk, with the substantive body text severed into a
    different page's chunk entirely — of 2,360 total chunks, only 5
    mentioned "154" at all.

    Groups consecutive same-source PDF-type Documents into one virtual
    Document per file, page text joined by "\\n\\n" (the same paragraph
    separator `split_text_into_chunks()` already normalizes on), so the
    chunker sees one continuous stream per PDF and its existing sentence-
    boundary snapping can naturally prefer to break BETWEEN sections
    instead of mid-section.

    Non-PDF Documents (Excel per-sheet, docx per-section) are NOT grouped —
    gated explicitly on `metadata.get("type") == "pdf"`, not just "shares a
    source", because those splits are semantically real (a sheet is a
    genuinely distinct table), not a pagination artifact — concatenating
    them would be wrong, unlike PDF pages which carry no semantic meaning
    of their own.

    Returns a list of (representative_document, page_offsets) pairs, where
    `page_offsets` is a list of (char_offset, page_number) marking where
    each source page's text begins in the merged text — used by
    `chunk_documents()` below to derive each output CHUNK's `page`
    metadata as the page its start character falls into, without
    polluting the merged text itself with a literal page-marker string
    (which would pollute embeddings and citations with "page 47" noise).
    """
    groups: list[tuple[Document, list[tuple[int, Optional[int]]]]] = []
    i = 0
    n = len(documents)
    while i < n:
        doc = documents[i]
        if doc.metadata.get("type") != "pdf":
            groups.append((doc, [(0, doc.metadata.get("page"))]))
            i += 1
            continue

        source = doc.metadata.get("source")
        run = [doc]
        j = i + 1
        while (
            j < n
            and documents[j].metadata.get("type") == "pdf"
            and documents[j].metadata.get("source") == source
        ):
            run.append(documents[j])
            j += 1

        if len(run) == 1:
            groups.append((doc, [(0, doc.metadata.get("page"))]))
        else:
            merged_text_parts: list[str] = []
            page_offsets: list[tuple[int, Optional[int]]] = []
            offset = 0
            for k, page_doc in enumerate(run):
                page_offsets.append((offset, page_doc.metadata.get("page")))
                merged_text_parts.append(page_doc.text)
                offset += len(page_doc.text)
                if k < len(run) - 1:
                    offset += 2  # the "\n\n" separator joined below
            merged_doc = Document(
                text="\n\n".join(merged_text_parts),
                metadata=dict(run[0].metadata),
                doc_id=run[0].doc_id,
            )
            groups.append((merged_doc, page_offsets))
        i = j
    return groups


def _page_for_offset(page_offsets: list[tuple[int, Optional[int]]], char_offset: int) -> Optional[int]:
    """The page number whose span contains `char_offset` — the last
    `page_offsets` entry whose starting offset is <= `char_offset`."""
    page = page_offsets[0][1] if page_offsets else None
    for start_offset, page_no in page_offsets:
        if start_offset <= char_offset:
            page = page_no
        else:
            break
    return page


# [Gold-QA fix — Module 8] A chunk that is structurally a numbered-entry
# list (a table of contents / index page) reads almost identically to a
# real section heading ("154. Information in cognizable cases.") but
# carries none of the actual statutory text — confirmed live, these
# fragments compete with and often outrank the real section content in
# retrieval. Heuristic: a high fraction of the chunk's lines matching a
# short "<number>. <title>" pattern (a TOC/index entry), with little else,
# marks it as such. Tags via the metadata["section"] field — already
# declared in the Chroma metadata allowlist (src/retrieval/vector_store.py)
# but unused until now — rather than inventing a new key, which would need
# that allowlist updated in two places for no benefit over reusing what's
# already plumbed through.
_TOC_ENTRY_LINE_RE = re.compile(r"^\s*\d+[A-Za-z]?\.\s")
_TOC_LINE_FRACTION_THRESHOLD = 0.6
_TOC_MIN_LINES = 3


def _looks_like_table_of_contents(text: str) -> bool:
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if len(lines) < _TOC_MIN_LINES:
        return False
    toc_lines = sum(1 for ln in lines if _TOC_ENTRY_LINE_RE.match(ln))
    return (toc_lines / len(lines)) >= _TOC_LINE_FRACTION_THRESHOLD


def chunk_documents(
    documents: list[Document],
    chunk_size: int = config.CHUNK_SIZE,
    chunk_overlap: int = config.CHUNK_OVERLAP,
    case_id: str = None,
    project_id: str = None,
) -> list[Document]:
    """
    Take a list of raw Documents (one per page / sheet / etc.) and produce a
    larger list of smaller chunked Documents.

    Each output Document:
    - Has a text field <= chunk_size characters
    - Inherits all metadata from its parent document, plus:
      - 'chunk_index': which chunk within the parent (0-based)
      - 'chunk_total': total chunks from that parent
    - Gets a new doc_id that includes the chunk index (making it unique)

    Args:
        documents:     Raw documents from a loader.
        chunk_size:    Max characters per chunk.
        chunk_overlap: Overlap between chunks.
        case_id:       Case this ingestion belongs to, if any. The loader
                       that produced `documents` has no notion of case_id, so
                       each parent doc's `doc_id` was generated (in
                       `Document.__post_init__`) before this scope was known.
                       Tagging it here and re-deriving `doc_id` is what
                       prevents two different cases' same-named files from
                       colliding to the same id in Chroma/Postgres.
        project_id:    Same idea as case_id, for the project dimension.

    Returns:
        Flat list of chunked Document objects ready for embedding.
    """
    chunked: list[Document] = []

    # [Gold-QA fix — Module 8] Group same-source PDF pages into one
    # continuous text stream before chunking — see _group_pdf_pages()'s own
    # docstring for the full rationale. A no-op for non-PDF input (each
    # Document comes back in its own single-item group, unchanged).
    for doc, page_offsets in _group_pdf_pages(documents):
        if case_id or project_id:
            if case_id:
                doc.metadata["case_id"] = case_id
            if project_id:
                doc.metadata["project_id"] = project_id
            # Re-derive now that case_id/project_id are known — the id
            # computed at load time had no case/project dimension to seed with.
            doc.doc_id = doc._generate_id()

        text_chunks_with_offsets = _split_text_into_chunks_with_offsets(doc.text, chunk_size, chunk_overlap)

        if not text_chunks_with_offsets:
            logger.warning("Document %s produced no chunks (empty text?)", doc.doc_id)
            continue

        for i, (chunk_start, chunk_text) in enumerate(text_chunks_with_offsets):
            # Copy the parent's metadata and add chunk-level fields.
            #
            # `doc_id` is the PARENT's id, and carrying it here is load-bearing:
            # vector_store groups chunks into a document row by this key. Without
            # it, it fell back to "unknown_<chunk_id>" — a different id for every
            # chunk — so each chunk became its own one-chunk "document" and the
            # chunks-per-document breakdown was meaningless.
            chunk_metadata = {
                **doc.metadata,
                "doc_id": doc.doc_id,
                "chunk_index": i,
                "chunk_total": len(text_chunks_with_offsets),
            }
            # `page` now points at whichever source page this chunk's text
            # actually STARTS on (a merged multi-page PDF chunk may span
            # more than one page — the schema only has one `page` slot, see
            # _group_pdf_pages()'s own docstring — the first page it
            # overlaps is what a citation needs). A no-op for non-PDF/
            # single-page input: page_offsets is a single (0, original_page)
            # entry, so this always resolves to the same page the old code
            # already set.
            resolved_page = _page_for_offset(page_offsets, chunk_start)
            if resolved_page is not None:
                chunk_metadata["page"] = resolved_page
            if _looks_like_table_of_contents(chunk_text):
                chunk_metadata["section"] = "table_of_contents"
            # Derive a unique ID: parent_id + chunk index
            chunk_id = f"{doc.doc_id}_c{i}"

            chunked.append(
                Document(text=chunk_text, metadata=chunk_metadata, doc_id=chunk_id)
            )

    logger.info(
        "Chunked %d documents → %d chunks", len(documents), len(chunked)
    )
    return chunked
