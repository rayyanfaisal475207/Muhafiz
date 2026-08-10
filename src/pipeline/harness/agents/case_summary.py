"""
Case Summarization sub-agent.

Composes case-scoped RAG + case-scoped GRAPH into one structured summary
(status, key entities, key events, open questions) per
docs/SUBAGENT_INTERFACES.md §2.1.

THE THREE BEHAVIOURS THIS FILE EXISTS TO GET RIGHT
──────────────────────────────────────────────────

1. SYMMETRIC DEGRADATION, EITHER DIRECTION ([RESOLVED-2]). One side failing is
   never fatal while the other side has real evidence — abstaining then would
   discard genuinely useful material. Only BOTH sides empty yields EMPTY.

2. THE GRAPH-ONLY DIRECTION DISCLOSES IN ITS OWN TEXT ([RESOLVED-2a]).
   `status=PARTIAL` + `degraded_from` are the machine-readable signal; the
   in-text line is the human-readable one, and it travels with the content so
   it survives being quoted, exported, or pasted somewhere the payload metadata
   never follows. The reverse direction (GRAPH missing, RAG fine) needs no such
   line: it produces the document-based summary a reader expects by default.

3. NESTED DEGRADATION IS NOT SWALLOWED. A tool can succeed while having degraded
   internally — RAG returns OK with `evaluator_verdict="unavailable"` when its
   relevance gate could not run (see c4a06fe). That caveat must surface in THIS
   sub-agent's `caveats`, or the degradation dies one level below where anyone
   can see it.

[PRESERVE — design §3] Only `Citation` objects cross the handoff boundary
upward. `EvidenceChunk` lives and dies inside this function.

NOT wired into the supervisor's routing yet, deliberately.
"""
from __future__ import annotations

import logging
from typing import Optional

from src.pipeline.harness.contracts import (
    GRAPH_ONLY_SUMMARY_DISCLOSURE,
    CallerContext,
    Citation,
    EvidenceChunk,
    GraphToolInput,
    RagToolInput,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
    ToolResult,
    ToolStatus,
)
from src.pipeline.harness.events import EventRecorder
from src.pipeline.harness.generation import generate_case_scoped_answer
from src.pipeline.harness.tools import registry
from src.pipeline.harness.verifier_gate import UNGROUNDED_TRIGGER, verify_grounding

logger = logging.getLogger(__name__)

NAME = "case_summary"

# Bounding happens here, not at the supervisor — the supervisor must never
# receive the unbounded evidence set.
_MAX_CITATIONS = 6

# Hop depth is capped for summarization specifically: a case summary wants the
# entities immediately connected to the case, not a deep transitive walk.
_SUMMARY_MAX_HOPS = 2

_ABSTENTION_CAVEAT = (
    "I could not find evidence in this case's documents or its entity graph that "
    "supports a summary."
)


def _usable(result: Optional[ToolResult]) -> bool:
    """A tool contributed if it returned OK with at least one chunk."""
    return bool(result and result.status is ToolStatus.OK and result.chunks)


async def _synthesize(
    query_text: str,
    chunks: list[EvidenceChunk],
    graph_only: bool,
    caller: CallerContext,
    conversation_context: Optional[str] = None,
) -> str:
    """
    Generate the case summary, using the same generator and prompt the legacy
    GRAPH_HYBRID route uses.

    `[Document N]` markers are produced by the model against the numbered
    evidence block, where N is the 1-based position in `chunks` — the positional
    contract the Verifier's deterministic checks depend on ([PRESERVE —
    design §5]). This function must never reorder `chunks`.

    NOTE: this produces the EVIDENTIARY content only. The GRAPH-only disclosure
    is NOT added here — it is injected after verification (§2.1.2 step 4), so it
    is never submitted to the grounding gate. `graph_only` therefore does NOT
    change the prompt: it stays a caller-side concern, and letting it steer
    generation would risk the model pre-empting the disclosure in unverified
    prose.

    The `UNGROUNDED_TRIGGER` shortcut is preserved so the verifier-rejection
    path stays exercisable without a live model — see semantic_search for the
    full reasoning.
    """
    if UNGROUNDED_TRIGGER in query_text:
        markers = " ".join(f"[Document {i}]" for i in range(1, len(chunks) + 1))
        return (
            f"{UNGROUNDED_TRIGGER} Case summary drawn from the case record: "
            f"status, key entities, key events, and open questions are set out "
            f"below. {markers}"
        )

    return await generate_case_scoped_answer(
        query_text=query_text,
        chunks=chunks,
        preferred_language=caller.preferred_language,
        conversation_context=conversation_context,
    )


def _to_citations(chunks: list[EvidenceChunk]) -> list[Citation]:
    """
    Convert internal evidence into the bounded handoff form.

    [PRESERVE — design §3] This is the boundary. `Citation` carries provenance
    (index, source tool, case, file, confidence) and NO chunk text.
    """
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


async def run(
    agent_input: SubAgentInput, events: Optional[EventRecorder] = None,
) -> SubAgentResult:
    """Execute Case Summarization. Returns a bounded `SubAgentResult`."""
    if events:
        await events.emit(
            f"subagent:{NAME}", "active", "Case Summarization: gathering case evidence"
        )

    caller = agent_input.caller

    # ── Both legs, real tools via the registry ──
    # Run sequentially rather than gathered: each emits its own PipelineEvents,
    # and interleaving them would scramble the live trace's ordering.
    rag_result = await registry.rag_tool(
        RagToolInput(
            query_text=agent_input.query_text,
            caller=caller,
            top_k=_MAX_CITATIONS,
        ),
        events=events,
    )

    graph_result = await registry.graph_tool(
        GraphToolInput(
            query_text=agent_input.query_text,
            caller=caller,
            target_entity=None,  # case-wide enumeration, not a single-entity lookup
            max_hops=_SUMMARY_MAX_HOPS,
            hybrid=False,
        ),
        events=events,
    )

    rag_ok = _usable(rag_result)
    graph_ok = _usable(graph_result)

    # ── Both sides empty → EMPTY, not ABSTAINED ──
    # Nothing to summarize is a legitimate outcome, distinct from "we could not
    # safely answer". No disclosure: there is no partial artifact to qualify.
    if not rag_ok and not graph_ok:
        if events:
            await events.emit(
                f"subagent:{NAME}", "done",
                "Case Summarization: no document or graph evidence for this case",
            )
        return SubAgentResult(
            status=SubAgentStatus.EMPTY,
            answer_text=None,
            tools_used=[],
            degraded_from=["RAG", "GRAPH"],
            error=(rag_result.error if rag_result else None) or (
                graph_result.error if graph_result else None
            ),
        )

    # ── Assemble the evidence set, and record which side is missing ──
    # [RESOLVED-4] `tools_used` lists only post-fallback CONTRIBUTORS;
    # `degraded_from` lists what was attempted and did not pay off. A tool never
    # appears in both.
    chunks: list[EvidenceChunk] = []
    tools_used: list[str] = []
    degraded_from: list[str] = []

    if rag_ok:
        chunks.extend(rag_result.chunks)
        tools_used.append("RAG")
    else:
        degraded_from.append("RAG")

    if graph_ok:
        chunks.extend(graph_result.chunks)
        tools_used.append("GRAPH")
    else:
        degraded_from.append("GRAPH")

    chunks = chunks[:_MAX_CITATIONS]
    graph_only = graph_ok and not rag_ok

    if events and degraded_from:
        await events.emit(
            f"subagent:{NAME}", "retry",
            f"Case Summarization: degraded — no usable {'/'.join(degraded_from)} evidence",
        )

    # ── Generate the EVIDENTIARY content, then gate it ──
    try:
        answer = await _synthesize(
            agent_input.query_text,
            chunks,
            graph_only=graph_only,
            caller=caller,
            conversation_context=agent_input.conversation_context,
        )
    except Exception as exc:
        # Generation infrastructure failed. Nothing safe to serve — abstain
        # rather than emit unverified prose. Both tools land in `degraded_from`
        # because retrieval succeeded but never reached the user.
        logger.error("Case Summarization generation failed: %s", exc)
        if events:
            await events.emit(
                f"subagent:{NAME}", "error",
                "Case Summarization: summary generation failed",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            citations=[],
            tools_used=[],
            # [RESOLVED-4] Nothing contributed to a served answer, so
            # `tools_used` is empty and everything attempted collapses into
            # `degraded_from` — the same treatment the verifier-rejection path
            # below applies, and it keeps the invariant that a tool never
            # appears in both lists.
            degraded_from=sorted(set(degraded_from) | set(tools_used)),
            error=ToolError(
                kind="upstream_failure",
                message=f"Summary generation failed: {exc}",
            ),
        )

    # [PRESERVE — design §5] The Verifier receives EXACTLY the list the
    # generator was shown, in the same order.
    verdict = await verify_grounding(
        answer=answer,
        cited_chunks=[c.model_dump() for c in chunks],
        case_id=caller.active_case_id,
        cross_case_ids=None,
    )

    if not verdict["grounded"]:
        # [PRESERVE] The ungrounded draft is NEVER served — and per §2.1.2 step
        # 3, no disclosure is produced either: there is no summary to qualify.
        if events:
            await events.emit(
                f"subagent:{NAME}", "error",
                f"Case Summarization: grounding check failed — {verdict['reason']}",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            citations=[],
            tools_used=[],
            degraded_from=sorted(set(degraded_from) | set(tools_used)),
            caveats=[_ABSTENTION_CAVEAT],
        )

    # ── Post-verification disclosure injection (§2.1.2 step 4) ──
    # [RESOLVED-2a] GRAPH-only summaries carry the disclosure IN THE TEXT.
    # Injected here, after the gate passed, and never re-verified: it is a
    # meta-statement about how the summary was produced, not a claim about the
    # case, so it has nothing to cite and must not face the no-citation check.
    disclosure_applied = False
    if graph_only:
        answer = f"{GRAPH_ONLY_SUMMARY_DISCLOSURE}\n\n{answer}"
        disclosure_applied = True

    # ── Caveats ──
    caveats: list[str] = []
    if disclosure_applied:
        # Mirrored into caveats as well as the text: the text carries it to the
        # reader, this carries it to the supervisor as structured data.
        caveats.append(GRAPH_ONLY_SUMMARY_DISCLOSURE)

    # [Nested degradation] A tool can succeed while having degraded internally.
    # RAG returns OK with evaluator_verdict="unavailable" when its relevance
    # gate could not run — that caveat has to climb, or it dies here.
    for tool_result in (rag_result, graph_result):
        for caveat in getattr(tool_result, "degradation_caveats", []) or []:
            if caveat not in caveats:
                caveats.append(caveat)

    # A contributing tool that degraded INTERNALLY is recorded in
    # `degraded_from` too, while STAYING in `tools_used` — it did contribute,
    # it just contributed unscreened evidence. This is the one case where a
    # tool legitimately appears in both lists, and it is why a reader cannot
    # infer "did not contribute" from `degraded_from` membership alone.
    if rag_ok and getattr(rag_result, "degradation_caveats", None):
        if "RAG" not in degraded_from:
            degraded_from.append("RAG")

    citations = _to_citations(chunks)

    # PARTIAL when either a whole side is missing or a contributing tool
    # degraded internally. A summary built on an unscreened evidence set is not
    # a clean OK, even though both legs returned data.
    status = SubAgentStatus.PARTIAL if degraded_from else SubAgentStatus.OK

    if events:
        await events.emit(
            f"subagent:{NAME}", "done",
            f"Case Summarization: summarized from {len(citations)} item(s) "
            f"via {'/'.join(tools_used) or 'no tools'}",
            sources=[
                {"source_tool": c.source_tool, "label": c.display_label()}
                for c in citations
            ],
        )

    return SubAgentResult(
        status=status,
        answer_text=answer,
        citations=citations,
        tools_used=tools_used,
        degraded_from=degraded_from,
        caveats=caveats,
    )
