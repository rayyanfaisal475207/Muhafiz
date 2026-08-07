"""
Tests for src/pipeline/harness/tools/xnetwork.py (Phase 0, foundation layer).

Covers:
  (a) standardized XNetworkToolResult/EvidenceChunk shape, one chunk per
      community summary;
  (b) fallback_to_rag permanently False;
  (c) PermissionError -> DENIED, other exceptions -> FAILED;
  (d) raw_summary_text is populated for sub-agent-level fallback use
      (this tool does NOT itself call an LLM or the Verifier — the
      XNETWORK-specific cloud-retry belongs one layer up).
"""
import pytest

import src.pipeline.harness.tools.xnetwork as xnetwork_mod
from src.pipeline.harness.tools.xnetwork import XNetworkToolInput, XNetworkToolResult, xnetwork_tool
from src.pipeline.harness.types import CallerContext, ToolStatus


def _caller(role="supervisor"):
    return CallerContext(user_id="u1", role=role, active_case_id=None)


class _FakeGateway:
    pass


@pytest.fixture(autouse=True)
def stub_gateway(monkeypatch):
    async def _get_gateway():
        return _FakeGateway()
    monkeypatch.setattr(xnetwork_mod, "get_gateway", _get_gateway)


@pytest.mark.asyncio
async def test_ok_result_shape(monkeypatch):
    async def _run_network_query(query_text, gateway, user_id, user_role, top_k):
        return {
            "kind": "network_synthesis",
            "results": [
                {"community_id": "COMM-1", "summary_text": "Pattern of vehicle theft across 3 cases.", "case_ids": ["CASE-001", "CASE-002"]},
            ],
            "community_ids": ["COMM-1"],
            "case_ids": ["CASE-001", "CASE-002"],
        }

    monkeypatch.setattr(xnetwork_mod, "run_network_query", _run_network_query)

    result = await xnetwork_tool(XNetworkToolInput(query_text="overall picture", caller=_caller()))

    assert isinstance(result, XNetworkToolResult)
    assert result.status == ToolStatus.OK
    assert result.fallback_to_rag is False
    assert result.community_ids == ["COMM-1"]
    assert result.case_ids_touched == ["CASE-001", "CASE-002"]
    assert len(result.chunks) == 1
    assert result.chunks[0].metadata.source_tool == "XNETWORK"
    assert "Pattern of vehicle theft" in result.raw_summary_text


@pytest.mark.asyncio
async def test_empty_results_maps_to_empty_status(monkeypatch):
    async def _run_network_query(*a, **kw):
        return {"kind": "network_synthesis", "results": [], "community_ids": [], "case_ids": []}

    monkeypatch.setattr(xnetwork_mod, "run_network_query", _run_network_query)

    result = await xnetwork_tool(XNetworkToolInput(query_text="q", caller=_caller()))

    assert result.status == ToolStatus.EMPTY
    assert result.chunks == []
    assert result.raw_summary_text is None


@pytest.mark.asyncio
async def test_permission_error_maps_to_denied(monkeypatch):
    async def _run_network_query(*a, **kw):
        raise PermissionError("Cross-case network queries require supervisor role or higher.")

    monkeypatch.setattr(xnetwork_mod, "run_network_query", _run_network_query)

    result = await xnetwork_tool(XNetworkToolInput(query_text="q", caller=_caller(role="investigator")))

    assert result.status == ToolStatus.DENIED
    assert result.error.kind == "permission_denied"


@pytest.mark.asyncio
async def test_other_exception_maps_to_failed(monkeypatch):
    async def _run_network_query(*a, **kw):
        raise RuntimeError("chroma unavailable")

    monkeypatch.setattr(xnetwork_mod, "run_network_query", _run_network_query)

    result = await xnetwork_tool(XNetworkToolInput(query_text="q", caller=_caller()))

    assert result.status == ToolStatus.FAILED
    assert result.error.kind == "upstream_failure"


def test_fallback_to_rag_cannot_be_overridden_true():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        XNetworkToolResult(status=ToolStatus.OK, fallback_to_rag=True)
