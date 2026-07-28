"""
Phase 7, Module 7.2 — upload validation and size caps.

Covers src/ingestion/validation.py::validate_file (size cap, magic-byte/
claimed-extension match, zip decompression-ratio guard) and its wiring
into loader_router.route_and_load(), the single chokepoint every
ingestion entry point (admin upload, ingest_directory, ingest_file) goes
through.
"""
import io
import zipfile

import pytest

from src import config
from src.ingestion import loader_router
from src.ingestion.validation import FileValidationError, validate_file


class TestSizeCap:
    def test_file_exceeding_cap_is_rejected(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "MAX_UPLOAD_SIZE_MB", 1)
        big = tmp_path / "big.txt"
        big.write_bytes(b"x" * (2 * 1024 * 1024))

        with pytest.raises(FileValidationError, match="limit is 1MB"):
            validate_file(big)

    def test_file_under_cap_passes(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "MAX_UPLOAD_SIZE_MB", 1)
        small = tmp_path / "small.txt"
        small.write_bytes(b"x" * 100)

        validate_file(small)  # must not raise

    def test_size_cap_enforced_via_route_and_load_not_just_admin_endpoint(self, monkeypatch, tmp_path):
        """The whole point of Module 7.2: the cap must live in the shared
        path (route_and_load), not just admin.py's HTTP endpoint — so
        ingest_directory/ingest_file/any future caller is covered too."""
        monkeypatch.setattr(config, "MAX_UPLOAD_SIZE_MB", 1)
        big = tmp_path / "big.txt"
        big.write_bytes(b"x" * (2 * 1024 * 1024))

        with pytest.raises(ValueError, match="limit is 1MB"):
            loader_router.route_and_load(big)


class TestMagicBytes:
    def test_pdf_named_file_with_wrong_magic_bytes_is_rejected(self, tmp_path):
        fake_pdf = tmp_path / "evidence.pdf"
        fake_pdf.write_bytes(b"MZ\x90\x00this is actually an exe")  # PE header

        with pytest.raises(FileValidationError, match="doesn't match a .pdf signature"):
            validate_file(fake_pdf)

    def test_real_pdf_signature_passes(self, tmp_path):
        real_pdf = tmp_path / "evidence.pdf"
        real_pdf.write_bytes(b"%PDF-1.4\n%fake but correctly-signed content")

        validate_file(real_pdf)  # must not raise

    def test_png_named_file_with_wrong_magic_bytes_is_rejected(self, tmp_path):
        fake_png = tmp_path / "photo.png"
        fake_png.write_bytes(b"not a png at all")

        with pytest.raises(FileValidationError, match="doesn't match a .png signature"):
            validate_file(fake_png)

    def test_real_png_signature_passes(self, tmp_path):
        real_png = tmp_path / "photo.png"
        real_png.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 20)

        validate_file(real_png)  # must not raise

    def test_webp_signature_checked_at_correct_offset(self, tmp_path):
        real_webp = tmp_path / "photo.webp"
        real_webp.write_bytes(b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"\x00" * 10)
        validate_file(real_webp)  # must not raise

        fake_webp = tmp_path / "fake.webp"
        fake_webp.write_bytes(b"not riff webp content")
        with pytest.raises(FileValidationError, match="WEBP signature"):
            validate_file(fake_webp)

    def test_plain_text_formats_have_no_magic_byte_check(self, tmp_path):
        """.txt/.md/.csv/.html have no reliable signature — validate_file
        must not reject them on content grounds, only size/nothing else."""
        for ext in (".txt", ".md", ".csv", ".html", ".htm"):
            f = tmp_path / f"doc{ext}"
            f.write_bytes(b"anything at all, no fixed signature to check")
            validate_file(f)  # must not raise

    def test_legacy_xls_has_no_magic_byte_check(self, tmp_path):
        f = tmp_path / "sheet.xls"
        f.write_bytes(b"whatever bytes")
        validate_file(f)  # must not raise (ambiguous OLE2 format, intentionally skipped)


class TestZipBomb:
    def _make_zip(self, path, member_name="data.txt", uncompressed_size=None, content=None):
        if content is None:
            content = b"A" * uncompressed_size
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.writestr(member_name, content)

    def test_extreme_compression_ratio_rejected_before_full_decompression(self, monkeypatch, tmp_path):
        monkeypatch.setattr(
            __import__("src.ingestion.validation", fromlist=["_MAX_ZIP_RATIO"]),
            "_MAX_ZIP_RATIO", 10,
        )
        # Highly compressible content (all zeros) gives a large ratio at a
        # size small enough to build safely inside a test.
        bomb = tmp_path / "bomb.docx"
        self._make_zip(bomb, content=b"\x00" * (5 * 1024 * 1024))  # 5MB of zeros

        with pytest.raises(FileValidationError, match="compression ratio"):
            validate_file(bomb)

    def test_normal_docx_zip_passes(self, tmp_path):
        normal = tmp_path / "normal.docx"
        self._make_zip(normal, content=b"Not very compressible content 12345 !@#$%")

        validate_file(normal)  # must not raise

    def test_corrupted_zip_named_docx_is_rejected(self, tmp_path):
        fake = tmp_path / "corrupt.docx"
        fake.write_bytes(b"PK\x03\x04" + b"this is not a real zip structure")

        with pytest.raises(FileValidationError, match="not a valid zip archive"):
            validate_file(fake)

    def test_total_uncompressed_size_cap_enforced_across_many_members(self, monkeypatch, tmp_path):
        val_mod = __import__("src.ingestion.validation", fromlist=["_MAX_ZIP_UNCOMPRESSED_TOTAL_BYTES"])
        monkeypatch.setattr(val_mod, "_MAX_ZIP_UNCOMPRESSED_TOTAL_BYTES", 1024)
        monkeypatch.setattr(val_mod, "_MAX_ZIP_RATIO", 10_000_000)  # neutralize per-member check

        many = tmp_path / "many_members.xlsx"
        with zipfile.ZipFile(many, "w", zipfile.ZIP_DEFLATED) as zf:
            for i in range(5):
                zf.writestr(f"part{i}.xml", b"B" * 500)  # 5 * 500 = 2500 > 1024 cap

        with pytest.raises(FileValidationError, match="exceeds the"):
            validate_file(many)

    def test_non_zip_extensions_skip_zip_bomb_check(self, tmp_path):
        f = tmp_path / "plain.pdf"
        f.write_bytes(b"%PDF-1.4 normal small pdf")
        validate_file(f)  # must not raise — zip-bomb check is docx/xlsx-only


class TestRouteAndLoadIntegration:
    def test_route_and_load_rejects_bad_magic_bytes_before_calling_the_loader(self, monkeypatch, tmp_path):
        called = []
        monkeypatch.setitem(loader_router.LOADER_MAP, ".pdf", lambda p: called.append(p) or [])

        fake_pdf = tmp_path / "evidence.pdf"
        fake_pdf.write_bytes(b"not a pdf")

        with pytest.raises(ValueError, match="doesn't match a .pdf signature"):
            loader_router.route_and_load(fake_pdf)

        assert called == [], "the loader must never be invoked on a file that failed validation"
