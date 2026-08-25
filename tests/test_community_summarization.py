"""
Tests for src/graph/community_summarization.py's
_finest_levels_to_summarize() (findings.md Module 9, Stage 2 — real
hierarchy). No real Postgres — get_session monkeypatched with a fake
session (same pattern tests/test_identity_index.py establishes).

The rest of summarize_communities() (LLM calls, case metadata fetch,
Chroma upsert) is unchanged by Stage 2 and already exercised implicitly
by this module's own live-verification runs — this file's scope is the
new level cap only.
"""
import pytest

import src.graph.community_summarization as community_summarization
from src.graph.community_summarization import MAX_LEVELS_TO_SUMMARIZE, _finest_levels_to_summarize


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeSession:
    def __init__(self, level_rows: list[tuple]):
        self._level_rows = level_rows
        self.executed: list[tuple] = []

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        return _FakeResult(self._level_rows)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_get_session(session):
    def _factory():
        return session
    return _factory


async def test_fewer_levels_than_cap_returns_all_of_them(monkeypatch):
    session = _FakeSession([(0,), (1,)])
    monkeypatch.setattr(community_summarization, "get_session", _fake_get_session(session))

    result = await _finest_levels_to_summarize("RUN-1")

    assert result == [0, 1]


async def test_more_levels_than_cap_keeps_only_the_finest(monkeypatch):
    # A >=5-level fixture — findings.md's own Test plan wording — must
    # summarize only the finest MAX_LEVELS_TO_SUMMARIZE (3), not every
    # level Louvain happened to produce.
    session = _FakeSession([(0,), (1,), (2,), (3,), (4,)])
    monkeypatch.setattr(community_summarization, "get_session", _fake_get_session(session))

    result = await _finest_levels_to_summarize("RUN-1")

    assert result == [0, 1, 2]
    assert len(result) == MAX_LEVELS_TO_SUMMARIZE


async def test_exactly_at_cap_returns_all_of_them(monkeypatch):
    session = _FakeSession([(0,), (1,), (2,)])
    monkeypatch.setattr(community_summarization, "get_session", _fake_get_session(session))

    result = await _finest_levels_to_summarize("RUN-1")

    assert result == [0, 1, 2]


async def test_no_levels_at_all_returns_empty(monkeypatch):
    session = _FakeSession([])
    monkeypatch.setattr(community_summarization, "get_session", _fake_get_session(session))

    result = await _finest_levels_to_summarize("RUN-1")

    assert result == []
