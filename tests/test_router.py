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
    assert result["station"] is None
    assert result["district"] is None


# ── Milestone E1: station/district (only meaningful for XGRAPH/XAGG/XNETWORK) ─

async def test_station_and_district_pass_through_for_xagg(monkeypatch):
    result = await _route(monkeypatch, json.dumps({
        "route": "XAGG", "case_scope": "cross_case", "station": "Iqbal Town", "district": "Lahore",
    }))
    assert result["station"] == "Iqbal Town"
    assert result["district"] == "Lahore"


async def test_station_and_district_default_to_none_when_absent(monkeypatch):
    result = await _route(monkeypatch, json.dumps({"route": "XGRAPH", "case_scope": "cross_case"}))
    assert result["station"] is None
    assert result["district"] is None


@pytest.mark.parametrize("route_name", ["GRAPH", "GRAPH_HYBRID", "RAG", "DIRECT", "SQL", "WEB"])
async def test_station_and_district_forced_to_none_for_non_cross_case_routes(monkeypatch, route_name):
    """
    Even if the LLM mistakenly emits station/district alongside a
    case-scoped route, the router must correct it — same discipline as
    case_scope's own "never cross_case for a case-scoped route" guard
    above, since jurisdiction narrowing only ever applies before
    cross-case work runs.
    """
    result = await _route(monkeypatch, json.dumps({
        "route": route_name, "station": "Iqbal Town", "district": "Lahore",
    }))
    assert result["station"] is None
    assert result["district"] is None


# ── Deterministic pre-classification override (2026-08-04) ──────────────────
#
# Guards the live-confirmed failure: the LLM classifier reliably defaulted
# these exact query shapes to RAG (in English, Urdu script, and Roman-Urdu)
# even with calibrating few-shot examples. These queries must never reach
# the LLM at all — call_llm must not even be invoked — so a fake that
# raises proves the override fired without it.

async def _no_llm_call(*args, **kwargs):
    raise AssertionError("LLM must not be called — the deterministic override should have short-circuited")


@pytest.mark.parametrize("query,expected_route", [
    ("Which police stations have the most open theft cases?", "XAGG"),
    ("How many recurring vehicles have appeared across multiple cases?", "XAGG"),
    ("What are the top recurring vehicles across all cases this year?", "XAGG"),
    ("How many cases are there in total?", "XAGG"),
    ("List of all cases", "XAGG"),
    ("بند کیسز کی تعداد بتائیں", "XAGG"),
    ("band cases kitne hain", "XAGG"),
    ("kitni gariyan bar bar cases mein aayi hain", "XAGG"),
    ("Has this phone number appeared in other cases?", "XGRAPH"),
    ("Is this suspect a repeat offender?", "XGRAPH"),
    ("کسی اور کیس میں بھی ملوث رہا ہے؟", "XGRAPH"),
    ("kya number 0372-1590538 kisi aur case mein bhi aya hai", "XGRAPH"),
    ("Which persons have appeared as suspects in multiple cases?", "XAGG"),
    ("Are there vehicles involved in more than one case?", "XAGG"),
])
async def test_deterministic_override_fires_for_confirmed_failure_patterns(monkeypatch, query, expected_route):
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query(query)
    assert result["route"] == expected_route
    assert result["case_scope"] == "cross_case"
    assert result["confidence"] == "high"


# ── Bug fix: XGRAPH's override must not discard a CNIC/phone/plate that
# names the actual instance being asked about — target_entity=None
# unconditionally meant graph_retriever._seed_candidates() had nothing to
# seed a traversal from, guaranteeing a false "no connections found" for
# any entity this override's own trigger vocabulary ("elsewhere," "other
# cases") fires on. Found via a live full-route sweep against real graph
# data: a real Person confirmed (via direct Cypher) to recur across two
# real cases still came back "no connections found" through this path. ──

async def test_xgraph_override_extracts_a_phone_number_into_target_entity(monkeypatch):
    """The exact case this codebase's OWN test suite already parametrized
    above ('kya number 0372-1590538 kisi aur case mein bhi aya hai') --
    it asserted the route but never that the phone number survived. It
    didn't, before this fix."""
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query("kya number 0372-1590538 kisi aur case mein bhi aya hai")
    assert result["route"] == "XGRAPH"
    assert result["target_entity"] == "0372-1590538"


@pytest.mark.parametrize("query", [
    "Has this phone number appeared in other cases?",
    "Is this suspect a repeat offender?",
    "کسی اور کیس میں بھی ملوث رہا ہے؟",
])
async def test_xgraph_override_leaves_target_entity_null_with_no_identifier_in_text(monkeypatch, query):
    """Per router.txt's own XGRAPH definition: target_entity stays null
    when the query asks about recurrence/enumeration with no specific
    instance NAMED in the text -- correct today and must stay correct;
    this fix must not start guessing an entity out of thin air."""
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query(query)
    assert result["route"] == "XGRAPH"
    assert result["target_entity"] is None


@pytest.mark.parametrize("query", [
    "What PPC section covers mobile phone theft?",
    "Is cyber harassment a cognizable offense?",
    "What PPC section applies to burglary?",
    "What section covers cyber harassment?",
    "Is theft of a motorcycle a cognizable offense?",
])
async def test_deterministic_override_fires_for_sql_patterns(monkeypatch, query):
    """
    Added alongside the XAGG/XGRAPH/XNETWORK overrides above, for the same
    confirmed-live failure class one route later: these exact queries
    (including two of router.txt's own few-shot examples, verbatim)
    reliably misrouted to RAG in live pipeline testing — not a JSON-
    validation bug (that was fixed separately), a genuine classification-
    reliability gap in the local model for this prompt shape, reproduced
    across independent live test runs. SQL is within-case by design
    (a penal-code lookup isn't a cross-case concept), unlike the
    XAGG/XGRAPH/XNETWORK overrides above.
    """
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query(query)
    assert result["route"] == "SQL"
    assert result["case_scope"] == "within_case"
    assert result["confidence"] == "high"


async def test_deterministic_override_does_not_fire_for_an_active_case(monkeypatch):
    """
    'how many...cases' matches the XAGG pattern textually, but a named
    case/FIR anchors this as a within-case GRAPH question instead (per
    router.txt) — the override must not hijack it. Falls through to the LLM.
    """
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return json.dumps({"route": "GRAPH", "case_scope": "within_case"})

    monkeypatch.setattr(router, "call_llm", fake_call_llm)
    result = await router.route_query("How many cases like this has CASE-009 been linked to before?")
    assert result["route"] == "GRAPH"


# ── Bug fix: an ACTIVE case_id must suppress the override too, not just a
# literal case number typed in the query text (found via a live full-route
# sweep — "how many accused are involved in this case" with a real case_id
# on the request still misrouted to XAGG, since _ACTIVE_CASE_RE only ever
# looked at the query text) ──────────────────────────────────────────────

async def test_deterministic_override_does_not_fire_when_a_case_id_is_active(monkeypatch):
    """
    The exact reproduction: router.txt's own GRAPH few-shot example
    ("How many accused are involved in CASE-009 and how are they
    connected?"), reworded to say "this case" instead of the literal
    case number -- the natural phrasing once a case is already active --
    must still fall through to the LLM (GRAPH), not get hijacked by the
    XAGG override just because no case number appears in the text.
    """
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return json.dumps({"route": "GRAPH", "case_scope": "within_case"})

    monkeypatch.setattr(router, "call_llm", fake_call_llm)
    result = await router.route_query(
        "How many accused are involved in this case and how are they connected?",
        case_id="fir-97-26",
    )
    assert result["route"] == "GRAPH"


async def test_deterministic_override_still_fires_without_an_active_case_id(monkeypatch):
    """Regression guard: the real cross-case behavior this override exists
    for must be unaffected when no case is active — the fix must not have
    just disabled the override outright."""
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query("How many closed cases are there in total?", case_id=None)
    assert result["route"] == "XAGG"


async def test_sql_override_fires_regardless_of_an_active_case_id(monkeypatch):
    """SQL's override is orthogonal to case context by design (see its own
    comment in router.py) -- an active case_id must not suppress it."""
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query("What PPC section covers mobile phone theft?", case_id="fir-97-26")
    assert result["route"] == "SQL"


@pytest.mark.parametrize("query", [
    "Summarize the FIR for this case.",
    "Who is connected to the accused in CASE-009?",
    "Hello",
])
async def test_deterministic_override_does_not_fire_for_unrelated_queries(monkeypatch, query):
    """
    Ordinary DIRECT/RAG/GRAPH-shaped queries must still reach the LLM
    classifier. "What PPC section covers mobile phone theft?" used to be
    in this list — it now correctly DOES fire a deterministic override
    (see test_deterministic_override_fires_for_sql_patterns above), so it
    moved there instead of being a regression.
    """
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return json.dumps({"route": "RAG"})

    monkeypatch.setattr(router, "call_llm", fake_call_llm)
    result = await router.route_query(query)
    assert result["route"] == "RAG"  # came from the fake LLM, not an override short-circuit


# ── prompts/router.txt few-shot consistency guard ────────────────────────
#
# Regression: the JSON schema declares "station"/"district" as
# always-present fields (null when not applicable), but ~6 few-shot
# examples omitted them entirely from their example output — a real
# inconsistency in the prompt's own calibration data that could teach the
# model these fields are optional to drop rather than always-present.
# Found via a full prompt-vs-code cross-check. Code tolerated the gap
# (`.get()` with defaults), so this never broke a real request — this
# test guards the prompt's own internal consistency going forward.

def test_every_few_shot_example_output_is_valid_json_with_station_and_district():
    example_outputs = [
        line[len("Output: "):]
        for line in router._SYSTEM_PROMPT.splitlines()
        if line.startswith('Output: {"route"')
    ]
    assert len(example_outputs) > 40, "sanity check: still finding router.txt's real few-shot examples"
    for raw in example_outputs:
        parsed = json.loads(raw)  # must not raise
        assert "station" in parsed, f"missing 'station' key: {raw[:100]}..."
        assert "district" in parsed, f"missing 'district' key: {raw[:100]}..."
