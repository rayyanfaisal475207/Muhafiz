"""
Tests for src/pipeline/harness/tools/global_search.py (findings.md
Module 9, Stage 1).

Covers:
  (a) standardized GlobalSearchToolResult/EvidenceChunk shape, one chunk
      per community report, source_tool="XNETWORK" (see that module's own
      "SourceTool TAGGING" docstring section for why);
  (b) fallback_to_rag permanently False;
  (c) PermissionError -> DENIED, other exceptions -> FAILED;
  (d) report_count_total reflects the pre-cap count (the composing
      sub-agent's own MAX_REPORTS_SAMPLE cap is applied one layer up, not
      here — this tool never trims);
  (e) hierarchy_level/community_ids pass through from
      run_global_search_query()'s own return shape.
"""
import pytest

import src.pipeline.harness.tools.global_search as global_search_mod
from src.pipeline.harness.tools.global_search import (
    GlobalSearchToolInput,
    GlobalSearchToolResult,
    global_search_tool,
)
from src.pipeline.harness.types import CallerContext, ExecutionContext, ToolStatus


def _caller(role="supervisor"):
    return CallerContext(user_id="u1", role=role, active_case_id=None)


def _execution(role="supervisor"):
    return ExecutionContext(caller=_caller(role))


class _FakeGateway:
    pass


@pytest.fixture(autouse=True)
def stub_gateway(monkeypatch):
    async def _get_gateway():
        return _FakeGateway()
    monkeypatch.setattr(global_search_mod, "get_gateway", _get_gateway)


@pytest.mark.asyncio
async def test_ok_result_shape(monkeypatch):
    async def _run_global_search_query(query_text, gateway, user_id, user_role, hierarchy_level, jurisdiction_case_ids=None):
        return {
            "kind": "global_search_reports",
            "hierarchy_level": 0,
            "reports": [
                {"community_id": "C-1", "summary_text": "Pattern of vehicle theft across 3 cases.", "case_ids": ["CASE-001", "CASE-002"], "member_count": 4},
                {"community_id": "C-2", "summary_text": "Cluster tied to phone-fraud calls.", "case_ids": ["CASE-003"], "member_count": 2},
            ],
            "community_ids": ["C-1", "C-2"],
            "case_ids": ["CASE-001", "CASE-002", "CASE-003"],
        }

    monkeypatch.setattr(global_search_mod, "run_global_search_query", _run_global_search_query)

    result = await global_search_tool(GlobalSearchToolInput(query_text="top themes", execution=_execution()))

    assert isinstance(result, GlobalSearchToolResult)
    assert result.status == ToolStatus.OK
    assert result.fallback_to_rag is False
    assert result.hierarchy_level == 0
    assert result.community_ids == ["C-1", "C-2"]
    assert result.case_ids_touched == ["CASE-001", "CASE-002", "CASE-003"]
    assert result.report_count_total == 2
    assert len(result.chunks) == 2
    assert all(c.metadata.source_tool == "XNETWORK" for c in result.chunks)
    assert result.chunks[0].text == "Pattern of vehicle theft across 3 cases."


@pytest.mark.asyncio
async def test_empty_reports_maps_to_empty_status(monkeypatch):
    async def _run_global_search_query(*a, **kw):
        return {"kind": "global_search_reports", "hierarchy_level": 0, "reports": [], "community_ids": [], "case_ids": []}

    monkeypatch.setattr(global_search_mod, "run_global_search_query", _run_global_search_query)

    result = await global_search_tool(GlobalSearchToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.EMPTY
    assert result.chunks == []
    assert result.report_count_total == 0


@pytest.mark.asyncio
async def test_permission_error_maps_to_denied(monkeypatch):
    async def _run_global_search_query(*a, **kw):
        raise PermissionError("Global search queries require supervisor role or higher.")

    monkeypatch.setattr(global_search_mod, "run_global_search_query", _run_global_search_query)

    result = await global_search_tool(
        GlobalSearchToolInput(query_text="q", execution=_execution(role="investigator"))
    )

    assert result.status == ToolStatus.DENIED
    assert result.error.kind == "permission_denied"


@pytest.mark.asyncio
async def test_other_exception_maps_to_failed(monkeypatch):
    async def _run_global_search_query(*a, **kw):
        raise RuntimeError("postgres unavailable")

    monkeypatch.setattr(global_search_mod, "run_global_search_query", _run_global_search_query)

    result = await global_search_tool(GlobalSearchToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.FAILED
    assert result.error.kind == "upstream_failure"


def test_fallback_to_rag_cannot_be_overridden_true():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        GlobalSearchToolResult(status=ToolStatus.OK, fallback_to_rag=True)
