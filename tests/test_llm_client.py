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

    async def fake_call_groq(system_prompt, user_message, temperature, max_tokens, **kwargs):
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

    async def fake_call_groq(system_prompt, user_message, temperature, max_tokens, **kwargs):
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


# ── reasoning_effort plumbing (Module 2 follow-up, findings.md) ──────────────
#
# config.GROQ_MODEL (openai/gpt-oss-120b) is itself a reasoning model: with
# no way to disable its hidden reasoning trace, a small cloud_max_tokens
# budget (needed to clear the account's TPM cap — see router.py's own
# comment) got entirely consumed by that trace, returning empty content
# instead of the JSON classification. reasoning_effort="low" fixed it live.
# These tests lock in that the parameter actually reaches Groq's API call,
# is omitted (not sent as None) when a caller doesn't set it — unchanged
# behavior for every pre-existing call site — and is a no-op for Gemini.

async def test_reasoning_effort_reaches_the_groq_api_call(monkeypatch):
    monkeypatch.setattr(client.config, "LLM_PROVIDER", "groq")
    monkeypatch.setattr(client.config, "LOCAL_LLM_URL", "")
    monkeypatch.setattr(client.config, "LOCAL_GEN_LLM_URL", "")

    seen = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            seen.update(kwargs)
            class R:
                choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})]
            return R()

    class FakeChat:
        completions = FakeCompletions()

    class FakeGroqClient:
        chat = FakeChat()

    monkeypatch.setattr(client, "_get_groq_client", lambda: FakeGroqClient())

    result = await client.call_llm("system", "user", max_tokens=300, reasoning_effort="low")
    assert result == "ok"
    assert seen.get("reasoning_effort") == "low"


async def test_reasoning_effort_omitted_by_default(monkeypatch):
    """Every pre-existing call site (none of which pass reasoning_effort)
    must see unchanged behaviour: the field is never sent to Groq at all,
    not sent as an explicit None."""
    monkeypatch.setattr(client.config, "LLM_PROVIDER", "groq")
    monkeypatch.setattr(client.config, "LOCAL_LLM_URL", "")
    monkeypatch.setattr(client.config, "LOCAL_GEN_LLM_URL", "")

    seen = {}

    class FakeCompletions:
        async def create(self, **kwargs):
            seen.update(kwargs)
            class R:
                choices = [type("C", (), {"message": type("M", (), {"content": "ok"})()})]
            return R()

    class FakeChat:
        completions = FakeCompletions()

    class FakeGroqClient:
        chat = FakeChat()

    monkeypatch.setattr(client, "_get_groq_client", lambda: FakeGroqClient())

    await client.call_llm("system", "user", max_tokens=300)
    assert "reasoning_effort" not in seen


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


# ── Finding U regression: 413 oversized-payload failover ─────────────────────
# Groq reports "this request exceeds the per-request token cap" as HTTP 413
# with code `rate_limit_exceeded`. That made it indistinguishable from a
# throttle, so call_llm rotated keys — useless, since every key on the tier
# shares the same cap — burned its retry budget, and killed the chat turn
# outright (scenario-verify Finding U: the ~10.2k-token router prompt vs
# Groq's 8k free-tier cap). It must now be recognised as an oversized payload
# and failed over to a provider without that cap.

from src.llm.client import _is_payload_too_large, _is_rate_limit

_GROQ_413 = (
    "Error code: 413 - {'error': {'message': 'Request too large for model "
    "`openai/gpt-oss-120b` in organization `org_x` service tier `on_demand` on "
    "tokens per minute (TPM): Limit 8000, Requested 9970, please reduce your "
    "message size and try again.', 'type': 'tokens', 'code': 'rate_limit_exceeded'}}"
)
_GROQ_429 = (
    "Error code: 429 - {'error': {'message': 'Rate limit reached for model', "
    "'code': 'rate_limit_exceeded'}}"
)


def test_groq_413_is_detected_as_oversized_not_throttle():
    exc = Exception(_GROQ_413)
    assert _is_payload_too_large(exc) is True
    # Must NOT be treated as a throttle, or it gets "retried" by rotating keys
    # that all share the same per-request cap.
    assert _is_rate_limit(exc) is False


def test_real_throttle_still_routes_to_key_rotation():
    exc = Exception(_GROQ_429)
    assert _is_payload_too_large(exc) is False
    assert _is_rate_limit(exc) is True


def test_gemini_resource_exhausted_is_still_a_throttle():
    exc = Exception("429 RESOURCE_EXHAUSTED. quota exceeded for this project")
    assert _is_payload_too_large(exc) is False
    assert _is_rate_limit(exc) is True


def test_unrelated_errors_are_neither():
    exc = Exception("500 internal server error")
    assert _is_payload_too_large(exc) is False
    assert _is_rate_limit(exc) is False
