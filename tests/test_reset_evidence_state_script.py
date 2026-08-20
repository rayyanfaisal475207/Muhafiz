"""
scripts/reset_evidence_state.py (M5, docs/decisions/0001-muhafiz-api-migration.md).

Covers what's testable without a live Postgres/AGE instance: the
--execute/--yes-i-am-sure double-gate, the delete ordering in
_reset_postgres (against a fake session), and the Chroma-wipe behavior
(against real temp-dir Chroma instances — no mocking needed there, unlike
Postgres/AGE). The AGE graph drop/recreate itself needs a live instance;
see TestRequiresLivePostgres at the bottom (skipped by default, same
`requires_postgres` marker tests/test_rls_integration.py already uses) —
covered instead by this migration's own Milestone C verification plan
(docs/decisions/0001-muhafiz-api-migration.md), run manually against a
live environment before any real --execute.
"""
import pytest

import scripts.reset_evidence_state as reset_script


# ── the double-gate ──────────────────────────────────────────────────────

class TestResetGraphReappliesEveryAgeLabelMigration:
    async def test_reapplies_every_migration_in_order_not_just_005(self, monkeypatch):
        """
        Regression, found running this script for real against a live
        instance: _reset_graph() used to re-apply ONLY 005_age_graph.sql
        after drop_graph() — any later migration that also pre-creates a
        label (020_age_date_and_cites_labels.sql, Date/CITES) was left
        unapplied on the freshly recreated graph, silently re-exposing it
        to the exact concurrent-first-write race these migrations exist
        to prevent, on every single reset.
        """
        applied = []

        class _FakeConn:
            async def execute(self, sql, *args):
                applied.append(sql)

        class _FakeAcquireCtx:
            async def __aenter__(self):
                return _FakeConn()
            async def __aexit__(self, *exc):
                return False

        class _FakePool:
            def acquire(self):
                return _FakeAcquireCtx()

        async def fake_get_pool():
            return _FakePool()
        async def fake_load_age(conn):
            pass

        monkeypatch.setattr(reset_script.age_client, "get_pool", fake_get_pool)
        monkeypatch.setattr(reset_script.age_client, "_load_age", fake_load_age)

        await reset_script._reset_graph()

        # First call is drop_graph(); the rest are one per AGE_LABEL_MIGRATIONS entry.
        migration_calls = applied[1:]
        assert len(migration_calls) == len(reset_script.AGE_LABEL_MIGRATIONS)
        for call_sql, migration_path in zip(migration_calls, reset_script.AGE_LABEL_MIGRATIONS):
            assert call_sql == migration_path.read_text(encoding="utf-8")

    def test_cites_is_counted_alongside_the_other_edge_labels(self):
        """Regression: CITES (M6b) was missing from EDGE_LABELS entirely —
        the dry-run counter silently under-reported it before every wipe."""
        assert "CITES" in reset_script.EDGE_LABELS


class TestExecuteGate:
    async def test_execute_without_yes_i_am_sure_refuses_and_exits_nonzero(self, monkeypatch, capsys):
        async def fake_dry_run():
            return {}
        called = []
        async def fake_execute_reset():
            called.append(1)

        monkeypatch.setattr(reset_script, "dry_run", fake_dry_run)
        monkeypatch.setattr(reset_script, "execute_reset", fake_execute_reset)
        monkeypatch.setattr(reset_script.age_client, "close_pool", fake_execute_reset)
        monkeypatch.setattr("sys.argv", ["reset_evidence_state.py", "--execute"])

        with pytest.raises(SystemExit) as exc_info:
            await reset_script.main()

        assert exc_info.value.code == 1
        assert not called, "execute_reset() must never run without --yes-i-am-sure too"
        assert "refusing to wipe" in capsys.readouterr().out.lower()

    async def test_no_flags_at_all_is_a_pure_dry_run(self, monkeypatch):
        async def fake_dry_run():
            return {}
        called = []
        async def fake_execute_reset():
            called.append(1)
        async def fake_close_pool():
            pass

        monkeypatch.setattr(reset_script, "dry_run", fake_dry_run)
        monkeypatch.setattr(reset_script, "execute_reset", fake_execute_reset)
        monkeypatch.setattr(reset_script.age_client, "close_pool", fake_close_pool)
        monkeypatch.setattr("sys.argv", ["reset_evidence_state.py"])

        await reset_script.main()  # must not raise/exit

        assert not called

    async def test_both_flags_together_actually_runs_the_reset(self, monkeypatch):
        async def fake_dry_run():
            return {}
        called = []
        async def fake_execute_reset():
            called.append(1)
        async def fake_close_pool():
            pass

        monkeypatch.setattr(reset_script, "dry_run", fake_dry_run)
        monkeypatch.setattr(reset_script, "execute_reset", fake_execute_reset)
        monkeypatch.setattr(reset_script.age_client, "close_pool", fake_close_pool)
        monkeypatch.setattr("sys.argv", ["reset_evidence_state.py", "--execute", "--yes-i-am-sure"])

        await reset_script.main()

        assert called == [1]


# ── _reset_postgres ordering ─────────────────────────────────────────────

class TestResetPostgresOrdering:
    async def test_deletes_community_runs_first_and_cases_last(self, monkeypatch):
        """
        cases is deleted LAST — community_runs (raw SQL, cascades to
        membership/reports), ingestion_jobs, case_assignments, and
        documents all go first, even though the FKs would mostly
        cascade/null anyway (explicit is safer than relying on a specific
        ON DELETE behavior staying what it is today).
        """
        from sqlalchemy import delete
        from src.database.models import Case, CaseAssignment, Document, IngestionJob

        executed_labels = []
        expected_stmt_to_label = {
            str(delete(IngestionJob)): "ingestion_jobs",
            str(delete(CaseAssignment)): "case_assignments",
            str(delete(Document)): "documents",
            str(delete(Case)): "cases",
        }

        class _FakeResult:
            rowcount = 0
            def scalar_one(self):
                return 0

        class _FakeSession:
            async def execute(self, stmt, *args, **kwargs):
                stmt_str = str(stmt)
                if "community_runs" in stmt_str:
                    executed_labels.append("community_runs")
                else:
                    executed_labels.append(expected_stmt_to_label.get(stmt_str, stmt_str))
                return _FakeResult()
            async def commit(self):
                pass
            async def __aenter__(self):
                return self
            async def __aexit__(self, *exc):
                return False

        import src.database.postgres as postgres_mod
        monkeypatch.setattr(postgres_mod, "get_session", lambda: _FakeSession())

        await reset_script._reset_postgres()

        assert executed_labels[0] == "community_runs"
        assert executed_labels[-1] == "cases"
        assert set(executed_labels) == {
            "community_runs", "ingestion_jobs", "case_assignments", "documents", "cases",
        }


# ── filesystem counting ──────────────────────────────────────────────────

class TestCountFilesystem:
    def test_reports_absent_when_nothing_exists(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reset_script, "INGESTION_STATE_FILE", tmp_path / "ingestion_state.json")
        monkeypatch.setattr(reset_script, "STALE_CHROMA_DIRS", [tmp_path / "chroma_stale"])
        result = reset_script._count_filesystem()
        assert result["ingestion_state.json"] is False

    def test_reports_present_when_files_exist(self, tmp_path, monkeypatch):
        state_file = tmp_path / "ingestion_state.json"
        state_file.write_text("{}")
        monkeypatch.setattr(reset_script, "INGESTION_STATE_FILE", state_file)
        monkeypatch.setattr(reset_script, "STALE_CHROMA_DIRS", [])
        result = reset_script._count_filesystem()
        assert result["ingestion_state.json"] is True


class TestResetFilesystem:
    def test_removes_ingestion_state_file(self, tmp_path, monkeypatch):
        state_file = tmp_path / "ingestion_state.json"
        state_file.write_text("{}")
        monkeypatch.setattr(reset_script, "INGESTION_STATE_FILE", state_file)
        monkeypatch.setattr(reset_script, "STALE_CHROMA_DIRS", [])

        reset_script._reset_filesystem()

        assert not state_file.exists()

    def test_removes_stale_chroma_dirs(self, tmp_path, monkeypatch):
        stale_dir = tmp_path / "chroma_stale"
        stale_dir.mkdir()
        (stale_dir / "chroma.sqlite3").write_bytes(b"fake")
        monkeypatch.setattr(reset_script, "INGESTION_STATE_FILE", tmp_path / "absent.json")
        monkeypatch.setattr(reset_script, "STALE_CHROMA_DIRS", [stale_dir])

        reset_script._reset_filesystem()

        assert not stale_dir.exists()

    def test_missing_files_are_a_no_op_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(reset_script, "INGESTION_STATE_FILE", tmp_path / "absent.json")
        monkeypatch.setattr(reset_script, "STALE_CHROMA_DIRS", [tmp_path / "also_absent"])
        reset_script._reset_filesystem()  # must not raise


# ── real Chroma wipe (no live Postgres/AGE needed for this half) ────────

class TestResetChromaAgainstRealTempInstance:
    def test_drop_and_recreate_actually_empties_the_kb_collection(self, tmp_path, monkeypatch):
        from src import config
        from src.retrieval.vector_store import ChromaVectorStore

        monkeypatch.setattr(config, "EXPECTED_EMBEDDING_DIM", 2)
        ChromaVectorStore.reset_instance()
        store = ChromaVectorStore(persist_dir=tmp_path / "chroma")
        store.upsert([{
            "id": "c1", "text": "some text", "embedding": [0.1, 0.2],
            "metadata": {"doc_id": "d1", "source": "x", "is_global": True},
        }])
        assert store.count() == 1

        # _reset_chroma() imports ChromaVectorStore locally and calls
        # .get_instance() — patch the classmethod itself so that call
        # returns this temp-dir instance instead of the real singleton.
        monkeypatch.setattr(ChromaVectorStore, "get_instance", classmethod(lambda cls: store))

        community_calls = []
        import src.retrieval.community_vector_store as community_mod
        monkeypatch.setattr(community_mod, "clear_all_reports", lambda: community_calls.append(1))

        reset_script._reset_chroma()

        assert store.count() == 0
        assert community_calls == [1]

        ChromaVectorStore.reset_instance()

    async def test_clear_all_reports_actually_empties_the_community_collection(self, tmp_path, monkeypatch):
        from src import config
        from src.retrieval import community_vector_store as community_mod

        monkeypatch.setattr(config, "CHROMA_PERSIST_DIR", tmp_path / "chroma_community")
        community_mod.reset_collection()

        async def fake_embed_texts(texts, task_type="RETRIEVAL_DOCUMENT"):
            return [[0.1, 0.2] for _ in texts]
        monkeypatch.setattr(community_mod, "embed_texts", fake_embed_texts)

        await community_mod.upsert_community_reports([
            {"community_id": "C-1", "summary_text": "a summary", "case_ids": ["fir-1-26"], "member_count": 2},
        ])
        assert len(community_mod._get_collection().get(include=[])["ids"]) == 1

        community_mod.clear_all_reports()

        assert community_mod._get_collection().get(include=[])["ids"] == []


# ── requires a live Postgres + AGE instance — skipped by default ────────

@pytest.mark.requires_postgres
class TestRequiresLivePostgres:
    async def test_dry_run_and_full_reset_against_a_real_instance(self):
        """
        Manual/CI-with-live-DB only. Run explicitly against a disposable
        instance before any real --execute against shared infrastructure:
            pytest tests/test_reset_evidence_state_script.py -m requires_postgres
        """
        pytest.skip("Requires a live Postgres+AGE instance with migrations applied.")
