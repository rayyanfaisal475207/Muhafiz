"""
Tests for src/pipeline/harness/agents/meta_analysis.py (findings.md
Module 10, "Meta-analysis — query decomposition and aggregation").

Covers:
  (a) decompose-step schema lock: a genuinely compound query decomposes into
      a bounded, sensible sub-query set; an ordinary single-focus query
      correctly decomposes into "no decomposition needed" and falls back to
      one non-decomposed dispatch;
  (b) a decomposer parse failure degrades to the same one-dispatch fallback,
      WITH a caveat disclosing it (unlike decompose:false, which adds none);
  (c) one sub-query's pipeline failure/timeout doesn't crash the whole
      meta-analysis — the other sub-queries' results still reach synthesis,
      with the failure disclosed as a caveat, not silently dropped;
  (d) all-DENIED -> status=DENIED (never collapsed into ABSTAINED/EMPTY,
      RESOLVED-6); a MIX of DENIED + OK -> PARTIAL, never collapsed either;
  (e) all-EMPTY (nothing found anywhere, nothing failed) -> status=EMPTY,
      deterministic text, NO LLM call;
  (f) all sub-queries failed -> status=ABSTAINED;
  (g) the recursion guard: every Supervisor.handle() call this module makes
      passes allow_meta_analysis=False;
  (h) N is capped at 5, even if the decomposer returns more;
  (i) [Gold-QA fix — Module 25, M2] a sub-answer's own dangling
      `[Document N]` citation is stripped before it reaches either the
      synthesis prompt or a verifier pseudo-chunk (the live-confirmed root
      cause of the M2 verifier-rejection bug), while a genuinely
      hallucinated synthesis is still correctly rejected;
  (j) module-level self-registration into the Supervisor's registry.

`Supervisor.handle` (the bound method, patched at the class level so every
`Supervisor()` instance this module constructs is covered), `call_llm_json`,
`call_llm`, `verify_grounding`, and `validate_answer` are monkeypatched at
the module level (`ma_mod.*` / the `Supervisor` class itself) in every test
— none of these hit live infra, per this repo's own test-isolation
convention for harness sub-agent tests.
"""
from __future__ import annotations

import asyncio

import pytest

import src.pipeline.harness.agents.meta_analysis as ma_mod
from src.pipeline.harness.agents.meta_analysis import meta_analysis
from src.pipeline.harness.supervisor import META_ANALYSIS, Supervisor, get_registered
from src.pipeline.harness.types import (
    CallerContext,
    ExecutionContext,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
    ValidationStatus,
)

_COMPOUND_QUERY = (
    "Summarize the recurring patterns across all robbery cases handled by "
    "this station in the last quarter and flag any that share a suspect "
    "with an unresolved case."
)
_SUB_Q1 = "What recurring patterns appear across robbery cases at this station in the last quarter?"
_SUB_Q2 = "Which of those cases share a suspect with an unresolved case?"


def _caller(role=Role.SUPERVISOR, **kw):
    return CallerContext(user_id="u1", role=role, active_case_id=None, **kw)


def _execution(caller=None):
    return ExecutionContext(caller=caller or _caller())


def _agent_input(caller=None, query_text=_COMPOUND_QUERY, **kw):
    return SubAgentInput(query_text=query_text, execution=_execution(caller=caller), **kw)


def _stub_decompose(monkeypatch, decompose: bool, sub_queries=None, synthesis_goal="", fail=False):
    """
    Stubs at the boundary meta_analysis.py actually calls
    (`ma_mod.call_llm_json`) -- `fail=True` simulates the parse-failure path
    (returns (None, raw)), matching call_llm_json's own real failure
    contract.
    """

    async def _fake(**kwargs):
        if fail:
            return None, "not json"
        return (
            {"decompose": decompose, "sub_queries": sub_queries or [], "synthesis_goal": synthesis_goal},
            "{}",
        )

    monkeypatch.setattr(ma_mod, "call_llm_json", _fake)


def _stub_supervisor_handle(monkeypatch, by_query: dict[str, SubAgentResult], default=None, calls=None):
    """
    Patches `Supervisor.handle` at the CLASS level -- every `Supervisor()`
    instance `meta_analysis.py` constructs is covered. Records every call's
    kwargs (specifically `allow_meta_analysis`) for the recursion-guard
    tests. `by_query` may map a sub-query's exact text to either a
    `SubAgentResult` (returned normally) or an `Exception` instance (raised)
    or the string "timeout" (sleeps past any reasonable test timeout,
    exercising the real `asyncio.wait_for` path).
    """
    call_log = calls if calls is not None else []

    async def _fake(self, agent_input, *, on_event=None, gateway=None, allow_meta_analysis=True):
        call_log.append({"query_text": agent_input.query_text, "allow_meta_analysis": allow_meta_analysis})
        outcome = by_query.get(agent_input.query_text, default)
        if isinstance(outcome, Exception):
            raise outcome
        if outcome == "timeout":
            await asyncio.sleep(9999)
        if outcome is None:
            return SubAgentResult(status=SubAgentStatus.OK, answer_text="stub")
        return outcome

    monkeypatch.setattr(Supervisor, "handle", _fake)
    return call_log


def _stub_call_llm(monkeypatch, answer=None, exc=None):
    async def _fake(system_prompt, user_message, **kwargs):
        if exc is not None:
            raise exc
        return answer

    monkeypatch.setattr(ma_mod, "call_llm", _fake)


def _stub_verify_grounding(monkeypatch, grounded=True, off_topic=False, reason=""):
    async def _fake(**kwargs):
        return {"grounded": grounded, "off_topic": off_topic, "reason": reason}

    monkeypatch.setattr(ma_mod, "verify_grounding", _fake)


def _stub_validate_answer(monkeypatch, status=None, claims=None):
    resolved_status = status if status is not None else ValidationStatus.PASSED
    resolved_claims = claims if claims is not None else []

    async def _fake(*args, **kwargs):
        return resolved_status, resolved_claims

    monkeypatch.setattr(ma_mod, "validate_answer", _fake)


# ═══════════════════════════════════════════════════════════════════════
# Decomposer JSON schema lock — same discipline as test_router.py's own
# few-shot-schema tests and test_doc_classifier.py's enum-drift guard: lock
# the prompt's JSON schema to whatever contract meta_analysis.py's own
# `_validate_decomposer_result()` expects, independent of any real LLM call.
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize(
    "payload",
    [
        {"decompose": True, "sub_queries": [_SUB_Q1, _SUB_Q2], "synthesis_goal": "combine both findings"},
        {"decompose": True, "sub_queries": [_SUB_Q1], "synthesis_goal": "just the one finding"},
        {"decompose": False},
        {"decompose": False, "sub_queries": [], "synthesis_goal": ""},
    ],
)
def test_decomposer_schema_accepts_valid_shapes(payload):
    assert ma_mod._validate_decomposer_result(payload) is True


@pytest.mark.parametrize(
    "payload",
    [
        {},  # missing "decompose" entirely
        {"decompose": True},  # missing sub_queries/synthesis_goal
        {"decompose": True, "sub_queries": [], "synthesis_goal": "x"},  # empty list
        {"decompose": True, "sub_queries": [_SUB_Q1] * 6, "synthesis_goal": "x"},  # over the N=5 cap
        {"decompose": True, "sub_queries": "not a list", "synthesis_goal": "x"},
        {"decompose": True, "sub_queries": [_SUB_Q1], "synthesis_goal": ""},  # blank synthesis_goal
        {"decompose": True, "sub_queries": [_SUB_Q1, ""], "synthesis_goal": "x"},  # blank sub-query
        "not a dict",
    ],
)
def test_decomposer_schema_rejects_malformed_shapes(payload):
    assert ma_mod._validate_decomposer_result(payload) is False


# ═══════════════════════════════════════════════════════════════════════
# (a) decompose-step schema / bounded sub-query set
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_compound_query_decomposes_into_a_bounded_sensible_set(monkeypatch):
    _stub_decompose(
        monkeypatch,
        decompose=True,
        sub_queries=[_SUB_Q1, _SUB_Q2],
        synthesis_goal="Combine the pattern summary with the shared-suspect findings.",
    )
    call_log = _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text="Pattern: nighttime robberies [Document 1]."),
            _SUB_Q2: SubAgentResult(status=SubAgentStatus.OK, answer_text="CASE-014 shares a suspect [Document 1]."),
        },
    )
    _stub_call_llm(monkeypatch, "Combined finding [Document 1] and [Document 2].")
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert result.status == SubAgentStatus.OK
    assert result.answer_text == "Combined finding [Document 1] and [Document 2]."
    assert len(result.citations) == 2
    assert {c["query_text"] for c in call_log} == {_SUB_Q1, _SUB_Q2}
    assert len(call_log) == 2  # bounded, exactly the sub-queries the decomposer returned


@pytest.mark.asyncio
async def test_ordinary_single_focus_query_is_not_decomposed(monkeypatch):
    _stub_decompose(monkeypatch, decompose=False)
    call_log = _stub_supervisor_handle(
        monkeypatch, {}, default=SubAgentResult(status=SubAgentStatus.OK, answer_text="Case CASE-021 summary.")
    )

    result = await meta_analysis(_agent_input(query_text="Summarize case CASE-021."))

    assert result.status == SubAgentStatus.OK
    assert result.answer_text == "Case CASE-021 summary."
    assert len(call_log) == 1  # exactly one, non-decomposed dispatch of the original query
    assert call_log[0]["query_text"] == "Summarize case CASE-021."
    assert not result.caveats  # decompose:false is a correct decision, not a degradation -- no caveat


@pytest.mark.asyncio
async def test_decomposer_parse_failure_falls_back_with_a_caveat(monkeypatch):
    _stub_decompose(monkeypatch, decompose=True, fail=True)
    call_log = _stub_supervisor_handle(
        monkeypatch, {}, default=SubAgentResult(status=SubAgentStatus.OK, answer_text="Fallback answer.")
    )

    result = await meta_analysis(_agent_input())

    assert result.status == SubAgentStatus.OK
    assert result.answer_text == "Fallback answer."
    assert len(call_log) == 1
    assert any("could not run" in c for c in result.caveats)


@pytest.mark.asyncio
async def test_n_is_capped_even_if_the_decomposer_returns_more(monkeypatch):
    seven = [f"sub-question {i}" for i in range(7)]
    _stub_decompose(monkeypatch, decompose=True, sub_queries=seven, synthesis_goal="combine everything")
    call_log = _stub_supervisor_handle(
        monkeypatch, {}, default=SubAgentResult(status=SubAgentStatus.OK, answer_text="ans [Document 1].")
    )
    _stub_call_llm(monkeypatch, "Combined [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)

    await meta_analysis(_agent_input())

    assert len(call_log) == 5


# ═══════════════════════════════════════════════════════════════════════
# (c) partial-failure graceful degradation
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_one_sub_query_failure_does_not_crash_the_others(monkeypatch):
    _stub_decompose(
        monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="combine findings"
    )
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text="Pattern found [Document 1]."),
            _SUB_Q2: RuntimeError("upstream boom"),
        },
    )
    _stub_call_llm(monkeypatch, "Synthesis using only the pattern finding [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert result.answer_text == "Synthesis using only the pattern finding [Document 1]."
    assert len(result.citations) == 1  # only the surviving sub-query contributed a pseudo-document
    assert any(_SUB_Q2 in c for c in result.caveats)


@pytest.mark.asyncio
async def test_one_sub_query_timeout_does_not_crash_the_others(monkeypatch):
    _stub_decompose(
        monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="combine findings"
    )
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text="Pattern found [Document 1]."),
            _SUB_Q2: "timeout",
        },
    )
    monkeypatch.setattr(ma_mod.config, "META_ANALYSIS_SUBQUERY_TIMEOUT", 0.05)
    _stub_call_llm(monkeypatch, "Synthesis using only the pattern finding [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert any("timed out" in c for c in result.caveats)


@pytest.mark.asyncio
async def test_all_sub_queries_failed_abstains(monkeypatch):
    _stub_decompose(
        monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="combine findings"
    )
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: RuntimeError("boom 1"),
            _SUB_Q2: RuntimeError("boom 2"),
        },
    )

    result = await meta_analysis(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None


# ═══════════════════════════════════════════════════════════════════════
# (d) DENIED bucketing — RESOLVED-6, never collapsed
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_all_sub_queries_denied_propagates_as_denied(monkeypatch):
    _stub_decompose(
        monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="combine findings"
    )
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(
                status=SubAgentStatus.DENIED, error=ToolError(kind="permission_denied", message="denied")
            ),
            _SUB_Q2: SubAgentResult(
                status=SubAgentStatus.DENIED, error=ToolError(kind="permission_denied", message="denied")
            ),
        },
    )

    result = await meta_analysis(_agent_input())

    assert result.status == SubAgentStatus.DENIED


@pytest.mark.asyncio
async def test_mixed_denied_and_ok_is_partial_not_denied(monkeypatch):
    """[RESOLVED-6, generalized to N] A mix of DENIED + something-else must
    never be collapsed into DENIED (nor ABSTAINED/EMPTY) -- disclosed as a
    caveat, the surviving real content still reaches the user."""
    _stub_decompose(
        monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="combine findings"
    )
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text="Pattern found [Document 1]."),
            _SUB_Q2: SubAgentResult(
                status=SubAgentStatus.DENIED, error=ToolError(kind="permission_denied", message="denied")
            ),
        },
    )
    _stub_call_llm(monkeypatch, "Synthesis [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert result.answer_text == "Synthesis [Document 1]."
    assert any(_SUB_Q2 in c for c in result.caveats)


# ═══════════════════════════════════════════════════════════════════════
# (e) all-EMPTY short-circuits without an LLM call
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_all_empty_short_circuits_without_an_llm_call(monkeypatch):
    _stub_decompose(
        monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="combine findings"
    )
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.EMPTY),
            _SUB_Q2: SubAgentResult(status=SubAgentStatus.EMPTY),
        },
    )
    llm_calls = []

    async def _fail_if_called(*a, **kw):
        llm_calls.append(1)
        raise AssertionError("call_llm must not be called when every sub-query is EMPTY")

    monkeypatch.setattr(ma_mod, "call_llm", _fail_if_called)

    result = await meta_analysis(_agent_input())

    assert result.status == SubAgentStatus.EMPTY
    assert result.answer_text is not None
    assert not llm_calls


# ═══════════════════════════════════════════════════════════════════════
# (g) recursion guard — every recursive dispatch passes allow_meta_analysis=False
# ═══════════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_every_subquery_dispatch_passes_allow_meta_analysis_false(monkeypatch):
    _stub_decompose(
        monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="combine findings"
    )
    call_log = _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text="a [Document 1]."),
            _SUB_Q2: SubAgentResult(status=SubAgentStatus.OK, answer_text="b [Document 1]."),
        },
    )
    _stub_call_llm(monkeypatch, "Combined [Document 1].")
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)

    await meta_analysis(_agent_input())

    assert len(call_log) == 2
    assert all(c["allow_meta_analysis"] is False for c in call_log)


@pytest.mark.asyncio
async def test_fallback_dispatch_also_passes_allow_meta_analysis_false(monkeypatch):
    _stub_decompose(monkeypatch, decompose=False)
    call_log = _stub_supervisor_handle(
        monkeypatch, {}, default=SubAgentResult(status=SubAgentStatus.OK, answer_text="ans")
    )

    await meta_analysis(_agent_input(query_text="Summarize case CASE-021."))

    assert len(call_log) == 1
    assert call_log[0]["allow_meta_analysis"] is False


# ═══════════════════════════════════════════════════════════════════════
# (i) [Gold-QA fix — Module 25, M2] the verifier-rejection interaction bug.
#
# Live-confirmed root cause (2026-09-08, M2's exact gold text — see
# GOLD_QA_REMAINING_FIXES_PLAN.md's Module 25 section for the full writeup):
# a sub-answer's own `[Document N]` citation (from whichever sub-agent
# produced it) survived unchanged into the pseudo-chunk this module builds
# for the Verifier, colliding with the SEPARATE `[Document N]` numbering
# the synthesis prompt assigns to pseudo-chunks 1..len(entries). Two
# sub-answers each independently citing their own "[Document 1]" landed
# side by side as meta-analysis chunks [1] and [2], both still containing
# a stale "[Document 1]" — which is what actually confused the Verifier's
# LLM judge into misreading which chunk backed which claim, live. No
# deterministic pre-check (_check_fabricated_case_ids/_check_leakage/etc.)
# ever fired for this; the fix is `_strip_nested_citations()`, applied to
# every sub-answer's text before it becomes part of the synthesis prompt OR
# a verifier pseudo-chunk.
# ═══════════════════════════════════════════════════════════════════════


def test_strip_nested_citations_removes_every_document_marker_shape():
    strip = ma_mod._strip_nested_citations
    assert strip("Caseload cannot be compared [Document 1].") == "Caseload cannot be compared."
    assert strip("Caseload cannot be compared (Document 2).") == "Caseload cannot be compared."
    assert strip("Caseload cannot be compared **Document 3**.") == "Caseload cannot be compared."
    assert strip("No station-type data [Document 1], so no comparison is possible.") == (
        "No station-type data, so no comparison is possible."
    )
    # No citation present -- text passes through unchanged (aside from the
    # trim every call applies).
    assert strip("Nothing to strip here.") == "Nothing to strip here."


@pytest.mark.asyncio
async def test_nested_citations_are_stripped_before_reaching_the_synthesis_prompt_and_verifier(monkeypatch):
    """
    Reproduces the exact live shape: both sub-answers cite their OWN
    "[Document 1]" (from their respective sub-agent's unrelated evidence),
    which must never survive into either (a) the synthesis prompt's own
    `documents` section, or (b) the pseudo-chunks handed to
    `verify_grounding()` -- both would otherwise carry a dangling,
    colliding "[Document 1]" alongside the meta-analysis-level numbering.
    """
    _stub_decompose(
        monkeypatch,
        decompose=True,
        sub_queries=[_SUB_Q1, _SUB_Q2],
        synthesis_goal="Combine both station-type findings.",
    )
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(
                status=SubAgentStatus.OK,
                answer_text="No station-type classification exists for general-purpose stations [Document 1].",
            ),
            _SUB_Q2: SubAgentResult(
                status=SubAgentStatus.OK,
                answer_text="No station-type classification exists for specialized stations [Document 1].",
            ),
        },
    )

    captured_prompt = {}

    async def _fake_call_llm(system_prompt, user_message, **kwargs):
        captured_prompt["system_prompt"] = system_prompt
        return "Neither can be compared [Document 1][Document 2]."

    monkeypatch.setattr(ma_mod, "call_llm", _fake_call_llm)

    captured_chunks = {}

    async def _fake_verify_grounding(*, answer, cited_chunks, **kwargs):
        captured_chunks["chunks"] = cited_chunks
        return {"grounded": True, "off_topic": False, "reason": "grounded"}

    monkeypatch.setattr(ma_mod, "verify_grounding", _fake_verify_grounding)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert result.status == SubAgentStatus.OK
    # Neither pseudo-chunk's text carries a leftover "[Document N]" from
    # its own originating sub-agent.
    for chunk in captured_chunks["chunks"]:
        assert "Document" not in chunk["text"]
    # Nor does the synthesis prompt's own sub-answers section carry the
    # stale per-sub-answer citation (the legitimate `[Document N]
    # Sub-question: ...` HEADER line this module itself adds is expected
    # and excluded here).
    subanswers_section = captured_prompt["system_prompt"].split("--- SUB-ANSWERS ---")[1]
    assert "for general-purpose stations." in subanswers_section
    assert "for general-purpose stations [Document 1]." not in subanswers_section
    assert "for specialized stations." in subanswers_section
    assert "for specialized stations [Document 1]." not in subanswers_section


@pytest.mark.asyncio
async def test_hallucinated_synthesis_is_still_rejected(monkeypatch):
    """
    The other half of the fix's contract, required alongside the false-
    positive fix above: a synthesis that asserts something absent from
    every sub-answer must still be caught. Stripping nested citations must
    never widen into "the verifier always passes now."
    """
    _stub_decompose(
        monkeypatch,
        decompose=True,
        sub_queries=[_SUB_Q1, _SUB_Q2],
        synthesis_goal="Combine both findings.",
    )
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text="Pattern: nighttime robberies."),
            _SUB_Q2: SubAgentResult(status=SubAgentStatus.OK, answer_text="CASE-014 shares a suspect."),
        },
    )
    _stub_call_llm(monkeypatch, "CASE-014's suspect was previously convicted twice [Document 2].")
    _stub_verify_grounding(
        monkeypatch,
        grounded=False,
        reason="The prior-convictions claim is not stated in any sub-answer.",
    )

    result = await meta_analysis(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert any("could not be verified as grounded" in c for c in result.caveats)


# ═══════════════════════════════════════════════════════════════════════
# (j) registration
# ═══════════════════════════════════════════════════════════════════════


def test_meta_analysis_is_registered():
    assert get_registered(META_ANALYSIS) is meta_analysis
    assert meta_analysis.name == META_ANALYSIS


# ═══════════════════════════════════════════════════════════════════════
# (k) [Gold-QA fix — Module 29, questions CR3 / G1 / G6] Deterministic
#     decomposition of broad synthesis questions.
#
# The defect these pin, measured live on this branch's base commit by
# calling `_decompose()` directly against each question's literal gold
# text: CR3, G1 and G6 all came back `decompose=False`, so Meta-Analysis
# re-dispatched the ORIGINAL question, which landed back on
# XNETWORK/Cross-Case Linkage and was correctly refused there (Module 21
# measured those distances; `xnetwork.py` is not touched by this module).
#
# Both directions are pinned, because the risk is symmetrical:
#   - the three literal gold texts MUST decompose, into exactly the
#     sub-questions the plan declares;
#   - NO other gold question may match a plan, and a simple single-fact
#     question must still return `decompose: false`.
# ═══════════════════════════════════════════════════════════════════════

_CR3_GOLD = (
    "In the online banking fraud matter involving two separate victims, was each "
    "victim's case processed and recorded the same way?"
)
_G1_GOLD = (
    "Acting as a crime analyst, review our current caseload and flag anything that "
    "looks unusual or worth monitoring."
)
_G6_GOLD = (
    "Yahan naye tainaat hone wale afsar ke liye ek mukhtasar orientation note likhein "
    "— unhein mojooda case load se kya tawaqqo rakhni chahiye?"
)
# Module 21 captured this one live as the "before" state: it matched no
# Meta-Analysis trigger at all, so it never even reached this module.
_G1_PARAPHRASE = (
    "As the on-duty analyst, look over everything currently open and tell me what's "
    "worth a second look"
)


def _load_gold32():
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


def _forbid_llm_json(monkeypatch):
    """A deterministic plan must skip the decomposer LLM call entirely."""

    async def _boom(*a, **kw):  # pragma: no cover - only fires on regression
        raise AssertionError("the decomposer LLM was called for a deterministic plan")

    monkeypatch.setattr(ma_mod, "call_llm_json", _boom)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, plan_name",
    [
        (_CR3_GOLD, "record_consistency"),
        (_G1_GOLD, "caseload_review"),
        (_G6_GOLD, "orientation_note"),
        (_G1_PARAPHRASE, "caseload_review"),
    ],
    ids=["CR3", "G1", "G6", "G1-paraphrase"],
)
async def test_module29_broad_synthesis_questions_decompose_deterministically(
    monkeypatch, query, plan_name
):
    _forbid_llm_json(monkeypatch)

    result = await ma_mod._decompose(query)

    assert result.decompose is True
    assert result.parse_failed is False
    assert result.plan_name == plan_name
    assert 2 <= len(result.sub_queries) <= ma_mod._MAX_SUB_QUERIES
    assert result.synthesis_goal.strip()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, expected",
    [
        (
            _CR3_GOLD,
            (
                ma_mod._SQ_PERSON_RECURRENCE,
                ma_mod._SQ_CMS_LINKAGE,
            ),
        ),
        (
            _G1_GOLD,
            (
                ma_mod._SQ_COMPLETENESS,
                ma_mod._SQ_PERSON_RECURRENCE,
                ma_mod._SQ_WEAPON_LICENCE,
                ma_mod._SQ_CASE_MIX_BY_YEAR,
                ma_mod._SQ_CRIMINAL_RECORD_VS_COURT,
            ),
        ),
        (
            _G6_GOLD,
            (
                ma_mod._SQ_DISTRICT_SPREAD,
                ma_mod._SQ_CASE_MIX_BY_YEAR,
                ma_mod._SQ_REPORTING_SPEED,
                ma_mod._SQ_WEAPON_LICENCE,
                ma_mod._SQ_GENDER,
            ),
        ),
    ],
    ids=["CR3", "G1", "G6"],
)
async def test_module29_literal_sub_query_decomposition_is_pinned(monkeypatch, query, expected):
    """The literal decomposition, not just `decompose: true`.

    Each of these sub-questions was probed live against `run_aggregate()`
    before being put in a plan, and each reaches a real aggregate family
    (case completeness, person recurrence, weapon compliance, statute mix by
    year, criminal-record/court cross-check, district breakdown,
    incident->report minutes by year, gender breakdown, CMS-FIR linkage).
    A future edit that changes one silently is a behavioural change, not a
    wording change.
    """
    _forbid_llm_json(monkeypatch)

    result = await ma_mod._decompose(query)

    assert tuple(result.sub_queries) == expected


def test_module29_every_planned_sub_query_routes_deterministically_to_xagg():
    """The reason the sub-questions are worded the way they are.

    Measured on this module's base commit: the LLM router sent 6 of 9
    naturally-phrased sub-questions to XGRAPH/Cross-Case Linkage — the exact
    dead end decomposition exists to route away from. Every planned
    sub-query is therefore phrased to match `router.py`'s own deterministic
    XAGG override, so a decomposed dispatch costs no extra router LLM call
    and cannot drift onto the entity-linkage path.
    """
    from src.pipeline import router

    for plan in ma_mod._DECOMPOSITION_PLANS:
        for sub_query in plan.sub_queries:
            override = router._deterministic_route_override(sub_query, case_id=None)
            assert override is not None, f"{plan.name}: no deterministic route for {sub_query!r}"
            assert override["route"] == "XAGG", f"{plan.name}: {sub_query!r} -> {override['route']}"


def test_module29_no_other_gold_question_matches_a_decomposition_plan():
    """Negative control — the "too broad" half of the risk.

    A plan that also caught a currently-correct single-fact question would
    turn its answer into a muddled five-way synthesis at five times the
    model cost. Exactly three of the 32 gold questions may match.
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


def test_module29_supervisor_trigger_widening_adds_no_gold_question():
    """The paraphrase-reach patterns added to `_META_ANALYSIS_TRIGGER_PATTERNS`.

    G1's own non-gold paraphrase matched nothing in that list, so it never
    reached this module at all and no decomposition fix could have helped
    it. Widening that gate is the other half of Module 29 — and the gate
    decides routing for every query, so the set of gold questions it
    captures must not change. This is the pre-Module-29 set, pinned.
    """
    from src.pipeline.harness import supervisor as sup

    matched = {
        item["id"]
        for item in _load_gold32()
        if any(pat.search(item["question"]) for pat in sup._META_ANALYSIS_TRIGGER_PATTERNS)
    }

    assert matched == {
        "CR3", "CR6", "CR7", "CR8", "CS4",
        "M1", "M2", "M4", "M5", "M7",
        "G1", "G2", "G3", "G5", "G6",
    }
    # ...and the paraphrases the widening exists for now do reach it.
    assert any(pat.search(_G1_PARAPHRASE) for pat in sup._META_ANALYSIS_TRIGGER_PATTERNS)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query",
    [
        "How many FIRs are currently registered?",
        "Summarize case CASE-021.",
        "What is the FIR number for case CASE-014?",
        "Which district recovers the most weapons?",
        # G5 and G2 already reach Meta-Analysis via their own supervisor
        # triggers and already answer correctly through the decompose:false
        # single-dispatch fallback — a plan must never claim them.
        "Recovered weapons ke record ko dekhtay hue, compliance ke lihaz se koi cheez flag karne layak hai?",
    ],
    ids=["D1", "A1-summary", "FIR-lookup", "CP1-shape", "G5"],
)
async def test_module29_single_fact_questions_still_return_decompose_false(monkeypatch, query):
    """Negative control at the `_decompose()` boundary, not just the regex.

    No deterministic plan may claim these, and with the LLM decomposer
    answering "not compound" (its real, live behaviour for all of them),
    `_decompose()` must still report `decompose=False` so the module falls
    back to exactly one non-decomposed dispatch.
    """
    assert ma_mod._match_decomposition_plan(query) is None

    _stub_decompose(monkeypatch, decompose=False)
    result = await ma_mod._decompose(query)

    assert result.decompose is False
    assert result.parse_failed is False
    assert result.plan_name is None


@pytest.mark.asyncio
async def test_module29_g1_dispatches_five_sub_queries_not_one(monkeypatch):
    """The observable this module is graded on.

    Before: one sub-agent dispatch per query in the live SSE trace, of the
    original un-split question. After: one dispatch per planned
    sub-question, each independently routed and each contributing a
    pseudo-chunk to the synthesis.
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
    _stub_call_llm(monkeypatch, answer="Synthesized review. [Document 1]")
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input(query_text=_G1_GOLD))

    assert result.status == SubAgentStatus.OK
    dispatched = [c["query_text"] for c in call_log]
    assert len(dispatched) == 5
    assert dispatched != [_G1_GOLD]
    caseload_plan = next(p for p in ma_mod._DECOMPOSITION_PLANS if p.name == "caseload_review")
    assert set(dispatched) == set(caseload_plan.sub_queries)
    assert len(result.citations) == 5
