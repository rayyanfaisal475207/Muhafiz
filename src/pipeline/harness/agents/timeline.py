"""
Timeline Building sub-agent.

Produces an ordered event list for a case, each event carrying a three-state
conflict flag, per docs/SUBAGENT_INTERFACES.md §2.1.

WHAT THIS COMPOSES, AND WHAT IT DELIBERATELY DOES NOT CALL
──────────────────────────────────────────────────────────
`conflict_detection.detect_conflicts()` is a background WRITER: it returns
None, runs an LLM pass, and writes `CONFLICTS_WITH` edges into the graph.
Calling it from a read-path query would mean LLM work and graph mutations every
time an investigator asks for a timeline. This sub-agent therefore reads the
ALREADY-COMPUTED conflicts that the GRAPH tool surfaces (via
`_fetch_case_conflicts`, which tags chunks with `metadata.conflict_basis`), and
never triggers detection itself.

[RESOLVED-5] THE THREE-STATE FLAG, AND WHY NONE IS CURRENTLY UNREACHABLE
─────────────────────────────────────────────────────────────────────────
This is the first live path to exercise `ConflictState`, and the ambiguity it
exists for is real here rather than theoretical.

The absence of `CONFLICTS_WITH` edges means one of two different things:

    * detection ran and found nothing        -> NONE
    * detection never ran, or never finished -> UNKNOWN

and NOTHING CURRENTLY DISTINGUISHES THEM AT READ TIME. Three independent ways a
case can have no conflict edges without having been checked:

  1. THE RACE. `service.py` schedules detection with a bare
     `asyncio.create_task()` AFTER graph extraction has already written the
     events. Between that write and the task finishing (an LLM call — seconds),
     the events are queryable and no conflict edges exist yet.
  2. DETECTION ONLY RUNS ON INGESTION WITH A case_id. Events that reach the
     graph any other way — a backfill, a manual write, an ingest where case_id
     was omitted — never trigger it at all.
  3. `detect_conflicts()` RETURNS EARLY on fewer than two incidents, and
     SWALLOWS its own fetch failure (logs a warning, returns).

Nothing records that detection completed for a case, so there is no marker to
read either.

RESOLVED BY THE MARKER (migration 019): `cases.conflicts_checked_at` records
when detection COMPLETED for a case, written by the background task ON RETURN.
That is what makes NONE honest:

    * conflict basis present                       -> CONFLICT
    * no basis, marker present (check completed)    -> NONE
    * no basis, marker absent                      -> UNKNOWN

The marker is written on return rather than at schedule time precisely so the
race in (1) stays correctly represented — a query arriving while detection is
in flight finds no marker and reads UNKNOWN. It covers detect_conflicts()'s
early return on fewer than two incidents (a completed check with nothing to
find) but NOT its raise path, since a failed check is not a check.

Anything that prevents establishing the marker — absent, unreadable, gateway
error, failed graph call — leaves conflict state at UNKNOWN. Failing closed is
the point: a wrong UNKNOWN costs precision, a wrong NONE asserts an all-clear
the system never performed.

INVESTIGATOR LOCKS: ENFORCED UPSTREAM, NOT DUPLICATED HERE
───────────────────────────────────────────────────────────
`versioning.py::write_edge()` refuses to supersede a locked OCCURRED_ON edge
(logs and returns None rather than overriding an investigator). That is the
single graph write chokepoint. This sub-agent only READS — it has no write path
for a lock to guard — so re-checking here would guard nothing. `locked` is
surfaced per event because an investigator should see which events are
verified, but it is reported, never enforced.

TRACE MECHANISM (§2.1.4.1): TOOL-EMITTED EVENTS SUFFICE
────────────────────────────────────────────────────────
The decision test is "can any two legs end up as the same effective source?"
Here there is only ONE tool (GRAPH) plus a read of data it already carries.
Two STEPS, not two independently-degradable legs, and nothing can collapse into
anything else — there is no second source for GRAPH to fall back into within
this sub-agent. That places it in the Case Summarization category, not the
Investigative Analysis one: `tool:graph` maps one-to-one onto the only source,
so sub-agent-interpreted `timeline:*` events would emit a second event for the
same transition, which §2.2 forbids as much as emitting none.

[PRESERVE — design §3] Only `TimelineEvent`/`Citation` objects cross the
handoff boundary. Raw graph edge rows never do.

NOT wired into supervisor routing yet. Tracing is automatic via the
supervisor's completion hook.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from src.pipeline.harness.contracts import (
    CallerContext,
    Citation,
    ConflictState,
    EvidenceChunk,
    GraphToolInput,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    TimelineEvent,
    ToolError,
    ToolStatus,
)
from src.pipeline.harness.events import EventRecorder
from src.pipeline.harness.generation import generate_case_scoped_answer
from src.pipeline.harness.tools import registry
from src.pipeline.harness.verifier_gate import UNGROUNDED_TRIGGER, verify_grounding

logger = logging.getLogger(__name__)

NAME = "timeline"

# Bounding happens here, not at the supervisor.
_MAX_EVENTS = 20

# Traversal is capped tighter than a general graph query: a timeline wants the
# events attached to this case, not a deep transitive walk out from them.
_TIMELINE_MAX_HOPS = 1

_ABSTENTION_CAVEAT = (
    "I could not build a timeline for this case from the available evidence."
)

_UNKNOWN_CONFLICT_CAVEAT = (
    "Conflict detection could not be completed for this timeline, so the events "
    "below have not been checked for contradictions."
)

# ISO-ish date at the start of a metadata value. Deliberately permissive: the
# graph stores dates as free text, and an unparseable date must leave the event
# undated rather than drop it from the timeline.
_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


def _extract_date(chunk: EvidenceChunk) -> Optional[str]:
    """
    Best-effort event date from chunk metadata, then text.

    Returns None when no date is recoverable — `TimelineEvent.occurred_on` is
    Optional precisely so an undated event still appears on the timeline rather
    than being silently dropped. An event we cannot date is still an event.
    """
    meta = chunk.metadata.model_dump()
    for key in ("occurred_on", "date", "event_date", "incident_date"):
        value = meta.get(key)
        if value:
            match = _DATE_RE.search(str(value))
            return match.group(1) if match else str(value)
    match = _DATE_RE.search(chunk.text or "")
    return match.group(1) if match else None


def _conflict_state_for(
    chunk: EvidenceChunk, detection_confirmed: bool,
) -> tuple[ConflictState, Optional[str]]:
    """
    [RESOLVED-5] Resolve one event's three-state conflict flag.

    A conflict basis on the chunk is positive evidence: detection demonstrably
    ran and found something, so CONFLICT is always safe to assert.

    Everything else is UNKNOWN. `detection_confirmed` is the hook for a future
    per-case "detection completed" marker — until one exists it is always
    False, because no read-time signal can prove the check happened (see the
    module docstring). NONE is therefore currently unreachable BY DESIGN, not
    by oversight: asserting "checked, clean" without evidence the check ran is
    the precise failure this contract forbids.
    """
    basis = chunk.metadata.model_dump().get("conflict_basis")
    if basis:
        return ConflictState.CONFLICT, str(basis)

    if detection_confirmed:
        # Reachable only once a completion marker is threaded in. The ONLY path
        # that may assert NONE — that the check happened and was clean.
        return ConflictState.NONE, None

    return ConflictState.UNKNOWN, None


async def _conflict_detection_confirmed(case_id: str, gateway: Any) -> bool:
    """
    Has conflict detection COMPLETED for this case (migration 019)?

    Reads `cases.conflicts_checked_at`, written by the background task on
    return. Any failure to establish this — no marker, no case row, gateway
    error — returns False, which keeps conflict state at UNKNOWN. Failing
    closed here is the whole point: the cost of a wrong False is a slightly
    less precise timeline, while the cost of a wrong True is asserting a clean
    check that never happened.
    """
    try:
        gw = gateway
        if gw is None:
            from src.data_gateway import get_gateway

            gw = await get_gateway()
        case = await gw.get_case(case_id)
    except Exception as exc:
        logger.warning(
            "Could not read conflict-detection marker for case %s: %s — "
            "reporting conflict state as unknown.", case_id, exc,
        )
        return False

    return bool(case and case.get("conflicts_checked_at"))


def _sort_key(event: TimelineEvent) -> tuple[int, str]:
    """
    Chronological, with undated events last rather than first.

    A None date sorting to the top would put the least-known events at the head
    of the timeline, which reads as though they came first.
    """
    return (1, "") if event.occurred_on is None else (0, event.occurred_on)


async def _synthesize(
    query_text: str,
    timeline_events: list[TimelineEvent],
    chunks: list[EvidenceChunk],
    caller: CallerContext,
    conversation_context: Optional[str] = None,
) -> str:
    """
    Generate the timeline narrative, using the same generator and prompt the
    legacy case-scoped routes use.

    `[Document N]` markers are produced by the model against the numbered
    evidence block and match `chunks` positionally ([PRESERVE — design §5]).
    This function must never reorder `chunks`.

    The ALREADY-ORDERED event list is handed to the model as part of the query
    rather than left implicit in the chunks. That ordering is this sub-agent's
    own deterministic work — it comes from `_build_timeline`, not from the
    model — and re-deriving it from raw passages is exactly the error the
    deterministic pass exists to prevent. The model narrates the sequence; it
    does not decide it.
    """
    if UNGROUNDED_TRIGGER in query_text:
        markers = " ".join(f"[Document {i}]" for i in range(1, len(chunks) + 1))
        return (
            f"{UNGROUNDED_TRIGGER} Timeline of {len(timeline_events)} recorded "
            f"event(s) for this case, ordered by date. {markers}"
        )

    # A CONFLICT event is annotated inline rather than dropped: a contradiction
    # between sources is a finding an investigator needs surfaced, and
    # final_response.txt rule 6 already instructs the model to lead with a
    # "[Known contradiction]" note when one is present.
    ordered_lines: list[str] = []
    for ev in timeline_events:
        note = ""
        if ev.conflict_state is ConflictState.CONFLICT:
            note = f"  [Known contradiction] {ev.conflict_basis or 'sources disagree'}"
        ordered_lines.append(f"- {ev.occurred_on or 'undated'}: {ev.description}{note}")
    ordered = "\n".join(ordered_lines)
    augmented_query = (
        f"{query_text}\n\n"
        "--- CHRONOLOGY EXTRACTED FROM THIS CASE (already ordered; do not "
        "reorder) ---\n"
        f"{ordered or '(no dated events extracted)'}\n"
        "--- END CHRONOLOGY ---\n"
        "Narrate this chronology, citing [Document N] for each claim."
    )

    return await generate_case_scoped_answer(
        query_text=augmented_query,
        chunks=chunks,
        preferred_language=caller.preferred_language,
        conversation_context=conversation_context,
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
    """Execute Timeline Building. Returns a bounded `SubAgentResult`."""
    if events:
        await events.emit(
            f"subagent:{NAME}", "active", "Timeline Building: gathering case events"
        )

    caller = agent_input.caller

    graph_result = await registry.graph_tool(
        GraphToolInput(
            query_text=agent_input.query_text,
            caller=caller,
            target_entity=None,  # case-wide: every event, not one entity's
            max_hops=_TIMELINE_MAX_HOPS,
            hybrid=False,
        ),
        events=events,
    )

    # [RESOLVED-5] `cases.conflicts_checked_at` (migration 019) is the evidence
    # that detection actually COMPLETED for this case — written by the
    # background task on return, so a query racing an in-flight detection still
    # finds nothing here and correctly reads UNKNOWN. Absent marker, unreadable
    # marker, or a failed graph call all leave this False, and NONE stays
    # unreachable.
    detection_confirmed = False
    if graph_result.status is not ToolStatus.FAILED and caller.active_case_id:
        detection_confirmed = await _conflict_detection_confirmed(
            caller.active_case_id, gateway
        )

    raw_chunks = list(graph_result.chunks or [])

    # ── No events for this case → an EXPLICIT EMPTY TIMELINE ──
    # A legitimate "nothing to show", NOT an error and NOT an abstention: the
    # question was answerable and the answer is that no events are recorded.
    # Distinguished from a tool FAILURE, which abstains below.
    if graph_result.status is ToolStatus.FAILED:
        if events:
            await events.emit(
                f"subagent:{NAME}", "error", "Timeline Building: could not read case events"
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            tools_used=[],
            degraded_from=["GRAPH"],
            caveats=[_ABSTENTION_CAVEAT],
            error=graph_result.error,
        )

    if not raw_chunks:
        if events:
            await events.emit(
                f"subagent:{NAME}", "done",
                "Timeline Building: no recorded events for this case",
            )
        return SubAgentResult(
            status=SubAgentStatus.OK,
            answer_text="No dated events are recorded for this case.",
            citations=[],
            tools_used=["GRAPH"],
            degraded_from=[],
            timeline=[],
        )

    # ── Build the ordered event list ──
    chunks = raw_chunks[:_MAX_EVENTS]
    timeline: list[TimelineEvent] = []
    for chunk in chunks:
        state, basis = _conflict_state_for(chunk, detection_confirmed)
        meta = chunk.metadata.model_dump()
        timeline.append(
            TimelineEvent(
                event_id=chunk.id,
                description=(chunk.text or "").strip()[:400],
                occurred_on=_extract_date(chunk),
                conflict_state=state,
                conflict_basis=basis,
                # Reported, never enforced — see the module docstring.
                locked=bool(meta.get("locked", False)),
            )
        )
    timeline.sort(key=_sort_key)

    # ── Generate, then gate ──
    try:
        answer = await _synthesize(
            agent_input.query_text,
            timeline,
            chunks,
            agent_input.caller,
            agent_input.conversation_context,
        )
    except Exception as exc:
        # Generation infrastructure failed. The deterministic timeline itself
        # succeeded, but there is no narrative to verify and nothing safe to
        # serve — abstain rather than emit unverified prose.
        logger.error("Timeline Building generation failed: %s", exc)
        if events:
            await events.emit(
                f"subagent:{NAME}", "error",
                "Timeline Building: narrative generation failed",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            citations=[],
            timeline=[],
            tools_used=[],
            degraded_from=["GRAPH"],
            error=ToolError(
                kind="upstream_failure",
                message=f"Timeline narrative generation failed: {exc}",
            ),
        )

    verdict = await verify_grounding(
        answer=answer,
        cited_chunks=[c.model_dump() for c in chunks],
        case_id=caller.active_case_id,
        cross_case_ids=None,
    )

    if not verdict["grounded"]:
        if events:
            await events.emit(
                f"subagent:{NAME}", "error",
                f"Timeline Building: grounding check failed — {verdict['reason']}",
            )
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            answer_text=None,
            citations=[],
            tools_used=[],
            degraded_from=["GRAPH"],
            caveats=[_ABSTENTION_CAVEAT],
        )

    caveats: list[str] = []
    degraded_from: list[str] = []
    # Keyed on the ACTUAL per-event states rather than on a flag, so the caveat
    # cannot drift out of step with what the timeline says. An unverified
    # timeline is usable but degraded, and the reader is told so explicitly
    # rather than being left to infer it from a flag they may not notice.
    if any(e.conflict_state is ConflictState.UNKNOWN for e in timeline):
        caveats.append(_UNKNOWN_CONFLICT_CAVEAT)
        degraded_from.append("GRAPH")

    for caveat in getattr(graph_result, "degradation_caveats", []) or []:
        if caveat not in caveats:
            caveats.append(caveat)

    citations = _to_citations(chunks)
    conflicts = sum(1 for e in timeline if e.conflict_state is ConflictState.CONFLICT)

    if events:
        await events.emit(
            f"subagent:{NAME}", "done",
            f"Timeline Building: {len(timeline)} event(s)"
            + (f", {conflicts} with conflicts" if conflicts else ""),
            sources=[
                {"source_tool": c.source_tool, "label": c.display_label()}
                for c in citations
            ],
        )

    return SubAgentResult(
        status=SubAgentStatus.PARTIAL if degraded_from else SubAgentStatus.OK,
        answer_text=answer,
        citations=citations,
        tools_used=["GRAPH"],
        degraded_from=degraded_from,
        caveats=caveats,
        timeline=timeline,
    )
