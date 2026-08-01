"""
call_llm()'s local-vs-cloud max_tokens split (Module 6.3).

The caller can't know in advance whether a call resolves to the local model
or a cloud fallback — call_llm() decides that internally, per attempt. These
tests guard that cloud_max_tokens (when given) reaches the cloud branch
without disturbing the local branch's own (confirmed, live-verified)
max_tokens value.
"""
import pytest

import src.llm.client as client


@pytest.fixture(autouse=True)
def _no_local_endpoint(monkeypatch):
    # Force every call in this file down the cloud path unless a test
    # explicitly re-enables a local URL.
    monkeypatch.setattr(client.config, "LOCAL_LLM_URL", "")
    monkeypatch.setattr(client.config, "LOCAL_GEN_LLM_URL", "")
    monkeypatch.setattr(client.config, "AIR_GAP_MODE", False)


async def test_cloud_max_tokens_overrides_max_tokens_on_the_groq_branch(monkeypatch):
    monkeypatch.setattr(client.config, "LLM_PROVIDER", "groq")

    seen = {}

    async def fake_call_groq(system_prompt, user_message, temperature, max_tokens):
        seen["max_tokens"] = max_tokens
        return "ok"

    monkeypatch.setattr(client, "_call_groq", fake_call_groq)

    result = await client.call_llm(
        "system", "user", max_tokens=2000, cloud_max_tokens=800,
    )
    assert result == "ok"
    assert seen["max_tokens"] == 800


async def test_cloud_max_tokens_overrides_max_tokens_on_the_gemini_branch(monkeypatch):
    monkeypatch.setattr(client.config, "LLM_PROVIDER", "gemini")

    seen = {}

    async def fake_call_gemini(system_prompt, user_message, temperature, max_tokens):
        seen["max_tokens"] = max_tokens
        return "ok"

    monkeypatch.setattr(client, "_call_gemini", fake_call_gemini)

    result = await client.call_llm(
        "system", "user", max_tokens=2000, cloud_max_tokens=800,
    )
    assert result == "ok"
    assert seen["max_tokens"] == 800


async def test_omitting_cloud_max_tokens_falls_back_to_max_tokens(monkeypatch):
    """Every pre-existing call site (none of which pass cloud_max_tokens)
    must see unchanged behaviour: the cloud branch gets the same value as
    the local branch would have."""
    monkeypatch.setattr(client.config, "LLM_PROVIDER", "groq")

    seen = {}

    async def fake_call_groq(system_prompt, user_message, temperature, max_tokens):
        seen["max_tokens"] = max_tokens
        return "ok"

    monkeypatch.setattr(client, "_call_groq", fake_call_groq)

    await client.call_llm("system", "user", max_tokens=1500)
    assert seen["max_tokens"] == 1500


async def test_local_branch_uses_max_tokens_not_cloud_max_tokens(monkeypatch):
    monkeypatch.setattr(client.config, "LOCAL_LLM_URL", "http://local-model")

    seen = {}

    async def fake_call_local(system_prompt, user_message, temperature, max_tokens, role="reasoning"):
        seen["max_tokens"] = max_tokens
        return "ok"

    monkeypatch.setattr(client, "_call_local", fake_call_local)

    result = await client.call_llm(
        "system", "user", max_tokens=2000, cloud_max_tokens=800,
    )
    assert result == "ok"
    assert seen["max_tokens"] == 2000


# ── _stream_local empty/whitespace-content fallback (Module 6.4) ──────────────
#
# _call_local() already treats a blank/whitespace-only completion the same as
# a hard failure (Qwen3-14B can spend its entire budget on its hidden
# thinking trace and return empty content in an otherwise-200 response).
# _stream_local() never got that guard, so a stream that produced nothing
# real would complete "successfully" and stream_llm() would never fall back
# to Groq/Gemini.
#
# The local server turned out not to be OpenAI-compatible at all (bespoke
# {"prompt": ...} -> {"response": ...} API, confirmed live, no streaming
# mode) — _call_local/_stream_local both now go through _post_local, a
# single non-streaming httpx call; _stream_local fakes "streaming" by
# yielding that one response as a single chunk. These tests mock
# _post_local directly instead of an AsyncOpenAI-shaped fake client.

async def test_stream_local_raises_when_response_is_empty_or_whitespace(monkeypatch):
    async def fake_post_local(system_prompt, user_message, temperature, max_tokens, role):
        return "   "

    monkeypatch.setattr(client, "_post_local", fake_post_local)
    with pytest.raises(ValueError):
        async for _ in client._stream_local("system", "user", 0.0, 100):
            pass


async def test_stream_local_passes_through_real_content_without_raising(monkeypatch):
    async def fake_post_local(system_prompt, user_message, temperature, max_tokens, role):
        return "Hello world"

    monkeypatch.setattr(client, "_post_local", fake_post_local)
    chunks = [chunk async for chunk in client._stream_local("system", "user", 0.0, 100)]
    assert chunks == ["Hello world"]


async def test_stream_local_falls_back_to_cloud_when_response_is_empty(monkeypatch):
    """End-to-end through stream_llm(): an all-empty local response must still
    trigger the existing Groq fallback, not silently yield nothing."""
    monkeypatch.setattr(client.config, "LOCAL_LLM_URL", "http://local-model")
    monkeypatch.setattr(client.config, "LLM_PROVIDER", "groq")

    async def fake_post_local(system_prompt, user_message, temperature, max_tokens, role):
        return ""

    monkeypatch.setattr(client, "_post_local", fake_post_local)

    async def fake_stream_groq(system_prompt, user_message, temperature, max_tokens, enable_tools):
        yield "fallback answer"

    monkeypatch.setattr(client, "_stream_groq", fake_stream_groq)

    chunks = [chunk async for chunk in client.stream_llm("system", "user")]
    assert chunks == ["fallback answer"]
