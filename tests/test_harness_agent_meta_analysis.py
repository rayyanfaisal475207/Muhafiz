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
from src import config
from src.pipeline.harness.agents import _salvage
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
    hallucination = "CASE-014's suspect was previously convicted twice [Document 2]."
    _stub_call_llm(monkeypatch, hallucination)
    _stub_verify_grounding(
        monkeypatch,
        grounded=False,
        reason="The prior-convictions claim is not stated in any sub-answer.",
    )
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    # [Module 71] The status changed (ABSTAINED -> PARTIAL, so the verified
    # sub-answers are served instead of `status=error`), and the contract
    # this test exists for did NOT: the rejected text is never served.
    assert result.status == SubAgentStatus.PARTIAL
    assert "previously convicted" not in (result.answer_text or "")
    assert hallucination not in (result.answer_text or "")
    assert "Pattern: nighttime robberies." in result.answer_text
    assert "CASE-014 shares a suspect." in result.answer_text
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
    # [Module 110] `_MAX_PLAN_SUB_QUERIES`, not `_MAX_SUB_QUERIES`: this is
    # the deterministic-plan path, and `_decompose()` truncates it against
    # the PLAN cap. The two constants were the same number until Module 110
    # raised the plan cap to 8, which is exactly the divergence Module 50
    # split them to allow.
    assert 2 <= len(result.sub_queries) <= ma_mod._MAX_PLAN_SUB_QUERIES
    assert result.synthesis_goal.strip()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, expected",
    [
        # [Gold-QA fix — Module 50] All three tuples CHANGED PREMISE, not
        # assertion — the same distinction Module 41 drew for the two tests
        # it had to swap. Module 29 wrote these to pin the composition it
        # shipped; Module 50 re-composes all three plans to reach the six
        # aggregates Modules 31–36 built, and the test's PURPOSE — "a future
        # edit that changes one silently is a behavioural change" — is
        # unchanged and still enforced. What each plan is now composed of,
        # and what was dropped to fit `_MAX_PLAN_SUB_QUERIES`, is argued in
        # `meta_analysis.py` at each plan's own comment block.
        (
            _CR3_GOLD,
            (
                ma_mod._SQ_FIR_LISTING_CYBER,   # Module 36 — identifies the pair
                ma_mod._SQ_PERSON_RECURRENCE,
                ma_mod._SQ_CMS_LINKAGE,
            ),
        ),
        (
            _G1_GOLD,
            (
                ma_mod._SQ_ACCUSED_AGE,         # Module 31 — gold finding (1)
                ma_mod._SQ_RELATIONSHIP,        # Module 32 — gold finding (2)
                ma_mod._SQ_SEIZED_PROPERTY,     # Module 33 — gold finding (3)
                ma_mod._SQ_TIME_OF_DAY,         # Module 34 — gold finding (4)
                ma_mod._SQ_PERSON_RECURRENCE,
            ),
        ),
        (
            _G6_GOLD,
            (
                ma_mod._SQ_DISTRICT_SPREAD,
                ma_mod._SQ_CASE_MIX_BY_YEAR,
                ma_mod._SQ_ARREST_RATE,         # Module 35
                ma_mod._SQ_REPORTING_SPEED,
                ma_mod._SQ_WEAPON_LICENCE,
                # [Gold-QA fix - Module 110] CHANGED PREMISE again, and for
                # the third time on this block the purpose is unchanged: the
                # five above are gold's findings 1, 2, 4, 6 and 7, and these
                # three are gold's findings 3 and 5, which nothing computed.
                ma_mod._SQ_ACCUSED_AGE,         # gold finding (3), part a
                ma_mod._SQ_RELATIONSHIP,        # gold finding (3), part b
                ma_mod._SQ_COURT_STAGE,         # gold finding (5)
            ),
        ),
    ],
    ids=["CR3", "G1", "G6"],
)
async def test_module29_literal_sub_query_decomposition_is_pinned(monkeypatch, query, expected):
    """The literal decomposition, not just `decompose: true`.

    Each of these sub-questions was probed live against `run_aggregate()`
    before being put in a plan, and each reaches a real aggregate family
    (filtered FIR listing, person recurrence, CMS-FIR linkage; offender age,
    accused-relationship breakdown, seized-property disposition, incident
    time-of-day; district breakdown, statute mix by year, arrest rate,
    incident->report minutes by year, weapon compliance).
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

    [Gold-QA fix - Module 110] SUPERSEDED IN PART, and left here rather than
    deleted because the measurement above is still the reason the cap is not
    unbounded. What changed is the DEADLINE, not the staircase: Module 53
    moved `META_ANALYSIS_SUBQUERY_TIMEOUT` from 60 s to 150 s and this
    constant was deliberately not revisited at the time ("left as tracked
    work rather than changed on inference" - Module 59). Module 110 could not
    fix G6's five-of-seven coverage without it: each of `orientation_note`'s
    five slots carries exactly one of gold's seven findings, so no
    re-composition inside a cap of 5 can ever reach more than five. The plan
    cap is now 8 - see
    `test_module110_the_plan_cap_is_eight_because_the_headroom_invariant_says_so`
    for why exactly 8 - while the UNTRUSTED decomposer-output cap stays at 5,
    which is the whole point of Module 50's having split the two constants.
    """
    assert ma_mod._MAX_PLAN_SUB_QUERIES == 8
    # The LLM-decomposer cap is a SEPARATE bound on an untrusted list, and
    # Module 110 moved only the hand-authored one. The two constants have now
    # actually moved apart, which is what Module 50 split them for.
    assert ma_mod._MAX_SUB_QUERIES == 5


# ═══════════════════════════════════════════════════════════════════════
# Module 110 - G6's `orientation_note` plan computes five of gold's SEVEN
# findings, and the two it misses are unreachable from above.
#
# Gold's G6 answer (`evaluation/Gold_QA_Dataset_Final32_With_Answers.json`,
# Roman Urdu) carries seven findings. Five were already dispatched; two were
# not, and no synthesis over the five could carry them, because nothing
# computed them. Both were verified against the live graph before this module
# changed anything - see MODULE110_RESULT.md Section 1 - and both hold:
#
#   "mostly men aged 25-40, usually strangers"  67 of 94 accused entries are
#                                               men; 15 of the 17 recorded
#                                               ages fall in 25-40 (range
#                                               24-49, mean 31.5); 'اجنبی'
#                                               (stranger) is 15 of 24
#                                               recorded relationships
#   "most matters still pending in court"       32 of 33 criminal records are
#                                               in progress, 30 "Under trial"
#
# The tests below are the assertion form of "the plan can reach all seven".
# They are deliberately written over the PLAN, not over a generated answer:
# what this module fixes is computability, and whether the synthesis then
# says it well is Module 83's layer and is measured live, not asserted here.
# ═══════════════════════════════════════════════════════════════════════

# Gold's seven findings, each mapped to the sub-query constant that computes
# it. Written from gold's own sentence order. `accused_profile` is the one
# entry whose gold sentence has three components ("men", "25-40",
# "strangers"); only two of the three fit inside the cap, so it is listed
# against the two that do - see `_M110_UNCOMPUTED` below for the third.
_M110_GOLD_FINDINGS = {
    "district_spread": ("_SQ_DISTRICT_SPREAD",),
    "case_mix_change": ("_SQ_CASE_MIX_BY_YEAR",),
    "accused_profile": ("_SQ_ACCUSED_AGE", "_SQ_RELATIONSHIP"),
    "arrest_rate": ("_SQ_ARREST_RATE",),
    "still_pending_in_court": ("_SQ_COURT_STAGE",),
    "reporting_speed": ("_SQ_REPORTING_SPEED",),
    "weapon_licence": ("_SQ_WEAPON_LICENCE",),
}

# Honest residual, pinned so it cannot be quietly forgotten: gold's "zyada
# tar mulzim aisay mard hain" ("mostly men") is STILL not computed by this
# plan. `_SQ_GENDER` is the aggregate for it, Module 50 dropped it from this
# very plan to fit the arrest rate, and Module 59 records it as the first
# thing to restore if the cap rises. The cap rose to 8 and the plan is
# exactly 8, so it still does not fit.
_M110_UNCOMPUTED = ("_SQ_GENDER",)


def test_module110_orientation_note_computes_all_seven_of_golds_findings():
    """THE defect, as an assertion. Before this module the plan dispatched
    five sub-queries and `accused_profile` and `still_pending_in_court`
    resolved to nothing, so this fails on those two entries."""
    plan = next(p for p in ma_mod._DECOMPOSITION_PLANS if p.name == "orientation_note")
    missing = {
        finding: [c for c in consts if getattr(ma_mod, c) not in plan.sub_queries]
        for finding, consts in _M110_GOLD_FINDINGS.items()
    }
    missing = {k: v for k, v in missing.items() if v}
    assert not missing, (
        f"orientation_note cannot carry these gold findings - no sub-query "
        f"computes them, so no synthesis can state them: {missing}"
    )


def test_module110_the_two_new_findings_reuse_g1s_sub_queries_verbatim():
    """No new aggregate and no new dispatch string was written for G6.

    The age and relationship sub-queries are the SAME constants
    `caseload_review` dispatches for G1 - already live-verified by Modules
    31/32 and pinned byte-identical in `tests/test_xagg.py`. If a later
    module rewords one for G1's benefit, G6 must move with it or this fails;
    a divergent copy is exactly the silent failure Module 50's own
    byte-identity test exists to prevent.
    """
    plans = {p.name: p for p in ma_mod._DECOMPOSITION_PLANS}
    shared = (ma_mod._SQ_ACCUSED_AGE, ma_mod._SQ_RELATIONSHIP)
    for sq in shared:
        assert sq in plans["orientation_note"].sub_queries
        assert sq in plans["caseload_review"].sub_queries, (
            "G6 must reuse G1's sub-query, not carry a private copy of it"
        )
    # The third addition is a NEW dispatch string, and the reason it is new
    # rather than the long-declared `_SQ_CRIMINAL_RECORD_VS_COURT` is a live
    # measurement, so it is asserted rather than left to the comment: the two
    # reach the SAME aggregate but ask for different halves of it, and G6
    # needs the half CR7's question does not ask for. If a later module
    # collapses them back into one string, G6 silently loses gold's finding
    # 5 again while every unit test still passes.
    from src.pipeline import xagg as _xagg

    assert ma_mod._SQ_COURT_STAGE != ma_mod._SQ_CRIMINAL_RECORD_VS_COURT
    assert (
        _xagg.resolve_aggregate_kind(ma_mod._SQ_COURT_STAGE)
        == _xagg.resolve_aggregate_kind(ma_mod._SQ_CRIMINAL_RECORD_VS_COURT)
        == "criminal_record_court_crosscheck"
    )
    assert ma_mod._SQ_COURT_STAGE in next(
        p for p in ma_mod._DECOMPOSITION_PLANS if p.name == "orientation_note"
    ).sub_queries


def test_module110_the_plan_cap_is_eight_because_the_headroom_invariant_says_so():
    """8 is not a taste judgement, and this is where that is enforced.

    `test_module53_subquery_timeout_leaves_headroom_over_the_measured_staircase`
    already requires `_MAX_PLAN_SUB_QUERIES` x 12 s x 1.5 to fit inside
    `META_ANALYSIS_SUBQUERY_TIMEOUT`. At the post-Module-53 deadline of 150 s
    that permits 8 (144 s) and refuses 9 (162 s). Asserted from both ends so
    that a later module cannot raise the cap without moving the deadline, and
    cannot lower the deadline without lowering the cap.
    """
    assert ma_mod._MAX_PLAN_SUB_QUERIES == 8
    step_seconds, headroom = 12.0, 1.5
    assert ma_mod._MAX_PLAN_SUB_QUERIES * step_seconds * headroom <= config.META_ANALYSIS_SUBQUERY_TIMEOUT
    assert (ma_mod._MAX_PLAN_SUB_QUERIES + 1) * step_seconds * headroom > config.META_ANALYSIS_SUBQUERY_TIMEOUT


def test_module110_golds_gender_element_is_still_not_computed_and_says_so():
    """A test that asserts a KNOWN GAP, on purpose.

    Gold's accused-profile sentence has three components and only two fit.
    Recording the third as an assertion means the day someone raises the cap
    to 9 (or frees a slot), this test fails and points straight at the thing
    to add - rather than the gap surviving as a sentence in a result file
    nobody re-reads. It is not a claim that the gap is acceptable.
    """
    plan = next(p for p in ma_mod._DECOMPOSITION_PLANS if p.name == "orientation_note")
    assert len(plan.sub_queries) == ma_mod._MAX_PLAN_SUB_QUERIES, (
        "the plan is full - that is why the gap below still exists"
    )
    for const in _M110_UNCOMPUTED:
        assert getattr(ma_mod, const) not in plan.sub_queries, (
            f"{const} is now dispatched - good; delete it from _M110_UNCOMPUTED "
            f"and add it to _M110_GOLD_FINDINGS['accused_profile']"
        )


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

    # Same `route_result` shape `test_harness_supervisor.py` uses: the guard
    # is scoped to `route == "XAGG"`, so XAGG is the case that must be shown
    # still reaching Meta-Analysis, not a route the guard never inspects.
    xagg_cross_case = {"route": "XAGG", "case_scope": "cross_case", "output_format": "chat"}
    for query in (_CR3_GOLD, _G1_GOLD, _G6_GOLD, _G1_PARAPHRASE):
        assert sup.classify_to_subagent(xagg_cross_case, query) == sup.META_ANALYSIS


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query, expected_n",
    # [Module 110] G6 5 -> 8. A truncation bug shows up here as a dispatch
    # count short by exactly the number of sub-queries that module added.
    [(_G1_GOLD, 5), (_G6_GOLD, 8), (_CR3_GOLD, 3)],
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


# ═══════════════════════════════════════════════════════════════════════
# [Gold-QA fix — Module 53] The sub-query timeout measures QUEUE POSITION,
# not cost: `META_ANALYSIS_SUBQUERY_TIMEOUT` is one wall-clock deadline
# shared by the whole fan-out, and the model server serialises the
# sub-queries, so the last slot is killed for being served last. Module 50
# measured that the `XAGG <kind>` line is present for every timed-out
# sub-query — the aggregate had already computed, and only its LLM
# paraphrase was discarded. These tests pin the salvage path that now
# serves that computed aggregate instead of dropping the sub-answer.
# ═══════════════════════════════════════════════════════════════════════

_SALVAGED_TEXT = "Weapons: 30 of 32 recovered weapons (94%) are recorded unlicensed."


def _stub_supervisor_handle_offering_then_hanging(monkeypatch, hanging_query: str, offer_text: str):
    """The real shape of the defect: the sub-agent computes its deterministic
    aggregate, offers it, and is then cancelled mid-paraphrase by the shared
    deadline. Exercises the genuine `asyncio.wait_for` + ContextVar path —
    nothing about the salvage mechanism itself is stubbed."""

    async def _fake(self, agent_input, *, on_event=None, gateway=None, allow_meta_analysis=True):
        if agent_input.query_text == hanging_query:
            _salvage.offer(offer_text, tool="XAGG", kind="weapon_licence_status")
            await asyncio.sleep(9999)
        return SubAgentResult(
            status=SubAgentStatus.OK, answer_text="Pattern found [Document 1].", tools_used=["XAGG"]
        )

    monkeypatch.setattr(Supervisor, "handle", _fake)


@pytest.mark.asyncio
async def test_module53_timed_out_paraphrase_still_yields_the_raw_aggregate(monkeypatch):
    """THE Module 53 regression test. Before this fix the second sub-answer
    was dropped entirely and reported as 'Could not answer sub-question
    (timed out)'. It must now reach synthesis carrying the computed
    aggregate's own text."""
    _stub_decompose(
        monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="combine findings"
    )
    _stub_supervisor_handle_offering_then_hanging(monkeypatch, _SUB_Q2, _SALVAGED_TEXT)
    monkeypatch.setattr(ma_mod.config, "META_ANALYSIS_SUBQUERY_TIMEOUT", 0.05)

    seen = {}

    async def _capture_llm(system_prompt, user_message, **kwargs):
        seen["prompt"] = system_prompt
        return "Synthesis over both findings. [Document 1] [Document 2]"

    monkeypatch.setattr(ma_mod, "call_llm", _capture_llm)
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    # The salvaged sub-answer reached the synthesis prompt verbatim...
    assert _SALVAGED_TEXT in seen["prompt"]
    # ...and got its own pseudo-chunk / citation slot, i.e. it CONTRIBUTED
    # rather than being dropped.
    assert len(result.citations) == 2
    # ...but the run is still disclosed as degraded and raw, never as clean.
    assert result.status == SubAgentStatus.PARTIAL
    assert any("raw computed aggregate" in c for c in result.caveats)
    assert not any("Could not answer sub-question" in c for c in result.caveats)


@pytest.mark.asyncio
async def test_module53_timeout_with_nothing_computed_still_reports_a_failure(monkeypatch):
    """The narrow scope of the fix, pinned. A sub-agent with no
    deterministic result to offer (RAG/GRAPH) has nothing correct-by-
    construction to serve, so its timeout must still be a disclosed failure
    — the salvage path must not become 'always answer something'."""
    _stub_decompose(
        monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="combine findings"
    )
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text="Pattern found."),
            _SUB_Q2: "timeout",
        },
    )
    monkeypatch.setattr(ma_mod.config, "META_ANALYSIS_SUBQUERY_TIMEOUT", 0.05)
    _stub_call_llm(monkeypatch, "Synthesis using only the pattern finding. [Document 1]")
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert any("timed out" in c for c in result.caveats)
    assert len(result.citations) == 1


@pytest.mark.asyncio
async def test_module53_salvage_boxes_do_not_leak_between_sibling_sub_queries(monkeypatch):
    """Each `asyncio.gather` child opens its own box in its own Task context.
    If they shared one, a sub-query that timed out with nothing computed
    would be rescued by a SIBLING's aggregate — attributing one
    sub-question's numbers to another. Pinned because the whole mechanism
    rests on that isolation."""
    _stub_decompose(
        monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="combine findings"
    )

    async def _fake(self, agent_input, *, on_event=None, gateway=None, allow_meta_analysis=True):
        if agent_input.query_text == _SUB_Q1:
            # Computes and offers, but RETURNS normally — its offer must
            # never be visible to _SUB_Q2's box.
            _salvage.offer(_SALVAGED_TEXT, tool="XAGG", kind="weapon_licence_status")
            return SubAgentResult(status=SubAgentStatus.OK, answer_text="Paraphrased fine.")
        await asyncio.sleep(9999)

    monkeypatch.setattr(Supervisor, "handle", _fake)
    monkeypatch.setattr(ma_mod.config, "META_ANALYSIS_SUBQUERY_TIMEOUT", 0.05)
    _stub_call_llm(monkeypatch, "Synthesis. [Document 1]")
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert any("timed out" in c for c in result.caveats)
    assert not any("raw computed aggregate" in c for c in result.caveats)
    assert len(result.citations) == 1


def test_module53_subquery_timeout_leaves_headroom_over_the_measured_staircase():
    """`config.META_ANALYSIS_SUBQUERY_TIMEOUT` is not a taste setting. It is
    a SHARED deadline over a serialised staircase Module 50 measured at
    ~10 s per sub-query, and `_MAX_PLAN_SUB_QUERIES` sub-queries must fit
    inside it with room to spare — at the old 60 s the fifth slot landed at
    +56.7 s, i.e. 3.3 s of headroom, which is what Module 53 was filed for.
    """
    measured_seconds_per_sub_query = 12.0  # Module 50's staircase, rounded up.
    needed = ma_mod._MAX_PLAN_SUB_QUERIES * measured_seconds_per_sub_query
    assert config.META_ANALYSIS_SUBQUERY_TIMEOUT >= needed * 1.5


# ═══════════════════════════════════════════════════════════════════════
# Module 71 — a synthesis that blends denominators across sub-answers
#
# Pinned to the LIVE-CAPTURED shape, not an invented one. G1's five
# aggregates render, on this machine, as:
#
#   offender_age_profile:            17 of 92 distinct accused carry an age
#                                    (19 of 94 accused entries); range 24-49,
#                                    mean 31.5
#   accused_relationship_breakdown:  24 relationship edge(s) across 10 FIR(s);
#                                    dominant='اجنبی' 15; coverage 12 of 92
#   seized_property_disposition:     45 register entr(ies) across 28 FIR(s);
#                                    forensic-lab 13; heirs 7
#   incident_time_of_day:            64 of 73 incident(s) carry a datetime …
#   graph_recurrence:                4 recurring node(s)
#
# **73 appears in exactly one of them — the time-of-day sub-answer** — and
# the live rejection Module 61 recorded was
# *"the 73-case total for seized property"*. That is the number crossing
# from [Document 4] to [Document 3], and it is what these tests pin.
# ═══════════════════════════════════════════════════════════════════════

_M71_AGE = "17 of 92 distinct accused carry an age (19 of 94 accused entries); range 24-49, mean 31.5."
_M71_RELATIONSHIP = "24 relationship edges across 10 FIRs; the dominant value is اجنبی with 15; coverage 12 of 92."
_M71_PROPERTY = "45 register entries across 28 FIRs; 13 items went to a forensic laboratory and 7 were held for heirs."
_M71_TIME = "64 of 73 incidents carry a datetime; 50 usable; evening 19, afternoon 16, morning 14, night 1."
_M71_RECURRENCE = "4 accused recur across more than one FIR."

_M71_ENTRIES = [
    ("age", _M71_AGE),
    ("relationship", _M71_RELATIONSHIP),
    ("property", _M71_PROPERTY),
    ("time", _M71_TIME),
    ("recurrence", _M71_RECURRENCE),
]


def test_module71_figures_in_reads_ascii_and_urdu_digits():
    assert ma_mod.figures_in(_M71_PROPERTY) == ["45", "28", "13", "7"]
    # Urdu-rendered sub-answers carry Extended Arabic-Indic digits; the same
    # figures must be recognised, or the roster silently empties on an Urdu
    # run and rule 2 has nothing to point at.
    assert ma_mod.figures_in("۴۵ اندراجات، ۲۸ ایف آئی آر") == ["45", "28"]
    assert ma_mod.figures_in("٤٥ اندراجات") == ["45"]
    assert ma_mod.figures_in("mean 31.5 across 1,234 records") == ["31.5", "1,234", "1234"]
    # Structural noise below the floor is not a "figure".
    assert ma_mod.figures_in("the 2 records and 1 listing") == []
    assert ma_mod.figures_in("") == []


def test_module71_an_identifier_is_not_a_figure():
    """Measured, not assumed. Run offline against the eleven pre-fix live G1
    answers this module captured, the first draft of the rule flagged `24`
    and `64` on three of them — both pulled out of `fir-891-24` /
    `fir-64-26` in the recurring-accused sub-answer, where they are case
    numbers. Without this the detector would cry wolf on a correct answer,
    which is the fastest way to make a signal worthless."""
    assert ma_mod.figures_in("عاصم رشید appears in fir-64-26 and fir-65-26") == []
    assert ma_mod.figures_in("linked to CMS-ISB-2026-0341 on 2026-09-09") == []
    assert ma_mod.figures_in("evening (18:00-23:59) 19 incidents") == ["19"]
    # A genuine two-ended range is NOT an identifier and keeps both ends.
    assert ma_mod.figures_in("range 24-49, mean 31.5") == ["24", "49", "31.5"]
    # Nor is a hyphenated compound. The first draft of the identifier rule
    # read "73-case" as an identifier and threw away the exact number the
    # live rejection names — the rule must not be symmetric.
    assert ma_mod.figures_in("a 73-case total") == ["73"]
    assert ma_mod.figures_in("a 24-year-old accused") == ["24"]


def test_module71_the_recurring_accused_sub_answer_does_not_trip_the_detector():
    """The live shape of the false positive, end to end."""
    answer = (
        "شہزیب عرف شابی appears in fir-214-26 and fir-891-24, and عاصم رشید "
        "appears in fir-64-26 and fir-65-26 [Document 5]."
    )
    assert ma_mod._misattributed_figures(answer, _M71_ENTRIES) == []


def test_module71_the_live_rejection_number_belongs_to_exactly_one_sub_answer():
    """The root-cause claim, asserted rather than narrated: 73 is stated by
    the time-of-day sub-answer and by no other, so a 73-case total for
    seized property is a cross-document borrow by construction."""
    holders = [name for name, text in _M71_ENTRIES if "73" in ma_mod.figures_in(text)]
    assert holders == ["time"]


def test_module71_a_cross_denominator_total_is_detected():
    """The live shape verbatim: the corpus case count from [Document 4]
    attached to [Document 3]'s seized property."""
    answer = (
        "The accused are aged 24-49, mean 31.5 [Document 1]. "
        "Seized property covers a 73-case total [Document 3]. "
        "Incidents peak in the evening [Document 4]."
    )
    assert ma_mod._misattributed_figures(answer, _M71_ENTRIES) == [("73", 3)]


def test_module71_a_correctly_attributed_answer_is_not_flagged():
    answer = (
        "Accused ages run 24-49 with a mean of 31.5, from 17 of 92 [Document 1]. "
        "اجنبی dominates at 15 of 24 relationship edges [Document 2]. "
        "45 register entries across 28 FIRs: 13 to a forensic laboratory, 7 to "
        "heirs [Document 3]. "
        "64 of 73 incidents carry a datetime, peaking in the evening [Document 4]."
    )
    assert ma_mod._misattributed_figures(answer, _M71_ENTRIES) == []


def test_module71_an_outright_invention_is_left_to_the_verifier():
    """A figure NO sub-answer states is not this module's business — the
    detector is deliberately narrow, so a firing means "cross-document
    borrow" and nothing else. The verifier still owns the invention."""
    answer = "Seized property covers 999 cases [Document 3]."
    assert ma_mod._misattributed_figures(answer, _M71_ENTRIES) == []


def test_module71_a_sentence_citing_the_owning_document_too_is_not_flagged():
    """Citing both documents is a legitimate cross-reference, not a borrow."""
    answer = "64 of 73 incidents are dated [Document 4], and 45 entries were seized [Document 3][Document 4]."
    assert ma_mod._misattributed_figures(answer, _M71_ENTRIES) == []


def test_module71_the_sub_answers_section_carries_no_extra_document_marker():
    """The roster this module built and then deleted lived here. It cost G6
    a live regression (0 of 4 pre-fix rejections -> 7 of 8 with it in, every
    one the "cites no [Document N] source at all" refusal Module 29 filed),
    and bought no measured benefit, so the section is back to one line per
    sub-answer. This test is the guard: exactly one `[Document N]` marker
    per document, and nothing interleaved between the sub-answers.
    `_format_subanswers_for_prompt`'s docstring carries the measurement."""
    rendered = ma_mod._format_subanswers_for_prompt(_M71_ENTRIES)
    assert rendered.count("[Document 3]") == 1
    assert rendered.count("[Document ") == len(_M71_ENTRIES)
    assert "Figures stated by this sub-answer" not in rendered
    # One block per sub-answer, header line + text, nothing else.
    blocks = rendered.split("\n\n")
    assert len(blocks) == len(_M71_ENTRIES)
    assert blocks[2] == "[Document 3] Sub-question: property\n" + _M71_PROPERTY


def test_module71_the_synthesis_rules_scope_a_number_to_its_own_document():
    """A wording lock. The old rule 2 ("appears literally in a sub-answer
    above") is the rule the fabricated 73 SATISFIED, so its return would
    silently reopen the defect."""
    template = ma_mod._SYNTHESIS_SYSTEM_PROMPT_TEMPLATE
    assert "THE SUB-ANSWER YOU CITE FOR IT" in template
    assert "not merely somewhere above" in template
    assert "Never carry a total, a denominator or a coverage figure from one" in template
    # The derived-number half of the old rule survives, as its own rule 3.
    assert "Do not add up, average, or convert figures into percentages" in template


def test_module71_g1s_synthesis_goal_forbids_lending_a_denominator():
    plan = next(p for p in ma_mod._DECOMPOSITION_PLANS if p.name == "caseload_review")
    assert "ITS OWN figure" in plan.synthesis_goal
    assert "never give one finding another's total" in plan.synthesis_goal


@pytest.mark.asyncio
async def test_module71_a_rejected_synthesis_serves_the_sub_answers_not_an_error(monkeypatch):
    """The user-visible half. `cutover.py` turns ABSTAINED-with-no-text into
    `status=error`; five correctly computed aggregates must not be thrown
    away because the paragraph joining them over-reached."""
    _stub_decompose(
        monkeypatch,
        decompose=True,
        sub_queries=[_SUB_Q1, _SUB_Q2],
        synthesis_goal="Profile the caseload.",
    )
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_PROPERTY, tools_used=["XAGG"]),
            _SUB_Q2: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_TIME, tools_used=["XAGG"]),
        },
    )
    _stub_call_llm(monkeypatch, "Seized property covers a 73-case total [Document 1].")
    _stub_verify_grounding(
        monkeypatch,
        grounded=False,
        reason="Two claims lack explicit support in the cited chunks: the alleged data "
        "discrepancy and the 73-case total for seized property.",
    )
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    # Served: every verified sub-answer, with its own sub-question as a heading.
    assert _M71_PROPERTY in result.answer_text
    assert _M71_TIME in result.answer_text
    assert _SUB_Q1 in result.answer_text
    # NOT served: the rejected synthesis, in whole or in the offending part.
    assert "73-case total" not in result.answer_text
    assert any("shown as computed" in c for c in result.caveats)
    # Citations still line up 1:1 with the documents.
    assert [c.document_index for c in result.citations] == [1, 2]
    assert result.tools_used == ["XAGG"]


@pytest.mark.asyncio
async def test_module71_the_fallback_makes_no_second_llm_call(monkeypatch):
    """The fallback is deterministic — it composes the sub-answers, it does
    not ask a model to try again. A retry loop here would hide exactly the
    non-determinism this module exists to measure (Module 61's own posture
    on its deterministic post-pass)."""
    _stub_decompose(monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="g")
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_PROPERTY, tools_used=["XAGG"]),
            _SUB_Q2: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_TIME, tools_used=["XAGG"]),
        },
    )
    calls = []

    async def _counting_call_llm(system_prompt, user_message, **kwargs):
        calls.append(system_prompt)
        return "Seized property covers a 73-case total [Document 1]."

    monkeypatch.setattr(ma_mod, "call_llm", _counting_call_llm)
    _stub_verify_grounding(monkeypatch, grounded=False, reason="unsupported")
    _stub_validate_answer(monkeypatch)

    await meta_analysis(_agent_input())

    assert len(calls) == 1


@pytest.mark.asyncio
async def test_module71_an_off_topic_synthesis_also_falls_back_rather_than_erroring(monkeypatch):
    _stub_decompose(monkeypatch, decompose=True, sub_queries=[_SUB_Q1], synthesis_goal="g")
    _stub_supervisor_handle(
        monkeypatch,
        {_SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_PROPERTY, tools_used=["XAGG"])},
    )
    _stub_call_llm(monkeypatch, "Unrelated prose about the weather.")
    _stub_verify_grounding(monkeypatch, grounded=True, off_topic=True, reason="off topic")
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert "weather" not in result.answer_text
    assert _M71_PROPERTY in result.answer_text


@pytest.mark.asyncio
async def test_module71_nothing_changes_on_a_synthesis_the_verifier_accepts(monkeypatch):
    """The fallback must not become the normal path. A passing synthesis is
    still served verbatim, as OK, with its validation gate run."""
    _stub_decompose(monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="g")
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_PROPERTY, tools_used=["XAGG"]),
            _SUB_Q2: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_TIME, tools_used=["XAGG"]),
        },
    )
    _stub_call_llm(monkeypatch, "45 entries were seized [Document 1]; 64 of 73 are dated [Document 2].")
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert result.status == SubAgentStatus.OK
    assert result.answer_text == "45 entries were seized [Document 1]; 64 of 73 are dated [Document 2]."


# ═══════════════════════════════════════════════════════════════════════
# [Gold-QA fix — Module 83] Deterministic provenance recovery.
#
# The defect Module 71 measured: the citation rule survives only because it
# sits near the end of the synthesis prompt, so five short lines interleaved
# into the sub-answers section took G6 from 0 rejections in 4 runs to 7 of
# 8, all of them verifier.py's "cites no [Document N] source at all". These
# tests pin the MECHANISM that replaces the reliance on prompt position —
# they fail on pre-change code because `_attach_provenance` does not exist
# there, and the end-to-end one fails because the uncited answer is served
# (and would be refused live) unchanged.
# ═══════════════════════════════════════════════════════════════════════


def test_module83_an_uncited_sentence_gets_the_document_that_uniquely_states_it():
    """The whole fix in one assertion: prose with no marker at all comes back
    carrying the marker of the sub-answer that actually states its figures.
    `45`/`28` are the property sub-answer's and nobody else's; `64`/`73`
    are the time sub-answer's."""
    answer = (
        "Seized property runs to 45 register entries across 28 FIRs. "
        "64 of 73 incidents carry a usable datetime."
    )
    out, attached = ma_mod._attach_provenance(answer, _M71_ENTRIES)
    assert attached == [(1, 3), (2, 4)]
    assert out == (
        "Seized property runs to 45 register entries across 28 FIRs [Document 3]. "
        "64 of 73 incidents carry a usable datetime [Document 4]."
    )


def test_module83_a_figure_two_sub_answers_state_is_left_uncited():
    """Ambiguity is left alone rather than guessed. `73` alone appears in the
    time sub-answer here, but a sentence whose figures are supported by more
    than one document must get nothing — the point of the rule is UNIQUE
    support, which is what makes the attached marker true rather than
    plausible."""
    entries = [("a", "There are 73 cases."), ("b", "There are 73 cases and 19 stations.")]
    out, attached = ma_mod._attach_provenance("The corpus holds 73 cases.", entries)
    assert attached == []
    assert out == "The corpus holds 73 cases."


def test_module83_a_fabricated_figure_is_never_given_a_citation():
    """The guard against trading a visible failure for an invisible one
    (Module 29's and Module 25's findings). A number no sub-answer states is
    an invention; attaching a marker to it would make it LOOK grounded to
    the very gate that exists to catch it. It stays uncited, and the
    Verifier keeps its job."""
    out, attached = ma_mod._attach_provenance("There were 999 arrests.", _M71_ENTRIES)
    assert attached == []
    assert "[Document" not in out


def test_module83_a_sentence_stating_no_figure_is_left_alone():
    out, attached = ma_mod._attach_provenance(
        "The picture is consistent across the corpus.", _M71_ENTRIES
    )
    assert attached == []
    assert "[Document" not in out


def test_module83_an_answer_that_already_cites_is_never_rewritten():
    """Never renumber, move or add to a marker the model produced itself —
    that is `citation_consistency.py`'s failure mode, and this pass must not
    become a second source of it."""
    answer = "45 entries were seized [Document 1]; 64 of 73 are dated."
    out, attached = ma_mod._attach_provenance(answer, _M71_ENTRIES)
    assert attached == []
    assert out == answer


def test_module83_urdu_digits_and_the_urdu_full_stop_are_handled():
    """A synthesis answered in Urdu renders its counts in Extended
    Arabic-Indic digits and ends its sentences with `۔`. If either is
    missed the recovery silently does nothing on exactly the questions this
    system is for."""
    entries = [("age", "کل ۷۳ مقدمات ہیں۔"), ("other", "انیس تھانے۔")]
    out, attached = ma_mod._attach_provenance("مجموعی طور پر ۷۳ مقدمات ہیں۔", entries)
    assert attached == [(1, 1)]
    assert out == "مجموعی طور پر ۷۳ مقدمات ہیں [Document 1]۔"


def test_module83_multi_line_layout_survives_the_rewrite():
    """The sentence splitter also splits on newlines, so a naive re-join
    would flatten a bulleted synthesis into one paragraph. The served text
    must differ from the model's only by the markers."""
    answer = "- 45 entries across 28 FIRs\n- 64 of 73 incidents are dated\n- nothing else"
    out, _attached = ma_mod._attach_provenance(answer, _M71_ENTRIES)
    assert out == (
        "- 45 entries across 28 FIRs [Document 3]\n"
        "- 64 of 73 incidents are dated [Document 4]\n"
        "- nothing else"
    )


@pytest.mark.asyncio
async def test_module83_an_uncited_synthesis_reaches_the_verifier_with_provenance(monkeypatch):
    """End to end, and the direction that matters: the text handed to
    `verify_grounding` — and then served — carries provenance, so
    `verifier.py::_check_no_citation`'s "substantial but cites no [Document
    N] source at all" refusal no longer depends on where the citation rule
    happens to sit in the prompt."""
    seen: dict = {}

    async def _fake_verify(**kwargs):
        seen["answer"] = kwargs["answer"]
        return {"grounded": True, "off_topic": False, "reason": ""}

    _stub_decompose(monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="g")
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_PROPERTY, tools_used=["XAGG"]),
            _SUB_Q2: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_TIME, tools_used=["XAGG"]),
        },
    )
    _stub_call_llm(
        monkeypatch,
        "Seized property runs to 45 register entries across 28 FIRs. "
        "64 of 73 incidents carry a usable datetime.",
    )
    monkeypatch.setattr(ma_mod, "verify_grounding", _fake_verify)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert "[Document 1]" in seen["answer"]
    assert "[Document 2]" in seen["answer"]
    assert result.status == SubAgentStatus.OK
    assert result.answer_text == seen["answer"]


@pytest.mark.asyncio
async def test_module83_an_unrecoverable_uncited_synthesis_is_still_refused(monkeypatch):
    """The refusal is NOT relaxed. When nothing can be attributed with
    unique support, the answer reaches the Verifier exactly as the model
    wrote it — uncited — and a rejection still falls back to Module 71's
    sub-answer composition rather than serving ungrounded prose."""
    seen: dict = {}

    async def _fake_verify(**kwargs):
        seen["answer"] = kwargs["answer"]
        return {
            "grounded": False,
            "off_topic": True,
            "reason": "Answer is substantial (long, or a multi-item list) but cites no [Document N] source at all",
        }

    _stub_decompose(monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="g")
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_PROPERTY, tools_used=["XAGG"]),
            _SUB_Q2: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_TIME, tools_used=["XAGG"]),
        },
    )
    _stub_call_llm(monkeypatch, "The overall picture is broadly consistent across the corpus.")
    monkeypatch.setattr(ma_mod, "verify_grounding", _fake_verify)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert "[Document" not in seen["answer"]
    assert result.status == SubAgentStatus.PARTIAL
    assert "broadly consistent" not in (result.answer_text or "")


# ═══════════════════════════════════════════════════════════════════════
# [Gold-QA fix — Module 83] The synthesis collapse.
#
# Pinned to the LIVE capture, not an invented shape: 20 G6 runs on this
# branch logged the synthesis before the Verifier saw it, and every refused
# one was the same degenerate repetition loop — 758 tokens, 18 unique
# (ratio 0.024), one 4-gram repeated 370 times, byte-identical across six
# runs because `call_llm` decodes at `temperature=0.0`. The passing runs
# measured 0.57-0.69 with a top 4-gram count of 2.
# ═══════════════════════════════════════════════════════════════════════

# The live loop, reproduced at its real shape (the phrase and the ratio are
# the captured ones; the length is trimmed to keep the test readable).
_M83_COLLAPSE = (
    "Naye tainaat hone wale afsar ko mojooda case load se yeh tawaqqo rakhna chahiye ke "
    + "kismat-e-murad ki " * 200
)
_M83_HEALTHY = (
    "Naye tainaat hone wale afsar ko yeh pata hona chahiye ke 19 case(s) Faisalabad, "
    "18 case(s) Lahore aur 10 case(s) Rawalpindi mein daal di gayi hain. Case mix main "
    "ab 2026 mein 51 FIRs hain, jo 2024 mein 13 FIRs se ziyada hain, aur legal acts "
    "jaise PPC section 34 bhi shamil hain. Arrest ki soorat main 11 FIRs mein arrest "
    "record ki gayi hai, jabke 62 FIRs mein koi arrest record nahi hai. Reporting delay "
    "2026 mein barh kar 23.4 hours tak pahunch gayi hai. Weapon licensing ki taraf se "
    "30 weapons ka licence record nahi hai."
)


def test_module83_the_live_collapse_is_detected_and_a_healthy_answer_is_not():
    """Both directions on the captured data. A detector that fires on the
    healthy answer would send every G6 run into the fallback."""
    assert ma_mod._is_degenerate(_M83_COLLAPSE)
    assert not ma_mod._is_degenerate(_M83_HEALTHY)

    _tokens, ratio, repeats = ma_mod._repetition_profile(_M83_COLLAPSE)
    assert ratio < 0.05 and repeats > 100
    _tokens, ratio, repeats = ma_mod._repetition_profile(_M83_HEALTHY)
    assert ratio > 0.5 and repeats < 5


def test_module83_a_long_legitimate_list_is_not_degenerate():
    """The threshold's real risk. A multi-item listing repeats its
    STRUCTURE, not its words — and `verifier.py`'s own `_LIST_ITEM_RE`
    comment is there because these answers are common. 19 stations, one line
    each, is a shape this system produces constantly."""
    listing = "\n".join(
        f"- Station {n}: {n * 3} FIRs recorded, {n} with an arrest" for n in range(4, 40)
    )
    assert not ma_mod._is_degenerate(listing)


def test_module83_a_short_answer_is_never_called_degenerate():
    """Below `_DEGENERATE_MIN_TOKENS` the ratio is meaningless — a two-line
    "no information found" is not a collapse."""
    assert not ma_mod._is_degenerate("Koi maloomat nahi mili. " * 5)
    assert not ma_mod._is_degenerate("")


@pytest.mark.asyncio
async def test_module83_a_collapsed_synthesis_is_regenerated_once(monkeypatch):
    """The recovery. `temperature=0.0` makes the loop deterministic, so the
    regeneration must decode differently — the assertion on `temperature` is
    the whole point of the retry, not decoration."""
    calls: list[dict] = []

    async def _fake_call_llm(system_prompt, user_message, **kwargs):
        calls.append(kwargs)
        return _M83_COLLAPSE if len(calls) == 1 else _M83_HEALTHY

    _stub_decompose(monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="g")
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_PROPERTY, tools_used=["XAGG"]),
            _SUB_Q2: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_TIME, tools_used=["XAGG"]),
        },
    )
    monkeypatch.setattr(ma_mod, "call_llm", _fake_call_llm)
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert len(calls) == 2
    assert calls[0].get("temperature") in (None, 0.0)
    assert calls[1]["temperature"] == ma_mod._DEGENERATE_RETRY_TEMPERATURE
    assert result.status == SubAgentStatus.OK
    assert "kismat-e-murad" not in (result.answer_text or "")


@pytest.mark.asyncio
async def test_module83_a_twice_collapsed_synthesis_is_never_served_or_verified(monkeypatch):
    """Exactly two generations, no Verifier call at all, and not one
    character of the loop in the served answer. Spending an LLM grounding
    call on 6,764 characters of one repeated phrase buys latency and quota
    and nothing else."""
    calls: list[dict] = []
    verified: list = []

    async def _fake_call_llm(system_prompt, user_message, **kwargs):
        calls.append(kwargs)
        return _M83_COLLAPSE

    async def _fake_verify(**kwargs):
        verified.append(kwargs)
        return {"grounded": True, "off_topic": False, "reason": ""}

    _stub_decompose(monkeypatch, decompose=True, sub_queries=[_SUB_Q1, _SUB_Q2], synthesis_goal="g")
    _stub_supervisor_handle(
        monkeypatch,
        {
            _SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_PROPERTY, tools_used=["XAGG"]),
            _SUB_Q2: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_TIME, tools_used=["XAGG"]),
        },
    )
    monkeypatch.setattr(ma_mod, "call_llm", _fake_call_llm)
    monkeypatch.setattr(ma_mod, "verify_grounding", _fake_verify)
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert len(calls) == 2, "one regeneration, never a loop"
    assert verified == [], "a collapsed synthesis must not cost a grounding call"
    assert result.status == SubAgentStatus.PARTIAL
    assert "kismat-e-murad" not in (result.answer_text or "")
    assert _M71_PROPERTY in result.answer_text and _M71_TIME in result.answer_text
    assert any("did not generate cleanly" in c for c in result.caveats)


@pytest.mark.asyncio
async def test_module83_a_healthy_synthesis_is_never_regenerated(monkeypatch):
    """The guard on the common path. Module 71 pinned "no retry loop" closed
    for a reason — this retry fires ONLY on a deterministically detected
    collapse, never on a verifier rejection, and never on a good answer."""
    calls: list[dict] = []

    async def _fake_call_llm(system_prompt, user_message, **kwargs):
        calls.append(kwargs)
        return _M83_HEALTHY

    _stub_decompose(monkeypatch, decompose=True, sub_queries=[_SUB_Q1], synthesis_goal="g")
    _stub_supervisor_handle(
        monkeypatch,
        {_SUB_Q1: SubAgentResult(status=SubAgentStatus.OK, answer_text=_M71_TIME, tools_used=["XAGG"])},
    )
    monkeypatch.setattr(ma_mod, "call_llm", _fake_call_llm)
    _stub_verify_grounding(monkeypatch, grounded=False, reason="unsupported")
    _stub_validate_answer(monkeypatch)

    result = await meta_analysis(_agent_input())

    assert len(calls) == 1
    assert result.status == SubAgentStatus.PARTIAL
    assert any("could not be verified as grounded" in c for c in result.caveats)
