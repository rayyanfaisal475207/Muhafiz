"""
Investigative Analysis sub-agent.

Composes RAG + GRAPH + SQL into one synthesized analytical answer, per
docs/SUBAGENT_INTERFACES.md §2.1 and the accuracy rules RESOLVED-4/4a were
written for. This is the first sub-agent running three tools at once, and the
one where `tools_used` accuracy actually bites.

THE COLLAPSE PROBLEM, WHICH IS THE WHOLE POINT OF RESOLVED-4
────────────────────────────────────────────────────────────
GRAPH and SQL both degrade *to RAG*. So a three-tool call can collapse to ONE
effective source: RAG answered, and the other two produced nothing of their
own. Reporting `tools_used=["RAG","GRAPH","SQL"]` would tell the supervisor
that three independent sources agreed when only one did — and the supervisor
may generate on that basis. Correct reporting is
`tools_used=["RAG"], degraded_from=["GRAPH","SQL"]`.

WHO PERFORMS THE FALLBACK
─────────────────────────
The tools SIGNAL `fallback_to_rag=True`; they do not perform it (design §2.2 —
"the tool does not perform the fallback itself; it reports that one is
warranted"). §2's note that this sub-agent "should not need its own duplicate
fallback logic" means it must not re-implement GRAPH's or SQL's *degradation
rules* — deciding when a graph result counts as empty is the tool's job, and
this file does not second-guess it. But something has to honour the signal, and
here that is deliberately NOT a second RAG call: RAG is already being invoked
as one of the three legs, so a tool that falls back is simply recorded as
having degraded into the RAG result already in hand. That is the cheapest
correct reading of the signal and the one that makes the collapse case fall out
naturally.

STATUS, decided per RESOLVED-4 and consistent with Case Summarization:
  * all three contributed, none degraded          -> OK
  * ≥1 contributed, anything degraded or collapsed -> PARTIAL
  * none contributed                               -> ABSTAINED
A full 3-for-3 success is a clean OK; a collapse to one effective source is
PARTIAL even though an answer was produced, because the evidence base is
materially narrower than the query asked for.

[PRESERVE — design §3] Only `Citation` objects cross the handoff boundary.
`EvidenceChunk` lives and dies inside this function, and the three tools'
results are rolled up into ONE answer — never handed back as three result sets.

NOT wired into supervisor routing yet. Tracing is automatic via the
supervisor's completion hook.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from src.pipeline.harness.contracts import (
    CallerContext,
    Citation,
    EvidenceChunk,
    GraphToolInput,
    RagToolInput,
    SourceTool,
    SqlToolInput,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
    ToolResult,
    ToolStatus,
)
from src.pipeline.harness.generation import generate_case_scoped_answer
from src.pipeline.harness.events import EventRecorder
from src.pipeline.harness.tools import registry
from src.pipeline.harness.verifier_gate import UNGROUNDED_TRIGGER, verify_grounding

logger = logging.getLogger(__name__)

NAME = "investigative_analysis"

# Bounding happens here, not at the supervisor. Three tools can return a lot;
# the supervisor must never receive the unbounded union.
_MAX_CITATIONS = 8

_ABSTENTION_CAVEAT = (
    "I could not find evidence in this case's documents, its entity graph, or the "
    "penal-code reference data that supports an analysis of that question."
)


def _usable(result: Optional[ToolResult]) -> bool:
    """A tool contributed if it returned OK with at least one chunk."""
    return bool(result and result.status is ToolStatus.OK and result.chunks)


async def _synthesize(
    query_text: str,
    chunks: list[EvidenceChunk],
    caller: CallerContext,
    conversation_context: Optional[str] = None,
) -> str:
    """
    Generate the analysis, using the same generator and prompt the legacy
    case-scoped routes use.

    `[Document N]` markers are produced by the model against the FLAT,
    ROLLED-UP evidence block, where N is the 1-based position in `chunks` — the
    positional contract the Verifier's deterministic checks depend on
    ([PRESERVE — design §5]). Critically, the numbering spans all sources in one
    sequence: a reader cannot tell from the marker whether document 3 came from
    RAG or GRAPH, and does not need to. The per-chunk `source_tool` metadata
    carries that, and it surfaces through `Citation.source_tool`. This function
    must never reorder `chunks`.

    The `UNGROUNDED_TRIGGER` shortcut is preserved so the verifier-rejection
    path stays exercisable without a live model — see semantic_search for the
    full reasoning.
    """
    if UNGROUNDED_TRIGGER in query_text:
        markers = " ".join(f"[Document {i}]" for i in range(1, len(chunks) + 1))
        return (
            f"{UNGROUNDED_TRIGGER} Analysis drawn from the available case "
            f"evidence, entity relationships, and reference data. {markers}"
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
    (index, source tool, case, file, confidence) and NO chunk text. The rolled
    up list keeps its positional correspondence with the `[Document N]` markers
    in the synthesized answer.
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


async def _emit_source_outcome(
    events: Optional[EventRecorder], tool: str, result: Optional[ToolResult],
) -> None:
    """
    [RESOLVED-4a] One `PipelineEvent` per source-tool outcome, AS IT RESOLVES.

    The final roll-up is after-the-fact: without these, a user watching a
    three-tool analysis sees nothing until everything finishes, then only the
    bundled result, with no signal about when or why a source dropped out.

    Status uses the existing five-value SSE vocabulary — no new values. A tool
    that fell back is `retry`; one that failed outright with no usable result is
    `error`; one that contributed is `done`.
    """
    if not events or result is None:
        return

    step = f"analysis:{tool.lower()}"
    if _usable(result):
        await events.emit(step, "done", f"{tool} contributed {len(result.chunks)} item(s)")
    elif getattr(result, "fallback_to_rag", False):
        await events.emit(step, "retry", f"{tool} fell back — no independent contribution")
    else:
        await events.emit(step, "error", f"{tool} returned no usable evidence")


async def run(
    agent_input: SubAgentInput,
    events: Optional[EventRecorder] = None,
    gateway: Any = None,
) -> SubAgentResult:
    """
    Execute Investigative Analysis. Returns a bounded `SubAgentResult`.

    Project scope is NOT a parameter here: it rides on `caller.project_id` and
    reaches the retrieval tools automatically. This function previously
    declared a `project_id` argument that nothing passed and nothing read — so
    project-scoped retrieval silently widened to every project. Caller scope
    belongs on `CallerContext`, where forgetting to forward it is impossible.
    """
    if events:
        await events.emit(
            f"subagent:{NAME}", "active", "Investigative Analysis: gathering evidence"
        )

    caller = agent_input.caller

    # ── The three legs ──
    # Run sequentially rather than gathered: each emits its own PipelineEvents,
    # and interleaving them would scramble the live trace's ordering — which
    # RESOLVED-4a exists to make legible.
    rag_result = await registry.rag_tool(
        RagToolInput(query_text=agent_input.query_text, caller=caller, top_k=_MAX_CITATIONS),
        events=events,
    )
    await _emit_source_outcome(events, "RAG", rag_result)

    graph_result = await registry.graph_tool(
        GraphToolInput(
            query_text=agent_input.query_text, caller=caller,
            target_entity=None, max_hops=2, hybrid=False,
        ),
        events=events,
    )
    await _emit_source_outcome(events, "GRAPH", graph_result)

    sql_result = await registry.sql_tool(
        SqlToolInput(query_text=agent_input.query_text, caller=caller),
        gateway=gateway,
        events=events,
    )
    await _emit_source_outcome(events, "SQL", sql_result)

    legs: list[tuple[SourceTool, Optional[ToolResult]]] = [
        ("RAG", rag_result), ("GRAPH", graph_result), ("SQL", sql_result),
    ]

    # ── Classify each leg: contributed, degraded, or both ──
    # [RESOLVED-4] `tools_used` = actually contributed, POST-fallback.
    # `degraded_from` = attempted but failed, empty, or fell back.
    # A tool appears in BOTH when it contributed while degrading internally —
    # the overlap case Case Summarization already produces (RAG returning
    # evidence with its relevance gate unavailable). `degraded_from` membership
    # therefore does NOT imply "did not contribute".
    chunks: list[EvidenceChunk] = []
    tools_used: list[SourceTool] = []
    degraded_from: list[SourceTool] = []
    caveats: list[str] = []

    for tool, result in legs:
        contributed = _usable(result)
        if contributed:
            chunks.extend(result.chunks)
            tools_used.append(tool)

        internally_degraded = bool(getattr(result, "degradation_caveats", None))
        if internally_degraded:
            for caveat in result.degradation_caveats:
                if caveat not in caveats:
                    caveats.append(caveat)

        # Degraded if it produced nothing usable, OR contributed but under a
        # documented internal degradation.
        if not contributed or internally_degraded:
            degraded_from.append(tool)

    # ── All three produced nothing → ABSTAINED ──
    if not tools_used:
        if events:
            await events.emit(
                f"subagent:{NAME}", "done",
                "Investigative Analysis: no usable evidence from any source",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            tools_used=[],
            degraded_from=degraded_from,
            caveats=[_ABSTENTION_CAVEAT],
            error=next((r.error for _, r in legs if r is not None and r.error), None),
        )

    chunks = chunks[:_MAX_CITATIONS]

    # ── Synthesize ONE answer, then gate it ──
    try:
        answer = await _synthesize(
            agent_input.query_text,
            chunks,
            caller,
            agent_input.conversation_context,
        )
    except Exception as exc:
        # Generation infrastructure failed. Nothing safe to serve — abstain
        # rather than emit unverified prose. Everything attempted collapses
        # into `degraded_from`, keeping [RESOLVED-4]'s invariant that a tool
        # never appears in both lists.
        logger.error("Investigative Analysis generation failed: %s", exc)
        if events:
            await events.emit(
                f"subagent:{NAME}", "error",
                "Investigative Analysis: answer generation failed",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            citations=[],
            tools_used=[],
            degraded_from=sorted(set(degraded_from) | set(tools_used)),
            error=ToolError(
                kind="upstream_failure",
                message=f"Answer generation failed: {exc}",
            ),
        )

    # [PRESERVE — design §5] The Verifier receives EXACTLY the list the
    # generator was shown, in the same order — one flat list spanning all
    # contributing sources, not three grouped payloads.
    verdict = await verify_grounding(
        answer=answer,
        cited_chunks=[c.model_dump() for c in chunks],
        case_id=caller.active_case_id,
        cross_case_ids=None,
    )

    if not verdict["grounded"]:
        # [PRESERVE] The ungrounded draft is NEVER served.
        if events:
            await events.emit(
                f"subagent:{NAME}", "error",
                f"Investigative Analysis: grounding check failed — {verdict['reason']}",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            citations=[],
            tools_used=[],
            degraded_from=sorted(set(degraded_from) | set(tools_used)),
            caveats=[_ABSTENTION_CAVEAT],
        )

    citations = _to_citations(chunks)

    # [RESOLVED-4] OK only when all three contributed with nothing degraded.
    # A collapse to one effective source still produces an answer, but on a
    # materially narrower evidence base than the query asked for — that is what
    # PARTIAL is for. Same full-vs-degraded distinction Case Summarization draws.
    status = SubAgentStatus.OK if not degraded_from else SubAgentStatus.PARTIAL

    if events:
        await events.emit(
            f"subagent:{NAME}", "done",
            f"Investigative Analysis: synthesized from {len(citations)} item(s) "
            f"via {'/'.join(tools_used)}",
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
