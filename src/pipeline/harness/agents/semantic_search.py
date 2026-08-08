"""
Semantic Search sub-agent — src/pipeline/harness/agents/semantic_search.py
(Phase 2, AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 1).

Source of truth: SUBAGENT_INTERFACES.md §2.1's "Semantic Search" row
("Composes: RAG. Bounded payload: synthesized answer + top-N citations.
Never the full ranked set. Partial-failure: evaluator rejection after
retry exhaustion -> ABSTAINED."), AGENT_HARNESS_DESIGN.md §3's matching
row, and AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 1's build note
("Simplest slice — build and prove the whole pipeline works end to end
before anything else").

SCOPE. Composes exactly one tool — `src.pipeline.harness.tools.rag.rag_tool`
(Phase 0), called as-is, never re-wrapped or re-implemented. Everything
below this sub-agent's own generation/verification step is Phase 0's
concern, not this module's.

WHAT THIS MODULE ADDS ON TOP OF THE RAG TOOL (the sub-agent's own job,
per design §3 -- a sub-agent is "compose tool(s) + generate + verify +
bound the payload", not just a tool pass-through):
  1. Generation: one `call_llm()` call over the RAG tool's (already
     bounded, already reranked) chunks, prompting for [Document N]
     citations per chunk position.
  2. Grounding check: the EXISTING `verify_grounding()` from
     src/pipeline/verifier.py (unchanged signature, per
     SUBAGENT_INTERFACES.md §2's "Verifier boundary" note and design §5's
     decision (a) -- flatten to the flat `list[{id, text, metadata}]`
     shape verify_grounding already expects). This module does NOT build
     a new Verifier module -- formalizing this call into
     src/pipeline/harness/verifier.py is Phase 3, tracked separately, not
     done here.
  3. Bounding: the returned `SubAgentResult` carries `citations` (no chunk
     text) and `answer_text`, never `EvidenceChunk`/raw rows -- the RAG
     tool's own chunk list stays inside this function's stack frame and is
     never handed to the caller.

VALIDATION GATE -- EXPLICITLY NOT WIRED THIS PHASE. `validation.py`
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §5/§7.1) does not exist yet, and
`SubAgentResult` has no `validation_status` field -- the §7.1 amendment
was proposed but was never actually added to types.py in Phase 0
(confirmed against src/pipeline/harness/types.py before writing this
module). See the `# TODO(validation-gate)` marker at its intended insertion
point below (after the Verifier passes, per plan §7.1's ordering and
§5's Verifier -> Citation-Consistency -> Validation chain). No ad hoc
validation logic is invented here to fill that gap.

[RENAMED -- Large-Scale Aggregate session] This marker used to read
`# TODO(phase-3)`. That name collided with a later session's own internal
use of "Phase 3" for a *different* checklist item (Large-Scale Aggregate,
AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 2) -- this marker was never
about that sub-agent, only about the Validation module (§5/§7.1), a later,
separate checklist item. Renamed to `# TODO(validation-gate)` so the two
are not confused again; no behavior change.

PARTIAL-FAILURE MAPPING (this sub-agent composes exactly one tool, so
there is nothing for it to degrade TO -- `degraded_from` is always empty
here; RAG is itself the fallback TARGET for every other tool, not a
tool with a fallback of its own -- design §2.1):
  - RAG tool FAILED                                -> ABSTAINED
  - RAG tool EMPTY, evaluator_verdict=="not_relevant"
    (the retry loop ran and never got a "relevant" verdict)
                                                     -> ABSTAINED, per this
                                                        phase's explicit
                                                        spec ("same as
                                                        today's RAG route")
  - RAG tool EMPTY, no evaluator ever ran (nothing in
    scope to search at all -- see RagToolInput's
    no-case/no-global short-circuit)                -> EMPTY (legitimate
                                                        "nothing to
                                                        report", not a
                                                        failure)
  - RAG tool OK, but the generated answer fails
    `verify_grounding()`                             -> ABSTAINED, no
                                                        answer_text served
                                                        (SubAgentResult's
                                                        own [PRESERVE] rule)
  - RAG tool OK and grounded                         -> OK, tools_used=["RAG"]
  - RAG tool DENIED -- handled defensively only. RAG carries no role gate
    (design §2.1: "scoping is case-assignment-based, not role-based") and
    the tool never actually produces this status; kept so an unexpected
    future change to rag_tool can't silently fall through unhandled.

NOT IN SCOPE THIS PHASE (per this session's brief): any other sub-agent,
a formalized Verifier module, a Validation module, live wiring into
main.py/orchestrator.py/router.py.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.llm.client import call_llm
from src.pipeline.harness.supervisor import SEMANTIC_SEARCH, register
from src.pipeline.harness.tools.rag import RagToolInput, rag_tool
from src.pipeline.harness.types import (
    Citation,
    EvidenceChunk,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
    ToolStatus,
)
from src.pipeline.verifier import verify_grounding

logger = logging.getLogger(__name__)

# Kept self-contained rather than importing orchestrator.py's own
# `_FINAL_PROMPT_TEMPLATE`/history/project-memory machinery -- that
# template's parameters (project memory, attached-file context, full
# conversation history) have no equivalent in `SubAgentInput` (query_text,
# execution, output_format, conversation_context only, per
# SUBAGENT_INTERFACES.md §2.0 as amended by plan §10), and reusing it would mean either padding in
# fake values for fields this sub-agent doesn't have or partially
# reimplementing orchestrator.py's own prompt assembly here -- neither is
# "composing the RAG tool", which is this module's actual scope. This is a
# smaller, sub-agent-scoped prompt that still enforces the same [Document N]
# citation contract the Verifier's deterministic checks depend on.
_SYSTEM_PROMPT_TEMPLATE = (
    "You are a police case-evidence search assistant. Answer the user's "
    "question using ONLY the documents provided below.\n\n"
    "Every factual claim MUST cite its source as [Document N], where N is "
    "that document's 1-based position below. If the documents do not "
    "contain enough information to answer, say so plainly instead of "
    "guessing or answering from general knowledge.\n\n"
    "Respond in {preferred_language}.\n\n"
    "{conversation_block}"
    "--- DOCUMENTS ---\n{documents}\n--- END OF DOCUMENTS ---"
)


def _generation_role(preferred_language: Optional[str]) -> str:
    """
    Which local model slot answers the final response.

    Mirrors orchestrator.py's own `_generation_role()` fix inline rather
    than importing it (no coupling to orchestrator.py, per this session's
    scope -- AGENT_HARNESS_IMPLEMENTATION_PLAN.md §6): the Urdu-fine-tuned
    local generation-slot model ignores an explicit "reply in English"
    instruction and answers in Urdu regardless (confirmed live, same
    finding orchestrator.py's own comment documents) -- routing every
    non-Urdu response through the reasoning-slot model instead sidesteps
    that bias.
    """
    return "generation" if preferred_language == "Urdu" else "reasoning"


def _format_documents_for_prompt(chunks: list[EvidenceChunk]) -> str:
    """
    [Document N] numbering here MUST match the numbering `Citation.document_index`
    and `verify_grounding()`'s positional `chunks[n-1]` indexing use below --
    same list, same order, per EvidenceChunk's [PRESERVE — design §5] contract.
    """
    parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        source = chunk.metadata.source_file or "unknown"
        parts.append(f"[Document {i}] Source: {source}\n{chunk.text}")
    return "\n\n".join(parts)


def _chunk_to_verifier_dict(chunk: EvidenceChunk) -> dict:
    """
    Flatten one EvidenceChunk back into `verify_grounding()`'s pre-existing
    flat dict shape (design §5's decision (a); SUBAGENT_INTERFACES.md §2's
    "Verifier boundary" note) -- the signature is unchanged, so this is a
    reshape, not a new interface.

    NOTE ON THE CONFIDENCE GAP (AGENT_HARNESS_DESIGN.md §7 / types.py's
    ChunkMetadata.confidence_status docstring): bridging
    `metadata.confidence`/`confidence_status` into the Verifier's legacy
    `graph_confidence` key is a general, cross-tool concern the design doc
    explicitly leaves open and defers past this phase. It does not need
    solving here: RAG never computes a per-chunk confidence (every chunk
    this tool wrapper emits has `confidence_status="not_computed"`), so
    `verify_grounding()`'s hedging check never fires for Semantic Search's
    own evidence regardless of how that bridge eventually gets wired.
    """
    return {
        "id": chunk.id,
        "text": chunk.text,
        "metadata": chunk.metadata.model_dump(),
    }


async def semantic_search(agent_input: SubAgentInput) -> SubAgentResult:
    """The Semantic Search sub-agent. See module docstring for the contract."""
    execution = agent_input.execution
    caller = execution.caller

    tool_result = await rag_tool(RagToolInput(query_text=agent_input.query_text, execution=execution))

    if tool_result.status == ToolStatus.FAILED:
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=tool_result.error,
            caveats=["Document search failed; no answer could be produced."],
        )

    if tool_result.status == ToolStatus.DENIED:
        # Defensive only -- see module docstring's "RAG tool DENIED" note.
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=tool_result.error,
            caveats=["Document search was denied; no answer could be produced."],
        )

    if tool_result.status == ToolStatus.EMPTY:
        if tool_result.evaluator_verdict == "not_relevant":
            # [Phase 2 spec] Evaluator "not relevant" surviving RAG's
            # internal retry loop -> ABSTAINED, no answer_text served.
            # Same outcome as today's orchestrator.py RAG route exhausting
            # config.MAX_RETRIES (that route's `_SAFE_RESPONSE`) -- the
            # actual response text a caller shows for ABSTAINED is the
            # caller's own concern (see SubAgentResult.answer_text's
            # docstring), not something this sub-agent serves itself.
            return SubAgentResult(
                status=SubAgentStatus.ABSTAINED,
                caveats=[
                    "No sufficiently relevant documents were found for this "
                    "question after retrying with query refinements."
                ],
            )
        # Nothing was even in scope to search (no active case and global
        # material excluded -- see RagToolInput's short-circuit) -- a
        # legitimate "nothing to report", distinct from a search that ran
        # and failed to find anything relevant.
        return SubAgentResult(
            status=SubAgentStatus.EMPTY,
            caveats=["Nothing was in scope to search for this question."],
        )

    # status == OK: real, relevant chunks to answer from (ToolResult's own
    # contract: "chunks non-empty iff status is OK").
    chunks = tool_result.chunks
    resolved_language = caller.preferred_language or "the same language as the user's question"
    # [UPGRADED — plan §10.2] conversation_context is now Optional[ConversationContext],
    # not a bare string. This sub-agent only ever consumed the pre-bounded prose
    # summary, so `.summary` is the minimal, behavior-preserving read here --
    # `.project_memory`/`.attachment_refs` are new fields this phase's scope
    # does not use (composing them is a later sub-agent's concern, not a rename
    # side effect).
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
        logger.error("Semantic Search: generation failed: %s", exc)
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
            caveats=["Answer generation failed; no answer could be produced."],
        )

    verification = await verify_grounding(
        answer=answer,
        cited_chunks=[_chunk_to_verifier_dict(c) for c in chunks],
        case_id=caller.active_case_id,
    )
    verifier_passed = verification.get("grounded", False) and not verification.get("off_topic", False)

    if not verifier_passed:
        logger.warning(
            "Semantic Search: verifier rejected answer: %s",
            (verification.get("reason") or "")[:150],
        )
        # [PRESERVE — SubAgentResult docstring] An answer that failed
        # verification is NEVER served -- answer_text stays None.
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=[
                "The generated answer could not be verified as grounded in "
                "the retrieved documents."
            ],
        )

    # TODO(validation-gate): Validation plugs in here -- after the Verifier passes,
    # before the result is returned -- per AGENT_HARNESS_IMPLEMENTATION_PLAN.md
    # §7.1 ("Validation ... runs after the Verifier passes") and §5's ordering
    # (Verifier -> Citation-Consistency [Report Drafting only] -> Validation).
    # `validation.py` does not exist yet and `SubAgentResult` has no
    # `validation_status` field (the §7.1 amendment was proposed, never
    # added to types.py in Phase 0) -- do not invent ad hoc validation logic
    # here to fill that gap.

    citations = [
        Citation(
            document_index=i,
            source_tool=chunk.metadata.source_tool,
            case_id=chunk.metadata.case_id,
            source_file=chunk.metadata.source_file,
            confidence=chunk.metadata.confidence,
        )
        for i, chunk in enumerate(chunks, start=1)
    ]

    return SubAgentResult(
        status=SubAgentStatus.OK,
        answer_text=answer,
        citations=citations,
        tools_used=["RAG"],
    )


semantic_search.name = SEMANTIC_SEARCH

# Import-time self-registration -- the pattern supervisor.py's own module
# docstring documents for future sub-agent modules ("typically at import
# time... a sub-agent registered anywhere is immediately dispatchable
# everywhere"). Importing this module is what makes Semantic Search live
# in the module-level registry; nothing else imports it yet (see module
# docstring's scope note), so this has no effect on live traffic today.
register(semantic_search)
