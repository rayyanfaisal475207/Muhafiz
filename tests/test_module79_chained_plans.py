"""
[Gold-QA fix — Module 79] Chained aggregate plans — a plan step that runs
TWO aggregates in sequence, the second narrowed by a field of the first's
output.

The defect (live, 2026-09-14): *"How is the most frequently cited offence
pattern distributed across districts?"* ran one aggregate (cases per
district) and honestly said the data does not specify the most-cited
offence. Both halves existed as aggregates; no plan structure could chain
them.

What is pinned here, in both directions:
  (a) `pluck()` / `ThenStep` / `filters_for()` — the small pure pieces;
  (b) `run_aggregate_chain()` — the second aggregate receives the allow-list
      the first's value selects, via `jurisdiction_case_ids`; every failure
      degrades, never raises; a family mismatch on either side drops the
      chain; a `take` that finds nothing serves the first half and says so;
      the no-`take` form runs the second aggregate unnarrowed;
  (c) Meta-Analysis: the live example and its paraphrases match the new
      plan; the two new plans match NONE of the 32 gold questions; every
      pre-existing plan decomposes to exactly `list(plan.sub_queries)` —
      byte-identical to before this module — and a string sub-query still
      dispatches through `Supervisor.handle`, while a chained step never
      does; the chained step's dispatch strings resolve to the families the
      step names;
  (d) RAG: KB9's data-half plan carries a `then` (the heirs figure) whose
      string is KB4's verbatim; every other plan has `then=None` and takes
      the unchanged `xagg_tool` path; the chained path returns the same
      chunk id/shape.

Before this module: this file fails at collection (`ThenStep`,
`_ChainedSubQuery`, `run_aggregate_chain` do not exist); with the imports
guarded, `_match_decomposition_plan(LIVE)` is None and KB9's plan has no
second aggregate.
"""
from __future__ import annotations

import json
import os

import pytest

import src.pipeline.harness.agents.meta_analysis as ma_mod
import src.pipeline.harness.tools.chained_aggregate as chain_mod
import src.pipeline.harness.tools.rag as rag_mod
from src.pipeline.aggregate_filters import AggregateFilters
from src.pipeline.harness.agents.meta_analysis import _ChainedSubQuery, meta_analysis
from src.pipeline.harness.supervisor import Supervisor
from src.pipeline.harness.tools.chained_aggregate import (
    ThenStep,
    filters_for,
    pluck,
    run_aggregate_chain,
)
from src.pipeline.harness.types import (
    CallerContext,
    ExecutionContext,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ValidationStatus,
)
from src.pipeline.xagg import resolve_aggregate_kind

LIVE = "How is the most frequently cited offence pattern distributed across districts?"
P1_EN = "Which offence section comes up most often in the FIRs, and which districts are those FIRs spread across?"
P2_RU = "Sab se zyada istemal hone wali dafa kaun si hai aur us ke cases kin zilon mein hain?"
P3_SWAP = "Which district has the most FIRs, and what sections are those FIRs charged under?"

# `resolve_aggregate_kind()` names the DISPATCH branch; the result dict's
# `kind` is what the chain checks. Only one family spells them differently.
_DISPATCH_TO_RESULT_KIND = {"top_districts_by": "district_breakdown"}


def _gold32():
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "evaluation", "Gold_QA_Dataset_Final32_With_Answers.json",
    )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _agent_input(query_text: str) -> SubAgentInput:
    return SubAgentInput(
        query_text=query_text,
        execution=ExecutionContext(
            caller=CallerContext(user_id="u1", role=Role.SUPERVISOR, active_case_id=None)
        ),
    )


# ── (a) the pure pieces ────────────────────────────────────────────────


def test_pluck_reads_a_dotted_path_and_returns_the_parent_record():
    result = {"kind": "fir_section_case_count", "sections": [
        {"key": "PPC §34", "section_code": "34", "fir_count": 40},
        {"key": "PPC §302", "section_code": "302", "fir_count": 10},
    ]}
    value, parent = pluck(result, "sections.0.section_code")
    assert value == "34"
    assert parent == result["sections"][0]


@pytest.mark.parametrize("path", ["sections.0.section_code", "sections.5", "nope.0", "kind.0", "sections.x"])
def test_pluck_returns_none_none_for_anything_missing(path):
    assert pluck({"kind": "k", "sections": []}, path) == (None, None)


def test_then_step_requires_take_and_filter_field_together():
    with pytest.raises(ValueError):
        ThenStep(sub_query="q", expected_kind="k", take="sections.0.section_code")
    with pytest.raises(ValueError):
        ThenStep(sub_query="q", expected_kind="k", filter_field="section")
    with pytest.raises(ValueError):
        ThenStep(sub_query="q", expected_kind="k", take="a", filter_field="province")
    assert ThenStep(sub_query="q", expected_kind="k").take is None


def test_filters_for_fills_exactly_one_field():
    assert filters_for("section", "34") == AggregateFilters(section="34")
    district = filters_for("district", "فیصل آباد")
    assert district.districts == ("فیصل آباد",)
    assert district.district == "Faisalabad"
    assert filters_for("age_max", "29").age_max == 29


# ── (b) the runner ─────────────────────────────────────────────────────

_FIRST = {"kind": "fir_section_case_count", "charged_fir_count": 73, "section_entry_count": 218,
          "distinct_section_count": 2, "focus_section_code": None, "focus": None,
          "sections": [{"key": "PPC §34", "act": "PPC", "section_code": "34", "fir_count": 40},
                       {"key": "PPC §302", "act": "PPC", "section_code": "302", "fir_count": 10}]}
_SECOND = {"kind": "district_breakdown", "entity_label": None,
           "counts": [{"district": "فیصل آباد", "count": 12}, {"district": "لاہور", "count": 10}]}
_ALLOW = {f"CASE-{i:03d}" for i in range(1, 41)}


def _stub_aggregates(monkeypatch, *, first=_FIRST, second=_SECOND, allow=_ALLOW, calls=None,
                     raise_on=None):
    """Stub `run_aggregate` and `resolve_filter_case_ids` at `xagg`, which is
    where the runner imports them from (lazily, inside the function)."""
    import src.pipeline.xagg as xagg

    log = calls if calls is not None else []

    async def _run(query_text, target_entity, gateway, user_id=None, user_role="investigator",
                   jurisdiction_case_ids=None):
        log.append({"query": query_text, "jurisdiction_case_ids": jurisdiction_case_ids,
                    "user_role": user_role})
        if raise_on is not None and raise_on[0] == len(log):
            raise raise_on[1]
        return first if len(log) == 1 else second

    async def _resolve(filters, *, include_age=True):
        log.append({"filters": filters})
        return allow, {"applied": filters.to_log()}

    monkeypatch.setattr(xagg, "run_aggregate", _run)
    monkeypatch.setattr(xagg, "resolve_filter_case_ids", _resolve)
    return log


_THEN = ThenStep(
    sub_query="How many cases are registered in each district, across all cases?",
    expected_kind="district_breakdown",
    take="sections.0.section_code",
    filter_field="section",
    lead="{key} is the section cited by the most FIRs: {fir_count} of the {charged_fir_count} FIR(s).",
)


@pytest.mark.asyncio
async def test_chain_feeds_the_first_results_top_row_into_the_second_as_an_allow_list(monkeypatch):
    """THE MODULE'S CENTRAL PIN: step two runs narrowed to exactly the
    cases the value read from step one selects, through the
    `jurisdiction_case_ids` parameter every aggregate already accepts."""
    calls = _stub_aggregates(monkeypatch)
    outcome = await run_aggregate_chain(
        "How many FIRs cite each section, across all cases?", "fir_section_case_count", _THEN,
        gateway=object(), user_id="u1", user_role="supervisor", label="t",
    )
    assert outcome.status == "ok"
    assert outcome.value == "34"
    assert outcome.narrowed_to == 40
    assert outcome.first_kind == "fir_section_case_count"
    assert outcome.then_kind == "district_breakdown"
    assert [c.get("query") for c in calls if "query" in c] == [
        "How many FIRs cite each section, across all cases?",
        "How many cases are registered in each district, across all cases?",
    ]
    assert calls[0]["jurisdiction_case_ids"] is None
    assert calls[1]["filters"] == AggregateFilters(section="34")
    assert calls[2]["jurisdiction_case_ids"] == sorted(_ALLOW)
    # Both halves rendered, the lead between them, the narrowing stated.
    assert "PPC §34: 40 FIR(s)" in outcome.text
    assert "PPC §34 is the section cited by the most FIRs: 40 of the 73 FIR(s)." in outcome.text
    assert "Narrowed to the 40 case(s) under section 34" in outcome.text
    assert "- فیصل آباد: 12 case(s)" in outcome.text
    # The role passed through to BOTH aggregates — the gate inside
    # `run_aggregate()` is the only gate.
    assert {c["user_role"] for c in calls if "user_role" in c} == {"supervisor"}


@pytest.mark.asyncio
async def test_chain_without_take_runs_the_second_aggregate_unnarrowed(monkeypatch):
    """KB9's shape: two figures, no data dependency."""
    calls = _stub_aggregates(monkeypatch)
    then = ThenStep(sub_query="second q", expected_kind="district_breakdown")
    outcome = await run_aggregate_chain(
        "first q", "fir_section_case_count", then, gateway=object(), user_id="u1", user_role="supervisor",
    )
    assert outcome.status == "ok"
    assert outcome.narrowed_to is None
    assert outcome.value is None
    assert all("filters" not in c for c in calls)
    assert calls[1]["jurisdiction_case_ids"] is None
    assert "Narrowed to" not in outcome.text
    assert "PPC §34: 40 FIR(s)" in outcome.text and "- لاہور: 10 case(s)" in outcome.text


@pytest.mark.asyncio
async def test_chain_drops_itself_on_a_family_mismatch_on_either_side(monkeypatch):
    """`rag.py`'s defence-in-depth rule, applied to both aggregates: a
    dispatch string that drifts onto another family must never inject an
    unrelated figure."""
    _stub_aggregates(monkeypatch, first={"kind": "station_or_category_counts", "counts": []})
    outcome = await run_aggregate_chain("q1", "fir_section_case_count", _THEN, gateway=object(),
                                        user_id=None, user_role="supervisor")
    assert outcome.status == "mismatch" and outcome.text is None

    _stub_aggregates(monkeypatch, second={"kind": "total_count", "total_cases": 40})
    outcome = await run_aggregate_chain("q1", "fir_section_case_count", _THEN, gateway=object(),
                                        user_id=None, user_role="supervisor")
    assert outcome.status == "mismatch" and outcome.text is None


@pytest.mark.asyncio
async def test_chain_serves_the_first_half_alone_when_take_finds_nothing(monkeypatch):
    """An empty first result is a finding, not an error — and the second
    aggregate is NOT run over an unnarrowed corpus in its place."""
    empty = {**_FIRST, "sections": [], "charged_fir_count": 0, "distinct_section_count": 0}
    calls = _stub_aggregates(monkeypatch, first=empty)
    outcome = await run_aggregate_chain("q1", "fir_section_case_count", _THEN, gateway=object(),
                                        user_id=None, user_role="supervisor")
    assert outcome.status == "ok"
    assert outcome.then_kind is None
    assert len([c for c in calls if "query" in c]) == 1
    assert "was not run" in outcome.text


@pytest.mark.asyncio
async def test_chain_never_raises(monkeypatch):
    _stub_aggregates(monkeypatch, raise_on=(1, PermissionError("no")))
    outcome = await run_aggregate_chain("q1", "fir_section_case_count", _THEN, gateway=object(),
                                        user_id=None, user_role="investigator")
    assert outcome.status == "denied"

    _stub_aggregates(monkeypatch, raise_on=(3, RuntimeError("graph down")))
    outcome = await run_aggregate_chain("q1", "fir_section_case_count", _THEN, gateway=object(),
                                        user_id=None, user_role="supervisor")
    assert outcome.status == "failed" and "graph down" in (outcome.error or "")


# ── (c) Meta-Analysis ──────────────────────────────────────────────────


@pytest.mark.parametrize("query, plan_name", [
    (LIVE, "most_cited_section_by_district"),
    (P1_EN, "most_cited_section_by_district"),
    (P2_RU, "most_cited_section_by_district"),
    ("سب سے زیادہ لگائی جانے والی دفعہ کون سی ہے اور اس کے مقدمات کن اضلاع میں ہیں؟",
     "most_cited_section_by_district"),
    (P3_SWAP, "busiest_district_by_section"),
], ids=["live", "P1_EN", "P2_RU", "urdu", "P3_SWAP"])
def test_the_live_example_and_its_paraphrases_match_a_chained_plan(query, plan_name):
    plan = ma_mod._match_decomposition_plan(query)
    assert plan is not None and plan.name == plan_name
    assert plan.sub_queries == ()
    assert len(plan.chained) == 1


@pytest.mark.parametrize("query", [
    "Which district recovers the most weapons?",          # CP1's shape: one call today
    "What is the most common section?",                    # no Y noun
    "How many cases are registered in each district?",     # no top cue
    "How many cases were registered in Lahore district?",  # Module 148's shape
    "Which police station has the most cases?",
])
def test_one_call_questions_do_not_match_a_chained_plan(query):
    assert ma_mod._match_decomposition_plan(query) is None


def test_the_two_chained_plans_match_none_of_the_32_gold_questions():
    """The all-32 negative control, restated for the two new plans: the
    set of gold questions that match ANY plan is still exactly CR3/G1/G6
    (G1 scores 1.00 and G6 has just recovered — neither may move)."""
    chained_names = {p.name for p in ma_mod._DECOMPOSITION_PLANS if p.chained}
    assert chained_names == {"most_cited_section_by_district", "busiest_district_by_section"}
    matched = {}
    for item in _gold32():
        plan = ma_mod._match_decomposition_plan(item["question"])
        if plan is not None:
            matched[item["id"]] = plan.name
    assert matched == {"CR3": "record_consistency", "G1": "caseload_review", "G6": "orientation_note"}
    assert not (set(matched.values()) & chained_names)


@pytest.mark.asyncio
@pytest.mark.parametrize("plan_name", ["record_consistency", "caseload_review", "orientation_note"])
async def test_every_pre_existing_plan_decomposes_exactly_as_before(monkeypatch, plan_name):
    """The single-step plans G1/G6/CR3 depend on are BYTE-IDENTICAL in what
    they dispatch: `chained` defaults to empty, so `_decompose()` yields
    exactly the string tuple it always did, in order, with nothing appended."""
    async def _forbidden(**kwargs):  # pragma: no cover
        raise AssertionError("the LLM decomposer must not run for a planned question")

    monkeypatch.setattr(ma_mod, "call_llm_json", _forbidden)
    plan = next(p for p in ma_mod._DECOMPOSITION_PLANS if p.name == plan_name)
    assert plan.chained == ()
    question = {"record_consistency": "were the two processed and recorded the same way?",
                "caseload_review": "review the caseload and flag anything unusual",
                "orientation_note": "write an orientation note for a newly posted officer"}[plan_name]
    result = await ma_mod._decompose(question)
    assert result.plan_name == plan_name
    assert result.sub_queries == list(plan.sub_queries)
    assert all(isinstance(sq, str) for sq in result.sub_queries)


def test_every_chained_dispatch_string_resolves_to_the_family_the_step_names():
    """Same convention as `test_module29_every_planned_sub_query_routes_
    deterministically_to_xagg`, for the strings the chain dispatches
    directly: each must land on the family the step expects, or the chain
    would drop itself at run time with every unit test green."""
    for plan in ma_mod._DECOMPOSITION_PLANS:
        for step in plan.chained:
            first = resolve_aggregate_kind(step.first)
            assert _DISPATCH_TO_RESULT_KIND.get(first, first) == step.first_kind, (plan.name, step.first)
            then = resolve_aggregate_kind(step.then.sub_query)
            assert _DISPATCH_TO_RESULT_KIND.get(then, then) == step.then.expected_kind, (
                plan.name, step.then.sub_query,
            )


def _stub_chain(monkeypatch, outcome):
    calls = []

    async def _fake(first_query, first_kind, then, *, gateway, user_id, user_role, label=""):
        calls.append({"first": first_query, "then": then.sub_query, "user_role": user_role})
        return outcome

    monkeypatch.setattr(ma_mod, "run_aggregate_chain", _fake)
    return calls


def _forbid_supervisor(monkeypatch, calls):
    async def _fake(self, agent_input, *, on_event=None, gateway=None, allow_meta_analysis=True):
        calls.append(agent_input.query_text)
        return SubAgentResult(status=SubAgentStatus.OK, answer_text="stub", tools_used=["XAGG"])

    monkeypatch.setattr(Supervisor, "handle", _fake)


@pytest.mark.asyncio
async def test_a_chained_step_is_dispatched_directly_and_a_string_step_through_the_supervisor(monkeypatch):
    supervisor_calls: list[str] = []
    _forbid_supervisor(monkeypatch, supervisor_calls)
    chain_calls = _stub_chain(monkeypatch, chain_mod.ChainedAggregateOutcome(
        status="ok", text="PPC §34 ... by district ...", first_kind="fir_section_case_count",
        then_kind="district_breakdown", value="34", narrowed_to=40, seconds=0.3,
    ))
    step = next(p for p in ma_mod._DECOMPOSITION_PLANS if p.name == "most_cited_section_by_district").chained[0]

    outcome = await ma_mod._dispatch_one(step, _agent_input(LIVE), None, object())
    assert outcome.result is not None and outcome.result.status == SubAgentStatus.OK
    assert outcome.result.answer_text == "PPC §34 ... by district ..."
    assert outcome.result.tools_used == ["XAGG"]
    assert outcome.sub_query == step.label
    assert supervisor_calls == []
    assert chain_calls == [{"first": step.first, "then": step.then.sub_query, "user_role": "supervisor"}]

    outcome = await ma_mod._dispatch_one("a plain sub-query", _agent_input(LIVE), None, object())
    assert supervisor_calls == ["a plain sub-query"]
    assert len(chain_calls) == 1


@pytest.mark.asyncio
async def test_a_chained_step_degrades_like_a_string_step(monkeypatch):
    step = next(p for p in ma_mod._DECOMPOSITION_PLANS if p.name == "most_cited_section_by_district").chained[0]
    _stub_chain(monkeypatch, chain_mod.ChainedAggregateOutcome(status="denied", error="no"))
    outcome = await ma_mod._dispatch_one(step, _agent_input(LIVE), None, object())
    assert outcome.result is not None and outcome.result.status == SubAgentStatus.DENIED

    _stub_chain(monkeypatch, chain_mod.ChainedAggregateOutcome(status="mismatch"))
    outcome = await ma_mod._dispatch_one(step, _agent_input(LIVE), None, object())
    assert outcome.result is None and "mismatch" in (outcome.failure_reason or "")


@pytest.mark.asyncio
async def test_the_live_example_synthesises_over_the_chained_text(monkeypatch):
    """End to end through `meta_analysis()`: the chained text is the
    sub-answer the synthesis prompt sees, under the step's label, and no
    Supervisor pass and no LLM decomposer call happen."""
    supervisor_calls: list[str] = []
    _forbid_supervisor(monkeypatch, supervisor_calls)
    _stub_chain(monkeypatch, chain_mod.ChainedAggregateOutcome(
        status="ok", text="PPC §34 is the section cited by the most FIRs: 40. By district: فیصل آباد 12.",
        first_kind="fir_section_case_count", then_kind="district_breakdown", value="34",
    ))

    async def _forbidden(**kwargs):  # pragma: no cover
        raise AssertionError("no LLM decomposer call for a planned question")

    monkeypatch.setattr(ma_mod, "call_llm_json", _forbidden)
    prompts = []

    async def _call_llm(system_prompt, user_message, **kwargs):
        prompts.append(system_prompt)
        return "PPC §34 is the most-cited section (40 FIRs); by district: Faisalabad 12. [Document 1]"

    monkeypatch.setattr(ma_mod, "call_llm", _call_llm)

    async def _verify(**kwargs):
        return {"grounded": True, "off_topic": False, "reason": ""}

    monkeypatch.setattr(ma_mod, "verify_grounding", _verify)

    async def _validate(*args, **kwargs):
        return ValidationStatus.PASSED, []

    monkeypatch.setattr(ma_mod, "validate_answer", _validate)

    result = await meta_analysis(_agent_input(LIVE))
    assert result.status == SubAgentStatus.OK
    assert supervisor_calls == []
    assert len(prompts) == 1
    step = next(p for p in ma_mod._DECOMPOSITION_PLANS if p.name == "most_cited_section_by_district").chained[0]
    assert step.label in prompts[0]
    assert "40. By district: فیصل آباد 12." in prompts[0]
    assert "PPC §34" in result.answer_text
    assert result.tools_used == ["XAGG"]


# ── (d) RAG's data-half plan ───────────────────────────────────────────


def test_kb9_carries_the_heirs_aggregate_as_its_second_step_and_no_other_plan_has_one():
    plans = {p.name: p for p in rag_mod._KB_DATA_HALF_PLANS}
    kb9 = plans["death_investigation_charging"]
    assert kb9.then is not None
    assert kb9.then.expected_kind == "seized_property_disposition"
    assert kb9.then.take is None, "no data dependency between KB9's two figures"
    # Verbatim copy of entry (2)'s string, so the two cannot drift apart.
    assert kb9.then.sub_query == plans["property_register"].sub_query
    assert resolve_aggregate_kind(kb9.then.sub_query) == "seized_property_disposition"
    assert resolve_aggregate_kind(kb9.sub_query) == "fir_section_case_count"
    for name, plan in plans.items():
        if name != "death_investigation_charging":
            assert plan.then is None, name


@pytest.mark.asyncio
async def test_a_single_step_kb_plan_still_takes_the_unchanged_xagg_tool_path(monkeypatch):
    import src.pipeline.harness.tools.xagg as xagg_mod

    class _Res:
        status = rag_mod.ToolStatus.OK
        aggregate_kind = "seized_property_disposition"
        raw_summary_text = "45 entries"

    calls = []

    async def _xagg_tool(tool_input):
        calls.append(tool_input.query_text)
        return _Res()

    async def _forbidden(*a, **kw):  # pragma: no cover
        raise AssertionError("the chained runner must not run for a single-step plan")

    monkeypatch.setattr(xagg_mod, "xagg_tool", _xagg_tool)
    monkeypatch.setattr(rag_mod, "run_aggregate_chain", _forbidden)
    plan = next(p for p in rag_mod._KB_DATA_HALF_PLANS if p.name == "property_register")
    chunk = await rag_mod._run_kb_data_half(plan, _agent_input("x").execution)
    assert calls == [plan.sub_query]
    assert chunk == {
        "id": "kb-data-half:property_register", "text": "45 entries",
        "metadata": {"source": "our own case records (cross-case aggregate)",
                     "source_tool": "XAGG", "is_global": True},
    }


@pytest.mark.asyncio
async def test_kb9s_two_step_plan_returns_one_chunk_of_the_same_shape(monkeypatch):
    import src.pipeline.harness.tools.xagg as xagg_mod

    async def _forbidden_tool(tool_input):  # pragma: no cover
        raise AssertionError("a two-step plan must not take the single-aggregate path")

    monkeypatch.setattr(xagg_mod, "xagg_tool", _forbidden_tool)
    calls = []

    async def _chain(first_query, first_kind, then, *, gateway, user_id, user_role, label=""):
        calls.append((first_query, first_kind, then.sub_query, then.expected_kind, user_role))
        return chain_mod.ChainedAggregateOutcome(
            status="ok", text="10 of the 73 FIR(s) ... cite PPC §302.\n\n45 entries ... 7 held for heirs",
            first_kind=first_kind, then_kind=then.expected_kind, seconds=0.4,
        )

    monkeypatch.setattr(rag_mod, "run_aggregate_chain", _chain)

    async def _gateway():
        return object()

    import src.data_gateway as dg
    monkeypatch.setattr(dg, "get_gateway", _gateway)

    plan = next(p for p in rag_mod._KB_DATA_HALF_PLANS if p.name == "death_investigation_charging")
    chunk = await rag_mod._run_kb_data_half(plan, _agent_input("x").execution)
    assert calls == [(plan.sub_query, "fir_section_case_count", plan.then.sub_query,
                      "seized_property_disposition", "supervisor")]
    assert chunk["id"] == "kb-data-half:death_investigation_charging"
    assert chunk["metadata"]["source_tool"] == "XAGG"
    assert "PPC §302" in chunk["text"] and "heirs" in chunk["text"]

    # And a non-ok chain degrades to "no data half", exactly as before.
    async def _bad(*a, **kw):
        return chain_mod.ChainedAggregateOutcome(status="mismatch")

    monkeypatch.setattr(rag_mod, "run_aggregate_chain", _bad)
    assert await rag_mod._run_kb_data_half(plan, _agent_input("x").execution) is None
