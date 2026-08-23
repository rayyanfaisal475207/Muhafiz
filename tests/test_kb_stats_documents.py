"""
Tests for src/data_gateway/direct_backend.py's _kb_stats_documents() — the
pure helper behind GET /api/admin/kb/stats's `documents`/`total_documents`
fields. Pure function, no Chroma/DB — same isolation style as
tests/test_ingestion_quality.py's pure-counting tests.

Guards a real bug found during a full end-to-end test pass: a freshly
uploaded, small document never appeared in the Knowledge Base page's
document list, and `total_documents` silently capped at 500 while
reading as a true total.
"""
from collections import Counter
from types import SimpleNamespace

from src.data_gateway.direct_backend import _kb_stats_documents


def _doc(doc_type, ingested_at):
    return SimpleNamespace(doc_type=doc_type, ingested_at=ingested_at)


class _FakeDatetime:
    """Minimal stand-in with an .isoformat(), matching what get_kb_stats()
    actually calls on a real SQLAlchemy datetime column."""
    def __init__(self, iso: str):
        self._iso = iso

    def isoformat(self):
        return self._iso


def test_most_recently_ingested_document_appears_ahead_of_a_high_chunk_count_older_one():
    """The bug's exact shape: a big old document (many chunks) must not
    crowd out a small new one (few chunks) — recency wins, not chunk count."""
    counts = Counter({"old-fir-with-lots-of-chunks": 50, "new-small-upload.txt": 1})
    doc_by_filename = {
        "old-fir-with-lots-of-chunks": _doc("fir_narrative", _FakeDatetime("2026-01-01T00:00:00")),
        "new-small-upload.txt": _doc("txt", _FakeDatetime("2026-08-23T14:46:13")),
    }

    total, docs = _kb_stats_documents(counts, doc_by_filename)

    assert total == 2
    assert docs[0]["doc_id"] == "new-small-upload.txt", "the newer, smaller document must sort first"
    assert docs[1]["doc_id"] == "old-fir-with-lots-of-chunks"


def test_total_documents_is_the_true_count_not_capped_at_500():
    """Guards the mislabeling half of the bug — total_documents must
    reflect every source, even ones the 500-cap on `documents` trims."""
    counts = Counter({f"doc-{i}": 1 for i in range(600)})
    doc_by_filename = {}

    total, docs = _kb_stats_documents(counts, doc_by_filename)

    assert total == 600
    assert len(docs) == 500, "the returned list is still capped for response-size sanity"


def test_a_document_with_no_matching_row_sorts_as_oldest_not_first():
    counts = Counter({"orphan-chunk-source": 3, "known-doc": 2})
    doc_by_filename = {
        "known-doc": _doc("pdf", _FakeDatetime("2020-01-01T00:00:00")),
        # "orphan-chunk-source" has no row in `documents` at all.
    }

    total, docs = _kb_stats_documents(counts, doc_by_filename)

    assert total == 2
    assert docs[0]["doc_id"] == "known-doc", "a document with a real, even old, timestamp outranks one with none"
    assert docs[1]["doc_id"] == "orphan-chunk-source"
    assert docs[1]["ingested_at"] is None


def test_empty_corpus_returns_zero_and_empty_list():
    total, docs = _kb_stats_documents(Counter(), {})
    assert total == 0
    assert docs == []


def test_chunk_count_and_doc_type_are_preserved_correctly():
    counts = Counter({"report.pdf": 7})
    doc_by_filename = {"report.pdf": _doc("pdf", _FakeDatetime("2026-05-01T00:00:00"))}

    total, docs = _kb_stats_documents(counts, doc_by_filename)

    assert docs[0]["chunk_count"] == 7
    assert docs[0]["doc_type"] == "pdf"
    assert docs[0]["is_global"] is True
