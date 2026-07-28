# ============================================================
# Shared Upload Validation
#
# Phase 7, Module 7.2 — closes two related gaps:
#   1. No file-size cap existed inside the shared ingestion path itself;
#      the 50MB limit lived only on the admin HTTP upload endpoint, so
#      ingest_directory()/a future case-evidence endpoint/a bulk re-ingest
#      had no bound at all.
#   2. No MIME/magic-byte validation anywhere: a file renamed to .pdf/
#      .docx/.xlsx with arbitrary binary content was routed straight to
#      the corresponding loader, and .docx/.xlsx (zip archives) had no
#      decompression-bomb protection.
#
# validate_file() is called from loader_router.route_and_load() — the
# single dispatch chokepoint every current and future ingestion entry
# point already goes through — rather than at each individual caller, so
# nothing can bypass it by construction.
# ============================================================

import logging
import zipfile
from pathlib import Path

from src import config

logger = logging.getLogger(__name__)


class FileValidationError(ValueError):
    """Raised by validate_file() when a file fails a pre-ingestion check."""


# Magic-byte signatures for formats where content-type sniffing is
# reliable. Plain-text formats (.txt/.md/.csv/.html/.htm) and legacy
# .xls (OLE2, ambiguous with other Office formats) have no signature
# worth enforcing here and are intentionally skipped — a mismatched
# .txt isn't the smuggling vector a mismatched .pdf/.docx/image is.
_SIGNATURES: dict[str, tuple[bytes, ...]] = {
    ".pdf": (b"%PDF-",),
    # .docx/.xlsx are zip archives; PK\x05\x06 is an empty archive,
    # PK\x07\x08 a spanned one — both legitimate, rare edge cases.
    ".docx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".xlsx": (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"),
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    # .webp needs an offset-8 check (RIFF....WEBP) — handled separately.
}

_ZIP_EXTENSIONS = {".docx", ".xlsx"}

# Reject an individual zip member whose uncompressed size exceeds this
# many times its compressed size.
_MAX_ZIP_RATIO = 100

# Reject on total uncompressed size alone, even under the per-member
# ratio cap — catches a bomb built from many moderately-compressed
# members rather than one extreme one.
_MAX_ZIP_UNCOMPRESSED_TOTAL_BYTES = 2 * 1024 * 1024 * 1024  # 2GB


def validate_file(file_path: Path) -> None:
    """
    Pre-ingestion validation shared by every entry point that reaches
    loader_router.route_and_load(): size cap, magic-byte/claimed-extension
    match, and a zip decompression-ratio guard for .docx/.xlsx.

    Raises FileValidationError (a ValueError subclass, so existing
    `except ValueError`/`except Exception` callers need no changes) on
    any check failure. Rejections are logged, never silently dropped.
    """
    if not file_path.exists():
        raise FileValidationError(f"File not found: {file_path}")

    _check_size(file_path)
    _check_magic_bytes(file_path)
    _check_zip_bomb(file_path)


def _check_size(file_path: Path) -> None:
    size_bytes = file_path.stat().st_size
    max_bytes = config.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if size_bytes > max_bytes:
        _reject(
            f"{file_path.name} is {size_bytes // 1024 // 1024}MB; "
            f"the limit is {config.MAX_UPLOAD_SIZE_MB}MB."
        )


def _check_magic_bytes(file_path: Path) -> None:
    ext = file_path.suffix.lower()

    if ext == ".webp":
        with open(file_path, "rb") as f:
            header = f.read(12)
        if not (header[:4] == b"RIFF" and header[8:12] == b"WEBP"):
            _reject(f"{file_path.name} claims to be .webp but its content doesn't match the WEBP signature.")
        return

    signatures = _SIGNATURES.get(ext)
    if signatures is None:
        return  # No reliable signature for this format — nothing to check.

    with open(file_path, "rb") as f:
        header = f.read(16)
    if not any(header.startswith(sig) for sig in signatures):
        _reject(f"{file_path.name} claims to be {ext} but its content doesn't match a {ext} signature.")


def _check_zip_bomb(file_path: Path) -> None:
    ext = file_path.suffix.lower()
    if ext not in _ZIP_EXTENSIONS:
        return

    try:
        with zipfile.ZipFile(file_path) as zf:
            total_uncompressed = 0
            for info in zf.infolist():
                total_uncompressed += info.file_size
                if info.compress_size > 0:
                    ratio = info.file_size / info.compress_size
                    if ratio > _MAX_ZIP_RATIO:
                        _reject(
                            f"{file_path.name}: member {info.filename!r} has a "
                            f"{ratio:.0f}x compression ratio (limit {_MAX_ZIP_RATIO}x) "
                            "— rejected as a possible decompression bomb."
                        )
            if total_uncompressed > _MAX_ZIP_UNCOMPRESSED_TOTAL_BYTES:
                _reject(
                    f"{file_path.name}: total uncompressed size "
                    f"({total_uncompressed // 1024 // 1024}MB) exceeds the "
                    f"{_MAX_ZIP_UNCOMPRESSED_TOTAL_BYTES // 1024 // 1024}MB cap."
                )
    except zipfile.BadZipFile:
        _reject(f"{file_path.name} claims to be {ext} but is not a valid zip archive.")


def _reject(message: str) -> None:
    logger.warning("Upload validation rejected a file: %s", message)
    raise FileValidationError(message)
