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
