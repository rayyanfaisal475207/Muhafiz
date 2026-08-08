"""
Report Drafting sub-agent — contract tests.

The case worth guarding hardest is DISCLOSURE SUPPRESSION (§2.1.3): when Case
Summarization already disclosed a gap in its own text, Report Drafting must not
disclose the same gap again. Two statements of one fact in a single document
reads as an error and dilutes both.

Production boundaries (the summarization tools, the LLM structurer, the file
builders, the gateway) are mocked, so these need no database, model server,
network, or filesystem writes.
"""
from __future__ import annotations

import pytest

from src.pipeline.harness.agents import report_draft
from src.pipeline.harness.contracts import (
    GRAPH_ONLY_SUMMARY_DISCLOSURE,
    PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE,
    SOURCE_TOOL_DISPLAY_LABELS,
    CallerContext,
    Citation,
    EvidenceChunk,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
)
from src.pipeline.harness.events import EventRecorder
from src.pipeline.harness.verifier_gate import UNGROUNDED_TRIGGER


def _caller() -> CallerContext:
    return CallerContext(user_id="u1", role=Role.INVESTIGATOR, active_case_id="CASE-A")


def _input(fmt: str = "file_pdf", query: str = "Draft a report") -> SubAgentInput:
    return SubAgentInput(query_text=query, caller=_caller(), output_format=fmt)


def _citation(i: int = 1, tool: str = "RAG") -> Citation:
    return Citation(document_index=i, source_tool=tool, case_id="CASE-A", source_file="f.pdf")


@pytest.fixture
def summary_returns(monkeypatch):
    """Drive the Case Summarization sub-agent to a caller-chosen result."""
    def _configure(result: SubAgentResult):
        async def _run(agent_input, events=None):
            return result

        from src.pipeline.harness.agents import case_summary

        monkeypatch.setattr(case_summary, "run", _run)
        return result

    return _configure


@pytest.fixture
def builders(monkeypatch):
    """
    Mock the LLM structurer and the three file builders.

    Captures the payload each builder received, so tests can assert on what
    actually reached the DOCUMENT rather than only on the returned metadata.
    """
    captured: dict = {"payload": None, "built": None}

    async def _structure(content, requested_format):
        return {
            "title": "Case Report",
            "description": "summary",
            "sections": [{"type": "paragraph", "content": content}],
        }

    def _build(kind):
        def _inner(payload):
            captured["payload"] = payload
            captured["built"] = kind
            return (f"/generated/report.{kind}", 2048)
        return _inner

    monkeypatch.setattr("src.pipeline.file_structurer.structure_for_file", _structure)
    monkeypatch.setattr("src.generation.pdf_builder.build_pdf", _build("pdf"))
    monkeypatch.setattr("src.generation.xlsx_builder.build_xlsx", _build("xlsx"))
    monkeypatch.setattr("src.generation.docx_builder.build_docx", _build("docx"))
    return captured


def _disclosure_texts(payload: dict) -> list[str]:
    """
    Every section body in the built document that is a disclosure.

    Keyed on the CONSTANTS' own distinctive opening rather than on a marker
    string. The wording is final but still owned by product, so matching on a
    stable structural feature of each template keeps these tests from breaking
    on a future rewording — the same reasoning that makes report_draft.py key
    suppression on the caveats constant instead of the prose.
    """
    stems = (
        PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE.split("{")[0].strip(),
        GRAPH_ONLY_SUMMARY_DISCLOSURE.split(".")[0].strip(),
    )
    return [
        s.get("content", "") for s in payload.get("sections", [])
        if any(stem and stem in str(s.get("content", "")) for stem in stems)
    ]


# ── Full-evidence report ─────────────────────────────────────────────────

async def test_full_evidence_report_is_ok_with_a_file(summary_returns, builders, gateway):
    summary_returns(SubAgentResult(
        status=SubAgentStatus.OK, answer_text="Full summary [Document 1]",
        citations=[_citation()], tools_used=["RAG", "GRAPH"],
    ))

    result = await report_draft.run(_input(), gateway=gateway)

    assert result.status is SubAgentStatus.OK
    assert result.generated_file is not None
    assert result.generated_file.file_name.endswith(".pdf")
    assert result.generated_file.disclosure_rendered is False
    assert result.caveats == []


async def test_full_evidence_report_injects_no_disclosure(summary_returns, builders, gateway):
    summary_returns(SubAgentResult(
        status=SubAgentStatus.OK, answer_text="Full summary [Document 1]",
        citations=[_citation()], tools_used=["RAG", "GRAPH"],
    ))

    await report_draft.run(_input(), gateway=gateway)

    assert _disclosure_texts(builders["payload"]) == []


@pytest.mark.parametrize("fmt,kind", [
    ("file_pdf", "pdf"), ("file_xlsx", "xlsx"), ("file_docx", "docx"),
])
async def test_routes_to_the_right_builder(fmt, kind, summary_returns, builders, gateway):
    summary_returns(SubAgentResult(
        status=SubAgentStatus.OK, answer_text="s [Document 1]", citations=[_citation()],
        tools_used=["RAG"],
    ))

    result = await report_draft.run(_input(fmt=fmt), gateway=gateway)

    assert builders["built"] == kind
    assert result.generated_file.file_name.endswith(f".{kind}")


# ── PARTIAL inherited, and the suppression rule ──────────────────────────

async def test_partial_summary_yields_partial_report(summary_returns, builders, gateway):
    summary_returns(SubAgentResult(
        status=SubAgentStatus.PARTIAL, answer_text="Partial summary [Document 1]",
        citations=[_citation(tool="GRAPH")], tools_used=["GRAPH"], degraded_from=["RAG"],
        caveats=[GRAPH_ONLY_SUMMARY_DISCLOSURE],
    ))

    result = await report_draft.run(_input(), gateway=gateway)

    assert result.status is SubAgentStatus.PARTIAL
    assert result.degraded_from == ["RAG"]


async def test_does_not_double_disclose_a_gap_summarization_already_covered(
    summary_returns, builders, gateway
):
    """
    THE SUPPRESSION RULE. Case Summarization's GRAPH-only disclosure already
    names the missing-document gap and rides along in the inherited text.
    Report Drafting must NOT add PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE for that
    same gap — the document would state one fact twice.
    """
    summary_returns(SubAgentResult(
        status=SubAgentStatus.PARTIAL,
        answer_text=f"{GRAPH_ONLY_SUMMARY_DISCLOSURE}\n\nPartial summary [Document 1]",
        citations=[_citation(tool="GRAPH")], tools_used=["GRAPH"], degraded_from=["RAG"],
        caveats=[GRAPH_ONLY_SUMMARY_DISCLOSURE],
    ))

    result = await report_draft.run(_input(), gateway=gateway)

    injected = _disclosure_texts(builders["payload"])
    template_stem = PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE.split("{")[0].strip()
    new_disclosures = [d for d in injected if template_stem in d]
    assert new_disclosures == [], (
        "Report Drafting re-disclosed a gap Case Summarization had already "
        "disclosed — the document now states the same fact twice."
    )
    assert result.generated_file.disclosure_rendered is False, (
        "disclosure_rendered asserts THIS sub-agent wrote a line; the inherited "
        "one is already in the drafted text."
    )


async def test_inherited_disclosure_still_reaches_the_document(
    summary_returns, builders, gateway
):
    """
    Suppression must not mean the gap goes undisclosed. The upstream line is
    part of the content being drafted from, so it lands in the document by
    propagation rather than by re-injection.
    """
    summary_returns(SubAgentResult(
        status=SubAgentStatus.PARTIAL,
        answer_text=f"{GRAPH_ONLY_SUMMARY_DISCLOSURE}\n\nPartial summary [Document 1]",
        citations=[_citation(tool="GRAPH")], tools_used=["GRAPH"], degraded_from=["RAG"],
        caveats=[GRAPH_ONLY_SUMMARY_DISCLOSURE],
    ))

    await report_draft.run(_input(), gateway=gateway)

    body = " ".join(
        str(s.get("content", "")) for s in builders["payload"].get("sections", [])
    )
    assert GRAPH_ONLY_SUMMARY_DISCLOSURE in body


async def test_discloses_a_gap_summarization_did_not_cover(
    summary_returns, builders, gateway
):
    """
    The other half of the rule: a DIFFERENT gap does get a new disclosure. Here
    GRAPH degraded while RAG carried the summary — nothing upstream disclosed
    that, so this sub-agent must.
    """
    summary_returns(SubAgentResult(
        status=SubAgentStatus.PARTIAL, answer_text="Doc-based summary [Document 1]",
        citations=[_citation()], tools_used=["RAG"], degraded_from=["GRAPH"],
    ))

    result = await report_draft.run(_input(), gateway=gateway)

    injected = _disclosure_texts(builders["payload"])
    assert len(injected) == 1
    # Named by its investigator-facing label, not the internal tool name.
    assert SOURCE_TOOL_DISPLAY_LABELS["GRAPH"] in injected[0]
    assert "GRAPH" not in injected[0], (
        "the raw tool identifier leaked into a delivered document"
    )
    assert result.generated_file.disclosure_rendered is True


async def test_disclosure_is_injected_after_structuring_not_before(
    summary_returns, builders, gateway
):
    """
    §2.1.3 step 5: added to the payload AFTER structure_for_file() ran, so the
    LLM can never rewrite or paraphrase it. Its exemption from the grounding
    gate rests entirely on it being a fixed reviewed string.
    """
    summary_returns(SubAgentResult(
        status=SubAgentStatus.PARTIAL, answer_text="Doc summary [Document 1]",
        citations=[_citation()], tools_used=["RAG"], degraded_from=["GRAPH"],
    ))

    await report_draft.run(_input(), gateway=gateway)

    first = builders["payload"]["sections"][0]
    # Substituted with the INVESTIGATOR-FACING label, not the raw tool name.
    expected = PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE.format(
        unavailable_sources=SOURCE_TOOL_DISPLAY_LABELS["GRAPH"]
    )
    assert first["content"] == expected, "the disclosure was altered, not injected verbatim"


async def test_multiple_gaps_are_all_named_with_display_labels(
    summary_returns, builders, gateway
):
    """
    A report degraded on several sources names each one, in investigator-facing
    vocabulary. Omitting any would understate what is missing.
    """
    summary_returns(SubAgentResult(
        status=SubAgentStatus.PARTIAL, answer_text="Thin summary [Document 1]",
        citations=[_citation()], tools_used=["RAG"], degraded_from=["GRAPH", "SQL"],
    ))

    await report_draft.run(_input(), gateway=gateway)

    injected = _disclosure_texts(builders["payload"])
    assert len(injected) == 1
    assert SOURCE_TOOL_DISPLAY_LABELS["GRAPH"] in injected[0]
    assert SOURCE_TOOL_DISPLAY_LABELS["SQL"] in injected[0]


def test_final_disclosures_carry_no_placeholder_marker():
    """
    Wording is approved. If a placeholder marker ever reappears in either
    string, it is on a path to being delivered verbatim to an investigator.
    """
    for text in (PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE, GRAPH_ONLY_SUMMARY_DISCLOSURE):
        assert "PLACEHOLDER" not in text
        assert "SIGN-OFF" not in text


# ── File generation failure ──────────────────────────────────────────────

async def test_builder_failure_abstains_with_explicit_error(
    summary_returns, builders, gateway, monkeypatch
):
    summary_returns(SubAgentResult(
        status=SubAgentStatus.OK, answer_text="Summary [Document 1]",
        citations=[_citation()], tools_used=["RAG"],
    ))

    def _boom(payload):
        raise RuntimeError("disk full")

    monkeypatch.setattr("src.generation.pdf_builder.build_pdf", _boom)

    result = await report_draft.run(_input(), gateway=gateway)

    assert result.status is SubAgentStatus.ABSTAINED
    assert result.generated_file is None
    assert result.error is not None
    assert "disk full" in result.error.message


async def test_unrecorded_file_is_a_failure_not_a_silent_success(
    summary_returns, builders, gateway, monkeypatch
):
    """Matches _generate_file(): a file that never got a DB record is an error."""
    summary_returns(SubAgentResult(
        status=SubAgentStatus.OK, answer_text="Summary [Document 1]",
        citations=[_citation()], tools_used=["RAG"],
    ))

    async def _no_id(_data):
        return None

    monkeypatch.setattr(gateway, "log_generated_file", _no_id)

    result = await report_draft.run(_input(), gateway=gateway)

    assert result.status is SubAgentStatus.ABSTAINED
    assert result.generated_file is None


async def test_structurer_failure_abstains(summary_returns, builders, gateway, monkeypatch):
    summary_returns(SubAgentResult(
        status=SubAgentStatus.OK, answer_text="Summary [Document 1]",
        citations=[_citation()], tools_used=["RAG"],
    ))

    async def _boom(content, requested_format):
        raise RuntimeError("structurer unavailable")

    monkeypatch.setattr("src.pipeline.file_structurer.structure_for_file", _boom)

    result = await report_draft.run(_input(), gateway=gateway)

    assert result.status is SubAgentStatus.ABSTAINED
    assert "structurer unavailable" in result.error.message


# ── Upstream outcomes and the boundary ───────────────────────────────────

@pytest.mark.parametrize("upstream", [
    SubAgentStatus.ABSTAINED, SubAgentStatus.EMPTY, SubAgentStatus.DENIED,
])
async def test_nothing_to_draft_propagates_upstream_status(
    upstream, summary_returns, builders, gateway
):
    summary_returns(SubAgentResult(status=upstream, answer_text=None))

    result = await report_draft.run(_input(), gateway=gateway)

    assert result.status is upstream
    assert result.generated_file is None


async def test_never_reinvokes_tools_directly(summary_returns, builders, gateway, monkeypatch):
    """
    [PRESERVE — design §3] Drafting from a degraded summary must NOT reach past
    the summarization boundary to "fill gaps". The tools are booby-trapped.
    """
    summary_returns(SubAgentResult(
        status=SubAgentStatus.PARTIAL, answer_text="Partial [Document 1]",
        citations=[_citation(tool="GRAPH")], tools_used=["GRAPH"], degraded_from=["RAG"],
        caveats=[GRAPH_ONLY_SUMMARY_DISCLOSURE],
    ))

    async def _explode(*a, **k):
        raise AssertionError("Report Drafting bypassed the summarization boundary")

    from src.pipeline.harness.tools import registry

    monkeypatch.setattr(registry, "rag_tool", _explode)
    monkeypatch.setattr(registry, "graph_tool", _explode)

    result = await report_draft.run(_input(), gateway=gateway)

    assert result.status is SubAgentStatus.PARTIAL


async def test_failing_verifier_produces_no_document_and_no_disclosure(
    summary_returns, builders, gateway
):
    """§2.1.3 step 3: a failed gate means no artifact at all, so nothing to qualify."""
    summary_returns(SubAgentResult(
        status=SubAgentStatus.PARTIAL,
        answer_text=f"{UNGROUNDED_TRIGGER} partial [Document 1]",
        citations=[_citation()], tools_used=["RAG"], degraded_from=["GRAPH"],
    ))

    result = await report_draft.run(_input(), gateway=gateway)

    assert result.status is SubAgentStatus.ABSTAINED
    assert result.generated_file is None
    assert builders["payload"] is None, "a document was built despite a failed gate"


async def test_chat_output_format_is_rejected(summary_returns, builders, gateway):
    result = await report_draft.run(_input(fmt="chat"), gateway=gateway)

    assert result.status is SubAgentStatus.ABSTAINED
    assert result.error.kind == "invalid_input"


# ── Bounded payload ──────────────────────────────────────────────────────

def test_subagent_result_has_no_field_that_can_hold_evidence():
    offenders = [
        name for name, field in SubAgentResult.model_fields.items()
        if "EvidenceChunk" in repr(field.annotation)
    ]
    assert not offenders, (
        f"SubAgentResult fields {offenders} can hold EvidenceChunk objects. "
        "Design §3: the bounded payload must never carry raw evidence upward."
    )


async def test_handoff_carries_a_file_reference_not_content(
    summary_returns, builders, gateway
):
    """
    [PRESERVE — design §3] The payload carries file_id/file_name/storage_path —
    matching _generate_file()'s shape — never file bytes or the payload dict.
    """
    summary_returns(SubAgentResult(
        status=SubAgentStatus.OK, answer_text="Summary [Document 1]",
        citations=[_citation()], tools_used=["RAG"],
    ))

    result = await report_draft.run(_input(), gateway=gateway)

    ref = result.generated_file
    assert set(ref.model_dump()) == {
        "file_id", "file_name", "storage_path", "disclosure_rendered"
    }
    assert not any(isinstance(c, EvidenceChunk) for c in result.citations)


# ── Trace ────────────────────────────────────────────────────────────────

async def test_emits_events_for_subagent_and_file_generation(
    summary_returns, builders, gateway
):
    summary_returns(SubAgentResult(
        status=SubAgentStatus.OK, answer_text="Summary [Document 1]",
        citations=[_citation()], tools_used=["RAG"],
    ))

    recorder = EventRecorder()
    await report_draft.run(_input(), events=recorder, gateway=gateway)

    steps = [e.step for e in recorder.events]
    assert f"subagent:{report_draft.NAME}" in steps
    assert "file_generation" in steps
