"""
Tests for src/graph/ingestion_quality.py (Ingestion Quality Control at
Scale, Module G1).

get_session is monkeypatched with a fake session — no real Postgres
(matches the `no_network` guard, conftest, autouse), same pattern as
tests/test_identity_index.py. Pure counting, no LLM, no graph writes —
this module structurally cannot confirm/reject an entity match.
"""
import pytest

import src.graph.ingestion_quality as ingestion_quality


class _FakeSession:
    def __init__(self, raise_on_execute=False):
        self.executed: list[tuple] = []
        self._raise = raise_on_execute

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        if self._raise:
            raise RuntimeError("simulated Postgres failure")

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_get_session(session):
    def _factory():
        return session
    return _factory


@pytest.fixture(autouse=True)
def _reset_current_run():
    """Every test starts with no run tracked, regardless of test order."""
    ingestion_quality._current_run.set(None)
    yield
    ingestion_quality._current_run.set(None)


# ── record_*() are no-ops with no active run ─────────────────────────────

def test_record_tier_is_a_noop_with_no_active_run():
    ingestion_quality.record_tier("cnic_auto")  # must not raise
    assert ingestion_quality._current_run.get() is None


def test_record_new_tier_from_gate_is_a_noop_with_no_active_run():
    ingestion_quality.record_new_tier_from_gate(True)  # must not raise


def test_record_extraction_error_is_a_noop_with_no_active_run():
    ingestion_quality.record_extraction_error()  # must not raise


# ── start_run() / record_*() / finish_run() ──────────────────────────────

async def test_start_run_writes_the_starting_row(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(ingestion_quality, "get_session", _fake_get_session(session))

    await ingestion_quality.start_run("run-1", "ingest_file", case_id="CASE-A")

    assert len(session.executed) == 1
    sql, params = session.executed[0]
    assert "INSERT INTO ingestion_run_quality" in sql
    assert params == {"run_id": "run-1", "source": "ingest_file", "case_id": "CASE-A"}


async def test_start_run_sets_a_fresh_zeroed_accumulator(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(ingestion_quality, "get_session", _fake_get_session(session))

    await ingestion_quality.start_run("run-1", "ingest_file")

    acc = ingestion_quality._current_run.get()
    assert acc == {
        "tier_cnic_auto": 0, "tier_flagged_unverified": 0, "tier_human_review": 0, "tier_new": 0,
        "corroboration_gate_rejections": 0, "extraction_errors": 0,
    }


async def test_start_run_failure_is_swallowed_but_accumulator_still_set(monkeypatch):
    session = _FakeSession(raise_on_execute=True)
    monkeypatch.setattr(ingestion_quality, "get_session", _fake_get_session(session))

    await ingestion_quality.start_run("run-1", "ingest_file")  # must not raise

    assert ingestion_quality._current_run.get() is not None


async def test_record_tier_increments_the_right_column(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(ingestion_quality, "get_session", _fake_get_session(session))
    await ingestion_quality.start_run("run-1", "ingest_file")

    ingestion_quality.record_tier("cnic_auto")
    ingestion_quality.record_tier("cnic_auto")
    ingestion_quality.record_tier("flagged_unverified")
    ingestion_quality.record_tier("human_review")
    ingestion_quality.record_tier("new")

    acc = ingestion_quality._current_run.get()
    assert acc["tier_cnic_auto"] == 2
    assert acc["tier_flagged_unverified"] == 1
    assert acc["tier_human_review"] == 1
    assert acc["tier_new"] == 1


async def test_record_tier_ignores_an_unrecognized_tier_string(monkeypatch):
    """Undercounts rather than raises — defends against a future new tier landing in
    entity_resolution.py before this table's schema is extended to match."""
    session = _FakeSession()
    monkeypatch.setattr(ingestion_quality, "get_session", _fake_get_session(session))
    await ingestion_quality.start_run("run-1", "ingest_file")

    ingestion_quality.record_tier("some_future_tier")  # must not raise, must not KeyError

    acc = ingestion_quality._current_run.get()
    assert sum(acc.values()) == 0


async def test_record_new_tier_from_gate_true_counts_both_new_and_rejection(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(ingestion_quality, "get_session", _fake_get_session(session))
    await ingestion_quality.start_run("run-1", "ingest_file")

    ingestion_quality.record_new_tier_from_gate(True)

    acc = ingestion_quality._current_run.get()
    assert acc["tier_new"] == 1
    assert acc["corroboration_gate_rejections"] == 1


async def test_record_new_tier_from_gate_false_counts_only_new(monkeypatch):
    """Fallback administratively disabled is NOT a gate rejection — must not inflate that count."""
    session = _FakeSession()
    monkeypatch.setattr(ingestion_quality, "get_session", _fake_get_session(session))
    await ingestion_quality.start_run("run-1", "ingest_file")

    ingestion_quality.record_new_tier_from_gate(False)

    acc = ingestion_quality._current_run.get()
    assert acc["tier_new"] == 1
    assert acc["corroboration_gate_rejections"] == 0


async def test_record_extraction_error_increments(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(ingestion_quality, "get_session", _fake_get_session(session))
    await ingestion_quality.start_run("run-1", "ingest_file")

    ingestion_quality.record_extraction_error()
    ingestion_quality.record_extraction_error()

    assert ingestion_quality._current_run.get()["extraction_errors"] == 2


async def test_finish_run_flushes_counts_and_clears_the_accumulator(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(ingestion_quality, "get_session", _fake_get_session(session))
    await ingestion_quality.start_run("run-1", "ingest_file")
    ingestion_quality.record_tier("cnic_auto")

    result = await ingestion_quality.finish_run("run-1")

    assert result["tier_cnic_auto"] == 1
    assert ingestion_quality._current_run.get() is None
    update_calls = [c for c in session.executed if "UPDATE ingestion_run_quality" in c[0]]
    assert len(update_calls) == 1
    _, params = update_calls[0]
    assert params["run_id"] == "run-1"
    assert params["tier_cnic_auto"] == 1


async def test_finish_run_with_no_active_run_returns_none(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(ingestion_quality, "get_session", _fake_get_session(session))

    result = await ingestion_quality.finish_run("run-that-was-never-started")

    assert result is None
    assert session.executed == []


async def test_finish_run_failure_is_swallowed_and_accumulator_still_cleared(monkeypatch):
    session = _FakeSession(raise_on_execute=True)
    monkeypatch.setattr(ingestion_quality, "get_session", _fake_get_session(session))
    await ingestion_quality.start_run("run-1", "ingest_file")
    # start_run's own INSERT already "raised" and was swallowed; reset the
    # flag isn't possible mid-session-object, so use a fresh raising session
    # for the finish_run call specifically.
    session2 = _FakeSession(raise_on_execute=True)
    monkeypatch.setattr(ingestion_quality, "get_session", _fake_get_session(session2))

    result = await ingestion_quality.finish_run("run-1")  # must not raise

    assert result is not None  # counts are still returned even though the write failed
    assert ingestion_quality._current_run.get() is None


# ── track_run() context manager ───────────────────────────────────────────

async def test_track_run_starts_and_finishes_around_the_body(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(ingestion_quality, "get_session", _fake_get_session(session))

    async with ingestion_quality.track_run("run-1", "ingest_file", case_id="CASE-A"):
        ingestion_quality.record_tier("cnic_auto")
        assert ingestion_quality._current_run.get()["tier_cnic_auto"] == 1

    assert ingestion_quality._current_run.get() is None
    assert any("INSERT INTO ingestion_run_quality" in c[0] for c in session.executed)
    assert any("UPDATE ingestion_run_quality" in c[0] for c in session.executed)


async def test_track_run_still_finishes_when_the_body_raises(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(ingestion_quality, "get_session", _fake_get_session(session))

    with pytest.raises(ValueError):
        async with ingestion_quality.track_run("run-1", "ingest_file"):
            ingestion_quality.record_tier("human_review")
            raise ValueError("simulated failure mid-run")

    # finish_run() still ran (in the `finally`), so the accumulator was cleared
    # and the partial count (1 human_review) was still flushed, not lost.
    assert ingestion_quality._current_run.get() is None
    update_calls = [c for c in session.executed if "UPDATE ingestion_run_quality" in c[0]]
    assert update_calls[0][1]["tier_human_review"] == 1
