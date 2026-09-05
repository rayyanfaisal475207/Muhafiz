"""
Tests for src/pipeline/harness/agents/cross_case_linkage.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 5, this session's "Phase 6").

Covers:
  (a) both tools contribute -> status=OK, tools_used=["XGRAPH","XNETWORK"];
  (b) one tool EMPTY, the other contributes -> status=PARTIAL, tools_used/
      degraded_from split accordingly (both directions);
  (c) both a definite "no connections" result -> status=EMPTY, presented as
      a real finding (not softened, not an error);
  (d) both DENIED -> status=DENIED, never collapsed into ABSTAINED/EMPTY
      (RESOLVED-6);
  (e) both FAILED -> status=ABSTAINED;
  (f) unconfirmed_links -> CrossCaseLink(is_unconfirmed=True) + a matching
      caveats entry, specifically, per this session's brief's single
      highest-consequence correctness requirement;
  (g) the sixth, brief-uncovered combination (one FAILED, other a definite
      empty) -> PARTIAL with degraded_from=["XGRAPH","XNETWORK"],
      tools_used=[] (module docstring's dedicated note);
  (h) XNETWORK's verify -> one-shot cloud-retry -> raw-fallback sequence
      (this module's own implementation, resolved via AskUserQuestion --
      see module docstring);
  (i) module-level self-registration into the Supervisor's registry, and a
      Supervisor.handle() -> Cross-Case Linkage -> real XGRAPH/XNETWORK tool
      integration path.

`xgraph_tool`, `xnetwork_tool`, `call_llm`, and `verify_grounding` are
monkeypatched at the module level (`ccl_mod.*`) in every test -- none of
these hit live infra, per this session's scope (test/mock data only).
"""
from __future__ import annotations

import pytest

import src.pipeline.harness.agents.cross_case_linkage as ccl_mod
from src.pipeline.harness.agents.cross_case_linkage import cross_case_linkage
from src.pipeline.harness.supervisor import (
    CROSS_CASE_LINKAGE,
    Supervisor,
    get_registered,
)
from src.pipeline.harness.tools.xgraph import XGraphToolInput, XGraphToolResult
from src.pipeline.harness.tools.xnetwork import XNetworkToolResult
from src.pipeline.harness.types import (
    CallerContext,
    ChunkMetadata,
    EvidenceChunk,
    ExecutionContext,
    Role,
    SubAgentInput,
    SubAgentStatus,
    ToolError,
    ToolStatus,
)


def _xgraph_chunk(case_id="CASE-002", text="Vehicle ABC-123 mentioned in FIR."):
    return EvidenceChunk(
        id="xg-1",
        text=text,
        metadata=ChunkMetadata(source_tool="XGRAPH", case_id=case_id, source_file="doc.pdf"),
    )


def _xnetwork_chunk(community_id="community-1", text="These cases share a burglary-at-night pattern."):
    return EvidenceChunk(
        id=f"community-{community_id}",
        text=text,
        metadata=ChunkMetadata(source_tool="XNETWORK", source_file=community_id),
    )


def _caller(role=Role.SUPERVISOR, **kw):
    return CallerContext(user_id="u1", role=role, active_case_id=None, **kw)


def _execution(caller=None):
    return ExecutionContext(caller=caller or _caller())


def _agent_input(caller=None, query_text="has this vehicle appeared in other cases", **kw):
    return SubAgentInput(query_text=query_text, execution=_execution(caller=caller), **kw)


def _stub_xgraph_tool(monkeypatch, result: XGraphToolResult):
    async def _fake(tool_input):
        return result

    monkeypatch.setattr(ccl_mod, "xgraph_tool", _fake)


def _stub_xnetwork_tool(monkeypatch, result: XNetworkToolResult):
    async def _fake(tool_input):
        return result

    monkeypatch.setattr(ccl_mod, "xnetwork_tool", _fake)


def _stub_call_llm(monkeypatch, answer=None, exc=None, sequence=None):
    """
    `sequence`, if given, is a list of (answer_or_None, exc_or_None) pairs
    consumed in call order -- lets a test drive the local-call ->
    cloud-retry-call two-step sequence deterministically.
    """
    calls = {"n": 0}

    async def _fake(system_prompt, user_message, **kwargs):
        if sequence is not None:
            idx = calls["n"]
            calls["n"] += 1
            step_answer, step_exc = sequence[idx]
            if step_exc is not None:
                raise step_exc
            return step_answer
        if exc is not None:
            raise exc
        return answer

    monkeypatch.setattr(ccl_mod, "call_llm", _fake)
    return calls


def _stub_validate_answer(monkeypatch, status=None, claims=None):
    """
    Stubs the Validation gate at the boundary this module actually calls
    (`ccl_mod.validate_answer`) -- same discipline as
    `test_harness_agent_investigative_analysis.py`'s own helper of the same
    name. Defaults to PASSED/[] so tests not specifically about Validation's
    caveat-appending behavior see no change from before this gate was wired.
    """
    from src.pipeline.harness.types import ValidationStatus

    resolved_status = status if status is not None else ValidationStatus.PASSED
    resolved_claims = claims if claims is not None else []

    async def _fake(*args, **kwargs):
        return resolved_status, resolved_claims

    monkeypatch.setattr(ccl_mod, "validate_answer", _fake)


def _stub_verify_grounding(monkeypatch, sequence):
    """`sequence`: list of dicts (grounded/off_topic/reason), consumed in call order."""
    calls = {"n": 0}

    async def _fake(answer, cited_chunks, case_id, cross_case_ids=None, target_date=None):
        idx = min(calls["n"], len(sequence) - 1)
        calls["n"] += 1
        verdict = sequence[idx]
        return {
            "grounded": verdict["grounded"],
            "off_topic": verdict.get("off_topic", False),
            "leaked_case_id": None,
            "unsupported_claims": [],
            "reason": verdict.get("reason", ""),
        }

    monkeypatch.setattr(ccl_mod, "verify_grounding", _fake)
    return calls


# ═══════════════════════════════════════════════════════════════════════
# [Reconciliation fix — harness-reconciliation Unit 7] target_entity
# threading + statistical-NER recovery fallback
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_explicit_target_entity_threaded_to_xgraph(monkeypatch):
    """An explicit value on the input always wins over recovery."""
    calls = []

    async def _fake_xgraph(tool_input):
        calls.append(tool_input)
        return XGraphToolResult(status=ToolStatus.EMPTY)

    monkeypatch.setattr(ccl_mod, "xgraph_tool", _fake_xgraph)
    _stub_xnetwork_tool(monkeypatch, XNetworkToolResult(status=ToolStatus.EMPTY))

    await cross_case_linkage(_agent_input(target_entity="ABC-123"))

    assert len(calls) == 1
    assert calls[0].target_entity == "ABC-123"


@pytest.mark.asyncio
async def test_target_entity_recovered_from_query_when_router_supplied_none(monkeypatch):
    """[Reconciliation fix — Unit 7] router.py's deterministic XGRAPH
    override always reports target_entity=None -- on exactly the queries
    where an entity matters most ("what other cases is X involved in?"),
    routing short-circuits before the LLM extraction that would have found
    it. Regression guard for the statistical-NER recovery fallback."""
    calls = []

    async def _fake_xgraph(tool_input):
        calls.append(tool_input)
        return XGraphToolResult(status=ToolStatus.EMPTY)

    monkeypatch.setattr(ccl_mod, "xgraph_tool", _fake_xgraph)
    _stub_xnetwork_tool(monkeypatch, XNetworkToolResult(status=ToolStatus.EMPTY))

    await cross_case_linkage(
        _agent_input(query_text="what other cases is Ahmed Khan involved in?")
    )

    assert len(calls) == 1
    # A real person name should be recovered by the statistical NER pass --
    # exact casing/format depends on that pass, so assert non-None rather
    # than a specific string.
    assert calls[0].target_entity is not None


# ═══════════════════════════════════════════════════════════════════════
# (a) both tools contribute -> OK
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_both_contribute_returns_ok(monkeypatch):
    xg_chunk = _xgraph_chunk()
    _stub_xgraph_tool(
        monkeypatch,
        XGraphToolResult(
            status=ToolStatus.OK,
            chunks=[xg_chunk],
            case_ids_touched=["CASE-002"],
            hop_count=1,
            chain_confidence=0.9,
        ),
    )
    xn_chunk = _xnetwork_chunk()
    _stub_xnetwork_tool(
        monkeypatch,
        XNetworkToolResult(
            status=ToolStatus.OK,
            chunks=[xn_chunk],
            case_ids_touched=["CASE-002", "CASE-005"],
            community_ids=["community-1"],
            raw_summary_text="(community-1) These cases share a burglary-at-night pattern.",
        ),
    )
    _stub_call_llm(monkeypatch, "These cases share a pattern [Document 1].")
    _stub_validate_answer(monkeypatch)
    _stub_verify_grounding(monkeypatch, [{"grounded": True}])

    result = await cross_case_linkage(_agent_input())

    assert result.status == SubAgentStatus.OK
    assert result.tools_used == ["XGRAPH", "XNETWORK"]
    assert result.degraded_from == []
    assert result.answer_text
    # One deterministic XGRAPH link + one XNETWORK per-community link.
    sources = {link.source_tool for link in result.links}
    assert sources == {"XGRAPH", "XNETWORK"}
    xgraph_links = [l for l in result.links if l.source_tool == "XGRAPH"]
    assert len(xgraph_links) == 1
    assert xgraph_links[0].is_unconfirmed is False
    assert "CASE-002" in xgraph_links[0].description
    # XNETWORK's contribution ran through the Verifier and carries citations.
    assert len(result.citations) == 1
    assert result.citations[0].source_tool == "XNETWORK"
    # Bounded payload -- no raw chunks ever cross the boundary.
    for link in result.links:
        assert not hasattr(link, "chunks")


@pytest.mark.asyncio
async def test_validation_gate_runs_full_tier_against_xnetwork_chunks_only(monkeypatch):
    from src.pipeline.harness.types import ClaimSupport, ValidationClaimResult, ValidationStatus

    xg_chunk = _xgraph_chunk()
    _stub_xgraph_tool(
        monkeypatch,
        XGraphToolResult(
            status=ToolStatus.OK, chunks=[xg_chunk], case_ids_touched=["CASE-002"],
            hop_count=1, chain_confidence=0.9,
        ),
    )
    xn_chunk = _xnetwork_chunk()
    _stub_xnetwork_tool(
        monkeypatch,
        XNetworkToolResult(
            status=ToolStatus.OK, chunks=[xn_chunk], case_ids_touched=["CASE-002", "CASE-005"],
            community_ids=["community-1"], raw_summary_text="raw text",
        ),
    )
    _stub_call_llm(monkeypatch, "These cases share a pattern [Document 1].")
    _stub_verify_grounding(monkeypatch, [{"grounded": True}])

    flagged = ValidationClaimResult(
        document_index=1, claim_excerpt="pattern claim",
        support=ClaimSupport.PARTIALLY_SUPPORTED, reason="Overstates the connection.",
    )
    captured = {}

    async def _fake_validate(answer_text, cited_chunks, *, tier):
        captured["tier"] = tier
        captured["chunk_ids"] = [c["id"] for c in cited_chunks]
        return ValidationStatus.ISSUES_FOUND, [flagged]

    monkeypatch.setattr(ccl_mod, "validate_answer", _fake_validate)

    result = await cross_case_linkage(_agent_input())

    # [PRESERVE -- plan §4 row 5] MANDATORY full tier, highest-stakes sub-agent.
    assert captured["tier"] == "full"
    # Only XNETWORK's own chunks are validated -- XGRAPH's deterministic
    # summary line carries no [Document N] markers of its own.
    assert captured["chunk_ids"] == [xn_chunk.id]
    assert result.status == SubAgentStatus.OK  # caveat-only, never blocking
    assert result.validation_status == ValidationStatus.ISSUES_FOUND
    assert result.validation_claims == [flagged]
    assert any("Overstates the connection" in c for c in result.caveats)


# ═══════════════════════════════════════════════════════════════════════
# (b) one EMPTY, other contributes -> PARTIAL, both directions
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_xnetwork_empty_xgraph_contributes_returns_partial(monkeypatch):
    xg_chunk = _xgraph_chunk()
    _stub_xgraph_tool(
        monkeypatch,
        XGraphToolResult(
            status=ToolStatus.OK,
            chunks=[xg_chunk],
            case_ids_touched=["CASE-002"],
            hop_count=1,
            chain_confidence=0.8,
        ),
    )
    _stub_xnetwork_tool(monkeypatch, XNetworkToolResult(status=ToolStatus.EMPTY, chunks=[]))

    result = await cross_case_linkage(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert result.tools_used == ["XGRAPH"]
    assert result.degraded_from == ["XNETWORK"]
    assert any(link.source_tool == "XGRAPH" for link in result.links)
    assert not any(link.source_tool == "XNETWORK" for link in result.links)


@pytest.mark.asyncio
async def test_xgraph_empty_xnetwork_contributes_returns_partial(monkeypatch):
    _stub_xgraph_tool(monkeypatch, XGraphToolResult(status=ToolStatus.EMPTY, chunks=[]))
    xn_chunk = _xnetwork_chunk()
    _stub_xnetwork_tool(
        monkeypatch,
        XNetworkToolResult(
            status=ToolStatus.OK,
            chunks=[xn_chunk],
            case_ids_touched=["CASE-005"],
            community_ids=["community-1"],
            raw_summary_text="(community-1) pattern text",
        ),
    )
    _stub_call_llm(monkeypatch, "Pattern summary [Document 1].")
    _stub_validate_answer(monkeypatch)
    _stub_verify_grounding(monkeypatch, [{"grounded": True}])

    result = await cross_case_linkage(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert result.tools_used == ["XNETWORK"]
    assert result.degraded_from == ["XGRAPH"]
    assert any(link.source_tool == "XNETWORK" for link in result.links)
    assert not any(link.source_tool == "XGRAPH" for link in result.links)


# ═══════════════════════════════════════════════════════════════════════
# (c) both a definite "no connections" result -> EMPTY, a real finding
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_both_definite_empty_returns_empty_as_real_finding(monkeypatch):
    _stub_xgraph_tool(
        monkeypatch, XGraphToolResult(status=ToolStatus.EMPTY, chunks=[], unconfirmed_links=[])
    )
    _stub_xnetwork_tool(monkeypatch, XNetworkToolResult(status=ToolStatus.EMPTY, chunks=[]))

    result = await cross_case_linkage(_agent_input())

    assert result.status == SubAgentStatus.EMPTY
    # [PRESERVE] Presented as a real finding, not softened -- answer_text
    # is populated, not None, and no error is attached.
    assert result.answer_text
    assert result.error is None
    assert result.links == []


# ═══════════════════════════════════════════════════════════════════════
# (d) both DENIED -> DENIED, never collapsed (RESOLVED-6)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_both_denied_propagates_as_its_own_status(monkeypatch):
    err = ToolError(kind="permission_denied", message="Cross-case queries require supervisor role or higher.")
    _stub_xgraph_tool(monkeypatch, XGraphToolResult(status=ToolStatus.DENIED, error=err))
    _stub_xnetwork_tool(monkeypatch, XNetworkToolResult(status=ToolStatus.DENIED, error=err))

    result = await cross_case_linkage(_agent_input(caller=_caller(role=Role.INVESTIGATOR)))

    assert result.status == SubAgentStatus.DENIED
    assert result.status != SubAgentStatus.ABSTAINED
    assert result.status != SubAgentStatus.EMPTY
    assert result.answer_text is None
    assert result.error is err
    assert result.caveats


# ═══════════════════════════════════════════════════════════════════════
# (e) both FAILED -> ABSTAINED
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_both_failed_abstains(monkeypatch):
    err = ToolError(kind="upstream_failure", message="AGE/postgres unreachable")
    _stub_xgraph_tool(monkeypatch, XGraphToolResult(status=ToolStatus.FAILED, error=err))
    _stub_xnetwork_tool(monkeypatch, XNetworkToolResult(status=ToolStatus.FAILED, error=err))

    result = await cross_case_linkage(_agent_input())

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.error is err


# ═══════════════════════════════════════════════════════════════════════
# (f) unconfirmed_links -> CrossCaseLink(is_unconfirmed=True) + caveats
#     (the single highest-consequence correctness requirement)
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_unconfirmed_links_become_caveated_crosscaselinks(monkeypatch):
    unconfirmed = [
        {"entity": "Ali Khan", "candidate": "Ali Ahmed Khan", "tier": "high", "confidence": 0.72, "status": "pending"},
        {"entity": "0300-1234567", "candidate": "0300-1234568", "tier": "medium", "confidence": 0.5, "status": "pending"},
    ]
    _stub_xgraph_tool(
        monkeypatch,
        XGraphToolResult(status=ToolStatus.EMPTY, chunks=[], unconfirmed_links=unconfirmed),
    )
    _stub_xnetwork_tool(monkeypatch, XNetworkToolResult(status=ToolStatus.EMPTY, chunks=[]))

    result = await cross_case_linkage(_agent_input())

    # XGRAPH is EMPTY on chunks but carries unconfirmed_links -> contributes,
    # not the "both definite empty" branch (XNETWORK is definite-empty, so
    # this is the one-contributes PARTIAL branch).
    assert result.status == SubAgentStatus.PARTIAL
    assert result.tools_used == ["XGRAPH"]

    unconfirmed_result_links = [l for l in result.links if l.is_unconfirmed]
    assert len(unconfirmed_result_links) == 2
    for link in unconfirmed_result_links:
        assert link.source_tool == "XGRAPH"
        # [PRESERVE] Never asserted as confirmed fact.
        assert link.is_unconfirmed is True

    # Every unconfirmed link contributes a MATCHING caveats entry.
    assert len(result.caveats) >= 2
    assert any("Ali Khan" in c and "Ali Ahmed Khan" in c for c in result.caveats)
    assert any("0300-1234567" in c and "0300-1234568" in c for c in result.caveats)
    # No confirmed XGRAPH connection link exists (chunks were empty).
    assert not any(l.source_tool == "XGRAPH" and not l.is_unconfirmed for l in result.links)


@pytest.mark.asyncio
async def test_unconfirmed_links_alongside_confirmed_connection(monkeypatch):
    unconfirmed = [{"entity": "A", "candidate": "B", "tier": "low", "confidence": 0.3, "status": "pending"}]
    _stub_xgraph_tool(
        monkeypatch,
        XGraphToolResult(
            status=ToolStatus.OK,
            chunks=[_xgraph_chunk()],
            case_ids_touched=["CASE-002"],
            hop_count=1,
            chain_confidence=0.95,
            unconfirmed_links=unconfirmed,
        ),
    )
    _stub_xnetwork_tool(monkeypatch, XNetworkToolResult(status=ToolStatus.EMPTY, chunks=[]))

    result = await cross_case_linkage(_agent_input())

    confirmed = [l for l in result.links if l.source_tool == "XGRAPH" and not l.is_unconfirmed]
    unconfirmed_out = [l for l in result.links if l.source_tool == "XGRAPH" and l.is_unconfirmed]
    assert len(confirmed) == 1
    assert len(unconfirmed_out) == 1
    assert any("A" in c and "B" in c for c in result.caveats)


# ═══════════════════════════════════════════════════════════════════════
# (g) sixth combination -- one FAILED, other a definite empty
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_xgraph_failed_xnetwork_definite_empty_returns_partial_no_tools_used(monkeypatch):
    err = ToolError(kind="upstream_failure", message="AGE unreachable")
    _stub_xgraph_tool(monkeypatch, XGraphToolResult(status=ToolStatus.FAILED, error=err))
    _stub_xnetwork_tool(monkeypatch, XNetworkToolResult(status=ToolStatus.EMPTY, chunks=[]))

    result = await cross_case_linkage(_agent_input())

    assert result.status == SubAgentStatus.PARTIAL
    assert result.tools_used == []
    assert set(result.degraded_from) == {"XGRAPH", "XNETWORK"}
    assert result.answer_text == "No confirmed cross-case connections were found."
    assert result.caveats


# ═══════════════════════════════════════════════════════════════════════
# (h) XNETWORK verify -> one-shot cloud-retry -> raw-fallback
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_xnetwork_verifier_rejection_triggers_one_shot_cloud_retry_then_passes(monkeypatch):
    _stub_xgraph_tool(monkeypatch, XGraphToolResult(status=ToolStatus.EMPTY, chunks=[]))
    xn_chunk = _xnetwork_chunk()
    _stub_xnetwork_tool(
        monkeypatch,
        XNetworkToolResult(
            status=ToolStatus.OK,
            chunks=[xn_chunk],
            case_ids_touched=["CASE-005"],
            community_ids=["community-1"],
            raw_summary_text="(community-1) pattern text",
        ),
    )
    calls = _stub_call_llm(
        monkeypatch,
        sequence=[
            ("local paraphrase [Document 1].", None),
            ("cloud paraphrase [Document 1].", None),
        ],
    )
    _stub_validate_answer(monkeypatch)
    _stub_verify_grounding(monkeypatch, [{"grounded": False, "reason": "local rejected"}, {"grounded": True}])

    result = await cross_case_linkage(_agent_input())

    assert calls["n"] == 2  # local attempt, then exactly one cloud retry
    assert result.answer_text == "cloud paraphrase [Document 1]."
    # XGRAPH is EMPTY here (test isolates XNETWORK's own retry sequence) ->
    # overall PARTIAL, degraded_from=["XGRAPH"] -- XNETWORK itself contributed.
    assert result.status == SubAgentStatus.PARTIAL
    assert result.tools_used == ["XNETWORK"]
    assert result.degraded_from == ["XGRAPH"]
    assert not any("raw community-cluster" in c for c in result.caveats)


@pytest.mark.asyncio
async def test_xnetwork_cloud_retry_also_rejected_falls_back_to_raw_summary(monkeypatch):
    _stub_xgraph_tool(monkeypatch, XGraphToolResult(status=ToolStatus.EMPTY, chunks=[]))
    xn_chunk = _xnetwork_chunk()
    _stub_xnetwork_tool(
        monkeypatch,
        XNetworkToolResult(
            status=ToolStatus.OK,
            chunks=[xn_chunk],
            case_ids_touched=["CASE-005"],
            community_ids=["community-1"],
            raw_summary_text="(community-1) raw pattern text",
        ),
    )
    calls = _stub_call_llm(
        monkeypatch,
        sequence=[
            ("local paraphrase [Document 1].", None),
            ("cloud paraphrase [Document 1].", None),
        ],
    )
    _stub_validate_answer(monkeypatch)
    _stub_verify_grounding(monkeypatch, [{"grounded": False}, {"grounded": False}])

    result = await cross_case_linkage(_agent_input())

    assert calls["n"] == 2
    assert result.answer_text == "(community-1) raw pattern text"
    assert result.status == SubAgentStatus.PARTIAL
    assert result.tools_used == ["XNETWORK"]
    assert any("raw community-cluster" in c for c in result.caveats)


@pytest.mark.asyncio
async def test_xnetwork_cloud_retry_raising_falls_back_to_raw_summary(monkeypatch):
    """E.g. AIR_GAP_MODE refuses the forced-cloud call -- call_llm raises."""
    _stub_xgraph_tool(monkeypatch, XGraphToolResult(status=ToolStatus.EMPTY, chunks=[]))
    xn_chunk = _xnetwork_chunk()
    _stub_xnetwork_tool(
        monkeypatch,
        XNetworkToolResult(
            status=ToolStatus.OK,
            chunks=[xn_chunk],
            case_ids_touched=["CASE-005"],
            community_ids=["community-1"],
            raw_summary_text="(community-1) raw pattern text",
        ),
    )
    _stub_call_llm(
        monkeypatch,
        sequence=[
            ("local paraphrase [Document 1].", None),
            (None, RuntimeError("AIR_GAP_MODE is active — refusing cloud call")),
        ],
    )
    _stub_validate_answer(monkeypatch)
    _stub_verify_grounding(monkeypatch, [{"grounded": False}])

    result = await cross_case_linkage(_agent_input())

    assert result.answer_text == "(community-1) raw pattern text"
    assert result.status == SubAgentStatus.PARTIAL
    assert result.tools_used == ["XNETWORK"]


@pytest.mark.asyncio
async def test_xnetwork_generation_exception_on_first_call_is_treated_as_degraded(monkeypatch):
    _stub_xgraph_tool(
        monkeypatch,
        XGraphToolResult(
            status=ToolStatus.OK,
            chunks=[_xgraph_chunk()],
            case_ids_touched=["CASE-002"],
            hop_count=1,
            chain_confidence=0.9,
        ),
    )
    xn_chunk = _xnetwork_chunk()
    _stub_xnetwork_tool(
        monkeypatch,
        XNetworkToolResult(
            status=ToolStatus.OK,
            chunks=[xn_chunk],
            case_ids_touched=["CASE-005"],
            community_ids=["community-1"],
            raw_summary_text="(community-1) pattern text",
        ),
    )
    _stub_call_llm(monkeypatch, exc=RuntimeError("llm service unreachable"))
    _stub_validate_answer(monkeypatch)

    result = await cross_case_linkage(_agent_input())

    # XGRAPH still contributes -> PARTIAL, not ABSTAINED; XNETWORK's tool
    # succeeded but this sub-agent's own generation step could not run.
    assert result.status == SubAgentStatus.PARTIAL
    assert result.tools_used == ["XGRAPH"]
    assert "XNETWORK" in result.degraded_from
    assert not any(l.source_tool == "XNETWORK" for l in result.links)


# ═══════════════════════════════════════════════════════════════════════
# (i) Supervisor integration -- real registration, real dispatch chain
# ═══════════════════════════════════════════════════════════════════════

def test_cross_case_linkage_is_registered_under_its_own_name():
    assert get_registered(CROSS_CASE_LINKAGE) is cross_case_linkage


@pytest.mark.asyncio
async def test_supervisor_dispatches_to_real_cross_case_linkage_and_real_tools(monkeypatch):
    """
    Supervisor.handle() -> real Cross-Case Linkage -> real xgraph_tool()/
    xnetwork_tool(), with router.route_query() and the underlying
    retrieve_graph()/run_network_query() stubbed to deterministic test data
    (not live infra). Proves the Supervisor and this sub-agent actually
    connect end to end, and confirms XGRAPH/XNETWORK classification both
    route here (SUBAGENT_INTERFACES.md §1.4/§1.6, supervisor.py's
    _ROUTE_TO_SUBAGENT).
    """
    import src.pipeline.harness.supervisor as supervisor_mod
    import src.pipeline.harness.tools.xgraph as xgraph_tool_mod
    import src.pipeline.harness.tools.xnetwork as xnetwork_tool_mod

    async def _fake_route_query(query_text: str) -> dict:
        return {"route": "XGRAPH", "case_scope": "cross_case", "output_format": "chat"}

    monkeypatch.setattr(supervisor_mod, "route_query", _fake_route_query)

    async def _fake_retrieve_graph(query_text, target_entity, case_id=None, cross_case=False, max_hops=2, user_id=None, user_role="investigator"):
        return {
            "chunks": [],
            "hop_count": 0,
            "compounded_confidence": 1.0,
            "seed_entities": [],
            "unconfirmed_links": [],
        }

    monkeypatch.setattr(xgraph_tool_mod, "retrieve_graph", _fake_retrieve_graph)

    async def _fake_get_gateway():
        class _FakeGateway:
            pass

        return _FakeGateway()

    async def _fake_run_network_query(query_text, gateway, user_id=None, user_role="investigator", top_k=5):
        return {"results": [], "case_ids": [], "community_ids": []}

    monkeypatch.setattr(xnetwork_tool_mod, "get_gateway", _fake_get_gateway)
    monkeypatch.setattr(xnetwork_tool_mod, "run_network_query", _fake_run_network_query)

    sup = Supervisor()  # no override -> real module-level registry
    result = await sup.handle(_agent_input(caller=_caller(role=Role.SUPERVISOR)))

    # Both tools resolved to a definite empty -> EMPTY, a real finding.
    assert result.status == SubAgentStatus.EMPTY
    assert result.answer_text


# ═══════════════════════════════════════════════════════════════════════
# findings.md CCL-C3 — each XNETWORK link carries its OWN community's
# case_ids, never the tool's aggregate union
# ═══════════════════════════════════════════════════════════════════════
#
# Real shape that exposed this: a 3-result query over the live corpus
# returned C-20260825-0000 (['fir-410-25']), C-20260825-0006
# (['fir-64-26','fir-65-26']) and C-20260825-0001 (['fir-233-26']). All
# three were rendered as spanning 4 cases, because every link was stamped
# with the flattened union. 17 of 19 real communities are single-case.


def _xn_result(per_community, union, status=ToolStatus.OK):
    """An XNetworkToolResult with N chunks index-aligned to N case-id lists."""
    return XNetworkToolResult(
        status=status,
        chunks=[
            _xnetwork_chunk(community_id=f"community-{i}", text=f"summary {i}")
            for i in range(len(per_community))
        ],
        case_ids_touched=union,
        community_ids=[f"community-{i}" for i in range(len(per_community))],
        community_case_ids=per_community,
        raw_summary_text="raw",
    )


def test_ccl_c3_single_result_single_case_is_not_widened():
    """(1) A single-case community must report exactly its own case."""
    links = ccl_mod._xnetwork_links(_xn_result([["CASE-001"]], ["CASE-001"]))

    assert len(links) == 1
    assert links[0].case_ids == ["CASE-001"]


def test_ccl_c3_multiple_results_same_case_do_not_become_cross_case():
    """(2) Multiplicity of results must not imply a cross-case span."""
    links = ccl_mod._xnetwork_links(_xn_result([["CASE-001"], ["CASE-001"]], ["CASE-001"]))

    assert [l.case_ids for l in links] == [["CASE-001"], ["CASE-001"]]


def test_ccl_c3_multiple_results_different_cases_stay_separate():
    """
    (3) The core regression. Two single-case communities must each report
    ONLY their own case — while case_ids_touched stays the union for the
    Verifier.
    """
    result = _xn_result([["CASE-001"], ["CASE-002"]], ["CASE-001", "CASE-002"])
    links = ccl_mod._xnetwork_links(result)

    assert links[0].case_ids == ["CASE-001"]
    assert links[1].case_ids == ["CASE-002"]
    assert result.case_ids_touched == ["CASE-001", "CASE-002"], (
        "the Verifier's allowed-cross-case-ID union must be preserved"
    )
    for link in links:
        assert link.case_ids != result.case_ids_touched, (
            "a single-case community must not inherit the aggregate span"
        )


def test_ccl_c3_genuinely_multi_case_community_is_preserved():
    """(4) The 2 of 19 real multi-case communities must keep both IDs."""
    links = ccl_mod._xnetwork_links(
        _xn_result([["CASE-001", "CASE-002"]], ["CASE-001", "CASE-002"])
    )

    assert links[0].case_ids == ["CASE-001", "CASE-002"]


def test_ccl_c3_ordering_is_index_aligned_with_chunks():
    """(6) Entry i must belong to community i, not merely be present."""
    result = _xn_result(
        [["CASE-001"], ["CASE-002", "CASE-003"], ["CASE-004"]],
        ["CASE-001", "CASE-002", "CASE-003", "CASE-004"],
    )
    links = ccl_mod._xnetwork_links(result)

    assert [l.case_ids for l in links] == [
        ["CASE-001"], ["CASE-002", "CASE-003"], ["CASE-004"],
    ]
    assert [l.description for l in links] == ["summary 0", "summary 1", "summary 2"]


def test_ccl_c3_absent_field_degrades_to_empty_never_to_the_union():
    """
    (7) Backward compatibility. A caller that omits community_case_ids must
    yield [] — NOT case_ids_touched, which would silently recreate CCL-C3.
    """
    legacy = XNetworkToolResult(
        status=ToolStatus.OK,
        chunks=[_xnetwork_chunk(community_id="community-0", text="summary 0")],
        case_ids_touched=["CASE-001", "CASE-002"],
        community_ids=["community-0"],
    )

    links = ccl_mod._xnetwork_links(legacy)

    assert links[0].case_ids == []
    assert links[0].case_ids != legacy.case_ids_touched


def test_ccl_c3_no_chunks_yields_no_links():
    """(5) EMPTY-shaped results are unaffected."""
    assert ccl_mod._xnetwork_links(_xn_result([], [], status=ToolStatus.EMPTY)) == []


# ═══════════════════════════════════════════════════════════════════════
# CCL-C2 — an aggregate traversal footprint must not be attributed to a
# fictional singular recurring entity.
#
# `case_ids_touched` is the UNION of cases across every returned chunk,
# and the Verifier's allowed-cross-case-ID list. When the router supplies
# no `target_entity` (the correct, intended state for broad questions
# like "which cases share suspects?"), no single entity was ever
# identified — so the old "A recurring entity appears across N case(s)"
# asserted a recurrence the traversal never established.
#
# Fixtures use real-corpus-shaped case IDs (fir-88-26 / fir-117-26 /
# fir-97-26). `fir-97-26` is used ONLY as an identifier string here; its
# real victim-edge issue is explicitly out of scope.
# ═══════════════════════════════════════════════════════════════════════

_CCL_C2_NAMED_ENTITY = "محمد علی"  # "Muhammad Ali"


def _ccl_c2_xg_result(case_ids, *, hop_count=2, status=ToolStatus.OK):
    return XGraphToolResult(
        status=status,
        chunks=[_xgraph_chunk()],
        case_ids_touched=list(case_ids),
        hop_count=hop_count,
        chain_confidence=0.8,
    )


def _ccl_c2_link(target_entity, case_ids, *, hop_count=2, status=ToolStatus.OK):
    tool_input = XGraphToolInput(
        query_text="which cases share suspects",
        execution=_execution(),
        target_entity=target_entity,
    )
    return ccl_mod._xgraph_confirmed_link(
        tool_input, _ccl_c2_xg_result(case_ids, hop_count=hop_count, status=status)
    )


def test_ccl_c2_open_ended_does_not_invent_a_recurring_entity():
    """(A) target_entity=None must not claim one unnamed entity recurs."""
    link = _ccl_c2_link(None, ["fir-88-26", "fir-117-26"])
    assert link is not None
    desc = link.description

    # The defect verbatim, and the semantic claim behind it.
    assert "A recurring entity" not in desc
    assert "recurring entity" not in desc.lower()
    assert "appears across" not in desc.lower()

    # It must still describe the real aggregate footprint.
    assert "2 case(s)" in desc
    assert "fir-88-26" in desc and "fir-117-26" in desc
    assert link.case_ids == ["fir-88-26", "fir-117-26"]


def test_ccl_c2_named_target_behavior_preserved():
    """(B) Regression guard: the named branch is unchanged."""
    link = _ccl_c2_link(_CCL_C2_NAMED_ENTITY, ["fir-88-26", "fir-117-26"])
    assert link is not None
    desc = link.description

    assert f"'{_CCL_C2_NAMED_ENTITY}'" in desc
    assert "appears across 2 case(s)" in desc
    assert "fir-88-26" in desc and "fir-117-26" in desc
    assert "traversal depth 2 hop(s)" in desc
    assert "A recurring entity" not in desc


def test_ccl_c2_open_ended_multi_case_preserves_traversal_facts():
    """(C) Hop count and the full case footprint survive the rewording."""
    case_ids = ["fir-88-26", "fir-97-26", "fir-117-26"]
    link = _ccl_c2_link(None, case_ids, hop_count=3)
    assert link is not None
    desc = link.description

    assert "recurring entity" not in desc.lower()
    assert "3 case(s)" in desc
    for cid in case_ids:
        assert cid in desc
    assert "3 hop(s)" in desc
    assert link.case_ids == case_ids
    assert link.source_tool == "XGRAPH"
    assert link.is_unconfirmed is False


def test_ccl_c2_open_ended_empty_footprint_yields_no_link():
    """
    (D) The genuinely reachable empty state.

    `xgraph_tool` sets status=OK from `chunks` being non-empty, while
    `case_ids_touched` is built only from chunks that carry a `case_id`
    (tools/xgraph.py:145-151) — so OK with an empty footprint is real,
    not a contrived fixture. The old code emitted a link reading
    "appears across 0 case(s): unknown". No link at all is correct: the
    existing `_xgraph_summary_line` empty path then stands.
    """
    assert _ccl_c2_link(None, []) is None

    # And the summary line that now stands alone says nothing was found.
    assert (
        ccl_mod._xgraph_summary_line([], 2, 0)
        == "No confirmed cross-case entity connections were found."
    )


def test_ccl_c2_named_target_empty_footprint_unchanged():
    """
    (B2) The named branch keeps its prior empty-footprint behavior — the
    fix narrows only the unnamed branch, so this stays a link.
    """
    link = _ccl_c2_link(_CCL_C2_NAMED_ENTITY, [])
    assert link is not None
    assert "unknown" in link.description
    assert f"'{_CCL_C2_NAMED_ENTITY}'" in link.description


# ── Finding AC regression: identity certainty must be hedged ─────────────────
# "Is X definitely the same person across all these cases?" came back as
# "Entity-graph search found confirmed connections across 13 other case(s):
# ..." — a flat assertion, with the supporting confidence never shown and no
# statement that entity resolution is an inference rather than proof of one
# individual (verify-log Finding AC). chain_confidence is the product of edge
# confidences along the weakest chain, so it degrades with hop depth; a 50%
# chain must not read the same as a 95% one.

from src.pipeline.harness.agents.cross_case_linkage import (
    _CONFIDENT_CHAIN_THRESHOLD,
    _xgraph_summary_line,
)

_CASES = ["fir-201-26", "fir-202-26"]


def test_weak_chain_is_reported_as_possible_not_confirmed():
    # [Gold-QA fix — Module 6] Phrasing is now answer-first ("Yes — … a
    # possible cross-case link"); a weak chain must still read as
    # tentative/possible, never as a flat confirmed assertion.
    text = _xgraph_summary_line(_CASES, 1, 0, chain_confidence=0.50)
    assert "possible" in text and "uncertainty" in text
    assert "confirmed cross-case link" not in text


def test_strong_chain_may_still_say_confirmed():
    text = _xgraph_summary_line(_CASES, 1, 0, chain_confidence=0.95)
    assert "confirmed cross-case link" in text


def test_chain_confidence_is_surfaced_to_the_reader():
    assert "50%" in _xgraph_summary_line(_CASES, 1, 0, chain_confidence=0.50)
    assert "95%" in _xgraph_summary_line(_CASES, 1, 0, chain_confidence=0.95)


def test_identity_inference_caveat_always_present_when_links_exist():
    """A graph link must never read as proof that every mention is one human."""
    for confidence in (None, 0.20, 0.99):
        text = _xgraph_summary_line(_CASES, 1, 0, chain_confidence=confidence)
        assert "not independent proof" in text


def test_threshold_boundary_is_not_hedged():
    """Exactly at the threshold counts as confident, matching the verifier's
    own 0.85 hedging rule so the two cannot disagree."""
    text = _xgraph_summary_line(_CASES, 1, 0, chain_confidence=_CONFIDENT_CHAIN_THRESHOLD)
    assert "confirmed cross-case link" in text


def test_no_links_message_is_unchanged_and_carries_no_caveat():
    text = _xgraph_summary_line([], 0, 0)
    assert text == "No confirmed cross-case entity connections were found."


def test_unconfirmed_matches_still_reported():
    text = _xgraph_summary_line(_CASES, 1, 2, chain_confidence=0.95)
    assert "2 additional possible identity matches" in text
    assert "unconfirmed" in text


# ── Gold-QA fix — Module 6: answer-first phrasing ────────────────────────────
# A cross-case-link question ("is anyone a repeat suspect across cases?")
# must read as a direct answer, not a debug-log-style "Entity-graph search
# found …" line — the underlying facts (case ids, hop depth, confidence,
# identity caveat) are unchanged, only the lead-in framing.

def test_confident_chain_leads_with_a_plain_yes():
    text = _xgraph_summary_line(_CASES, 1, 0, chain_confidence=0.95)
    assert text.startswith("Yes —")
    assert "Entity-graph search found" not in text


def test_tentative_chain_leads_with_a_hedged_yes():
    text = _xgraph_summary_line(_CASES, 1, 0, chain_confidence=0.50)
    assert text.startswith("Yes (with some uncertainty) —")


def test_answer_first_phrasing_still_names_the_case_ids_and_hop_depth():
    """The reframe must not drop any fact the old phrasing stated."""
    text = _xgraph_summary_line(_CASES, 2, 0, chain_confidence=0.95)
    for case_id in _CASES:
        assert case_id in text
    assert "depth 2 hop(s)" in text
