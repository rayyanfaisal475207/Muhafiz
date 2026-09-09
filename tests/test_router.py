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
    # [findings.md Module 4] "weapon" was missing from the XAGG
    # recurring-entity keyword group entirely, so this fell through to
    # XGRAPH's own "across ... cases" override (its multiple/other group
    # is optional, so it fires even with no named entity at all).
    ("Which type of weapon appears most often across all cases?", "XAGG"),
    ("Are there firearms that have appeared in multiple cases?", "XAGG"),
    ("What are the top recurring weapons across all cases?", "XAGG"),
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


# ── Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 2 ──────────────────────────

@pytest.mark.parametrize("query", [
    "How many police stations are there?",
    "How many stations are there?",
    "kitne thanay hain?",
    "کتنے تھانے ہیں؟",
])
async def test_station_count_override_fires_to_xagg(monkeypatch, query):
    """
    Live-confirmed failure (Gold-QA report §2.3): "how many police stations
    are there" routed to document search instead of an aggregate/count
    route, because it matched none of the existing XAGG patterns (all
    tuned to "cases", not "stations").
    """
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query(query)
    assert result["route"] == "XAGG"


@pytest.mark.parametrize("query", [
    "What does Section 154 of the CrPC say?",
    "Explain Section 161 CrPC",
    "What does section 302 say?",
])
async def test_legal_text_content_override_fires_to_rag_not_sql(monkeypatch, query):
    """
    Live-confirmed failure (Gold-QA report §2.3): "what does Section 154
    CrPC say" is a legal-text-CONTENT question (document/RAG's job), not a
    "which section applies" lookup (SQL's job) -- it must never reach a
    route that structurally has no legal text to search.
    """
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query(query)
    assert result["route"] == "RAG"


async def test_legal_text_content_override_distinct_from_sql_which_section_applies(monkeypatch):
    """Regression guard: the existing "which section applies" SQL shape
    must be unaffected by the new RAG override."""
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query("What PPC section covers mobile phone theft?")
    assert result["route"] == "SQL"


# ── Gold-QA fix — Module 3 (questions CR2/S3) ───────────────────────────────

@pytest.mark.parametrize("query", [
    # CR2's exact gold-dataset text.
    "Is there anyone with an earlier case already on record who has since "
    "resurfaced as a suspect in a newer, separate case?",
    "Has any suspect resurfaced in a separate case?",
    "Was the same person arrested more than once?",
    "Was anyone charged again in a different case?",
])
async def test_person_recurrence_narrative_override_fires_to_xagg(monkeypatch, query):
    """
    Live-confirmed failure (goldtest-eval3 branch review): a person-
    recurrence question phrased NARRATIVELY ("resurfaced", "already on
    record", "arrested more than once") rather than with the "multiple/
    several cases" count-word shape the existing entity-group patterns
    already cover fell through to the LLM classifier, which sent it to
    XGRAPH -- correct for a NAMED-seed traversal, but for a broad
    no-named-seed query XGRAPH can only report a flat case-ID union, never
    the person's NAME the ground truth actually rewards. XAGG's own
    graph_recurrence path already names the person; this override just
    gets the deterministic pre-router to send it there.
    """
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query(query)
    assert result["route"] == "XAGG"


@pytest.mark.parametrize("query", [
    # S3's exact gold-dataset text (Urdu).
    "کیا کسی شخص کو ایک سے زیادہ بار گرفتار کیا گیا ہے؟",
    "کیا ملزم کو ایک سے زیادہ بار گرفتار کیا گیا؟",
    "kya kisi shakhs ko aik se zyada bar giraftar kiya gaya hai?",
])
async def test_person_recurrence_urdu_roman_urdu_override_fires_to_xagg(monkeypatch, query):
    """Same failure class as the English narrative test above, for the
    Urdu/Roman-Urdu "arrested more than once" phrasing (S3)."""
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query(query)
    assert result["route"] == "XAGG"


async def test_person_recurrence_override_does_not_swallow_named_xgraph_query(monkeypatch):
    """Regression guard: a query that actually NAMES an entity/case (XGRAPH's
    own job) must be unaffected by the new narrative-recurrence patterns --
    they require a recurrence verb/phrase, not just any mention of "case"."""
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query("Show connections for CNIC 12345-6789012-3 across other cases")
    assert result["route"] == "XGRAPH"


# ═══════════════════════════════════════════════════════════════════════
# Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Root Cause 1 (the actual upstream
# fix): the LLM classifier gets an explicit ACTIVE_CASE signal when no
# case is selected, so a within-case-SHAPED question ("this weapon,"
# "the accused") with no case active can be classified to a cross-case
# route instead of GRAPH/GRAPH_HYBRID (which would find nothing to
# summarize). None of these queries match any deterministic override
# pattern, so they all reach the LLM call this test captures.
#
# [Module 28] The first test's sample query used to be CR4's own text ("if
# we have a weapon logged as evidence, can we tell who it was taken off?").
# That is now intercepted by `_WEAPON_EVIDENCE_CHAIN_XAGG_PATTERNS` before
# the LLM call, so it no longer exercises what this test is about — and its
# old stub returning XGRAPH is itself a record of the misroute Module 28
# fixes. Swapped for another within-case-SHAPED weapon question that still
# matches no override; the ACTIVE_CASE assertion is unchanged.
# ═══════════════════════════════════════════════════════════════════════

async def test_no_case_id_prefixes_the_llm_call_with_active_case_none(monkeypatch):
    captured = {}

    async def fake_call_llm(system_prompt, user_message, **kwargs):
        captured["user_message"] = user_message
        return json.dumps({"route": "XGRAPH", "case_scope": "cross_case"})

    monkeypatch.setattr(router, "call_llm", fake_call_llm)
    query = "What is the condition of this weapon and where is it being stored?"
    await router.route_query(query, case_id=None)

    assert captured["user_message"].startswith("ACTIVE_CASE: none")
    assert query in captured["user_message"]


async def test_active_case_id_sends_the_query_unchanged_to_the_llm(monkeypatch):
    """The one thing this fix must never do: change what's sent to the LLM
    when a case IS active — that path stays byte-for-byte identical to
    before, since it's the already-tested, already-tuned behavior."""
    captured = {}

    async def fake_call_llm(system_prompt, user_message, **kwargs):
        captured["user_message"] = user_message
        return json.dumps({"route": "GRAPH_HYBRID", "case_scope": "within_case"})

    monkeypatch.setattr(router, "call_llm", fake_call_llm)
    query = "Summarize what happened in this case."
    await router.route_query(query, case_id="fir-401-26")

    assert captured["user_message"] == query


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


# ── secondary_methods [findings.md Module 7 — adaptive multi-method
# retrieval] ──────────────────────────────────────────────────────────────
#
# An OPTIONAL, additive field: every test above this section predates it
# and returns LLM JSON with no "secondary_methods" key at all — the single
# most important regression this section guards is that ALL of those
# existing single-method classifications still come back with
# secondary_methods == [] (the same as the field never having existed),
# never breaking or altering their own "route" value.

async def test_secondary_methods_absent_defaults_to_empty_list(monkeypatch):
    """The overwhelming common case: no compound need, key omitted entirely."""
    result = await _route(monkeypatch, json.dumps({"route": "GRAPH"}))
    assert result["secondary_methods"] == []
    assert result["route"] == "GRAPH"  # unaffected by the new field's presence


async def test_secondary_methods_parses_valid_values(monkeypatch):
    result = await _route(monkeypatch, json.dumps({"route": "GRAPH", "secondary_methods": ["SQL"]}))
    assert result["secondary_methods"] == ["SQL"]


async def test_secondary_methods_is_case_insensitive(monkeypatch):
    result = await _route(monkeypatch, json.dumps({"route": "GRAPH", "secondary_methods": ["sql"]}))
    assert result["secondary_methods"] == ["SQL"]


async def test_secondary_methods_drops_unrecognized_values(monkeypatch):
    result = await _route(monkeypatch, json.dumps({
        "route": "GRAPH", "secondary_methods": ["SQL", "NOT_A_REAL_METHOD", "WEB"],
    }))
    assert result["secondary_methods"] == ["SQL"]


async def test_secondary_methods_drops_self_reference(monkeypatch):
    """A route can't be its own secondary method — the router's own primary
    choice already covers whatever that method retrieves."""
    result = await _route(monkeypatch, json.dumps({
        "route": "SQL", "secondary_methods": ["SQL", "GRAPH"],
    }))
    assert result["secondary_methods"] == ["GRAPH"]


async def test_secondary_methods_capped_at_two(monkeypatch):
    result = await _route(monkeypatch, json.dumps({
        "route": "GRAPH", "secondary_methods": ["SQL", "XGRAPH", "XAGG"],
    }))
    assert len(result["secondary_methods"]) == 2
    assert result["secondary_methods"] == ["SQL", "XGRAPH"]


async def test_secondary_methods_not_a_list_defaults_to_empty(monkeypatch):
    result = await _route(monkeypatch, json.dumps({"route": "GRAPH", "secondary_methods": "SQL"}))
    assert result["secondary_methods"] == []


async def test_secondary_methods_non_string_items_are_dropped(monkeypatch):
    result = await _route(monkeypatch, json.dumps({"route": "GRAPH", "secondary_methods": [123, "SQL", None]}))
    assert result["secondary_methods"] == ["SQL"]


async def test_secondary_methods_empty_on_json_parse_failure(monkeypatch):
    """The exception-fallback dict (malformed/unparseable LLM output) must
    carry the same field, at parity with the success dict, so callers never
    have to special-case a missing key."""
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return "not valid json"

    monkeypatch.setattr(router, "call_llm", fake_call_llm)
    result = await router.route_query("some query")
    assert result["route"] == "RAG"
    assert result["secondary_methods"] == []


async def test_sql_override_still_fires_for_an_ordinary_single_intent_lookup(monkeypatch):
    """
    Regression guard: the compound exception below must NOT weaken the
    override for the overwhelming majority of SQL-shaped queries that have
    no compound "and ... this X" language — this is the exact query the
    override's own comment cites as the reason it exists (LLM reliably
    misclassified it live).
    """
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query("What PPC section covers mobile phone theft?")
    assert result["route"] == "SQL"


async def test_sql_override_skips_for_a_compound_question_naming_this_x_first(monkeypatch):
    """
    [findings.md Module 7] The override's own trigger vocabulary
    ("PPC section") can appear as just the SQL HALF of a genuinely
    compound question — this is this module's own live-tested example,
    phrased with the case-specific "this weapon" clause FIRST. Must fall
    through to the LLM classifier (only it can set secondary_methods),
    not short-circuit to a SQL-only route that silently drops the other
    half.
    """
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return json.dumps({"route": "GRAPH", "secondary_methods": ["SQL"]})

    monkeypatch.setattr(router, "call_llm", fake_call_llm)
    result = await router.route_query(
        "What is this weapon's condition, and what PPC section covers "
        "illegal possession of an unlicensed firearm?"
    )
    assert result["route"] == "GRAPH", "must reach the LLM classifier, not short-circuit to SQL"
    assert result["secondary_methods"] == ["SQL"]


async def test_sql_override_skips_for_a_compound_question_naming_this_x_second(monkeypatch):
    """Same guard, other clause order — 'and' before 'this weapon'."""
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return json.dumps({"route": "SQL", "secondary_methods": ["GRAPH"]})

    monkeypatch.setattr(router, "call_llm", fake_call_llm)
    result = await router.route_query(
        "What does 379 PPC cover, and what item was stolen in this case?"
    )
    assert result["route"] == "SQL"
    assert result["secondary_methods"] == ["GRAPH"]


async def test_secondary_methods_present_on_every_deterministic_override(monkeypatch):
    """The four regex fast-paths (SQL/XNETWORK/XAGG/XGRAPH) bypass the LLM
    entirely and never set this key — callers must still get a safe []
    via .get(...) rather than a missing key. Guards that route_query()'s
    caller-facing contract (always a "secondary_methods" list) holds even
    on the short-circuit path, not just the LLM path tested above."""
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query("What PPC section covers mobile phone theft?")
    assert result["route"] == "SQL"
    assert result.get("secondary_methods", []) == []


# ── Gold-QA fix — Module 15's CR8 pattern vs. KB1 collision ─────────────────
#
# Live-caught regression (not covered by any existing test): Module 15's
# forwarded/converted-report <-> FIR-match override for CR8
# ("report|converted|forwarded" + a bare "f.i.r." mention) also matched
# KB1's wording ("What legal requirement governs how a report of a crime
# becomes a formal FIR...") purely because both words appear within 60
# chars of each other — an ordinary, unrelated co-occurrence for a
# definitional/legal-reasoning question, not the CR8-shaped "does the
# record confirm a match" question the pattern was written for. KB1 was
# hijacked to XAGG's flat statute-count aggregate instead of reaching the
# KB-scoped RAG path Module 8c built for it. Fixed by requiring the actual
# confirm/match framing in the second group instead of a bare FIR mention,
# with "forwarded/converted + FIR number" kept as its own narrower pattern.

async def test_kb1_legal_definitional_question_does_not_route_to_xagg(monkeypatch):
    """Regression guard for the exact live collision: KB1's wording must
    fall through to the LLM classifier (this test's fake LLM says RAG),
    not get hijacked by CR8's forwarded-report/FIR override just because
    both "report" and "FIR" appear in the sentence."""
    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return json.dumps({"route": "RAG", "case_scope": "cross_case"})

    monkeypatch.setattr(router, "call_llm", fake_call_llm)
    result = await router.route_query(
        "What legal requirement governs how a report of a crime becomes a "
        "formal FIR, and does our recordkeeping actually follow it?"
    )
    assert result["route"] == "RAG"


@pytest.mark.parametrize("query", [
    "If a domestic violence complaint was recorded as converted into a formal case, is that confirmed by the case record?",
    "Was this report's status confirmed by the case record?",
])
async def test_cr8_converted_report_confirm_override_still_fires_to_xagg(monkeypatch, query):
    """The narrowed pattern must still catch the actual CR8 shape it exists
    for — a converted/forwarded report whose match is confirmed against the
    case record — even after dropping the bare "FIR mention" alternative
    that caused the KB1 collision above."""
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query(query)
    assert result["route"] == "XAGG"


async def test_cr8_forwarded_fir_number_override_fires_to_xagg(monkeypatch):
    """The "forwarded/converted + FIR number" shape (CR8's other real
    phrasing) must still route to XAGG via its own narrower pattern."""
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query(
        "Does the forwarded report's FIR number match a real, active FIR?"
    )
    assert result["route"] == "XAGG"


# ═══════════════════════════════════════════════════════════════════════
# [Gold-QA fix — Module 26, question M1] Year-over-year / period comparison
# override. See `_TIME_COMPARISON_XAGG_PATTERNS`'s own module-level comment
# in router.py for the full rationale (shared with supervisor.py's
# Meta-Analysis-skip guard).
# ═══════════════════════════════════════════════════════════════════════

async def test_m1_year_over_year_comparison_fires_to_xagg(monkeypatch):
    """M1's exact gold-dataset text must route deterministically to XAGG —
    the live-confirmed defect this module fixes: without this override the
    classification depended on the flaky local LLM, which historically
    (per GOLD32_RESULTS_FOR_TEAMMATE.md) sent this exact text to XGRAPH."""
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query(
        "What kinds of cases are we dealing with now compared to a couple of years back?"
    )
    assert result["route"] == "XAGG"
    assert result["case_scope"] == "cross_case"


@pytest.mark.parametrize("query", [
    # Non-gold paraphrases — the pattern must not be pinned to M1's one
    # literal string.
    "Has the mix of crimes we handle shifted since 2024?",
    "How does this year's caseload compare with last year?",
    "What's changed in our case types versus two years ago?",
    "Crime type breakdown this year vs 2024?",
    "Year over year, how has our caseload composition changed?",
    # Urdu / Roman-Urdu paraphrases, matching M5's own comparison idiom.
    "موجودہ کیسز کی نوعیت 2024 کے مقابلے میں کیا بدل گئی ہے؟",
    "case types ab 2024 ke muqable mein kaise badal gaye hain?",
])
async def test_m1_paraphrases_fire_to_xagg(monkeypatch, query):
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query(query)
    assert result["route"] == "XAGG"


def test_m1_pattern_negative_control_against_all_other_gold_questions():
    """
    Mandatory negative control (per this module's brief, and the lesson
    from the three historical collisions it names: Module 8c's 0/7 KB
    pattern gap, CR8->KB1, and M4->G3 — a pattern that looks reasonable in
    isolation is not evidence it matches only the intended question).

    Asserts the new `_TIME_COMPARISON_XAGG_PATTERNS` family matches M1 and
    matches NONE of the other 31 gold questions in the dataset.

    M5 (a different question, itself already reaching XAGG's
    `_statute_mix_by_year` today via its own Urdu "کے مقابلے میں" wording)
    is the one EXPECTED co-match — both are genuine year-over-year
    comparison questions that this same aggregate answers, so a shared
    match here is correct, not a collision. Every other gold question must
    match zero patterns in this family.
    """
    import json
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "evaluation",
        "Gold_QA_Dataset_Final32_With_Answers.json",
    )
    gold = json.load(open(path, encoding="utf-8"))
    assert len(gold) == 32

    matched_ids = [
        item["id"]
        for item in gold
        if any(pat.search(item["question"]) for pat in router._TIME_COMPARISON_XAGG_PATTERNS)
    ]

    assert "M1" in matched_ids
    # M5 is the one legitimate co-match (see docstring); every other ID
    # matching would be a genuine false-positive collision.
    unexpected = set(matched_ids) - {"M1", "M5"}
    assert unexpected == set(), (
        f"Year-over-year comparison pattern unexpectedly matched: {sorted(unexpected)} "
        f"(full match list: {sorted(matched_ids)})"
    )


# ═══════════════════════════════════════════════════════════════════════
# [Gold-QA fix — Module 28, question CR4] Weapon-evidence attribution chain
# override. See `_WEAPON_EVIDENCE_CHAIN_XAGG_PATTERNS`'s own module-level
# comment in router.py for the full rationale and the CR2/G5/CP1/M5/KB6
# discriminator.
# ═══════════════════════════════════════════════════════════════════════

def _gold32() -> list[dict]:
    import json
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "evaluation",
        "Gold_QA_Dataset_Final32_With_Answers.json",
    )
    gold = json.load(open(path, encoding="utf-8"))
    assert len(gold) == 32
    return gold


async def test_cr4_weapon_evidence_chain_fires_to_xagg(monkeypatch):
    """CR4's exact gold-dataset text must route deterministically to XAGG.

    The live-confirmed defect this module fixes (Module 21's investigation,
    GOLD_QA_REMAINING_FIXES_PLAN.md): this text routed to XGRAPH and was
    dispatched to Cross-Case Linkage alone, which — with no named entity to
    seed a traversal — runs a recurring-entity-across-cases search instead
    of the single weapon -> FIR -> accused -> status chain gold asks for,
    and came back empty every time.
    """
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query(
        "If we've got a weapon logged as evidence, can we tell who it was "
        "taken off and what happened to them?"
    )
    assert result["route"] == "XAGG"
    assert result["case_scope"] == "cross_case"


@pytest.mark.parametrize("query", [
    # The brief's required non-gold paraphrase, plus others that share no
    # distinctive keyword with CR4's literal text — the pattern must not be
    # pinned to one string.
    "For weapons we've seized as evidence, can we trace them back to whoever they were taken from?",
    "Who was this pistol recovered from, and what happened to him?",
    "Can we tell whose firearm each seized gun was?",
    "For a gun in the evidence register, do we know who it was taken off?",
    # Urdu / Roman-Urdu paraphrases.
    "برآمد شدہ ہتھیار کس سے لیا گیا اور اس شخص کا کیا بنا؟",
    "Evidence mein darj hathiyar kis se baramad hua tha?",
])
async def test_cr4_paraphrases_fire_to_xagg(monkeypatch, query):
    monkeypatch.setattr(router, "call_llm", _no_llm_call)
    result = await router.route_query(query)
    assert result["route"] == "XAGG"


def test_cr4_pattern_negative_control_against_all_other_gold_questions():
    """
    Mandatory negative control for a ROUTING module (Module 28's brief §4,
    and the M4/G3 collision of PR #8 that shipped without one).

    Asserts the new `_WEAPON_EVIDENCE_CHAIN_XAGG_PATTERNS` family matches
    CR4 and NONE of the other 31 gold questions. Zero co-matches are
    expected here — unlike Module 26's M1/M5 pair, there is no second gold
    question asking whose weapon it was. The four gold questions that DO
    share weapon vocabulary (G5, CP1, M5, KB6) all currently work and must
    not move; CR2, the cross-case recurrence question Module 21 warned a
    weapon keyword would misroute, carries no weapon vocabulary at all.
    """
    matched_ids = [
        item["id"]
        for item in _gold32()
        if any(
            pat.search(item["question"])
            for pat in router._WEAPON_EVIDENCE_CHAIN_XAGG_PATTERNS
        )
    ]
    assert matched_ids == ["CR4"], (
        f"Weapon-evidence-chain pattern matched {sorted(matched_ids)}; "
        f"expected exactly ['CR4']"
    )


def test_module28_changes_no_other_gold_question_route():
    """
    The stronger form of the negative control the brief actually asks for:
    not just "the new patterns don't match", but "no other gold question's
    deterministic ROUTE changes".

    Computes `_deterministic_route_override()` for all 32 gold questions
    with the new pattern family in place, then again with it removed, and
    asserts CR4 is the only question whose outcome differs. This is the
    check that would have caught the M4/G3 collision (PR #8) — a pattern
    can be "narrow" and still steal a question from an EARLIER override
    block by changing which one matches first.
    """
    gold = _gold32()

    def routes() -> dict:
        return {
            item["id"]: (router._deterministic_route_override(item["question"]) or {}).get("route")
            for item in gold
        }

    after = routes()

    original_xagg = list(router._XAGG_OVERRIDE_PATTERNS)
    try:
        router._XAGG_OVERRIDE_PATTERNS[:] = [
            p for p in original_xagg
            if p not in router._WEAPON_EVIDENCE_CHAIN_XAGG_PATTERNS
        ]
        before = routes()
    finally:
        router._XAGG_OVERRIDE_PATTERNS[:] = original_xagg

    changed = {qid: (before[qid], after[qid]) for qid in before if before[qid] != after[qid]}
    assert changed == {"CR4": (None, "XAGG")}, (
        f"Module 28's override changed the deterministic route of: {changed}. "
        f"Only CR4 may change (from 'no deterministic override' to XAGG)."
    )
    # Named explicitly because Module 21's investigation called this exact
    # collision out as the hazard for this module.
    assert after["CR2"] == before["CR2"]
    assert after["G5"] == before["G5"]


# ═══════════════════════════════════════════════════════════════════════
# [Gold-QA fix — Module 60, question M4] M4 must reach its own aggregate.
#
# The tracker recorded M4 as skipping decomposition since Module 41. Live it
# never did — 7 runs of 7 across two code states — because Module 41's guard
# is conditional on `route == "XAGG"` and the LLM router classifies M4 as
# XNETWORK. Module 60 fixes the ROUTE (option 3 of the three the plan lists)
# rather than the guard, gated on `resolve_aggregate_kind()` — Module 41's
# single source of dispatch truth — so the router cannot disagree with the
# chain XAGG will actually run.
# ═══════════════════════════════════════════════════════════════════════

_M4_GOLD = (
    "ایک طرف یہ دیکھیں کہ لوگوں پر کن دفعات میں مقدمے بن رہے ہیں، اور دوسری "
    "طرف یہ کہ وہ مقدمے عدالت میں کہاں تک پہنچے — کیا دونوں سے کیس لوڈ کی "
    "سنگینی کا ایک ہی اندازہ ہوتا ہے؟"
)


def test_module60_m4_gold_text_routes_deterministically_to_xagg():
    """Pinned to M4's LITERAL Urdu gold text. Before Module 60 this returned
    `None` (no deterministic override) and the LLM router answered XNETWORK
    on every one of seven live runs."""
    override = router._deterministic_route_override(_M4_GOLD)
    assert override is not None
    assert override["route"] == "XAGG"
    assert override["case_scope"] == "cross_case"
    assert "statute" in override["reason"].lower()


def test_module60_m4_gold_text_is_in_the_dataset_verbatim():
    """The test above is only worth anything if the string really is M4's."""
    m4 = [it for it in _gold32() if (it.get("id") or "").upper() == "M4"]
    assert len(m4) == 1
    assert m4[0]["question"] == _M4_GOLD


def test_module60_override_is_gated_on_the_aggregate_resolver_not_a_regex():
    """Module 62's warning, pinned: this override must ask XAGG's own chain,
    so it inherits every precedence rule above `_is_statute_court_stage_join`
    (G3's court-readiness scan, CR7's criminal-record cross-check) for free.
    A future edit that reimplements it as a pattern list fails here."""
    from src.pipeline.xagg import resolve_aggregate_kind

    assert router._resolves_to_statute_court_stage_join(_M4_GOLD)
    assert resolve_aggregate_kind(_M4_GOLD) == "statute_court_stage_join"
    # A question the resolver sends elsewhere must not be captured, even
    # though it carries M4's court vocabulary.
    g3 = [it for it in _gold32() if (it.get("id") or "").upper() == "G3"][0]["question"]
    assert resolve_aggregate_kind(g3) == "court_readiness_scan"
    assert not router._resolves_to_statute_court_stage_join(g3)


def test_module60_an_active_case_still_short_circuits_the_new_override():
    """A within-case "how far did THIS case get in court?" must stay GRAPH —
    the override sits below the active-case short-circuit deliberately."""
    within = "How far has CASE-009 got in court, and what sections was it charged under?"
    assert router._deterministic_route_override(within) is None
    assert router._deterministic_route_override(
        "How far has the case got in court and under what sections?", case_id="CASE-009"
    ) is None


# Captured from `_deterministic_route_override()` on the pre-Module-60 tree
# (branch point `main` @ c5533ba). `None` means "no override — the LLM router
# decides". This is the control: the new override must move NOTHING but M4.
# A diff here is a regression, not a test to update.
_GOLD32_DETERMINISTIC_ROUTES_BEFORE_MODULE60 = {
    "D1": "XAGG", "S2": "XAGG", "S3": "XAGG", "A1": None, "A7": "XAGG",
    "CP6": "XAGG", "CR2": "XAGG", "CR3": None, "CR4": "XAGG", "CR6": "XAGG",
    "CR7": "XAGG", "CR8": "XAGG", "CS4": None, "CP1": None, "M1": "XAGG",
    "M2": None, "M4": None, "M5": "XAGG", "M7": None, "G1": None,
    "G2": "XAGG", "G3": "XAGG", "G5": "XAGG", "G6": None, "KB1": None,
    "KB2": None, "KB3": None, "KB4": None, "KB5": None, "KB6": None,
    "KB8": None, "KB9": None,
}


def test_module60_all_32_gold_questions_route_exactly_as_before_except_m4():
    """The non-negotiable all-32 negative control, stated as EQUALITY.

    Asserting only "M4 now routes to XAGG" would pass if the override had
    also dragged G3 or CR7 sideways. A router change that quietly moves a
    working question is the most expensive mistake available here — the
    M4/G3 keyword collision (PR #8) is the precedent."""
    after = {
        (it.get("id") or "").upper():
            (router._deterministic_route_override(it["question"]) or {}).get("route")
        for it in _gold32()
    }
    expected = dict(_GOLD32_DETERMINISTIC_ROUTES_BEFORE_MODULE60)
    expected["M4"] = "XAGG"  # the one intended change
    changed = {
        qid: (_GOLD32_DETERMINISTIC_ROUTES_BEFORE_MODULE60[qid], after[qid])
        for qid in after
        if _GOLD32_DETERMINISTIC_ROUTES_BEFORE_MODULE60[qid] != after[qid]
    }
    assert changed == {"M4": (None, "XAGG")}, changed
    assert after == expected


def test_module60_gold_question_variants_route_exactly_as_before():
    """The 32 questions' own paraphrase variants, held to the same bar."""
    for item in _gold32():
        for variant in item.get("question_variants") or []:
            route = (router._deterministic_route_override(variant) or {}).get("route")
            # No variant may newly land on the Module 60 override.
            if route == "XAGG":
                assert not router._resolves_to_statute_court_stage_join(variant), (
                    f"{item['id']} variant newly captured by Module 60: {variant}"
                )


def test_module60_broad_form_was_rejected_for_a_measured_reason():
    """Documents, executably, why this override names ONE aggregate kind
    instead of `resolves_to_specific_aggregate()`.

    The broad form — "route to XAGG whenever XAGG resolves to something
    specific" — moves SEVEN of the 32, including KB5, a legal-KB question
    that resolves to `gender_breakdown` purely as a resolver false
    positive. That is the bound on the risk, and it is measured, not
    asserted."""
    from src.pipeline.xagg import resolves_to_specific_aggregate

    would_move = sorted(
        (it.get("id") or "").upper()
        for it in _gold32()
        if router._deterministic_route_override(it["question"]) is None
        and resolves_to_specific_aggregate(it["question"])
    )
    assert "KB5" in would_move
    assert len(would_move) >= 5, would_move
