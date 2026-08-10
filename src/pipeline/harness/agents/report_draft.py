"""
Report Drafting sub-agent.

Turns Case Summarization's output into a PDF/XLSX/DOCX per
docs/SUBAGENT_INTERFACES.md §2.1, following §2.1.3's ordering contract.

THE FOUR RULES THIS FILE EXISTS TO GET RIGHT
────────────────────────────────────────────

1. IT CONSUMES CASE SUMMARIZATION, NOT TOOLS. [PRESERVE — design §3] If the
   summary came back degraded, this sub-agent drafts from whatever it returned
   and NEVER re-invokes RAG/GRAPH directly to "fill the gaps" — doing so would
   bypass the summarization boundary and defeat the layer's purpose. Case
   Summarization is treated as this sub-agent's single tool.

2. ORDERING IS FIXED (§2.1.3): summary -> compose EVIDENTIARY content ->
   Verifier -> on pass, build the document -> THEN inject the disclosure.
   Reversing the last two steps means submitting the disclosure to the
   grounding gate, where it has nothing to cite and could fail the report
   closed — withholding a document BECAUSE it was honest about being partial.

3. ONE DISCLOSURE PER GAP [RESOLVED-2a]. Case Summarization's GRAPH-only
   disclosure arrives already inside the inherited text. Adding
   PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE for that same gap would print two
   statements of one fact in a single document. A new disclosure is injected
   ONLY for gaps not already covered upstream.

4. THE DISCLOSURE GOES IN THE DOCUMENT BODY [RESOLVED-3]. Not the payload, not
   the logs. A generated report outlives the turn that produced it and is read
   by people who never see pipeline metadata — an investigator holding a PDF
   has no other way to know it was built on partial evidence.

NOT wired into supervisor routing yet, deliberately. Tracing is automatic via
the supervisor's completion hook — nothing to wire up here.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from src.pipeline.harness.contracts import (
    PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE,
    SOURCE_TOOL_DISPLAY_LABELS,
    GeneratedFileRef,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
)
from src.pipeline.harness.events import EventRecorder
from src.pipeline.harness.verifier_gate import verify_grounding

logger = logging.getLogger(__name__)

NAME = "report_draft"

_VALID_FILE_FORMATS = ("file_pdf", "file_xlsx", "file_docx")

_ABSTENTION_CAVEAT = (
    "I could not produce a report for this case from the available evidence."
)


def _sources_already_disclosed(summary: SubAgentResult) -> set[str]:
    """
    Which gaps Case Summarization already disclosed IN ITS OWN TEXT.

    [RESOLVED-2a] Its GRAPH-only disclosure names exactly one gap: missing
    document evidence, i.e. RAG. Derived from the caveats it mirrored rather
    than by substring-matching its prose, so a wording change to the
    (still-placeholder) disclosure string does not silently break suppression.
    """
    from src.pipeline.harness.contracts import GRAPH_ONLY_SUMMARY_DISCLOSURE

    if GRAPH_ONLY_SUMMARY_DISCLOSURE in (summary.caveats or []):
        return {"RAG"}
    return set()


def _undisclosed_gaps(summary: SubAgentResult) -> list[str]:
    """
    Gaps that need a NEW disclosure — those in `degraded_from` that Case
    Summarization has not already disclosed in the inherited text.

    Note a tool can appear in both `tools_used` and `degraded_from` (it
    contributed but degraded internally). Those still count as gaps worth
    disclosing: the report rests on unscreened evidence, which a reader needs
    to know even though the source did contribute.
    """
    already = _sources_already_disclosed(summary)
    return [t for t in (summary.degraded_from or []) if t not in already]


def _render_disclosure(gaps: list[str]) -> str:
    """
    Fill the disclosure template with INVESTIGATOR-FACING source names.

    `degraded_from` carries internal identifiers (`RAG`, `GRAPH`, `SQL`).
    Dropping those into a delivered document would read as noise — "the
    following evidence sources were unavailable: RAG". [RESOLVED-1a] already
    establishes `SOURCE_TOOL_DISPLAY_LABELS` as the user-facing vocabulary, so
    the same mapping applies here.

    Unknown values fall back to the raw name rather than being dropped: a
    disclosure that silently omits a source would be worse than one naming it
    awkwardly.
    """
    labels = [SOURCE_TOOL_DISPLAY_LABELS.get(g, g) for g in gaps]
    return PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE.format(
        unavailable_sources=", ".join(labels)
    )


async def run(
    agent_input: SubAgentInput,
    events: Optional[EventRecorder] = None,
    gateway: Any = None,
) -> SubAgentResult:
    """Execute Report Drafting. Returns a bounded `SubAgentResult`."""
    from src.pipeline.harness.agents import case_summary

    if events:
        await events.emit(
            f"subagent:{NAME}", "active", "Report Drafting: building case report"
        )

    output_format = agent_input.output_format
    if output_format not in _VALID_FILE_FORMATS:
        # Report Drafting exists to produce a document. A chat-shaped request
        # routed here is a supervisor bug, not something to paper over by
        # silently emitting prose.
        if events:
            await events.emit(
                f"subagent:{NAME}", "error",
                f"Report Drafting requires a file output_format, got {output_format!r}",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            error=ToolError(
                kind="invalid_input",
                message=(
                    f"Report Drafting requires one of {_VALID_FILE_FORMATS}, "
                    f"got {output_format!r}."
                ),
            ),
        )

    # Validated HERE, alongside output_format, rather than at the storage step.
    # `generated_files.session_id` is NOT NULL, so a report produced without a
    # session cannot be recorded — and an unrecorded report is undownloadable,
    # making it worthless however good its content. Checking upfront turns a
    # ~45s retrieve → generate → verify → build-PDF → fail into an immediate,
    # explainable refusal.
    if not agent_input.session_id:
        if events:
            await events.emit(
                f"subagent:{NAME}", "error",
                "Report Drafting requires a session to record the generated file",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            error=ToolError(
                kind="invalid_input",
                message=(
                    "Report Drafting requires session_id on SubAgentInput: the "
                    "generated file must be recorded against a session "
                    "(generated_files.session_id is NOT NULL) or it cannot be "
                    "downloaded."
                ),
            ),
        )

    # ── Step 1: consume Case Summarization (this sub-agent's only tool) ──
    summary = await case_summary.run(agent_input, events=events)

    # Nothing to draft from. Propagate the upstream outcome rather than
    # inventing a document — and never reach for tools directly to fill it.
    if summary.status in (SubAgentStatus.ABSTAINED, SubAgentStatus.EMPTY, SubAgentStatus.DENIED):
        if events:
            await events.emit(
                f"subagent:{NAME}", "done",
                f"Report Drafting: nothing to draft (summary was {summary.status.value})",
            )
        return SubAgentResult(
            status=summary.status,
            answer_text=None,
            tools_used=list(summary.tools_used),
            degraded_from=list(summary.degraded_from),
            caveats=list(summary.caveats),
            error=summary.error,
        )

    # ── Step 2: the report's EVIDENTIARY content ──
    # This is the summary's own answer_text, which already contains Case
    # Summarization's disclosure if it fired. Drafted from as-is.
    report_content = summary.answer_text or ""

    # ── Step 3: Verifier over EVIDENTIARY CONTENT ONLY (§2.1.3 step 3) ──
    # Case Summarization already gated its own content, but the report is a
    # separate delivered artifact and is gated in its own right — the same
    # discipline every route that answers from evidence follows.
    verdict = await verify_grounding(
        answer=report_content,
        cited_chunks=[c.model_dump() for c in summary.citations],
        case_id=agent_input.caller.active_case_id,
        cross_case_ids=None,
    )

    if not verdict["grounded"]:
        # [PRESERVE] No document is built, and no disclosure is produced —
        # there is no partial artifact to qualify (§2.1.3 step 3).
        if events:
            await events.emit(
                f"subagent:{NAME}", "error",
                f"Report Drafting: grounding check failed — {verdict['reason']}",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            citations=[],
            tools_used=[],
            degraded_from=sorted(set(summary.degraded_from) | set(summary.tools_used)),
            caveats=[_ABSTENTION_CAVEAT],
        )

    # ── Step 4: build the document (§2.1.3 step 4) ──
    inherited_partial = summary.status is SubAgentStatus.PARTIAL
    new_gaps = _undisclosed_gaps(summary) if inherited_partial else []

    try:
        file_ref = await _build_document(
            content=report_content,
            output_format=output_format,
            new_gaps=new_gaps,
            agent_input=agent_input,
            gateway=gateway,
            events=events,
        )
    except Exception as exc:
        # Matches _generate_file()'s handling: an explicit file-generation
        # error, never a silent partial success.
        logger.error("Report file generation failed: %s", exc)
        if events:
            await events.emit(
                f"subagent:{NAME}", "error", f"Failed to generate {output_format}: {exc}"
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            citations=[],
            tools_used=list(summary.tools_used),
            degraded_from=list(summary.degraded_from),
            caveats=[f"The report could not be generated: {exc}"],
            error=ToolError(
                kind="upstream_failure",
                message=f"Failed to generate {output_format}: {exc}",
            ),
        )

    # ── Result: degradation is INHERITED (§2.1.3) ──
    caveats = list(summary.caveats)
    if new_gaps:
        # Same rendering the document received, so the caveat and the document
        # body cannot drift apart.
        caveats.append(_render_disclosure(new_gaps))

    if events:
        await events.emit(
            f"subagent:{NAME}", "done",
            f"Report Drafting: {file_ref.file_name} ready"
            + (" (partial evidence disclosed)" if file_ref.disclosure_rendered else ""),
            sources=[{
                "filename": file_ref.file_name,
                "type": output_format.split("_")[1],
                "file_id": file_ref.file_id,
            }],
        )

    return SubAgentResult(
        status=SubAgentStatus.PARTIAL if inherited_partial else SubAgentStatus.OK,
        answer_text=report_content,
        citations=list(summary.citations),
        tools_used=list(summary.tools_used),
        degraded_from=list(summary.degraded_from),
        caveats=caveats,
        generated_file=file_ref,
    )


async def _build_document(
    content: str,
    output_format: str,
    new_gaps: list[str],
    agent_input: SubAgentInput,
    gateway: Any,
    events: Optional[EventRecorder],
) -> GeneratedFileRef:
    """
    Structure, inject the disclosure, build, and record the file.

    Mirrors `orchestrator.py::_generate_file()`'s shape — same builders, same
    filename/extension handling, same `log_generated_file` record — so the
    harness produces files indistinguishable from the legacy path's. Raises on
    failure; the caller turns that into an explicit abstention.
    """
    from src.data_gateway import get_gateway
    from src.generation.docx_builder import build_docx
    from src.generation.pdf_builder import build_pdf
    from src.generation.xlsx_builder import build_xlsx
    from src.pipeline.file_structurer import structure_for_file

    if events:
        await events.emit("file_generation", "active", f"Generating {output_format}...")

    payload = await structure_for_file(content, output_format)
    file_type = output_format.split("_")[1]  # 'pdf' | 'xlsx' | 'docx'

    # ── §2.1.3 step 5: inject the disclosure AFTER verification ──
    # Added to the structured payload rather than the prose so it survives into
    # every builder identically, and — critically — it is added AFTER
    # structure_for_file() has run, so the LLM never rewrites or paraphrases it.
    # It is a fixed reviewed string with one substitution; that is the entire
    # basis on which it is exempt from the grounding gate.
    disclosure_rendered = False
    if new_gaps:
        disclosure = _render_disclosure(new_gaps)
        sections = payload.get("sections")
        if isinstance(sections, list):
            sections.insert(0, {"type": "paragraph", "content": disclosure})
        else:
            payload["sections"] = [{"type": "paragraph", "content": disclosure}]
        disclosure_rendered = True

    if file_type == "pdf":
        filepath, size = build_pdf(payload)
    elif file_type == "xlsx":
        filepath, size = build_xlsx(payload)
    else:
        filepath, size = build_docx(payload)

    title = payload.get("title") or "Case Report"
    file_name = (
        title if title.lower().endswith(f".{file_type}") else f"{title}.{file_type}"
    )

    gw = gateway if gateway is not None else await get_gateway()
    file_id = await gw.log_generated_file({
        # Guaranteed non-None: `run()` refuses the request upfront without it,
        # because this column is NOT NULL and an unrecorded report cannot be
        # downloaded.
        "session_id": agent_input.session_id,
        "user_id": agent_input.caller.user_id,
        "case_id": agent_input.caller.active_case_id,
        "file_type": file_type,
        "file_name": file_name,
        "file_size_bytes": size,
        "storage_path": filepath,
    })
    if not file_id:
        raise RuntimeError("Failed to record the generated file in the database")

    if events:
        await events.emit("file_generation", "done", f"File ready: {file_name}")

    return GeneratedFileRef(
        file_id=str(file_id),
        file_name=file_name,
        storage_path=filepath,
        # [RESOLVED-3] Asserts the DOCUMENT discloses its partiality. Set only
        # here, by the code that actually wrote the line into the payload —
        # never optimistically by a caller that merely intended one.
        disclosure_rendered=disclosure_rendered,
    )
