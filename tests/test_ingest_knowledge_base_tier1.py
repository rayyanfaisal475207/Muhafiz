"""
Tests for scripts/ingest_knowledge_base_tier1.py's pure helpers:
discover_files() and summarize(). No subprocess, no asyncio, no Chroma/DB —
same isolation style as tests/test_kb_stats_documents.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from ingest_knowledge_base_tier1 import discover_files, summarize


def test_discover_files_excludes_readme_and_sorts_deterministically(tmp_path):
    (tmp_path / "3_third.pdf").write_bytes(b"%PDF-fake")
    (tmp_path / "1_first.pdf").write_bytes(b"%PDF-fake")
    (tmp_path / "2_second.pdf").write_bytes(b"%PDF-fake")
    (tmp_path / "README.md").write_text("selection rationale")

    found = discover_files(tmp_path)

    assert [f.name for f in found] == ["1_first.pdf", "2_second.pdf", "3_third.pdf"]


def test_discover_files_readme_exclusion_is_case_insensitive(tmp_path):
    (tmp_path / "readme.MD").write_text("x")
    (tmp_path / "1_doc.pdf").write_bytes(b"%PDF-fake")

    found = discover_files(tmp_path)

    assert [f.name for f in found] == ["1_doc.pdf"]


def test_discover_files_missing_directory_returns_empty_not_an_error():
    found = discover_files(Path("this/directory/does/not/exist"))

    assert found == []


def test_summarize_splits_succeeded_and_failed():
    results = [
        ("a.pdf", True, "OK: 12 chunks"),
        ("b.pdf", False, "ERROR: No text could be extracted from this file."),
        ("c.pdf", True, "OK: 30 chunks"),
    ]

    summary = summarize(results)

    assert summary["total"] == 3
    assert summary["succeeded"] == ["a.pdf", "c.pdf"]
    assert summary["failed"] == [("b.pdf", "ERROR: No text could be extracted from this file.")]


def test_summarize_all_succeeded_has_empty_failed_list():
    results = [("a.pdf", True, "OK: 5 chunks")]

    summary = summarize(results)

    assert summary["failed"] == []
