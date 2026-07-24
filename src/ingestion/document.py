# ============================================================
# Document Dataclass — The Unified Interface for All Formats
#
# WHY THIS EXISTS:
# Every file format (PDF, Excel, Word, etc.) loads data differently.
# If each loader returned a different structure, every downstream
# component (chunker, embedder, ChromaDB writer) would need to
# handle every format separately — that's a maintenance nightmare.
#
# The solution is the Strategy Pattern + a shared interface:
# - Each loader is responsible for extracting text and metadata
#   from its own format
# - Every loader returns the SAME Document dataclass
# - After the loader, the pipeline doesn't care about the format anymore
#
# Think of it like a post office: no matter where a parcel came from
# (truck, airplane, motorcycle), once it's sorted at the depot it
# goes on the same conveyor belt.
# ============================================================

from dataclasses import dataclass, field
import hashlib


@dataclass
class Document:
    """
    The single unified data structure that every loader returns.

    Attributes:
        text     : The extracted plain text content of the document chunk.
        metadata : A dictionary of descriptive information about the source.
                   Must always include at least 'source' (filename) and 'type'
                   (file format). Other keys depend on the format, e.g.:
                     - PDFs   → 'page': int
                     - Excel  → 'sheet': str, 'row_start': int, 'row_end': int
                     - Images → 'vision_model': str
        doc_id   : A unique, deterministic identifier. "Deterministic" means
                   the same source file always produces the same IDs, so
                   re-ingesting a file replaces (upserts) existing chunks
                   rather than creating duplicates.
    """

    text: str
    metadata: dict = field(default_factory=dict)
    doc_id: str = field(default="")

    def __post_init__(self) -> None:
        """Auto-generate doc_id if not provided."""
        if not self.doc_id:
            self.doc_id = self._generate_id()

    def _generate_id(self) -> str:
        """
        Create a deterministic ID from the source path + a short hash of the text.

        Using source + text hash means:
        - Same file re-ingested → same ID → ChromaDB upsert replaces it (no duplicate)
        - Different files with identical text → different IDs (different source)
        """
        source = self.metadata.get("source", "unknown")
        page = str(self.metadata.get("page", ""))
        # Take first 200 chars of text for the hash seed (fast, stable)
        seed = f"{source}::{page}::{self.text[:200]}"
        short_hash = hashlib.md5(seed.encode()).hexdigest()[:8]
        # Replace path separators so the ID is filesystem-safe
        safe_source = source.replace("/", "_").replace("\\", "_").replace(".", "_")
        return f"{safe_source}_{short_hash}"

    def __repr__(self) -> str:
        preview = self.text[:60].replace("\n", " ")
        return (
            f"Document(id={self.doc_id!r}, "
            f"source={self.metadata.get('source', '?')!r}, "
            f"preview={preview!r}...)"
        )
