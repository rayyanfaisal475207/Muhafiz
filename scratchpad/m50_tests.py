

# ═══════════════════════════════════════════════════════════════════════
# (l) [Gold-QA fix — Module 50, questions G1 / G6 / CR3] The six
#     aggregates Modules 31–36 built, WIRED INTO THEIR PLANS.
#
# The defect these pin is not a wrong answer — it is an absent call.
# Modules 31–36 each built and live-verified an aggregate, and each one
# deferred the wiring because `meta_analysis.py` was owned by another
# track. Module 31's own result file states it outright: G1's answer is
# unchanged. Six modules of correct, tested, live-verified work sat behind
# three tuples that never named them.
#
# The risk this module introduces is therefore the mirror image of Module
# 29's: not "does the plan match", but "does the plan still DISPATCH what
# it claims to". Two ways that breaks silently, both pinned below:
#   - a sub-query string drifts out of sync with the aggregate's own
#     keyword family, and lands on the wrong route (Modules 31–34 caught
#     this LIVE — `_XGRAPH_OVERRIDE_PATTERNS` steals "across all cases");
#   - a plan grows past `_MAX_PLAN_SUB_QUERIES` and is silently truncated,
#     dropping the aggregates from the tail of the tuple.
# ═══════════════════════════════════════════════════════════════════════

# The canonical strings, re-declared here from `tests/test_xagg.py`, where
# Modules 31–36 pinned them. Deliberately a SECOND copy: the test below
# asserts the two agree, so a reword in either file fails loudly instead of
# quietly detaching a plan from its aggregate.
_M50_PINNED = {
    "_SQ_ACCUSED_AGE": (
        "How many cases involve an accused person, and what is their age range "
        "and average age, across all cases?"
    ),
    "_SQ_RELATIONSHIP": (
        "How many cases record a relationship between the accused and the "
        "complainant, and which relationship is it, across all cases?"
    ),
    "_SQ_SEIZED_PROPERTY": (
        "How many cases record seized property, and what happens to it — how "
        "many items were sent to a forensic laboratory or held for a deceased's "
        "heirs, across all cases?"
    ),
    "_SQ_TIME_OF_DAY": (
        "How many cases record an incident time, and at what time of day do "
        "those incidents happen, across all cases?"
    ),
    "_SQ_ARREST_RATE": (
        "How many cases record an arrest of an accused person, and on how many "
        "is no arrest recorded, across all cases?"
    ),
    "_SQ_FIR_LISTING_CYBER": (
        "How many cases are registered under the cybercrime act at a cyber "
        "crime circle station, and what are their FIR numbers and current status?"
    ),
}


def test_module50_wired_sub_queries_are_byte_identical_to_the_pinned_strings():
    """The copy-across, asserted byte-exact in BOTH directions.

    Modules 31–36 each pinned their sub-query in `tests/test_xagg.py`
    *specifically* so this module could copy it unchanged. A paraphrase that
    reads identically to a human — "an accused" for "an accused person" — is
    enough to change which `run_aggregate()` keyword family matches, and the
    resulting failure is invisible: the sub-query still dispatches, still
    returns a fluent answer, and answers a different question.
    """
    import tests.test_xagg as xagg_tests

    xagg_side = {
        "_SQ_ACCUSED_AGE": xagg_tests._G1_SQ_ACCUSED_AGE,
        "_SQ_RELATIONSHIP": xagg_tests._G1_SQ_RELATIONSHIP,
        "_SQ_SEIZED_PROPERTY": xagg_tests._G1_SQ_SEIZED_PROPERTY,
        "_SQ_TIME_OF_DAY": xagg_tests._G1_SQ_TIME_OF_DAY,
        "_SQ_ARREST_RATE": xagg_tests._G6_SQ_ARREST_RATE,
        "_SQ_FIR_LISTING_CYBER": xagg_tests._CR3_SQ_FIR_LISTING,
    }
    for name, literal in _M50_PINNED.items():
        assert getattr(ma_mod, name) == literal, f"{name} drifted from this file's copy"
        assert getattr(ma_mod, name) == xagg_side[name], (
            f"{name} drifted from the string Modules 31-36 pinned in tests/test_xagg.py"
        )


@pytest.mark.parametrize(
    "plan_name, must_contain",
    [
        ("caseload_review", ("_SQ_ACCUSED_AGE", "_SQ_RELATIONSHIP",
                             "_SQ_SEIZED_PROPERTY", "_SQ_TIME_OF_DAY")),
        ("orientation_note", ("_SQ_ARREST_RATE",)),
        ("record_consistency", ("_SQ_FIR_LISTING_CYBER",)),
    ],
    ids=["G1-caseload_review", "G6-orientation_note", "CR3-record_consistency"],
)
def test_module50_each_aggregate_is_wired_into_the_plan_that_needs_it(plan_name, must_contain):
    """The whole point of this module, stated as an assertion per plan.

    Mapped to what each question's gold answer actually asks for:
      - G1's four headline findings are Modules 31–34's four aggregates,
        in the order gold states them;
      - G6's caseload-pace element is Module 35's arrest rate, which Module
        35 confirmed live never fired because this plan did not ask for it;
      - CR3's instability was an IDENTIFICATION gap, closed by Module 36's
        filtered FIR listing.
    """
    plan = next(p for p in ma_mod._DECOMPOSITION_PLANS if p.name == plan_name)
    for const in must_contain:
        assert getattr(ma_mod, const) in plan.sub_queries, (
            f"{plan_name} does not dispatch {const} — the aggregate behind it is unreachable"
        )


def test_module50_no_plan_exceeds_the_plan_cap_so_none_is_silently_truncated():
    """The failure mode this module was one edit away from shipping.

    `_decompose()` truncates a plan to `_MAX_PLAN_SUB_QUERIES`. Appending
    Modules 31–34's four sub-queries to `caseload_review`'s existing five
    would have produced a NINE-entry plan of which only the first five ever
    dispatched — a wiring module whose wiring is discarded with nothing
    anywhere saying so. A plan longer than the cap is a code bug, and this
    is where it is caught, not at runtime.
    """
    for plan in ma_mod._DECOMPOSITION_PLANS:
        assert len(plan.sub_queries) <= ma_mod._MAX_PLAN_SUB_QUERIES, (
            f"{plan.name} has {len(plan.sub_queries)} sub-queries and would be "
            f"truncated to {ma_mod._MAX_PLAN_SUB_QUERIES}"
        )
        assert len(set(plan.sub_queries)) == len(plan.sub_queries), (
            f"{plan.name} dispatches the same sub-query twice, wasting a scarce slot"
        )


def test_module50_the_plan_cap_decision_is_five_and_holds():
    """The `_MAX_SUB_QUERIES = 5` question the brief asked to settle, pinned
    with its measured justification so raising it is a deliberate act.

    MEASURED LIVE on this branch (port 8013, 2026-09-08), and the constraint
    is the 60 s `META_ANALYSIS_SUBQUERY_TIMEOUT`, which every sub-query in a
    fan-out shares as ONE wall-clock deadline:

      N=3 (CR3)  last sub-answer at +25.0 s / +29.2 s   0 timeouts, 2/2 runs
      N=5 (G1)   last sub-answer at +56.7 s / +53.5 s   0 timeouts, 2/2 runs
      N=6 (G6)   last sub-answer at +41.1/+57.7/+58.1s  1 TIMEOUT in 3 runs,
                                                        and that run's
                                                        synthesis came back
                                                        NOT grounded
      N=9 (G1)   5 answered by +55.4 s                  4 TIMEOUTS of 9

    The sub-queries do not overlap — the shared model server serialises
    them into a ~6-10 s staircase — so N is bounded by 60 s / step, and at
    N=9 the four that died included the age and time-of-day aggregates this
    module exists to wire in. Raising the cap does not buy coverage; it buys
    the silent loss of whichever aggregate is served last.
    """
    assert ma_mod._MAX_PLAN_SUB_QUERIES == 5
    # The LLM-decomposer cap is a SEPARATE bound on an untrusted list. It is
    # the same number today; the split exists so the two can move apart.
    assert ma_mod._MAX_SUB_QUERIES == 5


def test_module50_every_wired_sub_query_still_routes_deterministically_to_xagg():
    """Module 29's routing invariant, re-asserted over Module 50's additions.

    `test_module29_every_planned_sub_query_routes_deterministically_to_xagg`
    already covers every plan member, these six included. This test names
    them explicitly so the reason they are worded "How many cases ..."
    survives in the file that changed them. A sub-query that loses its
    deterministic override falls back to the LLM router, which Modules
    31–34 measured sending 6 of 9 naturally phrased sub-questions to
    XGRAPH/Cross-Case Linkage.
    """
    from src.pipeline import router

    for name, literal in _M50_PINNED.items():
        override = router._deterministic_route_override(literal, case_id=None)
        assert override is not None, f"{name}: no deterministic route"
        assert override["route"] == "XAGG", f"{name} -> {override['route']}"


def test_module50_wiring_changes_no_plan_boundary():
    """Module 29's negative control, restated as Module 50's own guard.

    This module re-composed all three plans but touched no `patterns`
    tuple. WHICH questions decompose must therefore be byte-identical to
    before — a re-composition that also widened the gate would be two
    changes wearing one commit message.
    """
    matched = {}
    for item in _load_gold32():
        plan = ma_mod._match_decomposition_plan(item["question"])
        if plan is not None:
            matched[item["id"]] = plan.name

    assert matched == {
        "CR3": "record_consistency",
        "G1": "caseload_review",
        "G6": "orientation_note",
    }
    assert ma_mod._match_decomposition_plan(_G1_PARAPHRASE).name == "caseload_review"


def test_module50_module41_guard_still_lets_all_three_reach_meta_analysis():
    """The Module 41 interaction the brief asked to check FIRST.

    Module 41 generalised the supervisor's skip guard so that any query
    XAGG resolves to a specific aggregate bypasses decomposition entirely.
    G1 is the load-bearing case: it DOES resolve specifically
    (`case_completeness_scan`), and only `_xagg_answers_in_one_call()`'s
    explicit veto on `_match_decomposition_plan()` keeps it decomposing. If
    a future edit removes that veto, this module's entire wiring becomes
    dead code again — silently, because G1 would still return a fluent
    single-aggregate answer.
    """
    from src.pipeline.harness import supervisor as sup

    for query in (_CR3_GOLD, _G1_GOLD, _G6_GOLD, _G1_PARAPHRASE):
        assert sup._xagg_answers_in_one_call(query) is False, query[:60]

    for query in (_CR3_GOLD, _G1_GOLD, _G6_GOLD):
        assert sup.classify_to_subagent({"route": "XNETWORK"}, query) == sup.META_ANALYSIS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, expected_n",
    [(_G1_GOLD, 5), (_G6_GOLD, 5), (_CR3_GOLD, 3)],
    ids=["G1", "G6", "CR3"],
)
async def test_module50_each_question_dispatches_its_whole_plan(monkeypatch, query, expected_n):
    """End to end at this module's boundary: every wired sub-query is
    actually DISPATCHED, and each contributes a citation.

    Module 29 pinned this for G1 alone. Extended to all three, because all
    three plans changed and because a truncation bug would show up here as
    a dispatch count short by exactly the number of aggregates this module
    added.
    """
    _forbid_llm_json(monkeypatch)
    call_log = []
    _stub_supervisor_handle(
        monkeypatch,
        by_query={},
        default=SubAgentResult(
            status=SubAgentStatus.OK, answer_text="A computed fact.", tools_used=["XAGG"]
        ),
        calls=call_log,
    )
    _stub_call_llm(monkeypatch, answer="Synthesized. [Document 1]")
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input(query_text=query))

    assert result.status == SubAgentStatus.OK
    dispatched = [c["query_text"] for c in call_log]
    plan = ma_mod._match_decomposition_plan(query)
    assert len(dispatched) == expected_n
    assert set(dispatched) == set(plan.sub_queries)
    assert len(result.citations) == expected_n
