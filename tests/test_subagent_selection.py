# -*- coding: utf-8 -*-
"""
[Gold-QA fix — Modules 158 / 164] Semantic sub-agent selection — the
plan-match clause and the cross-encoder fallback under
`supervisor.classify_to_subagent()`'s trigger list.

Checked in both directions (MODULE158_RESULT.md §3). Every semantic decision
here is planted with `subagent_selection.seed_for_tests()` at its MEASURED
score from `docs/gold-qa-wave2-results/module158_probe.json`; no test
reaches the model server. `_MEASURED` carries the scores verbatim so the
pins stay readable without opening the JSON.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from src.pipeline import semantic_dispatch as sd
from src.pipeline.harness import subagent_selection as ss
from src.pipeline.harness import supervisor as sup
from src.pipeline.harness.agents import meta_analysis as ma
from src.pipeline.harness.supervisor import (
    CROSS_CASE_LINKAGE,
    LARGE_SCALE_AGGREGATE,
    META_ANALYSIS,
    SEMANTIC_SEARCH,
    classify_to_subagent,
    semantic_selection_applies,
)

_ROOT = Path(__file__).resolve().parent.parent
_GOLD32_PATH = _ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
_ROUTE_DICTS = _ROOT / "docs" / "gold-qa-wave2-results" / "module145_live" / "module145_route_dict_after.json"
_PROBE_PATH = _ROOT / "docs" / "gold-qa-wave2-results" / "module158_probe.json"
_TARGETS = _ROOT / "docs" / "gold-qa-wave2-results" / "module158_targets.json"

# Module 79's live example (row 158) and Module 116's six paraphrases (row
# 164), text verbatim from module158_targets.json.
_Q1 = "How is the most frequently cited offence pattern distributed across districts?"
_Q1_R1 = "Take whichever offence shows up in the largest number of FIRs — how do the cases charged under it spread out from one district to the next?"
_M116_CR3_UR = "آن لائن بینکنگ فراڈ کے دو متاثرین کے معاملات کیا کاغذی کارروائی میں یکساں طور پر نمٹائے گئے؟"
_M116_G1_EN = "Put your crime-analyst hat on and tell me what in the pile of cases we have right now looks off or deserves a closer eye."
_M116_G6_EN = "Write a brief starter note for an officer who has just been posted here — what should they expect from the caseload as it stands?"

_XNETWORK = {"route": "XNETWORK", "case_scope": "cross_case", "output_format": "chat"}
_XAGG = {"route": "XAGG", "case_scope": "cross_case", "output_format": "chat"}
_RAG_WITHIN = {"route": "RAG", "case_scope": "within_case", "output_format": "chat"}


def _measured() -> dict[str, sd.SemanticMatch]:
    """Measured decisions, keyed by text, from the committed probe output."""
    probe = json.loads(_PROBE_PATH.read_text(encoding="utf-8"))
    return {
        " ".join(it["text"].split()): sd.SemanticMatch(
            it["best"], it["score"], it["runner_up"], it["runner_up_score"]
        )
        for it in probe["items"]
    }


@pytest.fixture(autouse=True)
def _isolated_cache():
    ss.reset_for_tests()
    yield
    ss.reset_for_tests()


def _gold32() -> list[dict]:
    return json.loads(_GOLD32_PATH.read_text(encoding="utf-8"))


def _route_dicts() -> dict[str, dict]:
    return json.loads(_ROUTE_DICTS.read_text(encoding="utf-8"))


# ── 1. The table cannot drift behind the plans ─────────────────────────────

def test_every_decomposition_plan_has_a_description_and_nothing_else_is_dispatchable():
    plans = {p.name for p in ma._DECOMPOSITION_PLANS}
    assert set(ss.PLAN_DESCRIPTIONS) == plans
    assert ss.DISPATCHABLE_KINDS == plans


def test_module_145s_whole_table_is_absorbed_here():
    for kind in sd.CAPABILITY_DESCRIPTIONS:
        assert f"xagg:{kind}" in ss.CAPABILITY_DESCRIPTIONS
        assert f"xagg:{kind}" not in ss.DISPATCHABLE_KINDS


def test_descriptions_are_sentences_not_trigger_words():
    for kind, text in {**ss.PLAN_DESCRIPTIONS, **ss.NEIGHBOUR_DESCRIPTIONS}.items():
        assert len(text.split()) >= 6, (kind, text)
        assert text == text.strip() and text[0].islower(), (kind, text)


def test_plan_descriptions_do_not_reuse_the_trigger_lists_load_bearing_phrases():
    """The anti-goal: a description that restated the trigger vocabulary
    would be a fourth phrase list. Checked against the distinctive English
    trigger phrases; ordinary words ('officer', 'district') are allowed."""
    banned = ("flag anything", "worth monitoring", "orientation note", "acting as a",
              "review our caseload", "same way", "processed and recorded", "newly posted",
              "worth a second look", "look over everything", "compared to", "most frequently cited")
    for kind, text in ss.PLAN_DESCRIPTIONS.items():
        for phrase in banned:
            assert phrase not in text.lower(), (kind, phrase)


def test_threshold_is_pinned_at_the_measured_value():
    assert ss.SUBAGENT_SELECTION_THRESHOLD == 0.40
    assert ss.gate.threshold == ss.SUBAGENT_SELECTION_THRESHOLD


# ── 2. Row 158: a matched plan selects Meta-Analysis on its own ─────────────

def test_q1_matches_a_plan_and_selects_meta_analysis_without_a_trigger_or_a_score():
    """Before this module Q1 vetoed the XAGG one-call skip and then fell
    through to Large-Scale Aggregate (MODULE79_RESULT.md §4.1)."""
    assert ma._match_decomposition_plan(_Q1).name == "most_cited_section_by_district"
    assert not any(p.search(_Q1) for p in sup._META_ANALYSIS_TRIGGER_PATTERNS)
    assert classify_to_subagent(_XAGG, _Q1) == META_ANALYSIS


@pytest.mark.parametrize("text", json.loads(Path(_ROOT / "docs" / "gold-qa-wave2-results" / "module79_paraphrases.json").read_text(encoding="utf-8")).values())
def test_module_79s_four_pre_written_paraphrases_select_meta_analysis(text):
    assert ma._match_decomposition_plan(text) is not None
    assert classify_to_subagent(_XAGG, text) == META_ANALYSIS


def test_plan_clause_is_logged_as_a_subagent_select_line(caplog):
    with caplog.at_level(logging.INFO, logger="src.pipeline.harness.supervisor"):
        classify_to_subagent(_XAGG, _Q1)
    assert any("SUBAGENT-SELECT" in r.getMessage() and "plan=most_cited_section_by_district" in r.getMessage()
               for r in caplog.records)


def test_plan_clause_respects_the_recursion_guard():
    """A nested sub-query never re-enters Meta-Analysis — same guard as the
    trigger clause, so `meta_analysis.py`'s recursion argument is unchanged."""
    assert classify_to_subagent(_XAGG, _Q1, allow_meta_analysis=False) == LARGE_SCALE_AGGREGATE


def test_plan_clause_still_passes_through_the_case_scope_demotion_guard():
    within = {**_XAGG, "case_scope": "within_case"}
    assert classify_to_subagent(within, _Q1) == SEMANTIC_SEARCH


# ── 3. Row 164: the semantic layer ─────────────────────────────────────────

def test_unprepared_question_falls_through_exactly_as_before():
    """No decision cached (unit tests, offline callers, a dead tunnel):
    the phrase result stands. This is what makes the layer additive."""
    assert classify_to_subagent(_XNETWORK, _M116_G6_EN) == CROSS_CASE_LINKAGE
    assert classify_to_subagent(_XNETWORK, _M116_G1_EN) == CROSS_CASE_LINKAGE
    assert classify_to_subagent(_XNETWORK, _M116_CR3_UR) == CROSS_CASE_LINKAGE


@pytest.mark.parametrize("text, plan", [
    (_M116_G6_EN, "orientation_note"),
    (_M116_G1_EN, "caseload_review"),
    (_M116_CR3_UR, "record_consistency"),
])
def test_module_116s_three_refused_paraphrases_select_meta_analysis_once_prepared(text, plan):
    """The measured decisions (module158_probe.json), replayed."""
    m = _measured()[" ".join(text.split())]
    assert m.kind == plan and m.score >= ss.SUBAGENT_SELECTION_THRESHOLD, m
    ss.seed_for_tests(text, m)
    assert classify_to_subagent(_XNETWORK, text) == META_ANALYSIS
    assert classify_to_subagent(_XAGG, text) == META_ANALYSIS


def test_q1s_english_rewording_is_claimed_by_the_xagg_one_call_skip_above_this_layer():
    """Written before running (§6): predicted to reach Meta-Analysis through
    the semantic layer. Measured: it scores 0.999 on the right plan, but its
    phrase aggregate kind is `top_districts_by`, so on the XAGG route the
    one-call skip claims it first and the layer is never consulted (filed,
    §8). On any other cross-case route the layer selects the plan."""
    from src.pipeline.xagg import resolve_aggregate_kind
    assert resolve_aggregate_kind(_Q1_R1) == "top_districts_by"
    m = _measured()[" ".join(_Q1_R1.split())]
    assert m.kind == "most_cited_section_by_district" and m.score >= 0.99
    ss.seed_for_tests(_Q1_R1, m)
    assert not semantic_selection_applies(_XAGG, _Q1_R1)
    assert classify_to_subagent(_XAGG, _Q1_R1) == LARGE_SCALE_AGGREGATE
    assert classify_to_subagent(_XNETWORK, _Q1_R1) == META_ANALYSIS


def test_semantic_clause_is_logged_with_score_and_runner_up(caplog):
    m = _measured()[" ".join(_M116_G6_EN.split())]
    ss.seed_for_tests(_M116_G6_EN, m)
    with caplog.at_level(logging.INFO, logger="src.pipeline.harness.supervisor"):
        classify_to_subagent(_XNETWORK, _M116_G6_EN)
    line = next(r.getMessage() for r in caplog.records if "SUBAGENT-SELECT" in r.getMessage())
    assert "via semantic=orientation_note(" in line and "runner_up=" in line


def test_a_planted_score_below_threshold_does_not_select():
    ss.seed_for_tests(_M116_G6_EN, sd.SemanticMatch("orientation_note", 0.39, "xagg:total_count", 0.1))
    assert classify_to_subagent(_XNETWORK, _M116_G6_EN) == CROSS_CASE_LINKAGE


def test_a_planted_absorbing_best_does_not_select_however_high():
    ss.seed_for_tests(_M116_G6_EN, sd.SemanticMatch("xagg:total_count", 0.999, "orientation_note", 0.998))
    assert classify_to_subagent(_XNETWORK, _M116_G6_EN) == CROSS_CASE_LINKAGE


def test_semantic_clause_respects_the_recursion_guard_and_the_demotion_guard():
    ss.seed_for_tests(_M116_G6_EN, _measured()[" ".join(_M116_G6_EN.split())])
    assert classify_to_subagent(_XNETWORK, _M116_G6_EN, allow_meta_analysis=False) == CROSS_CASE_LINKAGE
    assert classify_to_subagent(_RAG_WITHIN, _M116_G6_EN) == SEMANTIC_SEARCH


# ── 4. When the layer is consulted at all ──────────────────────────────────

def test_semantic_selection_applies_only_where_meta_analysis_is_reachable_and_undecided():
    assert semantic_selection_applies(_XNETWORK, _M116_G6_EN)
    assert not semantic_selection_applies(_XNETWORK, _M116_G6_EN, allow_meta_analysis=False)
    assert not semantic_selection_applies(_RAG_WITHIN, _M116_G6_EN)
    assert not semantic_selection_applies({**_XNETWORK, "route": "DIRECT"}, _M116_G6_EN)
    assert not semantic_selection_applies({**_XNETWORK, "output_format": "file_pdf"}, _M116_G6_EN)
    assert not semantic_selection_applies(_XNETWORK, "")
    # decided by the trigger list / a plan / the XAGG one-call skip
    assert not semantic_selection_applies(_XNETWORK, "Acting as a crime analyst, review our current caseload and flag anything unusual.")
    assert not semantic_selection_applies(_XAGG, _Q1)
    assert not semantic_selection_applies(_XAGG, "What is the ratio of male to female among the named accused?")


@pytest.mark.asyncio
async def test_handle_prepares_only_when_selection_applies(monkeypatch):
    """`Supervisor.handle()` pays for the scorer only for questions the
    deterministic chain left undecided."""
    from src.pipeline.harness.types import (
        CallerContext, ExecutionContext, Role, SubAgentInput, SubAgentResult, SubAgentStatus,
    )

    calls: list[str] = []

    async def fake_prepare(text):
        calls.append(text)
        return None

    async def fake_route(text):
        return dict(_XNETWORK)

    async def fake_agent(agent_input, *, on_event=None, gateway=None):
        return SubAgentResult(status=SubAgentStatus.OK, answer_text="ok")

    monkeypatch.setattr(sup.subagent_selection, "prepare", fake_prepare)
    monkeypatch.setattr(sup, "route_query", fake_route)
    fake_agent.name = CROSS_CASE_LINKAGE
    s = sup.Supervisor(registry={CROSS_CASE_LINKAGE: fake_agent, META_ANALYSIS: fake_agent})

    def inp(text):
        caller = CallerContext(user_id="u1", role=Role.PLATFORM_ADMIN, active_case_id=None)
        return SubAgentInput(query_text=text, execution=ExecutionContext(caller=caller))

    await s.handle(inp(_M116_G6_EN))
    assert calls == [_M116_G6_EN]
    calls.clear()
    await s.handle(inp("Acting as a crime analyst, review our current caseload and flag anything unusual."))
    assert calls == []
    await s.handle(inp(_M116_G6_EN), allow_meta_analysis=False)
    assert calls == []


@pytest.mark.asyncio
async def test_prepare_never_raises_and_disables_itself_on_a_dead_scorer(monkeypatch):
    async def dead(text):
        raise RuntimeError("tunnel down")

    monkeypatch.setattr(ss.gate, "score", dead)
    monkeypatch.setattr(ss.config, "SEMANTIC_DISPATCH_ENABLED", True)
    assert await ss.prepare(_M116_G6_EN) is None
    assert ss.lookup(_M116_G6_EN) is None
    assert ss.gate._disabled_until > 0
    # a plan-matched question is unaffected by the dead scorer
    assert classify_to_subagent(_XAGG, _Q1) == META_ANALYSIS


# ── 5. The fast-path guarantee: all 32 gold questions ──────────────────────

def _gold_selection() -> dict[str, str]:
    rd = _route_dicts()
    return {g["id"]: classify_to_subagent(rd[g["id"]], g["question"]) for g in _gold32()}


_EXPECTED_GOLD = {
    "CR3": META_ANALYSIS, "G1": META_ANALYSIS, "G6": META_ANALYSIS,
    **{k: LARGE_SCALE_AGGREGATE for k in (
        "D1", "S2", "S3", "A1", "A7", "CP6", "CR2", "CR4", "CR6", "CR7", "CR8", "CS4",
        "CP1", "M1", "M2", "M4", "M5", "M7", "G2", "G3", "G5")},
    **{k: SEMANTIC_SEARCH for k in ("KB1", "KB2", "KB3", "KB4", "KB5", "KB6", "KB8", "KB9")},
}


def test_all_32_gold_questions_select_the_same_subagent_as_before():
    assert _gold_selection() == _EXPECTED_GOLD


def test_cr3_g1_g6_still_select_meta_analysis_through_the_trigger_list():
    rd = _route_dicts()
    for gid in ("CR3", "G1", "G6"):
        q = next(g["question"] for g in _gold32() if g["id"] == gid)
        assert sup._meta_analysis_decided_by_phrase(rd[gid]["route"], q) == "trigger"
        assert not semantic_selection_applies(rd[gid], q)


def test_only_four_gold_questions_can_reach_the_semantic_layer_and_all_four_are_absorbed():
    """D1, S2, S3 and CR2 are the gold questions whose aggregate kind is a
    generic or bare-recurrence tier, so the XAGG one-call skip does not
    claim them; their measured best description is an absorbing class."""
    rd = _route_dicts()
    reach = {g["id"] for g in _gold32() if semantic_selection_applies(rd[g["id"]], g["question"])}
    assert reach == {"D1", "S2", "S3", "CR2"}
    measured = _measured()
    for g in _gold32():
        if g["id"] in reach:
            m = measured[" ".join(g["question"].split())]
            assert m.kind not in ss.DISPATCHABLE_KINDS, (g["id"], m)


def test_all_32_gold_questions_force_armed_with_their_measured_decision_do_not_move():
    measured = _measured()
    before = _gold_selection()
    for g in _gold32():
        ss.seed_for_tests(g["question"], measured[" ".join(g["question"].split())])
    assert _gold_selection() == before


def test_the_29_non_meta_analysis_gold_hazards_cannot_be_decomposed():
    """The hazard pin the brief asks for, in the shape the measurement has
    (MODULE158_RESULT.md §6): for every gold question that is NOT a
    Meta-Analysis question, its measured best description is either an
    absorbing class or below threshold — so even with the layer force-armed
    it selects exactly what it selected before. The 21 cross-case ones are
    checked on their scores; the 8 within-case KB questions never reach the
    layer at all."""
    probe = json.loads(_PROBE_PATH.read_text(encoding="utf-8"))
    gold_items = {it["gold_id"]: it for it in probe["items"] if it["set"] == "gold"}
    hazards = [gid for gid in _EXPECTED_GOLD if gid not in ("CR3", "G1", "G6")]
    assert len(hazards) == 29
    rd = _route_dicts()
    for gid in hazards:
        it = gold_items[gid]
        fires = it["best"] in ss.DISPATCHABLE_KINDS and it["score"] >= ss.SUBAGENT_SELECTION_THRESHOLD
        assert not fires, (gid, it["best"], it["score"])
        if gid.startswith("KB"):
            q = next(g["question"] for g in _gold32() if g["id"] == gid)
            assert not semantic_selection_applies(rd[gid], q)


def test_m4_is_the_thinnest_absorbing_margin_and_is_pinned():
    """M4's Urdu gold text ("on one side ... on the other side") scores
    0.951 on the most-cited-section plan and 0.998 on its own aggregate's
    absorbing description. Recorded so a description edit that flips it is
    caught here, not live. M4 is also claimed by the XAGG one-call skip
    and never reaches the layer."""
    probe = json.loads(_PROBE_PATH.read_text(encoding="utf-8"))
    m4 = next(it for it in probe["items"] if it["id"] == "M4::gold")
    assert m4["best"] == "xagg:statute_court_stage_join"
    assert m4["best_plan"] == "most_cited_section_by_district"
    assert m4["score"] > m4["best_plan_score"] >= 0.9
    assert m4["decided"] == "xagg-one-call"


def test_the_probe_records_the_measurement_the_threshold_was_chosen_from():
    probe = json.loads(_PROBE_PATH.read_text(encoding="utf-8"))
    assert probe["threshold"] == ss.SUBAGENT_SELECTION_THRESHOLD
    assert probe["gold_moved"] == 0
    assert probe["highest_hazard"] < ss.SUBAGENT_SELECTION_THRESHOLD <= probe["lowest_fired_tp"]
    assert probe["highest_hazard"] == pytest.approx(0.162, abs=0.001)
    assert probe["lowest_fired_tp"] == pytest.approx(0.648, abs=0.001)
    # the table measured is the table shipped
    import hashlib
    blob = json.dumps(ss.CAPABILITY_DESCRIPTIONS, sort_keys=True, ensure_ascii=False)
    assert probe["table_hash"] == hashlib.sha1(blob.encode("utf-8")).hexdigest()[:10]
