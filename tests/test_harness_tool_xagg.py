"""
Tests for src/pipeline/harness/tools/xagg.py (Phase 0, foundation layer).

Covers:
  (a) standardized XAggToolResult/EvidenceChunk shape, one synthetic
      aggregate chunk carrying the deterministic rendering;
  (b) fallback_to_rag permanently False;
  (c) PermissionError -> DENIED, other exceptions -> FAILED;
  (d) raw_summary_text matches orchestrator.py's own per-kind rendering,
      including the "total_count" kind the literal doc text omits;
  (e) the synthetic chunk carries no case_id (inert to the leakage check,
      same as SQL/WEB — matches today's orchestrator XAGG verifier call).
"""
import pytest

import src.pipeline.harness.tools.xagg as xagg_mod
from src.pipeline.harness.tools.xagg import XAggToolInput, XAggToolResult, xagg_tool
from src.pipeline.harness.types import CallerContext, ToolStatus


def _caller(role="supervisor"):
    return CallerContext(user_id="u1", role=role, active_case_id=None)


class _FakeGateway:
    pass


@pytest.fixture(autouse=True)
def stub_gateway(monkeypatch):
    async def _get_gateway():
        return _FakeGateway()
    monkeypatch.setattr(xagg_mod, "get_gateway", _get_gateway)


@pytest.mark.asyncio
async def test_graph_recurrence_renders_and_touches_cases(monkeypatch):
    async def _run_aggregate(query_text, target_entity, gateway, user_id, user_role):
        return {
            "kind": "graph_recurrence", "entity_type": "Vehicle",
            "results": [{"name": "ABC-123", "case_count": 3, "case_ids": ["CASE-001", "CASE-002", "CASE-003"]}],
        }

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)

    result = await xagg_tool(XAggToolInput(query_text="top recurring vehicles across cases", caller=_caller()))

    assert isinstance(result, XAggToolResult)
    assert result.status == ToolStatus.OK
    assert result.fallback_to_rag is False
    assert result.aggregate_kind == "graph_recurrence"
    assert "ABC-123" in result.raw_summary_text
    assert set(result.case_ids_touched) == {"CASE-001", "CASE-002", "CASE-003"}
    assert len(result.chunks) == 1
    assert result.chunks[0].metadata.source_tool == "XAGG"
    # Aggregate chunk carries no case_id — inert to the leakage check, same
    # reasoning already applied to SQL/WEB.
    assert result.chunks[0].metadata.case_id is None


@pytest.mark.asyncio
async def test_total_count_kind_does_not_crash(monkeypatch):
    # The exact kind the literal SUBAGENT_INTERFACES.md AggregateKind text
    # omits (see xagg.py's module docstring) — must not raise.
    async def _run_aggregate(*a, **kw):
        return {"kind": "total_count", "total_cases": 42}

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)

    result = await xagg_tool(XAggToolInput(query_text="how many cases in total", caller=_caller()))

    assert result.status == ToolStatus.OK
    assert result.aggregate_kind == "total_count"
    assert result.raw_summary_text == "Total cases: 42"


@pytest.mark.asyncio
async def test_case_listing_and_relational_aggregate_render(monkeypatch):
    async def _run_aggregate(*a, **kw):
        return {
            "kind": "case_listing",
            "cases": [{"case_id": "CASE-009", "fir_number": "FIR-1", "crime_category": "Theft",
                       "investigation_status": "Open", "police_station": "Central"}],
        }

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)
    result = await xagg_tool(XAggToolInput(query_text="list of all cases", caller=_caller()))
    assert "CASE-009" in result.raw_summary_text
    assert result.case_ids_touched == ["CASE-009"]


@pytest.mark.asyncio
async def test_permission_error_maps_to_denied(monkeypatch):
    async def _run_aggregate(*a, **kw):
        raise PermissionError("Cross-case aggregate queries require supervisor role or higher.")

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)

    result = await xagg_tool(XAggToolInput(query_text="q", caller=_caller(role="investigator")))

    assert result.status == ToolStatus.DENIED
    assert result.error.kind == "permission_denied"
    assert result.chunks == []


@pytest.mark.asyncio
async def test_other_exception_maps_to_failed(monkeypatch):
    async def _run_aggregate(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)

    result = await xagg_tool(XAggToolInput(query_text="q", caller=_caller()))

    assert result.status == ToolStatus.FAILED
    assert result.error.kind == "upstream_failure"


def test_fallback_to_rag_cannot_be_overridden_true():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        XAggToolResult(status=ToolStatus.OK, fallback_to_rag=True)
