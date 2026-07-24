"""
Tests for src/pipeline/router.py (Phase 5.1 extension).

Guards the exact regression the Phase 5 spec calls out by name: the
`if route not in [...]` allowlist must learn every new route name or it
silently coerces GRAPH/GRAPH_HYBRID/XGRAPH/XAGG back to RAG. Also guards
case_scope defaulting (case-scoped is the default; only XGRAPH/XAGG are
ever cross-case — a GRAPH route can never carry case_scope="cross_case")
and that the exception-fallback dict stays at parity with the success dict.
"""
import json

import pytest

import src.pipeline.router as router


async def _route(monkeypatch, response_json: str) -> dict:
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return response_json

    monkeypatch.setattr(router, "call_llm", fake_call_llm)
    return await router.route_query("some query")


# ── New route names must survive the allowlist ──────────────────────────────

@pytest.mark.parametrize("route_name", ["GRAPH", "GRAPH_HYBRID", "XGRAPH", "XAGG", "DIRECT", "RAG", "WEB", "SQL"])
async def test_every_documented_route_survives_the_guard(monkeypatch, route_name):
    result = await _route(monkeypatch, json.dumps({"route": route_name}))
    assert result["route"] == route_name


async def test_unknown_route_still_defaults_to_rag(monkeypatch):
    """Regression guard for the coercion bug itself: an unlisted route name must not slip through as-is."""
    result = await _route(monkeypatch, json.dumps({"route": "NOT_A_REAL_ROUTE"}))
    assert result["route"] == "RAG"


async def test_route_matching_is_case_insensitive(monkeypatch):
    result = await _route(monkeypatch, json.dumps({"route": "xgraph"}))
    assert result["route"] == "XGRAPH"


# ── case_scope: within_case is the default; only XGRAPH/XAGG are cross_case ─

async def test_case_scope_defaults_to_within_case_when_absent(monkeypatch):
    result = await _route(monkeypatch, json.dumps({"route": "GRAPH"}))
    assert result["case_scope"] == "within_case"


async def test_xgraph_can_be_cross_case(monkeypatch):
    result = await _route(monkeypatch, json.dumps({"route": "XGRAPH", "case_scope": "cross_case"}))
    assert result["case_scope"] == "cross_case"


async def test_xagg_can_be_cross_case(monkeypatch):
    result = await _route(monkeypatch, json.dumps({"route": "XAGG", "case_scope": "cross_case"}))
    assert result["case_scope"] == "cross_case"


@pytest.mark.parametrize("route_name", ["GRAPH", "GRAPH_HYBRID", "RAG", "DIRECT", "SQL", "WEB"])
async def test_non_cross_case_routes_can_never_carry_cross_case_scope(monkeypatch, route_name):
    """
    Cross-case is only ever reachable through XGRAPH/XAGG's structurally
    separate path (spec: "cross-case is explicit and never silent") — even
    if the LLM mistakenly emits case_scope=cross_case alongside a
    case-scoped route, the router must correct it, not pass it through.
    """
    result = await _route(monkeypatch, json.dumps({"route": route_name, "case_scope": "cross_case"}))
    assert result["case_scope"] == "within_case"


async def test_invalid_case_scope_value_defaults_to_within_case(monkeypatch):
    result = await _route(monkeypatch, json.dumps({"route": "XGRAPH", "case_scope": "everything"}))
    assert result["case_scope"] == "within_case"


# ── target_entity passthrough ────────────────────────────────────────────────

async def test_target_entity_is_passed_through_verbatim(monkeypatch):
    result = await _route(monkeypatch, json.dumps({"route": "GRAPH", "target_entity": "0372-1590538"}))
    assert result["target_entity"] == "0372-1590538"


async def test_target_entity_defaults_to_none(monkeypatch):
    result = await _route(monkeypatch, json.dumps({"route": "RAG"}))
    assert result["target_entity"] is None


# ── Fallback dict parity ─────────────────────────────────────────────────────

async def test_unparseable_response_falls_back_with_full_field_parity(monkeypatch):
    """
    Regression: the exception-fallback dict previously lacked target_year
    (present on the success path) — any field the success path returns
    must also exist on the fallback path, or a caller reading it KeyErrors
    only on the rare failure branch.
    """
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return "not json at all {{{"

    monkeypatch.setattr(router, "call_llm", fake_call_llm)
    result = await router.route_query("some query")

    assert result["route"] == "RAG"
    assert result["case_scope"] == "within_case"
    assert result["target_entity"] is None
    assert "target_year" in result
    assert "output_format" in result
    assert "confidence" in result
    assert "reason" in result
