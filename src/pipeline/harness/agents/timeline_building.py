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
`_build_events(..., conflict_bases=None, detection_confirmed=False)`
never sets `NONE`), and (c) an explicit `caveats` entry naming conflict
detection by name as the degraded component. No `SourceTool`/`types.py`
change was made or proposed for this.

[Reconciliation fix — harness-reconciliation Unit 6, post-hoc addition to
this deviation] A SUCCESSFUL `_fetch_conflict_bases()` call is not, by
itself, proof detection ever ran for this case — a live `CONFLICTS_WITH`
query can return zero rows because detection genuinely found nothing
*or* because it never ran/hasn't finished yet (see `cases.
conflicts_checked_at`'s own migration comment, `018_case_conflicts_checked_at.sql`,
for the three concrete race/gap conditions). `_build_events()` now takes a
`detection_confirmed` flag (read from that marker via
`_conflict_detection_confirmed()`, gateway-backed, fails closed to
`False`) and only asserts `ConflictState.NONE` for an absent basis when
it is `True` — otherwise an absent basis renders `UNKNOWN`, same as a
failed fetch. A present basis still always asserts `CONFLICT`
unconditionally (positive evidence needs no marker). This closes a real
gap the original resolution above did not address: it only handled
`_fetch_conflict_bases()` *raising*, not the marker-absent-but-the-query-
still-succeeds case, which was silently asserting NONE for cases that had
never actually been checked.

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
from typing import Optional, Sequence

from src.data_gateway.base import DataGateway
from src.graph.case_scope import scoped_cypher
from src.pipeline.harness.supervisor import TIMELINE_BUILDING, register
from src.pipeline.harness.types import (
    ConflictState,
    OnEventCallback,
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

    ONE ROW PER LIVE OCCURRED_ON EDGE, not one row per Incident — M10 of
    the Muhafiz Data API migration (docs/decisions/0001-muhafiz-api-migration.md).
    Before this migration, one Incident carrying more than one live
    OCCURRED_ON edge was assumed to be a bug (a resolution/extraction
    re-run should have superseded the old one, per versioning.py's own
    discipline) — and this function de-duplicated defensively on
    `entity_id`, keeping only whichever row the query happened to return
    first. src/graph/structured_projection.py (M6a) makes that assumption
    wrong on purpose: ONE Incident deliberately carries SEVERAL live,
    non-superseded OCCURRED_ON edges to DIFFERENT Date nodes — the
    incident date itself, one per zimni entry, one per chalaan dispatch —
    each a genuinely different real-world event in that investigation's
    timeline, tagged with a distinct `event_type` property, not competing
    revisions of one fact. The old per-entity_id dedup would have
    silently collapsed a real case's whole multi-event timeline down to
    one arbitrary row. `id(occ)` (the edge's own graph id, the same
    identifier src/api/graph_review.py already uses to address a specific
    edge) is now the dedup key — still a genuine defensive guard against
    a literal duplicate row, without discarding real, distinct events.
    """
    rows = await scoped_cypher(
        """
        MATCH (i:Incident)-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id})
        MATCH (i)-[occ:OCCURRED_ON]->(d:Date)
        WHERE occ.superseded_by IS NULL
        RETURN i.entity_id AS entity_id, i.description AS description,
               d.date AS occurred_on, occ.locked AS locked,
               id(occ) AS occ_id, occ.event_type AS event_type, occ.detail AS detail
        """,
        case_id,
        columns=["entity_id", "description", "occurred_on", "locked", "occ_id", "event_type", "detail"],
    )
    seen: set = set()
    out: list[dict] = []
    for row in rows:
        occ_id = row.get("occ_id")
        if occ_id is None or occ_id in seen:
            continue
        seen.add(occ_id)
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


async def _conflict_detection_confirmed(case_id: str, gateway: Optional[DataGateway]) -> bool:
    """
    [Reconciliation fix — harness-reconciliation Unit 6, migration 018] Has
    conflict detection COMPLETED for this case? Reads `cases.
    conflicts_checked_at`, written by `src/ingestion/conflict_bg.py`'s
    background task only after a genuinely completed run (see that
    column's own migration comment for the full rationale, including the
    three concrete ways detection can be silently absent).

    Any failure to establish this — no marker, no case row, a gateway
    error — returns False, which keeps conflict state at UNKNOWN rather
    than NONE. Failing closed here is the whole point: the cost of a wrong
    False is a slightly less precise timeline (an event correctly checked
    still renders as "unconfirmed"); the cost of a wrong True is asserting
    a clean check that never actually happened.
    """
    try:
        gw = gateway
        if gw is None:
            from src.data_gateway import get_gateway

            gw = await get_gateway()
        case = await gw.get_case(case_id)
    except Exception as exc:
        logger.warning(
            "Timeline Building: could not read conflict-detection marker for "
            "case %s: %s — reporting conflict state as unknown.", case_id, exc,
        )
        return False

    return bool(case and case.get("conflicts_checked_at"))


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


# [findings.md TB-2] `position` is the ONLY event_type whose blank detail
# gets an explicit placeholder. Deliberately NOT generalised to every
# detail-less event: 341 events already render with no detail at all
# (measured: incident 64, arrest 74, chalaan_dispatch 15, zimni_entry 188),
# and for those the absence of a detail is unremarkable — an `incident`
# event simply has no sub-detail to carry. `position` is different because
# the detail IS the payload: the upstream psrms.fir_position row exists
# precisely to record a case position, so a blank one is a recorded review
# that captured no position text.
_POSITION_EVENT_TYPE = "position"

# Wording follows this module's own established idiom — compare
# `f"Incident {entity_id} (no description recorded)"` below. States the
# source fact exactly: a position row exists for this date, and no position
# text was recorded on it.
#
# Deliberately NOT "unknown status": the upstream field is پوزیشن
# (position), not a status enum, and "unknown" would imply the value exists
# but could not be read. Measured on the live corpus, all 65 blank rows
# carry a real status_date and null in EVERY other field
# (position/prosecutor_name/cross_certificate_ref/
# pending_challan_objections/remarks) — nothing was lost in projection;
# there was nothing recorded to begin with.
_NO_POSITION_RECORDED = "no position recorded"


def _build_events(
    rows: list[dict],
    conflict_bases: Optional[dict[str, list[str]]],
    detection_confirmed: bool,
) -> list[TimelineEvent]:
    """
    `conflict_bases is None` means the conflict-detection fetch itself
    failed (RESOLVED-5's exact scenario) — every event MUST get
    `conflict_state=UNKNOWN` (the type's own default; never set `NONE`
    here, since `NONE` may only be set on a check that actually
    succeeded and found nothing — RESOLVED-5).

    [Reconciliation fix — harness-reconciliation Unit 6] A present conflict
    `basis` is positive evidence — detection demonstrably ran and found
    something, so `CONFLICT` is always safe to assert regardless of
    `detection_confirmed`. But an ABSENT basis is ambiguous on its own: a
    live, successful `CONFLICTS_WITH` query with zero matching edges reads
    identically whether detection completed and found nothing, or never
    ran/hasn't finished yet (see `cases.conflicts_checked_at`'s own
    migration comment for the three concrete ways that happens). `NONE` —
    "checked, no conflict found" — may therefore only be asserted when
    `detection_confirmed` is True (the `cases.conflicts_checked_at` marker
    is present for this case); otherwise an absent basis renders `UNKNOWN`,
    the same "not known to have been checked" reading a failed fetch gets.
    Previously this function trusted ANY successfully-returned
    `conflict_bases` dict as proof the check ran — silently asserting NONE
    even when detection had never actually completed for the case, exactly
    the false all-clear RESOLVED-5's three-state design exists to prevent.
    """
    events: list[TimelineEvent] = []
    for row in rows:
        entity_id = row["entity_id"]
        event_type = row.get("event_type")
        detail = row.get("detail")
        base_description = row.get("description") or f"Incident {entity_id} (no description recorded)"
        # M10 (Muhafiz Data API migration): distinguishes the several
        # events one Incident can now genuinely carry (see
        # _fetch_dated_incidents' own docstring) — event_type/detail are
        # only ever present on M6a's typed-timestamp OCCURRED_ON edges;
        # a legacy, LLM-derived incident with no such properties keeps
        # exactly its old bare description.
        #
        # [findings.md TB-1] A typed event carries ONLY its own
        # distinguishing text; the Incident narrative is rendered once, by
        # _incident_narratives() below, instead of being repeated inside
        # every event.
        #
        # T1 populated Incident.description from the FIR's own narrative
        # (avg 1,347 chars, max 2,070). Because a real Incident carries
        # 3-9 dated OCCURRED_ON edges (measured: avg 5.9 across 73/73
        # cases), prefixing that narrative onto every event rendered it
        # ~5.9x per timeline — 577,488 characters corpus-wide where
        # ~98,400 carry the same information, and 17,676 characters in the
        # worst single timeline. Every event read near-identically; the
        # only per-event content was a ~20-character suffix, which is
        # precisely the part a reader needs to tell two events apart.
        #
        # Deliberately NOT a truncation: the full narrative is still
        # served verbatim, once, and no event-specific text is dropped.
        # This removes duplication only.
        #
        # An event with NO event_type keeps its bare description
        # unchanged — a pre-M6a, LLM-derived incident has no
        # distinguishing text to fall back on, so stripping its
        # description would leave it empty. Guarded by
        # test_legacy_row_with_no_event_type_keeps_bare_description.
        if event_type:
            description = f"{event_type}" + (f": {detail}" if detail else "")
            if event_type == _POSITION_EVENT_TYPE and not (detail or "").strip():
                description = f"{event_type}: {_NO_POSITION_RECORDED}"
        else:
            description = base_description
        locked = bool(row.get("locked"))

        # Conflict status is per-INCIDENT (CONFLICTS_WITH connects two
        # Incident nodes), shared by every event this incident now
        # contributes — looked up by entity_id, not the per-edge id below.
        bases = conflict_bases.get(entity_id) if conflict_bases is not None else None
        if bases:
            conflict_state = ConflictState.CONFLICT
            conflict_basis = "; ".join(dict.fromkeys(bases))
        elif conflict_bases is not None and detection_confirmed:
            conflict_state = ConflictState.NONE
            conflict_basis = None
        else:
            conflict_state = ConflictState.UNKNOWN
            conflict_basis = None

        # event_id must be unique PER EVENT, not per incident, now that
        # one incident can contribute several — id(occ) (the edge's own
        # graph id) is the true per-event identity; entity_id alone would
        # collide across an incident's multiple events.
        event_id = f"{entity_id}::{row.get('occ_id')}" if row.get("occ_id") is not None else entity_id

        events.append(
            TimelineEvent(
                event_id=event_id,
                description=description,
                occurred_on=row.get("occurred_on"),
                conflict_state=conflict_state,
                conflict_basis=conflict_basis,
                locked=locked,
            )
        )

    events.sort(key=lambda e: _sort_key(e.occurred_on))
    return events


def _incident_narratives(rows: list[dict]) -> list[str]:
    """
    [findings.md TB-1] Each distinct Incident narrative, once, in the order
    first seen.

    A case usually has exactly one Incident, so this is normally a
    single-element list; it is a list rather than a scalar because
    `_fetch_dated_incidents` is not restricted to one Incident per case and
    a legacy, LLM-derived corpus can carry several. De-duplicated on the
    narrative text itself, not on entity_id: two Incident nodes carrying
    the identical narrative would otherwise reintroduce the duplication
    this exists to remove.

    Only rows that actually have an `event_type` contribute. A row without
    one keeps its narrative inside its own `TimelineEvent.description`
    (see _build_events), so surfacing it here too would duplicate it.
    """
    seen: set[str] = set()
    out: list[str] = []
    for row in rows:
        if not row.get("event_type"):
            continue
        text = (row.get("description") or "").strip()
        if text and text not in seen:
            seen.add(text)
            out.append(text)
    return out


def _answer_text(
    events: list[TimelineEvent], conflict_checked: bool, narratives: Sequence[str] = (),
) -> str:
    """
    Deterministic, graph-derived summary — see module docstring's
    "deterministic description" decision for why this needs no
    `verify_grounding()` call. Built via plain string formatting over
    already-computed `events`, never model-generated.

    [findings.md TB-1] The Incident narrative(s) are prepended here, once
    each, rather than repeated inside every event's own description. Same
    text, same completeness, served exactly once — see _build_events'
    own TB-1 comment for the measured duplication this removes.
    """
    n = len(events)
    span = ""
    if events and events[0].occurred_on and events[-1].occurred_on:
        span = f" (spanning {events[0].occurred_on} to {events[-1].occurred_on})"

    if not conflict_checked:
        summary = (
            f"This case's timeline has {n} dated event(s){span}. Conflict "
            "detection could not be completed for this case, so conflict "
            "status is unknown for every event."
        )
    else:
        conflict_count = sum(1 for e in events if e.conflict_state == ConflictState.CONFLICT)
        clear_count = sum(1 for e in events if e.conflict_state == ConflictState.NONE)
        summary = (
            f"This case's timeline has {n} dated event(s){span}. "
            f"{conflict_count} event(s) flagged with a contradicting record; "
            f"{clear_count} checked with no conflict found."
        )

    if not narratives:
        return summary
    return "\n\n".join(list(narratives) + [summary])


async def timeline_building(
    agent_input: SubAgentInput,
    *,
    on_event: Optional[OnEventCallback] = None,
    gateway: Optional[DataGateway] = None,
) -> SubAgentResult:
    """
    The Timeline Building sub-agent. See module docstring for the full contract.

    [AMENDMENT — pre-Phase-7 contract amendment, mirrors §10/§11's pattern]
    `on_event` is accepted for `SubAgent` protocol conformance and otherwise
    ignored — this sub-agent's own data source is not composed from
    multiple harness tools (see module docstring's deviation note on the
    two dedicated Cypher templates), so there is nothing per-source-tool to
    report.
    """
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
        events = _build_events(rows, conflict_bases=None, detection_confirmed=False)
        return SubAgentResult(
            status=SubAgentStatus.PARTIAL,
            answer_text=_answer_text(
                events, conflict_checked=False, narratives=_incident_narratives(rows),
            ),
            events=events,
            tools_used=["GRAPH"],
            degraded_from=[],
            caveats=[
                "Conflict detection could not be completed for this case; "
                "conflict status is unknown for every event in this timeline."
            ],
        )

    # [Reconciliation fix — harness-reconciliation Unit 6, migration 018]
    # A live, successful CONFLICTS_WITH query returning zero matches is NOT
    # by itself proof detection ran for this case — see cases.
    # conflicts_checked_at's own migration comment for the three concrete
    # ways it can be silently absent. Any failure to read the marker
    # (gateway unavailable, unreadable case row) fails closed to
    # unconfirmed, the same posture a fetch exception gets above.
    detection_confirmed = await _conflict_detection_confirmed(case_id, gateway)

    events = _build_events(rows, conflict_bases=conflict_bases, detection_confirmed=detection_confirmed)
    caveats: list[str] = []
    if not detection_confirmed and any(e.conflict_state == ConflictState.UNKNOWN for e in events):
        caveats.append(
            "Conflict detection has not been confirmed complete for this case; "
            "events with no recorded conflict are shown as unchecked, not clear."
        )
    return SubAgentResult(
        status=SubAgentStatus.PARTIAL if caveats else SubAgentStatus.OK,
        answer_text=_answer_text(
            events, conflict_checked=detection_confirmed, narratives=_incident_narratives(rows),
        ),
        events=events,
        tools_used=["GRAPH"],
        degraded_from=[],
        caveats=caveats,
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
