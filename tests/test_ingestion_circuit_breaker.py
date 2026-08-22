"""
Tests for src/graph/ingestion_circuit_breaker.py (Ingestion Quality
Control at Scale, Module G2).

get_session is monkeypatched with a fake session/result set — no real
Postgres, same pattern as tests/test_ingestion_quality.py. Structurally
incapable of touching any graph edge/node: this module only reads/writes
Postgres's ingestion_run_quality table, same "cannot do the risky thing
even by mistake" discipline candidate_reprioritization.py established for
D1 (never even imports src.graph.versioning or age_client).
"""
import pytest

import src.graph.ingestion_circuit_breaker as breaker


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Row:
    """Mimics SQLAlchemy's Row — a `._mapping` that supports dict(row._mapping)."""
    def __init__(self, d):
        self._mapping = d


class _FakeSession:
    def __init__(self, query_results: list):
        # A list of row-lists, one per expected execute() call, consumed in order.
        self._query_results = list(query_results)
        self.executed: list[tuple] = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        rows = self._query_results.pop(0) if self._query_results else []
        return _FakeResult([_Row(r) for r in rows])

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


_ZERO_COUNTS = {
    "tier_cnic_auto": 0, "tier_flagged_unverified": 0, "tier_human_review": 0,
    "tier_new": 0, "corroboration_gate_rejections": 0,
}


def _counts(**overrides):
    return {**_ZERO_COUNTS, **overrides}


# ── rate helpers ──────────────────────────────────────────────────────────

def test_ambiguous_rate_is_none_for_a_zero_mention_run():
    assert breaker._ambiguous_rate(_counts()) is None


def test_ambiguous_rate_computed_over_all_four_tiers():
    counts = _counts(tier_cnic_auto=6, tier_flagged_unverified=2, tier_human_review=2)
    assert breaker._ambiguous_rate(counts) == pytest.approx(0.4)


def test_gate_rejection_rate_is_none_when_no_tier_new_mentions():
    assert breaker._gate_rejection_rate(_counts()) is None


def test_gate_rejection_rate_uses_tier_new_as_denominator_not_total():
    counts = _counts(tier_cnic_auto=90, tier_new=10, corroboration_gate_rejections=5)
    assert breaker._gate_rejection_rate(counts) == pytest.approx(0.5)


# ── check_and_flag(): propagation from an unacknowledged prior run ───────

async def test_flag_propagates_from_an_unacknowledged_prior_run(monkeypatch):
    prior_row = {"run_id": "run-0", "flagged_for_review": True, "flagged_reason": "old problem"}
    session = _FakeSession([[prior_row]])
    monkeypatch.setattr(breaker, "get_session", _fake_get_session(session))

    result = await breaker.check_and_flag("run-1", "sync_muhafiz_data", _counts(tier_cnic_auto=100))

    assert result["flagged"] is True
    assert "run-0" in result["reason"]
    assert "not been acknowledged" in result["reason"]
    # The propagation path writes the flag and never even queries history —
    # an unacknowledged prior problem is decisive on its own.
    update_calls = [c for c in session.executed if "UPDATE ingestion_run_quality" in c[0]]
    assert len(update_calls) == 1
    assert update_calls[0][1]["run_id"] == "run-1"


async def test_no_propagation_when_prior_run_was_not_flagged(monkeypatch):
    prior_row = {"run_id": "run-0", "flagged_for_review": False, "flagged_reason": None}
    # No propagation -> falls through to history query -> insufficient (0 rows).
    session = _FakeSession([[prior_row], []])
    monkeypatch.setattr(breaker, "get_session", _fake_get_session(session))

    result = await breaker.check_and_flag("run-1", "sync_muhafiz_data", _counts(tier_cnic_auto=100))

    assert result["flagged"] is False
    assert "insufficient baseline history" in result["reason"]


# ── check_and_flag(): baseline threshold ─────────────────────────────────

async def test_insufficient_history_never_flags(monkeypatch):
    """Fewer than MIN_BASELINE_RUNS prior runs -> skipped, not treated as normal."""
    history = [_counts(tier_cnic_auto=100)] * (breaker.MIN_BASELINE_RUNS - 1)
    # First execute() is the "most recent finished run" query (empty -> no
    # prior flag to propagate); second is the baseline-history query.
    session = _FakeSession([[], history])
    monkeypatch.setattr(breaker, "get_session", _fake_get_session(session))

    result = await breaker.check_and_flag(
        "run-1", "sync_muhafiz_data", _counts(tier_flagged_unverified=90, tier_human_review=10, tier_cnic_auto=0)
    )

    assert result["flagged"] is False
    assert "insufficient baseline history" in result["reason"]


async def test_flags_when_ambiguous_rate_exceeds_baseline_by_more_than_threshold(monkeypatch):
    """A healthy baseline (10% ambiguous) followed by a run that comes in at
    100% ambiguous — the constructed-fixture case the plan's own
    verification section calls for."""
    baseline_run = _counts(tier_cnic_auto=90, tier_flagged_unverified=5, tier_human_review=5)  # 10% ambiguous
    history = [baseline_run] * breaker.MIN_BASELINE_RUNS
    session = _FakeSession([[], history])  # no unacknowledged prior flag
    monkeypatch.setattr(breaker, "get_session", _fake_get_session(session))

    bad_run = _counts(tier_flagged_unverified=50, tier_human_review=50)  # 100% ambiguous, 0 cnic_auto

    result = await breaker.check_and_flag("run-1", "sync_muhafiz_data", bad_run)

    assert result["flagged"] is True
    assert "ambiguous-match rate" in result["reason"]
    assert "100.0%" in result["reason"]


async def test_does_not_flag_a_normal_rate_run_against_the_same_baseline(monkeypatch):
    """Same baseline as above; a run whose ambiguous rate is close to
    baseline must NOT false-positive."""
    baseline_run = _counts(tier_cnic_auto=90, tier_flagged_unverified=5, tier_human_review=5)  # 10% ambiguous
    history = [baseline_run] * breaker.MIN_BASELINE_RUNS
    session = _FakeSession([[], history])
    monkeypatch.setattr(breaker, "get_session", _fake_get_session(session))

    normal_run = _counts(tier_cnic_auto=88, tier_flagged_unverified=6, tier_human_review=6)  # 12% ambiguous

    result = await breaker.check_and_flag("run-1", "sync_muhafiz_data", normal_run)

    assert result["flagged"] is False
    assert result["reason"] == "within baseline"


async def test_flags_on_gate_rejection_rate_even_when_ambiguous_rate_is_fine(monkeypatch):
    baseline_run = _counts(tier_new=10, corroboration_gate_rejections=1)  # 10% gate rejection
    history = [baseline_run] * breaker.MIN_BASELINE_RUNS
    session = _FakeSession([[], history])
    monkeypatch.setattr(breaker, "get_session", _fake_get_session(session))

    bad_run = _counts(tier_new=10, corroboration_gate_rejections=9)  # 90% gate rejection

    result = await breaker.check_and_flag("run-1", "sync_muhafiz_data", bad_run)

    assert result["flagged"] is True
    assert "corroboration-gate rejection rate" in result["reason"]


async def test_check_and_flag_swallows_a_db_failure_and_reports_not_flagged(monkeypatch):
    async def _raise(*a, **k):
        raise RuntimeError("simulated Postgres failure")

    class _RaisingSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, *a, **k):
            raise RuntimeError("simulated Postgres failure")

    monkeypatch.setattr(breaker, "get_session", _fake_get_session(_RaisingSession()))

    result = await breaker.check_and_flag("run-1", "sync_muhafiz_data", _counts())

    assert result["flagged"] is False
    assert "circuit breaker check failed" in result["reason"]
