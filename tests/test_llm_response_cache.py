"""A cache in front of 36 call sites must not change what any of them gets.

`src/llm/client.py` cached SDK *clients* and not one token. Every caller of
`call_llm` -- document classification, NER, relationship extraction, entity
resolution, conflict detection, the aggregate spec reader, the evaluator,
the file structurer, every harness agent -- paid full price for every call,
including re-extracting a document it had already read.

This is worth having only if it cannot serve a wrong answer, cannot take
sampling away from a caller that asked for it, and cannot pretend a model
was called when it was not.
"""
import pytest

from src.llm import cache as response_cache
from src.llm.cache import CACHE, ResponseCache


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setenv("LLM_RESPONSE_CACHE", "true")
    monkeypatch.delenv("LLM_RESPONSE_CACHE_MAX_TEMPERATURE", raising=False)
    CACHE.clear()
    yield
    CACHE.clear()


@pytest.fixture
def calls(monkeypatch):
    """Count real calls, so a cached answer is visibly not one."""
    seen = []

    async def fake(system_prompt, user_message, temperature=0.0, max_tokens=1000,
                   provider_override=None, llm_mode=None, role="reasoning",
                   cloud_max_tokens=None, force_cloud=False, reasoning_effort=None):
        seen.append({"system": system_prompt, "user": user_message,
                     "temperature": temperature, "role": role})
        return f"answer-{len(seen)}"

    monkeypatch.setattr("src.llm.client._call_llm_uncached", fake)
    return seen


@pytest.mark.asyncio
async def test_an_identical_request_does_not_reach_the_model(calls):
    """The point of the thing."""
    from src.llm.client import call_llm

    first = await call_llm("SYS", "extract this document")
    second = await call_llm("SYS", "extract this document")

    assert len(calls) == 1, "the second request must not reach the model"
    assert first == second == "answer-1"
    assert CACHE.hits == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field,value", [
    ("user_message", "a different document"),
    ("system_prompt", "A DIFFERENT PROMPT"),
    ("role", "generation"),
    ("max_tokens", 4000),
    ("force_cloud", True),
    ("reasoning_effort", "high"),
])
async def test_anything_that_changes_the_answer_changes_the_key(calls, field, value):
    """A changed document must never be served the old document's answer.

    This is the failure that would matter most: extraction runs over the
    corpus, so a key that ignored the document would quietly attribute one
    FIR's facts to another.
    """
    from src.llm.client import call_llm

    base = {"system_prompt": "SYS", "user_message": "doc A"}
    await call_llm(**base)
    await call_llm(**{**base, field: value})

    assert len(calls) == 2, f"{field} must take part in the key"


@pytest.mark.asyncio
async def test_the_provider_that_will_actually_serve_it_is_in_the_key(calls, monkeypatch):
    """Two vendors do not share answers.

    The RESOLVED provider is what belongs in the key, not the override
    argument: `provider_override=None` and `provider_override="openai"` are
    the same request when LLM_PROVIDER is already openai, and caching them
    together is correct. An earlier version of this test asserted on the
    argument and started failing the moment LLM_PROVIDER became openai --
    the test was wrong, not the cache.
    """
    monkeypatch.setattr("src.config.LLM_PROVIDER", "groq")
    from src.llm.client import call_llm

    await call_llm("SYS", "q")                            # resolves to groq
    await call_llm("SYS", "q", provider_override="gemini")

    assert len(calls) == 2, "a different vendor is a different request"

    # And the no-op override is correctly treated as the same request.
    await call_llm("SYS", "q", provider_override="groq")
    assert len(calls) == 2, "an override to the configured provider changes nothing"


@pytest.mark.asyncio
async def test_a_caller_that_asked_to_sample_is_never_served_a_stored_answer(calls):
    """Above the threshold the caller wants variety; a cache would remove it.

    MEASURED in this repo: 26 of 30 call sites pass temperature=0.0, so the
    four that sample keep their behaviour and almost nothing is given up.
    """
    from src.llm.client import call_llm

    await call_llm("SYS", "rewrite this", temperature=0.7)
    await call_llm("SYS", "rewrite this", temperature=0.7)

    assert len(calls) == 2, "a sampled request must be re-sent"
    assert CACHE.stats()["entries"] == 0, "and must not be stored either"


@pytest.mark.asyncio
async def test_the_sampling_threshold_can_be_raised(calls, monkeypatch):
    """A deployment that wants everything cached may say so."""
    monkeypatch.setenv("LLM_RESPONSE_CACHE_MAX_TEMPERATURE", "1.0")
    from src.llm.client import call_llm

    await call_llm("SYS", "rewrite this", temperature=0.7)
    await call_llm("SYS", "rewrite this", temperature=0.7)

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_a_failure_is_never_remembered(monkeypatch):
    """The next attempt may well succeed, so a raise must not be cached."""
    state = {"fail": True}

    async def flaky(*args, **kwargs):
        if state["fail"]:
            raise RuntimeError("provider down")
        return "recovered"

    monkeypatch.setattr("src.llm.client._call_llm_uncached", flaky)
    from src.llm.client import call_llm

    with pytest.raises(RuntimeError):
        await call_llm("SYS", "q")
    state["fail"] = False
    assert await call_llm("SYS", "q") == "recovered"


@pytest.mark.asyncio
async def test_an_empty_response_is_a_failure_wearing_a_success_coat(monkeypatch):
    """An empty string is not an answer and must not be stored as one."""
    seen = []

    async def empty(*args, **kwargs):
        seen.append(1)
        return "   "

    monkeypatch.setattr("src.llm.client._call_llm_uncached", empty)
    from src.llm.client import call_llm

    await call_llm("SYS", "q")
    await call_llm("SYS", "q")
    assert len(seen) == 2, "an empty reply must be retried, not remembered"


@pytest.mark.asyncio
async def test_the_cache_can_be_switched_off(calls, monkeypatch):
    monkeypatch.setenv("LLM_RESPONSE_CACHE", "false")
    from src.llm.client import call_llm

    await call_llm("SYS", "q")
    await call_llm("SYS", "q")
    assert len(calls) == 2


def test_the_key_does_not_hold_a_second_copy_of_the_case_text():
    """Prompts carry case text; the key is a digest, not the prompt.

    A dictionary keyed on raw prompts would keep case text in a structure
    with none of the row-level security Postgres applies to the same text.
    """
    key = ResponseCache.key(system_prompt="SYS", user_message="کاشف was arrested")
    assert "کاشف" not in key and "arrested" not in key
    assert len(key) == 64 and all(c in "0123456789abcdef" for c in key)


def test_it_is_bounded_and_evicts_the_oldest():
    """An unbounded cache over a corpus is a memory leak."""
    small = ResponseCache(limit=3)
    for i in range(5):
        small.put(f"k{i}", f"v{i}")
    assert small.stats()["entries"] == 3
    assert small.get("k0") is None, "the oldest entry is evicted first"
    assert small.get("k4") == "v4"


def test_a_cacheable_request_reports_a_key_and_a_sampled_one_does_not():
    """A caller cannot store a sampled answer, because it is given no key."""
    _, key = response_cache.lookup(0.0, system_prompt="SYS", user_message="q")
    assert key is not None
    _, sampled = response_cache.lookup(0.9, system_prompt="SYS", user_message="q")
    assert sampled is None

    response_cache.remember(sampled, "must not be stored")
    assert CACHE.stats()["entries"] == 0
