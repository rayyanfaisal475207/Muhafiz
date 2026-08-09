"""
Data-Quality / Extraction-Coverage sub-agent —
src/pipeline/harness/agents/data_quality.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 row 8, §7.3 "final shape" — "Phase 9"
per this session's brief and §8's build checklist. The 8th and final
sub-agent in §4's table.)

Source of truth: AGENT_HARNESS_IMPLEMENTATION_PLAN.md §7.3 (the closest
thing to a settled spec this sub-agent has), the contract amendment
immediately preceding this branch (`types.py`'s `DataQualityMetric`/
`DataQualityReadiness`/`SubAgentResult.metrics`), and this session's
explicit brief. SUBAGENT_INTERFACES.md has NO row for this sub-agent at
all — its §2.1 table is titled "the seven sub-agents" — confirmed by
reading it before writing any code, not assumed; supervisor.py's own
module docstring already flags this discrepancy. This module follows the
plan doc's 8-name scope, the same resolution Phase 1 already made.

═══════════════════════════════════════════════════════════════════════
WHAT THIS SUB-AGENT IS. Pure structured stats. NO LLM call anywhere in
the happy path — no generation, no `verify_grounding()`, no citations.
Per §7.3: "This is the one agent in the system that structurally cannot
hallucinate, because it never generates prose — worth preserving
deliberately." `answer_text` below is built via plain deterministic
string formatting over already-computed counts, the same "nothing
generated, nothing to verify" property Timeline Building's own
`TimelineEvent.description`/`answer_text` already established
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §9's Phase 5 entry) — extended here
to the whole sub-agent, not just one field.

═══════════════════════════════════════════════════════════════════════
DATA SOURCES CONFIRMED BEFORE WRITING ANY CODE (per this session's
explicit instruction not to assume) — "Postgres + graph metadata" per
plan §4 row 8, resolved concretely as THREE separate backends, none of
them `DataGateway`, none of them the Phase 0 tool wrappers:

1. `get_kb_stats()` (src/data_gateway/direct_backend.py) is CONFIRMED
   NOT REUSABLE for this sub-agent's "document coverage" metric group —
   it is GLOBAL (Chroma chunk counts by source filename, doc_type/
   ingested_at from Postgres), with no case_id filter anywhere in its own
   query. `documents` (Postgres, src/database/models.py::Document) DOES
   carry `case_id` (nullable — "pre-Phase-1 documents... have no case")
   directly on the row, so this module queries that table directly,
   filtered by `case_id`, rather than reusing or extending
   `get_kb_stats()`.
2. A CONFIRMED, REAL SCHEMA GAP, not guessed around: `ingestion_jobs`
   (src/database/models.py::IngestionJob) carries NO `case_id` column at
   all, and the one live document-upload path that writes it
   (`POST /api/admin/kb/upload`, src/api/admin.py) always ingests
   `is_global=True` — case-scoped `Document` rows are written by offline
   scripts calling `ingest_file(case_id=...)` directly (confirmed by
   grepping every call site), which never create an `IngestionJob` row.
   Consequence: "failed/quarantined count" (part of §7.3's "document
   coverage" row) CANNOT be attributed to a specific case anywhere in the
   current schema. This is reported honestly — `document_coverage`'s
   `counts` carries only what is actually case-attributable
   (`total_documents`, `by_doc_type`), and a fixed, always-present caveat
   names the gap rather than fabricating a zero or omitting the
   limitation silently. See `_STRUCTURAL_CAVEATS` below.
3. Graph metadata (entity counts, timeline readiness, identity health,
   conflict coverage) — NEW, dedicated, case-scoped Cypher templates,
   ALL routed through `src.graph.case_scope.scoped_cypher()` per design
   §4.5's own anticipation of exactly this need (the same instruction
   Timeline Building's own session followed for its two templates,
   AGENT_HARNESS_IMPLEMENTATION_PLAN.md §9 Phase 5 entry). Every query
   below uses a MANDATORY (not OPTIONAL) `MATCH` chain, per
   `conflict_detection.py`'s own documented live AGE bug (OPTIONAL MATCH
   directly followed by a mandatory MATCH is invalid openCypher/AGE) and
   Timeline Building's own precedent of sidestepping that whole bug class
   rather than reproducing it. Aggregation (`GROUP BY`-shaped queries,
   e.g. "count SAME_AS edges grouped by status") is deliberately AVOIDED
   in favor of one small query per discrete value (one per entity label,
   one per SAME_AS status) — AGE's aggregate/group-by support has no
   proven precedent anywhere in this codebase as of this session (grepped
   before writing), and given the ALREADY-DOCUMENTED history of live AGE
   Cypher-support surprises in this exact area (conflict_detection.py's
   OPTIONAL MATCH bug, its "columns must be passed explicitly" bug), this
   session does not introduce an unproven construct into new production
   code. Six small, boring, individually-testable queries are chosen over
   one clever one.
4. Embedding coverage — Chroma, via
   `ChromaVectorStore.get_all_metadata()` (the same call `get_kb_stats()`
   itself uses), filtered to this case's `metadata["case_id"]` in Python
   — `get_all_metadata()` takes no filter argument, so the narrowing
   happens the same way `get_kb_stats()`'s own `Counter(...)` grouping
   does, just filtered instead of grouped.

None of the above is `DataGateway`-mediated. `gateway`, accepted per the
`SubAgent` protocol, is unused — this sub-agent's own read-only backends
(Postgres, AGE, Chroma) are reached directly, the same posture Timeline
Building's own session took toward `graph_tool()`/`retrieve_graph()` (§9
Phase 5 entry: "there is nothing it could contribute here that these two
dedicated queries don't already cover more precisely").

═══════════════════════════════════════════════════════════════════════
READINESS RULE — see the full rationale in types.py's block comment
above `DataQualityReadiness`. Summary: each metric group has ONE defining
raw count (`_PRIMARY_COUNT_KEY` below); `UNAVAILABLE` iff that count is
exactly 0, `READY` iff it is > 0, `THIN` never constructed this session
(reserved for a future calibration pass), `UNKNOWN` iff the group's own
underlying query raised (never silently reported as `UNAVAILABLE` — that
would be the exact "checked vs. never-checked" false-all-clear defect
RESOLVED-5/the confidence_status split already exist to prevent
elsewhere in this contract).

Per-group failure isolation, decided and documented, not guessed: EACH of
the six groups is fetched independently and failures do not cascade —
one group's query raising does not prevent the other five from
computing and being reported. This is a deliberate departure from, say,
Investigative Analysis's "all failed -> ABSTAIN the whole sub-agent"
posture for its THREE composed tools: Data-Quality's entire purpose is
diagnostic ("explain why another agent's answer was thin"), so an
infrastructure hiccup in the graph store must not silence a still-healthy
Postgres-backed document-coverage reading, or vice versa. Only if ALL SIX
groups come back UNKNOWN does this sub-agent abstain at the whole-result
level (see `data_quality()` below) — reusing RESOLVED-4's "all attempted
sources failed -> ABSTAINED" convention (its origin case, Investigative
Analysis, §9 Phase 7 entry) for consistency of vocabulary across the
harness, even though nothing generated here needs "safe" withholding in
the hallucination sense that status exists for elsewhere.

═══════════════════════════════════════════════════════════════════════
`tools_used`/`degraded_from` — DECIDED, NOT LEFT IMPLICIT: both stay `[]`
UNCONDITIONALLY. `SourceTool` (types.py) is a closed
`Literal["RAG","GRAPH","GRAPH_HYBRID","XGRAPH","XAGG","XNETWORK","SQL","WEB"]`
— none of the eight primitives is composed here at all (unlike Timeline
Building, whose entire data source WAS the case graph and could
therefore honestly tag itself `"GRAPH"` as the retrieval MECHANISM class;
this sub-agent spans three heterogeneous backends — Postgres, AGE,
Chroma — and only one of the six groups is AGE-sourced). Tagging only the
graph-sourced groups as `"GRAPH"` while leaving the Postgres/Chroma
groups untagged would be inconsistent and misleading; RESOLVED-4's
`tools_used`/`degraded_from` rule is written for sub-agents composing the
eight tool primitives, which this one structurally does not. The actual,
correct granular degradation signal for THIS sub-agent is each
`DataQualityMetric.readiness`/`.error` — that is what a caller should
read, not `tools_used`/`degraded_from`.

═══════════════════════════════════════════════════════════════════════
NO ROLE GATE — case-scoped, same-role-as-everyone-else territory, exactly
like Case Summarization/Timeline Building before it, CONFIRMED not
assumed: none of §7.3's six metric groups touches a cross-case tool or a
cross-case Cypher template (design §2's role-gate rules are specific to
XGRAPH/XAGG/XNETWORK). `SubAgentStatus.DENIED` is therefore never built
as an output path here.

═══════════════════════════════════════════════════════════════════════
VALIDATION GATE — not wired, and (unlike every other sub-agent's
`# TODO(validation-gate)` marker) there is no insertion point to mark.
Same reasoning Timeline Building's own session already established (§9
Phase 5 entry, its own final "No other deviations" note): there is no
generated evidentiary text and no `verify_grounding()` call anywhere in
this sub-agent, so there is nothing for Validation's semantic
entailment check (plan §5/§7.1) to check claims against. Plan §5's own
text does not list Data-Quality among the "full semantic tier" sub-agents
(Cross-Case Linkage/Investigative Analysis/Report Drafting) — CONFIRMED
here as "structurally exempt, being non-generative," the same reasoning
already applied to Timeline Building's deterministic content, not merely
"not required this session."

═══════════════════════════════════════════════════════════════════════
CLASSIFICATION REACHABILITY — KNOWN, DOCUMENTED, NOT CLOSED THIS SESSION,
CONFIRMED (not assumed) by reading `supervisor.py`'s `_ROUTE_TO_SUBAGENT`
table before writing this file: no route maps to `DATA_QUALITY` — the
same gap Phase 1's own progress-log entry already named for this exact
sub-agent ("no route was ever built for 'how much evidence exists for
this case'... a NEW capability per plan §7.3, with no predecessor in
orchestrator.py at all"). This session does NOT invent new trigger
keywords/patterns to close that gap, per the same instruction Timeline
Building's own session followed — new classification logic needs the
same live-failure evidence basis XAGG/XGRAPH/XNETWORK's own deterministic
overrides had, not guessed patterns. This sub-agent is built, registered,
and tested via DIRECT DISPATCH and a Supervisor integration test that
BYPASSES `route_query()`'s actual classification (monkeypatching
`classify_to_subagent()`), the identical shape of bypass Timeline
Building's own integration test uses.

NOT IN SCOPE THIS SESSION: the Verifier/Validation modules themselves,
new classification/routing logic, live wiring into
main.py/orchestrator.py/router.py, the parked "auto-attach to another
sub-agent's degraded output" idea (§7.3: "valuable but adds coupling...
parked, not rejected" — routable-only for MVP).
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from typing import Optional

from sqlalchemy import select

from src.data_gateway.base import DataGateway
from src.database.models import Document
from src.database.postgres import get_session
from src.graph.case_scope import scoped_cypher
from src.pipeline.harness.supervisor import DATA_QUALITY, register
from src.pipeline.harness.types import (
    DataQualityMetric,
    DataQualityReadiness,
    OnEventCallback,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
    ToolError,
)
from src.retrieval.vector_store import ChromaVectorStore

logger = logging.getLogger(__name__)

# The graph node labels §7.3's own table names ("person/vehicle/phone/
# address/org/weapon"), translated to the REAL node-label vocabulary this
# codebase actually writes — CONFIRMED by reading
# src/graph/entity_resolution.py::TYPE_TO_LABEL,
# src/retrieval/graph_retriever.py::_SEED_LABELS, and
# src/ingestion/service.py's weapon-node write path before hardcoding this
# list, not copied from §7.3's prose verbatim. §7.3 says "phone"/"address"/
# "org"/"weapon"; the actual node labels are PhoneNumber/Address/
# Organization/Weapon.
_ENTITY_LABELS: tuple[str, ...] = (
    "Person",
    "Vehicle",
    "PhoneNumber",
    "Address",
    "Organization",
    "Weapon",
)

# The three literal SAME_AS.status values — CONFIRMED by reading
# src/graph/entity_resolution.py (writes "pending"),
# src/graph/community_detection.py (reads "confirmed"), and
# src/retrieval/graph_retriever.py's own module-docstring rule ("SAME_AS
# IDENTITY IS CONFIRMED-ONLY... status in {pending, confirmed, rejected}")
# before hardcoding this list.
_IDENTITY_STATUSES: tuple[str, ...] = ("pending", "confirmed", "rejected")

# Static per-group (name, label, explains, primary-count-key) table — the
# "Explains" column is §7.3's own text verbatim, not computed per call.
_GROUP_META: dict[str, tuple[str, str, str]] = {
    "document_coverage": ("Document coverage", "Thin Semantic Search results", "total_documents"),
    "entity_extraction": ("Entity extraction", "Sparse graph traversal", "total_entities"),
    "timeline_readiness": ("Timeline readiness", "Empty Timeline Building output", "dated_incidents"),
    "identity_health": ("Identity health", "Heavy caveats on cross-case links", "total_identity_matches"),
    "conflict_coverage": ("Conflict coverage", "UNKNOWN conflict states", "incidents_checkable"),
    "embedding_coverage": ("Embedding coverage", "Retrieval gaps", "chunks_embedded"),
}

# Fixed, always-present caveats naming known structural limitations —
# these are not per-call findings, they are true every time these two
# groups are computed at all, so they are attached unconditionally rather
# than only on error. Plain English, no raw exception text (matches
# `ToolError.message`'s own "operator-facing, not shown verbatim to the
# end user" posture — kept out of caveats, which per SubAgentResult.caveats
# ARE user-facing).
_STRUCTURAL_CAVEATS: tuple[str, ...] = (
    "Failed/quarantined document counts could not be attributed to this "
    "specific case — the ingestion tracking table does not record which "
    "case a failed upload belonged to.",
    "Conflict coverage reports how many incidents in this case are "
    "eligible for conflict checking and how many contradictions were "
    "found — it cannot distinguish 'checked, none found' from 'conflict "
    "detection was never run for this case.'",
)


# ═══════════════════════════════════════════════════════════════════════
# Per-group fetch functions. Each returns a small, already-aggregated
# dict[str, int] or raises — callers (the _run_metric wrapper below)
# translate a raise into DataQualityReadiness.UNKNOWN, never a crash.
# ═══════════════════════════════════════════════════════════════════════


async def _fetch_document_coverage(case_id: str) -> dict[str, int]:
    """
    Case-scoped document count by doc_type, from Postgres `documents`
    directly (NOT `get_kb_stats()` — see module docstring point 1).
    """
    async with get_session() as db:
        res = await db.execute(select(Document.doc_type).where(Document.case_id == case_id))
        doc_types = [row[0] or "unknown" for row in res.all()]

    counts: dict[str, int] = {"total_documents": len(doc_types)}
    for doc_type, n in Counter(doc_types).items():
        counts[f"by_type:{doc_type}"] = n
    return counts


async def _fetch_entity_extraction(case_id: str) -> dict[str, int]:
    """
    Case-scoped node count per entity label (see module docstring point 3
    for why this is six small queries, not one grouped one).
    """

    async def _count_label(label: str) -> int:
        rows = await scoped_cypher(
            f"""
            MATCH (n:{label})-[:BELONGS_TO_CASE]->(c:Case {{case_id: $case_id}})
            RETURN count(n) AS cnt
            """,
            case_id,
            columns=["cnt"],
        )
        return int(rows[0]["cnt"]) if rows else 0

    results = await asyncio.gather(*(_count_label(label) for label in _ENTITY_LABELS))
    counts = {f"by_label:{label}": n for label, n in zip(_ENTITY_LABELS, results)}
    counts["total_entities"] = sum(results)
    return counts


async def _fetch_timeline_readiness(case_id: str) -> dict[str, int]:
    """
    Count of Incident nodes carrying a live (non-superseded) OCCURRED_ON
    edge. Mirrors timeline_building.py's own `_fetch_dated_incidents()`
    MATCH shape (mandatory MATCH chain, same live-edge convention),
    projected to a count instead of full rows — this sub-agent needs "how
    many," not the events themselves, per §7.3's own "Count of dated
    incident events" wording.
    """
    rows = await scoped_cypher(
        """
        MATCH (i:Incident)-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id})
        MATCH (i)-[occ:OCCURRED_ON]->(d:Date)
        WHERE occ.superseded_by IS NULL
        RETURN count(DISTINCT i) AS cnt
        """,
        case_id,
        columns=["cnt"],
    )
    return {"dated_incidents": int(rows[0]["cnt"]) if rows else 0}


async def _fetch_identity_health(case_id: str) -> dict[str, int]:
    """
    Case-scoped SAME_AS edge count per status (pending/confirmed/
    rejected) — one query per status rather than a grouped aggregate, see
    module docstring point 3.
    """

    async def _count_status(status: str) -> int:
        rows = await scoped_cypher(
            """
            MATCH (a)-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id})
            MATCH (a)-[r:SAME_AS]-(b)
            WHERE r.status = $status AND r.superseded_by IS NULL
            RETURN count(DISTINCT r) AS cnt
            """,
            case_id,
            params={"status": status},
            columns=["cnt"],
        )
        return int(rows[0]["cnt"]) if rows else 0

    results = await asyncio.gather(*(_count_status(status) for status in _IDENTITY_STATUSES))
    counts = {status: n for status, n in zip(_IDENTITY_STATUSES, results)}
    counts["total_identity_matches"] = sum(results)
    return counts


async def _fetch_conflict_coverage(case_id: str) -> dict[str, int]:
    """
    Two counts: how many incidents in this case are even ELIGIBLE for
    conflict checking (mirrors `_fetch_timeline_readiness`'s own query —
    re-fetched independently here, not shared, so this group's failure
    isolation does not depend on timeline_readiness's own success), and
    how many live CONFLICTS_WITH edges actually exist (mirrors
    timeline_building.py's own `_fetch_conflict_bases()` MATCH shape,
    projected to a count). `incidents_checkable`, not `conflicts_found`,
    is this group's PRIMARY/readiness-defining count — see module
    docstring: zero conflicts found is a normal, healthy outcome, not a
    sign of missing coverage; zero checkable incidents is.
    """
    incidents_row, conflicts_row = await asyncio.gather(
        scoped_cypher(
            """
            MATCH (i:Incident)-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id})
            MATCH (i)-[occ:OCCURRED_ON]->(d:Date)
            WHERE occ.superseded_by IS NULL
            RETURN count(DISTINCT i) AS cnt
            """,
            case_id,
            columns=["cnt"],
        ),
        scoped_cypher(
            """
            MATCH (a:Incident)-[r:CONFLICTS_WITH]->(b:Incident)-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id})
            WHERE r.superseded_by IS NULL
            RETURN count(DISTINCT r) AS cnt
            """,
            case_id,
            columns=["cnt"],
        ),
    )
    return {
        "incidents_checkable": int(incidents_row[0]["cnt"]) if incidents_row else 0,
        "conflicts_found": int(conflicts_row[0]["cnt"]) if conflicts_row else 0,
    }


async def _fetch_embedding_coverage(case_id: str) -> dict[str, int]:
    """
    Chunks embedded (Chroma, filtered by metadata.case_id in Python —
    `get_all_metadata()` takes no filter argument, same as `get_kb_stats()`
    itself) vs. documents ingested for this case (Postgres, reused from a
    fresh count rather than sharing document_coverage's own result, for
    the same independent-failure-isolation reason `conflict_coverage`
    re-fetches its own incident count rather than sharing
    `timeline_readiness`'s).
    """
    store = ChromaVectorStore.get_instance()
    all_metadata = await asyncio.to_thread(store.get_all_metadata)
    chunks_embedded = sum(1 for m in all_metadata if m.get("case_id") == case_id)

    async with get_session() as db:
        res = await db.execute(select(Document.doc_id).where(Document.case_id == case_id))
        documents_ingested = len(res.all())

    return {"chunks_embedded": chunks_embedded, "documents_ingested": documents_ingested}


_FETCHERS = {
    "document_coverage": _fetch_document_coverage,
    "entity_extraction": _fetch_entity_extraction,
    "timeline_readiness": _fetch_timeline_readiness,
    "identity_health": _fetch_identity_health,
    "conflict_coverage": _fetch_conflict_coverage,
    "embedding_coverage": _fetch_embedding_coverage,
}


async def _run_metric(name: str, case_id: str) -> DataQualityMetric:
    """
    Runs one metric group's fetch, translating an exception into
    `DataQualityReadiness.UNKNOWN` (never a crash, never silently
    reported as UNAVAILABLE — see the readiness rule in module docstring
    and types.py's own block comment above `DataQualityReadiness`).
    """
    label, explains, primary_key = _GROUP_META[name]
    try:
        counts = await _FETCHERS[name](case_id)
    except Exception as exc:
        logger.error("Data-Quality: %s fetch failed for case %s: %s", name, case_id, exc)
        return DataQualityMetric(
            name=name,
            label=label,
            readiness=DataQualityReadiness.UNKNOWN,
            explains=explains,
            error=str(exc),
        )

    primary = counts.get(primary_key, 0)
    readiness = DataQualityReadiness.READY if primary > 0 else DataQualityReadiness.UNAVAILABLE
    return DataQualityMetric(name=name, label=label, readiness=readiness, counts=counts, explains=explains)


def _answer_text(case_id: str, metrics: list[DataQualityMetric]) -> str:
    """
    Deterministic, count-derived summary — see module docstring's "WHAT
    THIS SUB-AGENT IS" section for why this needs no `verify_grounding()`
    call. Built via plain string formatting over already-computed
    `metrics`, never model-generated.
    """
    lines = [f"{m.label}: {m.readiness.value}" for m in metrics]
    return f"Data-quality snapshot for case {case_id} — " + "; ".join(lines) + "."


async def data_quality(
    agent_input: SubAgentInput,
    *,
    on_event: Optional[OnEventCallback] = None,
    gateway: Optional[DataGateway] = None,
) -> SubAgentResult:
    """
    The Data-Quality/Extraction-Coverage sub-agent. See module docstring
    for the full contract.

    `on_event`/`gateway` are accepted for `SubAgent` protocol conformance
    and otherwise ignored — this sub-agent's data sources are none of the
    eight `SourceTool` primitives (nothing per-source-tool to report via
    `on_event`) and none of it is `DataGateway`-mediated (see module
    docstring's "DATA SOURCES" section).
    """
    case_id = agent_input.execution.caller.active_case_id

    if not case_id:
        # Mirrors Timeline Building's identical precedent: `scoped_cypher()`
        # itself refuses an empty/None case_id, and every metric group here
        # is within-case, so a query with no active case has nothing to
        # scope stats to — a legitimate EMPTY, not an infrastructure
        # failure.
        return SubAgentResult(
            status=SubAgentStatus.EMPTY,
            caveats=["No active case was specified; data-quality metrics require a case to scope to."],
        )

    metrics = list(
        await asyncio.gather(*(_run_metric(name, case_id) for name in _GROUP_META))
    )

    unknown_names = [m.label for m in metrics if m.readiness == DataQualityReadiness.UNKNOWN]

    if len(unknown_names) == len(metrics):
        # All six groups' own queries raised -- nothing could be computed
        # at all. Reuses RESOLVED-4's "all attempted sources failed ->
        # ABSTAINED" vocabulary (its origin case, Investigative Analysis)
        # for consistency across the harness -- see module docstring.
        return SubAgentResult(
            status=SubAgentStatus.ABSTAINED,
            error=ToolError(
                kind="upstream_failure",
                message="All six data-quality metric groups failed to compute.",
            ),
            caveats=["Data-quality metrics could not be computed for this case."],
        )

    status = SubAgentStatus.PARTIAL if unknown_names else SubAgentStatus.OK
    caveats = list(_STRUCTURAL_CAVEATS)
    if unknown_names:
        caveats.append(
            "Could not be computed for this case: " + ", ".join(sorted(unknown_names)) + "."
        )

    return SubAgentResult(
        status=status,
        answer_text=_answer_text(case_id, metrics),
        metrics=metrics,
        caveats=caveats,
    )


data_quality.name = DATA_QUALITY

# Import-time self-registration -- the same pattern every prior sub-agent
# module used. Per module docstring's "CLASSIFICATION REACHABILITY"
# section, `Supervisor.handle()`'s real `route_query()` classification
# path cannot reach this name yet -- registration only makes it
# dispatchable via a route_result whose classify_to_subagent() output is
# DATA_QUALITY, which nothing produces today outside of a test forcing it
# directly.
register(data_quality)
