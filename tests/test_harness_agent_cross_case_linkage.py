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
from src.pipeline.harness.tools.xgraph import XGraphToolResult
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
        return {"route": "XGRAPH", "output_format": "chat"}

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
