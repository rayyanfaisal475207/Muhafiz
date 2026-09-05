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
    query_text = (
        "Aggregate the weapon types used across all cases this year and flag "
        "any case where the weapon matches an unresolved case's weapon."
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
