"""
Tests for src/pipeline/harness/tools/web.py (Phase 0, foundation layer).

Covers:
  (a) standardized WebToolResult/EvidenceChunk shape;
  (b) two-tier fallback (Tavily -> Gemini grounded search), fallback_to_rag
      only True after BOTH tiers fail;
  (c) AIR_GAP_MODE blocks BOTH providers, never reaching either;
  (d) domain allowlist filtering applied to the Gemini tier;
  (e) no case scope, no role gate — emitted chunks carry no case_id.
"""
import pytest

import src.pipeline.harness.tools.web as web_mod
from src.pipeline.harness.tools.web import WebToolInput, WebToolResult, web_tool
from src.pipeline.harness.types import CallerContext, ExecutionContext, ToolStatus


def _caller():
    return CallerContext(user_id="u1", role="investigator", active_case_id=None)


def _execution():
    return ExecutionContext(caller=_caller())


@pytest.fixture(autouse=True)
def allowed_domains(monkeypatch):
    monkeypatch.setattr(web_mod.config, "WEB_ALLOWED_DOMAINS", ["gov.pk", "wikipedia.org"])
    monkeypatch.setattr(web_mod.config, "AIR_GAP_MODE", False)


@pytest.mark.asyncio
async def test_tavily_success_returns_ok_without_gemini(monkeypatch):
    async def _tavily(query, max_results=5):
        return [{"title": "Law page", "url": "https://gov.pk/law", "content": "Section text", "score": 0.9}]

    async def _gemini(*a, **kw):
        raise AssertionError("Gemini fallback must not be reached when Tavily succeeds")

    monkeypatch.setattr(web_mod, "perform_web_search", _tavily)
    monkeypatch.setattr(web_mod, "call_gemini_with_search", _gemini)

    result = await web_tool(WebToolInput(query_text="q", execution=_execution()))

    assert isinstance(result, WebToolResult)
    assert result.status == ToolStatus.OK
    assert result.fallback_to_rag is False
    assert result.provider_used == "primary_search"
    assert len(result.chunks) == 1
    assert result.chunks[0].metadata.source_tool == "WEB"
    assert result.chunks[0].metadata.case_id is None


@pytest.mark.asyncio
async def test_tavily_empty_falls_through_to_gemini(monkeypatch):
    async def _tavily(query, max_results=5):
        return []

    async def _gemini(user_message, max_tokens=1500):
        return "Answer text", [{"title": "Wiki", "url": "https://wikipedia.org/x"}]

    monkeypatch.setattr(web_mod, "perform_web_search", _tavily)
    monkeypatch.setattr(web_mod, "call_gemini_with_search", _gemini)

    result = await web_tool(WebToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.OK
    assert result.provider_used == "grounded_search_fallback"
    assert result.fallback_to_rag is False


@pytest.mark.asyncio
async def test_both_tiers_failing_falls_back_to_rag(monkeypatch):
    async def _tavily(query, max_results=5):
        raise RuntimeError("Tavily down")

    async def _gemini(user_message, max_tokens=1500):
        return "", []

    monkeypatch.setattr(web_mod, "perform_web_search", _tavily)
    monkeypatch.setattr(web_mod, "call_gemini_with_search", _gemini)

    result = await web_tool(WebToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.EMPTY
    assert result.fallback_to_rag is True
    assert result.chunks == []


@pytest.mark.asyncio
async def test_gemini_exception_maps_to_failed_and_falls_back(monkeypatch):
    async def _tavily(query, max_results=5):
        return []

    async def _gemini(*a, **kw):
        raise RuntimeError("gemini quota exhausted")

    monkeypatch.setattr(web_mod, "perform_web_search", _tavily)
    monkeypatch.setattr(web_mod, "call_gemini_with_search", _gemini)

    result = await web_tool(WebToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.FAILED
    assert result.fallback_to_rag is True
    assert result.error.kind == "upstream_failure"


@pytest.mark.asyncio
async def test_air_gap_mode_reaches_neither_provider(monkeypatch):
    monkeypatch.setattr(web_mod.config, "AIR_GAP_MODE", True)

    async def _tavily(*a, **kw):
        raise AssertionError("Tavily must not be reached under AIR_GAP_MODE")

    async def _gemini(*a, **kw):
        raise AssertionError("Gemini must not be reached under AIR_GAP_MODE")

    monkeypatch.setattr(web_mod, "perform_web_search", _tavily)
    monkeypatch.setattr(web_mod, "call_gemini_with_search", _gemini)

    result = await web_tool(WebToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.EMPTY
    assert result.fallback_to_rag is True


@pytest.mark.asyncio
async def test_gemini_sources_filtered_to_allowed_domains(monkeypatch):
    async def _tavily(query, max_results=5):
        return []

    async def _gemini(user_message, max_tokens=1500):
        return "answer", [
            {"title": "Allowed", "url": "https://gov.pk/x"},
            {"title": "Not allowed", "url": "https://random-blog.example/y"},
        ]

    monkeypatch.setattr(web_mod, "perform_web_search", _tavily)
    monkeypatch.setattr(web_mod, "call_gemini_with_search", _gemini)

    result = await web_tool(WebToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.OK
    assert len(result.chunks) == 1
    assert result.chunks[0].metadata.source_file == "https://gov.pk/x"


def test_filter_allowed_domains_matches_substring():
    web_mod.config.WEB_ALLOWED_DOMAINS = ["gov.pk"]
    sources = [{"url": "https://gov.pk/a"}, {"url": "https://other.example/a"}]
    assert web_mod._filter_allowed_domains(sources) == [{"url": "https://gov.pk/a"}]
