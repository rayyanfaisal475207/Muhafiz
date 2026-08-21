"""
Tests for src/retrieval/embedder.py's _embed_local_e5() (Graph Scale &
Schema Expansion, Milestone A3 — bounded-concurrency embedding, replacing
the old sequential/0.3s-paced loop).

httpx.AsyncClient is monkeypatched with a fake — no real network (matches
the `no_network` guard, conftest, autouse). Live behavior (real throughput
against the model server) was verified during development — see
docs/decisions/0002-graph-schema-expansion-and-scale.md.
"""
import asyncio

import httpx
import pytest

from src import config
from src.retrieval import embedder


class _FakeResponse:
    def __init__(self, embedding):
        self._embedding = embedding

    def raise_for_status(self):
        pass

    def json(self):
        return {"embedding": self._embedding}


def _make_fake_client(post_impl):
    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def post(self, url, json):
            return await post_impl(url, json)

    return _FakeAsyncClient


@pytest.fixture(autouse=True)
def embeddings_url(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDINGS_URL", "https://model-server.example/embed")
    yield
    monkeypatch.setattr(config, "EMBEDDINGS_URL", "")


async def test_empty_texts_returns_empty_list_no_request():
    result = await embedder._embed_local_e5([], is_query=False)
    assert result == []


async def test_raises_when_embeddings_url_not_configured(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDINGS_URL", "")
    with pytest.raises(ValueError):
        await embedder._embed_local_e5(["x"], is_query=False)


async def test_preserves_input_order_even_when_requests_complete_out_of_order(monkeypatch):
    """asyncio.gather must preserve order regardless of which request
    actually finishes first — the whole reason concurrent requests are
    safe to use here without a corpus/embedding misalignment bug."""
    async def post_impl(url, json):
        text = json["text"]
        # Deliberately finish in the REVERSE of call order: "c" (called
        # last) resolves fastest, "a" (called first) resolves slowest.
        delay = {"a": 0.03, "b": 0.02, "c": 0.01}[text]
        await asyncio.sleep(delay)
        return _FakeResponse(embedding=[ord(text) / 1000.0])

    monkeypatch.setattr(httpx, "AsyncClient", _make_fake_client(post_impl))

    result = await embedder._embed_local_e5(["a", "b", "c"], is_query=False)

    assert result == [[ord("a") / 1000.0], [ord("b") / 1000.0], [ord("c") / 1000.0]]


async def test_concurrency_never_exceeds_the_configured_limit(monkeypatch):
    monkeypatch.setattr(config, "EMBEDDING_MAX_CONCURRENCY", 3)
    active = 0
    peak = 0
    lock = asyncio.Lock()

    async def post_impl(url, json):
        nonlocal active, peak
        async with lock:
            active += 1
            peak = max(peak, active)
        await asyncio.sleep(0.02)
        async with lock:
            active -= 1
        return _FakeResponse(embedding=[0.1])

    monkeypatch.setattr(httpx, "AsyncClient", _make_fake_client(post_impl))

    texts = [f"chunk-{i}" for i in range(12)]
    result = await embedder._embed_local_e5(texts, is_query=False)

    assert len(result) == 12
    assert peak <= 3, f"expected at most 3 concurrent requests, saw {peak}"
    assert peak > 1, "expected genuine concurrency, not accidental serialization"


async def test_throughput_scales_with_concurrency_not_linear_in_corpus_size(monkeypatch):
    """
    The actual Milestone A3 claim: wall-clock cost for N chunks should be
    roughly (N / concurrency) * per_request_latency, not N * per_request_
    latency (the old sequential-with-pacing shape).
    """
    monkeypatch.setattr(config, "EMBEDDING_MAX_CONCURRENCY", 8)
    per_request_latency = 0.05

    async def post_impl(url, json):
        await asyncio.sleep(per_request_latency)
        return _FakeResponse(embedding=[0.1])

    monkeypatch.setattr(httpx, "AsyncClient", _make_fake_client(post_impl))

    texts = [f"chunk-{i}" for i in range(16)]
    import time
    start = time.perf_counter()
    await embedder._embed_local_e5(texts, is_query=False)
    elapsed = time.perf_counter() - start

    sequential_would_have_taken = len(texts) * per_request_latency
    # With concurrency=8 over 16 requests, wall clock should be roughly
    # 2 batches worth of latency, nowhere near 16 sequential requests'
    # worth (~0.8s) plus the old 0.3s-per-request pacing this replaces.
    assert elapsed < sequential_would_have_taken / 2, (
        f"expected concurrent execution well under {sequential_would_have_taken:.2f}s, "
        f"took {elapsed:.2f}s"
    )


async def test_is_query_flag_threaded_through_to_every_request(monkeypatch):
    seen_is_query = []

    async def post_impl(url, json):
        seen_is_query.append(json["is_query"])
        return _FakeResponse(embedding=[0.1])

    monkeypatch.setattr(httpx, "AsyncClient", _make_fake_client(post_impl))

    await embedder._embed_local_e5(["a", "b"], is_query=True)

    assert seen_is_query == [True, True]


async def test_retries_on_transient_http_error(monkeypatch):
    attempts = {"count": 0}

    async def post_impl(url, json):
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise httpx.TimeoutException("simulated timeout")
        return _FakeResponse(embedding=[0.1])

    monkeypatch.setattr(httpx, "AsyncClient", _make_fake_client(post_impl))

    result = await embedder._embed_local_e5(["a"], is_query=False)

    assert result == [[0.1]]
    assert attempts["count"] == 2
