"""
Tests for src/pipeline/title_generator.py.

Regression: this call site used max_tokens=20, the same bug class Phase 0
fixed everywhere else (Qwen3's thinking trace consumes the token budget
before the real answer, silently producing an empty/truncated title). Live
testing this session showed a `title_generation` event firing with an empty
detail. Guards the fix (max_tokens=800, explicit role="reasoning") and the
existing fail-safe fallback behavior.
"""
import pytest

import src.pipeline.title_generator as title_generator


@pytest.mark.asyncio
async def test_generate_and_save_title_uses_a_real_token_budget(monkeypatch, gateway):
    captured_kwargs = {}

    async def fake_call_llm(**kwargs):
        captured_kwargs.update(kwargs)
        return "Mobile Theft FIR Inquiry"

    monkeypatch.setattr(title_generator, "call_llm", fake_call_llm)
    monkeypatch.setattr(title_generator, "get_gateway", lambda: _async_return(gateway))

    session_id = "11111111-1111-1111-1111-111111111111"
    gateway.sessions[session_id] = {"session_id": session_id, "title": "New Chat"}

    title = await title_generator.generate_and_save_title(session_id, "Who stole my phone?")

    assert title == "Mobile Theft FIR Inquiry"
    assert captured_kwargs["max_tokens"] == 800, "max_tokens=20 silently starved Qwen3's thinking trace"
    assert captured_kwargs["role"] == "reasoning"
    assert gateway.sessions[session_id]["title"] == "Mobile Theft FIR Inquiry"


@pytest.mark.asyncio
async def test_generate_and_save_title_falls_back_on_llm_failure(monkeypatch, gateway):
    async def failing_call_llm(**kwargs):
        raise RuntimeError("model server unreachable")

    monkeypatch.setattr(title_generator, "call_llm", failing_call_llm)
    monkeypatch.setattr(title_generator, "get_gateway", lambda: _async_return(gateway))

    session_id = "22222222-2222-2222-2222-222222222222"
    gateway.sessions[session_id] = {"session_id": session_id, "title": "New Chat"}
    message = "What section covers theft under 5000 rupees?"

    title = await title_generator.generate_and_save_title(session_id, message)

    expected_fallback = " ".join(message.split(" ")[:5]) + "..."
    assert title == expected_fallback
    assert gateway.sessions[session_id]["title"] == expected_fallback


async def _async_return(value):
    return value
