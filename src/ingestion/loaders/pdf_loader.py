# ============================================================
# PDF Loader — Docling-Based Extraction with Vision Fallback
#
# Docling handles both flavours of PDF in one pass:
#   1. Text-based PDFs: Docling parses the text layer directly, plus
#      layout analysis (headings, lists) and table-structure detection —
#      form-style key/value tables (FIR fields, traffic report fields)
#      come out as real markdown tables instead of a flat run of labels.
#   2. Scanned PDFs: Docling's default pipeline includes built-in OCR
#      (RapidOCR), applied automatically per page when the text layer is
#      weak or absent — no separate code path needed for this repo.
#
# SECONDARY FALLBACK: if a page's Docling output is still empty/garbled
# even after Docling's own OCR (a genuinely bad scan), we render that one
# page with PyMuPDF and pass it to the Gemini Vision fallback — the same
# mechanism this loader already used before the Docling swap.
# ============================================================

import logging
import json
import re
import threading
from pathlib import Path

from dateutil import parser
from src.ingestion.document import Document

logger = logging.getLogger(__name__)

# Minimum characters per page before we consider extraction "failed"
# and fall back to vision. Some pages have headers/footers but no real text.
MIN_TEXT_CHARS = 50

# Docling's DocumentConverter loads layout/table-structure models on first
# use (~a few minutes, one-time per process); reusing one instance across
# calls means only the very first PDF in a process pays that cost.
_converter = None
_converter_lock = threading.Lock()


def _get_converter():
    global _converter
    if _converter is None:
        with _converter_lock:
            if _converter is None:
                from docling.document_converter import DocumentConverter
                _converter = DocumentConverter()
    return _converter


def _extract_temporal_metadata(file_path: Path) -> tuple[int, int]:
    """Extract effective_from and effective_to dates as YYYYMMDD integers for ChromaDB filtering."""
    effective_from = 19900101
    effective_to = 99991231

    # Try checking metadata.jsonl for SROs
    metadata_file = file_path.parent / "metadata.jsonl"
    if metadata_file.exists():
        try:
            with open(metadata_file, "r", encoding="utf-8") as f:
                for line in f:
                    if not line.strip(): continue
                    data = json.loads(line)
                    if file_path.name in data.get("pdf_url", ""):
                        raw_date = data.get("issue_date_raw", "")
                        try:
                            dt = parser.parse(raw_date, dayfirst=True)
                            effective_from = int(dt.strftime("%Y%m%d"))
                            return effective_from, effective_to
                        except Exception:
                            pass
        except Exception as e:
            logger.warning("Failed to parse metadata.jsonl for %s: %s", file_path.name, e)

    # Fallback to parsing filename (e.g., FinanceAct2026.pdf)
    match = re.search(r'(20\d{2})', file_path.name)
    if match:
        year = match.group(1)
        effective_from = int(f"{year}0701") # Default to fiscal year start

        # Check if it's an auto-expiring rate schedule
        if "FinanceAct" in file_path.name or "Finance_Act" in file_path.name:
            next_year = int(year) + 1
            effective_to = int(f"{next_year}0630")

    return effective_from, effective_to


def load_pdf(file_path: Path) -> list[Document]:
    """
    Load a PDF file via Docling, returning one Document per page.

    For each page:
    - If Docling's extraction succeeds (>= MIN_TEXT_CHARS, including via its
      own built-in OCR for scanned pages), use that text (markdown, with
      detected tables rendered as markdown tables).
    - If a page's text is still empty/garbled after Docling (a genuinely
      bad scan Docling's own OCR couldn't recover), fall back to rendering
      that page and calling the Gemini Vision path.

    Args:
        file_path: Path to the .pdf file.

    Returns:
        List of Documents, one per page with text content.
    """
    documents: list[Document] = []
    effective_from, effective_to = _extract_temporal_metadata(file_path)

    try:
        converter = _get_converter()
        result = converter.convert(str(file_path))
        doc = result.document
    except Exception as exc:
        logger.error("Docling failed to convert %s: %s", file_path.name, exc)
        raise

    total_pages = doc.num_pages()
    logger.info("Opened PDF %s (%d pages) via Docling", file_path.name, total_pages)

    # Pages that fail BOTH Docling and the vision fallback contribute zero
    # Documents — with no record of which page numbers those were. Module
    # 4.3: track them explicitly so `total_pages` (the true page count)
    # never has to double as "how many pages actually made it", which
    # previously masked partial-ingestion silently.
    dropped_pages: list[int] = []

    for page_no in sorted(doc.pages.keys()):
        text = doc.export_to_markdown(page_no=page_no).strip()

        if len(text) >= MIN_TEXT_CHARS:
            documents.append(
                Document(
                    text=text,
                    metadata={
                        "source": file_path.name,
                        "source_path": str(file_path),
                        "type": "pdf",
                        "page": page_no,              # already 1-indexed by Docling
                        "total_pages": total_pages,
                        "extraction_method": "docling",
                        "effective_from": effective_from,
                        "effective_to": effective_to,
                    },
                )
            )
            logger.debug(
                "  Page %d: extracted %d chars via Docling", page_no, len(text)
            )
        else:
            logger.warning(
                "  Page %d of %s: Docling extraction is empty/garbled (%d chars) "
                "even with its built-in OCR. Falling back to vision LLM.",
                page_no, file_path.name, len(text)
            )
            vision_docs = _load_scanned_page_with_vision(
                file_path, page_no, total_pages, effective_from, effective_to
            )
            if not vision_docs:
                dropped_pages.append(page_no)
            documents.extend(vision_docs)

    # Every loader in LOADER_MAP shares one contract — (file_path) ->
    # list[Document] — so dropped_pages/total_pages ride along on the
    # returned Documents' own metadata rather than widening that contract.
    # ingestion/service.py reads it off documents[0], the same way it
    # already reads effective_from/effective_to.
    for d in documents:
        d.metadata["dropped_pages"] = dropped_pages

    logger.info(
        "Loaded %d pages from %s (%d dropped)", len(documents), file_path.name, len(dropped_pages)
    )
    return documents


def _load_scanned_page_with_vision(
    file_path: Path,
    page_no: int,
    total_pages: int,
    effective_from: int,
    effective_to: int,
) -> list[Document]:
    """
    Render a PDF page to a PNG image and pass it to the vision LLM for OCR.

    Secondary fallback, only reached when Docling's own (OCR-capable)
    extraction still returned too little text for this page. Uses
    PyMuPDF purely to rasterize the page — same technique as
    load_image_with_vision(), applied to a rendered page rather than a
    standalone image file.

    Args:
        file_path: Original PDF path.
        page_no:   1-based page index (matches Docling's numbering).
        total_pages: Page count, carried into metadata.
        effective_from: Extracted effective start date.
        effective_to: Extracted effective end date.

    Returns:
        List of one Document with vision-extracted text (or empty if vision fails).
    """
    max_retries = 10
    for attempt in range(max_retries):
        try:
            import fitz  # PyMuPDF — used only to rasterize this one page
            import time
            from src.ingestion.loaders.image_loader import _describe_image_bytes

            pdf = fitz.open(str(file_path))
            try:
                page = pdf[page_no - 1]  # fitz is 0-indexed
                # Render at 2x resolution (matrix scale=2) for better OCR quality
                mat = fitz.Matrix(2, 2)
                pix = page.get_pixmap(matrix=mat)
                image_bytes = pix.tobytes("png")
            finally:
                pdf.close()

            extracted_text = _describe_image_bytes(image_bytes, "png")

            if not extracted_text:
                logger.warning(
                    "Vision LLM returned no text for page %d of %s",
                    page_no, file_path.name
                )
                return []

            return [
                Document(
                    text=extracted_text,
                    metadata={
                        "source": file_path.name,
                        "source_path": str(file_path),
                        "type": "pdf",
                        "page": page_no,
                        "total_pages": total_pages,
                        "extraction_method": "vision_llm",
                        "effective_from": effective_from,
                        "effective_to": effective_to,
                    },
                )
            ]

        except Exception as exc:
            err_msg = str(exc).lower()
            if "limit: 20" in err_msg:
                logger.error(
                    "Daily Gemini Free Tier Quota (20 requests) exhausted. Aborting retries for page %d of %s.",
                    page_no, file_path.name
                )
                raise

            if "quota" in err_msg or "429" in err_msg or "exhausted" in err_msg:
                if attempt < max_retries - 1:
                    logger.warning(
                        "Rate limit hit on page %d of %s. Retrying in 120s... (%d/%d)",
                        page_no, file_path.name, attempt + 1, max_retries
                    )
                    time.sleep(120)
                    continue
                else:
                    logger.error(
                        "Vision fallback failed for page %d of %s after %d retries: %s",
                        page_no, file_path.name, max_retries, exc
                    )
                    raise  # Re-raise so the outer robust_ingest catches it and fails the whole file
            else:
                logger.error(
                    "Vision fallback failed for page %d of %s: %s",
                    page_no, file_path.name, exc
                )
                return []

    return []
