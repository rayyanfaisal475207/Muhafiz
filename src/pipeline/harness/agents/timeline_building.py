"""
Timeline Building sub-agent —
src/pipeline/harness/agents/timeline_building.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 4, "Phase 5" per this
session's brief and §8's build checklist).

Source of truth: SUBAGENT_INTERFACES.md §2.1's "Timeline Building" row +
RESOLVED-5 in §3, `TimelineEvent`/`ConflictState` (already in
`types.py`, transcribed there per the §11 contract amendment — NOT
redefined here), AGENT_HARNESS_DESIGN.md §3's matching row, and this
session's explicit brief.

═══════════════════════════════════════════════════════════════════════
DEVIATION 1 (resolved with the user via AskUserQuestion before writing
any code, not guessed) — DATA SOURCE IS A NEW DEDICATED CYPHER TEMPLATE,
NOT graph_tool().
═══════════════════════════════════════════════════════════════════════
This session's brief describes composing "the Phase 0 GRAPH tool
wrapper, filtered to date-bearing (OCCURRED_ON) edges." Read literally
that is not possible: `graph_tool()`/`retrieve_graph()`
(src/pipeline/harness/tools/graph.py, src/retrieval/graph_retriever.py)
performs ASSOCIATED_WITH entity traversal and returns document-text
`EvidenceChunk`s that carry no `occurred_on` date field anywhere, and its
existing conflict-chunk path (`_fetch_case_conflicts()`) does not surface
which `Incident.entity_id` each returned chunk belongs to. There is no
way to assemble `TimelineEvent.occurred_on` from what `graph_tool()`
returns today, and no other exported function anywhere already returns
"Incident + its OCCURRED_ON date" — the only place that query shape
existed before this session was inlined, unexported, inside
`src/graph/conflict_detection.py::detect_conflicts()`.

AGENT_HARNESS_DESIGN.md §4.5 explicitly anticipates exactly this: "If the
harness introduces new within-case Cypher templates anywhere (e.g.
inside Timeline Building), route them through `scoped_cypher()`, not raw
`age_client.execute_cypher()`." That sentence only makes sense if
Timeline Building was expected to need a genuinely new case-scoped Cypher
template, not to force `graph_tool()`'s existing shape into a job it
cannot do.

Resolved with the user: this module defines two small, dedicated,
case-scoped Cypher templates (`_fetch_dated_incidents`,
`_fetch_conflict_bases`, below), both routed through
`src.graph.case_scope.scoped_cypher()` per §4.5, mirroring
`conflict_detection.py`'s own `Incident`/`OCCURRED_ON` query shape and
`graph_retriever.py::_fetch_case_conflicts()`'s own `CONFLICTS_WITH`
query shape (both already-proven patterns against the real AGE instance —
this module reuses their MATCH structure, not their exact projections).
Neither `src/pipeline/harness/tools/graph.py` nor
`src/retrieval/graph_retriever.py` (Phase 0, already-merged, other
sub-agents depend on them) is touched. `graph_tool()` is not called by
this sub-agent at all — there is nothing it could contribute here that
these two dedicated queries don't already cover more precisely.
`tools_used`/`degraded_from` still tag this data as `"GRAPH"`
(`SourceTool`) throughout, since it is graph-sourced evidence via the
same `evidence_graph` AGE instance, retrieved via Cypher — the
`SourceTool` classification is about the retrieval MECHANISM class, not
about which specific wrapper function ran.

The compliance suite's enforcement-point-5 check
(`src/pipeline/harness/compliance/test_enforcement_5_scoped_cypher.py`)
only scans the fixed list of Phase 0 tool wrapper files
(`_source_scan.py::TOOL_WRAPPER_MODULE_NAMES`) for a raw
`age_client`/`execute_cypher` reference — this module is a sub-agent, not
a tool wrapper, so it is out of that check's scope by construction, and
correctly so: it never calls `age_client.execute_cypher()` directly
either, only `scoped_cypher()`, so the actual security property (every
within-case template provably references `$case_id` or refuses to run)
holds regardless.

═══════════════════════════════════════════════════════════════════════
DEVIATION 2 (also resolved with the user before writing code) —
RESOLVED-5's `degraded_from` WHEN CONFLICT DETECTION ALONE FAILS.
═══════════════════════════════════════════════════════════════════════
RESOLVED-5 (SUBAGENT_INTERFACES.md §3) says a conflict-detection failure
(with date edges resolving fine) should return `status=PARTIAL` with
`degraded_from` "recording the conflict check" that degraded. But
`SubAgentResult.degraded_from: list[SourceTool]`, and `SourceTool` is a
closed Literal with no conflict-detection-specific member — conflict
detection is Phase 8 machinery (`src/graph/conflict_detection.py`), not
one of the eight `SourceTool` primitives. Literally writing `"GRAPH"`
into `degraded_from` here would collide with `tools_used=["GRAPH"]`
(the date-edge fetch genuinely succeeded) and violate
SUBAGENT_INTERFACES.md §2.0's stated invariant, "a tool never appears in
both lists for the same call."

Resolved with the user: `degraded_from` stays `[]` in this branch. GRAPH
correctly belongs in `tools_used` only, since its date-edge fetch
actually succeeded. The conflict-check failure is instead signaled by
(a) `status=PARTIAL`, (b) every `TimelineEvent.conflict_state=UNKNOWN`
(never `NONE` — see RESOLVED-5, enforced by construction below since
`_build_events(..., conflict_bases=None)` never sets `NONE`), and (c) an
explicit `caveats` entry naming conflict detection by name as the
degraded component. No `SourceTool`/`types.py` change was made or
proposed for this.

═══════════════════════════════════════════════════════════════════════
DECISION (not silently picked — this session's brief asked for it by
name) — TimelineEvent.description IS DETERMINISTIC, GRAPH-DERIVED, NEVER
LLM-COMPOSED.
═══════════════════════════════════════════════════════════════════════
`TimelineEvent.description` is copied directly from `Incident.description`
(the graph node property extracted by `src/extraction/domain_entities.py`
at ingestion time) — never regenerated, paraphrased, or otherwise passed
through an LLM. Reasoning, per this session's own brief and the
precedent it names:

  - This is safety-critical structured data feeding a live investigation.
    Per-event LLM generation would introduce a per-event hallucination
    risk (a fabricated or subtly altered description of what happened)
    for no benefit — the graph already holds the extracted, grounded
    description text.
  - This is the exact reasoning that already exempts XAGG's
    `raw_summary_text` from the Verifier (AGENT_HARNESS_IMPLEMENTATION_PLAN.md
    §9's Phase 3 entry, SUBAGENT_INTERFACES.md §1.5): a deterministic
    string derived directly from computed/extracted data was never
    submitted to any generation step and therefore has nothing to
    hallucinate. The same property holds here, per-event, at a smaller
    grain.
  - Consequence: no `verify_grounding()` call is made anywhere in this
    sub-agent, for the event list OR for `answer_text` below.
    `answer_text` is also built as a short, fully deterministic
    string (event count, date span, conflict counts) via plain string
    formatting over already-computed data — not model-generated — so it
    carries the same "nothing to hallucinate" property and needs no
    Verifier call either. This still satisfies "produce a short,
    deterministic `answer_text` ... for consistency with every other
    sub-agent returning non-null `answer_text` on a non-abstained
    result" (this session's brief) without inventing a second exemption
    category — it is the same XAGG-precedent exemption, applied to a
    second, independently-deterministic string in the same sub-agent.

═══════════════════════════════════════════════════════════════════════
NO ROLE GATE (case-assignment-based scoping only, matching Case
Summarization's precedent). Neither of this module's two Cypher
templates carries a cross-case role check — both are within-case,
`scoped_cypher()`-enforced, exactly like GRAPH/GRAPH_HYBRID
(design §2.2). `SubAgentStatus.DENIED` is therefore never built as an
output path here, per this session's explicit instruction.

═══════════════════════════════════════════════════════════════════════
CLASSIFICATION REACHABILITY — KNOWN, DOCUMENTED, NOT CLOSED THIS SESSION.
═══════════════════════════════════════════════════════════════════════
`router.py` has no classification signal for Timeline Building
(documented in Phase 1's own progress-log entry, AGENT_HARNESS_IMPLEMENTATION_PLAN.md
§9, and reiterated in §11's contract-amendment entry). This session does
NOT invent new trigger keywords/patterns to close that gap — per this
session's explicit instruction, new classification logic needs the same
evidence-driven basis XAGG/XGRAPH/XNETWORK's own deterministic overrides
had (live-confirmed misclassification failures), not guessed patterns.
This sub-agent is built, registered, and tested via DIRECT DISPATCH
(calling `timeline_building()` directly) and a Supervisor integration
test that BYPASSES `route_query()`'s actual classification (by
monkeypatching `classify_to_subagent()` to force the route, the same
shape of bypass Phase 3/4's own Supervisor integration tests use for
`route_query()` itself) — never through real end-user query
classification. `Supervisor.handle()`'s real classification path
(`route_query()` -> `_ROUTE_TO_SUBAGENT`) still cannot reach
`TIMELINE_BUILDING` today; that gap is real, tracked, and explicitly NOT
closed by this session.

NOT IN SCOPE THIS SESSION: Cross-Case Linkage, the Validation module,
new classification/routing logic, live wiring into
main.py/orchestrator.py/router.py.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from src.graph.case_scope import scoped_cypher
from src.pipeline.harness.supervisor import TIMELINE_BUILDING, register
from src.pipeline.harness.types import (
    ConflictState,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    TimelineEvent,
    ToolError,
)

logger = logging.getLogger(__name__)


async def _fetch_dated_incidents(case_id: str) -> list[dict]:
    """
    Case-scoped Incident nodes carrying a live (non-superseded)
    OCCURRED_ON edge — the literal "date-bearing edges" filter this
    sub-agent composes, per module docstring's Deviation 1. A mandatory
    (not OPTIONAL) MATCH on OCCURRED_ON IS the date-bearing filter: an
    Incident with no such edge simply never appears in these rows.

    Mirrors conflict_detection.py's own Incident/OCCURRED_ON query shape
    (two mandatory MATCH clauses, Date reached via the same pattern) —
    that module's own comment documents a live AGE bug where an OPTIONAL
    MATCH preceding a mandatory MATCH is invalid openCypher; using a
    mandatory MATCH here sidesteps that entire class of bug rather than
    reproducing it. `occ.superseded_by IS NULL` follows the same
    live-edge convention every other read in graph_retriever.py already
    applies (e.g. `_one_hop_neighbors`, `_fetch_appears_in`) — excludes a
    since-revised prior date, keeping only the current OCCURRED_ON edge
    per incident.
    """
    rows = await scoped_cypher(
        """
        MATCH (i:Incident)-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id})
        MATCH (i)-[occ:OCCURRED_ON]->(d:Date)
        WHERE occ.superseded_by IS NULL
        RETURN i.entity_id AS entity_id, i.description AS description,
               d.date AS occurred_on, occ.locked AS locked
        """,
        case_id,
        columns=["entity_id", "description", "occurred_on", "locked"],
    )
    # Defensive de-dup on entity_id: two live, non-superseded OCCURRED_ON
    # edges off the same Incident should not happen (versioning.py's own
    # supersede discipline is meant to prevent it), but this sub-agent
    # does not re-verify that invariant — a duplicate row must not
    # silently become two timeline entries for one real-world incident.
    seen: set[str] = set()
    out: list[dict] = []
    for row in rows:
        entity_id = row.get("entity_id")
        if not entity_id or entity_id in seen:
            continue
        seen.add(entity_id)
        out.append(row)
    return out


async def _fetch_conflict_bases(case_id: str) -> dict[str, list[str]]:
    """
    entity_id -> [basis, ...] for every live CONFLICTS_WITH edge touching
    an Incident in this case. Mirrors
    graph_retriever.py::_fetch_case_conflicts()'s own MATCH shape
    verbatim (same already-proven pattern against the real AGE instance)
    with a different projection: entity_id + basis text, not
    chunk-shaped rows — this sub-agent needs to know WHICH incident
    conflicts, not that incident's source document chunk.

    A self-referential row (a_id == b_id) represents
    conflict_detection.py's own deterministic "multiple contradictory
    dates for the same incident" check — handled correctly here without
    special-casing, since both sides of the fold below add to the same
    entity_id's basis list either way.
    """
    rows = await scoped_cypher(
        """
        MATCH (a:Incident)-[r:CONFLICTS_WITH]->(b:Incident)-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id})
        WHERE r.superseded_by IS NULL
        RETURN a.entity_id AS a_id, b.entity_id AS b_id, r.basis AS basis
        """,
        case_id,
        columns=["a_id", "b_id", "basis"],
    )
    bases: dict[str, list[str]] = {}
    for row in rows:
        basis = row.get("basis") or "Contradiction detected between these two incidents."
        for entity_id in (row.get("a_id"), row.get("b_id")):
            if entity_id:
                bases.setdefault(entity_id, []).append(basis)
    return bases


def _sort_key(occurred_on: Optional[str]) -> tuple:
    """
    Best-effort chronological ordering. `Date.date` (see
    entity_resolution.py's `if entity_type == "incident"` branch) is
    whatever string the extraction step produced — not guaranteed
    strict ISO-8601 in every case. A parseable value sorts first, by
    actual date; an unparseable one sorts after, by raw string, so the
    ordering stays fully deterministic either way rather than raising.
    """
    if occurred_on:
        try:
            return (0, datetime.fromisoformat(occurred_on))
        except ValueError:
            pass
    return (1, occurred_on or "")


def _build_events(
    rows: list[dict], conflict_bases: Optional[dict[str, list[str]]]
) -> list[TimelineEvent]:
    """
    `conflict_bases is None` means the conflict-detection fetch itself
    failed (RESOLVED-5's exact scenario) — every event MUST get
    `conflict_state=UNKNOWN` (the type's own default; never set `NONE`
    here, since `NONE` may only be set on a check that actually
    succeeded and found nothing — RESOLVED-5). When `conflict_bases` is a
    real (possibly empty) dict, the check DID succeed, so an incident
    absent from it is correctly `NONE` ("checked, no conflict found"),
    not `UNKNOWN`.
    """
    events: list[TimelineEvent] = []
    for row in rows:
        entity_id = row["entity_id"]
        description = row.get("description") or f"Incident {entity_id} (no description recorded)"
        locked = bool(row.get("locked"))

        if conflict_bases is None:
            conflict_state = ConflictState.UNKNOWN
            conflict_basis = None
        else:
            bases = conflict_bases.get(entity_id)
            if bases:
                conflict_state = ConflictState.CONFLICT
                conflict_basis = "; ".join(dict.fromkeys(bases))
            else:
                conflict_state = ConflictState.NONE
                conflict_basis = None

        events.append(
            TimelineEvent(
                event_id=entity_id,
                description=description,
                occurred_on=row.get("occurred_on"),
                conflict_state=conflict_state,
                conflict_basis=conflict_basis,
                locked=locked,
            )
        )

    events.sort(key=lambda e: _sort_key(e.occurred_on))
    return events


def _answer_text(events: list[TimelineEvent], conflict_checked: bool) -> str:
    """
    Deterministic, graph-derived summary — see module docstring's
    "deterministic description" decision for why this needs no
    `verify_grounding()` call. Built via plain string formatting over
    already-computed `events`, never model-generated.
    """
    n = len(events)
    span = ""
    if events and events[0].occurred_on and events[-1].occurred_on:
        span = f" (spanning {events[0].occurred_on} to {events[-1].occurred_on})"

    if not conflict_checked:
        return (
            f"This case's timeline has {n} dated event(s){span}. Conflict "
            "detection could not be completed for this case, so conflict "
            "status is unknown for every event."
        )

    conflict_count = sum(1 for e in events if e.conflict_state == ConflictState.CONFLICT)
    clear_count = sum(1 for e in events if e.conflict_state == ConflictState.NONE)
    return (
        f"This case's timeline has {n} dated event(s){span}. "
        f"{conflict_count} event(s) flagged with a contradicting record; "
        f"{clear_count} checked with no conflict found."
    )


async def timeline_building(agent_input: SubAgentInput) -> SubAgentResult:
    """The Timeline Building sub-agent. See module docstring for the full contract."""
    case_id = agent_input.execution.caller.active_case_id

    if not case_id:
        # scoped_cypher() itself refuses an empty/None case_id (case_scope.py)
        # — this sub-agent is entirely within-case, so a query with no
        # active case has nothing to scope a timeline to. Treated as a
        # legitimate EMPTY (nothing to build), not an infrastructure
        # failure: there genuinely is no case here, which is a different
        # fact from "the graph query failed."
        return SubAgentResult(
            status=SubAgentStatus.EMPTY,
            caveats=["No active case was specified; a timeline requires a case to scope to."],
        )

    try:
        rows = await _fetch_dated_incidents(case_id)
    except Exception as exc:
        logger.error("Timeline Building: date-edge fetch failed for case %s: %s", case_id, exc)
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
            caveats=["Timeline data could not be retrieved for this case."],
        )

    # [PRESERVE — SUBAGENT_INTERFACES.md §2.1's Timeline Building row]
    # "No date-bearing edges found -> explicit empty timeline, status=EMPTY,
    # not an error -- a legitimate 'nothing to show', distinct from tool
    # failure." Conflict detection is not even attempted here: there is
    # nothing for it to attach a conflict_state to.
    if not rows:
        return SubAgentResult(
            status=SubAgentStatus.EMPTY,
            caveats=["No dated events were found in the case graph for this case."],
        )

    try:
        conflict_bases = await _fetch_conflict_bases(case_id)
    except Exception as exc:
        # [RESOLVED-5's exact scenario] Date edges resolved fine (rows is
        # non-empty, above); conflict detection failed independently.
        # Every event gets conflict_state=UNKNOWN, never NONE — see
        # module docstring's Deviation 2 for why degraded_from stays [].
        logger.error(
            "Timeline Building: conflict-detection fetch failed for case %s: %s", case_id, exc
        )
        events = _build_events(rows, conflict_bases=None)
        return SubAgentResult(
            status=SubAgentStatus.PARTIAL,
            answer_text=_answer_text(events, conflict_checked=False),
            events=events,
            tools_used=["GRAPH"],
            degraded_from=[],
            caveats=[
                "Conflict detection could not be completed for this case; "
                "conflict status is unknown for every event in this timeline."
            ],
        )

    events = _build_events(rows, conflict_bases=conflict_bases)
    return SubAgentResult(
        status=SubAgentStatus.OK,
        answer_text=_answer_text(events, conflict_checked=True),
        events=events,
        tools_used=["GRAPH"],
        degraded_from=[],
    )


timeline_building.name = TIMELINE_BUILDING

# Import-time self-registration -- the same pattern semantic_search.py/
# large_scale_aggregate.py/case_summarization.py established
# (supervisor.py's own module docstring documents it for every future
# sub-agent module). Importing this module is what makes Timeline
# Building live in the module-level registry. Per module docstring's
# "CLASSIFICATION REACHABILITY" section, `Supervisor.handle()`'s real
# `route_query()` classification path cannot reach this name yet --
# registration only makes it dispatchable via a route_result whose
# classify_to_subagent() output is TIMELINE_BUILDING, which nothing
# produces today outside of a test forcing it directly.
register(timeline_building)
