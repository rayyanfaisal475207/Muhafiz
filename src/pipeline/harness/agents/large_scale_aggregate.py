"""
Large-Scale Aggregate sub-agent —
src/pipeline/harness/agents/large_scale_aggregate.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 2, this session's "Phase 3" —
see the naming disambiguation in this session's brief: "Phase 3" here means
this checklist item, NOT the Verifier/Citation-Consistency/Validation trust
layer, which is a later, unrelated checklist item).

Source of truth: SUBAGENT_INTERFACES.md §2.1's "Large-Scale Aggregate" row
("Composes: XAGG. Bounded payload: the computed aggregate + its
natural-language summary. Never the full case row set. Partial-failure:
Verifier-rejection fallback to raw aggregate text is XAGG's own existing
behavior — the sub-agent passes that through unchanged, does not add a
second layer of fallback"), AGENT_HARNESS_DESIGN.md §2.4/§3's matching
entries, and this session's explicit brief.

SCOPE. Composes exactly one tool —
`src.pipeline.harness.tools.xagg.xagg_tool` (Phase 0), called as-is, never
re-wrapped or re-implemented. XAGG's own role gate (supervisor /
station-admin / platform-admin only, design §4.3) lives INSIDE
`run_aggregate()` and is NOT duplicated here — this sub-agent only
translates the tool's resulting `ToolStatus` into a `SubAgentStatus`,
exactly the way `xagg_tool()` itself only translates `run_aggregate()`'s
`PermissionError` into `status=DENIED` rather than re-deriving the check.

STATUS MAPPING (per this session's explicit brief):
  - XAGG DENIED  -> SubAgentStatus.DENIED, propagated as its own status.
    [PRESERVE — SUBAGENT_INTERFACES.md RESOLVED-6] "Applies to any future
    cross-case sub-agent, not only [Cross-Case Linkage]" — never collapsed
    into ABSTAINED or EMPTY. A denial is a security-relevant signal
    ("blocked by permissions"), distinct from "searched and found nothing";
    flattening the two destroys that distinction for monitoring/audit.
  - XAGG FAILED  -> SubAgentStatus.ABSTAINED, tool's error propagated.
  - XAGG EMPTY   -> SubAgentStatus.EMPTY, not an error. NOTE: as of this
    writing `xagg_tool()`/`run_aggregate()` never actually produce this
    status — every canned aggregate family resolves to `status=OK` with a
    "(no matching cases found)" rendering inside `raw_summary_text` even
    when zero rows match (see xagg.py's `_render_aggregate_text()`). This
    branch is kept, exactly as semantic_search.py kept a defensive
    DENIED-from-RAG branch for a status its own composed tool never
    produces either, so an unexpected future change to `xagg_tool()` (e.g.
    a canned family that legitimately has nothing to compute over) can't
    silently fall through unhandled — `ToolStatus.EMPTY` is part of the
    tool's declared contract (types.py) even though this specific tool's
    current implementation never reaches it.
  - XAGG OK      -> generate a natural-language paraphrase of the computed
    aggregate via `call_llm()` (same self-contained-prompt convention
    Semantic Search established in Phase 2 — one system prompt built from
    the tool's own bounded/synthetic evidence, no coupling to
    orchestrator.py's `_FINAL_PROMPT_TEMPLATE`), citing the tool's single
    synthetic chunk as `[Document 1]`, then verified via the EXISTING,
    UNCHANGED `src.pipeline.verifier.verify_grounding()` — passing
    `cross_case_ids=tool_result.case_ids_touched`
    [PRESERVE — design §4.6, XAggToolResult.case_ids_touched docstring]
    so the leakage backstop has the tool's allowed-cross-case-ID list, even
    though (per xagg.py's own tool-level docstring) the synthetic aggregate
    chunk carries no `metadata.case_id` and is therefore inert to that
    specific check today — passing it is still correct because it is what
    the contract specifies and it costs nothing to be defensive here.
    `case_id="cross_case"`, matching orchestrator.py's own XAGG verifier
    call (`orchestrator.py` line ~1337) and `test_verifier.py`'s documented
    convention for XGRAPH/XAGG routes.
      - Verifier PASSES  -> SubAgentStatus.OK, answer_text = the paraphrase,
        tools_used=["XAGG"].
      - Verifier REJECTS -> see "VERIFIER-REJECTION STATUS DECISION" below.

VERIFIER-REJECTION STATUS DECISION (this session's brief flags this as
undecided by the docs and asks for an explicit call, not a silent pick):

`XAggToolResult`/`xagg.py`'s own docstrings, and SUBAGENT_INTERFACES.md
§1.5/§2.1's Large-Scale Aggregate row, are unanimous that a Verifier
rejection here is NOT a generic abstention: the aggregate is machine-
computed and correct by construction (a real SQL/Cypher count or listing),
so a rejection means the LLM's natural-language PARAPHRASE of that data
failed to faithfully reproduce it — not that the evidence is unsound or
thin. `orchestrator.py`'s own XAGG route (the pre-harness behavior this
sub-agent must not regress) already encodes this: on verifier rejection it
serves `raw_summary_text` (prefixed with an explanatory line) as
`full_response` and DELIVERS it — it does not abstain, and it does not
mark the response any differently from a successful one in the SSE/log
status it emits (`response_type = "xagg"` either way; a `regenerated` flag
rides along on the `citation_validator` event, not on a distinct response
status).

Decision: **`SubAgentStatus.OK`**, `answer_text = tool_result.raw_summary_text`
(unparaphrased), with a `caveats` entry stating the answer is the raw
computed aggregate because the natural-language summary could not be
verified. `tools_used=["XAGG"]` (the tool succeeded and its data is what is
served — just not paraphrased), `degraded_from=[]` (XAGG itself did not
degrade; only this sub-agent's own generation step did, and `degraded_from`
per RESOLVED-4 is about which TOOLS were attempted-and-fell-back, not about
this sub-agent's own generation retry surface).

Reasoning against the alternatives, made explicit per this session's
instruction not to silently pick a value:
  - NOT `ABSTAINED`: `SubAgentResult.answer_text`'s own [PRESERVE] docstring
    ("An answer that failed verification is NEVER served") is written for
    the ordinary case — an LLM-authored answer whose grounding in retrieved
    evidence could not be confirmed, where serving it risks presenting an
    ungrounded (possibly fabricated) claim as fact. That risk model does not
    apply here: `raw_summary_text` is not the rejected, unverified
    generation — it is a DIFFERENT, deterministic string that was never
    submitted to the Verifier at all and needs no grounding check, because
    it has no claims beyond "this is what the aggregate computed," the same
    way `PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE` (SUBAGENT_INTERFACES.md
    §2.1.1) is exempted from verification for the same reason: a
    machine-produced, fact-shaped string with nothing to hallucinate.
    Abstaining would discard a correct, useful answer because a *different*
    piece of text (the paraphrase) failed a check that string was never
    subjected to — the same "don't discard genuinely useful evidence"
    principle RESOLVED-2 applies to Case Summarization's symmetric
    degradation.
  - NOT `PARTIAL`: `PARTIAL` is defined (RESOLVED-4, `degraded_from`'s
    docstring) around TOOL-level degradation — "attempted but failed,
    empty, or fell back" tools recorded in `degraded_from`, with
    `tools_used` narrower than what was attempted. Here exactly one tool
    was attempted and it fully succeeded (`status=OK`, real chunks, real
    `case_ids_touched`); nothing degraded at the tool-composition level
    this sub-agent is responsible for reporting on. Marking this `PARTIAL`
    would overload that field with a second, unrelated meaning
    ("generation-step quality") that `degraded_from`/`tools_used` have no
    vocabulary to express (there is no tool to list in `degraded_from` —
    XAGG did not degrade), inventing an implicit meaning for `PARTIAL`
    the interfaces doc never assigns it.
  - `OK` best matches what orchestrator.py already does today (serves the
    result, does not abstain, does not treat it as a lesser/partial
    outcome in the persisted log), and keeps `SubAgentResult.answer_text`
    always non-None whenever `XAGG` itself succeeded — exactly the
    continuity `raw_summary_text` exists to provide per XAggToolResult's
    own docstring ("this is what gets served on Verifier rejection").
  - The caveat entry is what carries the "unparaphrased/raw format" signal
    forward for a human reader, the same pattern Case Summarization's
    GRAPH-only in-text disclosure (RESOLVED-2a) and Report Drafting's
    partial-evidence disclosure (RESOLVED-3) use for a comparable "the
    supervisor's typed fields alone under-communicate this" situation —
    except here it rides in `SubAgentResult.caveats`, not baked into
    `answer_text` itself, since `raw_summary_text` is not investigator-
    template prose that needs an inline disclosure; it is already visibly
    the "raw" shape (a bare listing/count), and the caveat text is what a
    supervisor-level consumer reads to know why.

BOUNDED PAYLOAD (per SUBAGENT_INTERFACES.md §2.1's table): `answer_text`
(the paraphrase or, on rejection, the raw rendering) plus one `Citation`
pointing at the tool's single synthetic chunk — never
`tool_result.chunks`/`EvidenceChunk` itself, never the underlying case rows
`run_aggregate()` computed over. The chunk list stays inside this
function's stack frame exactly as Semantic Search's RAG chunks do.

`degraded_from` STAYS `[]` UNCONDITIONALLY (per this session's brief):
XAGG's own `fallback_to_rag` is pinned `Literal[False]`
(`CrossCaseToolResult`) — it never falls back to another tool, so there is
nothing for this sub-agent to record as attempted-and-fell-back. This
mirrors Semantic Search's Phase 2 rationale for the same field (RAG is the
fallback TARGET, not a tool with a fallback of its own) — the mirror-image
case here: XAGG is a NEVER-FALLBACK cross-case tool, not a fallback target,
but the effect on this field is the same "always empty."

VALIDATION GATE — WIRED (Validation-module session). Plan §5's table puts
Large-Scale Aggregate on the STRUCTURAL-ONLY tier ("Validation only needs
its cheap structural check here" — plan §4 row 2's own build note, "does
the reported count match the raw rows"). Run at BOTH branches below (the
verifier-passed paraphrase, and the verifier-rejected raw-fallback), not
just one: `validate_answer()` finds zero `[Document N]` markers in
`raw_summary_text` (xagg.py's own renderer never emits any) and returns
`SKIPPED` there by construction, so wiring it uniformly at both branches
costs nothing on the raw-fallback path and needs no special-casing.

NOT IN SCOPE THIS PHASE: any other sub-agent, `validation.py` itself, live
wiring into main.py/orchestrator.py/router.py.
"""

from __future__ import annotations

import logging
from typing import Optional

from src.data_gateway.base import DataGateway
from src.llm.client import call_llm
from src.pipeline.harness.supervisor import LARGE_SCALE_AGGREGATE, register
from src.pipeline.harness.tools.xagg import XAggToolInput, xagg_tool
from src.pipeline.harness.types import (
    Citation,
    EvidenceChunk,
    OnEventCallback,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolStatus,
)
from src.pipeline.validation import caveats_for_validation, validate_answer
from src.pipeline.verifier import verify_grounding

logger = logging.getLogger(__name__)

# Self-contained, sub-agent-scoped prompt — same convention Semantic Search
# established (semantic_search.py's own module docstring): no coupling to
# orchestrator.py's `_CROSS_CASE_AGG_PROMPT_TEMPLATE` (which takes
# parameters, e.g. full conversation history, that SubAgentInput does not
# carry), still enforces the same [Document N] citation contract
# verify_grounding()'s deterministic checks depend on. XAGG's tool wrapper
# always emits exactly one synthetic chunk, so this prompt is written for
# that single-document shape rather than the generalized N-document loop
# _format_documents_for_prompt() uses in semantic_search.py.
_SYSTEM_PROMPT_TEMPLATE = (
    "You are a police cross-case statistics assistant. The document below "
    "is a computed aggregate result — counts or a listing derived directly "
    "from the case database, already correct. Paraphrase it into a clear, "
    "natural-language answer to the user's question. Do not add, omit, or "
    "alter any number, name, or case ID from the document. Cite it as "
    "[Document 1].\n\n"
    "Respond in {preferred_language}.\n\n"
    "--- DOCUMENTS ---\n[Document 1] Source: cross-case aggregate\n{aggregate_text}\n"
    "--- END OF DOCUMENTS ---"
)


def _generation_role(preferred_language: Optional[str]) -> str:
    """
    Mirrors semantic_search.py's own inline `_generation_role()` (itself
    mirroring orchestrator.py's `_generation_role()`) rather than importing
    either — no coupling to orchestrator.py or to another sub-agent module,
    per this session's scope. Same finding: the Urdu-fine-tuned local
    generation-slot model ignores an explicit "reply in English"
    instruction, so non-Urdu responses route through the reasoning slot.
    """
    return "generation" if preferred_language == "Urdu" else "reasoning"


def _chunk_to_verifier_dict(chunk: EvidenceChunk) -> dict:
    """Flatten one EvidenceChunk into verify_grounding()'s pre-existing flat
    dict shape — same reshape semantic_search.py's own helper performs, not
    a new interface (design §5 decision (a))."""
    return {
        "id": chunk.id,
        "text": chunk.text,
        "metadata": chunk.metadata.model_dump(),
    }


async def large_scale_aggregate(
    agent_input: SubAgentInput,
    *,
    on_event: Optional[OnEventCallback] = None,
    gateway: Optional[DataGateway] = None,
) -> SubAgentResult:
    """
    The Large-Scale Aggregate sub-agent. See module docstring for the contract.

    [AMENDMENT — pre-Phase-7 contract amendment, mirrors §10/§11's pattern]
    `on_event` is accepted for `SubAgent` protocol conformance and otherwise
    ignored — this sub-agent composes exactly one tool, nothing granular to
    report per-source (SUBAGENT_INTERFACES.md §2.1.4 is Investigative
    Analysis's own requirement, Phase 7).
    """
    execution = agent_input.execution
    caller = execution.caller

    # [Reconciliation fix — harness-reconciliation Unit 4] `target_entity`
    # selects the aggregate KIND inside `run_aggregate()`: with an entity it
    # computes graph recurrence ("which cases does X appear in"), without one
    # it computes case counts (matching legacy's own forwarding of the
    # router's value, orchestrator.py:1288). This call previously never
    # forwarded it, so every recurrence question was silently answerable
    # only as a case count.
    tool_result = await xagg_tool(
        XAggToolInput(
            query_text=agent_input.query_text,
            execution=execution,
            target_entity=agent_input.target_entity,
        )
    )

    if tool_result.status == ToolStatus.DENIED:
        # [PRESERVE — RESOLVED-6] Propagated as its own status, never
        # collapsed into ABSTAINED/EMPTY. The role gate itself already ran
        # (and already audit-logged) inside run_aggregate() via xagg_tool() —
        # this sub-agent does not re-check or re-derive it (design §3.1 /
        # SUBAGENT_INTERFACES.md §2.1's Cross-Case Linkage row: "do not add
        # a third gate at the sub-agent level").
        return SubAgentResult(
            status=SubAgentStatus.DENIED,
            error=tool_result.error,
            caveats=["Cross-case aggregate access was denied for your role."],
        )

    if tool_result.status == ToolStatus.FAILED:
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=tool_result.error,
            caveats=["Cross-case aggregate computation failed; no answer could be produced."],
        )

    if tool_result.status == ToolStatus.EMPTY:
        # Defensive only -- see module docstring's "XAGG EMPTY" note: no
        # canned aggregate family in the current xagg_tool()/run_aggregate()
        # implementation actually produces this status today (an empty
        # match renders as "(no matching cases found)" inside a status=OK
        # raw_summary_text instead). Kept so an unexpected future change to
        # the tool can't silently fall through unhandled.
        return SubAgentResult(
            status=SubAgentStatus.EMPTY,
            caveats=["No cases matched this aggregate query."],
        )

    # status == OK: a real, deterministic aggregate to paraphrase (ToolResult's
    # own contract: "chunks non-empty iff status is OK"; XAggToolResult always
    # populates exactly one synthetic chunk plus raw_summary_text).
    chunk = tool_result.chunks[0]
    resolved_language = caller.preferred_language or "the same language as the user's question"
    system_prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        preferred_language=resolved_language,
        aggregate_text=tool_result.raw_summary_text,
    )

    citations = [
        Citation(
            document_index=1,
            source_tool=chunk.metadata.source_tool,
            case_id=chunk.metadata.case_id,
            source_file=chunk.metadata.source_file,
            confidence=chunk.metadata.confidence,
        )
    ]

    try:
        paraphrase = await call_llm(
            system_prompt,
            agent_input.query_text,
            role=_generation_role(caller.preferred_language),
        )
    except Exception as exc:
        # Generation itself raising (e.g. the LLM service is unreachable) is
        # a different failure shape from a Verifier REJECTION of a produced
        # answer -- there is no candidate text to fall back to
        # raw_summary_text FROM here, unlike the verifier-rejection branch
        # below, so this stays a plain abstention, symmetric with
        # semantic_search.py's own generation-exception handling.
        logger.error("Large-Scale Aggregate: generation failed: %s", exc)
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            caveats=["Aggregate summary generation failed; no answer could be produced."],
        )

    verification = await verify_grounding(
        answer=paraphrase,
        cited_chunks=[_chunk_to_verifier_dict(chunk)],
        case_id="cross_case",
        cross_case_ids=tool_result.case_ids_touched,
    )
    verifier_passed = verification.get("grounded", False) and not verification.get("off_topic", False)

    if not verifier_passed:
        logger.warning(
            "Large-Scale Aggregate: verifier rejected paraphrase, serving raw aggregate: %s",
            (verification.get("reason") or "")[:150],
        )
        # [PRESERVE — design §2.4, XAggToolResult docstring] NOT a generic
        # abstention -- see the module docstring's "VERIFIER-REJECTION
        # STATUS DECISION" for the full reasoning. The aggregate is
        # machine-computed and correct by construction; a rejection here
        # means the paraphrase failed, not that the evidence is unsound.
        raw_validation_status, raw_validation_claims = await validate_answer(
            answer_text=tool_result.raw_summary_text,
            cited_chunks=[_chunk_to_verifier_dict(chunk)],
            tier="structural",
        )
        return SubAgentResult(
            status=SubAgentStatus.OK,
            answer_text=tool_result.raw_summary_text,
            citations=citations,
            tools_used=["XAGG"],
            caveats=[
                "The natural-language summary could not be verified as an "
                "accurate paraphrase; showing the raw computed aggregate instead."
            ] + caveats_for_validation(raw_validation_status, raw_validation_claims),
            validation_status=raw_validation_status,
            validation_claims=raw_validation_claims,
        )

    # Validation gate — plan §5's STRUCTURAL-ONLY tier for this sub-agent.
    validation_status, validation_claims = await validate_answer(
        answer_text=paraphrase,
        cited_chunks=[_chunk_to_verifier_dict(chunk)],
        tier="structural",
    )

    return SubAgentResult(
        status=SubAgentStatus.OK,
        answer_text=paraphrase,
        citations=citations,
        tools_used=["XAGG"],
        caveats=caveats_for_validation(validation_status, validation_claims),
        validation_status=validation_status,
        validation_claims=validation_claims,
    )


large_scale_aggregate.name = LARGE_SCALE_AGGREGATE

# Import-time self-registration -- the same pattern semantic_search.py
# established (supervisor.py's own module docstring documents it for every
# future sub-agent module). Importing this module is what makes Large-Scale
# Aggregate live in the module-level registry; nothing else imports it yet,
# so this has no effect on live traffic today (per §6, still not wired into
# main.py/orchestrator.py/router.py).
register(large_scale_aggregate)
