"""
Local Search sub-agent — src/pipeline/harness/agents/local_search.py
(findings.md Module 8, "Local Search — entity-based reasoning").

Source of truth: findings.md's Module 8 section (MS GraphRAG's Local Search
description, the three confirmed gaps, the "reuse existing machinery"
proposed shape) and this session's own approved plan
(steady-moseying-yeti.md). Modeled on semantic_search.py's and
cross_case_linkage.py's own conventions per this session's explicit
instruction — SUBAGENT_INTERFACES.md's contract shape, the
role-gate-lives-inside-the-composed-tool discipline (here: NO role gate,
same as GRAPH — within-case, case-assignment-scoped only), and the
self-contained-prompt-plus-verification pattern every prior sub-agent
follows.

SCOPE. Composes TWO tools:
  - `src.pipeline.harness.tools.local_search.local_search_tool` (this
    session) — the primary path: semantic entity-access-point match ->
    retrieve_graph() fan-out -> community-report join -> rerank -> evaluate.
  - `src.pipeline.harness.tools.rag.rag_tool` — the FALLBACK target, exactly
    like every other GRAPH-shaped consumer in this codebase degrades to RAG
    (GRAPH/GRAPH_HYBRID's own `fallback_to_rag` contract, Case
    Summarization's symmetric-degradation precedent). Not literally spelled
    out in findings.md's Module 8 proposal, but the natural, minimal
    extension of the SAME project-wide "GRAPH degrades to RAG" invariant
    every sibling sub-agent already honors, rather than leaving Local
    Search as the one GRAPH-shaped sub-agent that bare-ABSTAINs on
    `fallback_to_rag=True` instead of composing the fallback tool the way
    the field's own docstring says the CALLER should.

SourceTool TAGGING. `local_search_tool` already tags every chunk it emits
`source_tool="GRAPH"` (see that module's own docstring for why — no new
SourceTool value). `tools_used=["GRAPH"]` when it contributes;
`tools_used=["RAG"]`/`degraded_from=["GRAPH"]` on the RAG-fallback path —
consistent with how every other GRAPH-composing sub-agent already reports.

VALIDATION GATE. STRUCTURAL-ONLY tier, matching Case Summarization — the
closest existing risk-class analogue (within-case, entity-centric graph
evidence, not a cross-case identity claim, which is what earns the FULL
semantic tier elsewhere).

PARTIAL-FAILURE MAPPING:
  - local_search_tool FAILED                      -> ABSTAINED
  - local_search_tool DENIED (defensive only —
    the tool carries no role gate and never
    actually produces this)                        -> ABSTAINED
  - local_search_tool EMPTY (no case in scope, no
    semantic match, or evaluator rejection) and
    rag_tool OK & grounded                          -> PARTIAL,
                                                        tools_used=["RAG"],
                                                        degraded_from=["GRAPH"]
  - local_search_tool EMPTY and rag_tool also
    EMPTY/FAILED/ungrounded                          -> EMPTY (nothing was
                                                        even in scope) or
                                                        ABSTAINED (search ran,
                                                        found nothing
                                                        verifiable) — same
                                                        distinction
                                                        semantic_search.py's
                                                        own RAG composition
                                                        already draws
  - local_search_tool OK, generated answer fails
    verify_grounding()                               -> ABSTAINED, no
                                                        answer_text served
  - local_search_tool OK and grounded                -> OK, tools_used=["GRAPH"]

NOT IN SCOPE THIS PHASE: live wiring into main.py/orchestrator.py/router.py
— same "not yet cut over to live chat traffic" scope every prior sub-agent
phase shipped with (config.HARNESS_CUTOVER_ROUTES gates cutover
independently; this module only registers itself into the Supervisor's
dispatch table).
"""

from __future__ import annotations

import logging
from typing import Optional

from src.data_gateway.base import DataGateway
from src.llm.client import call_llm
from src.pipeline.harness.supervisor import LOCAL_SEARCH, register
from src.pipeline.harness.tools.local_search import LocalSearchToolInput, LocalSearchToolResult, local_search_tool
from src.pipeline.harness.tools.rag import RagToolInput, rag_tool
from src.pipeline.harness.types import (
    NAME_FIDELITY_RULE,
    Citation,
    EvidenceChunk,
    OnEventCallback,
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

# Self-contained prompt, same convention as semantic_search.py's own
# _SYSTEM_PROMPT_TEMPLATE and cross_case_linkage.py's
# _XNETWORK_SYSTEM_PROMPT_TEMPLATE — NOT an external prompts/*.txt file.
# Flagged explicitly in the approved plan: neither of the two agent files
# this module was modeled on uses an external prompt file, so this follows
# their actual convention over the brief's literal "new prompts/*.txt"
# phrasing.
_SYSTEM_PROMPT_TEMPLATE = (
    "You are a police case-evidence search assistant answering a question "
    "about a specific entity (a person, officer, vehicle, phone number, or "
    "organization) in this case. Answer using ONLY the documents provided "
    "below, which include both entity-linked case evidence and, where "
    "available, broader community/network context for that entity.\n\n"
    "Every factual claim MUST cite its source as [Document N], where N is "
    "that document's 1-based position below. If the documents do not "
    "contain enough information to answer, say so plainly instead of "
    "guessing or answering from general knowledge.\n\n"
    "Respond in {preferred_language}.\n\n"
    "{conversation_block}"
    "--- DOCUMENTS ---\n{documents}\n--- END OF DOCUMENTS ---"
) + NAME_FIDELITY_RULE

# Appended to the system prompt on the single citation-repair retry (see
# _generate_and_verify). Kept as a suffix rather than a second template so the
# retry answers the SAME question against the SAME documents — only the
# citation requirement is restated more forcefully.
_CITATION_REPAIR_SUFFIX = (
    "\n\nIMPORTANT — your previous attempt was rejected because it contained "
    "no [Document N] citations. Rewrite the answer so that EVERY factual "
    "sentence ends with the marker of the document it came from, e.g. "
    "'The investigating officer is X [Document 2].' Use only the documents "
    "above. Do not invent a citation for a claim the documents do not "
    "support — if the documents cannot answer, say so plainly instead."
)

# Matches verifier.py's uncited-answer gate (the "cites no [Document N] source
# at all" rejection). Only that specific rejection is worth re-prompting for;
# a genuine grounding/off-topic failure must still abstain.
_MISSING_CITATION_MARKERS = ("cites no [document n]", "cites no [document")


def _is_missing_citation_rejection(verification: dict) -> bool:
    """True only for the verifier's 'substantial but uncited' rejection."""
    if verification.get("off_topic"):
        return False
    reason = str(verification.get("reason") or "").lower()
    return any(marker in reason for marker in _MISSING_CITATION_MARKERS)


_NON_PERSON_LABELS = frozenset({"Officer", "Vehicle", "PhoneNumber", "Organization"})

# One caveat per LocalSearchToolResult.empty_reason. Each states only what the
# tool actually observed: none of them may imply the entity is ABSENT from the
# evidence, which the single generic predecessor line did for three of the four
# branches (and flatly contradicted on `evaluator_rejected`, where
# matched_entities is populated).
_DEGRADATION_CAVEATS: dict[str, str] = {
    "no_entity_match": (
        "No entity-index entry matched this question within the active case; "
        "answered from document search instead of entity-graph search."
    ),
    "no_linked_evidence": (
        "Entity matches were found, but no usable linked entity-graph evidence "
        "was available; answered from document search instead."
    ),
    "evaluator_rejected": (
        "Entity matches were found, but the retrieved entity-graph evidence was "
        "not judged sufficiently relevant; answered from document search instead."
    ),
}

# Used when the tool reports EMPTY without a reason (e.g. a stub or an older
# result shape). Deliberately makes no claim about WHY it degraded.
_DEGRADATION_CAVEAT_FALLBACK = (
    "Entity-graph search returned nothing usable; answered from document search instead."
)


def _generation_role(preferred_language: Optional[str]) -> str:
    """Mirrors every prior sub-agent module's own inline `_generation_role()`
    rather than importing one — no cross-sub-agent coupling, same finding
    every one of them documents (the Urdu-fine-tuned local generation-slot
    model ignores an explicit "reply in English" instruction)."""
    return "generation" if preferred_language == "Urdu" else "reasoning"


def _format_documents_for_prompt(chunks: list[EvidenceChunk]) -> str:
    """[Document N] numbering here MUST match Citation.document_index and
    verify_grounding()'s positional chunks[n-1] indexing — same list, same
    order, per EvidenceChunk's [PRESERVE — design §5] contract."""
    parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        source = chunk.metadata.source_file or "unknown"
        parts.append(f"[Document {i}] Source: {source}\n{chunk.text}")
    return "\n\n".join(parts)


def _chunk_to_verifier_dict(chunk: EvidenceChunk) -> dict:
    return {"id": chunk.id, "text": chunk.text, "metadata": chunk.metadata.model_dump()}


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


async def _generate_and_verify(
    agent_input: SubAgentInput, chunks: list[EvidenceChunk],
) -> tuple[Optional[str], dict]:
    """
    Shared generation+verification step for both the primary
    (local_search_tool) and fallback (rag_tool) chunk sources — the prompt/
    citation/verify shape is identical either way, only which chunks and
    which tools_used/degraded_from the caller records differs.

    Returns (answer_text_or_None, verification_dict). answer_text is None
    on a generation exception OR a verifier rejection — the caller decides
    what SubAgentStatus that maps to.
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
            system_prompt, agent_input.query_text, role=_generation_role(caller.preferred_language)
        )
    except Exception as exc:
        logger.error("Local Search: generation failed: %s", exc)
        return None, {"grounded": False, "reason": f"generation failed: {exc}"}

    verification = await verify_grounding(
        answer=answer,
        cited_chunks=[_chunk_to_verifier_dict(c) for c in chunks],
        case_id=caller.active_case_id,
    )
    verifier_passed = verification.get("grounded", False) and not verification.get("off_topic", False)

    # One bounded citation-repair retry. The verifier rejects a substantial
    # answer that carries no [Document N] marker at all (verifier.py's
    # uncited-answer gate) — and this sub-agent was failing that gate on
    # nearly every run, killing the whole turn even though the retrieved
    # evidence was fine (scenario-verify Finding T). The generation itself is
    # usually correct; it just omitted the markers. So re-ask ONCE with an
    # explicit repair instruction rather than abstaining outright.
    #
    # Deliberately narrow: only for the missing-citation rejection, never for
    # a genuine groundedness/off-topic failure (an answer that contradicts or
    # ignores its sources must still ABSTAIN — re-prompting there would risk
    # laundering a hallucination into a cited-looking one). Bounded at one
    # attempt so a persistently uncited model can't loop.
    if not verifier_passed and _is_missing_citation_rejection(verification):
        logger.info("Local Search: retrying once with explicit citation repair instruction.")
        try:
            repaired = await call_llm(
                system_prompt + _CITATION_REPAIR_SUFFIX,
                agent_input.query_text,
                role=_generation_role(caller.preferred_language),
            )
        except Exception as exc:
            logger.error("Local Search: citation-repair retry failed: %s", exc)
            repaired = None

        if repaired:
            repaired_verification = await verify_grounding(
                answer=repaired,
                cited_chunks=[_chunk_to_verifier_dict(c) for c in chunks],
                case_id=caller.active_case_id,
            )
            if repaired_verification.get("grounded", False) and not repaired_verification.get(
                "off_topic", False
            ):
                return repaired, repaired_verification
            verification = repaired_verification
            verifier_passed = False

    if not verifier_passed:
        logger.warning(
            "Local Search: verifier rejected answer: %s", (verification.get("reason") or "")[:150],
        )
        return None, verification
    return answer, verification


async def local_search(
    agent_input: SubAgentInput,
    *,
    on_event: Optional[OnEventCallback] = None,
    gateway: Optional[DataGateway] = None,
) -> SubAgentResult:
    """The Local Search sub-agent. See module docstring for the contract.

    `on_event`/`gateway` accepted for `SubAgent` protocol conformance and
    otherwise ignored — same as every sub-agent through Cross-Case Linkage
    that composes a small, fixed set of tools with nothing granular to
    report per SUBAGENT_INTERFACES.md §2.1.4's own scoping of that
    requirement to Investigative Analysis.
    """
    execution = agent_input.execution
    caller = execution.caller

    tool_result = await local_search_tool(
        LocalSearchToolInput(query_text=agent_input.query_text, execution=execution)
    )

    if tool_result.status == ToolStatus.FAILED:
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=tool_result.error,
            caveats=["Entity search failed; no answer could be produced."],
        )

    if tool_result.status == ToolStatus.DENIED:
        # Defensive only — local_search_tool carries no role gate (within-
        # case only, case-assignment-scoped, same as GRAPH) and never
        # actually produces this status.
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=tool_result.error,
            caveats=["Entity search was denied; no answer could be produced."],
        )

    if tool_result.status == ToolStatus.EMPTY:
        rag_result = await rag_tool(RagToolInput(query_text=agent_input.query_text, execution=execution))

        if rag_result.status != ToolStatus.OK:
            if rag_result.status == ToolStatus.EMPTY and rag_result.evaluator_verdict != "not_relevant":
                # Nothing was even in scope to search on either path — a
                # legitimate "nothing to report", not a failure.
                return SubAgentResult(
                    status=SubAgentStatus.EMPTY,
                    caveats=["Nothing was in scope to search for this question."],
                )
            return SubAgentResult(
                status=SubAgentStatus.ABSTAINED,
                error=rag_result.error,
                caveats=["No sufficiently relevant evidence was found for this question."],
            )

        answer, verification = await _generate_and_verify(agent_input, rag_result.chunks)
        if answer is None:
            return SubAgentResult(
                status=SubAgentStatus.ABSTAINED,
                caveats=["The generated answer could not be verified as grounded in the retrieved documents."],
            )

        validation_status, validation_claims = await validate_answer(
            answer_text=answer,
            cited_chunks=[_chunk_to_verifier_dict(c) for c in rag_result.chunks],
            tier="structural",
        )
        return SubAgentResult(
            status=SubAgentStatus.PARTIAL,
            answer_text=answer,
            citations=_citations_for(rag_result.chunks),
            tools_used=["RAG"],
            degraded_from=["GRAPH"],
            caveats=[
                _DEGRADATION_CAVEATS.get(
                    tool_result.empty_reason or "", _DEGRADATION_CAVEAT_FALLBACK
                ),
                *caveats_for_validation(validation_status, validation_claims),
            ],
            validation_status=validation_status,
            validation_claims=validation_claims,
        )

    # status == OK — local_search_tool contributed real evidence.
    chunks = tool_result.chunks
    answer, verification = await _generate_and_verify(agent_input, chunks)
    if answer is None:
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=["The generated answer could not be verified as grounded in the retrieved documents."],
        )

    validation_status, validation_claims = await validate_answer(
        answer_text=answer,
        cited_chunks=[_chunk_to_verifier_dict(c) for c in chunks],
        tier="structural",
    )
    caveats = list(caveats_for_validation(validation_status, validation_claims))
    if not tool_result.community_reports_included and any(
        m.get("label") in _NON_PERSON_LABELS for m in tool_result.matched_entities
    ):
        caveats.append(
            "No community-level network context is available for this entity type."
        )

    return SubAgentResult(
        status=SubAgentStatus.OK,
        answer_text=answer,
        citations=_citations_for(chunks),
        tools_used=["GRAPH"],
        caveats=caveats,
        validation_status=validation_status,
        validation_claims=validation_claims,
    )


local_search.name = LOCAL_SEARCH

# Import-time self-registration — same pattern every prior sub-agent module
# established (supervisor.py's own module docstring). Importing this module
# is what makes Local Search live in the module-level registry.
register(local_search)
