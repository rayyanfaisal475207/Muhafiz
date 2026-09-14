"""[Gold-QA fix — Module 143] `retry_could_help()` — the retry gate's own
contract: reads the evaluator's reason, fails open on every failure."""
import pytest

import src.pipeline.retry_gate as gate_mod


def _stub_json(monkeypatch, result, raw="raw"):
    seen = {}

    async def _call_llm_json(system_prompt, user_message, **kw):
        seen["system_prompt"] = system_prompt
        seen["user_message"] = user_message
        seen["kw"] = kw
        if isinstance(result, Exception):
            raise result
        return result, raw

    monkeypatch.setattr(gate_mod, "call_llm_json", _call_llm_json)
    return seen


@pytest.mark.asyncio
async def test_false_only_when_the_model_says_so(monkeypatch):
    seen = _stub_json(monkeypatch, {"retry_could_help": False, "reason": "aggregate"})
    assert await gate_mod.retry_could_help("how often?", "no statistical data") is False
    assert "how often?" in seen["user_message"]
    assert "no statistical data" in seen["user_message"]
    assert seen["kw"]["temperature"] == 0.0


@pytest.mark.asyncio
async def test_true_when_the_model_says_retry(monkeypatch):
    _stub_json(monkeypatch, {"retry_could_help": True, "reason": "a place is missing"})
    assert await gate_mod.retry_could_help("q", "none mention Iqbal Town") is True


@pytest.mark.asyncio
async def test_fails_open_on_no_json(monkeypatch):
    _stub_json(monkeypatch, None, raw="I think the documents...")
    assert await gate_mod.retry_could_help("q", "reason") is True


@pytest.mark.asyncio
async def test_fails_open_on_exception(monkeypatch):
    _stub_json(monkeypatch, RuntimeError("model server down"))
    assert await gate_mod.retry_could_help("q", "reason") is True


@pytest.mark.asyncio
async def test_empty_reason_never_calls_the_model(monkeypatch):
    seen = _stub_json(monkeypatch, {"retry_could_help": False, "reason": "x"})
    assert await gate_mod.retry_could_help("q", "") is True
    assert await gate_mod.retry_could_help("q", None) is True
    assert seen == {}


@pytest.mark.asyncio
async def test_validator_rejects_a_quoted_boolean(monkeypatch):
    # A model that quotes the boolean ("false") must not be read as truthy;
    # the validator handed to call_llm_json has to reject it so the JSON
    # retry/correction path runs instead.
    captured = {}

    async def _call_llm_json(system_prompt, user_message, **kw):
        captured["validate"] = kw["validate"]
        return None, ""

    monkeypatch.setattr(gate_mod, "call_llm_json", _call_llm_json)
    await gate_mod.retry_could_help("q", "reason")
    validate = captured["validate"]
    assert validate({"retry_could_help": False, "reason": "r"}) is True
    assert validate({"retry_could_help": "false", "reason": "r"}) is False
    assert validate({"retry_could_help": True}) is False
    assert validate("not a dict") is False


def test_prompt_is_loaded_and_names_the_two_outcomes():
    assert "retry_could_help" in gate_mod._SYSTEM_PROMPT
    assert "aggregate" in gate_mod._SYSTEM_PROMPT.lower()
