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

class _FakeDelta:
    def __init__(self, content):
        self.content = content


class _FakeChoice:
    def __init__(self, content):
        self.delta = _FakeDelta(content)


class _FakeChunk:
    def __init__(self, content):
        self.choices = [_FakeChoice(content)]


class _FakeStream:
    def __init__(self, contents):
        self._contents = contents

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        for content in self._contents:
            yield _FakeChunk(content)


class _FakeCompletions:
    def __init__(self, contents):
        self._contents = contents

    async def create(self, **kwargs):
        return _FakeStream(self._contents)


class _FakeChat:
    def __init__(self, contents):
        self.completions = _FakeCompletions(contents)


class _FakeLocalClient:
    def __init__(self, contents):
        self.chat = _FakeChat(contents)


async def test_stream_local_raises_when_every_chunk_is_empty_or_whitespace(monkeypatch):
    monkeypatch.setattr(
        client, "_local_client_and_model",
        lambda role: (_FakeLocalClient(["", "   ", ""]), "fake-model"),
    )
    with pytest.raises(ValueError):
        async for _ in client._stream_local("system", "user", 0.0, 100):
            pass


async def test_stream_local_passes_through_real_content_without_raising(monkeypatch):
    monkeypatch.setattr(
        client, "_local_client_and_model",
        lambda role: (_FakeLocalClient(["Hello", " world"]), "fake-model"),
    )
    chunks = [chunk async for chunk in client._stream_local("system", "user", 0.0, 100)]
    assert chunks == ["Hello", " world"]


async def test_stream_local_falls_back_to_cloud_when_stream_is_empty(monkeypatch):
    """End-to-end through stream_llm(): an all-empty local stream must still
    trigger the existing Groq fallback, not silently yield nothing."""
    monkeypatch.setattr(client.config, "LOCAL_LLM_URL", "http://local-model")
    monkeypatch.setattr(client.config, "LLM_PROVIDER", "groq")
    monkeypatch.setattr(
        client, "_local_client_and_model",
        lambda role: (_FakeLocalClient([""]), "fake-model"),
    )

    async def fake_stream_groq(system_prompt, user_message, temperature, max_tokens, enable_tools):
        yield "fallback answer"

    monkeypatch.setattr(client, "_stream_groq", fake_stream_groq)

    chunks = [chunk async for chunk in client.stream_llm("system", "user")]
    assert chunks == ["fallback answer"]
