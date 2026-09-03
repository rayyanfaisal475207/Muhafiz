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
    monkeypatch.setattr(xagg_mod, "get_gateway", _get_gateway)


@pytest.mark.asyncio
async def test_graph_recurrence_renders_and_touches_cases(monkeypatch):
    async def _run_aggregate(query_text, target_entity, gateway, user_id, user_role):
        return {
            "kind": "graph_recurrence", "entity_type": "Vehicle",
            "results": [{"name": "ABC-123", "case_count": 3, "case_ids": ["CASE-001", "CASE-002", "CASE-003"]}],
        }

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)

    result = await xagg_tool(XAggToolInput(query_text="top recurring vehicles across cases", execution=_execution()))

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

    result = await xagg_tool(XAggToolInput(query_text="how many cases in total", execution=_execution()))

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
    result = await xagg_tool(XAggToolInput(query_text="list of all cases", execution=_execution()))
    assert "CASE-009" in result.raw_summary_text
    assert result.case_ids_touched == ["CASE-009"]


@pytest.mark.asyncio
async def test_counts_by_act_renders_as_additional_breakdown(monkeypatch):
    """[Legal-code semantic layer] Kept in sync with orchestrator.py's own
    identical rendering — see _render_aggregate_text()'s own comment."""
    async def _run_aggregate(*a, **kw):
        return {
            "kind": "relational_aggregate", "group_by": "crime_category",
            "counts": [
                {"key": "PPC, Arms Ordinance 1965", "count": 21},
                {"key": "CNSA 1997, Arms Ordinance 1965", "count": 8},
            ],
            "counts_by_act": [
                {"key": "Arms Ordinance 1965", "count": 29},
                {"key": "PPC", "count": 21},
                {"key": "CNSA 1997", "count": 8},
            ],
        }

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)
    result = await xagg_tool(XAggToolInput(query_text="how many arms ordinance cases", execution=_execution()))

    assert "PPC, Arms Ordinance 1965: 21 cases" in result.raw_summary_text
    assert "Breakdown by individual legal code" in result.raw_summary_text
    assert "Arms Ordinance 1965: 29 cases" in result.raw_summary_text


@pytest.mark.asyncio
async def test_permission_error_maps_to_denied(monkeypatch):
    async def _run_aggregate(*a, **kw):
        raise PermissionError("Cross-case aggregate queries require supervisor role or higher.")

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)

    result = await xagg_tool(XAggToolInput(query_text="q", execution=_execution(role="investigator")))

    assert result.status == ToolStatus.DENIED
    assert result.error.kind == "permission_denied"
    assert result.chunks == []


@pytest.mark.asyncio
async def test_other_exception_maps_to_failed(monkeypatch):
    async def _run_aggregate(*a, **kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)

    result = await xagg_tool(XAggToolInput(query_text="q", execution=_execution()))

    assert result.status == ToolStatus.FAILED
    assert result.error.kind == "upstream_failure"


def test_fallback_to_rag_cannot_be_overridden_true():
    from pydantic import ValidationError
    with pytest.raises(ValidationError):
        XAggToolResult(status=ToolStatus.OK, fallback_to_rag=True)


# ── Finding W regression: enumerating aggregates must lead with a total ──────
# When the natural-language paraphrase fails verification, the user is shown
# `raw_summary_text` instead. For a "how many cases involve PPC?" question
# that rendered 60+ bullet rows with no number anywhere, so the question was
# never actually answered — the reader had to count the list (verify-log
# Finding W). Enumerating branches now lead with their own count, derived from
# the same rows so it cannot disagree with them.

from src.pipeline.harness.tools.xagg import _render_aggregate_text


def _case(case_id):
    return {
        "case_id": case_id, "fir_number": case_id.split("-")[1] + "/26",
        "crime_category": "PPC", "investigation_status": None,
        "police_station": "Model Town",
    }


def test_case_listing_leads_with_total():
    text = _render_aggregate_text(
        {"kind": "case_listing", "cases": [_case("fir-201-26"), _case("fir-202-26")]}
    )
    assert text.startswith("**2 matching case(s) found.**")
    # The enumeration itself is still present.
    assert "fir-201-26" in text and "fir-202-26" in text


def test_case_listing_total_matches_row_count():
    cases = [_case(f"fir-{200 + i}-26") for i in range(37)]
    text = _render_aggregate_text({"kind": "case_listing", "cases": cases})
    assert text.startswith("**37 matching case(s) found.**")
    assert text.count("\n- ") == 37


def test_graph_recurrence_leads_with_total():
    text = _render_aggregate_text({
        "kind": "graph_recurrence",
        "entity_type": "Person",
        "results": [
            {"name": "A", "case_count": 2, "case_ids": ["fir-1-26", "fir-2-26"]},
            {"name": "B", "case_count": 3, "case_ids": ["fir-3-26"]},
        ],
    })
    assert text.startswith("**2 matching Person(s) found.**")


def test_empty_listing_has_no_bogus_zero_header():
    """An empty result must keep its existing wording, not say '0 matching'."""
    assert _render_aggregate_text({"kind": "case_listing", "cases": []}).startswith(
        "(no matching cases found)"
    )


def test_total_count_branch_is_unchanged():
    text = _render_aggregate_text({"kind": "total_count", "total_cases": 61})
    assert "Total cases: 61" in text


# ── Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 1: this wrapper is a
# SEPARATE, hand-maintained copy of orchestrator.py's rendering (per this
# module's own docstring) — orchestrator.py already handling a new kind
# does NOT mean this file does. Live-confirmed bug this closes: every one
# of these five kinds either failed XAggToolResult's Literal validation
# or crashed _render_aggregate_text() with a bare KeyError the moment
# XAGG reached this wrapper — i.e. in any deployment with XAGG in
# HARNESS_CUTOVER_ROUTES (this project's own live config). ────────────

@pytest.mark.asyncio
async def test_unsupported_aggregate_renders_the_refusal_message(monkeypatch):
    async def _run_aggregate(*a, **kw):
        return {"kind": "unsupported_aggregate", "message": "Age-based aggregates are not available."}

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)
    result = await xagg_tool(XAggToolInput(query_text="average age of accused", execution=_execution()))

    assert result.status == ToolStatus.OK
    assert result.aggregate_kind == "unsupported_aggregate"
    assert result.raw_summary_text == "Age-based aggregates are not available."


@pytest.mark.asyncio
async def test_total_accused_count_renders(monkeypatch):
    async def _run_aggregate(*a, **kw):
        return {"kind": "total_accused_count", "total_accused": 92, "total_case_scoped_entries": 94}

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)
    result = await xagg_tool(XAggToolInput(query_text="how many accused in total", execution=_execution()))

    assert result.status == ToolStatus.OK
    assert result.aggregate_kind == "total_accused_count"
    assert result.raw_summary_text == "Total distinct accused persons: 92"


@pytest.mark.asyncio
async def test_gender_breakdown_renders_when_supported(monkeypatch):
    async def _run_aggregate(*a, **kw):
        return {
            "kind": "gender_breakdown", "unsupported": False,
            "counts": [{"key": "male", "count": 65}, {"key": "female", "count": 24}],
            "total_accused": 92,
        }

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)
    result = await xagg_tool(XAggToolInput(query_text="how many women accused", execution=_execution()))

    assert result.status == ToolStatus.OK
    assert "male: 65" in result.raw_summary_text
    assert "female: 24" in result.raw_summary_text
    assert "Total accused: 92" in result.raw_summary_text


@pytest.mark.asyncio
async def test_gender_breakdown_renders_the_not_yet_synced_message(monkeypatch):
    async def _run_aggregate(*a, **kw):
        return {"kind": "gender_breakdown", "unsupported": True, "message": "Gender is not yet recorded."}

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)
    result = await xagg_tool(XAggToolInput(query_text="how many women accused", execution=_execution()))

    assert result.raw_summary_text == "Gender is not yet recorded."


@pytest.mark.asyncio
async def test_district_breakdown_renders(monkeypatch):
    async def _run_aggregate(*a, **kw):
        return {
            "kind": "district_breakdown", "entity_label": None,
            "counts": [{"district": "Lahore", "count": 18}, {"district": "Faisalabad", "count": 19}],
        }

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)
    result = await xagg_tool(XAggToolInput(query_text="which district has the most FIRs", execution=_execution()))

    assert "Lahore: 18 case(s)" in result.raw_summary_text
    assert "Faisalabad: 19 case(s)" in result.raw_summary_text


@pytest.mark.asyncio
async def test_district_breakdown_with_entity_label_renders(monkeypatch):
    async def _run_aggregate(*a, **kw):
        return {
            "kind": "district_breakdown", "entity_label": "Weapon",
            "counts": [{"district": "Lahore", "count": 3}],
        }

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)
    result = await xagg_tool(XAggToolInput(query_text="which district recovers the most weapons", execution=_execution()))

    assert "Lahore: 3 Weapon record(s)" in result.raw_summary_text


@pytest.mark.asyncio
async def test_station_total_count_renders(monkeypatch):
    async def _run_aggregate(*a, **kw):
        return {"kind": "station_total_count", "total_stations": 19}

    monkeypatch.setattr(xagg_mod, "run_aggregate", _run_aggregate)
    result = await xagg_tool(XAggToolInput(query_text="how many police stations are there", execution=_execution()))

    assert result.raw_summary_text == "Total police stations: 19"
