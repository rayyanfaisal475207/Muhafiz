"""
Large-Scale Aggregate sub-agent.

Answers cross-case counting questions — "which stations have the most open
theft cases", "top recurring vehicles across all cases" — by composing the XAGG
tool, per docs/SUBAGENT_INTERFACES.md §2.1 and §2.4.

GROUPING SHAPES: WHAT XAGG ACTUALLY SUPPORTS
─────────────────────────────────────────────
XAGG dispatches to TWO canned families by keyword, and this sub-agent reflects
them rather than inventing a third ([PRESERVE — design §2.4] — the bounded
surface is the safety property, and "make it smarter" is explicitly out of
scope):

  * relational_aggregate — group-by `police_station` (area/station-wise) or
    `crime_category` (category-wise), with optional status/category filters
    applied first.
  * graph_recurrence — recurrence counts for Vehicle or Person nodes.
  * case_listing — a plain enumeration, distinct from the grouped counts.

TIME-WISE GROUPING DOES NOT EXIST. `xagg.py` has no date/month/year grouping at
all: `_station_or_category_counts` picks `police_station` when station keywords
are present and `crime_category` otherwise, so a "cases per month" query today
silently returns CATEGORY counts — an answer to a question nobody asked.
Building it here would mean generating a grouping XAGG cannot compute, which is
the free-form-query-generation §2.4 forbids. So this sub-agent DETECTS the
request and reports it as unsupported, rather than passing off category counts
as a time series. `_TIME_GROUPING_UNSUPPORTED` is the caveat; adding real
time-wise grouping is a change to `xagg.py`, not to this file.

[PRESERVE — design §2.4] THE VERIFIER-REJECTION PATH, AND WHY IT IS NOT AN
ABSTENTION
───────────────────────────────────────────────────────────────────────────
XAGG's evidence is MACHINE-COMPUTED AND CORRECT BY CONSTRUCTION: a count of
rows is not a claim that can be ungrounded. So when the Verifier rejects, what
failed is the LLM's PARAPHRASE of that count, not the count itself. Serving a
generic abstention would discard a correct, deterministic finding because the
prose wrapped around it was bad.

This is the SAME SHAPE OF PROBLEM as the flaw found in Cross-Case Linkage
(789867b): there, unconfirmed identity links had no chunks behind them, so the
grounding gate — correctly — failed closed and threw the finding away. In both
cases the gate is right and the mistake is routing a non-prose finding through
a prose-only check. The fix is the same in spirit: keep the finding, drop the
prose. Here that means falling back to `raw_summary_text`, the deterministic
rendering the tool already produced, and reporting PARTIAL rather than
ABSTAINED.

Do NOT unify this with the generic abstention path, and do not add a second
fallback layer on top of it (§2 is explicit: pass XAGG's own behaviour through
unchanged).

TRACE MECHANISM (§2.1.4.1): TOOL-EMITTED EVENTS SUFFICE
────────────────────────────────────────────────────────
Single-tool composition, so the decision test ("can any two legs end up as the
same effective source?") is trivially No — there is only one leg. `tool:xagg`
maps one-to-one onto the only source, and sub-agent-interpreted `aggregate:*`
events would duplicate the same transition. Recorded here for consistency with
how every other sub-agent's choice is documented.

NO THIRD ROLE GATE. Checked inside `run_aggregate()` — role first, audit on
denial, cross-case scope armed only after it passes (§2.4). This file reads
DENIED as a result and never re-checks.

[PRESERVE — design §3] Bounded handoff: top-N groups plus the stated total, never
the full case row set behind them.

NOT wired into supervisor routing yet.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from src.pipeline.harness.contracts import (
    CallerContext,
    Citation,
    EvidenceChunk,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
    ToolStatus,
    XAggToolInput,
)
from src.pipeline.harness.generation import (
    CROSS_CASE_AGG_PROMPT_TEMPLATE,
    generate_cross_case_answer,
)
from src.pipeline.harness.events import EventRecorder
from src.pipeline.harness.tools import registry
from src.pipeline.harness.verifier_gate import UNGROUNDED_TRIGGER, verify_grounding

logger = logging.getLogger(__name__)

NAME = "aggregate_analysis"

# Bounding happens here, not at the supervisor. XAGG already caps its own
# group-by at 15 (`most_common(15)`); this is the handoff cap, and the total is
# always reported alongside so a truncated list never reads as the whole set.
_MAX_GROUPS = 10

_CROSS_CASE_SENTINEL = "cross_case"

_ABSTENTION_CAVEAT = (
    "I could not compute an aggregate across cases for that question."
)

_TIME_GROUPING_UNSUPPORTED = (
    "Grouping by time period is not supported. The figures below are grouped by "
    "the available dimension instead — treat them as counts by that dimension, "
    "not as a trend over time."
)

# Deliberately narrow: these are the phrasings that unambiguously ask for a
# time series. A query merely MENTIONING a date is not asking to group by one.
_TIME_GROUPING_TERMS = (
    "per month", "per year", "per week", "per day",
    "by month", "by year", "by week", "by day",
    "monthly", "yearly", "annually", "weekly", "over time",
    "month-wise", "year-wise", "time-wise",
    "ماہانہ", "سالانہ", "ہر ماہ", "ہر سال",
)


def _wants_time_grouping(query_text: str) -> bool:
    """Does the query ask for a grouping XAGG structurally cannot produce?"""
    lowered = (query_text or "").lower()
    return any(term in lowered for term in _TIME_GROUPING_TERMS)


def _describe_grouping(result) -> str:
    """
    Human-readable name for the grouping XAGG actually applied.

    Reported so the payload states which dimension the numbers are grouped by,
    rather than leaving the supervisor to infer it from the figures.
    """
    kind = result.aggregate_kind
    if kind == "graph_recurrence":
        return "entity recurrence across cases"
    if kind == "case_listing":
        return "case listing"
    return "case counts by station or crime category"


def _bounded_summary(result, total_hint: Optional[int]) -> tuple[str, int]:
    """
    Top-N rendering plus the stated total.

    [PRESERVE — design §3] Never the unbounded table. The total is always
    stated, so a truncated list cannot be mistaken for the complete set — the
    specific way a bounded aggregate misleads if you only send the top rows.
    """
    raw = result.raw_summary_text or ""
    lines = [line for line in raw.splitlines() if line.strip()]
    shown = lines[:_MAX_GROUPS]
    total = total_hint if total_hint is not None else len(lines)
    return "\n".join(shown), total


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


async def _synthesize(
    query_text: str,
    chunks: list[EvidenceChunk],
    caller: CallerContext,
    conversation_context: Optional[str] = None,
) -> str:
    """
    Generate the aggregate summary, using legacy's dedicated XAGG template.

    `cross_case_aggregate.txt` exists rather than reusing the XGRAPH template
    for a concrete reason (orchestrator.py:70-77): XAGG's result is a single
    synthetic summary chunk, not a multi-document traversal, and the XGRAPH
    prompt's per-case `[Document N, CASE-ID]` citation rule made the model
    invent per-case citations the Verifier then correctly flagged as
    ungrounded.

    `[Document N]` markers match `chunks` positionally ([PRESERVE — design §5]);
    this function must never reorder them.
    """
    if UNGROUNDED_TRIGGER in query_text:
        markers = " ".join(f"[Document {i}]" for i in range(1, len(chunks) + 1))
        return (
            f"{UNGROUNDED_TRIGGER} Aggregate computed across the accessible "
            f"cases. {markers}"
        )

    return await generate_cross_case_answer(
        query_text=query_text,
        chunks=chunks,
        template=CROSS_CASE_AGG_PROMPT_TEMPLATE,
        preferred_language=caller.preferred_language,
        conversation_context=conversation_context,
    )


async def run(
    agent_input: SubAgentInput,
    events: Optional[EventRecorder] = None,
    gateway: Any = None,
) -> SubAgentResult:
    """Execute Large-Scale Aggregate. Returns a bounded `SubAgentResult`."""
    if events:
        await events.emit(
            f"subagent:{NAME}", "active", "Large-Scale Aggregate: computing across cases"
        )

    caller = agent_input.caller

    # `target_entity` selects the aggregate KIND inside `run_aggregate`: with an
    # entity it computes graph recurrence ("which cases does X appear in"),
    # without one it computes case counts. Legacy forwards the router's value
    # (orchestrator.py:1288); hardcoding None here silently made every
    # recurrence question answerable only as a case count.
    result = await registry.xagg_tool(
        XAggToolInput(
            query_text=agent_input.query_text, caller=caller,
            target_entity=agent_input.target_entity,
        ),
        gateway=gateway,
        events=events,
    )

    # ── [RESOLVED-6] DENIED propagates as its own status ──
    # Same handling as Cross-Case Linkage: never collapsed into ABSTAINED or
    # EMPTY. "Blocked by permissions" and "found nothing" are different facts,
    # and flattening them blinds monitoring to a security signal. The gate
    # itself lives in the tool; this only reads the outcome.
    if result.status is ToolStatus.DENIED:
        if events:
            await events.emit(
                f"subagent:{NAME}", "error", "Large-Scale Aggregate: cross-case access denied"
            )
        return SubAgentResult(
            status=SubAgentStatus.DENIED,
            answer_text=None,
            tools_used=[],
            degraded_from=["XAGG"],
            error=result.error,
        )

    if result.status is ToolStatus.FAILED:
        if events:
            await events.emit(
                f"subagent:{NAME}", "error", "Large-Scale Aggregate: computation failed"
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            tools_used=[],
            degraded_from=["XAGG"],
            caveats=[_ABSTENTION_CAVEAT],
            error=result.error,
        )

    # ── Nothing to count → EMPTY, a legitimate answer ──
    if not result.raw_summary_text and not result.chunks:
        if events:
            await events.emit(
                f"subagent:{NAME}", "done", "Large-Scale Aggregate: no matching cases"
            )
        return SubAgentResult(
            status=SubAgentStatus.EMPTY,
            answer_text="No cases matched that aggregate across the accessible cases.",
            citations=[],
            tools_used=["XAGG"],
            degraded_from=[],
        )

    caveats: list[str] = []
    degraded_from: list[str] = []

    # A time-series request that XAGG cannot compute. Reported explicitly
    # rather than letting category counts pass as a trend — see the module
    # docstring. The figures are still served: they are correct, just not the
    # grouping asked for.
    if _wants_time_grouping(agent_input.query_text):
        caveats.append(_TIME_GROUPING_UNSUPPORTED)
        degraded_from.append("XAGG")

    chunks = list(result.chunks or [])
    summary_text, total_groups = _bounded_summary(
        result, getattr(result, "total_considered", None)
    )
    grouping = _describe_grouping(result)

    # ── Generate, then gate ──
    try:
        answer = await _synthesize(
            agent_input.query_text,
            chunks,
            agent_input.caller,
            agent_input.conversation_context,
        )
    except Exception as exc:
        # [PRESERVE — design §2.4] NOT an abstention, for the same reason a
        # failed verdict below is not: the aggregate is machine-computed and
        # correct by construction, so an unreachable generator costs the
        # PARAPHRASE, not the evidence. Serve the deterministic rendering the
        # tool already produced. Every other sub-agent abstains here because
        # its answer only exists as generated prose; XAGG's does not.
        logger.error("Large-Scale Aggregate generation failed: %s", exc)
        if events:
            await events.emit(
                f"subagent:{NAME}", "retry",
                "Large-Scale Aggregate: summary generation failed — "
                "serving the computed figures directly",
            )
        return SubAgentResult(
            status=SubAgentStatus.PARTIAL,
            answer_text=summary_text,
            citations=_to_citations(chunks),
            tools_used=["XAGG"],
            degraded_from=sorted(set(degraded_from) | {"XAGG"}),
            caveats=caveats + [
                "The generated summary was unavailable, so the computed "
                "figures are shown directly."
            ],
            error=ToolError(
                kind="upstream_failure",
                message=f"Aggregate summary generation failed: {exc}",
            ),
        )

    verdict = await verify_grounding(
        answer=answer,
        cited_chunks=[c.model_dump() for c in chunks],
        case_id=_CROSS_CASE_SENTINEL,
        cross_case_ids=list(result.case_ids_touched or []) or None,
    )

    citations = _to_citations(chunks)

    if not verdict["grounded"]:
        # [PRESERVE — design §2.4] NOT an abstention. The aggregate is
        # machine-computed and correct by construction, so a rejection means
        # the PARAPHRASE failed, not the evidence. Serve the deterministic
        # rendering the tool already produced.
        #
        # Same shape as the Cross-Case Linkage flaw (789867b): the gate is
        # right, and routing a non-prose finding through a prose-only check is
        # the mistake. Keep the finding, drop the prose.
        if events:
            await events.emit(
                f"subagent:{NAME}", "retry",
                "Large-Scale Aggregate: summary failed verification — "
                "serving the computed figures directly",
            )
        return SubAgentResult(
            status=SubAgentStatus.PARTIAL,
            answer_text=summary_text,
            citations=citations,
            tools_used=["XAGG"],
            degraded_from=sorted(set(degraded_from) | {"XAGG"}),
            caveats=caveats + [
                "The generated summary could not be verified, so the computed "
                "figures are shown directly."
            ],
        )

    for caveat in getattr(result, "degradation_caveats", []) or []:
        if caveat not in caveats:
            caveats.append(caveat)

    if events:
        await events.emit(
            f"subagent:{NAME}", "done",
            f"Large-Scale Aggregate: {grouping}, {total_groups} group(s)",
            sources=[
                {"source_tool": c.source_tool, "label": c.display_label()}
                for c in citations
            ],
        )

    return SubAgentResult(
        status=SubAgentStatus.PARTIAL if degraded_from else SubAgentStatus.OK,
        # The bounded rendering, not the raw table: top-N with the total stated
        # alongside so truncation is never mistaken for the whole set.
        answer_text=(
            f"{answer}\n\n{summary_text}"
            + (f"\n\n({total_groups} group(s) in total, grouped by {grouping}.)"
               if total_groups else "")
        ),
        citations=citations,
        tools_used=["XAGG"],
        degraded_from=degraded_from,
        caveats=caveats,
    )
