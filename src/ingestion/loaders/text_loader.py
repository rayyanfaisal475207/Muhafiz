# ============================================================
# Text Loader — Plain Text and Markdown Files
#
# The simplest loader. Text files need no conversion — we just read
# them and attach metadata. Markdown is treated exactly like plain
# text at this stage (the markdown formatting characters are kept in
# the text, which is fine for semantic search).
# ============================================================

import logging
from pathlib import Path

from src.ingestion.document import Document

logger = logging.getLogger(__name__)


def load_text_file(file_path: Path) -> list[Document]:
    """
    Load a plain text or Markdown file as a single Document.

    One file → one Document. The chunker will split it later.
    We keep it whole here to allow the chunker to split at natural
    paragraph boundaries rather than arbitrary file-read buffers.

    Args:
        file_path: Path to the .txt or .md file.

    Returns:
        List containing a single Document with the full file text.
    """
    try:
        # Read with UTF-8 first; fall back to latin-1 for legacy files
        try:
            text = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning(
                "%s is not valid UTF-8, retrying with latin-1", file_path.name
            )
            text = file_path.read_text(encoding="latin-1")

        text = text.strip()

        if not text:
            logger.warning("File %s is empty.", file_path.name)
            return []

        doc = Document(
            text=text,
            metadata={
                "source": file_path.name,
                "source_path": str(file_path),
                "type": file_path.suffix.lower().lstrip("."),  # "txt" or "md"
            },
        )
        logger.debug("Loaded text file %s (%d chars)", file_path.name, len(text))
        return [doc]

    except Exception as exc:
        logger.error("Failed to load text file %s: %s", file_path.name, exc)
        raise
