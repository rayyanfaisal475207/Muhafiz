"""Tests for src/pipeline/query_expander.py's script-parity guard (D-2).

Live-caught (2026-08-04): a Roman-Urdu query expanded into Devanagari/
Hindi-script variants despite an explicit prompt instruction not to
translate — the prompt alone wasn't reliable, so expand_query() also
enforces script parity structurally, same pattern as query_rewriter.py's
post-hoc guards.
"""
import pytest

from src.pipeline.query_expander import _script_class, expand_query


def test_script_class_latin():
    assert _script_class("cyber harassment ke liye kaunsi section lagti hai") == "latin"


def test_script_class_arabic():
    assert _script_class("چوری کی ایف آئی آر کے لیے کون سے دستاویزات درکار ہیں") == "arabic"


def test_script_class_devanagari():
    assert _script_class("कैसे साइबर हरसमेंत के लिए कौन सा धारा लागू होती है") == "devanagari"


@pytest.mark.asyncio
async def test_expand_query_drops_devanagari_variant_for_roman_urdu_input(monkeypatch):
    """
    Real live failure: a Roman-Urdu query produced Devanagari variants.
    Must be filtered out rather than returned to the caller.
    """
    async def fake_call_llm_json(**kwargs):
        return (
            [
                "क्या साइबर हरासमेंट के लिए कोई विशेष धारा लागू होती है",  # Devanagari — must be dropped
                "online harassment ke against konsi dafa lagti hai",         # Latin — must survive
            ],
            "raw",
        )

    monkeypatch.setattr("src.pipeline.query_expander.call_llm_json", fake_call_llm_json)
    result = await expand_query("cyber harassment ke liye kaunsi section lagti hai", n=2)
    assert result == ["online harassment ke against konsi dafa lagti hai"]


@pytest.mark.asyncio
async def test_expand_query_drops_script_switched_variant_for_urdu_script_input(monkeypatch):
    """A pure-English variant for an Urdu-script query is a translation, not a paraphrase — must be dropped."""
    async def fake_call_llm_json(**kwargs):
        return (
            [
                "What documents are required for a theft FIR?",  # Latin — wrong script, must be dropped
                "چوری کی رپورٹ درج کروانے کے لیے کاغذات کی فہرست",  # Arabic — must survive
            ],
            "raw",
        )

    monkeypatch.setattr("src.pipeline.query_expander.call_llm_json", fake_call_llm_json)
    result = await expand_query("چوری کی ایف آئی آر کے لیے کون سے دستاویزات درکار ہیں", n=2)
    assert result == ["چوری کی رپورٹ درج کروانے کے لیے کاغذات کی فہرست"]


@pytest.mark.asyncio
async def test_expand_query_keeps_matching_script_variants(monkeypatch):
    async def fake_call_llm_json(**kwargs):
        return (
            [
                "required documents to obtain an attested FIR copy",
                "FIR reference number and CNIC requirements for certified copy issuance",
            ],
            "raw",
        )

    monkeypatch.setattr("src.pipeline.query_expander.call_llm_json", fake_call_llm_json)
    result = await expand_query("documents required for a certified copy of an FIR", n=2)
    assert len(result) == 2
