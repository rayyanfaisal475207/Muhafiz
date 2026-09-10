# -*- coding: utf-8 -*-
"""
Module 37 — tests for scripts/cleanup_orphaned_fulltext_rows.py and for the
two findings that explain WHY the orphans exist.

No Postgres and no Chroma: the backup/restore round trip is exercised on the
real functions with a temp file, the safety guard is exercised by injecting a
fake store, and the two recurrence findings are pinned by reading the source
of the code paths that cause them.

Run:
    PYTHONPATH=. python -m pytest tests/test_module37_orphan_cleanup.py -p no:randomly
"""
from __future__ import annotations

import gzip
import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SPEC = importlib.util.spec_from_file_location(
    "module37_cleanup", os.path.join(HERE, "scripts", "cleanup_orphaned_fulltext_rows.py")
)
cleanup = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(cleanup)


def _row(cid: str, text: str = "some statute text") -> dict:
    return {
        "chunk_id": cid,
        "doc_id": cid.rsplit("_c", 1)[0],
        "source": "1_1898_Code_of_Criminal_Procedure_(Pakistan).pdf",
        "project_id": None,
        "case_id": None,
        "is_global": True,
        "text": text,
        "metadata": json.dumps({"source": "x", "chunk_index": 0}),
        "updated_at": "2026-09-05T07:31:34.204413+00:00",
    }


# ── The backup, which is what makes the deletion reversible ────────────────


def test_backup_round_trips_every_column_the_table_has(tmp_path):
    """`tsv` is deliberately absent — it is derived, and restore_rows()
    recomputes it with the same expression fulltext_index.maintain() uses.
    Every other column must survive, or the deletion is not reversible."""
    path = str(tmp_path / "backup.jsonl.gz")
    rows = [_row("a_c1"), _row("a_c2", "اردو متن")]
    cleanup.write_backup(path, rows)
    back = cleanup.read_backup(path)
    assert back == sorted(rows, key=lambda r: r["chunk_id"])
    for col in cleanup.COLUMNS:
        assert col in back[0]
    assert "tsv" not in back[0]


def test_backup_is_gzipped_and_utf8(tmp_path):
    path = str(tmp_path / "backup.jsonl.gz")
    cleanup.write_backup(path, [_row("a_c1", "اردو متن")])
    with gzip.open(path, "rt", encoding="utf-8") as fh:
        assert "اردو متن" in fh.read()


def test_a_second_cleanup_never_drops_the_first_cleanups_rows(tmp_path):
    """write_backup() union-merges on chunk_id. A later run that finds a
    different orphan set must not truncate the file the earlier one wrote —
    otherwise the first deletion silently stops being reversible."""
    path = str(tmp_path / "backup.jsonl.gz")
    cleanup.write_backup(path, [_row("a_c1")])
    total = cleanup.write_backup(path, [_row("b_c1")])
    assert total == 2
    assert {r["chunk_id"] for r in cleanup.read_backup(path)} == {"a_c1", "b_c1"}


def test_a_rewritten_row_replaces_rather_than_duplicates(tmp_path):
    path = str(tmp_path / "backup.jsonl.gz")
    cleanup.write_backup(path, [_row("a_c1", "old")])
    cleanup.write_backup(path, [_row("a_c1", "new")])
    back = cleanup.read_backup(path)
    assert len(back) == 1 and back[0]["text"] == "new"


# ── The guard that stops this script emptying the whole BM25 index ─────────


def test_it_refuses_to_run_when_chroma_looks_empty(monkeypatch):
    """CHROMA_PERSIST_DIR is RELATIVE in .env (`./data/chroma_db`), so running
    this from a git worktree opens a NEW, EMPTY store — at which point every
    single chunk_fulltext row is 'absent from Chroma'. Without this guard the
    script would back up and delete the entire index."""

    class _Empty:
        def get(self, include=None):
            return {"ids": []}

    class _Store:
        _collection = _Empty()

        @classmethod
        def get_instance(cls):
            return cls()

    import sys
    import types

    mod = types.ModuleType("src.retrieval.vector_store")
    mod.ChromaVectorStore = _Store
    monkeypatch.setitem(sys.modules, "src.retrieval.vector_store", mod)
    with pytest.raises(SystemExit) as exc:
        cleanup.chroma_ids()
    assert "REFUSING TO RUN" in str(exc.value)


def test_the_guard_threshold_is_below_the_real_corpus(monkeypatch):
    """7,716 documents were in muhafiz_kb when this was written; the guard must
    not be so high that a legitimately smaller deployment cannot run it."""
    assert 0 < cleanup.MIN_CHROMA_DOCS < 7716


# ── Why the orphans exist. These are FINDINGS, not guards. ─────────────────
#
# Module 37's deletion is a CLEANUP, not a fix: nothing in the code prevents
# the rows coming back. Both tests below assert the current, defective
# behaviour, so that closing either defect (139 / 140) fails here and whoever
# closes it is pointed at this module's result file.


def _src(rel: str) -> str:
    with open(os.path.join(HERE, rel), encoding="utf-8") as fh:
        return fh.read()


def test_module37_defect_139_resetting_chroma_does_not_clear_chunk_fulltext():
    """`drop_and_recreate()` empties the Chroma collection; `chunk_fulltext` is
    untouched, so EVERY row in the BM25 index becomes an orphan. Same for
    scripts/reset_evidence_state.py, which enumerates the Postgres tables it
    clears and does not list chunk_fulltext. Filed as defect 139 —
    docs/gold-qa-wave2-results/MODULE37_RESULT.md §5."""
    vs = _src("src/retrieval/vector_store.py")
    body = vs.split("def drop_and_recreate", 1)[1].split("\n    def ", 1)[0]
    assert "fulltext_index" not in body
    reset = _src("scripts/reset_evidence_state.py")
    assert "chunk_fulltext" not in reset


def test_module37_defect_140_reingest_never_deletes_the_previous_ingestions_rows():
    """`Document._generate_id()` hashes the first 200 characters of the
    extracted text, so a change to extraction or chunking yields a DIFFERENT
    doc_id — and `upsert_documents()` only ever calls `fulltext_index.maintain()`
    per new chunk. The previous ingestion's rows are never deleted; they simply
    stop resolving in Chroma. This is exactly what produced the CrPC's 2,243
    `…_0519abd8_` orphans. Filed as defect 140."""
    doc = _src("src/ingestion/document.py")
    assert "self.text[:200]" in doc
    vs = _src("src/retrieval/vector_store.py")
    upsert = vs.split("async def upsert_documents", 1)[1]
    assert "fulltext_index.maintain" in upsert
    assert "fulltext_index.delete_by_source" not in upsert
    # `upsert_documents()` DOES call `fulltext_index.delete_by_ids()`, which
    # looks like the missing cleanup but is not: it lives in the `except`
    # branch that compensates a FAILED Postgres write by rolling the
    # just-written Chroma chunks back out. It only ever names the ids this
    # call itself just inserted, never the previous ingestion's, so it cannot
    # retire a superseded doc_id. Pinned so the distinction is not lost.
    rollback = upsert.split("except Exception:", 1)[1]
    assert "fulltext_index.delete_by_ids" in rollback
    assert "fulltext_index.delete_by_ids" not in upsert.split("except Exception:", 1)[0]


def test_module37_the_admin_delete_route_is_the_only_path_that_stays_in_sync():
    """The one place that does the right thing, pinned so it is not lost."""
    admin = _src("src/api/admin.py")
    route = admin.split("async def delete_kb_document", 1)[1].split("\n@router", 1)[0]
    assert "delete_by_source" in route
    assert "fulltext_index.delete_by_source" in route
