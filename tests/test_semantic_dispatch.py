# -*- coding: utf-8 -*-
"""
[Gold-QA fix — Module 145] Semantic dispatch — the cross-encoder fallback
under `xagg.resolve_aggregate_kind()`'s phrase lists.

Checked in both directions (MODULE145_RESULT.md §3). Every test here plants
decisions with `semantic_dispatch.seed_for_tests()` and never reaches the
model server: the scores it plants are the MEASURED ones from
`docs/gold-qa-wave2-results/module145_probe.json`, so a test that seeds
"L1 scores 1.000" is replaying a measurement, not inventing one. The
`_MEASURED` table below carries them verbatim, with the reranker's actual
runner-up, so the pins stay readable without opening the JSON.
"""
from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import pytest

from src import config
from src.pipeline import semantic_dispatch as sd
from src.pipeline import xagg
import src.pipeline.router as router

_ROOT = Path(__file__).resolve().parent.parent
_GOLD32_PATH = _ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
# The NEWEST measurement of the shipped table. Module 145 wrote
# module145_probe.json against its own 38-entry table; Module 161 added one
# description and re-scored the whole 261-item corpus against the enlarged
# table (evaluation/module161_semantic_probe.py). Every test below that
# replays a measured decision reads the file that matches the live table —
# `test_the_probe_records_zero_gold_moves_and_zero_hazard_fires` pins that.
_PROBE_PATH = _ROOT / "docs" / "gold-qa-wave2-results" / "module161_probe_after.json"

# The live query Module 145 was filed from, and the two Module 100 rewordings
# the layer reaches. Text verbatim from module145_targets.json.
_L1 = "How often is the officer who registers an FIR also the officer who investigates the case?"
_M100A = "Is the officer who registers a case normally the one who investigates it?"
_M100C = "Does the same officer both register and investigate our cases?"

# Measured cross-encoder decisions (module145_probe.json), verbatim.
_MEASURED = {
    _L1: sd.SemanticMatch("officer_role_pair_overlap", 1.000, "unsupported_officer", 0.314),
    _M100A: sd.SemanticMatch("officer_role_pair_overlap", 0.998, "unsupported_officer", 0.625),
    _M100C: sd.SemanticMatch("officer_role_pair_overlap", 0.992, "unsupported_officer", 0.313),
    # Hazards — pre-written in module145_hazards.json / Module 116's corpus /
    # router.txt's own few-shot examples. These are the three HIGHEST-scoring
    # hazards among the 102 corpus items that reach the layer; the other 47
    # score <= 0.050.
    "Put your crime-analyst hat on and tell me what in the pile of cases we have right now looks off or deserves a closer eye.":
        sd.SemanticMatch("statute_mix_by_year", 0.351, "graph_recurrence_weapon", 0.150),
    "Mujhe sab se naye case ke baare mein batao":
        sd.SemanticMatch("graph_recurrence_weapon", 0.104, "unsupported_officer", 0.003),
    "Summarize the FIR for this case.":
        sd.SemanticMatch("weapon_evidence_chain", 0.061, "dv_report_fir_match", 0.047),
    # Questions whose nearest description IS an absorbing class: the grand
    # total, S2's per-station ranking, a legal-procedure lookup.
    "How many cases in total?":
        sd.SemanticMatch("total_count", 0.994, "total_accused_count", 0.240),
    "Which police station has the most cases?":
        sd.SemanticMatch("station_or_category_counts", 0.996, "top_districts_by", 0.760),
    "What is the procedure for registering an FIR?":
        sd.SemanticMatch("legal_norm_lookup", 0.626, "fir_register_completeness", 0.249),
}


@pytest.fixture(autouse=True)
def _isolated_cache():
    sd.reset_for_tests()
    yield
    sd.reset_for_tests()


def _gold32() -> list[dict]:
    assert _GOLD32_PATH.exists(), f"gold dataset missing at {_GOLD32_PATH}"
    return json.loads(_GOLD32_PATH.read_text(encoding="utf-8"))


def _probe() -> dict:
    assert _PROBE_PATH.exists(), f"Module 145 probe output missing at {_PROBE_PATH}"
    return json.loads(_PROBE_PATH.read_text(encoding="utf-8"))


# ── 1. The table cannot drift behind the chain ─────────────────────────────

def _kinds_the_chain_returns() -> set[str]:
    tree = ast.parse(Path(xagg.__file__).read_text(encoding="utf-8"))
    for func in ast.walk(tree):
        if isinstance(func, ast.FunctionDef) and func.name == "phrase_aggregate_kind":
            return {
                node.value.value
                for node in ast.walk(func)
                if isinstance(node, ast.Return)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            }
    raise AssertionError("phrase_aggregate_kind() not found")


def test_every_kind_the_chain_can_return_has_a_description():
    """[Gold-QA fix — Module 161] One carve-out, declared by name in
    `xagg._SEMANTIC_ONLY_AGGREGATE_KINDS`: a kind with NO phrase list, which
    the chain therefore never returns and only a description can reach. It
    must be described, must be dispatchable, and must not be a chain kind."""
    chain = _kinds_the_chain_returns()
    assert chain, "the AST scan found no return kinds"
    described = set(sd.CAPABILITY_DESCRIPTIONS) - {"xgraph_named_entity_network", "legal_norm_lookup"}
    semantic_only = xagg._SEMANTIC_ONLY_AGGREGATE_KINDS
    assert semantic_only <= described
    assert not (semantic_only & chain)
    assert not (semantic_only & sd.NON_DISPATCHABLE_KINDS)
    assert described - semantic_only == chain


def test_absorbing_classes_mirror_xagg_generic_and_refusal_sets():
    """Every generic catch-all and honest refusal is absorbing; the only
    other absorbing entries are the two neighbouring-route descriptions,
    and neither is a kind the chain can return."""
    xagg_absorbing = xagg._GENERIC_AGGREGATE_KINDS | xagg._UNSUPPORTED_AGGREGATE_KINDS
    assert xagg_absorbing <= sd.NON_DISPATCHABLE_KINDS
    extra = sd.NON_DISPATCHABLE_KINDS - xagg_absorbing
    assert extra == {"xgraph_named_entity_network", "legal_norm_lookup"}
    assert not (extra & _kinds_the_chain_returns())


def test_descriptions_are_sentences_not_trigger_words():
    """The anti-goal: the table describes what an aggregate ANSWERS. A
    description that is a bare keyword list would be a fourth phrase list."""
    for kind, text in sd.CAPABILITY_DESCRIPTIONS.items():
        assert len(text.split()) >= 6, (kind, text)
        assert text == text.strip() and text[0].islower(), (kind, text)


# ── 2. The threshold and the fire rule ────────────────────────────────────

def test_threshold_is_the_measured_value():
    """0.40 sits between the highest hazard (0.351) and the lowest fired true
    positive (0.462) — MODULE145_RESULT.md §6. Moving it is a re-measurement,
    not an edit."""
    assert sd.SEMANTIC_DISPATCH_THRESHOLD == 0.40


@pytest.mark.parametrize("score,expected", [(0.399, False), (0.40, True), (0.462, True)])
def test_fire_rule_is_a_hard_threshold(score, expected):
    assert sd.SemanticMatch("arrest_rate", score, "total_count", 0.0).fires is expected


@pytest.mark.parametrize("kind", sorted(sd.NON_DISPATCHABLE_KINDS))
def test_an_absorbing_class_never_fires_even_at_full_score(kind):
    assert sd.SemanticMatch(kind, 1.0, "arrest_rate", 0.9).fires is False


def test_rank_scores_picks_best_and_runner_up():
    m = sd.rank_scores({"a": 0.2, "b": 0.9, "c": 0.5})
    assert (m.kind, m.score, m.runner_up, m.runner_up_score) == ("b", 0.9, "c", 0.5)


# ── 3. The change-detectors: fail on origin/main, pass here ──────────────────

def test_the_live_query_resolves_to_its_aggregate_once_prepared():
    """Module 145's filing example. Phrase chain: generic (two of three term
    lists hit, "also" is not a sameness word). With the measured decision
    planted, the resolver returns the aggregate that was one word away."""
    assert xagg.phrase_aggregate_kind(_L1) == "station_or_category_counts"
    assert xagg.resolve_aggregate_kind(_L1) == "station_or_category_counts"  # unprepared
    sd.seed_for_tests(_L1, _MEASURED[_L1])
    assert xagg.resolve_aggregate_kind(_L1) == "officer_role_pair_overlap"


@pytest.mark.parametrize("text", [_M100A, _M100C])
def test_module_100_rewordings_reach_kb3s_aggregate(text):
    assert xagg.resolve_aggregate_kind(text) in xagg._GENERIC_AGGREGATE_KINDS
    sd.seed_for_tests(text, _MEASURED[text])
    assert xagg.resolve_aggregate_kind(text) == "officer_role_pair_overlap"


async def test_router_routes_a_semantic_hit_to_xagg_before_the_llm(monkeypatch):
    """The route is where the live question died (RAG, 3 of 3). A prepared
    hit must reach XAGG without the classifier being consulted at all."""
    async def llm_must_not_be_called(*args, **kwargs):
        raise AssertionError("the LLM classifier was consulted")

    monkeypatch.setattr(router, "call_llm_json", llm_must_not_be_called)
    sd.seed_for_tests(_L1, _MEASURED[_L1])
    result = await router.route_query(_L1)
    assert result["route"] == "XAGG"
    assert result["case_scope"] == "cross_case"
    assert "officer_role_pair_overlap" in result["reason"]
    assert "1.000" in result["reason"]


async def test_router_falls_through_to_the_llm_when_nothing_fires(monkeypatch):
    seen = {}

    async def fake_llm(system_prompt, user_message, **kwargs):
        seen["called"] = True
        return {"route": "RAG"}, '{"route": "RAG"}'

    monkeypatch.setattr(router, "call_llm_json", fake_llm)
    result = await router.route_query(_L1)  # nothing prepared
    assert seen.get("called") is True
    assert result["route"] == "RAG"


async def test_router_semantic_override_respects_the_active_case_guard():
    sd.seed_for_tests(_L1, _MEASURED[_L1])
    assert await router._semantic_xagg_override(_L1, case_id="CASE-009") is None
    assert await router._semantic_xagg_override(_L1 + " in FIR-460/26") is None
    assert (await router._semantic_xagg_override(_L1))["route"] == "XAGG"


async def test_prepare_scores_logs_and_caches(monkeypatch, caplog):
    """The log line is the only live evidence of what fired."""
    monkeypatch.setattr(config, "SEMANTIC_DISPATCH_ENABLED", True)
    calls = []

    async def fake_scores(text):
        calls.append(text)
        scores = {k: 0.001 for k in sd.CAPABILITY_DESCRIPTIONS}
        scores["officer_role_pair_overlap"] = 1.0
        scores["unsupported_officer"] = 0.314
        return scores

    monkeypatch.setattr(sd, "_score_descriptions", fake_scores)
    with caplog.at_level(logging.INFO, logger="src.pipeline.semantic_dispatch"):
        match = await xagg.prepare_semantic_dispatch(_L1)
        again = await xagg.prepare_semantic_dispatch("  " + _L1 + "  ")
    assert match is not None and match.kind == "officer_role_pair_overlap"
    assert again == match
    assert calls == [_L1], "second call must be a cache hit"
    line = next(r.getMessage() for r in caplog.records if "SEMANTIC-DISPATCH" in r.getMessage())
    assert line.startswith("SEMANTIC-DISPATCH officer_role_pair_overlap: score=1.000")
    assert "runner_up=unsupported_officer(0.314)" in line
    assert "threshold=0.40" in line


async def test_prepare_never_runs_the_scorer_when_the_phrase_chain_resolves(monkeypatch):
    monkeypatch.setattr(config, "SEMANTIC_DISPATCH_ENABLED", True)

    async def scorer_must_not_run(text):
        raise AssertionError("scored a phrase-resolved question")

    monkeypatch.setattr(sd, "_score_descriptions", scorer_must_not_run)
    # A1's gold text resolves by phrase to gender_breakdown.
    assert await xagg.prepare_semantic_dispatch("نامزد ملزمان میں مرد اور عورت کا تناسب کیا ہے؟") is None


async def test_a_dead_scorer_falls_through_and_backs_off(monkeypatch, caplog):
    monkeypatch.setattr(config, "SEMANTIC_DISPATCH_ENABLED", True)
    calls = []

    async def dead(text):
        calls.append(text)
        raise ConnectionError("tunnel is down")

    monkeypatch.setattr(sd, "_score_descriptions", dead)
    with caplog.at_level(logging.WARNING, logger="src.pipeline.semantic_dispatch"):
        assert await sd.prepare(_L1) is None
        assert await sd.prepare(_M100A) is None  # inside the cooldown: no second call
    assert calls == [_L1]
    assert any("SEMANTIC-DISPATCH unavailable" in r.getMessage() for r in caplog.records)
    assert xagg.resolve_aggregate_kind(_L1) == "station_or_category_counts"


async def test_disabled_layer_is_inert(monkeypatch):
    monkeypatch.setattr(config, "SEMANTIC_DISPATCH_ENABLED", False)

    async def scorer_must_not_run(text):
        raise AssertionError("scored while disabled")

    monkeypatch.setattr(sd, "_score_descriptions", scorer_must_not_run)
    assert await sd.prepare(_L1) is None


# ── 4. The hazards that must NOT dispatch ─────────────────────────────────

@pytest.mark.parametrize("text", [
    "Put your crime-analyst hat on and tell me what in the pile of cases we have right now looks off or deserves a closer eye.",
    "Mujhe sab se naye case ke baare mein batao",
    "Summarize the FIR for this case.",
])
def test_the_three_highest_scoring_hazards_stay_below_threshold(text):
    """The three highest hazards among the corpus items that reach the layer,
    at their measured scores. Each stays on the phrase result; each is 0.049
    or more below the threshold."""
    before = xagg.resolve_aggregate_kind(text)
    sd.seed_for_tests(text, _MEASURED[text])
    assert sd.lookup(text) is None
    assert xagg.resolve_aggregate_kind(text) == before
    assert _MEASURED[text].score <= sd.SEMANTIC_DISPATCH_THRESHOLD - 0.049


@pytest.mark.parametrize("text", [
    "How many cases in total?",
    "Which police station has the most cases?",
    "What is the procedure for registering an FIR?",
])
def test_a_generic_or_other_route_question_is_absorbed_not_dispatched(text):
    """The absorbing classes: a grand total scores 0.994 against 'how many
    cases or FIRs there are in total' and that is exactly why it stays a
    grand total; a legal-procedure question lands on the legal-KB
    description, never on the FIR-register aggregate."""
    before = xagg.resolve_aggregate_kind(text)
    assert before in xagg._GENERIC_AGGREGATE_KINDS
    sd.seed_for_tests(text, _MEASURED[text])
    assert sd.lookup(text) is None
    assert xagg.resolve_aggregate_kind(text) == before


# ── 5. The all-32 dispatch equality control ─────────────────────────────────

def test_all_32_gold_questions_never_consult_the_semantic_layer_or_do_not_move():
    """Two guards, both structural.

    (a) 25 of 32 resolve by phrase: they must not consult the cache at
        all, so a FAKE full-score match planted for each changes nothing.
    (b) 7 land on the generic tier and DO consult it: with their MEASURED
        cross-encoder decisions planted, none clears the threshold, so
        `resolve_aggregate_kind()` is byte-identical on all 32.
    """
    probe = {it["text"]: it for it in _probe()["items"] if it["set"] == "gold"}
    generic, phrase_resolved = [], []
    for g in _gold32():
        text = g["question"]
        before = xagg.phrase_aggregate_kind(text)
        if before in xagg._GENERIC_AGGREGATE_KINDS:
            m = probe[text]
            sd.seed_for_tests(text, sd.SemanticMatch(m["best"], m["score"], m["runner_up"], m["runner_up_score"]))
            generic.append(g["id"])
        else:
            sd.seed_for_tests(text, sd.SemanticMatch("arrest_rate", 1.0, "total_count", 0.0))
            phrase_resolved.append(g["id"])
        assert xagg.resolve_aggregate_kind(text) == before, g["id"]
    assert len(generic) + len(phrase_resolved) == 32
    assert sorted(generic) == ["CR3", "D1", "G6", "KB1", "KB4", "KB8", "S2"]


def test_the_probe_records_zero_gold_moves_and_zero_hazard_fires():
    """The committed measurement itself, re-asserted: at the shipped
    threshold no gold question moves and no hazard fires."""
    probe = _probe()
    assert probe["threshold"] == sd.SEMANTIC_DISPATCH_THRESHOLD
    assert probe["descriptions"] == sd.CAPABILITY_DESCRIPTIONS, "table drifted since the measurement"
    for it in probe["items"]:
        if not it["fires"]:
            continue
        assert it["set"] != "gold", it["id"]
        assert it["gold_route"] == "XAGG", it["id"]
        assert it["want"] == it["best"], it["id"]
