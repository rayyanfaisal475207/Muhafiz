"""A cache that can serve a wrong answer is worse than no cache.

The response cache exists because one planning call costs most of a minute's
token allowance and the same questions get asked repeatedly. That is only
worth having if it cannot serve an answer about data that has moved, cannot
cross between users, and cannot pretend a model was called when it was not.
"""
import json

import httpx
import pytest

from src.evidence.model import CACHE, LocalModel
from src.evidence.types import QueryUnderstanding


def transport(calls, language="en"):
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={
            "response": json.dumps({"language": language, "parts": [], "terms": []}),
            "finish_reason": "stop"})
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def fresh():
    CACHE._items.clear()
    CACHE.hits = CACHE.misses = 0
    yield
    CACHE._items.clear()


@pytest.mark.asyncio
async def test_an_identical_request_is_served_without_calling_the_model(monkeypatch):
    """The point of the thing: the second ask costs nothing."""
    monkeypatch.setenv("EVIDENCE_MODEL_CACHE", "true")
    calls = []
    model = LocalModel(transport=transport(calls))

    first = await model.structured(QueryUnderstanding, "SYS", {"question": "q"})
    second = await model.structured(QueryUnderstanding, "SYS", {"question": "q"})

    assert len(calls) == 1, "the second request must not reach the model"
    assert first.language == second.language
    assert CACHE.hits == 1


@pytest.mark.asyncio
async def test_a_cached_answer_says_it_is_cached(monkeypatch):
    """A trace showing a model call that never happened would be a lie.

    This system refuses to publish a number it cannot show the source of;
    it must not quietly report a round trip it did not make either.
    """
    monkeypatch.setenv("EVIDENCE_MODEL_CACHE", "true")
    model = LocalModel(transport=transport([]))

    await model.structured(QueryUnderstanding, "SYS", {"question": "q"})
    await model.structured(QueryUnderstanding, "SYS", {"question": "q"})

    assert model.diagnostics[0].get("cached") is False
    assert model.diagnostics[1].get("cached") is True
    assert model.diagnostics[1].get("seconds") == 0.0


@pytest.mark.asyncio
async def test_a_changed_payload_is_a_different_question(monkeypatch):
    """Self-invalidating, because the data travels in the payload.

    The planning payload carries the catalog, the resolved candidates and
    the retrieved passages. If the records change, the payload changes, the
    key changes, and the old entry is never consulted again -- so the cache
    cannot answer about a corpus that has moved on.
    """
    monkeypatch.setenv("EVIDENCE_MODEL_CACHE", "true")
    calls = []
    model = LocalModel(transport=transport(calls))

    await model.structured(QueryUnderstanding, "SYS", {"question": "q", "catalog": ["a"]})
    await model.structured(QueryUnderstanding, "SYS", {"question": "q", "catalog": ["a", "b"]})

    assert len(calls) == 2, "a different catalog is a different request"


@pytest.mark.asyncio
async def test_two_endpoints_do_not_share_answers(monkeypatch):
    """A different transport is a different source of truth.

    MEASURED: a test issuing the identical request through a working
    transport and then a broken one got the first answer back for the
    second, so an assertion that the second FAILS saw it succeed.
    """
    monkeypatch.setenv("EVIDENCE_MODEL_CACHE", "true")
    good = LocalModel(transport=transport([], language="en"))
    await good.structured(QueryUnderstanding, "SYS", {"question": "q"})

    refused = []

    def handler(request):
        refused.append(1)
        return httpx.Response(500, json={"error": "down"})

    other = LocalModel(transport=httpx.MockTransport(handler))
    with pytest.raises(Exception):
        await other.structured(QueryUnderstanding, "SYS", {"question": "q"})
    assert refused, "the second endpoint must actually be contacted"


@pytest.mark.asyncio
async def test_a_failure_is_never_remembered(monkeypatch):
    """Only validated results are kept; the next attempt may well succeed."""
    monkeypatch.setenv("EVIDENCE_MODEL_CACHE", "true")
    state = {"fail": True}

    def handler(request):
        if state["fail"]:
            return httpx.Response(500, json={"error": "down"})
        return httpx.Response(200, json={
            "response": json.dumps({"language": "en", "parts": [], "terms": []}),
            "finish_reason": "stop"})

    model = LocalModel(transport=httpx.MockTransport(handler))
    with pytest.raises(Exception):
        await model.structured(QueryUnderstanding, "SYS", {"question": "q"})
    state["fail"] = False
    ok = await model.structured(QueryUnderstanding, "SYS", {"question": "q"})
    assert ok.language == "en", "a failure must not be cached as the answer"


@pytest.mark.asyncio
async def test_a_cached_result_cannot_be_mutated_by_its_caller(monkeypatch):
    """The runtime edits plans and results in place.

    Handing out the stored object would let one question quietly alter
    another's answer, which is the hardest kind of bug to find and exactly
    what this system must not have.
    """
    monkeypatch.setenv("EVIDENCE_MODEL_CACHE", "true")
    model = LocalModel(transport=transport([]))

    first = await model.structured(QueryUnderstanding, "SYS", {"question": "q"})
    first.language = "ur"
    second = await model.structured(QueryUnderstanding, "SYS", {"question": "q"})

    assert second.language == "en", "a caller's edit must not reach the cache"


@pytest.mark.asyncio
async def test_the_cache_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("EVIDENCE_MODEL_CACHE", "false")
    calls = []
    model = LocalModel(transport=transport(calls))

    await model.structured(QueryUnderstanding, "SYS", {"question": "q"})
    await model.structured(QueryUnderstanding, "SYS", {"question": "q"})

    assert len(calls) == 2
