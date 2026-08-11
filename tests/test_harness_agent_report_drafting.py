"""
Tests for src/pipeline/harness/agents/report_drafting.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 7, "Phase 8").

Covers:
  (a) the disclosure-ordering contract (SUBAGENT_INTERFACES.md §2.1.3):
      no disclosure on status=OK; inherited-disclosure suppression
      (degraded_from=["RAG"] -> propagate GRAPH_ONLY_SUMMARY_DISCLOSURE,
      never the generic template too); fresh-disclosure injection
      (degraded_from=["GRAPH"] -> inject PARTIAL_EVIDENCE_DISCLOSURE_
      TEMPLATE with a display-labeled source); citation-consistency
      failure aborts BEFORE the Verifier runs; Verifier rejection aborts
      BEFORE any document is built;
  (b) file-build failure -> ABSTAINED with an explicit file-generation
      error, matching _generate_file()'s own except-Exception shape;
  (c) Case Summarization terminal statuses (EMPTY/ABSTAINED/DENIED)
      propagate untouched, no document built;
  (d) invalid output_format handled without crashing;
  (e) _persist_generated_file()'s gateway/no-gateway fallback behavior;
  (f) module-level self-registration, and a Supervisor.handle() ->
      Report Drafting integration test proving output_format's
      file_pdf/file_xlsx/file_docx override reaches this sub-agent via
      REAL (unmodified) classify_to_subagent() -- not bypassed.

`case_summarization`, `call_llm`, `verify_grounding`, `structure_for_file`,
and the `_BUILDERS` entries are monkeypatched at the module level
(`rd_mod.*`) in every test -- none of these hit live infra.
"""
from __future__ import annotations

import pytest

import src.pipeline.harness.agents.report_drafting as rd_mod
from src.pipeline.harness.agents.report_drafting import report_drafting
from src.pipeline.harness.supervisor import (
    REPORT_DRAFTING,
    Supervisor,
    get_registered,
)
from src.pipeline.harness.types import (
    CallerContext,
    ExecutionContext,
    GRAPH_ONLY_SUMMARY_DISCLOSURE,
    PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
)


def _caller(case_id="CASE-001", role=Role.INVESTIGATOR, **kw):
    return CallerContext(user_id="u1", role=role, active_case_id=case_id, **kw)


def _execution(caller=None, session_id="sess-1"):
    return ExecutionContext(caller=caller or _caller(), session_id=session_id)


def _agent_input(caller=None, output_format="file_pdf", session_id="sess-1", **kw):
    return SubAgentInput(
        query_text="draft a report on this case",
        execution=_execution(caller=caller, session_id=session_id),
        output_format=output_format,
        **kw,
    )


def _stub_case_summarization(monkeypatch, result: SubAgentResult):
    async def _fake(agent_input, *, on_event=None, gateway=None):
        return result
    monkeypatch.setattr(rd_mod, "case_summarization", _fake)


def _stub_call_llm(monkeypatch, text="Drafted report text [Document 1]."):
    async def _fake(*args, **kwargs):
        return text
    monkeypatch.setattr(rd_mod, "call_llm", _fake)


def _stub_verify_grounding(monkeypatch, grounded=True, off_topic=False, reason=""):
    async def _fake(**kwargs):
        return {"grounded": grounded, "off_topic": off_topic, "reason": reason}
    monkeypatch.setattr(rd_mod, "verify_grounding", _fake)


def _stub_validate_answer(monkeypatch, status=None, claims=None):
    """
    Stubs the Validation gate at the boundary this module actually calls
    (`rd_mod.validate_answer`) -- same discipline as the other sub-agent
    test files' helper of the same name. Defaults to PASSED/[].
    """
    from src.pipeline.harness.types import ValidationStatus

    resolved_status = status if status is not None else ValidationStatus.PASSED
    resolved_claims = claims if claims is not None else []

    async def _fake(*args, **kwargs):
        return resolved_status, resolved_claims

    monkeypatch.setattr(rd_mod, "validate_answer", _fake)


def _stub_structure_for_file(monkeypatch, payload=None):
    payload = payload if payload is not None else {"title": "Case Report", "description": "", "sections": []}

    async def _fake(content, requested_format):
        # Return a fresh copy each call so mutation in one test can't leak.
        return {**payload, "sections": list(payload.get("sections", []))}

    monkeypatch.setattr(rd_mod, "structure_for_file", _fake)


def _stub_builder(monkeypatch, file_type="pdf", filepath="/tmp/x.pdf", size=1234, raises=None):
    def _fake(payload):
        if raises is not None:
            raise raises
        _fake.received_payload = payload
        return filepath, size

    monkeypatch.setitem(rd_mod._BUILDERS, file_type, _fake)
    return _fake


@pytest.fixture(autouse=True)
def isolated_registry(monkeypatch):
    """Report Drafting self-registers at import time into the module-level
    registry; give Supervisor its own dict so tests never leak into each
    other via that shared global."""
    from src.pipeline.harness import supervisor as supervisor_mod

    fresh = {REPORT_DRAFTING: get_registered(REPORT_DRAFTING)}
    monkeypatch.setattr(supervisor_mod, "_REGISTRY", fresh)
    return fresh


# ═══════════════════════════════════════════════════════════════════════
# (a) disclosure-ordering contract
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_ok_status_no_disclosure(monkeypatch):
    _stub_case_summarization(
        monkeypatch,
        SubAgentResult(
            status=SubAgentStatus.OK,
            answer_text="The case involves a stolen vehicle.",
            tools_used=["RAG", "GRAPH"],
        ),
    )
    _stub_call_llm(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)
    _stub_structure_for_file(monkeypatch)
    builder = _stub_builder(monkeypatch)

    result = await report_drafting(_agent_input(), gateway=None)

    assert result.status == SubAgentStatus.OK
    assert result.generated_file is not None
    assert result.generated_file.disclosure_rendered is False
    assert not any(s.get("content") for s in builder.received_payload.get("sections", []))


@pytest.mark.asyncio
async def test_inherited_graph_only_disclosure_is_suppressed_not_duplicated(monkeypatch):
    inherited_answer = f"{GRAPH_ONLY_SUMMARY_DISCLOSURE}\n\nEntities: Person P-1, Vehicle V-1."
    _stub_case_summarization(
        monkeypatch,
        SubAgentResult(
            status=SubAgentStatus.PARTIAL,
            answer_text=inherited_answer,
            tools_used=["GRAPH"],
            degraded_from=["RAG"],
        ),
    )
    _stub_call_llm(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)
    _stub_structure_for_file(monkeypatch)
    builder = _stub_builder(monkeypatch)

    result = await report_drafting(_agent_input(), gateway=None)

    assert result.status == SubAgentStatus.PARTIAL
    assert result.generated_file.disclosure_rendered is True
    injected = [
        s.get("content") for s in builder.received_payload.get("sections", [])
        if s.get("type") == "paragraph"
    ]
    assert injected == [GRAPH_ONLY_SUMMARY_DISCLOSURE]
    # Never the generic template ALSO injected for the same gap.
    assert PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE.split("{")[0] not in "".join(injected)
    # The disclosure must never have been passed into the drafting LLM's
    # evidentiary content -- it was stripped structurally before drafting.
    assert GRAPH_ONLY_SUMMARY_DISCLOSURE not in rd_mod._strip_inherited_disclosure(
        inherited_answer, ["RAG"]
    )


@pytest.mark.asyncio
async def test_validation_issues_found_stays_caveat_only_no_document_disclosure(monkeypatch):
    from src.pipeline.harness.types import ClaimSupport, ValidationClaimResult, ValidationStatus

    _stub_case_summarization(
        monkeypatch,
        SubAgentResult(status=SubAgentStatus.OK, answer_text="Summary text.", tools_used=["RAG"]),
    )
    _stub_call_llm(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)
    flagged = ValidationClaimResult(
        document_index=1, claim_excerpt="claim", support=ClaimSupport.NOT_SUPPORTED, reason="mismatch",
    )
    _stub_validate_answer(monkeypatch, status=ValidationStatus.ISSUES_FOUND, claims=[flagged])
    _stub_structure_for_file(monkeypatch)
    builder = _stub_builder(monkeypatch)

    result = await report_drafting(_agent_input(), gateway=None)

    assert result.status == SubAgentStatus.OK  # caveat-only, never blocking
    assert result.validation_status == ValidationStatus.ISSUES_FOUND
    assert any("mismatch" in c for c in result.caveats)
    # [PRESERVE -- module docstring's own exception is NOT_RUN only]
    # ISSUES_FOUND must NOT get the document-body disclosure treatment.
    assert result.generated_file.disclosure_rendered is False
    assert not any(s.get("content") for s in builder.received_payload.get("sections", []))


@pytest.mark.asyncio
async def test_validation_not_run_renders_document_body_disclosure(monkeypatch):
    from src.pipeline.harness.types import ValidationStatus

    _stub_case_summarization(
        monkeypatch,
        SubAgentResult(status=SubAgentStatus.OK, answer_text="Summary text.", tools_used=["RAG"]),
    )
    _stub_call_llm(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch, status=ValidationStatus.NOT_RUN, claims=[])
    _stub_structure_for_file(monkeypatch)
    builder = _stub_builder(monkeypatch)

    result = await report_drafting(_agent_input(), gateway=None)

    # [PRESERVE -- plan §7.1's own named exception for Report Drafting]
    assert result.status == SubAgentStatus.OK
    assert result.validation_status == ValidationStatus.NOT_RUN
    assert result.generated_file.disclosure_rendered is True
    injected = [
        s.get("content") for s in builder.received_payload.get("sections", [])
        if s.get("type") == "paragraph"
    ]
    assert injected == [rd_mod.VALIDATION_NOT_RUN_DISCLOSURE]


@pytest.mark.asyncio
async def test_fresh_disclosure_injected_for_rag_only_degradation(monkeypatch):
    _stub_case_summarization(
        monkeypatch,
        SubAgentResult(
            status=SubAgentStatus.PARTIAL,
            answer_text="Status: open. Based on case documents only.",
            tools_used=["RAG"],
            degraded_from=["GRAPH"],
        ),
    )
    _stub_call_llm(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)
    _stub_structure_for_file(monkeypatch)
    builder = _stub_builder(monkeypatch)

    result = await report_drafting(_agent_input(), gateway=None)

    assert result.status == SubAgentStatus.PARTIAL
    assert result.generated_file.disclosure_rendered is True
    injected = [
        s.get("content") for s in builder.received_payload.get("sections", [])
        if s.get("type") == "paragraph"
    ]
    assert len(injected) == 1
    assert "case-graph search" in injected[0]  # SOURCE_TOOL_DISPLAY_LABELS["GRAPH"]
    assert injected[0] != GRAPH_ONLY_SUMMARY_DISCLOSURE


@pytest.mark.asyncio
async def test_xlsx_disclosure_survives_when_a_table_section_exists(monkeypatch):
    """[PRESERVE — this session's own finding] build_xlsx() never reads
    "paragraph" sections; a leading paragraph alone would silently vanish.
    The disclosure must also land inside the table's own rows."""
    _stub_case_summarization(
        monkeypatch,
        SubAgentResult(
            status=SubAgentStatus.PARTIAL,
            answer_text="Key events: ...",
            tools_used=["RAG"],
            degraded_from=["GRAPH"],
        ),
    )
    _stub_call_llm(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)
    _stub_structure_for_file(
        monkeypatch,
        payload={
            "title": "Case Report",
            "description": "",
            "sections": [{"type": "table", "headers": ["Event", "Date"], "rows": [["Theft", "2024-01-01"]]}],
        },
    )
    builder = _stub_builder(monkeypatch, file_type="xlsx")

    result = await report_drafting(_agent_input(output_format="file_xlsx"), gateway=None)

    assert result.status == SubAgentStatus.PARTIAL
    assert result.generated_file.disclosure_rendered is True
    table_section = next(
        s for s in builder.received_payload["sections"] if s.get("type") == "table"
    )
    assert "case-graph search" in table_section["rows"][0][0]
    assert "case-graph search" in builder.received_payload["description"]


@pytest.mark.asyncio
async def test_citation_consistency_failure_aborts_before_verifier(monkeypatch):
    _stub_case_summarization(
        monkeypatch,
        SubAgentResult(status=SubAgentStatus.OK, answer_text="Summary text.", tools_used=["RAG"]),
    )
    # The redraft invents a second document reference that does not exist.
    _stub_call_llm(monkeypatch, text="Claim A [Document 1]. Claim B [Document 2].")
    verifier_calls = []

    async def _tracking_verify(**kwargs):
        verifier_calls.append(kwargs)
        return {"grounded": True, "off_topic": False}

    monkeypatch.setattr(rd_mod, "verify_grounding", _tracking_verify)
    _stub_structure_for_file(monkeypatch)
    _stub_builder(monkeypatch)

    result = await report_drafting(_agent_input(), gateway=None)

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.generated_file is None
    assert verifier_calls == []  # Verifier never reached.


@pytest.mark.asyncio
async def test_verifier_rejection_aborts_before_any_document_is_built(monkeypatch):
    _stub_case_summarization(
        monkeypatch,
        SubAgentResult(status=SubAgentStatus.OK, answer_text="Summary text.", tools_used=["RAG"]),
    )
    _stub_call_llm(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=False, reason="not grounded")

    structure_calls = []

    async def _tracking_structure(content, requested_format):
        structure_calls.append((content, requested_format))
        return {"title": "x", "sections": []}

    monkeypatch.setattr(rd_mod, "structure_for_file", _tracking_structure)
    _stub_builder(monkeypatch)

    result = await report_drafting(_agent_input(), gateway=None)

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.generated_file is None
    assert structure_calls == []  # No document assembly was ever attempted.


# ═══════════════════════════════════════════════════════════════════════
# (b) file-build failure
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_file_build_failure_yields_abstained_with_explicit_error(monkeypatch):
    _stub_case_summarization(
        monkeypatch,
        SubAgentResult(status=SubAgentStatus.OK, answer_text="Summary text.", tools_used=["RAG"]),
    )
    _stub_call_llm(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)
    _stub_structure_for_file(monkeypatch)
    _stub_builder(monkeypatch, raises=RuntimeError("disk full"))

    result = await report_drafting(_agent_input(), gateway=None)

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.generated_file is None
    assert result.error is not None
    assert result.error.kind == "upstream_failure"
    assert "disk full" in result.error.message


# ═══════════════════════════════════════════════════════════════════════
# (c) Case Summarization terminal statuses propagate untouched
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.parametrize(
    "status", [SubAgentStatus.EMPTY, SubAgentStatus.ABSTAINED, SubAgentStatus.DENIED]
)
@pytest.mark.asyncio
async def test_terminal_case_summarization_status_propagates(monkeypatch, status):
    _stub_case_summarization(
        monkeypatch,
        SubAgentResult(status=status, caveats=["nothing to summarize"]),
    )
    calls = {"n": 0}

    async def _should_not_be_called(*a, **kw):
        calls["n"] += 1
        return "should not run"

    monkeypatch.setattr(rd_mod, "call_llm", _should_not_be_called)

    result = await report_drafting(_agent_input(), gateway=None)

    assert result.status == status
    assert result.generated_file is None
    assert calls["n"] == 0  # No drafting LLM call for a terminal upstream status.


# ═══════════════════════════════════════════════════════════════════════
# (d) invalid output_format
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_invalid_output_format_returns_abstained_not_a_crash():
    result = await report_drafting(_agent_input(output_format="chat"), gateway=None)
    assert result.status == SubAgentStatus.ABSTAINED
    assert result.error.kind == "invalid_input"


@pytest.mark.asyncio
async def test_missing_session_id_aborts_upfront_before_any_work(monkeypatch):
    """[Reconciliation fix — harness-reconciliation Unit 9] Previously a
    missing session_id was only discovered at the storage step, after the
    full retrieve -> summarize -> draft -> verify -> build-file pipeline had
    already run to completion -- silently producing an undownloadable file
    reported as success. Now rejected immediately, before Case
    Summarization (or anything else expensive) is ever called."""
    called = {"case_summarization": False}

    async def _fail_if_called(*args, **kwargs):
        called["case_summarization"] = True
        raise AssertionError("case_summarization() should not be called without session_id")

    monkeypatch.setattr(rd_mod, "case_summarization", _fail_if_called)

    result = await report_drafting(_agent_input(session_id=None))

    assert result.status == SubAgentStatus.ABSTAINED
    assert result.answer_text is None
    assert result.error is not None
    assert result.error.kind == "invalid_input"
    assert "session" in result.error.message.lower()
    assert not called["case_summarization"]


# ═══════════════════════════════════════════════════════════════════════
# (e) gateway persistence fallback
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_persist_generated_file_uses_gateway_when_available():
    class _FakeGateway:
        async def log_generated_file(self, file_data):
            self.received = file_data
            return "persisted-id-123"

    gw = _FakeGateway()
    file_id = await rd_mod._persist_generated_file(
        gateway=gw, session_id="sess-1", user_id="u1", case_id="CASE-001",
        file_type="pdf", file_name="Report.pdf", file_size=100, storage_path="/x/Report.pdf",
    )
    assert file_id == "persisted-id-123"
    assert gw.received["session_id"] == "sess-1"


@pytest.mark.asyncio
async def test_persist_generated_file_falls_back_without_gateway():
    file_id = await rd_mod._persist_generated_file(
        gateway=None, session_id=None, user_id=None, case_id=None,
        file_type="pdf", file_name="Report.pdf", file_size=100, storage_path="/x/Report.pdf",
    )
    assert file_id  # a locally generated uuid string, not persisted


@pytest.mark.asyncio
async def test_persist_generated_file_falls_back_when_session_id_missing():
    class _FakeGateway:
        async def log_generated_file(self, file_data):
            raise AssertionError("should not be called without session_id")

    file_id = await rd_mod._persist_generated_file(
        gateway=_FakeGateway(), session_id=None, user_id="u1", case_id="CASE-001",
        file_type="pdf", file_name="Report.pdf", file_size=100, storage_path="/x/Report.pdf",
    )
    assert file_id


# ═══════════════════════════════════════════════════════════════════════
# (f) self-registration + Supervisor integration (real classify_to_subagent)
# ═══════════════════════════════════════════════════════════════════════

def test_module_self_registers():
    assert get_registered(REPORT_DRAFTING) is not None
    assert get_registered(REPORT_DRAFTING).name == REPORT_DRAFTING


@pytest.mark.asyncio
async def test_supervisor_dispatch_reaches_report_drafting_via_output_format_override(
    monkeypatch, isolated_registry
):
    """
    Proves Report Drafting is reachable via REAL, unmodified
    Supervisor.handle() -> classify_to_subagent() today: output_format in
    {file_pdf, file_xlsx, file_docx} overrides to Report Drafting
    regardless of route (Phase 1's own behavior, confirmed still true) --
    not bypassed the way Timeline Building's own integration test had to
    bypass classification.
    """
    from src.pipeline.harness import supervisor as supervisor_mod

    async def _stub_route_query(_query_text):
        return {"route": "RAG", "output_format": "file_pdf"}

    monkeypatch.setattr(supervisor_mod, "route_query", _stub_route_query)

    _stub_case_summarization(
        monkeypatch,
        SubAgentResult(status=SubAgentStatus.OK, answer_text="Summary text.", tools_used=["RAG"]),
    )
    _stub_call_llm(monkeypatch)
    _stub_verify_grounding(monkeypatch, grounded=True)
    _stub_validate_answer(monkeypatch)
    _stub_structure_for_file(monkeypatch)
    _stub_builder(monkeypatch)

    sup = Supervisor(registry=isolated_registry)
    result = await sup.handle(_agent_input(output_format="file_pdf"))

    assert result.status == SubAgentStatus.OK
    assert result.generated_file is not None
