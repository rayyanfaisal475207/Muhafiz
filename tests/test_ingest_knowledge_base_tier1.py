"""
Tests for scripts/ingest_knowledge_base_tier1.py's pure helpers:
discover_files() and summarize(). No subprocess, no asyncio, no Chroma/DB —
same isolation style as tests/test_kb_stats_documents.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from ingest_knowledge_base_tier1 import discover_files, summarize, already_ingested_filenames, safe_print


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


def test_already_ingested_filenames_only_counts_the_kb_category():
    metas = [
        {"source": "1_crpc.pdf", "category": "legal_procedural_reference"},
        {"source": "1_crpc.pdf", "category": "legal_procedural_reference"},  # second chunk, same file
        {"source": "2_qso.pdf", "category": "legal_procedural_reference"},
        {"source": "some_case_evidence.pdf", "category": None},
        {"source": "unrelated_global_doc.pdf", "category": "penal_code"},
    ]

    done = already_ingested_filenames(metas)

    assert done == {"1_crpc.pdf", "2_qso.pdf"}


def test_already_ingested_filenames_empty_store_returns_empty_set():
    assert already_ingested_filenames([]) == set()


def test_already_ingested_filenames_ignores_entries_missing_source():
    metas = [{"category": "legal_procedural_reference"}]  # no "source" key

    assert already_ingested_filenames(metas) == set()


def test_safe_print_never_raises_on_unencodable_text(monkeypatch, capsys):
    """
    Guards the incident this exists for: printing worker output containing a
    character the console codec can't encode (U+FFFD from OCR/vision output)
    crashed the whole batch script mid-run with UnicodeEncodeError, silently
    skipping every file after the one that triggered it. safe_print() must
    never propagate that exception, on any input.
    """
    # Simulate a strict-codec stdout the way Windows' cp1252 console behaves,
    # regardless of what encoding this test suite actually runs under.
    import io

    class StrictAsciiWriter(io.TextIOBase):
        def write(self, s):
            s.encode("ascii")  # raises UnicodeEncodeError on anything non-ASCII
            return len(s)

    monkeypatch.setattr("builtins.print", lambda *a, **k: StrictAsciiWriter().write(a[0] if a else ""))

    safe_print("plain ascii text")
    safe_print("has a replacement char: �")
    safe_print("has Urdu: القانون")
    # No assertion needed beyond "did not raise" — that's the whole guarantee.
