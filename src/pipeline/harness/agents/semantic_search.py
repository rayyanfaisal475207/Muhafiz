"""
Semantic Search sub-agent — the one fully-wired routable path.

Per SUBAGENT_INTERFACES.md §2.1: composes the RAG tool only; hands back a short
synthesized answer plus top-N citations, never the full reranked set;
abstains when the evaluator rejects after retry exhaustion.

THE BOUNDARY THIS FILE EXISTS TO ENFORCE
────────────────────────────────────────
[PRESERVE — design §3] `EvidenceChunk` objects live and die inside this
function. They go to the generator and the Verifier — both at this level — and
are converted to `Citation` objects before returning. `SubAgentResult` has no
field capable of holding a chunk, and that is deliberate: the supervisor's
context budget is the reason this layer exists, and a sub-agent that hands its
working set upward defeats the architecture.

The stub tools return deliberately verbose, near-duplicate chunks precisely so
this boundary is exercised under realistic pressure from day one.
"""
from __future__ import annotations

import logging
from typing import Optional

from src.pipeline.harness.contracts import (
    CallerContext,
    Citation,
    EvidenceChunk,
    RagToolInput,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
    ToolStatus,
)
from src.pipeline.harness.events import EventRecorder
from src.pipeline.harness.generation import generate_case_scoped_answer
from src.pipeline.harness.tools import registry
from src.pipeline.harness.verifier_gate import UNGROUNDED_TRIGGER, verify_grounding

logger = logging.getLogger(__name__)

NAME = "semantic_search"

# How many passages may back one answer. Bounding happens here, not at the
# supervisor — the supervisor must never receive the unbounded set.
_MAX_CITATIONS = 3

_ABSTENTION_REASON = (
    "I could not find evidence in this case's documents that supports an answer to "
    "that question."
)


async def _synthesize(
    query_text: str,
    chunks: list[EvidenceChunk],
    caller: CallerContext,
    conversation_context: Optional[str] = None,
) -> str:
    """
    Generate the answer, using the same generator and prompt the legacy RAG
    route uses (`generation.generate_case_scoped_answer`).

    `[Document N]` markers are produced by the model against the numbered
    evidence block, where N is the 1-based position in `chunks` — the positional
    contract the Verifier's deterministic checks depend on ([PRESERVE —
    design §5]). This function must never reorder `chunks`.

    The test-trigger shortcut is preserved: `UNGROUNDED_TRIGGER` in the query is
    carried into the answer WITHOUT calling the model at all. That keeps the
    verifier-rejection path exercisable in tests without a live model — and
    without a billable call — since the verifier inspects the ANSWER, so a
    trigger that never reached it could not exercise the rejection path.
    """
    if UNGROUNDED_TRIGGER in query_text:
        markers = " ".join(f"[Document {i}]" for i in range(1, len(chunks) + 1))
        return (
            f"{UNGROUNDED_TRIGGER} Based on the case documents, the retrieved "
            f"passages address the query. {markers}"
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
    """Execute Semantic Search. Returns a bounded `SubAgentResult`."""
    if events:
        await events.emit(
            f"subagent:{NAME}", "active", "Semantic Search: searching case documents"
        )

    result = await registry.rag_tool(
        RagToolInput(
            query_text=agent_input.query_text,
            caller=agent_input.caller,
            top_k=_MAX_CITATIONS,
        ),
        events=events,
    )

    # ── Tool failed or found nothing → abstain (§2.1: evaluator rejection
    # after retry exhaustion abstains, same as today's RAG route) ──
    if result.status in (ToolStatus.FAILED, ToolStatus.EMPTY) or not result.chunks:
        if events:
            await events.emit(
                f"subagent:{NAME}", "done",
                "Semantic Search: no supporting evidence — abstaining",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            tools_used=[],           # [RESOLVED-4] nothing contributed
            degraded_from=["RAG"],   # [RESOLVED-4] attempted, did not pay off
            error=result.error,
        )

    # ── Generate, then gate ──
    chunks = result.chunks[:_MAX_CITATIONS]
    try:
        answer = await _synthesize(
            agent_input.query_text,
            chunks,
            agent_input.caller,
            agent_input.conversation_context,
        )
    except Exception as exc:
        # Generation infrastructure failed (model unreachable, timeout). There
        # is no answer to verify and nothing safe to serve — abstain rather
        # than emit ungrounded prose. RAG is recorded in `degraded_from`
        # because retrieval DID succeed; it just never reached the user.
        logger.error("Semantic Search generation failed: %s", exc)
        if events:
            await events.emit(
                f"subagent:{NAME}", "error",
                "Semantic Search: answer generation failed",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            citations=[],
            tools_used=[],
            degraded_from=["RAG"],
            error=ToolError(
                kind="upstream_failure",
                message=f"Answer generation failed: {exc}",
            ),
        )

    # [PRESERVE — design §5] The verifier receives EXACTLY the list the
    # generator was shown, in the same order.
    verdict = await verify_grounding(
        answer=answer,
        cited_chunks=[c.model_dump() for c in chunks],
        case_id=agent_input.caller.active_case_id,
        cross_case_ids=None,
    )

    if not verdict["grounded"]:
        # [PRESERVE] The ungrounded draft is NEVER served.
        if events:
            await events.emit(
                f"subagent:{NAME}", "error",
                f"Semantic Search: grounding check failed — {verdict['reason']}",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            citations=[],
            tools_used=[],
            degraded_from=["RAG"],
            caveats=[_ABSTENTION_REASON],
        )

    citations = _to_citations(chunks)

    # Propagate tool-level degradation into the bounded payload. `caveats` is
    # the contract's existing channel for qualifications that MUST survive to
    # the final response (design §3) — the same treatment unconfirmed identity
    # links get. A relevance gate that could not run is exactly that kind of
    # qualification: the answer is still served, but the user is told the
    # evidence went unscreened. Dropping this here would make the tool's
    # `evaluator_verdict="unavailable"` invisible to everyone above it.
    caveats = list(result.degradation_caveats)
    degraded_from = ["RAG"] if result.evaluator_verdict == "unavailable" else []

    if events:
        await events.emit(
            f"subagent:{NAME}", "done",
            f"Semantic Search: answered from {len(citations)} passages",
            sources=[{"source_tool": c.source_tool, "label": c.display_label()} for c in citations],
        )

    return SubAgentResult(
        # A result whose relevance gate never ran is not a clean OK — it is
        # usable but degraded, which is what PARTIAL exists to say.
        status=SubAgentStatus.PARTIAL if caveats else SubAgentStatus.OK,
        answer_text=answer,
        citations=citations,
        tools_used=["RAG"],
        degraded_from=degraded_from,
        caveats=caveats,
    )
