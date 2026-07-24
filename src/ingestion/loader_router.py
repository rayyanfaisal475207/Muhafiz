# ============================================================
# Loader Router — Format Detection and Dispatch Table
#
# WHY A DISPATCH TABLE, NOT IF/ELIF?
# A long if/elif chain is hard to extend. Every time you add a format
# you have to find the right place in the chain and insert a branch.
# A dispatch table (dictionary) is the "Strategy Pattern":
#   - Adding a new format = adding ONE entry to LOADER_MAP
#   - No other code changes needed anywhere
#   - The routing logic (look up extension, call function) never changes
#
# This is exactly how web frameworks route URLs: they don't have
# a giant if/elif — they have a route table.
# ============================================================

import logging
from pathlib import Path
from typing import Callable

from src.ingestion.document import Document

# Import all format-specific loaders
from src.ingestion.loaders.text_loader import load_text_file
from src.ingestion.loaders.pdf_loader import load_pdf
from src.ingestion.loaders.excel_loader import load_excel, load_csv
from src.ingestion.loaders.html_loader import load_html
from src.ingestion.loaders.docx_loader import load_docx
from src.ingestion.loaders.image_loader import load_image_with_vision

logger = logging.getLogger(__name__)

# ── The Dispatch Table ────────────────────────────────────────────────────────
# Maps file extension (lowercase, with dot) → loader function.
# Each loader function signature: (file_path: Path) -> list[Document]
LOADER_MAP: dict[str, Callable[[Path], list[Document]]] = {
    ".txt":  load_text_file,
    ".md":   load_text_file,
    ".pdf":  load_pdf,
    ".xlsx": load_excel,
    ".xls":  load_excel,
    ".csv":  load_csv,
    ".html": load_html,
    ".htm":  load_html,
    ".docx": load_docx,
    ".jpg":  load_image_with_vision,
    ".jpeg": load_image_with_vision,
    ".png":  load_image_with_vision,
    ".webp": load_image_with_vision,
    ".gif":  load_image_with_vision,
}

SUPPORTED_EXTENSIONS = set(LOADER_MAP.keys())


def route_and_load(file_path: Path) -> list[Document]:
    """
    Detect the file format by extension and call the appropriate loader.

    Args:
        file_path: Path to the source document.

    Returns:
        A list of Document objects. Most loaders return multiple Documents
        (one per page, chunk, or row batch) rather than one giant document.

    Raises:
        ValueError: If the file extension is not in LOADER_MAP.
        FileNotFoundError: If the file does not exist.
    """
    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")

    extension = file_path.suffix.lower()

    if extension not in LOADER_MAP:
        supported = ", ".join(sorted(SUPPORTED_EXTENSIONS))
        raise ValueError(
            f"Unsupported file format '{extension}'. "
            f"Supported formats: {supported}"
        )

    loader_fn = LOADER_MAP[extension]
    logger.info("Loading %s using %s", file_path.name, loader_fn.__name__)

    documents = loader_fn(file_path)

    logger.info(
        "Loaded %d document(s) from %s", len(documents), file_path.name
    )
    return documents


def is_supported(file_path: Path) -> bool:
    """Quick check: does this file's extension have a registered loader?"""
    return file_path.suffix.lower() in SUPPORTED_EXTENSIONS
