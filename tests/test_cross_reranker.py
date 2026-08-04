"""Tests for src/retrieval/cross_reranker.py's result-to-candidate matching (C-2).

The reranker server returns matched document TEXT + score, not an index —
confirmed live against the real RERANKER_URL server before this fix that it
returns ONLY {"document", "score"} and silently ignores an "ids" field
added to the request, so index-based matching isn't achievable from this
repo alone. These tests cover the text-based matching logic (including the
whitespace-normalized fallback and the duplicate-consumption bug caught
during that fix) via a fake httpx client — no real network call.
"""
import pytest

import src.retrieval.cross_reranker as cross_reranker
from src import config


class _FakeResponse:
    def __init__(self, data):
        self._data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self._data


class _FakeAsyncClient:
    def __init__(self, server_response):
        self._server_response = server_response

    def __call__(self, timeout=30.0):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, json):
        return _FakeResponse(self._server_response)


@pytest.fixture
def fake_reranker(monkeypatch):
    monkeypatch.setattr(config, "RERANKER_URL", "http://fake-reranker")

    def _install(server_response):
        import httpx
        monkeypatch.setattr(httpx, "AsyncClient", _FakeAsyncClient(server_response))

    return _install


@pytest.mark.asyncio
async def test_matches_candidates_back_despite_server_reordering(fake_reranker):
    # The server returns results in SCORE order, not input order — confirmed
    # live. Matching must follow the returned order, not the input order.
    candidates = [
        {"id": "a", "text": "alpha"},
        {"id": "b", "text": "beta"},
        {"id": "c", "text": "gamma"},
    ]
    fake_reranker([
        {"document": "gamma", "score": 3},
        {"document": "alpha", "score": 2},
        {"document": "beta", "score": 1},
    ])
    result = await cross_reranker.cross_rerank("q", candidates, top_k=10)
    assert [c["id"] for c in result] == ["c", "a", "b"]
    assert [c["rerank_score"] for c in result] == [3, 2, 1]


@pytest.mark.asyncio
async def test_duplicate_text_candidates_each_matched_once(fake_reranker):
    """
    Regression: an earlier version of the normalized-text fallback built a
    second independent index pointing at the same candidate objects, which
    let one candidate be matched TWICE (once via exact match, once via the
    fallback) while its duplicate-text sibling was never matched at all.
    """
    candidates = [
        {"id": "d1", "text": "dup"},
        {"id": "d2", "text": "dup"},
    ]
    fake_reranker([
        {"document": "dup", "score": 5},
        {"document": "dup", "score": 4},
    ])
    result = await cross_reranker.cross_rerank("q", candidates, top_k=10)
    assert [c["id"] for c in result] == ["d1", "d2"]


@pytest.mark.asyncio
async def test_whitespace_only_difference_still_matches(fake_reranker):
    candidates = [{"id": "w1", "text": "hello   world"}]
    fake_reranker([{"document": "hello world", "score": 1.0}])
    result = await cross_reranker.cross_rerank("q", candidates, top_k=10)
    assert [c["id"] for c in result] == ["w1"]


@pytest.mark.asyncio
async def test_unmatchable_document_is_skipped_not_fatal(fake_reranker):
    candidates = [{"id": "e1", "text": "one"}, {"id": "e2", "text": "two"}]
    fake_reranker([
        {"document": "one", "score": 1},
        {"document": "text that was never a candidate", "score": 2},
        {"document": "two", "score": 0.5},
    ])
    result = await cross_reranker.cross_rerank("q", candidates, top_k=10)
    assert [c["id"] for c in result] == ["e1", "e2"]


@pytest.mark.asyncio
async def test_empty_candidates_returns_empty_without_network_call(monkeypatch):
    result = await cross_reranker.cross_rerank("q", [], top_k=5)
    assert result == []


@pytest.mark.asyncio
async def test_no_reranker_url_configured_falls_back_to_input_order(monkeypatch):
    monkeypatch.setattr(config, "RERANKER_URL", "")
    candidates = [{"id": "a", "text": "x"}, {"id": "b", "text": "y"}]
    result = await cross_reranker.cross_rerank("q", candidates, top_k=1)
    assert result == candidates[:1]
