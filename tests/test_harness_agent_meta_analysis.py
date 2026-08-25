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
  (i) module-level self-registration into the Supervisor's registry.

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
# (i) registration
# ═══════════════════════════════════════════════════════════════════════


def test_meta_analysis_is_registered():
    assert get_registered(META_ANALYSIS) is meta_analysis
    assert meta_analysis.name == META_ANALYSIS
