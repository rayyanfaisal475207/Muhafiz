"""
Tests for src/graph/entity_resolution_sampling.py (Ingestion Quality
Control at Scale, Module G3).

age_client and get_session are monkeypatched with fakes — no real
Postgres/AGE. The single most important property this suite asserts,
explicitly and repeatedly: this module NEVER calls
src.graph.versioning.write_edge or anything that could write a
SAME_AS/CITES edge — verified by monkeypatching versioning with a
call-recording stub the module never even imports, and by asserting no
Cypher string in age_client's recorded calls contains CREATE/SET/MERGE.
"""
import pytest

import src.graph.entity_resolution_sampling as sampling


class FakeAgeClient:
    def __init__(self):
        self.calls: list[dict] = []
        self.responses: list[list[dict]] = []

    def queue(self, response):
        self.responses.append(response)

    async def execute_cypher(self, cypher_query, params=None, columns=None, graph=None):
        self.calls.append({"cypher": cypher_query, "params": params or {}})
        if self.responses:
            return self.responses.pop(0)
        return []


class _Row:
    def __init__(self, d):
        self._mapping = d


class _FakeResult:
    def fetchall(self):
        return []


class _FakeSession:
    def __init__(self):
        self.executed: list[tuple] = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return _FakeResult()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_get_session(session):
    def _factory():
        return session
    return _factory


@pytest.fixture
def fake_age(monkeypatch):
    """
    Patches BOTH sampling.age_client and
    candidate_reprioritization.age_client with the SAME fake — the
    reused helpers (_fetch_cases_for/_fetch_node_by_entity_id) close over
    their OWN module's age_client reference, not sampling's, so both
    must point at one recorder for a test to see every call in order.
    """
    client = FakeAgeClient()
    monkeypatch.setattr(sampling, "age_client", client)
    import src.graph.candidate_reprioritization as candidate_reprioritization
    monkeypatch.setattr(candidate_reprioritization, "age_client", client)
    return client


def _person(entity_id, **props):
    return {"id": 1, "label": "Person", "properties": {"entity_id": entity_id, **props}}


def _same_as_edge(edge_id, *, tier="flagged_unverified", status="pending", name_similarity=0.9,
                   shared_case=True, shared_structured_id=False, basis="matched on name"):
    return {
        "id": edge_id, "label": "SAME_AS",
        "properties": {
            "tier": tier, "status": status, "basis": basis,
            "name_similarity": name_similarity, "shared_case": shared_case,
            "shared_structured_id": shared_structured_id,
            "as_of": "2026-01-01T00:00:00+00:00", "superseded_by": None,
        },
    }


# ── _is_degraded(): pure logic, no I/O ───────────────────────────────────

def test_is_degraded_none_when_nothing_changed():
    original = {"name_similarity": 0.9, "shared_case": True, "shared_structured_id": False}
    fresh = {"name_similarity": 0.9, "shared_case": True, "shared_structured_id": False}
    assert sampling._is_degraded(original, fresh) is None


def test_is_degraded_flags_lost_shared_case():
    original = {"name_similarity": 0.9, "shared_case": True, "shared_structured_id": False}
    fresh = {"name_similarity": 0.9, "shared_case": False, "shared_structured_id": False}
    reason = sampling._is_degraded(original, fresh)
    assert reason is not None
    assert "no longer shares a case" in reason


def test_is_degraded_flags_lost_structured_id():
    original = {"name_similarity": 0.9, "shared_case": True, "shared_structured_id": True}
    fresh = {"name_similarity": 0.9, "shared_case": True, "shared_structured_id": False}
    reason = sampling._is_degraded(original, fresh)
    assert reason is not None
    assert "structured identifier" in reason


def test_is_degraded_flags_significant_name_similarity_drop():
    original = {"name_similarity": 0.90, "shared_case": True, "shared_structured_id": False}
    fresh = {"name_similarity": 0.80, "shared_case": True, "shared_structured_id": False}
    reason = sampling._is_degraded(original, fresh)
    assert reason is not None
    assert "name similarity dropped" in reason


def test_is_degraded_ignores_a_small_name_similarity_wobble():
    """A tiny drop (below the 0.05 threshold) must not false-positive."""
    original = {"name_similarity": 0.90, "shared_case": True, "shared_structured_id": False}
    fresh = {"name_similarity": 0.88, "shared_case": True, "shared_structured_id": False}
    assert sampling._is_degraded(original, fresh) is None


def test_is_degraded_ignores_reinforcement_not_just_degradation():
    """Gaining a signal is not degradation — must not flag."""
    original = {"name_similarity": 0.80, "shared_case": False, "shared_structured_id": False}
    fresh = {"name_similarity": 0.95, "shared_case": True, "shared_structured_id": True}
    assert sampling._is_degraded(original, fresh) is None


# ── run_sample(): population + orchestration ─────────────────────────────

async def test_run_sample_returns_zero_zero_when_nothing_to_sample(fake_age):
    fake_age.queue([])  # _recent_candidates()
    result = await sampling.run_sample()
    assert result == {"sampled": 0, "findings": 0}


async def test_run_sample_records_a_finding_for_a_degraded_pending_candidate(monkeypatch, fake_age):
    edge = _same_as_edge(101, status="pending", name_similarity=0.95, shared_case=True)
    mention = _person("PER-1", canonical_name="Ali Khan")
    candidate = _person("PER-2", canonical_name="Ali Khann")

    fake_age.queue([{"a": mention, "r": edge, "b": candidate}])  # _recent_candidates()
    fake_age.queue([])  # _fetch_cases_for -> BELONGS_TO_CASE query (candidate_reprioritization's own helper)
    fake_age.queue([{"n": mention}])  # _fetch_node_by_entity_id(mention)
    fake_age.queue([{"n": candidate}])  # _fetch_node_by_entity_id(candidate)

    # Force a degraded outcome deterministically regardless of the real
    # name-similarity algorithm's exact float output.
    monkeypatch.setattr(sampling, "_fresh_signal", lambda m, c, cbe: {
        "name_similarity": 0.10, "shared_case": False, "shared_structured_id": False,
    })

    session = _FakeSession()
    monkeypatch.setattr(sampling, "get_session", _fake_get_session(session))

    result = await sampling.run_sample(sample_size=5)

    assert result == {"sampled": 1, "findings": 1}
    insert_calls = [c for c in session.executed if "INSERT INTO entity_resolution_consistency_findings" in c[0]]
    assert len(insert_calls) == 1
    _, params = insert_calls[0]
    assert params["edge_id"] == 101
    assert params["status"] == "pending"
    assert params["mention_id"] == "PER-1"
    assert params["candidate_id"] == "PER-2"


async def test_run_sample_records_nothing_for_a_still_healthy_candidate(monkeypatch, fake_age):
    edge = _same_as_edge(102, status="pending")
    mention = _person("PER-3")
    candidate = _person("PER-4")

    fake_age.queue([{"a": mention, "r": edge, "b": candidate}])
    fake_age.queue([])
    fake_age.queue([{"n": mention}])
    fake_age.queue([{"n": candidate}])

    monkeypatch.setattr(sampling, "_fresh_signal", lambda m, c, cbe: {
        "name_similarity": 0.9, "shared_case": True, "shared_structured_id": False,
    })

    session = _FakeSession()
    monkeypatch.setattr(sampling, "get_session", _fake_get_session(session))

    result = await sampling.run_sample(sample_size=5)

    assert result == {"sampled": 1, "findings": 0}
    assert session.executed == []


async def test_run_sample_walks_back_to_the_superseded_edge_for_a_confirmed_candidate(monkeypatch, fake_age):
    """A confirmed edge's own properties carry no name_similarity/
    shared_case/shared_structured_id (see graph_review.confirm_match) —
    the original signal must come from the pending edge it superseded."""
    confirmed_edge = _same_as_edge(201, status="confirmed")
    confirmed_edge["properties"] = {  # exactly what confirm_match() actually writes
        "tier": "flagged_unverified", "basis": "matched on name", "status": "confirmed",
        "reviewed_by": "investigator-1", "as_of": "2026-01-02T00:00:00+00:00", "superseded_by": None,
    }
    original_pending_edge = _same_as_edge(200, status="pending", name_similarity=0.95, shared_case=True)
    mention = _person("PER-5")
    candidate = _person("PER-6")

    fake_age.queue([{"a": mention, "r": confirmed_edge, "b": candidate}])  # _recent_candidates()
    fake_age.queue([])  # _fetch_cases_for
    fake_age.queue([{"old": original_pending_edge}])  # _original_signal() walk-back
    fake_age.queue([{"n": mention}])
    fake_age.queue([{"n": candidate}])

    monkeypatch.setattr(sampling, "_fresh_signal", lambda m, c, cbe: {
        "name_similarity": 0.10, "shared_case": False, "shared_structured_id": False,
    })
    session = _FakeSession()
    monkeypatch.setattr(sampling, "get_session", _fake_get_session(session))

    result = await sampling.run_sample(sample_size=5)

    assert result == {"sampled": 1, "findings": 1}
    walk_back_calls = [c for c in fake_age.calls if "old.superseded_by" in c["cypher"]]
    assert len(walk_back_calls) == 1
    assert walk_back_calls[0]["params"] == {"edge_id": 201}


async def test_run_sample_skips_a_confirmed_candidate_with_no_recoverable_original(monkeypatch, fake_age):
    confirmed_edge = _same_as_edge(202, status="confirmed")
    mention = _person("PER-7")
    candidate = _person("PER-8")

    fake_age.queue([{"a": mention, "r": confirmed_edge, "b": candidate}])
    fake_age.queue([])  # _fetch_cases_for
    fake_age.queue([])  # _original_signal walk-back finds nothing

    session = _FakeSession()
    monkeypatch.setattr(sampling, "get_session", _fake_get_session(session))

    result = await sampling.run_sample(sample_size=5)

    assert result == {"sampled": 1, "findings": 0}
    assert session.executed == []


# ── the hard rule: never writes a graph edge, ever ────────────────────────

async def test_module_never_imports_versioning():
    """versioning.py is where every SAME_AS/CITES write in this codebase
    goes through — this module must not even bind it as a name, the same
    structural guarantee candidate_reprioritization.py relies on. Checked
    against the module's actual namespace (not a source-text grep, which
    would also trip on this very docstring mentioning it)."""
    assert "versioning" not in vars(sampling)


async def test_run_sample_issues_no_write_cypher(monkeypatch, fake_age):
    """Every Cypher string this module sends to AGE across a full pass
    that DOES find and record a finding is read-only."""
    edge = _same_as_edge(103, status="pending")
    mention = _person("PER-9")
    candidate = _person("PER-10")
    fake_age.queue([{"a": mention, "r": edge, "b": candidate}])
    fake_age.queue([])
    fake_age.queue([{"n": mention}])
    fake_age.queue([{"n": candidate}])
    monkeypatch.setattr(sampling, "_fresh_signal", lambda m, c, cbe: {
        "name_similarity": 0.0, "shared_case": False, "shared_structured_id": False,
    })
    session = _FakeSession()
    monkeypatch.setattr(sampling, "get_session", _fake_get_session(session))

    await sampling.run_sample(sample_size=5)

    for call in fake_age.calls:
        upper = call["cypher"].upper()
        assert "CREATE" not in upper
        assert " SET " not in upper
        assert "MERGE" not in upper
        assert "DELETE" not in upper
