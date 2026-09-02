"""
Finding L regression — every prose-generating sub-agent must carry the
name-fidelity rule.

One person from one source record ("رابعہ" in the case file) came back as
رابعع, "Rabeeha", "Rabia", "رَبَعَه" and once "Raheela" across separate
answers (scenario-verify Finding L). To an investigator that reads as several
different people and breaks matching an answer against the case file.

The first attempt at fixing this put the rule in `prompts/final_response.txt`
— the LEGACY orchestrator's prompt. With the harness enabled, answers are
written by the sub-agents below instead, none of which carried it, so the fix
never reached the path actually serving traffic and Run #2 produced MORE
variants than Run #1.

This test exists so a new prose sub-agent cannot silently repeat that: the
rule is defined once in `types.NAME_FIDELITY_RULE` and asserted here on every
prompt that generates user-facing prose.
"""
from __future__ import annotations

import importlib

import pytest

from src.pipeline.harness.types import NAME_FIDELITY_RULE

# (module, prompt-template attribute) for every sub-agent that generates
# user-facing prose. Deterministic-text sub-agents (Timeline Building,
# Data-Quality) are excluded because they never paraphrase a name — they
# emit stored values verbatim.
#
# [Fix, confirmed live] Report Drafting was previously excluded here too,
# on the claim its "structured assembly... never paraphrases a name" — that
# claim was checked against the actual code and is false:
# report_drafting.py's _DRAFT_SYSTEM_PROMPT_TEMPLATE explicitly instructs
# "write a well-organized report in clear prose", a genuine call_llm()
# paraphrase over case-summary material containing names, not a verbatim
# reproduction. investigative_analysis.py and meta_analysis.py were never
# even considered for this list despite both making an equivalent genuine
# prose call_llm() call ("produce one synthesized analytical answer" /
# "combining these sub-answers... do not just concatenate them") — this
# test's own docstring says the rule must be asserted "on every prompt that
# generates user-facing prose," and these three call_llm() sites do exactly
# that; omitting them left 3 of 9 real prose call sites uncovered.
PROSE_PROMPTS = [
    ("semantic_search", "_SYSTEM_PROMPT_TEMPLATE"),
    ("local_search", "_SYSTEM_PROMPT_TEMPLATE"),
    ("case_summarization", "_SYSTEM_PROMPT_TEMPLATE"),
    ("cross_case_linkage", "_XNETWORK_SYSTEM_PROMPT_TEMPLATE"),
    ("large_scale_aggregate", "_SYSTEM_PROMPT_TEMPLATE"),
    ("global_search", "_FINAL_SYSTEM_PROMPT_TEMPLATE"),
    ("investigative_analysis", "_SYSTEM_PROMPT_TEMPLATE"),
    ("meta_analysis", "_SYNTHESIS_SYSTEM_PROMPT_TEMPLATE"),
    ("report_drafting", "_DRAFT_SYSTEM_PROMPT_TEMPLATE"),
]


@pytest.mark.parametrize("module_name, attr", PROSE_PROMPTS)
def test_prose_sub_agent_prompt_carries_name_fidelity_rule(module_name, attr):
    module = importlib.import_module(f"src.pipeline.harness.agents.{module_name}")
    template = getattr(module, attr)
    assert NAME_FIDELITY_RULE in template, (
        f"{module_name}.{attr} does not include NAME_FIDELITY_RULE — a name "
        f"re-spelled by this sub-agent will make one person look like several."
    )


def test_rule_states_the_actual_requirement():
    """Guards against the rule being weakened to something unenforceable."""
    assert "EXACTLY as it appears" in NAME_FIDELITY_RULE
    assert "transliterate" in NAME_FIDELITY_RULE
    # Romanization is allowed, but only ALONGSIDE the original.
    assert "in parentheses after the original" in NAME_FIDELITY_RULE
    # Identifiers must be character-exact too.
    assert "character-for-character" in NAME_FIDELITY_RULE


def test_legacy_orchestrator_prompt_also_carries_the_rule():
    """The legacy path still serves traffic whenever the harness is off or
    hands back, so it must not regress either."""
    import pathlib

    text = pathlib.Path("prompts/final_response.txt").read_text(encoding="utf-8")
    assert "never re-spell" in text.lower()
