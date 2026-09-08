"""
Tests for src/pipeline/harness/supervisor.py (Phase 1).

Covers:
  (a) correct classification -> dispatch using mock sub-agents satisfying
      the SubAgent Protocol (not real sub-agents — none exist yet);
  (b) ExecutionContext (wrapping CallerContext, per the contract retrofit
      in AGENT_HARNESS_IMPLEMENTATION_PLAN.md §10) passed through
      byte-for-byte unchanged (object identity preserved, not merely
      equal);
  (c) correct behavior on an unregistered route (typed ABSTAINED result,
      not a crash, not a silent fallback);
  (d) PipelineEvent emitted with the right shape (step/status/detail,
      existing five-value SSE vocabulary, one event per meaningful
      transition — never collapsed into one "ran" event).

`route_query` is monkeypatched at the module level (`supervisor.route_query`)
in every test — none of these exercise the real LLM-backed router; that is
router.py's own test suite's job, not this one's (per the plan: reuse
router.py, don't re-test it here).
"""
from __future__ import annotations

import pytest

import src.pipeline.harness.supervisor as supervisor_mod
from src.pipeline.harness.supervisor import (
    CASE_SUMMARIZATION,
    CROSS_CASE_LINKAGE,
    DATA_QUALITY,
    GLOBAL_SEARCH,
    INVESTIGATIVE_ANALYSIS,
    LARGE_SCALE_AGGREGATE,
    META_ANALYSIS,
    NO_SUB_AGENT,
    REPORT_DRAFTING,
    SEMANTIC_SEARCH,
    TIMELINE_BUILDING,
    Supervisor,
    classify_to_subagent,
    register,
    unregister,
)
from src.pipeline.xagg import (
    resolve_aggregate_kind,
    resolves_to_specific_aggregate,
)
from src.pipeline.harness.types import (
    CallerContext,
    ConversationContext,
    ExecutionContext,
    PipelineEvent,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
)


def _caller(role=Role.INVESTIGATOR, **kw):
    return CallerContext(user_id="u1", role=role, active_case_id="CASE-001", **kw)


def _execution(caller=None, **kw):
    return ExecutionContext(caller=caller or _caller(), **kw)


def _agent_input(caller=None, query_text="what happened in this case?", **kw):
    return SubAgentInput(query_text=query_text, execution=_execution(caller=caller), **kw)


def _mock_sub_agent(name: str, result: SubAgentResult):
    """A minimal stand-in satisfying the SubAgent Protocol — captures the
    exact `agent_input` it received for later inspection.

    [AMENDMENT — pre-Phase-7 contract amendment] Accepts the same
    keyword-only `on_event` every real sub-agent now does (see
    `types.SubAgent`'s amendment note) and records whatever it was given
    (including `None`) so tests can assert Supervisor.handle() actually
    forwards it, rather than only that the call didn't raise.

    [AMENDMENT — pre-Phase-8 contract amendment] Same treatment for the new
    keyword-only `gateway` parameter."""
    calls = []
    on_events_received = []
    gateways_received = []

    async def _handler(agent_input: SubAgentInput, *, on_event=None, gateway=None) -> SubAgentResult:
        calls.append(agent_input)
        on_events_received.append(on_event)
        gateways_received.append(gateway)
        return result

    _handler.name = name
    _handler.calls = calls
    _handler.on_events_received = on_events_received
    _handler.gateways_received = gateways_received
    return _handler


@pytest.fixture(autouse=True)
def isolated_registry():
    """
    Every test gets its own registry dict, never the module-level global
    one — prevents one test's registered mock from leaking into another
    (and from leaking into any other test module that imports supervisor).
    """
    return {}


def _stub_route_query(monkeypatch, route_result: dict):
    async def _fake(query_text: str) -> dict:
        return route_result
    monkeypatch.setattr(supervisor_mod, "route_query", _fake)


# ═══════════════════════════════════════════════════════════════════════
# (a) classification -> dispatch
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "route_result,expected_name",
    [
        ({"route": "RAG", "output_format": "chat"}, SEMANTIC_SEARCH),
        ({"route": "GRAPH", "output_format": "chat"}, CASE_SUMMARIZATION),
        ({"route": "GRAPH_HYBRID", "output_format": "chat"}, CASE_SUMMARIZATION),
        ({"route": "SQL", "output_format": "chat"}, INVESTIGATIVE_ANALYSIS),
        # XGRAPH/XNETWORK/XAGG carry case_scope="cross_case", matching real
        # route_query() output (router.py never forces these three back to
        # within_case) — required for the case_scope demotion guard
        # (reconciliation Unit 2) to dispatch them to their real sub-agent
        # rather than demoting to Semantic Search.
        ({"route": "XGRAPH", "case_scope": "cross_case", "output_format": "chat"}, CROSS_CASE_LINKAGE),
        ({"route": "XNETWORK", "case_scope": "cross_case", "output_format": "chat"}, CROSS_CASE_LINKAGE),
        ({"route": "XAGG", "case_scope": "cross_case", "output_format": "chat"}, LARGE_SCALE_AGGREGATE),
        # [Reconciliation fix — Unit 2] DIRECT -> NO_SUB_AGENT, not Semantic
        # Search. See NO_SUB_AGENT's own comment in supervisor.py.
        ({"route": "DIRECT", "output_format": "chat"}, NO_SUB_AGENT),
        ({"route": "WEB", "output_format": "chat"}, SEMANTIC_SEARCH),
        # File output_format overrides the route entirely.
        ({"route": "RAG", "output_format": "file_pdf"}, REPORT_DRAFTING),
        ({"route": "XAGG", "case_scope": "cross_case", "output_format": "file_xlsx"}, REPORT_DRAFTING),
        # [Reconciliation fix — Unit 2] DIRECT wins even over a file
        # output_format — see classify_to_subagent()'s own comment.
        ({"route": "DIRECT", "output_format": "file_pdf"}, NO_SUB_AGENT),
        # [Reconciliation fix — Unit 2] case_scope demotion guard: a
        # cross-case route whose case_scope did NOT come back "cross_case"
        # (a genuinely possible LLM-classification outcome, not just a
        # hypothetical) demotes to Semantic Search rather than reaching a
        # cross-case sub-agent under a within-case scope.
        ({"route": "XAGG", "case_scope": "within_case", "output_format": "chat"}, SEMANTIC_SEARCH),
        ({"route": "XGRAPH", "output_format": "chat"}, SEMANTIC_SEARCH),
    ],
)
def test_classify_to_subagent(route_result, expected_name):
    assert classify_to_subagent(route_result) == expected_name


# ── Provisional classification triggers: Timeline Building / broader
# Investigative Analysis reach (resolved via AskUserQuestion -- see the
# progress-log entry for this branch for the full "Problem A" reasoning) ──

@pytest.mark.parametrize(
    "query_text",
    [
        "give me a timeline of events for this case",
        "what is the chronological order of events",
        "show me the sequence of events",
        "what happened when in this investigation",
        "اس کیس کے واقعات کی ترتیب دکھائیں",
        "کب کیا ہوا اس کیس میں",
        "is case ki waqeat ki tarteeb batayen",
        "kab kya hua tha",
    ],
)
def test_timeline_trigger_overrides_graph_classification(query_text):
    route_result = {"route": "GRAPH", "output_format": "chat"}
    assert classify_to_subagent(route_result, query_text) == TIMELINE_BUILDING


@pytest.mark.parametrize(
    "query_text",
    [
        "give me a deep dive on this case",
        "I need a full analysis of this case",
        "give me a comprehensive analysis",
        "run a detailed investigation into this",
        "give me the full picture of this case",
        "اس کیس کی مکمل تحقیقات کریں",
        "تفصیلی تجزیہ درکار ہے",
        "گہرائی سے تجزیہ کریں",
        "mukammal tehqiqat chahiye",
        "tafseeli tajzia karen",
    ],
)
def test_investigative_analysis_trigger_overrides_base_classification(query_text):
    route_result = {"route": "RAG", "output_format": "chat"}
    assert classify_to_subagent(route_result, query_text) == INVESTIGATIVE_ANALYSIS


# [Audit hypothesis #12] classify_to_subagent() had no trigger vocabulary
# for DATA_QUALITY at all — every one of these queries fell through to
# SEMANTIC_SEARCH instead, regardless of route, before this fix.
@pytest.mark.parametrize(
    "query_text",
    [
        "what is the data quality for this case",
        "show me the extraction coverage",
        "how complete is the data for this case",
        "are there any missing fields in this case",
        "is this case's data unstructured",
        "اس کیس کی ڈیٹا کوالٹی کیا ہے",
        "اس کیس میں کتنا ڈیٹا نکالا گیا",
        "is case ki data quality kitni hai",
        "is case mein kitna data nikala gaya",
    ],
)
def test_data_quality_trigger_overrides_base_classification(query_text):
    route_result = {"route": "RAG", "output_format": "chat"}
    assert classify_to_subagent(route_result, query_text) == DATA_QUALITY


@pytest.mark.parametrize("route", ["XGRAPH", "XAGG", "XNETWORK"])
@pytest.mark.parametrize(
    "query_text",
    ["give me a timeline of events", "give me a deep dive on this", "what is the data quality for this case"],
)
def test_provisional_triggers_never_override_a_cross_case_classification(route, query_text):
    """[PRESERVE] A query matching both a cross-case trigger and one of the
    two provisional patterns is a genuine ambiguity these overrides must
    not try to resolve heuristically -- router.py's own already-evidenced
    cross-case precedence wins outright."""
    route_result = {"route": route, "case_scope": "cross_case", "output_format": "chat"}
    result = classify_to_subagent(route_result, query_text)
    assert result == CROSS_CASE_LINKAGE if route in ("XGRAPH", "XNETWORK") else result == LARGE_SCALE_AGGREGATE


# [AMENDMENT — findings.md Module 9, "Global Search"] classify_to_subagent()
# coverage for the new override, per findings.md's own Test plan.
def test_global_search_trigger_overrides_xnetwork_default():
    route_result = {"route": "XNETWORK", "case_scope": "cross_case", "output_format": "chat"}
    assert classify_to_subagent(route_result, "what are the top 5 themes in the data?") == GLOBAL_SEARCH


def test_global_search_trigger_does_not_fire_on_xnetworks_existing_default_shape():
    """XNETWORK's existing default (a specific network/cluster question,
    findings.md's own repro text) must stay CROSS_CASE_LINKAGE, unaffected
    by the new override."""
    route_result = {"route": "XNETWORK", "case_scope": "cross_case", "output_format": "chat"}
    assert (
        classify_to_subagent(route_result, "overall picture of associate networks across the robbery cases")
        == CROSS_CASE_LINKAGE
    )


def test_global_search_trigger_only_applies_to_the_xnetwork_route():
    """A 'top 5 themes' phrasing on a non-XNETWORK route must not
    accidentally reroute -- the override is scoped to XNETWORK only."""
    route_result = {"route": "RAG", "output_format": "chat"}
    assert classify_to_subagent(route_result, "what are the top 5 themes in the data?") == SEMANTIC_SEARCH


def test_global_search_is_covered_by_the_case_scope_demotion_guard():
    """GLOBAL_SEARCH is cross-case-role-gated like CROSS_CASE_LINKAGE/
    LARGE_SCALE_AGGREGATE -- it must be included in _CROSS_CASE_SUBAGENTS
    so the same demotion guard applies to it: an XNETWORK route whose
    case_scope did NOT come back "cross_case" must demote to Semantic
    Search, not reach Global Search under a within-case scope."""
    route_result = {"route": "XNETWORK", "case_scope": "within_case", "output_format": "chat"}
    assert classify_to_subagent(route_result, "what are the top 5 themes in the data?") == SEMANTIC_SEARCH


# [AMENDMENT — findings.md Module 10, "Meta-Analysis"] classify_to_subagent()
# coverage for the new decomposition-trigger override, per findings.md's own
# Test plan.
@pytest.mark.parametrize(
    "query_text",
    [
        "Summarize the recurring patterns across all robbery cases handled by "
        "this station in the last quarter and flag any that share a suspect "
        "with an unresolved case.",
        "Aggregate the weapon types used across all cases this year and flag "
        "any case where the weapon matches an unresolved case's weapon.",
        "What are the recurring themes across all cases at this station, and "
        "cross-reference them with cases involving juvenile suspects?",
    ],
)
def test_meta_analysis_trigger_overrides_classification(query_text):
    route_result = {"route": "XNETWORK", "case_scope": "cross_case", "output_format": "chat"}
    assert classify_to_subagent(route_result, query_text) == META_ANALYSIS


@pytest.mark.parametrize(
    "query_text",
    [
        "Summarize case CASE-021.",
        "What is the FIR number for case CASE-014?",
        "Aggregate the case counts by station.",
        "What are the recurring patterns in this case's witness statements?",
    ],
)
def test_meta_analysis_trigger_does_not_fire_on_ordinary_queries(query_text):
    route_result = {"route": "RAG", "output_format": "chat"}
    assert classify_to_subagent(route_result, query_text) != META_ANALYSIS


def test_meta_analysis_trigger_wins_over_other_provisional_triggers():
    """[PRESERVE] Module 10 is the outermost layer -- checked before
    TIMELINE/INVESTIGATIVE_ANALYSIS/LOCAL_SEARCH/GLOBAL_SEARCH so a
    genuinely compound question is never swallowed by a single-route
    override first."""
    route_result = {"route": "GRAPH", "case_scope": "cross_case", "output_format": "chat"}
    query_text = (
        "give me a full analysis and summarize the recurring patterns across "
        "all cases at this station and flag any repeat offenders"
    )
    assert classify_to_subagent(route_result, query_text) == META_ANALYSIS


def test_meta_analysis_is_covered_by_the_case_scope_demotion_guard():
    """META_ANALYSIS is cross-case-role-gated like CROSS_CASE_LINKAGE/
    LARGE_SCALE_AGGREGATE/GLOBAL_SEARCH -- resolved via AskUserQuestion as
    this module's RBAC answer: a compound-question trigger match on a query
    whose own case_scope did NOT come back "cross_case" demotes straight to
    Semantic Search -- no N-way decompose+dispatch is ever attempted for a
    within-case compound question."""
    route_result = {"route": "RAG", "case_scope": "within_case", "output_format": "chat"}
    query_text = "Summarize the recurring patterns across all our cases and flag any repeats."
    assert classify_to_subagent(route_result, query_text) == SEMANTIC_SEARCH


# [AMENDMENT — findings.md Module 11] Regression pin for RC-0's actual
# evidence: `evaluation/UNTOUCHED_BUCKETS_DIAGNOSIS.md` found the ORIGINAL
# four patterns above matched 0 of the 18 live Gold-32 questions RC-0 was
# diagnosed from. These are that evidence, verbatim (English/Urdu/Roman
# Urdu), plus G1's own live paraphrase from Module 11's verify step — every
# one of these previously fell through to XAGG/XNETWORK/XGRAPH/RAG with no
# decomposition ever attempted. A1 and CR4 are deliberately excluded (see
# the trigger block's own comment for why neither is a decomposition
# candidate).
@pytest.mark.parametrize(
    "query_text",
    [
        # (A) role-play / whole-caseload evaluative review
        "Acting as a crime analyst, review our current caseload and flag "
        "anything that looks unusual or worth monitoring.",
        "Is there anything about this caseload a supervisor should be worried about?",
        "فرض کریں آپ کسی ایس ایچ او کو بریفنگ دے رہے ہیں کہ کون سے مقدمے دب کر یا نظر سے اوجھل ہو کر رہ سکتے ہیں — آپ کن چیزوں کی نشاندہی کریں گے؟",
        "آپ عدالت کو حوالگی کے لیے ایک کیس فائل تیار کر رہے ہیں — ڈیٹا کی روشنی میں، کن چیزوں کے نامکمل قرار پانے کا سب سے زیادہ امکان ہے؟",
        "Baramad shuda hathiyaron ki record keeping ko dekhte hue, kya koi aisi baat hai jo compliance ke lihaz se flag karne layak ho?",
        "Yahan naye tainaat hone wale afsar ke liye ek mukhtasar orientation note likhein — unhein mojooda case load se kya tawaqqo rakhni chahiye?",
        # (B) comparative-over-time / branching comparison
        "What kinds of cases are we dealing with now compared to a couple of years back?",
        "Is caseload growing faster at our general-purpose stations, or at the handful set up for one specific type of crime?",
        "ہتھیار عام طور پر کس نوعیت کے مقدمات میں سامنے آتے ہیں، اور کیا 2024 کے مقابلے میں اب یہ نوعیت بدل گئی ہے؟",
        "Kya log 2026 mein waqiaat ki police ko itni hi jaldi ittila de rahe hain jitni 2024 mein dete the?",
        # (C) cross-record consistency/confirmation
        "In the online banking fraud matter involving two separate victims, "
        "was each victim's case processed and recorded the same way?",
        "جب کوئی شخص تھانے آ کر شکایت درج کراتا ہے، تو کیا وہ کسی باقاعدہ ایف آئی آر سے منسلک ہو جاتی ہے، یا دونوں الگ الگ ہی رہتے ہیں؟",
        "کرمنل ریکارڈ سسٹم میں کتنے کیس مکمل ہو چکے ہیں اور کتنے ابھی زیرِ کارروائی ہیں — اور جہاں کسی ایک کیس کا الگ عدالتی ریکارڈ بھی موجود ہے، کیا دونوں ایک دوسرے سے مطابقت رکھتے ہیں؟",
        "اگر کسی گھریلو تشدد کی شکایت کو باقاعدہ کیس میں تبدیل ہونے کے طور پر درج کیا گیا ہو، تو کیا کیس ریکارڈ سے اس کی تصدیق ہو جاتی ہے؟",
        "Kya wusee criminal-history records mein koi aisa shakhs hai jo hamare apne darj kiye hue kisi case se match nahi karta?",
        "ایک طرف یہ دیکھیں کہ لوگوں پر کن دفعات میں مقدمے بن رہے ہیں، اور دوسری طرف یہ کہ وہ مقدمے عدالت میں کہاں تک پہنچے — کیا دونوں سے کیس لوڈ کی سنگینی کا ایک ہی اندازہ ہوتا ہے؟",
    ],
)
def test_meta_analysis_trigger_covers_module_11_evidence(query_text):
    route_result = {"route": "XNETWORK", "case_scope": "cross_case", "output_format": "chat"}
    assert classify_to_subagent(route_result, query_text) == META_ANALYSIS


# [AMENDMENT — findings.md Module 11] CP1 and A1 are the two gold-32
# questions from the same 18-question set that must NOT trigger Meta-
# Analysis: CP1 is a flat per-district rate (Module 13's job, not a
# decomposition candidate) and A1/CR4-shaped single-chain lookups are
# covered by `test_meta_analysis_trigger_does_not_fire_on_ordinary_queries`
# above via their English equivalents. Pinned separately so a future
# over-broadening of the trigger list is caught here first.
def test_meta_analysis_trigger_still_excludes_flat_aggregate_question():
    route_result = {"route": "XAGG", "case_scope": "cross_case", "output_format": "chat"}
    query_text = "Kaunsa zila apne case load ke lihaz se sab se zyada hathiyar baramad karta hai?"
    assert classify_to_subagent(route_result, query_text) != META_ANALYSIS


def test_allow_meta_analysis_false_suppresses_the_trigger():
    """[PRESERVE -- the recursion guard] meta_analysis.py passes
    allow_meta_analysis=False for every sub-query it dispatches, so a
    sub-query whose own text still matches the trigger must classify EXACTLY
    as if Module 10 did not exist, never recursing back into META_ANALYSIS."""
    route_result = {"route": "XNETWORK", "case_scope": "cross_case", "output_format": "chat"}
    query_text = "What are the recurring themes across all cases at this station, and cross-reference them with cases involving juvenile suspects?"
    assert classify_to_subagent(route_result, query_text) == META_ANALYSIS
    assert (
        classify_to_subagent(route_result, query_text, allow_meta_analysis=False) != META_ANALYSIS
    )


# ═══════════════════════════════════════════════════════════════════════
# [Gold-QA fix — Module 26, question M1] Skip Meta-Analysis decomposition
# for a year-over-year/period comparison already routed to XAGG — see
# `_TIME_COMPARISON_XAGG_PATTERNS`'s own module-level comment in router.py
# (shared with this file) for the full rationale.
# ═══════════════════════════════════════════════════════════════════════

def test_time_comparison_xagg_query_skips_meta_analysis():
    """M1's exact gold-dataset text matches _META_ANALYSIS_TRIGGER_PATTERNS'
    own comparison group ("compared to") -- without this guard it would
    decompose into two independently-classified sub-queries instead of
    reaching Large-Scale Aggregate directly, which is the live-confirmed
    defect this guard exists to prevent (see GOLD_QA_REMAINING_FIXES_PLAN.md)."""
    route_result = {"route": "XAGG", "case_scope": "cross_case", "output_format": "chat"}
    query_text = "What kinds of cases are we dealing with now compared to a couple of years back?"
    assert classify_to_subagent(route_result, query_text) == LARGE_SCALE_AGGREGATE


def test_time_comparison_guard_is_scoped_to_xagg_route_only():
    """The guard must not suppress a genuine Meta-Analysis decomposition for
    a DIFFERENT cross-case route that has no equivalent one-call aggregate
    to fall back to -- only route == XAGG skips decomposition."""
    route_result = {"route": "XNETWORK", "case_scope": "cross_case", "output_format": "chat"}
    query_text = "How has this network changed compared to what it looked like a couple of years back?"
    assert classify_to_subagent(route_result, query_text) == META_ANALYSIS


def test_time_comparison_guard_does_not_suppress_unrelated_xagg_meta_analysis_triggers():
    """An XAGG-routed query matching a DIFFERENT Meta-Analysis trigger must
    still decompose normally -- neither guard is a blanket "XAGG never
    decomposes" rule.

    [Gold-QA fix -- Module 41] Query text changed, assertion unchanged.
    This test used to assert over M2's gold text ("Is caseload growing
    faster at our general-purpose stations, or at the handful set up for
    one specific type of crime?"), encoding Module 26's narrowness as an
    invariant. Module 41 deliberately supersedes that: M2 resolves to
    `unsupported_station_type`, XAGG's honest "this data model has no
    station-type dimension" refusal, which is a purpose-built outcome and
    a better answer than decomposing into halves that each invent a split.
    M2's own new behaviour is asserted separately in
    `test_module41_m2_station_type_refusal_skips_decomposition` below.

    The text here is instead a caseload-review shape that resolves to
    `station_or_category_counts` -- the chain's trailing catch-all, i.e.
    XAGG has no purpose-built answer for it -- so it still decomposes, and
    for the reason the guard is actually about."""
    route_result = {"route": "XAGG", "case_scope": "cross_case", "output_format": "chat"}
    query_text = (
        "Acting as a duty supervisor, is there anything about the way these "
        "matters have been handled that looks unusual?"
    )
    assert not resolves_to_specific_aggregate(query_text)
    assert classify_to_subagent(route_result, query_text) == META_ANALYSIS


def test_file_output_still_overrides_meta_analysis_trigger():
    route_result = {"route": "RAG", "output_format": "file_pdf"}
    query_text = "Summarize the recurring patterns across all cases and flag any repeats."
    assert classify_to_subagent(route_result, query_text) == REPORT_DRAFTING


def test_file_output_still_overrides_provisional_triggers():
    route_result = {"route": "GRAPH", "output_format": "file_pdf"}
    assert classify_to_subagent(route_result, "give me a timeline of events") == REPORT_DRAFTING


def test_classify_to_subagent_default_query_text_matches_pre_amendment_behavior():
    """The one-argument call form (every pre-existing direct caller,
    including this file's own parametrized test above) must behave
    identically to before this amendment -- query_text="" can never match
    either provisional trigger."""
    assert classify_to_subagent({"route": "GRAPH", "output_format": "chat"}) == CASE_SUMMARIZATION


def test_provisional_triggers_do_not_fire_on_ordinary_queries():
    """Narrow-by-design check: everyday case questions must not accidentally
    contain trigger language and get silently rerouted."""
    ordinary_queries = [
        "who is the accused in this case",
        "what documents are attached to this case",
        "summarize this case for me",
        "کیس کی تفصیلات بتائیں",
    ]
    for q in ordinary_queries:
        assert classify_to_subagent({"route": "GRAPH", "output_format": "chat"}, q) == CASE_SUMMARIZATION


def test_route_query_contract_is_unaffected_by_the_classification_amendment():
    """[PRESERVE -- non-negotiable] The provisional overrides above live
    entirely inside classify_to_subagent() and must never touch
    router.py's own classification contract, which orchestrator.py still
    depends on for all of its own (still-live) routing. Structural proof:
    router.py's own set of valid routes is exactly the same nine values it
    has always been -- nothing in this amendment added a tenth."""
    from src.pipeline.router import _VALID_ROUTES

    assert set(_VALID_ROUTES) == {
        "DIRECT", "RAG", "WEB", "SQL", "GRAPH", "GRAPH_HYBRID", "XGRAPH", "XAGG", "XNETWORK",
    }


@pytest.mark.asyncio
async def test_dispatch_routes_to_correct_registered_mock(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "XAGG", "case_scope": "cross_case", "output_format": "chat"})

    expected = SubAgentResult(status=SubAgentStatus.OK, answer_text="42 cases")
    mock = _mock_sub_agent(LARGE_SCALE_AGGREGATE, expected)
    isolated_registry[LARGE_SCALE_AGGREGATE] = mock

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(_agent_input(query_text="how many cases in total"))

    assert result is expected
    assert len(mock.calls) == 1


@pytest.mark.asyncio
async def test_handle_threads_query_text_into_classify_to_subagent_for_provisional_triggers(
    monkeypatch, isolated_registry
):
    """End-to-end proof (not just the pure-function test above) that
    Supervisor.handle() actually passes agent_input.query_text through to
    classify_to_subagent() -- a query whose base route_query() result is
    an ordinary GRAPH classification still reaches Timeline Building when
    its text matches a provisional trigger."""
    _stub_route_query(monkeypatch, {"route": "GRAPH", "output_format": "chat"})

    expected = SubAgentResult(status=SubAgentStatus.OK, answer_text="3 events")
    mock = _mock_sub_agent(TIMELINE_BUILDING, expected)
    isolated_registry[TIMELINE_BUILDING] = mock
    # A registered Case Summarization mock proves dispatch went to Timeline
    # Building BECAUSE of the query text, not by some other accident (e.g.
    # an unregistered-route fallback landing on the wrong name).
    other_mock = _mock_sub_agent(CASE_SUMMARIZATION, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[CASE_SUMMARIZATION] = other_mock

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(_agent_input(query_text="give me a timeline of events for this case"))

    assert result is expected
    assert len(mock.calls) == 1
    assert len(other_mock.calls) == 0


# ═══════════════════════════════════════════════════════════════════════
# Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 3: GRAPH/GRAPH_HYBRID with
# no active case_id must never reach Case Summarization (nothing to
# summarize) — must short-circuit to a guidance EMPTY result instead.
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["GRAPH", "GRAPH_HYBRID"])
async def test_case_summarization_with_no_active_case_returns_guidance_not_dispatch(
    monkeypatch, isolated_registry, route
):
    """Live-confirmed failure (Gold-QA report §2.1, CR3/CR4/G2/G3/G5/G6):
    a cross-case-shaped question classified GRAPH/GRAPH_HYBRID (router.py
    always forces case_scope="within_case" for these two routes) with no
    case selected must never dispatch into Case Summarization -- it has
    nothing to scope to and would return a generic, misleading "no data
    found" caveat instead."""
    _stub_route_query(monkeypatch, {"route": route, "output_format": "chat"})
    mock = _mock_sub_agent(CASE_SUMMARIZATION, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[CASE_SUMMARIZATION] = mock

    caller = CallerContext(user_id="u1", role=Role.SUPERVISOR, active_case_id=None)
    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(_agent_input(caller=caller, query_text="compare these two cases"))

    assert result.status == SubAgentStatus.EMPTY
    assert len(mock.calls) == 0  # never dispatched
    assert any("case" in c.lower() for c in (result.caveats or []))


@pytest.mark.asyncio
async def test_case_summarization_with_active_case_still_dispatches_normally(
    monkeypatch, isolated_registry
):
    """Regression guard: the guard above must only fire on the NO-case-id
    combination -- an ordinary within-case query is unaffected."""
    _stub_route_query(monkeypatch, {"route": "GRAPH_HYBRID", "output_format": "chat"})
    expected = SubAgentResult(status=SubAgentStatus.OK, answer_text="summary")
    mock = _mock_sub_agent(CASE_SUMMARIZATION, expected)
    isolated_registry[CASE_SUMMARIZATION] = mock

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(_agent_input(query_text="summarize this case"))  # default caller has active_case_id="CASE-001"

    assert result is expected
    assert len(mock.calls) == 1


# ═══════════════════════════════════════════════════════════════════════
# Gold-QA fix — Module 3 follow-up: "All Cases" history-based case-scope
# inference. Only a case the ASSISTANT itself mentioned earlier in THIS
# session's history may be reused; the user's own text never counts.
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_no_case_selected_reuses_case_the_assistant_mentioned_earlier(
    monkeypatch, isolated_registry
):
    _stub_route_query(monkeypatch, {"route": "GRAPH_HYBRID", "output_format": "chat"})
    expected = SubAgentResult(status=SubAgentStatus.OK, answer_text="summary of fir-401-26")
    mock = _mock_sub_agent(CASE_SUMMARIZATION, expected)
    isolated_registry[CASE_SUMMARIZATION] = mock

    caller = CallerContext(user_id="u1", role=Role.SUPERVISOR, active_case_id=None)
    history_summary = (
        "User: who are the accused in FIR-401-26?\n"
        "Assistant: The accused in fir-401-26 is Faisal, son of Abdul Hamid."
    )
    agent_input = _agent_input(
        caller=caller,
        query_text="give me a case summary for this accused",
        conversation_context=ConversationContext(summary=history_summary),
    )

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(agent_input)

    # Dispatched for real, scoped to the case found in the assistant's own
    # prior turn -- not the guidance-only EMPTY fallback.
    assert len(mock.calls) == 1
    assert mock.calls[0].execution.caller.active_case_id == "fir-401-26"
    # The original caller-supplied input (still carrying active_case_id=None)
    # must never be mutated -- only a copy is threaded to the sub-agent.
    assert agent_input.execution.caller.active_case_id is None
    # Caller is told the scope was implied, not left to guess.
    assert any("fir-401-26" in c for c in (result.caveats or []))


@pytest.mark.asyncio
async def test_no_case_selected_ignores_a_case_id_the_user_typed_themselves(
    monkeypatch, isolated_registry
):
    """Security boundary: a case reference in the USER's own message or
    the user's own earlier turns must never be trusted to imply scope --
    only what the ASSISTANT already surfaced counts."""
    _stub_route_query(monkeypatch, {"route": "GRAPH_HYBRID", "output_format": "chat"})
    mock = _mock_sub_agent(CASE_SUMMARIZATION, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[CASE_SUMMARIZATION] = mock

    caller = CallerContext(user_id="u1", role=Role.SUPERVISOR, active_case_id=None)
    history_summary = "User: what happened in FIR-999-26?\nAssistant: I don't have access to that case."
    agent_input = _agent_input(
        caller=caller,
        query_text="tell me more about that case",
        conversation_context=ConversationContext(summary=history_summary),
    )

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(agent_input)

    assert len(mock.calls) == 0  # never dispatched -- the user-typed id doesn't count
    assert result.status == SubAgentStatus.EMPTY


@pytest.mark.asyncio
async def test_no_case_selected_and_no_history_reference_still_returns_guidance(
    monkeypatch, isolated_registry
):
    """Regression guard: with no conversation_context at all, behavior is
    unchanged from the original Module 3 fix (guidance, no dispatch)."""
    _stub_route_query(monkeypatch, {"route": "GRAPH_HYBRID", "output_format": "chat"})
    mock = _mock_sub_agent(CASE_SUMMARIZATION, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[CASE_SUMMARIZATION] = mock

    caller = CallerContext(user_id="u1", role=Role.SUPERVISOR, active_case_id=None)
    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(_agent_input(caller=caller, query_text="compare these two cases"))

    assert len(mock.calls) == 0
    assert result.status == SubAgentStatus.EMPTY


# ═══════════════════════════════════════════════════════════════════════
# Gold-QA fix — Module 3 follow-up (query-text source): a case named
# DIRECTLY in the current query ("summarize case 435/26") is a deliberate
# request, not a passive history reference — must still pass a real
# authorization check before use.
# ═══════════════════════════════════════════════════════════════════════

class _FakeGateway:
    """Minimal DataGateway stand-in exposing check_case_access and
    get_case_by_fir_number, recording every call each receives."""
    def __init__(self, authorized: bool = True, raises: bool = False, fir_number_map: dict | None = None):
        self.authorized = authorized
        self.raises = raises
        self.fir_number_map = fir_number_map or {}
        self.calls: list[tuple] = []
        self.fir_number_calls: list[str] = []

    async def check_case_access(self, case_id, user_id, user_role, min_role=None):
        self.calls.append((case_id, user_id, user_role))
        if self.raises:
            raise RuntimeError("db unreachable")
        return self.authorized

    async def get_case_by_fir_number(self, fir_number):
        self.fir_number_calls.append(fir_number)
        case_id = self.fir_number_map.get(fir_number)
        return {"case_id": case_id} if case_id else None


@pytest.mark.asyncio
async def test_cross_case_role_naming_a_case_in_query_dispatches_without_assignment_check(
    monkeypatch, isolated_registry
):
    """Supervisor/station-admin/platform-admin get the same blanket
    cross-case reach here that every other cross-case capability in this
    codebase already grants them (graph_retriever.CROSS_CASE_ROLES,
    xagg.py's platform-admin get_cases() call) -- no CaseAssignment row
    needed, and no gateway call made at all."""
    _stub_route_query(monkeypatch, {"route": "GRAPH_HYBRID", "output_format": "chat"})
    expected = SubAgentResult(status=SubAgentStatus.OK, answer_text="summary")
    mock = _mock_sub_agent(CASE_SUMMARIZATION, expected)
    isolated_registry[CASE_SUMMARIZATION] = mock
    gateway = _FakeGateway(authorized=False)  # would deny -- must never even be asked

    caller = CallerContext(user_id="u1", role=Role.SUPERVISOR, active_case_id=None)
    agent_input = _agent_input(caller=caller, query_text="summarize case 435/26 for me")

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(agent_input, gateway=gateway)

    assert len(mock.calls) == 1
    assert mock.calls[0].execution.caller.active_case_id == "case-435-26"
    assert gateway.calls == []  # cross-case role never needs the check
    assert any("case-435-26" in c for c in (result.caveats or []))


@pytest.mark.asyncio
async def test_investigator_naming_an_assigned_case_in_query_dispatches(
    monkeypatch, isolated_registry
):
    """An investigator (not a cross-case role) CAN name a case directly
    and get an answer -- provided a real CaseAssignment authorizes it."""
    _stub_route_query(monkeypatch, {"route": "GRAPH_HYBRID", "output_format": "chat"})
    expected = SubAgentResult(status=SubAgentStatus.OK, answer_text="summary")
    mock = _mock_sub_agent(CASE_SUMMARIZATION, expected)
    isolated_registry[CASE_SUMMARIZATION] = mock
    gateway = _FakeGateway(authorized=True)

    caller = CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id=None)
    agent_input = _agent_input(caller=caller, query_text="summarize FIR-435-26")

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(agent_input, gateway=gateway)

    assert len(mock.calls) == 1
    assert mock.calls[0].execution.caller.active_case_id == "fir-435-26"
    assert gateway.calls == [("fir-435-26", "u1", "investigator")]
    assert result.status == SubAgentStatus.OK


@pytest.mark.asyncio
async def test_investigator_naming_an_unassigned_case_in_query_is_denied(
    monkeypatch, isolated_registry
):
    """The core security guarantee: an investigator naming a case they
    have no assignment to must be denied outright -- never silently
    fall through to the generic guidance message (which would look like
    a routing quirk, not an access decision), and never dispatch."""
    _stub_route_query(monkeypatch, {"route": "GRAPH_HYBRID", "output_format": "chat"})
    mock = _mock_sub_agent(CASE_SUMMARIZATION, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[CASE_SUMMARIZATION] = mock
    gateway = _FakeGateway(authorized=False)

    caller = CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id=None)
    agent_input = _agent_input(caller=caller, query_text="summarize FIR-999-99")

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(agent_input, gateway=gateway)

    assert len(mock.calls) == 0
    assert result.status == SubAgentStatus.ABSTAINED
    assert result.error is not None
    assert result.error.kind == "permission_denied"
    assert any("fir-999-99" in c.lower() for c in (result.caveats or []))


@pytest.mark.asyncio
async def test_investigator_naming_a_case_with_no_gateway_available_fails_closed(
    monkeypatch, isolated_registry
):
    """No gateway to check against -- must deny, never best-effort-grant."""
    _stub_route_query(monkeypatch, {"route": "GRAPH_HYBRID", "output_format": "chat"})
    mock = _mock_sub_agent(CASE_SUMMARIZATION, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[CASE_SUMMARIZATION] = mock

    caller = CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id=None)
    agent_input = _agent_input(caller=caller, query_text="summarize FIR-435-26")

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(agent_input, gateway=None)

    assert len(mock.calls) == 0
    assert result.status == SubAgentStatus.ABSTAINED
    assert result.error.kind == "permission_denied"


@pytest.mark.asyncio
async def test_query_named_case_takes_priority_over_history_reference(
    monkeypatch, isolated_registry
):
    """When both sources have something, the CURRENT query's own explicit
    reference wins over an older history mention."""
    _stub_route_query(monkeypatch, {"route": "GRAPH_HYBRID", "output_format": "chat"})
    expected = SubAgentResult(status=SubAgentStatus.OK, answer_text="summary")
    mock = _mock_sub_agent(CASE_SUMMARIZATION, expected)
    isolated_registry[CASE_SUMMARIZATION] = mock

    caller = CallerContext(user_id="u1", role=Role.SUPERVISOR, active_case_id=None)
    agent_input = _agent_input(
        caller=caller,
        query_text="now summarize case-200-26 instead",
        conversation_context=ConversationContext(
            summary="Assistant: Here is what I found in fir-100-26."
        ),
    )

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(agent_input, gateway=_FakeGateway(authorized=True))

    assert mock.calls[0].execution.caller.active_case_id == "case-200-26"


# ═══════════════════════════════════════════════════════════════════════
# Gold-QA fix — Module 3 follow-up (bare-number source): "summarize
# 435/26" or "435 26" with no "case"/"FIR" word, resolved via a real DB
# lookup against the case's own display number rather than trusted by
# shape alone.
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
@pytest.mark.parametrize("query_text", [
    "summarize 435/26 for me",
    "what's going on with 435 26",
])
async def test_bare_number_pair_resolves_when_it_matches_a_real_case(
    monkeypatch, isolated_registry, query_text
):
    _stub_route_query(monkeypatch, {"route": "GRAPH_HYBRID", "output_format": "chat"})
    expected = SubAgentResult(status=SubAgentStatus.OK, answer_text="summary")
    mock = _mock_sub_agent(CASE_SUMMARIZATION, expected)
    isolated_registry[CASE_SUMMARIZATION] = mock
    gateway = _FakeGateway(authorized=True, fir_number_map={"435/26": "fir-435-26"})

    caller = CallerContext(user_id="u1", role=Role.SUPERVISOR, active_case_id=None)
    agent_input = _agent_input(caller=caller, query_text=query_text)

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(agent_input, gateway=gateway)

    assert len(mock.calls) == 1
    assert mock.calls[0].execution.caller.active_case_id == "fir-435-26"
    assert gateway.fir_number_calls == ["435/26"]


@pytest.mark.asyncio
async def test_bare_number_pair_with_no_matching_case_is_silently_ignored(
    monkeypatch, isolated_registry
):
    """The core safety property: a query that happens to contain two
    numbers with no real case behind them must never be mistaken for a
    case reference -- falls through to guidance exactly as if the numbers
    were never there."""
    _stub_route_query(monkeypatch, {"route": "GRAPH_HYBRID", "output_format": "chat"})
    mock = _mock_sub_agent(CASE_SUMMARIZATION, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[CASE_SUMMARIZATION] = mock
    gateway = _FakeGateway(authorized=True, fir_number_map={})  # nothing resolves

    caller = CallerContext(user_id="u1", role=Role.SUPERVISOR, active_case_id=None)
    agent_input = _agent_input(caller=caller, query_text="he called 435 26 times that week")

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(agent_input, gateway=gateway)

    assert len(mock.calls) == 0
    assert result.status == SubAgentStatus.EMPTY


@pytest.mark.asyncio
async def test_bare_number_pair_resolving_to_an_unassigned_case_is_denied(
    monkeypatch, isolated_registry
):
    """Same access-control guarantee as the keyword-anchored source: a
    real case that resolves but the caller isn't authorized for must be
    denied outright, not silently skipped."""
    _stub_route_query(monkeypatch, {"route": "GRAPH_HYBRID", "output_format": "chat"})
    mock = _mock_sub_agent(CASE_SUMMARIZATION, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[CASE_SUMMARIZATION] = mock
    gateway = _FakeGateway(authorized=False, fir_number_map={"435/26": "fir-435-26"})

    caller = CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id=None)
    agent_input = _agent_input(caller=caller, query_text="summarize 435/26")

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(agent_input, gateway=gateway)

    assert len(mock.calls) == 0
    assert result.status == SubAgentStatus.ABSTAINED
    assert result.error.kind == "permission_denied"


@pytest.mark.asyncio
async def test_keyword_anchored_query_reference_wins_over_bare_number_fallback(
    monkeypatch, isolated_registry
):
    """The bare-number source is the LAST resort -- an explicit "case"/
    "FIR" reference elsewhere in the query must win even if a bare
    number pair also happens to be present."""
    _stub_route_query(monkeypatch, {"route": "GRAPH_HYBRID", "output_format": "chat"})
    expected = SubAgentResult(status=SubAgentStatus.OK, answer_text="summary")
    mock = _mock_sub_agent(CASE_SUMMARIZATION, expected)
    isolated_registry[CASE_SUMMARIZATION] = mock
    gateway = _FakeGateway(authorized=True, fir_number_map={"1 2": "fir-wrong-case"})

    caller = CallerContext(user_id="u1", role=Role.SUPERVISOR, active_case_id=None)
    agent_input = _agent_input(caller=caller, query_text="summarize case-435-26, item 1 2")

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(agent_input, gateway=gateway)

    assert mock.calls[0].execution.caller.active_case_id == "case-435-26"
    assert gateway.fir_number_calls == []  # bare-number path never even tried


@pytest.mark.asyncio
async def test_handle_allow_meta_analysis_false_reaches_classify_to_subagent(monkeypatch, isolated_registry):
    """[AMENDMENT — findings.md Module 10] End-to-end proof that
    Supervisor.handle()'s own allow_meta_analysis parameter actually reaches
    classify_to_subagent() -- a query whose text matches the decomposition
    trigger dispatches to META_ANALYSIS by default, but NOT when the caller
    passes allow_meta_analysis=False (the exact call meta_analysis.py itself
    makes for every sub-query it dispatches)."""
    # route="XAGG" deliberately, not XNETWORK -- avoids also tripping
    # _GLOBAL_SEARCH_TRIGGER_PATTERNS's own "recurring themes across" match,
    # which would otherwise confound which override this test is isolating.
    _stub_route_query(monkeypatch, {"route": "XAGG", "case_scope": "cross_case", "output_format": "chat"})
    # [Gold-QA fix -- Module 41] Query text changed, assertions unchanged.
    # The original text ("Aggregate the weapon types used across all cases
    # this year and flag any case where the weapon matches an unresolved
    # case's weapon.") now resolves to `weapon_compliance_scan` -- "weapon"
    # + the literal "flag", which is a `_COMPLIANCE_TERMS` entry -- so
    # Module 41's skip guard sends it straight to Large-Scale Aggregate and
    # it can no longer isolate the allow_meta_analysis plumbing this test
    # is actually about. Swapped for CR3's record-consistency shape, which
    # `meta_analysis.py::_DECOMPOSITION_PLANS` owns outright and which
    # Module 41's guard is deliberately subordinate to, so it reaches
    # META_ANALYSIS for a reason that cannot drift with the aggregate chain.
    query_text = (
        "In the online banking fraud matter involving two separate victims, "
        "was each victim's case processed and recorded the same way?"
    )

    meta_mock = _mock_sub_agent(META_ANALYSIS, SubAgentResult(status=SubAgentStatus.OK, answer_text="combined"))
    xagg_mock = _mock_sub_agent(LARGE_SCALE_AGGREGATE, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[META_ANALYSIS] = meta_mock
    isolated_registry[LARGE_SCALE_AGGREGATE] = xagg_mock

    sup = Supervisor(registry=isolated_registry)

    await sup.handle(_agent_input(query_text=query_text))
    assert len(meta_mock.calls) == 1
    assert len(xagg_mock.calls) == 0

    await sup.handle(_agent_input(query_text=query_text), allow_meta_analysis=False)
    assert len(meta_mock.calls) == 1  # unchanged -- not called again
    assert len(xagg_mock.calls) == 1


@pytest.mark.asyncio
async def test_dispatch_does_not_call_unrelated_registered_mocks(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})

    semantic_mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    other_mock = _mock_sub_agent(LARGE_SCALE_AGGREGATE, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = semantic_mock
    isolated_registry[LARGE_SCALE_AGGREGATE] = other_mock

    sup = Supervisor(registry=isolated_registry)
    await sup.handle(_agent_input())

    assert len(semantic_mock.calls) == 1
    assert len(other_mock.calls) == 0


# ═══════════════════════════════════════════════════════════════════════
# (b) ExecutionContext (wrapping CallerContext) threaded through completely
# unchanged — [RENAMED, AGENT_HARNESS_IMPLEMENTATION_PLAN.md §10.1]
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_caller_context_passed_through_unchanged_by_identity(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})

    caller = _caller(role=Role.PLATFORM_ADMIN, preferred_language="ur")
    agent_input = _agent_input(caller=caller)

    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = mock

    sup = Supervisor(registry=isolated_registry)
    await sup.handle(agent_input)

    received = mock.calls[0]
    # Identity, not just equality: the Supervisor must not reconstruct,
    # copy, or merge this object with anything else.
    assert received is agent_input
    assert received.execution.caller is caller
    assert received.execution.caller.role is Role.PLATFORM_ADMIN
    assert received.execution.caller.preferred_language == "ur"
    assert received.execution.caller.active_case_id == "CASE-001"


@pytest.mark.asyncio
async def test_caller_role_never_defaulted_for_investigator(monkeypatch, isolated_registry):
    # Regression guard for the historical bug documented throughout the
    # design/interfaces docs: role must never be silently defaulted to
    # "investigator" (or anything else) on the way through the Supervisor.
    _stub_route_query(monkeypatch, {"route": "XGRAPH", "case_scope": "cross_case", "output_format": "chat"})

    caller = _caller(role=Role.STATION_ADMIN)
    mock = _mock_sub_agent(CROSS_CASE_LINKAGE, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[CROSS_CASE_LINKAGE] = mock

    sup = Supervisor(registry=isolated_registry)
    await sup.handle(_agent_input(caller=caller))

    assert mock.calls[0].execution.caller.role is Role.STATION_ADMIN


# ═══════════════════════════════════════════════════════════════════════
# (c) unregistered route
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_unregistered_route_returns_typed_not_available_result(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "XAGG", "case_scope": "cross_case", "output_format": "chat"})

    sup = Supervisor(registry=isolated_registry)  # empty registry
    result = await sup.handle(_agent_input())

    assert isinstance(result, SubAgentResult)
    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.error is not None
    assert result.error.kind == "upstream_failure"
    assert LARGE_SCALE_AGGREGATE in result.error.message
    assert any(LARGE_SCALE_AGGREGATE in c for c in result.caveats)


@pytest.mark.asyncio
async def test_unregistered_route_does_not_raise(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "GRAPH", "output_format": "chat"})
    sup = Supervisor(registry=isolated_registry)
    # Must not raise — this is expected, not exceptional, behavior for
    # every route right now.
    result = await sup.handle(_agent_input())
    assert result.status == SubAgentStatus.ABSTAINED


@pytest.mark.asyncio
async def test_module_level_register_and_unregister(monkeypatch):
    """Exercises the real module-level registry (register/unregister),
    not an isolated dict — this is the mechanism a future sub-agent module
    actually uses at import time."""
    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    try:
        register(mock)
        assert supervisor_mod.get_registered(SEMANTIC_SEARCH) is mock

        _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})
        sup = Supervisor()  # no override -> reads the module-level registry
        result = await sup.handle(_agent_input())
        assert result.status == SubAgentStatus.OK
        assert len(mock.calls) == 1
    finally:
        unregister(SEMANTIC_SEARCH)
    assert supervisor_mod.get_registered(SEMANTIC_SEARCH) is None


def test_register_rejects_unknown_sub_agent_name():
    bad = _mock_sub_agent("Not A Real Sub-Agent", SubAgentResult(status=SubAgentStatus.OK))
    with pytest.raises(ValueError):
        register(bad)


# ═══════════════════════════════════════════════════════════════════════
# (d) PipelineEvent shape
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_pipeline_events_emitted_on_successful_dispatch(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})
    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = mock

    events: list[PipelineEvent] = []
    sup = Supervisor(registry=isolated_registry)
    await sup.handle(_agent_input(), on_event=events.append)

    assert len(events) == 2
    for evt in events:
        assert isinstance(evt, PipelineEvent)
        assert evt.step == "supervisor:dispatch"
        assert evt.status in ("active", "done", "error", "retry", "skipped")
        assert isinstance(evt.detail, str) and evt.detail

    assert events[0].status == "active"
    assert SEMANTIC_SEARCH in events[0].detail
    assert events[1].status == "done"
    assert SEMANTIC_SEARCH in events[1].detail
    assert "ok" in events[1].detail.lower()


@pytest.mark.asyncio
async def test_pipeline_events_emitted_on_unregistered_route(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "XNETWORK", "case_scope": "cross_case", "output_format": "chat"})

    events: list[PipelineEvent] = []
    sup = Supervisor(registry=isolated_registry)  # empty
    await sup.handle(_agent_input(), on_event=events.append)

    assert len(events) == 2
    assert events[0].status == "active"
    assert events[1].status == "skipped"
    assert CROSS_CASE_LINKAGE in events[1].detail


@pytest.mark.asyncio
async def test_on_event_is_optional(monkeypatch, isolated_registry):
    # Must not require a sink — dispatch works fine with none given.
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})
    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = mock

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(_agent_input())
    assert result.status == SubAgentStatus.OK


# ═══════════════════════════════════════════════════════════════════════
# (e) on_event threaded down to the sub-agent itself
# [AMENDMENT — pre-Phase-7 contract amendment, mirrors §10/§11's pattern]
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_on_event_forwarded_to_subagent_when_given(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})
    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = mock

    sup = Supervisor(registry=isolated_registry)
    sink = lambda evt: None
    await sup.handle(_agent_input(), on_event=sink)

    assert mock.on_events_received == [sink]


@pytest.mark.asyncio
async def test_on_event_forwarded_as_none_when_not_given(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})
    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = mock

    sup = Supervisor(registry=isolated_registry)
    await sup.handle(_agent_input())

    assert mock.on_events_received == [None]


# ═══════════════════════════════════════════════════════════════════════
# (f) gateway threaded down to the sub-agent itself
# [AMENDMENT — pre-Phase-8 contract amendment, mirrors §12/(e) above]
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_gateway_forwarded_to_subagent_when_given(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})
    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = mock

    sup = Supervisor(registry=isolated_registry)
    fake_gateway = object()
    await sup.handle(_agent_input(), gateway=fake_gateway)

    assert mock.gateways_received == [fake_gateway]


@pytest.mark.asyncio
async def test_gateway_forwarded_as_none_when_not_given(monkeypatch, isolated_registry):
    _stub_route_query(monkeypatch, {"route": "RAG", "output_format": "chat"})
    mock = _mock_sub_agent(SEMANTIC_SEARCH, SubAgentResult(status=SubAgentStatus.OK))
    isolated_registry[SEMANTIC_SEARCH] = mock

    sup = Supervisor(registry=isolated_registry)
    await sup.handle(_agent_input())

    assert mock.gateways_received == [None]


# ── Finding AA regression: data-quality queries must reach their sub-agent ───
# The Data-Quality/Extraction-Coverage sub-agent is selected by trigger
# patterns on top of a real retrieval route. When the router classified a
# data-quality question as DIRECT, the harness handed the turn back to the
# legacy path and the sub-agent never ran (verify-log Finding AA) — DIRECT
# performs no retrieval, so it cannot inspect a case at all. These pin the
# selection for every route the query can legitimately land on.

import pytest as _pytest

from src.pipeline.harness.supervisor import classify_to_subagent as _classify

_DQ_QUERIES = [
    "What is the data quality and extraction coverage for this case — are any fields missing or incomplete?",
    "Are there any gaps or missing fields in this case's records?",
]


@_pytest.mark.parametrize("query", _DQ_QUERIES)
@_pytest.mark.parametrize("route", ["GRAPH_HYBRID", "GRAPH", "RAG"])
def test_data_quality_query_selects_its_sub_agent(query, route):
    selected = _classify(
        {"route": route, "case_scope": "within_case"}, query, allow_meta_analysis=True
    )
    assert selected == "Data-Quality/Extraction-Coverage"


# ══════════════════════════════════════════════════════════════════════
# [Gold-QA fix — Module 41, questions G2/G5] The Meta-Analysis skip guard,
# generalised from Module 26's one time-comparison shape to "any question
# XAGG resolves to a purpose-built single-call aggregate".
#
# G2 regressed 0.4 → 0.0 and G5 0.6 → 0.0 in the 2026-09-08 post-fix
# evaluation because both route XAGG, both match
# `_META_ANALYSIS_TRIGGER_PATTERNS`, and neither matches
# `_TIME_COMPARISON_XAGG_PATTERNS` — so Meta-Analysis decomposed a question
# `run_aggregate()` answers in one call, a sub-question errored, and the
# synthesis was rejected as ungrounded.
# ══════════════════════════════════════════════════════════════════════

_XAGG_CROSS_CASE = {"route": "XAGG", "case_scope": "cross_case", "output_format": "chat"}

# The literal gold text of the two regressed questions, copied verbatim from
# `evaluation/Gold_QA_Dataset_Final32_With_Answers.json`. Asserting over the
# real strings, not paraphrases, is the whole point — the regression was
# specific to these exact texts.
_G2_GOLD = (
    "فرض کریں آپ کسی ایس ایچ او کو بریفنگ دے رہے ہیں کہ کون سے مقدمے دب کر یا "
    "نظر سے اوجھل ہو کر رہ سکتے ہیں — آپ کن چیزوں کی نشاندہی کریں گے؟"
)
_G5_GOLD = (
    "Baramad shuda hathiyaron ki record keeping ko dekhte hue, kya koi aisi "
    "baat hai jo compliance ke lihaz se flag karne layak ho?"
)
# M1's gold text — Module 26's own guard, which must keep working.
_M1_GOLD = "What kinds of cases are we dealing with now compared to a couple of years back?"
# The three questions Module 29's `_DECOMPOSITION_PLANS` own outright.
_CR3_GOLD = (
    "In the online banking fraud matter involving two separate victims, was "
    "each victim's case processed and recorded the same way?"
)
_G1_GOLD = (
    "Acting as a crime analyst, review our current caseload and flag anything "
    "that looks unusual or worth monitoring."
)
_G6_GOLD = (
    "Yahan naye tainaat hone wale afsar ke liye ek mukhtasar orientation note "
    "likhein — unhein mojooda case load se kya tawaqqo rakhni chahiye?"
)
_M2_GOLD = (
    "Is caseload growing faster at our general-purpose stations, or at the "
    "handful set up for one specific type of crime?"
)
# [Gold-QA fix — Module 43] M7's gold text.
_M7_GOLD = (
    "Kya log 2026 mein waqiaat ki police ko itni hi jaldi ittila de rahe hain "
    "jitni 2024 mein dete the?"
)


def test_module43_m7_gold_text_skips_decomposition_and_reaches_the_aggregate():
    """M7 must reach Large-Scale Aggregate in ONE call.

    Module 43's investigation: M7 was filed at FactualCorrectness 0.0,
    contradicting Module 22's recorded live verification. Layers 1 and 2
    (the aggregate, and `run_aggregate()`'s dispatch) were re-derived
    against the live graph and both return gold exactly, and three live
    `/api/chat` runs did too — so Module 22 was right.

    The one path that still leads back to a wrong-metric M7 answer is
    decomposition: `_reporting_delay_rate_by_year()` (the delay-REASON
    rate, 0% -> 14.9%, which is what the only M7 answer recorded in this
    repository actually contains) has no dispatch entry of its own any
    more, but a sub-question that drops M7's comparison vocabulary lands on
    the neighbouring reporting-delay family instead. Module 41's guard is
    what keeps M7 out of Meta-Analysis; nothing pinned that for M7
    specifically until now."""
    assert resolve_aggregate_kind(_M7_GOLD) == "incident_to_report_minutes_by_year"
    assert resolves_to_specific_aggregate(_M7_GOLD)
    assert classify_to_subagent(_XAGG_CROSS_CASE, _M7_GOLD) == LARGE_SCALE_AGGREGATE


def test_module41_g2_gold_text_skips_decomposition_and_reaches_the_aggregate():
    """G2's literal gold text must reach Large-Scale Aggregate, not
    Meta-Analysis. Before Module 41 the live trace read
    `route='XAGG' -> sub-agent='Meta-Analysis'`."""
    assert resolve_aggregate_kind(_G2_GOLD) == "case_completeness_scan"
    assert resolves_to_specific_aggregate(_G2_GOLD)
    assert classify_to_subagent(_XAGG_CROSS_CASE, _G2_GOLD) == LARGE_SCALE_AGGREGATE


def test_module41_g5_gold_text_skips_decomposition_and_reaches_the_aggregate():
    """G5's literal gold text, same shape as G2's."""
    assert resolve_aggregate_kind(_G5_GOLD) == "weapon_compliance_scan"
    assert resolves_to_specific_aggregate(_G5_GOLD)
    assert classify_to_subagent(_XAGG_CROSS_CASE, _G5_GOLD) == LARGE_SCALE_AGGREGATE


def test_module41_g2_and_g5_still_match_the_meta_analysis_trigger_patterns():
    """The negative half of the two tests above: they are only meaningful
    because both questions DO match `_META_ANALYSIS_TRIGGER_PATTERNS` and
    would still be decomposed without the guard. If a later edit removes
    those pattern matches, the two tests above would start passing for the
    wrong reason — this one fails instead."""
    for text in (_G2_GOLD, _G5_GOLD):
        assert any(
            pat.search(text) for pat in supervisor_mod._META_ANALYSIS_TRIGGER_PATTERNS
        )
        assert classify_to_subagent(_XAGG_CROSS_CASE, text, allow_meta_analysis=True) == (
            LARGE_SCALE_AGGREGATE
        )


def test_module41_m1_still_skips_via_the_original_time_comparison_guard():
    """Module 26's guard is untouched and still load-bearing on its own
    terms — M1 must skip because of the pattern list, independent of
    whatever the aggregate chain resolves it to."""
    from src.pipeline.router import _TIME_COMPARISON_XAGG_PATTERNS

    assert any(pat.search(_M1_GOLD) for pat in _TIME_COMPARISON_XAGG_PATTERNS)
    assert classify_to_subagent(_XAGG_CROSS_CASE, _M1_GOLD) == LARGE_SCALE_AGGREGATE


@pytest.mark.parametrize(
    "label, query_text, plan_name",
    [
        ("CR3", _CR3_GOLD, "record_consistency"),
        ("G1", _G1_GOLD, "caseload_review"),
        ("G6", _G6_GOLD, "orientation_note"),
    ],
)
def test_module41_guard_is_subordinate_to_module29_decomposition_plans(
    label, query_text, plan_name
):
    """CR3, G1 and G6 are the questions XAGG genuinely CANNOT answer in one
    call. Module 41's guard must never repeal Module 29 for them.

    G1 is the load-bearing case: it DOES resolve to a specific aggregate
    (`case_completeness_scan`), so without the plan veto in
    `_xagg_answers_in_one_call()` it would silently stop decomposing."""
    from src.pipeline.harness.agents.meta_analysis import _match_decomposition_plan

    plan = _match_decomposition_plan(query_text)
    assert plan is not None and plan.name == plan_name
    assert not supervisor_mod._xagg_answers_in_one_call(query_text)
    assert classify_to_subagent(_XAGG_CROSS_CASE, query_text) == META_ANALYSIS


def test_module41_g1_would_resolve_specifically_but_for_its_plan():
    """Pins the reason the previous test's G1 case is not a coincidence."""
    assert resolves_to_specific_aggregate(_G1_GOLD)
    assert resolve_aggregate_kind(_G1_GOLD) == "case_completeness_scan"


def test_module41_m2_station_type_refusal_skips_decomposition():
    """`unsupported_aggregate` counts as resolved — the deliberate call
    documented on `xagg._UNSUPPORTED_AGGREGATE_KINDS`. M2 has no
    station-type dimension in the data model at all, so XAGG's honest
    refusal is the correct outcome and decomposing it produces two halves
    that each invent a split instead."""
    from src.pipeline.xagg import _UNSUPPORTED_AGGREGATE_KINDS

    kind = resolve_aggregate_kind(_M2_GOLD)
    assert kind == "unsupported_station_type"
    assert kind in _UNSUPPORTED_AGGREGATE_KINDS
    assert classify_to_subagent(_XAGG_CROSS_CASE, _M2_GOLD) == LARGE_SCALE_AGGREGATE


def test_module41_generic_fallbacks_do_not_count_as_resolvable():
    """Reaching one of `run_aggregate()`'s three trailing catch-alls means
    NOTHING matched — the opposite of evidence that XAGG has a single-call
    answer — so those questions must still be free to decompose."""
    from src.pipeline.xagg import _GENERIC_AGGREGATE_KINDS

    for text in (
        "list all cases",
        "how many cases in total",
        "how many cases per station",
    ):
        assert resolve_aggregate_kind(text) in _GENERIC_AGGREGATE_KINDS
        assert not resolves_to_specific_aggregate(text)


def test_module41_bare_entity_recurrence_does_not_count_as_resolvable():
    """A query that matched only a NOUN is too weak a signal to suppress
    decomposition — see `xagg._ENTITY_RECURRENCE_AGGREGATE_KINDS`. The
    compound query below is the measured case: the recurrence aggregate
    answers its first half only."""
    text = (
        "Aggregate the vehicles seen across all cases this year and cross-"
        "reference any that also appear in an unresolved matter."
    )
    assert resolve_aggregate_kind(text) == "graph_recurrence_vehicle"
    assert not resolves_to_specific_aggregate(text)


def test_module41_guard_is_scoped_to_the_xagg_route_only():
    """Same scoping Module 26 established: a non-XAGG cross-case route has
    no equivalent one-call aggregate to fall back to, so it must still
    decompose even when the text would resolve to an aggregate kind."""
    assert resolves_to_specific_aggregate(_G5_GOLD)
    for route in ("XNETWORK", "XGRAPH"):
        route_result = {"route": route, "case_scope": "cross_case", "output_format": "chat"}
        assert classify_to_subagent(route_result, _G5_GOLD) == META_ANALYSIS


def test_module41_guard_respects_allow_meta_analysis_false_recursion_guard():
    """A sub-query dispatch (`allow_meta_analysis=False`) must classify
    exactly as before — the new guard runs ahead of the Meta-Analysis
    branch, so it can only ever move a query TOWARDS Large-Scale
    Aggregate, which is where that branch already sends it."""
    assert classify_to_subagent(
        _XAGG_CROSS_CASE, _G2_GOLD, allow_meta_analysis=False
    ) == LARGE_SCALE_AGGREGATE


def test_module41_resolution_is_side_effect_free():
    """The cost argument, pinned. `resolve_aggregate_kind()` must never
    acquire an `await`, a gateway call or an RLS/context-var write — the
    supervisor calls it at routing time, on every XAGG query, purely to
    ASK which aggregate would answer. Executing one there would double the
    DB and graph work for every aggregate question in the system."""
    import ast
    import inspect
    import textwrap

    import src.pipeline.xagg as xagg_mod

    for fn in (xagg_mod.resolve_aggregate_kind, xagg_mod.resolves_to_specific_aggregate):
        assert not inspect.iscoroutinefunction(fn)
        tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
        # Compare over the parsed body only -- comments and the docstring
        # (which legitimately DISCUSS the gateway and the await this test
        # forbids) are not code.
        fn_node = tree.body[0]
        body = fn_node.body
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            body = body[1:]
        code = "\n".join(ast.unparse(node) for node in body)
        assert "await " not in code
        assert "gateway" not in code
        assert "current_rls_active" not in code
        assert "current_cross_case" not in code
        assert "log_audit_event" not in code
        # And structurally: no await/async expression anywhere in the tree.
        for node in ast.walk(fn_node):
            assert not isinstance(node, (ast.Await, ast.AsyncFor, ast.AsyncWith))


def test_module41_all_32_gold_questions_dispatch_change_is_exactly_the_expected_set():
    """The all-32 negative control. Assuming an XAGG route for every
    question (the guard's precondition), exactly these nine change from
    Meta-Analysis to Large-Scale Aggregate and nothing else moves.

    Recorded in full in `docs/gold-qa-wave2-results/MODULE41_RESULT.md`.
    Anything added or removed here is a real behavioural change that needs
    re-verifying live, not a test to update casually."""
    import io as _io
    import json as _json
    from pathlib import Path as _Path

    dataset = (
        _Path(__file__).resolve().parents[1]
        / "evaluation"
        / "Gold_QA_Dataset_Final32_With_Answers.json"
    )
    questions = _json.load(_io.open(dataset, encoding="utf-8"))
    assert len(questions) == 32

    from src.pipeline.router import _TIME_COMPARISON_XAGG_PATTERNS

    changed = set()
    for q in questions:
        text = q["question"]
        matched_meta = any(
            pat.search(text) for pat in supervisor_mod._META_ANALYSIS_TRIGGER_PATTERNS
        )
        matched_time = any(pat.search(text) for pat in _TIME_COMPARISON_XAGG_PATTERNS)
        # "before Module 41": Meta-Analysis iff a trigger matched and the
        # time-comparison guard did not already claim it.
        before = META_ANALYSIS if (matched_meta and not matched_time) else LARGE_SCALE_AGGREGATE
        after = classify_to_subagent(_XAGG_CROSS_CASE, text)
        if before != after:
            assert before == META_ANALYSIS and after == LARGE_SCALE_AGGREGATE
            changed.add(q["id"])

    assert changed == {"CR6", "CR7", "CR8", "M2", "M4", "M7", "G2", "G3", "G5"}
