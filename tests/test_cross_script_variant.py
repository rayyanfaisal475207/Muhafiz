"""
cross_script_variant.py — the Fix 3 retrieval-only translation step
(RETRIEVAL_CROSS_LINGUAL_FIX_PROMPT.md). No network: call_llm is mocked.
"""
import pytest

import src.pipeline.cross_script_variant as csv_mod
from src.pipeline.cross_script_variant import _detect_script, generate_cross_script_variant


def test_detect_script_urdu():
    assert _detect_script("موبائل فون کی چوری") == "urdu"


def test_detect_script_latin_english():
    assert _detect_script("What PPC section applies to mobile phone theft?") == "latin"


def test_detect_script_latin_roman_urdu():
    assert _detect_script("FIR-2026-THEFT-011 mein chori kab hui thi?") == "latin"


def test_detect_script_mixed_defaults_to_urdu_if_any_arabic_char_present():
    # A single Urdu-script token amid otherwise-Latin text is still "urdu" —
    # detection only needs one match, mirroring how mixed-script police
    # documents in this corpus actually look.
    assert _detect_script("FIR-2026 میں کیا ہوا؟") == "urdu"


async def test_empty_query_returns_none():
    assert await generate_cross_script_variant("") is None
    assert await generate_cross_script_variant("   ") is None


async def test_english_query_targets_urdu_script(monkeypatch):
    captured = {}

    async def fake_call_llm(system_prompt, user_message, **kwargs):
        captured["system_prompt"] = system_prompt
        captured["user_message"] = user_message
        return "موبائل فون کی چوری پر کون سا سیکشن لاگو ہوتا ہے؟"

    monkeypatch.setattr(csv_mod, "call_llm", fake_call_llm)

    result = await generate_cross_script_variant("What section covers mobile phone theft?")
    assert result == "موبائل فون کی چوری پر کون سا سیکشن لاگو ہوتا ہے؟"
    assert "Urdu script" in captured["system_prompt"]
    assert "Urdu script" in captured["user_message"]


async def test_urdu_query_targets_english(monkeypatch):
    captured = {}

    async def fake_call_llm(system_prompt, user_message, **kwargs):
        captured["system_prompt"] = system_prompt
        return "What is the accused's name in case FIR-2026-CYBER-006?"

    monkeypatch.setattr(csv_mod, "call_llm", fake_call_llm)

    result = await generate_cross_script_variant(
        "مقدمہ FIR-2026-CYBER-006 میں ملزم کا نام کیا ہے؟"
    )
    assert result == "What is the accused's name in case FIR-2026-CYBER-006?"
    assert "English" in captured["system_prompt"]


async def test_llm_failure_returns_none(monkeypatch):
    async def failing_call_llm(*args, **kwargs):
        raise RuntimeError("model server unreachable")

    monkeypatch.setattr(csv_mod, "call_llm", failing_call_llm)

    result = await generate_cross_script_variant("What section covers theft?")
    assert result is None


async def test_empty_llm_response_returns_none(monkeypatch):
    async def blank_call_llm(*args, **kwargs):
        return "   "

    monkeypatch.setattr(csv_mod, "call_llm", blank_call_llm)

    result = await generate_cross_script_variant("What section covers theft?")
    assert result is None
