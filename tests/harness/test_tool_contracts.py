"""
Tool-primitive contract tests.

Asserts the schema properties SUBAGENT_INTERFACES.md §1 pins down — especially
the ones a well-meaning refactor could silently invert:
  * `fallback_to_rag` polarity per tool
  * cross-case tools NEVER falling back
  * the cross-case role gate ordering (check → audit → proceed)
  * `source_tool` populated, and GRAPH_HYBRID distinguishable from GRAPH
"""
from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.pipeline.harness.contracts import (
    SOURCE_TOOL_DISPLAY_LABELS,
    CallerContext,
    CrossCaseToolResult,
    GraphToolInput,
    RagToolInput,
    RagToolResult,
    Role,
    SqlToolInput,
    ToolStatus,
    WebToolInput,
    XAggToolInput,
    XAggToolResult,
    XGraphToolInput,
    XGraphToolResult,
    XNetworkToolInput,
    XNetworkToolResult,
)
from src.pipeline.harness.tools.stubs import (
    graph_tool,
    rag_tool,
    sql_tool,
    web_tool,
    xagg_tool,
    xgraph_tool,
    xnetwork_tool,
)


def _caller(role: Role = Role.INVESTIGATOR) -> CallerContext:
    return CallerContext(user_id="u1", role=role, active_case_id="CASE-A1B2C3D4")


def _supervisor() -> CallerContext:
    return CallerContext(user_id="u2", role=Role.SUPERVISOR, active_case_id="CASE-A1B2C3D4")


# ── source_tool tagging ──────────────────────────────────────────────────

async def test_every_chunk_carries_source_tool():
    result = await rag_tool(RagToolInput(query_text="q", caller=_caller()))

    assert result.chunks
    assert all(c.metadata.source_tool == "RAG" for c in result.chunks)


async def test_graph_hybrid_is_distinguishable_from_plain_graph():
    """[RESOLVED-1a] Hybrid must be structurally distinct, not just differently scored."""
    plain = await graph_tool(GraphToolInput(query_text="q", caller=_caller(), hybrid=False))
    hybrid = await graph_tool(GraphToolInput(query_text="q", caller=_caller(), hybrid=True))

    assert {c.metadata.source_tool for c in plain.chunks} == {"GRAPH"}
    assert {c.metadata.source_tool for c in hybrid.chunks} == {"GRAPH_HYBRID"}
    assert len(hybrid.chunks) > len(plain.chunks), "hybrid should fuse in document evidence"


def test_display_labels_cover_every_source_tool_and_are_distinct():
    from typing import get_args

    from src.pipeline.harness.contracts import SourceTool

    assert set(SOURCE_TOOL_DISPLAY_LABELS) == set(get_args(SourceTool))
    assert SOURCE_TOOL_DISPLAY_LABELS["GRAPH_HYBRID"] != SOURCE_TOOL_DISPLAY_LABELS["GRAPH"]
    assert len(set(SOURCE_TOOL_DISPLAY_LABELS.values())) == len(SOURCE_TOOL_DISPLAY_LABELS)


# ── fallback_to_rag polarity ─────────────────────────────────────────────

async def test_graph_signals_fallback_on_empty():
    result = await graph_tool(GraphToolInput(query_text="__empty__", caller=_caller()))

    assert result.status is ToolStatus.EMPTY
    assert result.fallback_to_rag is True


async def test_graph_signals_fallback_on_failure():
    result = await graph_tool(GraphToolInput(query_text="__fail__", caller=_caller()))

    assert result.status is ToolStatus.FAILED
    assert result.fallback_to_rag is True


async def test_sql_signals_fallback_on_empty():
    result = await sql_tool(SqlToolInput(query_text="__empty__", caller=_caller()))

    assert result.fallback_to_rag is True


async def test_web_signals_fallback_only_after_both_tiers():
    result = await web_tool(WebToolInput(query_text="__empty__", caller=_caller()))

    assert result.fallback_to_rag is True


async def test_web_disabled_under_air_gap_before_any_provider():
    result = await web_tool(
        WebToolInput(query_text="q", caller=_caller()), air_gap_mode=True
    )

    assert result.status is ToolStatus.FAILED
    assert result.provider_used is None, "no provider may be reached under air-gap mode"


async def test_rag_never_falls_back():
    """RAG is the fallback TARGET — it has no onward fallback."""
    ok = await rag_tool(RagToolInput(query_text="q", caller=_caller()))
    empty = await rag_tool(RagToolInput(query_text="__empty__", caller=_caller()))

    assert ok.fallback_to_rag is False
    assert empty.fallback_to_rag is False


def test_rag_result_cannot_be_constructed_with_fallback_true():
    with pytest.raises(ValidationError):
        RagToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True)


# ── Cross-case: never fall back ──────────────────────────────────────────

@pytest.mark.parametrize("model", [XGraphToolResult, XAggToolResult, XNetworkToolResult])
def test_cross_case_results_cannot_declare_fallback(model):
    """
    [PRESERVE — design §2.3/§2.4/§2.5] Pinned False at the type level, so a
    future edit reintroducing cross-case→RAG bleed fails construction.
    """
    with pytest.raises(ValidationError):
        model(status=ToolStatus.EMPTY, fallback_to_rag=True)


@pytest.mark.parametrize("model", [XGraphToolResult, XAggToolResult, XNetworkToolResult])
def test_cross_case_results_subclass_the_cross_case_base(model):
    assert issubclass(model, CrossCaseToolResult)
    assert model(status=ToolStatus.EMPTY).fallback_to_rag is False


# ── Cross-case: role gate ordering ───────────────────────────────────────

@pytest.mark.parametrize(
    "tool,input_model",
    [
        (xgraph_tool, XGraphToolInput),
        (xagg_tool, XAggToolInput),
        (xnetwork_tool, XNetworkToolInput),
    ],
)
async def test_cross_case_tools_deny_investigator(tool, input_model, gateway):
    result = await tool(input_model(query_text="q", caller=_caller()), gateway=gateway)

    assert result.status is ToolStatus.DENIED
    assert result.error.kind == "permission_denied"
    assert result.chunks == [], "a denied call must not return evidence"


@pytest.mark.parametrize(
    "tool,input_model",
    [
        (xgraph_tool, XGraphToolInput),
        (xagg_tool, XAggToolInput),
        (xnetwork_tool, XNetworkToolInput),
    ],
)
async def test_denial_writes_authorization_violation_audit(tool, input_model, gateway):
    """[PRESERVE — design §4.3] Audit written BEFORE returning, on the denial path."""
    await tool(input_model(query_text="q", caller=_caller()), gateway=gateway)

    events = [e for e in gateway.audit_log if e["event_type"] == "authorization_violation"]
    assert len(events) == 1
    assert events[0]["details"]["role"] == "investigator"


@pytest.mark.parametrize(
    "tool,input_model",
    [
        (xgraph_tool, XGraphToolInput),
        (xagg_tool, XAggToolInput),
        (xnetwork_tool, XNetworkToolInput),
    ],
)
async def test_cross_case_tools_allow_supervisor(tool, input_model, gateway):
    result = await tool(input_model(query_text="q", caller=_supervisor()), gateway=gateway)

    assert result.status is ToolStatus.OK
    assert not [e for e in gateway.audit_log if e["event_type"] == "authorization_violation"]


async def test_cross_case_results_report_case_ids_touched(gateway):
    """[PRESERVE — design §4.6] Feeds the Verifier's allowed-cross-case-ID list."""
    result = await xgraph_tool(
        XGraphToolInput(query_text="q", caller=_supervisor()), gateway=gateway
    )

    assert len(result.case_ids_touched) > 1


async def test_xagg_provides_raw_summary_for_verifier_rejection_path(gateway):
    """[PRESERVE — design §2.4] The machine-computed aggregate is served, not an abstention."""
    result = await xagg_tool(XAggToolInput(query_text="q", caller=_supervisor()), gateway=gateway)

    assert result.raw_summary_text


# ── Reference/web tools are not case evidence ────────────────────────────

@pytest.mark.parametrize(
    "tool,input_model", [(sql_tool, SqlToolInput), (web_tool, WebToolInput)]
)
async def test_reference_tools_emit_chunks_with_no_owning_case(tool, input_model):
    result = await tool(input_model(query_text="q", caller=_caller()))

    assert result.chunks
    assert all(c.metadata.case_id is None for c in result.chunks)
