"""
Investigative Analysis sub-agent —
src/pipeline/harness/agents/investigative_analysis.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 6, "Phase 7").

Source of truth: SUBAGENT_INTERFACES.md §2.1's "Investigative Analysis" row
+ §2.1.4's exact live-per-source-trace requirement + RESOLVED-4/RESOLVED-4a
in §3, AGENT_HARNESS_DESIGN.md §3's matching row, and this session's
explicit brief.

SCOPE. Composes exactly three tools, run CONCURRENTLY via `asyncio.gather`
(this session's explicit build note, plan §4 row 6: "Runs the three tool
calls in parallel, not one after another, cutting response time"):
  - `src.pipeline.harness.tools.rag.rag_tool` (Phase 0), case-scoped.
  - `src.pipeline.harness.tools.graph.graph_tool` (Phase 0), case-scoped,
    `hybrid=False` — this sub-agent composes RAG and GRAPH itself, so
    requesting GRAPH_HYBRID's internal fusion here would bypass that
    composition rather than perform it (same reasoning
    case_summarization.py already documents for its own RAG+GRAPH pair).
  - `src.pipeline.harness.tools.sql.sql_tool` (Phase 0), unscoped reference
    data (design §2.6: no case scoping, no role gate).
None of the three carries a role gate (RAG: case-assignment-based, no role
gate — design §2.1; GRAPH: within-case, no role gate beyond case
assignment — design §2.2; SQL: reference data, no case scoping, no role
gate — design §2.6), so `SubAgentStatus.DENIED` is never built as an output
path here, matching Case Summarization's precedent for the identical reason.

── FALLBACK-SUBSTITUTION QUESTION, RESOLVED FROM THE ACTUAL TOOL CODE ──────
This session's brief asked, explicitly, whether `fallback_to_rag=True` on
GRAPH/SQL means this sub-agent must itself re-invoke the RAG tool, or
whether something else already performs that substitution. Confirmed by
reading tools/rag.py, tools/graph.py, tools/sql.py directly (not assumed
from the docs): NOTHING inside `graph_tool()`/`sql_tool()` calls the RAG
tool. `ToolResult.fallback_to_rag`'s own docstring (types.py) is explicit —
"The tool does not perform the fallback itself; it reports that one is
warranted and the calling sub-agent acts on it." Case Summarization's
already-merged Phase 4 precedent confirms the pattern in practice: on a
GRAPH/RAG-direction degrade, the sub-agent uses whichever *other* tool it
already ran, not a special re-invocation.

So yes — this sub-agent must itself supply the RAG substitution. But
literally re-invoking `rag_tool()` a second time whenever GRAPH or SQL
degrade would be exactly the "duplicate fallback logic" this session's
brief says should be unnecessary, AND it would double the retrieval cost
for a request whose whole point (per the parallel-execution build note) is
to be fast. The resolution: because RAG is ALREADY one of the three tools
this sub-agent runs concurrently on every call (not conditionally, on
GRAPH/SQL's say-so), the single already-in-flight `rag_tool()` call IS the
substitution. Concretely, and by construction of `ToolResult` itself
("chunks non-empty iff status is OK"): when GRAPH or SQL degrade
(`status != OK`), their own `.chunks` is already `[]` — there is nothing to
flatten from them. The flattened evidence list is simply the union of
whichever of the three tools independently returned `status == OK`; RAG's
own chunks (when RAG itself succeeded) are what a degraded GRAPH's or
SQL's citations effectively rest on, for free, with no second call and no
double-counting. `tools_used` reports `"RAG"` at most once regardless of
how many siblings degraded toward it — RESOLVED-4's dedup rule falls out of
this shape automatically rather than needing an explicit dedup step.

If RAG's OWN concurrent call also fails/is empty while GRAPH or SQL report
`fallback_to_rag=True`, there is no substitute to fall back to (the
designated target degraded too) — that combination contributes nothing
from that slot, same as any other non-contributing tool; see the ABSTAINED
threshold below.

── LIVE PER-SOURCE TRACE EVENTS (SUBAGENT_INTERFACES.md §2.1.4, RESOLVED-4a) ──
One `PipelineEvent` per source-tool outcome, emitted AS IT RESOLVES — not
batched at the end. Implemented by wrapping each of the three tool calls in
its own small coroutine that emits its event the instant its own `await`
completes, before `asyncio.gather()` as a whole returns; because the three
run concurrently, whichever tool actually finishes first is what the
live trace shows first — not artificially serialized.

Status-value mapping onto the five-value SSE vocabulary (`active`/`done`/
`error`/`retry`/`skipped`) — a resolved decision, not merely copied from
the two illustrative examples in §2.1.4's text, since neither RAG nor a
"hard failure with no fallback signal" is spelled out there in full:
  - `status == OK`                              -> "done" (contributed).
  - `status != OK` AND `fallback_to_rag == True` -> "retry" (degraded
    toward RAG — §2.1.4's own literal GRAPH example).
  - `status != OK` AND `fallback_to_rag == False` -> "error". This is
    RAG's own case by construction (`RagToolResult.fallback_to_rag` is
    pinned `Literal[False]` — design §2.1, RAG has no fallback target of
    its own) AND the defensive uncaught-exception branch below for
    GRAPH/SQL (no formal `ToolResult` to read a flag from at all) — which
    is exactly what §2.1.4's third example ("SQL fails outright -> error")
    describes: `sql_tool()`'s own try/except always pairs a FAILED status
    with `fallback_to_rag=True` today (confirmed by reading tools/sql.py),
    so the only way SQL's event is legitimately "error" rather than
    "retry" is this hard, no-fallback-signal-available shape.
`"skipped"` never fires here — all three tools are always attempted, every
call, unconditionally (no conditional dispatch, unlike Cross-Case
Linkage's XGRAPH/XNETWORK choice).

The roll-up (`tools_used`/`degraded_from`) and the events agree by
construction whenever a result is actually returned as OK/PARTIAL: both
are computed from the exact same three per-tool outcomes, in the exact
same "status == OK" test. See the note below on the ONE case where they
intentionally diverge (a verifier rejection of the generated answer).

── DEFENSIVE EXCEPTION HANDLING — flagged, not silent, same pattern
case_summarization.py already established for the identical asymmetry.
`rag_tool()` and `sql_tool()` both catch their own retrieval exceptions
internally and return typed FAILED results (confirmed by reading both
files); `graph_tool()`, as of this session's read of tools/graph.py, does
NOT wrap its own `retrieve_graph()` calls in a try/except — an
infrastructure exception there propagates uncaught. This sub-agent
defensively catches around all three calls anyway (cheap, and SQL's own
wrapping could change without notice) and treats a caught exception
exactly like a FAILED result with no fallback signal available (event:
"error"; roll-up: attempted-and-not-contributed).

── STATUS THRESHOLDS (RESOLVED-4, this row's own explicit text) ───────────
"≥1 of RAG/GRAPH/SQL returning usable data -> PARTIAL (or OK if none
degraded); all three failed/empty -> ABSTAINED." Read and applied
LITERALLY, including the ABSTAINED choice for the empty/all-failed case —
this deliberately does NOT mirror Case Summarization's "both empty ->
EMPTY" convention. SUBAGENT_INTERFACES.md draws that line differently for
this sub-agent specifically (its own row's text says ABSTAINED, not EMPTY,
for the zero-contributor case), and RESOLVED-4 is written for this
sub-agent as its origin case — there is no ambiguity to resolve by
analogy to a different sub-agent's row.

── A SEPARATE, LATER REASON FOR ABSTAINED: VERIFIER REJECTION ─────────────
Distinct from the tool-availability threshold above. Every prior sub-agent
that generates `answer_text` (Semantic Search, Case Summarization) treats a
`verify_grounding()` rejection as its own ABSTAINED path, with
`tools_used`/`degraded_from` left at their SubAgentResult defaults (`[]`)
rather than reporting what the (now-discarded) draft had rested on — an
answer that fails verification is never served, and nothing about it,
including which tools backed it, is asserted as fact. This sub-agent
follows that same established convention for consistency with every other
sub-agent in the codebase. Consequence, flagged plainly: for THIS one
branch, a tool whose event said "done" (it genuinely retrieved data) can
still end up NOT listed in the final `tools_used`, because the answer built
from that data was rejected and discarded. This is not a bug in the
"events and roll-up must agree" rule — that rule governs the roll-up
actually returned for a result that stands (OK/PARTIAL); it was never
written to mean a discarded, never-served draft must still be reported as
if it succeeded. Documented here explicitly because it is the one place in
this module where that distinction actually matters.

── FLATTENING (design §5) ──────────────────────────────────────────────────
Canonical order RAG, then GRAPH, then SQL — whichever of the three
contributed, concatenated in that fixed order — before the generation
prompt is built. The exact same list, same order, is what
`verify_grounding()` sees afterward, so `[Document N]` citations and the
Verifier's positional `chunks[n-1]` indexing agree by construction, same
discipline case_summarization.py already established.

── BOUNDED PAYLOAD ──────────────────────────────────────────────────────────
One synthesized answer with citations rolled up across all three sources —
never three separate result sets, never raw chunks. `SubAgentResult.answer_text`
+ `.citations` only; the flattened chunk list stays inside this function's
stack frame.

── VALIDATION GATE ──────────────────────────────────────────────────────────
WIRED (Validation-module session). Per AGENT_HARNESS_IMPLEMENTATION_PLAN.md
§5's table, Validation's FULL semantic tier is MANDATORY here — same tier as
Cross-Case Linkage and Report Drafting, NOT the lighter structural-only
check some other sub-agents get — one batched local-LLM entailment call
over the same flattened chunk list the Verifier was shown, after the
Verifier passes, before the result is returned (§7.1's ordering).

── CLASSIFICATION REACHABILITY ──────────────────────────────────────────────
Read `src/pipeline/harness/supervisor.py`'s `_ROUTE_TO_SUBAGENT` table
before writing any code here, per this session's explicit instruction not
to assume. `"SQL": INVESTIGATIVE_ANALYSIS` is present (set in Phase 1,
unrelated to this session's own work) — so this sub-agent IS reachable via
real classification today, but ONLY through the SQL route.
`router.py::route_query()`'s RAG and GRAPH/GRAPH_HYBRID routes map to
Semantic Search and Case Summarization respectively (Phase 1's own
`_ROUTE_TO_SUBAGENT`), not to Investigative Analysis — there is no route
whose classification intent is "deep synthesis across RAG+GRAPH+SQL at
once." **Stated plainly: PARTIALLY reachable, via the SQL route only** —
not gapped like Timeline Building (which has zero routes), but also not
fully reachable the way Cross-Case Linkage is (both of its routes map to
it). Per this session's instruction and Phase 1/5's own precedent, no new
classification keyword/pattern is invented to close this partially — that
would be new classification logic layered on top of, not reuse of,
router.py's tuned classifier, and needs the same live-failure evidence
XAGG/XGRAPH/XNETWORK's own overrides had, not a guess made in this session.

NOT IN SCOPE THIS PHASE: any other sub-agent, `validation.py` itself, live
wiring into main.py/orchestrator.py/router.py, new classification triggers.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from src.data_gateway.base import DataGateway
from src.llm.client import call_llm
from src.pipeline.harness.supervisor import INVESTIGATIVE_ANALYSIS, register
from src.pipeline.harness.tools.graph import GraphToolInput, graph_tool
from src.pipeline.harness.tools.rag import RagToolInput, rag_tool
from src.pipeline.harness.tools.sql import SqlToolInput, sql_tool
from src.pipeline.harness.types import (
    Citation,
    EvidenceChunk,
    OnEventCallback,
    PipelineEvent,
    SOURCE_TOOL_DISPLAY_LABELS,
    SourceTool,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
    ToolStatus,
)
from src.pipeline.validation import caveats_for_validation, validate_answer
from src.pipeline.verifier import verify_grounding

logger = logging.getLogger(__name__)

# Self-contained, sub-agent-scoped prompt — same convention every prior
# sub-agent module established (no coupling to orchestrator.py's own
# prompt-assembly machinery). Names the three possible source kinds
# explicitly so the model doesn't conflate a penal-code reference row with
# case evidence.
_SYSTEM_PROMPT_TEMPLATE = (
    "You are a police investigative-analysis assistant. Using ONLY the "
    "material provided below — which may combine case documents, case "
    "graph/entity-relationship findings, and penal-code reference data — "
    "produce one synthesized analytical answer to the user's question.\n\n"
    "Every factual claim MUST cite its source as [Document N], where N is "
    "that document's 1-based position below. If the material does not "
    "contain enough information to answer, say so plainly rather than "
    "guessing or answering from general knowledge.\n\n"
    "Respond in {preferred_language}.\n\n"
    "{conversation_block}"
    "--- MATERIAL ---\n{documents}\n--- END OF MATERIAL ---"
)

# Canonical order used consistently for tools_used/degraded_from
# construction AND for flattening the evidence list (design §5) — one
# ordering, reused everywhere in this module so there is never a mismatch
# between "the order chunks were flattened in" and "the order tools are
# reported in".
_CANONICAL_TOOL_ORDER: tuple[SourceTool, ...] = ("RAG", "GRAPH", "SQL")


def _generation_role(preferred_language: Optional[str]) -> str:
    """
    Mirrors every prior sub-agent module's own inline `_generation_role()`
    rather than importing one — no cross-sub-agent coupling, per this
    session's scope. Same finding each module already documents: the
    Urdu-fine-tuned local generation-slot model ignores an explicit "reply
    in English" instruction, so non-Urdu responses route through the
    reasoning slot instead.
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
        label = SOURCE_TOOL_DISPLAY_LABELS.get(chunk.metadata.source_tool, chunk.metadata.source_tool)
        source = chunk.metadata.source_file or "unknown"
        parts.append(f"[Document {i}] Source: {label} ({source})\n{chunk.text}")
    return "\n\n".join(parts)


def _chunk_to_verifier_dict(chunk: EvidenceChunk) -> dict:
    """
    Flatten one EvidenceChunk into verify_grounding()'s pre-existing flat
    dict shape — same reshape every prior sub-agent's own helper performs,
    not a new interface (design §5 decision (a)).
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
    Local, sub-agent-internal normalization of one composed tool's result —
    NOT a harness type, never crosses this module's boundary. Exists so the
    three tools' shapes (each with its own result subclass, and GRAPH's
    unwrapped-exception risk — see module docstring) can be branched on
    uniformly below, and so the live-event emission has one place to read
    "did this contribute" / "should this read as retry or error" from.
    """

    tool: SourceTool
    usable: bool  # status == OK (chunks non-empty per ToolResult's own contract)
    chunks: list[EvidenceChunk]
    error: Optional[ToolError]
    fallback_to_rag: bool  # only meaningful when usable is False


def _emit(on_event: Optional[OnEventCallback], step: str, status: str, detail: str) -> None:
    if on_event is None:
        return
    on_event(PipelineEvent(step=step, status=status, detail=detail))


async def _run_rag(agent_input: SubAgentInput, on_event: Optional[OnEventCallback]) -> _ToolOutcome:
    try:
        result = await rag_tool(
            RagToolInput(query_text=agent_input.query_text, execution=agent_input.execution)
        )
    except Exception as exc:
        logger.error("Investigative Analysis: RAG tool raised: %s", exc)
        _emit(on_event, "analysis:rag", "error", f"RAG tool raised an exception: {exc}")
        return _ToolOutcome(
            tool="RAG", usable=False, chunks=[], error=ToolError(kind="upstream_failure", message=str(exc)),
            fallback_to_rag=False,
        )

    if result.status == ToolStatus.OK:
        _emit(on_event, "analysis:rag", "done", f"RAG returned {len(result.chunks)} usable chunk(s)")
        return _ToolOutcome(tool="RAG", usable=True, chunks=result.chunks, error=None, fallback_to_rag=False)

    # RagToolResult.fallback_to_rag is pinned False (design §2.1: RAG is the
    # fallback TARGET, it has no fallback of its own) — so a non-OK RAG
    # outcome always reads as "error", never "retry".
    _emit(on_event, "analysis:rag", "error", "RAG found nothing usable for this query")
    return _ToolOutcome(tool="RAG", usable=False, chunks=[], error=result.error, fallback_to_rag=False)


async def _run_graph(agent_input: SubAgentInput, on_event: Optional[OnEventCallback]) -> _ToolOutcome:
    try:
        result = await graph_tool(
            GraphToolInput(
                query_text=agent_input.query_text,
                execution=agent_input.execution,
                # [Reconciliation fix — harness-reconciliation Unit 8]
                # Previously hardcoded None. Legacy's GRAPH route forwards
                # the router's target_entity (orchestrator.py:858) — an
                # entity-focused analytical question ("how is X connected
                # to Y") traverses from that entity; without it the
                # traversal has no anchor and falls back to whatever the
                # query text alone matches.
                target_entity=agent_input.target_entity,
                hybrid=False,  # composed by THIS sub-agent, not GRAPH_HYBRID's internal fusion
            )
        )
    except Exception as exc:
        # [Flagged in module docstring] graph_tool()/retrieve_graph() has no
        # equivalent try/except of its own today — this sub-agent must not
        # let that propagate uncaught. No formal ToolResult exists here, so
        # there is no fallback_to_rag signal to read -> "error", matching
        # §2.1.4's own "hard failure, no fallback signal" example.
        logger.error("Investigative Analysis: GRAPH tool raised: %s", exc)
        _emit(on_event, "analysis:graph", "error", f"GRAPH tool raised an exception: {exc}")
        return _ToolOutcome(
            tool="GRAPH", usable=False, chunks=[], error=ToolError(kind="upstream_failure", message=str(exc)),
            fallback_to_rag=False,
        )

    if result.status == ToolStatus.OK:
        _emit(on_event, "analysis:graph", "done", f"GRAPH returned {len(result.chunks)} usable chunk(s)")
        return _ToolOutcome(tool="GRAPH", usable=True, chunks=result.chunks, error=None, fallback_to_rag=False)

    if result.fallback_to_rag:
        _emit(on_event, "analysis:graph", "retry", "GRAPH found nothing usable; falling back to RAG")
    else:
        # Defensive only — GraphToolResult's own contract sets
        # fallback_to_rag=True on every non-OK outcome today (design §2.2).
        _emit(on_event, "analysis:graph", "error", "GRAPH found nothing usable, with no fallback signal")
    return _ToolOutcome(
        tool="GRAPH", usable=False, chunks=[], error=result.error, fallback_to_rag=result.fallback_to_rag
    )


async def _run_sql(agent_input: SubAgentInput, on_event: Optional[OnEventCallback]) -> _ToolOutcome:
    try:
        result = await sql_tool(
            SqlToolInput(query_text=agent_input.query_text, execution=agent_input.execution)
        )
    except Exception as exc:
        # Defensive only — sql_tool()'s own try/except (confirmed by reading
        # tools/sql.py) already catches everything it calls and returns a
        # typed FAILED+fallback_to_rag=True result; this branch guards
        # against that changing silently underneath this sub-agent.
        logger.error("Investigative Analysis: SQL tool raised: %s", exc)
        _emit(on_event, "analysis:sql", "error", f"SQL tool raised an exception: {exc}")
        return _ToolOutcome(
            tool="SQL", usable=False, chunks=[], error=ToolError(kind="upstream_failure", message=str(exc)),
            fallback_to_rag=False,
        )

    if result.status == ToolStatus.OK:
        _emit(on_event, "analysis:sql", "done", f"SQL returned {result.row_count} row(s)")
        return _ToolOutcome(tool="SQL", usable=True, chunks=result.chunks, error=None, fallback_to_rag=False)

    if result.fallback_to_rag:
        _emit(on_event, "analysis:sql", "retry", "SQL found nothing usable; falling back to RAG")
    else:
        # Defensive only — SqlToolResult's own contract sets
        # fallback_to_rag=True on every non-OK outcome today (design §2.6).
        # This is the "SQL fails outright -> error" shape SUBAGENT_INTERFACES.md
        # §2.1.4 illustrates: no fallback signal available to read.
        _emit(on_event, "analysis:sql", "error", "SQL failed with no fallback signal available")
    return _ToolOutcome(
        tool="SQL", usable=False, chunks=[], error=result.error, fallback_to_rag=result.fallback_to_rag
    )


def _verifier_passed(verification: dict) -> bool:
    return bool(verification.get("grounded", False)) and not verification.get("off_topic", False)


async def _generate_and_verify(
    agent_input: SubAgentInput, chunks: list[EvidenceChunk]
) -> tuple[Optional[str], dict]:
    """
    Generate one answer over `chunks` (already the exact, ordered,
    flattened list this call's citations/Verifier call must agree on) and
    run `verify_grounding()` over that same list. Returns
    `(answer_or_None, verification_dict)` — `answer` is `None` only if
    generation itself raised.
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
        logger.error("Investigative Analysis: generation failed: %s", exc)
        return None, {}

    verification = await verify_grounding(
        answer=answer,
        cited_chunks=[_chunk_to_verifier_dict(c) for c in chunks],
        case_id=caller.active_case_id,
    )
    return answer, verification


async def investigative_analysis(
    agent_input: SubAgentInput,
    *,
    on_event: Optional[OnEventCallback] = None,
    gateway: Optional[DataGateway] = None,
) -> SubAgentResult:
    """The Investigative Analysis sub-agent. See module docstring for the full contract."""
    outcomes = {
        o.tool: o
        for o in await asyncio.gather(
            _run_rag(agent_input, on_event),
            _run_graph(agent_input, on_event),
            _run_sql(agent_input, on_event),
        )
    }

    tools_used: list[SourceTool] = [t for t in _CANONICAL_TOOL_ORDER if outcomes[t].usable]
    degraded_from: list[SourceTool] = [t for t in _CANONICAL_TOOL_ORDER if not outcomes[t].usable]

    # [RESOLVED-4, this row's own explicit text, applied literally] All
    # three failed/empty -> ABSTAINED, NOT EMPTY. Deliberately does not
    # mirror Case Summarization's "both empty -> EMPTY" convention — see
    # module docstring's "STATUS THRESHOLDS" note.
    if not tools_used:
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=[
                "No usable data was found from case documents, the case graph, "
                "or penal-code reference lookup for this question."
            ],
        )

    # ── Flatten (design §5): canonical RAG-then-GRAPH-then-SQL order, only
    # the tools that actually contributed — same order used for tools_used
    # above, so citation numbering and the roll-up are never at odds.
    flattened: list[EvidenceChunk] = []
    for tool in _CANONICAL_TOOL_ORDER:
        if outcomes[tool].usable:
            flattened.extend(outcomes[tool].chunks)

    answer, verification = await _generate_and_verify(agent_input, flattened)
    if answer is None:
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=["Analysis generation failed; no answer could be produced."],
        )
    if not _verifier_passed(verification):
        # [PRESERVE — SubAgentResult docstring, every prior sub-agent's own
        # convention] An answer that failed verification is NEVER served.
        # tools_used/degraded_from stay at their SubAgentResult defaults
        # ([]) here — see module docstring's "A SEPARATE, LATER REASON FOR
        # ABSTAINED" note for why this is not the same roll-up the live
        # events already reported.
        logger.warning(
            "Investigative Analysis: verifier rejected synthesized answer: %s",
            (verification.get("reason") or "")[:150],
        )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=[
                "The generated analysis could not be verified as grounded in "
                "the retrieved material."
            ],
        )

    # Validation gate — plan §5's FULL semantic tier is MANDATORY here (same
    # tier as Cross-Case Linkage / Report Drafting, not the lighter
    # structural-only check) — one batched local-LLM entailment call.
    validation_status, validation_claims = await validate_answer(
        answer_text=answer,
        cited_chunks=[_chunk_to_verifier_dict(c) for c in flattened],
        tier="full",
    )

    caveats: list[str] = []
    if degraded_from:
        degraded_labels = ", ".join(
            SOURCE_TOOL_DISPLAY_LABELS.get(t, t) for t in degraded_from
        )
        caveats.append(
            f"This analysis could not draw on: {degraded_labels}. It reflects "
            "only the sources that returned usable data."
        )
    caveats.extend(caveats_for_validation(validation_status, validation_claims))

    return SubAgentResult(
        status=SubAgentStatus.PARTIAL if degraded_from else SubAgentStatus.OK,
        answer_text=answer,
        citations=_citations_for(flattened),
        tools_used=tools_used,
        degraded_from=degraded_from,
        caveats=caveats,
        validation_status=validation_status,
        validation_claims=validation_claims,
    )


investigative_analysis.name = INVESTIGATIVE_ANALYSIS

# Import-time self-registration — the pattern every prior sub-agent module
# established (supervisor.py's own module docstring documents it).
# Importing this module is what makes Investigative Analysis live in the
# module-level registry; nothing else imports it yet, so this has no effect
# on live traffic today (per §6, still not wired into
# main.py/orchestrator.py/router.py).
register(investigative_analysis)
