"""
Cross-Case Linkage sub-agent —
src/pipeline/harness/agents/cross_case_linkage.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 5, "Phase 6" per this session's
brief — the numbered sub-agent build sequence is 0 Foundation -> 1
Supervisor -> 2 Semantic Search -> 3 Large-Scale Aggregate -> 4 Case
Summarization -> 5 Timeline Building -> 6 (this session) Cross-Case
Linkage; the two contract-fix sessions in between were not numbered).

Source of truth: SUBAGENT_INTERFACES.md §2.1's "Cross-Case Linkage" row +
§1.4/§1.6 (XGRAPH/XNETWORK tool contracts, including the XGRAPH-vs-XNETWORK
dispatch rule) + §3's RESOLVED-6, AGENT_HARNESS_DESIGN.md §3/§3.1's matching
entries, and this session's explicit brief.

SCOPE. Composes exactly two tools, called CONCURRENTLY on every invocation
(`asyncio.gather`, matching Case Summarization's Phase 4 precedent) — this
sub-agent does NOT try to pick one tool over the other per query. Per this
session's brief, that is the correct reading of SUBAGENT_INTERFACES.md's own
wording ("if one returns empty/abstains, present whichever succeeded") and
of RESOLVED-6's claim that both tools "always deny together — identical role
sets," which is only true if both are actually invoked every time:
  - `src.pipeline.harness.tools.xgraph.xgraph_tool` (Phase 0) — cross-case
    entity traversal/recurrence.
  - `src.pipeline.harness.tools.xnetwork.xnetwork_tool` (Phase 0) — cross-case
    open-ended thematic/MO pattern synthesis over precomputed community
    summaries.

ROLE GATE — NOT DUPLICATED HERE. Both tools independently enforce the same
cross-case role gate (supervisor / station-admin / platform-admin only,
design §4.3) *inside* `retrieve_graph()`/`run_network_query()` respectively,
each writing its own `authorization_violation` audit record on denial. Per
this session's explicit instruction and SUBAGENT_INTERFACES.md's own warning
("do not add a third gate at the sub-agent level ... redundant and risks
drifting out of sync with the tools' own checks"), this sub-agent adds NO
role check of its own — it only translates the two tools' resulting
`ToolStatus.DENIED` outcomes.

TARGET ENTITY. Neither `SubAgentInput` nor `CallerContext`/`ExecutionContext`
carries a structured, pre-extracted entity name — the same gap Large-Scale
Aggregate's Phase 3 session already hit for `XAggToolInput.target_entity`
(it passes none). This sub-agent follows that exact precedent:
`XGraphToolInput.target_entity` is left `None`, which `XGraphToolInput`'s own
docstring (xgraph.py, SUBAGENT_INTERFACES.md §1.4) documents as a fully valid
and meaningful case — "typed-entity recurrence discovery," not an error or a
degraded mode.

XGRAPH TOOL CONTRACT DISCREPANCY, CONFIRMED AGAINST THE CODE BEFORE WRITING
THIS MODULE (per this session's explicit instruction not to assume from the
docs alone) — flagged, resolved with the user via `AskUserQuestion`, not
guessed:

This session's brief characterized XNETWORK's "verify -> one-shot
cloud-regeneration -> raw-text fallback" behavior as "already built into the
Phase 0 tool wrapper, not something this session re-implements." Reading
`src/pipeline/harness/tools/xnetwork.py` before writing any code shows the
OPPOSITE is true, stated explicitly in that file's own module docstring:
`xnetwork_tool()` calls `run_network_query()` and translates its result/
`PermissionError`, nothing else — no `call_llm()`, no `verify_grounding()`
anywhere in it. That docstring says in so many words: "generation and
verification (including XNETWORK's own one-shot cloud-regeneration-before-
fallback retry) are NOT this primitive's job ... The XNETWORK-specific
cloud-retry behavior belongs to whichever sub-agent (Cross-Case Linkage)
eventually composes this tool with the Verifier — do not implement it
here." Cross-checked against `orchestrator.py`'s own pre-harness XNETWORK
route (~line 1387-1528): the verify -> `force_cloud=True` one-shot
regeneration -> re-verify -> raw-text-fallback sequence lives at the ROUTE
level there too, not inside `run_network_query()`. The tool-level code is
internally consistent; the brief's framing was not accurate to it.

Resolved with the user (AskUserQuestion, not guessed): this sub-agent DOES
implement the full XNETWORK-specific retry sequence itself (see
`_generate_xnetwork_text()` below), mirroring `orchestrator.py`'s existing
XNETWORK route and `xnetwork.py`'s own docstring instruction — this is a
materially larger, higher-stakes piece of this session's build than "wire
through an already-verified result," and is called out here so the scope
difference from the brief's original framing is not silently absorbed.

XGRAPH-VS-XNETWORK DESCRIPTION-GENERATION DECISION (this session's brief
asked for this decision to be made and documented explicitly, same as
Timeline Building's Phase 5 precedent for `TimelineEvent.description`):

  - XGRAPH-derived `CrossCaseLink.description` strings are DETERMINISTIC /
    STRUCTURED — composed from `case_ids_touched`/`hop_count`/
    `chain_confidence` (and `target_entity`, when the caller supplies one),
    never LLM-generated or paraphrased. Rationale, matching the reasoning
    already applied twice in this codebase (Phase 3's `XAggToolResult.
    raw_summary_text`, Phase 5's `TimelineEvent.description`): a cross-case
    identity/vehicle/pattern-recurrence claim is the single highest-stakes
    claim type in this system — asserting a real-world entity or pattern
    recurs across separately-scoped police cases — so avoiding LLM
    generation for this content avoids hallucination risk entirely rather
    than mitigating it after the fact with a verifier call. `unconfirmed_links`
    entries follow the same rule: deterministic template text, never
    paraphrased (see "UNCONFIRMED LINKS" below).
  - XNETWORK-derived content uses whatever the tool wrapper already
    returns/enables, per this session's explicit instruction: the
    synthesized narrative in `answer_text` is the (verified, or
    cloud-verified, or raw-fallback) paraphrase produced by
    `_generate_xnetwork_text()` above; each per-community `CrossCaseLink.
    description` is the tool's own already-grounded community-summary
    text verbatim (`XNetworkToolResult.chunks[i].text`) — never
    re-generated or re-verified per-item. Community summaries are
    themselves already LLM-written and grounded at summarization time
    (`community_summarization.py`, offline), so re-paraphrasing them again
    per link would add a second, unnecessary hallucination surface without
    the compensating benefit XNETWORK's own answer_text has (it IS run
    through `verify_grounding()` with the cloud-retry safety net).

UNCONFIRMED LINKS. `XGraphToolResult.unconfirmed_links` are not optional
decoration (this session's brief calls this out as the single
highest-consequence correctness requirement in this sub-agent). EVERY entry
becomes ONE `CrossCaseLink` with `is_unconfirmed=True`, AND contributes a
matching entry to `SubAgentResult.caveats` — an unconfirmed identity link is
never presented as confirmed fact. Per `_unconfirmed_same_as_links()`
(src/retrieval/graph_retriever.py), each entry's dict shape is
`{"entity", "candidate", "tier", "confidence", "status"}` — it carries no
per-link `case_ids`, so `CrossCaseLink.case_ids=[]` for these (the data is
genuinely not available at this boundary; using the tool's aggregate
`case_ids_touched` instead would misattribute an unconfirmed pairing to
specific cases it was never shown to actually span).

STATUS MAPPING (per this session's explicit brief, five named outcomes):
  1. Both DENIED -> `SubAgentStatus.DENIED` [RESOLVED-6], never collapsed
     into ABSTAINED/EMPTY. Since both tools share an identical role gate,
     this is asserted to be the ONLY way DENIED can occur — this module
     does NOT build defensive handling for a "one denied, one not" case
     (per this session's explicit instruction); the general fallthrough
     logic below simply treats a lone DENIED tool as non-contributing,
     same as a lone FAILED tool, without a dedicated branch.
  2. Both return a definite "no connections" result — `XGraphToolResult`
     EMPTY with no `unconfirmed_links` AND `XNetworkToolResult` EMPTY ->
     `SubAgentStatus.EMPTY`, presented as a real finding. [PRESERVE —
     XGRAPH's own module docstring / design §2.3] This is explicitly NOT a
     failure to be softened into ABSTAINED.
  3. Both genuinely FAILED -> `SubAgentStatus.ABSTAINED`.
  4. One contributes real data (chunks, unconfirmed links, or a
     community-pattern result), the other doesn't -> `SubAgentStatus.
     PARTIAL`, `tools_used`/`degraded_from` split accordingly — the same
     uniform RESOLVED-4 rule already established project-wide (a tool that
     returned EMPTY or FAILED and didn't contribute belongs in
     `degraded_from`, same as everywhere else).
  5. Both contribute -> `SubAgentStatus.OK`, `tools_used=["XGRAPH",
     "XNETWORK"]`.

ONE GENUINE GAP IN THE BRIEF'S FIVE BUCKETS, RESOLVED HERE AND FLAGGED, NOT
SILENTLY PICKED: neither of the two tools is guaranteed to fail/succeed
together (unlike DENIED, which shares one role gate, XGRAPH hits Postgres/
AGE and XNETWORK hits a separate Chroma collection — independent failure
modes). A genuinely reachable sixth combination exists that the five buckets
above don't name: one tool FAILED and the other a definite EMPTY (no
unconfirmed links) — i.e. NEITHER contributes, but it is not "both FAILED"
(bucket 3) and not "both definite-empty" (bucket 2). Resolved by falling
back to RESOLVED-4's general, project-wide rule (SUBAGENT_INTERFACES.md
§2.0): any attempted tool that failed, returned empty, or fell back belongs
in `degraded_from`, and non-empty `degraded_from` implies `PARTIAL` — so
this combination returns `PARTIAL`, `tools_used=[]` (nothing actually
contributed data), `degraded_from=["XGRAPH", "XNETWORK"]`, `answer_text`
stating no confirmed connections were found, and a caveat naming which side
encountered an error. This keeps the "no connections found" finding from
the cleanly-resolved side visible (matching the spirit of "present whichever
succeeded" from the design doc's own partial-failure column) rather than
discarding it into a blank ABSTAINED just because the OTHER, independent
tool call happened to error.

BOUNDED PAYLOAD. `SubAgentResult.links` (never raw chunks) carries the
per-connection items; `answer_text` is a short synthesized narrative; every
unconfirmed link contributes a `caveats` entry. XGRAPH's deterministic
content is never run through `verify_grounding()` (nothing generated, per
the description-generation decision above -- the same "nothing to cite,
nothing to verify" property Timeline Building's Phase 5 entry already
established for its own deterministic content) and therefore carries no
`Citation` entries either (`Citation.document_index` is defined against
`[Document N]` markers in generated text, which the deterministic XGRAPH
section never uses). XNETWORK's contribution DOES run through
`verify_grounding()` and DOES carry `Citation` entries, one per community
chunk, matching Semantic Search's/Large-Scale Aggregate's existing citation
convention.

VALIDATION GATE — NOT BUILT YET. A `# TODO(validation-gate)` marker is left
at this module's Verifier-boundary insertion point (below), matching every
prior sub-agent's convention. Per AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4
row 5 and §5's table, once `validation.py` exists, Validation's FULL
semantic re-check is MANDATORY here (not the lighter structural-only check
some other sub-agents get) — this is explicitly called out as the
highest-stakes sub-agent for that check, for the same reason the
description-generation decision above avoids LLM text for XGRAPH's confirmed
connections: identity/pattern recurrence across cases is the highest-stakes
claim type in the system.

NOT IN SCOPE THIS PHASE: any other sub-agent, `validation.py` itself, live
wiring into main.py/orchestrator.py/router.py.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Optional

from src.llm.client import call_llm
from src.pipeline.harness.supervisor import CROSS_CASE_LINKAGE, register
from src.pipeline.harness.tools.xgraph import XGraphToolInput, XGraphToolResult, xgraph_tool
from src.pipeline.harness.tools.xnetwork import XNetworkToolInput, XNetworkToolResult, xnetwork_tool
from src.pipeline.harness.types import (
    Citation,
    CrossCaseLink,
    EvidenceChunk,
    OnEventCallback,
    SourceTool,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolStatus,
)
from src.pipeline.verifier import verify_grounding

logger = logging.getLogger(__name__)

# Self-contained, sub-agent-scoped prompt — same convention every prior
# sub-agent module established (no coupling to orchestrator.py's own
# `_CROSS_CASE_NETWORK_PROMPT_TEMPLATE`, which takes parameters
# `SubAgentInput` does not carry, e.g. full conversation history). Mirrors
# the citation instruction every other harness generation prompt enforces
# ([Document N], matching `verify_grounding()`'s positional contract) and
# the "why [Document N] not [Community N]" fix orchestrator.py's own
# comment documents (verifier.py's deterministic citation check hardcodes
# the literal word "Document").
_XNETWORK_SYSTEM_PROMPT_TEMPLATE = (
    "You are a police cross-case pattern-synthesis assistant. The documents "
    "below are precomputed summaries of case clusters that share entities, "
    "themes, or a modus operandi. Using ONLY this material, describe the "
    "overall pattern(s) relevant to the user's question. Do not invent "
    "connections the material does not state.\n\n"
    "Every factual claim MUST cite its source as [Document N], where N is "
    "that document's 1-based position below.\n\n"
    "Respond in {preferred_language}.\n\n"
    "--- DOCUMENTS ---\n{documents}\n--- END OF DOCUMENTS ---"
)


def _generation_role(preferred_language: Optional[str]) -> str:
    """
    Mirrors every prior sub-agent module's own inline `_generation_role()`
    rather than importing one — no cross-sub-agent coupling, per this
    session's scope. Same finding all of them document: the Urdu-fine-tuned
    local generation-slot model ignores an explicit "reply in English"
    instruction, so non-Urdu responses route through the reasoning slot.
    """
    return "generation" if preferred_language == "Urdu" else "reasoning"


def _format_documents_for_prompt(chunks: list[EvidenceChunk]) -> str:
    """[Document N] numbering here MUST match `Citation.document_index` and
    `verify_grounding()`'s positional `chunks[n-1]` indexing below — same
    list, same order, per EvidenceChunk's [PRESERVE — design §5] contract."""
    parts: list[str] = []
    for i, chunk in enumerate(chunks, start=1):
        source = chunk.metadata.source_file or "unknown"
        parts.append(f"[Document {i}] ({source})\n{chunk.text}")
    return "\n\n".join(parts)


def _chunk_to_verifier_dict(chunk: EvidenceChunk) -> dict:
    """Flatten one EvidenceChunk into verify_grounding()'s pre-existing flat
    dict shape — same reshape every prior sub-agent's own helper performs,
    not a new interface (design §5 decision (a))."""
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


def _verifier_passed(verification: dict) -> bool:
    return bool(verification.get("grounded", False)) and not verification.get("off_topic", False)


@dataclass
class _XNetworkOutcome:
    """
    Local, sub-agent-internal result of composing XNETWORK's tool output
    with generation + the verify -> one-shot-cloud-retry -> raw-text-
    fallback sequence this module implements per the "XGRAPH TOOL CONTRACT
    DISCREPANCY" note above. Not a harness type, never crosses this
    module's boundary.
    """

    text: Optional[str]  # None only if the FIRST generation call itself raised.
    used_raw_fallback: bool
    verifier_regenerated: bool = False


async def _generate_xnetwork_text(
    agent_input: SubAgentInput, tool_result: XNetworkToolResult
) -> _XNetworkOutcome:
    """
    Mirrors `orchestrator.py`'s existing XNETWORK route (~line 1387-1528)
    exactly, per the resolution recorded in this module's docstring: verify
    the local paraphrase; on rejection, one attempt at `call_llm(...,
    force_cloud=True)` + re-verify; on that also failing (or raising, e.g.
    `AIR_GAP_MODE` refusing the cloud call) fall back to the tool's own
    `raw_summary_text` — which is already-grounded evidence generated and
    verified offline at community-summarization time
    (`community_summarization.py`), so a paraphrase rejection here means
    THIS generation step's paraphrase failed, not that the evidence is
    thin (same reasoning already applied to XAGG's raw-fallback, Phase 3).

    Only a raised exception from the FIRST (local) `call_llm()` call
    produces `text=None` — every path downstream of a successful first
    call has a guaranteed non-None fallback, matching XAGG's Phase 3
    precedent of an "always something to serve once the tool itself
    succeeded" outcome shape.
    """
    caller = agent_input.execution.caller
    resolved_language = caller.preferred_language or "the same language as the user's question"
    documents_text = _format_documents_for_prompt(tool_result.chunks)
    system_prompt = _XNETWORK_SYSTEM_PROMPT_TEMPLATE.format(
        preferred_language=resolved_language, documents=documents_text
    )
    verify_chunks = [_chunk_to_verifier_dict(c) for c in tool_result.chunks]

    try:
        text = await call_llm(
            system_prompt, agent_input.query_text, role=_generation_role(caller.preferred_language)
        )
    except Exception as exc:
        logger.error("Cross-Case Linkage: XNETWORK generation failed: %s", exc)
        return _XNetworkOutcome(text=None, used_raw_fallback=False)

    verification = await verify_grounding(
        answer=text,
        cited_chunks=verify_chunks,
        case_id="cross_case",
        cross_case_ids=tool_result.case_ids_touched,
    )
    verifier_passed = _verifier_passed(verification)

    if not verifier_passed:
        logger.warning(
            "Cross-Case Linkage: verifier rejected XNETWORK paraphrase (local): %s",
            (verification.get("reason") or "")[:150],
        )
        # [PRESERVE — design §2.5, XNetworkToolResult docstring] One-shot
        # cloud regeneration before falling back to raw evidence — do not
        # retry more than once, do not generalize this to XGRAPH/XAGG.
        try:
            text = await call_llm(
                system_prompt,
                agent_input.query_text,
                role=_generation_role(caller.preferred_language),
                force_cloud=True,
            )
            verification = await verify_grounding(
                answer=text,
                cited_chunks=verify_chunks,
                case_id="cross_case",
                cross_case_ids=tool_result.case_ids_touched,
            )
            verifier_passed = _verifier_passed(verification)
            if verifier_passed:
                logger.info("Cross-Case Linkage: XNETWORK cloud regeneration passed verification.")
            else:
                logger.warning(
                    "Cross-Case Linkage: verifier rejected XNETWORK paraphrase (cloud retry too): %s",
                    (verification.get("reason") or "")[:150],
                )
        except Exception as cloud_exc:
            # E.g. AIR_GAP_MODE refuses the cloud call outright — call_llm's
            # own documented contract is to raise, not silently fall back.
            logger.warning("Cross-Case Linkage: XNETWORK cloud regeneration attempt failed: %s", cloud_exc)
            verifier_passed = False

    if not verifier_passed:
        return _XNetworkOutcome(
            text=tool_result.raw_summary_text, used_raw_fallback=True, verifier_regenerated=True
        )
    return _XNetworkOutcome(text=text, used_raw_fallback=False)


def _xgraph_confirmed_link(
    tool_input: XGraphToolInput, tool_result: XGraphToolResult
) -> Optional[CrossCaseLink]:
    """
    Deterministic, structured description — see this module's docstring
    "XGRAPH-VS-XNETWORK DESCRIPTION-GENERATION DECISION". None when XGRAPH
    has no confirmed chunks to describe (status != OK).
    """
    if tool_result.status != ToolStatus.OK:
        return None
    entity_label = f"'{tool_input.target_entity}'" if tool_input.target_entity else "A recurring entity"
    case_ids = tool_result.case_ids_touched
    description = (
        f"{entity_label} appears across {len(case_ids)} case(s): "
        f"{', '.join(case_ids) if case_ids else 'unknown'} "
        f"(traversal depth {tool_result.hop_count} hop(s))."
    )
    return CrossCaseLink(
        description=description,
        case_ids=case_ids,
        confidence=tool_result.chain_confidence,
        source_tool="XGRAPH",
        is_unconfirmed=False,
    )


def _xgraph_unconfirmed_links(tool_result: XGraphToolResult) -> tuple[list[CrossCaseLink], list[str]]:
    """
    Every `unconfirmed_links` entry becomes one `CrossCaseLink` AND one
    matching `caveats` entry — [PRESERVE, this session's brief's
    single highest-consequence correctness requirement]. Deterministic
    template text only, per the description-generation decision above —
    never paraphrased, never run through the Verifier.
    """
    links: list[CrossCaseLink] = []
    caveats: list[str] = []
    for raw in tool_result.unconfirmed_links:
        entity = raw.get("entity", "unknown entity")
        candidate = raw.get("candidate", "unknown entity")
        tier = raw.get("tier")
        confidence = raw.get("confidence")
        description = (
            f"Unconfirmed possible identity match (tier {tier}): '{entity}' may be the "
            f"same real-world entity as '{candidate}' — pending investigator review."
        )
        links.append(
            CrossCaseLink(
                description=description,
                case_ids=[],
                confidence=confidence,
                source_tool="XGRAPH",
                is_unconfirmed=True,
            )
        )
        caveats.append(
            f"Unconfirmed: '{entity}' may be the same real-world entity as '{candidate}' "
            "(pending investigator review) — not a confirmed identity link."
        )
    return links, caveats


def _xnetwork_links(tool_result: XNetworkToolResult) -> list[CrossCaseLink]:
    """
    One `CrossCaseLink` per retrieved community summary, description
    verbatim from the tool's own (already-grounded) text — see the
    description-generation decision above for why this is never
    re-paraphrased per-item.
    """
    return [
        CrossCaseLink(
            description=chunk.text,
            case_ids=tool_result.case_ids_touched,
            confidence=chunk.score,
            source_tool="XNETWORK",
            is_unconfirmed=False,
        )
        for chunk in tool_result.chunks
    ]


def _xgraph_summary_line(case_ids: list[str], hop_count: int, unconfirmed_count: int) -> str:
    lines: list[str] = []
    if case_ids:
        lines.append(
            f"Entity-graph search found confirmed connections across {len(case_ids)} "
            f"other case(s): {', '.join(case_ids)} (depth {hop_count} hop(s))."
        )
    if unconfirmed_count:
        plural = "es" if unconfirmed_count != 1 else ""
        lines.append(
            f"{unconfirmed_count} additional possible identity match{plural} were found but "
            "are unconfirmed — see caveats."
        )
    if not lines:
        lines.append("No confirmed cross-case entity connections were found.")
    return " ".join(lines)


async def cross_case_linkage(
    agent_input: SubAgentInput, *, on_event: Optional[OnEventCallback] = None
) -> SubAgentResult:
    """
    The Cross-Case Linkage sub-agent. See module docstring for the contract.

    [AMENDMENT — pre-Phase-7 contract amendment, mirrors §10/§11's pattern]
    `on_event` is accepted for `SubAgent` protocol conformance and otherwise
    ignored this session. SUBAGENT_INTERFACES.md §2.1.4's "Generalization"
    note observes this sub-agent (XGRAPH+XNETWORK) is a candidate for the
    same live per-source trace Investigative Analysis (Phase 7) implements —
    tracked as future work, not done here; out of this amendment's scope.
    """
    execution = agent_input.execution

    xgraph_input = XGraphToolInput(
        query_text=agent_input.query_text, execution=execution, target_entity=None
    )
    xnetwork_input = XNetworkToolInput(query_text=agent_input.query_text, execution=execution)

    xgraph_result, xnetwork_result = await asyncio.gather(
        xgraph_tool(xgraph_input), xnetwork_tool(xnetwork_input)
    )

    # ── Both denied: propagated as its own status [RESOLVED-6] ───────────
    # The role gate already ran (and already audit-logged) independently
    # inside each tool — this sub-agent does not re-check or re-derive it
    # (design §3.1 / SUBAGENT_INTERFACES.md §2.1's Cross-Case Linkage row:
    # "do not add a third gate at the sub-agent level"). No dedicated
    # handling exists for a "one denied, one not" mix — per this session's
    # explicit instruction, that combination is asserted unreachable
    # (identical role gates) rather than defensively coded for; see the
    # module docstring's STATUS MAPPING note.
    if xgraph_result.status == ToolStatus.DENIED and xnetwork_result.status == ToolStatus.DENIED:
        return SubAgentResult(
            status=SubAgentStatus.DENIED,
            error=xgraph_result.error or xnetwork_result.error,
            caveats=["Cross-case linkage access was denied for your role."],
        )

    # ── Both genuinely failed: no data, no denial, an actual error ───────
    if xgraph_result.status == ToolStatus.FAILED and xnetwork_result.status == ToolStatus.FAILED:
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=xgraph_result.error or xnetwork_result.error,
            caveats=["Cross-case linkage search failed; no answer could be produced."],
        )

    # ── Both a definite "no connections" result: a real finding, not an
    #    error [PRESERVE — XGRAPH's own module docstring / design §2.3] ──
    xgraph_definite_empty = (
        xgraph_result.status == ToolStatus.EMPTY and not xgraph_result.unconfirmed_links
    )
    xnetwork_definite_empty = xnetwork_result.status == ToolStatus.EMPTY
    if xgraph_definite_empty and xnetwork_definite_empty:
        return SubAgentResult(
            status=SubAgentStatus.EMPTY,
            answer_text=(
                "No cross-case connections or patterns were found for this query, "
                "across either entity-graph search or cross-case pattern synthesis."
            ),
        )

    xgraph_contributes = xgraph_result.status == ToolStatus.OK or bool(xgraph_result.unconfirmed_links)
    xnetwork_contributes = xnetwork_result.status == ToolStatus.OK

    # ── Neither contributes, but not "both FAILED" and not "both definite
    #    empty" — a genuinely reachable sixth combination the brief's five
    #    named buckets don't cover (see module docstring's dedicated note):
    #    one FAILED, the other a definite empty. Falls back to RESOLVED-4's
    #    general project-wide degraded_from/PARTIAL rule rather than
    #    discarding the cleanly-resolved side's "nothing found" finding. ──
    if not xgraph_contributes and not xnetwork_contributes:
        degraded_from: list[SourceTool] = ["XGRAPH", "XNETWORK"]
        caveats = []
        if xgraph_result.status == ToolStatus.FAILED:
            caveats.append("Entity-graph cross-case search encountered an error.")
        if xnetwork_result.status == ToolStatus.FAILED:
            caveats.append("Cross-case pattern synthesis encountered an error.")
        return SubAgentResult(
            status=SubAgentStatus.PARTIAL,
            answer_text="No confirmed cross-case connections were found.",
            tools_used=[],
            degraded_from=degraded_from,
            caveats=caveats,
        )

    links: list[CrossCaseLink] = []
    caveats: list[str] = []
    citations: list[Citation] = []
    answer_sections: list[str] = []
    tools_used: list[SourceTool] = []
    degraded_from = []

    # ── XGRAPH contribution: deterministic, never LLM-generated, never
    #    run through the Verifier — see the description-generation
    #    decision in this module's docstring. ──────────────────────────
    if xgraph_contributes:
        confirmed_link = _xgraph_confirmed_link(xgraph_input, xgraph_result)
        if confirmed_link is not None:
            links.append(confirmed_link)
        unconfirmed_links, unconfirmed_caveats = _xgraph_unconfirmed_links(xgraph_result)
        links.extend(unconfirmed_links)
        caveats.extend(unconfirmed_caveats)
        answer_sections.append(
            _xgraph_summary_line(
                xgraph_result.case_ids_touched, xgraph_result.hop_count, len(unconfirmed_links)
            )
        )
        tools_used.append("XGRAPH")
    else:
        degraded_from.append("XGRAPH")
        if xgraph_result.status == ToolStatus.FAILED:
            caveats.append("Entity-graph cross-case search encountered an error.")

    # ── XNETWORK contribution: LLM-generated + verified (+ cloud-retry +
    #    raw-fallback), per this module's docstring resolution. ─────────
    if xnetwork_contributes:
        xnetwork_outcome = await _generate_xnetwork_text(agent_input, xnetwork_result)
        if xnetwork_outcome.text is None:
            # Only the FIRST generation call raising produces this — the
            # tool itself succeeded with real data, but this sub-agent's
            # own generation step could not run at all (e.g. the LLM
            # service is unreachable). Treated as non-contributing for
            # bookkeeping purposes, same as a tool-level failure, matching
            # semantic_search.py's/large_scale_aggregate.py's own
            # generation-exception handling.
            degraded_from.append("XNETWORK")
            caveats.append("Cross-case pattern synthesis text generation failed.")
        else:
            links.extend(_xnetwork_links(xnetwork_result))
            citations.extend(_citations_for(xnetwork_result.chunks))
            answer_sections.append(xnetwork_outcome.text)
            tools_used.append("XNETWORK")
            if xnetwork_outcome.used_raw_fallback:
                caveats.append(
                    "The cross-case pattern summary could not be verified as an accurate "
                    "paraphrase; showing the raw community-cluster summaries instead."
                )
    else:
        degraded_from.append("XNETWORK")
        if xnetwork_result.status == ToolStatus.FAILED:
            caveats.append("Cross-case pattern synthesis encountered an error.")

    # TODO(validation-gate): Validation plugs in here -- after this
    # point, before the result is returned -- per
    # AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 5 / §5's ordering
    # (Verifier -> Citation-Consistency [Report Drafting only] ->
    # Validation). Per that same plan row, Validation's FULL semantic
    # re-check is MANDATORY here once it exists (not the lighter
    # structural-only check some other sub-agents get) -- Cross-Case
    # Linkage is explicitly called out as the highest-stakes sub-agent for
    # that check, the same reasoning behind this module's
    # deterministic-XGRAPH-description decision above. `validation.py`
    # does not exist yet and `SubAgentResult` has no `validation_status`
    # field. No ad hoc validation logic is invented here to fill that gap.

    status = SubAgentStatus.OK if not degraded_from else SubAgentStatus.PARTIAL
    return SubAgentResult(
        status=status,
        answer_text=" ".join(answer_sections) if answer_sections else None,
        citations=citations,
        tools_used=tools_used,
        degraded_from=degraded_from,
        links=links,
        caveats=caveats,
    )


cross_case_linkage.name = CROSS_CASE_LINKAGE

# Import-time self-registration -- the same pattern every prior sub-agent
# module established (supervisor.py's own module docstring documents it for
# every future sub-agent module). Importing this module is what makes
# Cross-Case Linkage live in the module-level registry; nothing else
# imports it yet, so this has no effect on live traffic today (per §6,
# still not wired into main.py/orchestrator.py/router.py).
register(cross_case_linkage)
