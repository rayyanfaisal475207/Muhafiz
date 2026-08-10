"""
Cross-Case Linkage sub-agent.

Answers "what connects across cases" by dispatching to XGRAPH (entity-typed
traversal/recurrence) or XNETWORK (open-ended thematic synthesis), per
docs/SUBAGENT_INTERFACES.md §2.1 and §3.1's confirmed split.

DISPATCH REUSES router.py's PATTERNS, DELIBERATELY
───────────────────────────────────────────────────
`_XGRAPH_OVERRIDE_PATTERNS` and `_XNETWORK_OVERRIDE_PATTERNS` are already tuned
against live misclassification failures — including the specific one where
"network ... across cases" phrasing was excluded from XNETWORK's triggers to
stop it swallowing XGRAPH's own "map ORG-002's network across all cases"
example. Re-deriving that distinction here would reproduce the bugs it was
written to fix, so this imports them rather than inventing new logic.

Both tools may run when neither pattern set matches decisively: they are
independent sources, and presenting both is better than guessing.

NO THIRD ROLE GATE, BY INSTRUCTION
───────────────────────────────────
The gate is enforced twice already, once inside each tool, each writing its own
`authorization_violation` audit record before arming cross-case scope (§2.3,
§2.5). §3's contract says explicitly not to add a third at this level: it would
be redundant and could drift out of sync with the tools' own checks. This file
therefore reads DENIED as a result, never re-checks the role itself.

NO THIRD FALLBACK PATH
──────────────────────
Both tools pin `fallback_to_rag` to `Literal[False]`. Cross-case evidence must
never blend into a case-scoped stream, so this sub-agent adds no substitute
fallback either — a tool returning nothing means nothing, not "go look
elsewhere".

TRACE MECHANISM (§2.1.4.1): TOOL-EMITTED EVENTS SUFFICE — CONFIRMED, NOT ASSUMED
─────────────────────────────────────────────────────────────────────────────────
§2.1.4.1 predicted this would be the Case Summarization case and asked for
empirical confirmation rather than inheritance. Checked against the built
tools, and the prediction holds for FOUR independent reasons, not just the one
predicted:

  1. Neither can declare a fallback — `fallback_to_rag` is `Literal[False]` on
     both, enforced by the type system.
  2. Neither names a degradation TARGET at all. GRAPH and SQL collapse
     specifically because both name RAG; these two name nothing.
  3. They share no backing store — XGRAPH traverses the AGE graph, XNETWORK
     searches a precomputed community-summary Chroma collection.
  4. Their result shapes are disjoint (`unconfirmed_links`/`hop_count` vs
     `community_ids`/`raw_summary_text`), so neither can stand in for the other
     even in principle.

So "which source did this finding come from" is never ambiguous the way it is
in Investigative Analysis, and `tool:xgraph`/`tool:xnetwork` map one-to-one
onto the sources. Sub-agent-interpreted `linkage:*` events would double-report
the same transition, which §2.2 forbids as much as reporting none.

[PRESERVE — design §3] Only `CrossCaseLink`/`Citation` objects cross the
handoff boundary; raw chunks never do.

NOT wired into supervisor routing yet.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from src.pipeline.harness.contracts import (
    CallerContext,
    Citation,
    CrossCaseLink,
    EvidenceChunk,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
    ToolStatus,
    XGraphToolInput,
    XNetworkToolInput,
)
from src.pipeline.harness.generation import (
    CROSS_CASE_NETWORK_PROMPT_TEMPLATE,
    CROSS_CASE_PROMPT_TEMPLATE,
    generate_cross_case_answer,
)
from src.pipeline.harness.events import EventRecorder
from src.pipeline.harness.tools import registry
from src.pipeline.harness.verifier_gate import UNGROUNDED_TRIGGER, verify_grounding

logger = logging.getLogger(__name__)

NAME = "cross_case_linkage"

_MAX_LINKS = 10

# [PRESERVE — existing convention] Cross-case routes verify against the
# sentinel "cross_case" rather than a real case scope, with the actual case
# IDs passed as `cross_case_ids` so the leakage check can allow them.
_CROSS_CASE_SENTINEL = "cross_case"

_ABSTENTION_CAVEAT = (
    "I could not find cross-case connections that support an answer to that question."
)

_UNCONFIRMED_CAVEAT = (
    "Some connections below rest on unconfirmed identity links — entities that "
    "appear to match but have not been verified as the same real-world person or "
    "object. Treat them as leads, not established fact."
)


def _dispatch(query_text: str) -> tuple[bool, bool]:
    """
    Decide which tool(s) to run: `(use_xgraph, use_xnetwork)`.

    Reuses router.py's pattern sets rather than re-deriving the distinction —
    see the module docstring. XNETWORK is checked first, matching the router's
    own precedence: its trigger vocabulary is deliberately narrow
    open-ended-synthesis phrasing that does not overlap XGRAPH's, so it needs
    no tie-break.

    When neither matches, BOTH run. These are independent sources with no
    shared backing store, so presenting whatever each finds beats guessing —
    and §2's partial-failure rule already covers one returning empty.
    """
    from src.pipeline.router import (
        _XGRAPH_OVERRIDE_PATTERNS,
        _XNETWORK_OVERRIDE_PATTERNS,
    )

    if any(p.search(query_text) for p in _XNETWORK_OVERRIDE_PATTERNS):
        return False, True
    if any(p.search(query_text) for p in _XGRAPH_OVERRIDE_PATTERNS):
        return True, False
    return True, True


def _links_from_xgraph(result, limit: int) -> list[CrossCaseLink]:
    """
    Entity-typed connections, plus one link per unconfirmed identity match.

    [PRESERVE — design §3] Unconfirmed `SAME_AS` links are surfaced as CAVEATS,
    never as fact. They are emitted as their own `CrossCaseLink` entries with
    `is_unconfirmed=True` rather than being folded into confirmed findings,
    so a consumer cannot render them indistinguishably.
    """
    links: list[CrossCaseLink] = []
    case_ids = list(result.case_ids_touched or [])

    for chunk in (result.chunks or [])[:limit]:
        links.append(
            CrossCaseLink(
                description=(chunk.text or "").strip()[:400],
                case_ids=[chunk.metadata.case_id] if chunk.metadata.case_id else case_ids,
                confidence=chunk.metadata.confidence,
                source_tool="XGRAPH",
                is_unconfirmed=False,
            )
        )

    for raw in (result.unconfirmed_links or [])[:limit]:
        basis = raw.get("basis") or "identity match not verified"
        links.append(
            CrossCaseLink(
                description=(
                    f"Possible identity link between {raw.get('from', 'an entity')} and "
                    f"{raw.get('to', 'another entity')} — {basis}."
                ),
                case_ids=case_ids,
                confidence=raw.get("confidence"),
                source_tool="XGRAPH",
                is_unconfirmed=True,
            )
        )
    return links


def _links_from_xnetwork(result, limit: int) -> list[CrossCaseLink]:
    """Thematic/MO patterns. No identity claims, so nothing to hedge as unconfirmed."""
    links: list[CrossCaseLink] = []
    case_ids = list(result.case_ids_touched or [])
    for chunk in (result.chunks or [])[:limit]:
        meta = chunk.metadata.model_dump()
        links.append(
            CrossCaseLink(
                description=(chunk.text or "").strip()[:400],
                case_ids=list(meta.get("community_case_ids") or case_ids),
                confidence=chunk.metadata.confidence,
                source_tool="XNETWORK",
                is_unconfirmed=False,
            )
        )
    return links


def _rank(links: list[CrossCaseLink]) -> list[CrossCaseLink]:
    """
    Highest confidence first; unconfirmed links always last regardless of it.

    An unconfirmed identity match with a high similarity score is still a lead,
    not a finding — ranking it above confirmed connections would invert exactly
    the distinction `is_unconfirmed` exists to preserve.
    """
    return sorted(
        links,
        key=lambda link: (link.is_unconfirmed, -(link.confidence or 0.0)),
    )


async def _synthesize(
    query_text: str,
    chunks: list[EvidenceChunk],
    caller: CallerContext,
    used_xnetwork: bool,
    unconfirmed_links: Optional[str] = None,
    conversation_context: Optional[str] = None,
) -> str:
    """
    Generate the linkage answer, using the legacy CROSS-CASE templates —
    NOT the case-scoped one.

    Template selection mirrors legacy exactly, and the split is deliberate
    (orchestrator.py:82-86): XNETWORK's evidence is already-LLM-summarized
    community reports, while XGRAPH's is per-case graph traversal with a
    per-case `[Document N, CASE-ID]` citation rule. Reusing one template for
    both produced citations the Verifier then correctly rejected.

    `unconfirmed_links` is threaded into `cross_case_response.txt`'s hedging
    section: the Verifier's deterministic hedging check rejects an unhedged
    claim built on a low-confidence entity link, so the generator must be TOLD
    which links are unconfirmed or it cannot comply.

    `[Document N]` markers match `chunks` positionally ([PRESERVE — design §5]);
    this function must never reorder them.
    """
    if UNGROUNDED_TRIGGER in query_text:
        markers = " ".join(f"[Document {i}]" for i in range(1, len(chunks) + 1))
        return (
            f"{UNGROUNDED_TRIGGER} Connections identified across the accessible "
            f"cases. {markers}"
        )

    template = (
        CROSS_CASE_NETWORK_PROMPT_TEMPLATE
        if used_xnetwork
        else CROSS_CASE_PROMPT_TEMPLATE
    )
    return await generate_cross_case_answer(
        query_text=query_text,
        chunks=chunks,
        template=template,
        preferred_language=caller.preferred_language,
        conversation_context=conversation_context,
        unconfirmed_links=unconfirmed_links,
    )


def _to_citations(chunks: list[EvidenceChunk]) -> list[Citation]:
    """[PRESERVE — design §3] Provenance only; no chunk text crosses upward."""
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
    agent_input: SubAgentInput,
    events: Optional[EventRecorder] = None,
    gateway: Any = None,
) -> SubAgentResult:
    """Execute Cross-Case Linkage. Returns a bounded `SubAgentResult`."""
    if events:
        await events.emit(
            f"subagent:{NAME}", "active", "Cross-Case Linkage: searching across cases"
        )

    caller = agent_input.caller
    use_xgraph, use_xnetwork = _dispatch(agent_input.query_text)

    xgraph_result = None
    xnetwork_result = None

    if use_xgraph:
        xgraph_result = await registry.xgraph_tool(
            XGraphToolInput(
                query_text=agent_input.query_text, caller=caller,
                target_entity=None, max_hops=2,
            ),
            gateway=gateway,
            events=events,
        )

    if use_xnetwork:
        xnetwork_result = await registry.xnetwork_tool(
            XNetworkToolInput(query_text=agent_input.query_text, caller=caller, top_k=5),
            gateway=gateway,
            events=events,
        )

    attempted = [r for r in (xgraph_result, xnetwork_result) if r is not None]

    # ── [RESOLVED-6] DENIED propagates as its own status ──
    # Both tools check the same CROSS_CASE_ROLES set, so they always deny
    # together — but this reads the results rather than asserting that, so a
    # future divergence surfaces as a real outcome instead of a wrong one.
    # Never collapsed into ABSTAINED or EMPTY: "blocked by permissions" and
    # "searched and found nothing" are different facts, and flattening them
    # destroys the signal that separates a security event from a coverage gap.
    if attempted and all(r.status is ToolStatus.DENIED for r in attempted):
        if events:
            await events.emit(
                f"subagent:{NAME}", "error",
                "Cross-Case Linkage: cross-case access denied",
            )
        return SubAgentResult(
            status=SubAgentStatus.DENIED,
            answer_text=None,
            tools_used=[],
            degraded_from=[t for t, r in
                           (("XGRAPH", xgraph_result), ("XNETWORK", xnetwork_result))
                           if r is not None],
            error=next((r.error for r in attempted if r.error), None),
        )

    # ── Collect from whichever succeeded ──
    # §2: if one returns empty/abstains, present the other rather than failing
    # the whole sub-agent.
    chunks: list[EvidenceChunk] = []
    links: list[CrossCaseLink] = []
    tools_used: list[str] = []
    degraded_from: list[str] = []
    case_ids: set[str] = set()

    if xgraph_result is not None:
        if xgraph_result.status is ToolStatus.OK and xgraph_result.chunks:
            chunks.extend(xgraph_result.chunks)
            links.extend(_links_from_xgraph(xgraph_result, _MAX_LINKS))
            tools_used.append("XGRAPH")
            case_ids.update(xgraph_result.case_ids_touched or [])
        else:
            degraded_from.append("XGRAPH")
            # An EMPTY XGRAPH can still carry unconfirmed links worth
            # reporting — "no confirmed connections, but these possible
            # identity matches" is a real finding, not nothing.
            if xgraph_result.unconfirmed_links:
                links.extend(_links_from_xgraph(xgraph_result, _MAX_LINKS))
                case_ids.update(xgraph_result.case_ids_touched or [])

    if xnetwork_result is not None:
        if xnetwork_result.status is ToolStatus.OK and xnetwork_result.chunks:
            chunks.extend(xnetwork_result.chunks)
            links.extend(_links_from_xnetwork(xnetwork_result, _MAX_LINKS))
            tools_used.append("XNETWORK")
            case_ids.update(xnetwork_result.case_ids_touched or [])
        else:
            degraded_from.append("XNETWORK")

    # ── Nothing found at all → EMPTY, a legitimate outcome ──
    # "No connections exist across these cases" is an ANSWER — and for a
    # cross-case query, often the one an investigator most needs. Not an error,
    # and not an abstention.
    if not chunks and not links:
        if events:
            await events.emit(
                f"subagent:{NAME}", "done", "Cross-Case Linkage: no connections found"
            )
        return SubAgentResult(
            status=SubAgentStatus.EMPTY,
            answer_text="No connections were found across the accessible cases.",
            citations=[],
            tools_used=[],
            degraded_from=degraded_from,
            cross_case_links=[],
        )

    links = _rank(links)[:_MAX_LINKS]
    chunks = chunks[:_MAX_LINKS]

    # ── Unconfirmed links only, with no cited evidence behind them ──
    # An empty chunk list fails the grounding gate closed, by design — and
    # correctly, since there is nothing for a generated claim to cite. But
    # "no confirmed connections; these possible identity matches exist" is a
    # real finding, and running it through generation would discard it.
    #
    # So it is reported WITHOUT a generated answer: the structured links carry
    # the finding, the caveat carries the hedge, and no ungrounded prose is
    # produced to need verifying. EMPTY rather than PARTIAL — no source
    # actually contributed confirmed evidence.
    if not chunks:
        if events:
            await events.emit(
                f"subagent:{NAME}", "done",
                f"Cross-Case Linkage: no confirmed connections, "
                f"{len(links)} unconfirmed identity link(s)",
            )
        return SubAgentResult(
            status=SubAgentStatus.EMPTY,
            answer_text=(
                "No confirmed connections were found across the accessible cases. "
                "Possible identity matches are listed below and are unverified."
            ),
            citations=[],
            tools_used=[],
            degraded_from=degraded_from,
            caveats=[_UNCONFIRMED_CAVEAT],
            cross_case_links=links,
        )

    # ── Generate, then gate ──
    # Surface the unconfirmed links to the generator so it can hedge them. The
    # Verifier's deterministic hedging check rejects an unhedged claim resting
    # on a low-confidence identity match, so withholding this would make the
    # model fail a check it had no way to satisfy.
    unconfirmed_text = "\n".join(
        f"- {link.description}" for link in links if link.is_unconfirmed
    ) or None

    try:
        answer = await _synthesize(
            agent_input.query_text,
            chunks,
            agent_input.caller,
            used_xnetwork="XNETWORK" in tools_used,
            unconfirmed_links=unconfirmed_text,
            conversation_context=agent_input.conversation_context,
        )
    except Exception as exc:
        # Generation infrastructure failed. The structured links survived, but
        # there is no narrative to verify — abstain rather than serve
        # unverified cross-case prose, which is the highest-risk output this
        # system produces.
        logger.error("Cross-Case Linkage generation failed: %s", exc)
        if events:
            await events.emit(
                f"subagent:{NAME}", "error",
                "Cross-Case Linkage: answer generation failed",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            citations=[],
            cross_case_links=[],
            tools_used=[],
            degraded_from=sorted(set(degraded_from) | set(tools_used)),
            error=ToolError(
                kind="upstream_failure",
                message=f"Cross-case answer generation failed: {exc}",
            ),
        )

    verdict = await verify_grounding(
        answer=answer,
        cited_chunks=[c.model_dump() for c in chunks],
        case_id=_CROSS_CASE_SENTINEL,
        cross_case_ids=sorted(case_ids) or None,
    )

    if not verdict["grounded"]:
        if events:
            await events.emit(
                f"subagent:{NAME}", "error",
                f"Cross-Case Linkage: grounding check failed — {verdict['reason']}",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            citations=[],
            tools_used=[],
            degraded_from=sorted(set(degraded_from) | set(tools_used)),
            caveats=[_ABSTENTION_CAVEAT],
        )

    # ── Caveats ──
    caveats: list[str] = []
    if any(link.is_unconfirmed for link in links):
        # [PRESERVE — design §3] An unconfirmed link MUST contribute a matching
        # caveat, so the hedge travels with the payload rather than depending
        # on a consumer noticing the per-item flag.
        caveats.append(_UNCONFIRMED_CAVEAT)

    for result in attempted:
        for caveat in getattr(result, "degradation_caveats", []) or []:
            if caveat not in caveats:
                caveats.append(caveat)

    citations = _to_citations(chunks)

    if events:
        await events.emit(
            f"subagent:{NAME}", "done",
            f"Cross-Case Linkage: {len(links)} connection(s) across {len(case_ids)} case(s)",
            sources=[
                {"source_tool": c.source_tool, "label": c.display_label()}
                for c in citations
            ],
        )

    return SubAgentResult(
        status=SubAgentStatus.PARTIAL if degraded_from else SubAgentStatus.OK,
        answer_text=answer,
        citations=citations,
        tools_used=tools_used,
        degraded_from=degraded_from,
        caveats=caveats,
        cross_case_links=links,
    )
