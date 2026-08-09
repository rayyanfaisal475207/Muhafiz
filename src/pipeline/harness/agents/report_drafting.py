"""
Report Drafting sub-agent —
src/pipeline/harness/agents/report_drafting.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 7, "Phase 8" per this
session's brief).

Source of truth: SUBAGENT_INTERFACES.md §2.1's "Report Drafting" row,
§2.1.1's shared disclosure rules, §2.1.2 (Case Summarization's GRAPH-only
disclosure — inherited here), §2.1.3 (this sub-agent's own fixed 5-step
disclosure ordering + suppression rule), RESOLVED-3/RESOLVED-3a/RESOLVED-2a
in §3, and §0's `GeneratedFileRef`; AGENT_HARNESS_DESIGN.md §3's matching
table row (explicit PRESERVE: never re-invoke RAG/GRAPH tools directly to
"fill gaps"); AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 7 and §5's
trust-layer table.

SCOPE. Composes exactly:
  - `src.pipeline.harness.agents.case_summarization.case_summarization`,
    called directly as a function (NOT through the Supervisor, NOT via any
    tool) — per design §3's explicit PRESERVE, this sub-agent consumes
    Case Summarization's bounded `SubAgentResult`, never raw tools.
  - `src.pipeline.citation_consistency.check_citation_consistency` (Phase
    8's own new module — see the DISCREPANCY note below for why it exists
    this session at all).
  - `src.pipeline.verifier.verify_grounding` (unchanged signature).
  - `src.pipeline.file_structurer.structure_for_file` +
    `src.generation.{pdf_builder.build_pdf, xlsx_builder.build_xlsx,
    docx_builder.build_docx}` — the EXACT existing, unmodified functions
    `orchestrator.py::_generate_file()` already wraps. Confirmed by reading
    all four before writing this module: none of their signatures or
    return shapes needed to be assumed.

DISCREPANCY FLAGGED AND RESOLVED VIA ASKUSERQUESTION BEFORE ANY CODE WAS
WRITTEN (per this session's brief, matching every prior phase's rule):
`citation_consistency.py` did not exist in the repo at session start.
SUBAGENT_INTERFACES.md §2.1.3's own fixed 5-step ordering contract does
NOT itself name a citation-consistency step (Verifier -> build ->
disclosure, full stop) — only AGENT_HARNESS_IMPLEMENTATION_PLAN.md §5's
trust-layer table and §4 row 7's build note require it, and §8's checklist
lists it as its OWN separate, later, unchecked item. Offered two options
(defer with a `# TODO(citation-consistency-gate)` marker, matching how
`validation.py` has been deferred in every prior phase; or build it now).
**Resolved: build it now.** `src/pipeline/citation_consistency.py` is a new
module this session, invoked from this file per §5's exact placement
("Report Drafting, before the Verifier runs").

A SECOND GAP, SURFACED WHILE CHECKING THE CONTRACT, RESOLVED THE SAME WAY:
`_generate_file()`'s existing behavior persists a real, durable `file_id`
via `gateway.log_generated_file(session_id, user_id, case_id, ...)`, but no
harness type carried a `session_id` anywhere and no sub-agent built through
Phase 7 touched `DataGateway` at all. Resolved as a full contract amendment
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §13, merged to main BEFORE this
branch was cut) — `ExecutionContext.session_id` (additive) and a new
keyword-only `gateway: Optional[DataGateway] = None` parameter on every
`SubAgent.__call__`, mirroring `on_event`'s own §12 amendment exactly. See
`_persist_generated_file()` below for the fallback this sub-agent takes
when `gateway` or `session_id` is unavailable.

── THE CORE DESIGN QUESTION THIS SESSION HAD TO RESOLVE: WHAT DOES "REPORT
   DRAFTING RECOMPOSES CASE SUMMARIZATION'S OUTPUT" ACTUALLY DO? ─────────

§2.1.3 step 2 says "Report Drafting composes the report's EVIDENTIARY
content from that payload" and step 3 requires a REAL Verifier run with a
REAL fail branch ("fails -> ABSTAINED. No document is built."). That is
only a meaningful requirement if step 2 produces something NEW that could
fail grounding — a straight, unmodified passthrough of Case Summarization's
already-verified `answer_text` would have nothing left to verify (the same
"nothing generated, nothing to verify" logic Timeline Building's
deterministic `description`/`answer_text` already relies on, per §9's
Phase 5 entry) and step 3's fail branch would be dead code.

So this module DOES run one new LLM generation pass: a formal report draft
composed from Case Summarization's evidentiary text. That collides with a
real contract constraint: `Citation` (SUBAGENT_INTERFACES.md §2.0) is
"deliberately NOT an EvidenceChunk: it carries no chunk text" — and Report
Drafting, per design §3, only ever receives Case Summarization's bounded
`SubAgentResult` (never its internal `EvidenceChunk` list — that boundary
holds for EVERY caller of `case_summarization()`, not only the Supervisor).
`verify_grounding()` needs real chunk TEXT to judge grounding against.

RESOLVED (this session, documented here rather than re-asked, since the
two AskUserQuestion rounds already spent this session settled the two
genuinely open product/architecture questions — this is an implementation
detail within the settled contract, the same kind of call Case
Summarization's/Timeline Building's own sessions made and documented
in-line): collapse to a SINGLE synthetic grounding source. The entirety of
Case Summarization's own evidentiary text (already Verifier-passed once,
by Case Summarization itself) is treated as ONE trusted chunk, `[Document
1]`. The drafting prompt instructs the model to cite ONLY `[Document 1]`
and never introduce another reference. This is the exact "synthetic
single/multi-chunk wrapper" pattern design §5 already documents for
SQL/XAGG/XNETWORK ("construct synthetic single/multi-chunk wrappers
specifically to preserve [the Verifier's flat-list] invariant") — not a
new mechanism. `check_citation_consistency()` (new this session) then has
real, well-scoped teeth: with exactly one valid citation index, any
`[Document N]` the redraft invents with `N != 1` is caught deterministically
before the Verifier ever runs.

DISCLOSURE HANDLING — SUBAGENT_INTERFACES.md §2.1.3's EXACT 5 STEPS, WITH
CITATION-CONSISTENCY INSERTED BEFORE STEP 3 PER PLAN §5:
  1. Case Summarization returns its bounded payload (status, degraded_from,
     answer_text -- which ALREADY CONTAINS §2.1.2's GRAPH-only disclosure,
     prepended, if Case Summarization degraded in that direction).
  2. Compose evidentiary content: strip any inherited disclosure prefix
     (detected structurally via `degraded_from == ["RAG"]`, Case
     Summarization's own exact signal for "the GRAPH-only branch ran and
     prepended its disclosure" -- see `_strip_inherited_disclosure()` --
     never by string-matching the provisional wording, which is still
     [PROVISIONAL — PENDING PRODUCT SIGN-OFF] and must not be relied on as
     a stable string) to get the pure case-summary content, then draft a
     new, single-citation-regime report body from it via one LLM call.
  2.5. [PLAN §5's OWN PLACEMENT] `check_citation_consistency()` on the
       drafted text, BEFORE the Verifier runs.
       - fails -> ABSTAINED, explicit citation-consistency error, no
         document built.
  3. `verify_grounding()` over the drafted text against the ONE synthetic
     chunk described above -- the EVIDENTIARY CONTENT ONLY, never the
     disclosure (which is never generated at this point -- see step 5).
       - fails -> ABSTAINED. No document is built. No disclosure produced.
  4. Document builder assembles the file: `structure_for_file()` (the
     EXISTING, unmodified LLM-based structuring step `_generate_file()`
     already uses) turns the VERIFIED drafted text into the JSON payload;
     `build_pdf`/`build_xlsx`/`build_docx` render it. Any exception from
     either step -> ABSTAINED with an explicit file-generation error,
     matching `_generate_file()`'s own `except Exception` shape verbatim
     -- NOT the same failure mode as a Verifier rejection above.
  5. IF `status == PARTIAL`: inject a disclosure line, chosen per the
     suppression rule below, into the ALREADY-BUILT payload's `sections`
     list via plain dict insertion -- NEVER through `structure_for_file()`'s
     LLM pass, which could paraphrase a fixed template string that must
     never be paraphrased (SUBAGENT_INTERFACES.md §2.1.1 rule 2). Set
     `GeneratedFileRef.disclosure_rendered = True`.

SUPPRESSION RULE (RESOLVED-2a / §2.1.3), IMPLEMENTED STRUCTURALLY, NOT BY
STRING-MATCHING THE PROVISIONAL WORDING:
  - `degraded_from == ["RAG"]` (Case Summarization's own GRAPH-only
    branch) -> the gap is ALREADY disclosed in the inherited text. Do NOT
    inject `PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE`; instead inject the SAME
    `GRAPH_ONLY_SUMMARY_DISCLOSURE` string the disclosure was stripped from
    in step 2 -- this is what "propagates forward" concretely means once
    the pure evidentiary text has been separated from its own disclosure
    line for the drafting/verification steps. `disclosure_rendered=True`
    either way -- it asserts the DOCUMENT discloses its partiality, not who
    authored the line (§2.1.3's own wording).
  - `degraded_from == ["GRAPH"]` (Case Summarization's RAG-only branch,
    which per §2.1.2 needs NO in-text disclosure at its own level) -> this
    is a gap Report Drafting itself is the first to disclose. Inject
    `PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE`, substituting
    `{unavailable_sources}` from `degraded_from` routed through
    `SOURCE_TOOL_DISPLAY_LABELS` (plan §7.4's own explicit requirement --
    never the raw enum value).
  - `degraded_from == []` with `status == PARTIAL` cannot occur for
    anything Case Summarization itself returns today (its only two PARTIAL
    branches are exactly the two above) -- handled defensively below by
    falling through to the generic template rather than asserting.

VALIDATION GATE -- EXPLICITLY NOT WIRED THIS PHASE, same as every prior
sub-agent. `validation.py` does not exist yet. A `# TODO(validation-gate)`
marker is left at its intended insertion point -- per plan §7.1's ordering
(Verifier -> Citation-Consistency [this sub-agent only] -> Validation) AND
per plan §5's own note that Report Drafting needs Validation's FULL
semantic-tier check, not the lighter structural one -- flagged explicitly
below, not silently left at the lighter tier by omission.

NOT IN SCOPE THIS PHASE: any other sub-agent, `validation.py` itself, live
wiring into main.py/orchestrator.py/router.py (still not switched on, per
plan §6's rollout strategy -- `_generate_file()` keeps serving live file
generation until this sub-agent has its own proven go-live).
"""

from __future__ import annotations

import logging
import uuid
from typing import Optional

from src.data_gateway.base import DataGateway
from src.generation.docx_builder import build_docx
from src.generation.pdf_builder import build_pdf
from src.generation.xlsx_builder import build_xlsx
from src.llm.client import call_llm
from src.pipeline.citation_consistency import check_citation_consistency
from src.pipeline.file_structurer import structure_for_file
from src.pipeline.harness.agents.case_summarization import case_summarization
from src.pipeline.harness.supervisor import REPORT_DRAFTING, register
from src.pipeline.harness.types import (
    Citation,
    GeneratedFileRef,
    GRAPH_ONLY_SUMMARY_DISCLOSURE,
    OnEventCallback,
    PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE,
    SOURCE_TOOL_DISPLAY_LABELS,
    SourceTool,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
)
from src.pipeline.verifier import verify_grounding

logger = logging.getLogger(__name__)

# `output_format` -> the bare file-type token `_generate_file()`/
# `structure_for_file()`/the builders all key on. Mirrors
# `orchestrator.py::_generate_file()`'s own `output_format.split('_')[1]`
# exactly, as an explicit table rather than a repeated string-split, so an
# invalid `output_format` (e.g. "chat", reaching this sub-agent only via a
# caller bypassing Supervisor.classify_to_subagent()'s override) is a clean
# lookup miss, not an IndexError.
_FILE_TYPE_BY_FORMAT: dict[str, str] = {
    "file_pdf": "pdf",
    "file_xlsx": "xlsx",
    "file_docx": "docx",
}
_BUILDERS = {"pdf": build_pdf, "xlsx": build_xlsx, "docx": build_docx}

# Single-citation-regime drafting prompt -- see module docstring's "CORE
# DESIGN QUESTION" section for why exactly one synthetic source is used.
_DRAFT_SYSTEM_PROMPT_TEMPLATE = (
    "You are drafting a formal investigative case report for a police "
    "records system. Using ONLY the case summary material below, write a "
    "well-organized report in clear prose, with section breaks where the "
    "material supports them (e.g. status, key entities, key events, open "
    "questions).\n\n"
    "There is exactly ONE source available to you. Cite every factual "
    "claim as [Document 1]. Do NOT invent, number, or cite any other "
    "document reference -- there is nothing else to cite.\n\n"
    "Respond in {preferred_language}.\n\n"
    "--- CASE SUMMARY MATERIAL ([Document 1]) ---\n{summary_text}\n"
    "--- END OF CASE SUMMARY MATERIAL ---"
)


def _generation_role(preferred_language: Optional[str]) -> str:
    """Mirrors every other sub-agent's own inline helper -- no cross-module
    coupling. See case_summarization.py's identical helper for the finding
    this documents (Urdu-tuned local generation-slot model ignores an
    explicit "reply in English" instruction)."""
    return "generation" if preferred_language == "Urdu" else "reasoning"


def _strip_inherited_disclosure(answer_text: str, degraded_from: list[SourceTool]) -> str:
    """
    Remove Case Summarization's own prepended GRAPH-only disclosure (see
    case_summarization.py's `disclosed_answer = f"{GRAPH_ONLY_SUMMARY_
    DISCLOSURE}\\n\\n{answer}"`) from the text handed to the drafting
    LLM/Verifier, so a meta-statement about the generation process is never
    mistaken for case content to draft from or grounded against -- exactly
    the failure §2.1.1's shared rules exist to prevent, one level up.

    Detected STRUCTURALLY via `degraded_from == ["RAG"]` -- Case
    Summarization's own single, exact signal for "the GRAPH-only branch
    ran" -- never by matching the disclosure's own text, which is still
    [PROVISIONAL — PENDING PRODUCT SIGN-OFF] (types.py) and must not be
    treated as a stable string to search for.
    """
    if degraded_from == ["RAG"] and answer_text.startswith(GRAPH_ONLY_SUMMARY_DISCLOSURE):
        return answer_text[len(GRAPH_ONLY_SUMMARY_DISCLOSURE):].lstrip("\n")
    return answer_text


def _disclosure_line_for(status: SubAgentStatus, degraded_from: list[SourceTool]) -> Optional[str]:
    """
    The suppression rule (RESOLVED-2a / §2.1.3), implemented structurally.
    Returns None when no disclosure applies (status != PARTIAL).
    """
    if status != SubAgentStatus.PARTIAL:
        return None
    if degraded_from == ["RAG"]:
        # Already disclosed upstream -- propagate the SAME line forward,
        # never a second, different-worded statement of the same gap.
        return GRAPH_ONLY_SUMMARY_DISCLOSURE
    # Covers degraded_from == ["GRAPH"] (Case Summarization's RAG-only
    # branch, no upstream disclosure) and the defensive fallback for any
    # other/future PARTIAL cause -- both are gaps Report Drafting is the
    # first to disclose.
    unavailable = ", ".join(
        SOURCE_TOOL_DISPLAY_LABELS.get(tool, tool) for tool in degraded_from
    ) or "part of the case material"
    return PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE.format(unavailable_sources=unavailable)


def _inject_disclosure_into_payload(payload: dict, disclosure_line: str, file_type: str) -> None:
    """
    Deterministic dict mutation ONLY -- never through `structure_for_file()`'s
    LLM pass, so the fixed disclosure template is never paraphrased
    (SUBAGENT_INTERFACES.md §2.1.1 rule 2).

    Covers all three builders, confirmed by reading pdf_builder.py/
    xlsx_builder.py/docx_builder.py before writing this function, not
    assumed:
      - PDF/DOCX render every "paragraph" section verbatim, so a leading
        paragraph section is sufficient for both.
      - XLSX is a real gap otherwise: `build_xlsx()` ONLY EVER renders the
        payload's first "table" section (headers+rows) or, if none exists,
        `payload["description"]` as a single "Content" cell -- it never
        looks at "paragraph" sections at all. A leading paragraph section
        alone would be silently dropped the moment the drafted report
        structures into a table, making `disclosure_rendered=True` an
        empty promise for that format. Both of xlsx's own paths are
        covered here (description AND, if a table section exists, a
        leading disclosure row in that same table) so the field's
        contract -- "written INTO THE DOCUMENT BODY... must never be set
        optimistically" -- actually holds for every output format, not
        only the two builders that happen to support arbitrary paragraphs.
    """
    sections = payload.setdefault("sections", [])
    sections.insert(0, {"type": "paragraph", "content": disclosure_line})
    payload["description"] = (
        f"{disclosure_line}\n\n{payload['description']}"
        if payload.get("description")
        else disclosure_line
    )
    if file_type == "xlsx":
        table_section = next((s for s in sections if s.get("type") == "table"), None)
        if table_section is not None:
            headers = table_section.get("headers") or []
            rows = table_section.get("rows") or []
            width = len(headers) if headers else max((len(r) for r in rows), default=1)
            disclosure_row = [disclosure_line] + [""] * max(0, width - 1)
            table_section.setdefault("rows", []).insert(0, disclosure_row)


def _propagate_terminal_status(summary_result: SubAgentResult) -> SubAgentResult:
    """
    Case Summarization returned EMPTY / ABSTAINED / DENIED -- there is
    nothing safe or meaningful to draft a report from. [PRESERVE — design
    §3] Never re-invoke tools directly to fill the gap; simply propagate
    the same status/caveats/error Case Summarization already produced. No
    document is built, no `generated_file` is set.
    """
    return SubAgentResult(
        status=summary_result.status,
        answer_text=None,
        caveats=list(summary_result.caveats),
        error=summary_result.error,
    )


async def _persist_generated_file(
    *,
    gateway: Optional[DataGateway],
    session_id: Optional[str],
    user_id: Optional[str],
    case_id: Optional[str],
    file_type: str,
    file_name: str,
    file_size: int,
    storage_path: str,
) -> str:
    """
    Mirrors `_generate_file()`'s existing `gateway.log_generated_file()`
    call exactly when the pieces it needs are actually available. Falls
    back to a locally generated id (never persisted) when `gateway` was not
    supplied, or when `session_id`/`user_id` are missing -- both required
    by `DataGateway.log_generated_file()`'s existing contract
    (`src/data_gateway/direct_backend.py` unconditionally constructs a
    `uuid.UUID` from each). This mirrors every prior sub-agent's own
    posture of never crashing on an absent optional collaborator; a missing
    durable record is logged, not swallowed silently.
    """
    if gateway is not None and session_id and user_id:
        try:
            return await gateway.log_generated_file(
                {
                    "session_id": session_id,
                    "user_id": user_id,
                    "case_id": case_id,
                    "file_type": file_type,
                    "file_name": file_name,
                    "file_size_bytes": file_size,
                    "storage_path": storage_path,
                }
            )
        except Exception as exc:
            logger.error("Report Drafting: gateway.log_generated_file() failed: %s", exc)
    else:
        logger.warning(
            "Report Drafting: no gateway/session_id/user_id available -- "
            "generated file will not be recorded in Postgres. "
            "gateway=%s session_id=%r user_id=%r",
            gateway is not None, session_id, user_id,
        )
    return str(uuid.uuid4())


async def report_drafting(
    agent_input: SubAgentInput,
    *,
    on_event: Optional[OnEventCallback] = None,
    gateway: Optional[DataGateway] = None,
) -> SubAgentResult:
    """
    The Report Drafting sub-agent. See module docstring for the full
    contract. `on_event` accepted for `SubAgent` protocol conformance and
    otherwise ignored this session (no per-source composition here, unlike
    Investigative Analysis -- Report Drafting composes exactly one upstream
    sub-agent call, nothing granular to report per-source).
    """
    caller = agent_input.execution.caller
    file_type = _FILE_TYPE_BY_FORMAT.get(agent_input.output_format)
    if file_type is None:
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=ToolError(
                kind="invalid_input",
                message=(
                    f"Report Drafting requires output_format in "
                    f"{sorted(_FILE_TYPE_BY_FORMAT)!r}; got "
                    f"{agent_input.output_format!r}."
                ),
            ),
            caveats=["Report Drafting was invoked without a valid file output format."],
        )

    # ── Step 1: Case Summarization's bounded payload ──────────────────────
    # [PRESERVE — design §3] Direct function call, not a tool re-invocation.
    summary_input = agent_input.model_copy(update={"output_format": "chat"})
    summary_result = await case_summarization(summary_input, on_event=None, gateway=None)

    if summary_result.status in (
        SubAgentStatus.EMPTY,
        SubAgentStatus.ABSTAINED,
        SubAgentStatus.DENIED,
    ):
        return _propagate_terminal_status(summary_result)

    if not summary_result.answer_text:
        # Defensive: OK/PARTIAL contractually implies answer_text is set
        # (SubAgentResult's own docstring), but never trust that blindly
        # across a module boundary.
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=["Case Summarization returned no content to draft a report from."],
        )

    # ── Step 2: compose evidentiary content ────────────────────────────────
    evidentiary_summary = _strip_inherited_disclosure(
        summary_result.answer_text, summary_result.degraded_from
    )
    resolved_language = caller.preferred_language or "the same language as the user's question"
    draft_system_prompt = _DRAFT_SYSTEM_PROMPT_TEMPLATE.format(
        preferred_language=resolved_language,
        summary_text=evidentiary_summary,
    )
    try:
        drafted_text = await call_llm(
            draft_system_prompt,
            agent_input.query_text,
            role=_generation_role(caller.preferred_language),
        )
    except Exception as exc:
        logger.error("Report Drafting: draft generation failed: %s", exc)
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=["Report draft generation failed; no document could be produced."],
        )

    # ── Step 2.5 [plan §5's placement]: citation-consistency, before the
    #     Verifier ─────────────────────────────────────────────────────────
    consistency = check_citation_consistency(drafted_text, valid_citation_count=1)
    if not consistency.consistent:
        logger.warning("Report Drafting: citation-consistency check failed: %s", consistency.reason)
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=ToolError(kind="upstream_failure", message=consistency.reason),
            caveats=[
                "The drafted report's citations could not be verified as consistent "
                "with the case summary it was drafted from."
            ],
        )

    # ── Step 3: Verifier, over the evidentiary content only ────────────────
    representative_tool: SourceTool = (
        summary_result.tools_used[0] if summary_result.tools_used else "RAG"
    )
    synthetic_chunk = {
        "id": "case-summary",
        "text": evidentiary_summary,
        "metadata": {"source_tool": representative_tool, "case_id": caller.active_case_id},
    }
    verification = await verify_grounding(
        answer=drafted_text,
        cited_chunks=[synthetic_chunk],
        case_id=caller.active_case_id,
    )
    if not verification.get("grounded", False) or verification.get("off_topic", False):
        logger.warning(
            "Report Drafting: verifier rejected drafted report: %s",
            (verification.get("reason") or "")[:150],
        )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=[
                "The drafted report could not be verified as grounded in the case "
                "summary it was drafted from."
            ],
        )

    # TODO(validation-gate): Validation plugs in here, per
    # AGENT_HARNESS_IMPLEMENTATION_PLAN.md §7.1's ordering (Verifier ->
    # Citation-Consistency -> Validation) -- AFTER the Verifier passes,
    # BEFORE the document is assembled. `validation.py` does not exist yet.
    # NOTE: plan §5 requires the FULL semantic-tier check here (same tier as
    # Cross-Case Linkage / Investigative Analysis), NOT the lighter
    # structural-only check used elsewhere -- flagged explicitly so a
    # future session doesn't default to the cheaper tier by omission.

    # ── Step 4: document builder assembles the file ────────────────────────
    try:
        payload = await structure_for_file(drafted_text, file_type)
        builder = _BUILDERS[file_type]

        # ── Step 5: disclosure, injected AFTER verification, NEVER through
        #     structure_for_file()'s LLM pass (never paraphrased) ──────────
        disclosure_line = _disclosure_line_for(summary_result.status, summary_result.degraded_from)
        disclosure_rendered = False
        if disclosure_line is not None:
            _inject_disclosure_into_payload(payload, disclosure_line, file_type)
            disclosure_rendered = True

        filepath, file_size = builder(payload)
    except Exception as exc:
        logger.error("Report Drafting: file generation failed: %s", exc)
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=ToolError(kind="upstream_failure", message=f"File generation failed: {exc}"),
            caveats=[f"Failed to generate the {file_type} report."],
        )

    title = payload.get("title") or "Case Report"
    file_name = title if title.lower().endswith(f".{file_type}") else f"{title}.{file_type}"

    file_id = await _persist_generated_file(
        gateway=gateway,
        session_id=agent_input.execution.session_id,
        user_id=caller.user_id,
        case_id=caller.active_case_id,
        file_type=file_type,
        file_name=file_name,
        file_size=file_size,
        storage_path=filepath,
    )

    return SubAgentResult(
        status=summary_result.status,
        answer_text=drafted_text,
        citations=[
            Citation(
                document_index=1,
                source_tool=representative_tool,
                case_id=caller.active_case_id,
            )
        ],
        tools_used=summary_result.tools_used,
        degraded_from=summary_result.degraded_from,
        caveats=list(summary_result.caveats),
        generated_file=GeneratedFileRef(
            file_id=str(file_id),
            file_name=file_name,
            storage_path=filepath,
            disclosure_rendered=disclosure_rendered,
        ),
    )


report_drafting.name = REPORT_DRAFTING

# Import-time self-registration -- the same pattern every prior sub-agent
# module used (supervisor.py's own module docstring documents it).
register(report_drafting)
