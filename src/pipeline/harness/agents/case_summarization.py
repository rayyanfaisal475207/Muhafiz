"""
Case Summarization sub-agent —
src/pipeline/harness/agents/case_summarization.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 3, "Phase 4" per this session's
brief).

Source of truth: SUBAGENT_INTERFACES.md §2.1's "Case Summarization" row +
§2.1.2's exact GRAPH-only disclosure ordering + RESOLVED-2/RESOLVED-2a in
§3, AGENT_HARNESS_DESIGN.md §3's matching row, and this session's explicit
brief.

SCOPE. Composes exactly two tools:
  - `src.pipeline.harness.tools.rag.rag_tool` (Phase 0), case-scoped, called
    as-is.
  - `src.pipeline.harness.tools.graph.graph_tool` (Phase 0), case-scoped,
    `hybrid=False` (this sub-agent composes RAG and GRAPH itself at the
    sub-agent level — requesting GRAPH_HYBRID's internal fusion here would
    bypass that composition, not perform it), `max_hops` explicitly CAPPED
    below the tool's own maximum (`MAX_HOPS=3`) at `DEFAULT_HOPS=2` — per
    this session's brief: "this is a summary, not deep traversal." Neither
    tool carries a role gate (case-assignment-based scoping only, design
    §2.1/§2.2) — `SubAgentStatus.DENIED` is therefore never built as an
    output path here, per this session's explicit instruction.

FLATTENING (design §5; the first sub-agent for which this is a concrete,
not abstract, concern). Whenever both tools contribute usable chunks, they
are concatenated into ONE ordered list — RAG chunks first, then GRAPH
chunks — before the generation prompt is built. The exact same list, same
order, is what gets handed to `verify_grounding()` afterward, so the
generator's `[Document N]` citations and the Verifier's positional
`chunks[n-1]` indexing agree by construction. Each tool wrapper already
tags `metadata.source_tool` correctly (`"RAG"` / `"GRAPH"`) — this module
never re-tags a chunk, only concatenates the two already-tagged lists.

SYMMETRIC DEGRADATION — the crux of this sub-agent (RESOLVED-2/RESOLVED-2a):

  - GRAPH empty/failed, RAG usable -> ordinary RAG-based summary. This is
    the shape a user expects by default, so NO in-text disclosure is added
    (§2.1.2: "needs no in-text disclosure; `status` and `degraded_from`
    carry it"). `status=PARTIAL`, `degraded_from=["GRAPH"]`,
    `tools_used=["RAG"]`.
  - RAG empty/failed, GRAPH usable -> follows §2.1.2's ordering EXACTLY:
      1. RAG fails/empty; GRAPH returns usable data (established above).
      2. Compose the summary's EVIDENTIARY content from GRAPH output alone.
      3. Run `verify_grounding()` over that evidentiary content ONLY.
           - fails -> ABSTAINED. No summary served. No disclosure produced.
           - passes -> continue.
      4. PREPEND `GRAPH_ONLY_SUMMARY_DISCLOSURE` (types.py; provisional
         wording per plan §7.4 — see that constant's own docstring) to
         `answer_text`. Never re-verified, never regenerated, never
         paraphrased — a fixed string concatenation, nothing else.
      5. Return: `status=PARTIAL`, `degraded_from=["RAG"]`,
         `tools_used=["GRAPH"]`.
  - Both tools empty/failed -> `status=EMPTY`, not an error.
  - Both tools usable -> combined summary over the flattened RAG+GRAPH
    list; Verifier runs over that combined evidentiary content.
    `status=OK`, `tools_used=["RAG", "GRAPH"]`, `degraded_from=[]`.
  - NEITHER direction may ever produce `ABSTAINED` while real evidence
    exists on the surviving side (RESOLVED-2) — only a genuine Verifier
    rejection of the actual evidentiary content aborts to `ABSTAINED`, in
    every branch above (including the both-usable combined case, which
    §2.1.2 does not special-case but which inherits the same
    generic-evidentiary-content rule every other sub-agent's Verifier
    boundary already follows).

DEFENSIVE EXCEPTION HANDLING — flagged, not silent. `rag_tool()` has an
explicit `status=FAILED` path for retrieval-infrastructure exceptions
(rag.py's own `_retrieve_candidates` try/except). `graph_tool()`, as of
this session's read of tools/graph.py, has NO equivalent try/except around
its own `retrieve_graph()` calls — an infrastructure exception there
propagates uncaught out of `graph_tool()` today. Composing two tools whose
own failure-shapes are asymmetric (one always returns a typed FAILED
result, the other can raise) means this sub-agent MUST catch stray
exceptions from either tool call itself to implement graceful, exhaustive
degradation without crashing — the same "defensive branch for a status/
failure-mode the composed tool doesn't currently produce" pattern
`semantic_search.py`/`large_scale_aggregate.py` already established for
DENIED/EMPTY branches their own composed tools don't currently reach. A
caught exception is treated exactly like a `ToolStatus.FAILED` result for
this sub-agent's own degradation logic (see `_ToolOutcome`/`_run_rag`/
`_run_graph` below). This is a defensive addition on THIS module's side,
not a fix to `tools/graph.py` itself — modifying an already-merged Phase 0
tool wrapper is out of this session's scope.

CONCURRENCY. RAG and GRAPH are independent tool calls with no data
dependency between them, so they run concurrently via `asyncio.gather` —
an efficiency choice, not a preservation requirement; nothing in the design
docs mandates or forbids it for this sub-agent (contrast Investigative
Analysis, AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 6, where parallel
execution is an explicit build note).

BOUNDED PAYLOAD (SUBAGENT_INTERFACES.md §2.1's table + this session's
brief): one structured summary — status / key entities / key events / open
questions — rendered as organized prose/sections INSIDE `answer_text`.
There is no precedent anywhere in the contract for a sub-agent-specific
typed field on `SubAgentResult`, and this session's brief is explicit that
none should be invented here; the four-section structure is a generation
PROMPT instruction, not a schema change. Never `EvidenceChunk`/raw rows —
the flattened chunk list stays inside this function's stack frame exactly
as it does in `semantic_search.py`/`large_scale_aggregate.py`.

VALIDATION GATE — EXPLICITLY NOT WIRED THIS PHASE, same as every prior
sub-agent. `validation.py` does not exist yet and `SubAgentResult` has no
`validation_status` field. A `# TODO(validation-gate)` marker (the
Large-Scale Aggregate session's renamed convention, not the earlier
`phase-3` naming) is left at its intended insertion point below — after
the Verifier resolves (pass, or the GRAPH-only disclosure has been
prepended), before the result is returned.

NOT IN SCOPE THIS PHASE: any other sub-agent, `validation.py` itself, live
wiring into main.py/orchestrator.py/router.py.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from src.llm.client import call_llm
from src.pipeline.harness.supervisor import CASE_SUMMARIZATION, register
from src.pipeline.harness.tools.graph import GraphToolInput, graph_tool
from src.pipeline.harness.tools.rag import RagToolInput, rag_tool
from src.pipeline.harness.types import (
    Citation,
    EvidenceChunk,
    GRAPH_ONLY_SUMMARY_DISCLOSURE,
    OnEventCallback,
    SourceTool,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
    ToolStatus,
)
from src.retrieval.graph_retriever import DEFAULT_HOPS
from src.pipeline.verifier import verify_grounding

logger = logging.getLogger(__name__)

# Explicitly NOT MAX_HOPS (3, graph_retriever.py) — a capped traversal
# depth, per this session's brief: "this is a summary, not deep
# traversal." DEFAULT_HOPS (2) already satisfies "capped, not maximum";
# named here rather than left as an implicit default so the cap is visible
# at the call site, not just inherited silently.
_SUMMARY_MAX_HOPS = DEFAULT_HOPS

# Self-contained, sub-agent-scoped prompt — same convention Semantic Search
# established (semantic_search.py's own module docstring): no coupling to
# orchestrator.py's own prompt-assembly machinery. Structures the answer
# into the four bounded sections this session's brief specifies, still
# enforcing the same [Document N] citation contract verify_grounding()'s
# deterministic checks depend on.
_SYSTEM_PROMPT_TEMPLATE = (
    "You are a police case-summarization assistant. Using ONLY the case "
    "material provided below, produce a structured case summary with "
    "these four sections, in this order:\n"
    "  1. Status — the case's current state (e.g. open, closed, under "
    "investigation), if the material indicates one.\n"
    "  2. Key Entities — people, vehicles, phone numbers, organizations, "
    "or other named entities involved.\n"
    "  3. Key Events — significant events, ordered as best fits the case "
    "narrative.\n"
    "  4. Open Questions — gaps, contradictions, or unresolved leads the "
    "material does not settle.\n\n"
    "Every factual claim MUST cite its source as [Document N], where N is "
    "that document's 1-based position below. If a section has nothing to "
    "report from the material given, say so plainly rather than guessing "
    "or inventing content.\n\n"
    "Respond in {preferred_language}.\n\n"
    "{conversation_block}"
    "--- CASE MATERIAL ---\n{documents}\n--- END OF CASE MATERIAL ---"
)


def _generation_role(preferred_language: Optional[str]) -> str:
    """
    Mirrors semantic_search.py's/large_scale_aggregate.py's own inline
    `_generation_role()` rather than importing either — no cross-sub-agent
    coupling, per this session's scope. Same finding both modules already
    document: the Urdu-fine-tuned local generation-slot model ignores an
    explicit "reply in English" instruction, so non-Urdu responses route
    through the reasoning slot instead.
    """
    return "generation" if preferred_language == "Urdu" else "reasoning"


def _format_documents_for_prompt(chunks: list[EvidenceChunk]) -> str:
    """
    [Document N] numbering here MUST match `Citation.document_index` and
    `verify_grounding()`'s positional `chunks[n-1]` indexing below — same
    list, same order, per EvidenceChunk's [PRESERVE — design §5] contract.
    """
    parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        source = chunk.metadata.source_file or "unknown"
        parts.append(f"[Document {i}] Source: {source}\n{chunk.text}")
    return "\n\n".join(parts)


def _chunk_to_verifier_dict(chunk: EvidenceChunk) -> dict:
    """
    Flatten one EvidenceChunk into verify_grounding()'s pre-existing flat
    dict shape — same reshape semantic_search.py's/large_scale_aggregate.py's
    own helpers perform, not a new interface (design §5 decision (a)).
    """
    return {
        "id": chunk.id,
        "text": chunk.text,
        "metadata": chunk.metadata.model_dump(),
    }


def _citations_for(chunks: list[EvidenceChunk]) -> list[Citation]:
    return [
        Citation(
            document_index=i,
            source_tool=chunk.metadata.source_tool,
            case_id=chunk.metadata.case_id,
            source_file=chunk.metadata.source_file,
            confidence=chunk.metadata.confidence,
        )
        for i, chunk in enumerate(chunks, start=1)
    ]


@dataclass
class _ToolOutcome:
    """
    Local, sub-agent-internal normalization of a composed tool's result —
    NOT a harness type, never crosses this module's boundary. Exists only
    so RAG's and GRAPH's asymmetric failure shapes (typed FAILED result vs.
    a stray exception — see module docstring's "DEFENSIVE EXCEPTION
    HANDLING" note) can be branched on uniformly below.
    """

    usable: bool  # status == OK (chunks non-empty per ToolResult's own contract)
    chunks: list[EvidenceChunk]
    error: Optional[ToolError]


async def _run_rag(agent_input: SubAgentInput) -> _ToolOutcome:
    try:
        result = await rag_tool(
            RagToolInput(query_text=agent_input.query_text, execution=agent_input.execution)
        )
    except Exception as exc:
        logger.error("Case Summarization: RAG tool raised: %s", exc)
        return _ToolOutcome(
            usable=False, chunks=[], error=ToolError(kind="upstream_failure", message=str(exc))
        )
    if result.status == ToolStatus.OK:
        return _ToolOutcome(usable=True, chunks=result.chunks, error=None)
    return _ToolOutcome(usable=False, chunks=[], error=result.error)


async def _run_graph(agent_input: SubAgentInput) -> _ToolOutcome:
    try:
        result = await graph_tool(
            GraphToolInput(
                query_text=agent_input.query_text,
                execution=agent_input.execution,
                target_entity=None,  # case-wide enumeration, not a single-entity lookup
                max_hops=_SUMMARY_MAX_HOPS,
                hybrid=False,  # composed by THIS sub-agent, not GRAPH_HYBRID's internal fusion
            )
        )
    except Exception as exc:
        # [Flagged in module docstring] graph_tool()/retrieve_graph() has no
        # equivalent try/except of its own today — this sub-agent must not
        # let that propagate uncaught.
        logger.error("Case Summarization: GRAPH tool raised: %s", exc)
        return _ToolOutcome(
            usable=False, chunks=[], error=ToolError(kind="upstream_failure", message=str(exc))
        )
    if result.status == ToolStatus.OK:
        return _ToolOutcome(usable=True, chunks=result.chunks, error=None)
    return _ToolOutcome(usable=False, chunks=[], error=result.error)


async def _generate_and_verify(
    agent_input: SubAgentInput, chunks: list[EvidenceChunk]
) -> tuple[Optional[str], dict]:
    """
    Generate one summary over `chunks` (already the exact, ordered,
    flattened list this call's citations/Verifier call must agree on) and
    run `verify_grounding()` over that same list. Returns
    `(answer_or_None, verification_dict)` — `answer` is `None` only if
    generation itself raised; the Verifier's verdict is always in the
    second element for the caller to branch on.
    """
    caller = agent_input.execution.caller
    resolved_language = caller.preferred_language or "the same language as the user's question"
    conversation_summary = (
        agent_input.conversation_context.summary if agent_input.conversation_context else None
    )
    conversation_block = (
        f"--- CONVERSATION CONTEXT ---\n{conversation_summary}\n"
        "--- END OF CONVERSATION CONTEXT ---\n\n"
        if conversation_summary
        else ""
    )
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        preferred_language=resolved_language,
        conversation_block=conversation_block,
        documents=_format_documents_for_prompt(chunks),
    )

    try:
        answer = await call_llm(
            system_prompt,
            agent_input.query_text,
            role=_generation_role(caller.preferred_language),
        )
    except Exception as exc:
        logger.error("Case Summarization: generation failed: %s", exc)
        return None, {}

    verification = await verify_grounding(
        answer=answer,
        cited_chunks=[_chunk_to_verifier_dict(c) for c in chunks],
        case_id=caller.active_case_id,
    )
    return answer, verification


def _verifier_passed(verification: dict) -> bool:
    return bool(verification.get("grounded", False)) and not verification.get("off_topic", False)


async def case_summarization(
    agent_input: SubAgentInput, *, on_event: Optional[OnEventCallback] = None
) -> SubAgentResult:
    """
    The Case Summarization sub-agent. See module docstring for the contract.

    [AMENDMENT — pre-Phase-7 contract amendment, mirrors §10/§11's pattern]
    `on_event` is accepted for `SubAgent` protocol conformance and otherwise
    ignored this session. SUBAGENT_INTERFACES.md §2.1.4's "Generalization"
    note observes this sub-agent (RAG+GRAPH) is a candidate for the same
    live per-source trace Investigative Analysis (Phase 7) implements —
    tracked as future work, not done here; out of this amendment's scope.
    """
    rag_outcome, graph_outcome = await asyncio.gather(
        _run_rag(agent_input), _run_graph(agent_input)
    )

    # ── Both degraded: legitimate "nothing to report", not an error ──────
    if not rag_outcome.usable and not graph_outcome.usable:
        return SubAgentResult(
            status=SubAgentStatus.EMPTY,
            caveats=["No case documents or case graph data were found to summarize."],
        )

    # ── Both usable: combined summary, flattened RAG-then-GRAPH order ────
    if rag_outcome.usable and graph_outcome.usable:
        combined = rag_outcome.chunks + graph_outcome.chunks
        answer, verification = await _generate_and_verify(agent_input, combined)
        if answer is None:
            return SubAgentResult(
                status=SubAgentStatus.ABSTAINED,
                caveats=["Case summary generation failed; no answer could be produced."],
            )
        if not _verifier_passed(verification):
            logger.warning(
                "Case Summarization: verifier rejected combined RAG+GRAPH summary: %s",
                (verification.get("reason") or "")[:150],
            )
            return SubAgentResult(
                status=SubAgentStatus.ABSTAINED,
                caveats=[
                    "The generated case summary could not be verified as grounded "
                    "in the retrieved case material."
                ],
            )

        # TODO(validation-gate): Validation plugs in here -- after the
        # Verifier passes, before the result is returned -- per
        # AGENT_HARNESS_IMPLEMENTATION_PLAN.md §7.1's ordering (Verifier ->
        # Citation-Consistency [Report Drafting only] -> Validation).
        # `validation.py` does not exist yet and `SubAgentResult` has no
        # `validation_status` field. No ad hoc validation logic is invented
        # here to fill that gap.

        tools_used: list[SourceTool] = ["RAG", "GRAPH"]
        return SubAgentResult(
            status=SubAgentStatus.OK,
            answer_text=answer,
            citations=_citations_for(combined),
            tools_used=tools_used,
        )

    # ── GRAPH degraded, RAG usable: ordinary RAG-based summary ───────────
    # [PRESERVE — SUBAGENT_INTERFACES.md §2.1.2] "The symmetric case (GRAPH
    # fails, RAG succeeds) yields an ordinary document-based summary ... so
    # it needs no in-text disclosure; `status` and `degraded_from` carry
    # it." No GRAPH_ONLY_SUMMARY_DISCLOSURE-equivalent line is added here.
    if rag_outcome.usable and not graph_outcome.usable:
        answer, verification = await _generate_and_verify(agent_input, rag_outcome.chunks)
        if answer is None:
            return SubAgentResult(
                status=SubAgentStatus.ABSTAINED,
                caveats=["Case summary generation failed; no answer could be produced."],
            )
        if not _verifier_passed(verification):
            logger.warning(
                "Case Summarization: verifier rejected RAG-only summary: %s",
                (verification.get("reason") or "")[:150],
            )
            return SubAgentResult(
                status=SubAgentStatus.ABSTAINED,
                caveats=[
                    "The generated case summary could not be verified as grounded "
                    "in the retrieved case material."
                ],
            )

        # TODO(validation-gate): see the both-usable branch's identical note above.

        return SubAgentResult(
            status=SubAgentStatus.PARTIAL,
            answer_text=answer,
            citations=_citations_for(rag_outcome.chunks),
            tools_used=["RAG"],
            degraded_from=["GRAPH"],
            caveats=[
                "Case graph data was unavailable for this case; this summary is "
                "based on case documents only."
            ],
        )

    # ── RAG degraded, GRAPH usable: §2.1.2's exact GRAPH-only ordering ────
    assert graph_outcome.usable and not rag_outcome.usable  # exhaustive by construction
    answer, verification = await _generate_and_verify(agent_input, graph_outcome.chunks)
    if answer is None:
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=["Case summary generation failed; no answer could be produced."],
        )
    if not _verifier_passed(verification):
        # [PRESERVE — §2.1.2 step 3] "fails -> ABSTAINED. No summary is
        # served. No disclosure is produced." The disclosure is prepended
        # ONLY after this check passes -- never before, never regardless.
        logger.warning(
            "Case Summarization: verifier rejected GRAPH-only summary: %s",
            (verification.get("reason") or "")[:150],
        )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=[
                "The generated case summary could not be verified as grounded "
                "in the retrieved case graph data."
            ],
        )

    # TODO(validation-gate): see the both-usable branch's identical note above.
    # Validation, once it exists, plugs in AFTER this point too -- but still
    # strictly after the disclosure prepend below, since the disclosure
    # itself is never re-verified/re-validated per §2.1.1's shared rules.

    # [PRESERVE — §2.1.2 step 4] Prepend, never re-verify/regenerate/
    # paraphrase. Fixed string concatenation only.
    disclosed_answer = f"{GRAPH_ONLY_SUMMARY_DISCLOSURE}\n\n{answer}"

    return SubAgentResult(
        status=SubAgentStatus.PARTIAL,
        answer_text=disclosed_answer,
        citations=_citations_for(graph_outcome.chunks),
        tools_used=["GRAPH"],
        degraded_from=["RAG"],
    )


case_summarization.name = CASE_SUMMARIZATION

# Import-time self-registration -- the same pattern semantic_search.py/
# large_scale_aggregate.py established (supervisor.py's own module
# docstring documents it for every future sub-agent module). Importing this
# module is what makes Case Summarization live in the module-level
# registry; nothing else imports it yet, so this has no effect on live
# traffic today (per §6, still not wired into
# main.py/orchestrator.py/router.py).
register(case_summarization)
