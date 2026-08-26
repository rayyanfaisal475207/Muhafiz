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


# ═══════════════════════════════════════════════════════════════════════
# GS-1 — a community's own case footprint must survive to the chunk, and
# must stay distinct from the query-level `case_ids_touched` union.
#
# Fixtures use real-corpus IDs and the real 17:2 skew in miniature: two
# single-case communities from DIFFERENT cases plus one genuinely
# multi-case community (live: C-20260825-0005 spans fir-214-26 and
# fir-891-24).
# ═══════════════════════════════════════════════════════════════════════


def _gs1_reports(specs):
    return {
        "kind": "global_search_reports",
        "hierarchy_level": 0,
        "reports": [
            {
                "community_id": cid,
                "summary_text": f"Summary for {cid}.",
                "case_ids": list(cases),
                "member_count": 2,
            }
            for cid, cases in specs
        ],
        "community_ids": [cid for cid, _ in specs],
        "case_ids": sorted({c for _, cases in specs for c in cases}),
    }


def _stub_gs1(monkeypatch, specs):
    async def _run(*a, **kw):
        return _gs1_reports(specs)

    monkeypatch.setattr(global_search_mod, "run_global_search_query", _run)


async def _gs1_run():
    return await global_search_tool(
        GlobalSearchToolInput(query_text="dataset-wide themes", execution=_execution())
    )


@pytest.mark.asyncio
async def test_gs1_single_case_community_keeps_its_own_footprint(monkeypatch):
    """(A) A single-case community is never stamped with the union."""
    _stub_gs1(monkeypatch, [("C-A", ["fir-88-26"])])
    result = await _gs1_run()

    assert result.community_case_ids == [["fir-88-26"]]
    meta = result.chunks[0].metadata
    assert meta.case_id == "fir-88-26"
    assert meta.case_ids == ["fir-88-26"]


@pytest.mark.asyncio
async def test_gs1_multi_case_community_is_not_collapsed(monkeypatch):
    """(B) No arbitrary "first case" — case_id stays None, both IDs kept."""
    _stub_gs1(monkeypatch, [("C-B", ["fir-214-26", "fir-891-24"])])
    result = await _gs1_run()

    assert result.community_case_ids == [["fir-214-26", "fir-891-24"]]
    meta = result.chunks[0].metadata
    assert meta.case_id is None, "a 2-case community must not claim one case"
    assert meta.case_ids == ["fir-214-26", "fir-891-24"]


@pytest.mark.asyncio
async def test_gs1_mixed_top_k_keeps_per_community_attribution(monkeypatch):
    """(C) The real shape: per-community lists stay distinct from the union."""
    specs = [
        ("C-A", ["fir-88-26"]),
        ("C-B", ["fir-117-26"]),
        ("C-C", ["fir-214-26", "fir-891-24"]),
    ]
    _stub_gs1(monkeypatch, specs)
    result = await _gs1_run()

    assert result.community_case_ids == [
        ["fir-88-26"],
        ["fir-117-26"],
        ["fir-214-26", "fir-891-24"],
    ]
    # Index alignment with chunks/community_ids is the whole contract.
    assert len(result.community_case_ids) == len(result.chunks) == len(result.community_ids)

    # The union is a SEPARATE contract and stays the union.
    assert result.case_ids_touched == ["fir-117-26", "fir-214-26", "fir-88-26", "fir-891-24"]

    # No single-case chunk carries the union.
    for chunk, expected in zip(result.chunks, [c for _, c in specs]):
        assert chunk.metadata.case_ids == expected
        assert chunk.metadata.case_ids != result.case_ids_touched


@pytest.mark.asyncio
async def test_gs1_missing_case_ids_yields_empty_not_union(monkeypatch):
    """(D) An absent footprint degrades to [] — never to the union."""
    _stub_gs1(monkeypatch, [("C-A", []), ("C-B", ["fir-88-26"])])
    result = await _gs1_run()

    assert result.community_case_ids == [[], ["fir-88-26"]]
    assert result.chunks[0].metadata.case_ids == []
    assert result.chunks[0].metadata.case_id is None
    assert result.case_ids_touched == ["fir-88-26"]
