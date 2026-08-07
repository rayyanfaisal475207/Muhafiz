"""
Stub implementations of the seven tool primitives.

Every stub returns a realistic-but-fake result matching its exact schema in
SUBAGENT_INTERFACES.md §1. None of them touches `src.retrieval`, `src.graph`,
the database, the network, or an LLM — the harness is buildable and testable in
complete isolation from the live pipeline, by design.

What is REAL here, and must stay real when the stubs are replaced:
  * the return TYPES and their field semantics
  * `fallback_to_rag` polarity per tool (bool on GRAPH/SQL/WEB, pinned False on
    the cross-case tools via `CrossCaseToolResult`)
  * `metadata.source_tool` on every emitted chunk
  * the cross-case role gate ORDERING: check → audit → only then proceed
    ([PRESERVE — design §2.3, §4.3])

What is fake: the evidence bodies, the scores, and the dispatch heuristics.

Each stub takes an optional `EventRecorder` so tool-level transitions appear in
the trace from day one, rather than being retrofitted later.
"""
from __future__ import annotations

from typing import Any, Optional

from src.pipeline.harness.contracts import (
    CROSS_CASE_ROLES,
    CrossCaseToolInput,
    GraphToolInput,
    GraphToolResult,
    RagToolInput,
    RagToolResult,
    SqlToolInput,
    SqlToolResult,
    ToolError,
    ToolStatus,
    WebToolInput,
    WebToolResult,
    XAggToolInput,
    XAggToolResult,
    XGraphToolInput,
    XGraphToolResult,
    XNetworkToolInput,
    XNetworkToolResult,
)
from src.pipeline.harness.events import EventRecorder
from src.pipeline.harness.tools import _fixtures

# Query substrings that make a stub return EMPTY / FAIL, so tests can drive
# every branch deterministically without monkeypatching internals.
_EMPTY_TRIGGER = "__empty__"
_FAIL_TRIGGER = "__fail__"


async def _audit_denial(gateway: Any, tool: str, tool_input: CrossCaseToolInput) -> None:
    """
    Write the authorization-violation record for a refused cross-case call.

    [PRESERVE — design §4.3] This runs BEFORE anything else that touches
    cross-case scope, and the caller returns DENIED immediately after. Cross-
    case / RLS-bypass scope is armed ONLY on the success path — never before
    the role check, which is the fix for a documented historical bug.
    """
    if gateway is None:
        return
    await gateway.log_audit_event(
        event_type="authorization_violation",
        user_id=tool_input.caller.user_id,
        case_id=None,
        details={
            "query": tool_input.query_text,
            "role": tool_input.caller.role.value,
            "route": tool,
        },
    )


async def _check_cross_case_role(
    tool: str, tool_input: CrossCaseToolInput, gateway: Any, events: Optional[EventRecorder],
) -> Optional[ToolError]:
    """
    Step 1 + 2 of the cross-case ordering contract. Returns a ToolError when the
    caller is refused, None when the call may proceed.
    """
    if tool_input.caller.role in CROSS_CASE_ROLES:
        return None
    await _audit_denial(gateway, tool, tool_input)
    if events:
        await events.emit(
            f"tool:{tool.lower()}", "error",
            f"{tool} refused: cross-case access requires supervisor role or higher",
        )
    return ToolError(
        kind="permission_denied",
        message=f"Cross-case {tool} queries require supervisor role or higher.",
    )


# ── RAG ──────────────────────────────────────────────────────────────────

async def rag_tool(
    tool_input: RagToolInput, events: Optional[EventRecorder] = None,
) -> RagToolResult:
    """
    Stub RAG retrieval. Returns deliberately verbose, redundant, near-duplicate
    chunks (see `_fixtures`) so summarization is stress-tested now.

    [PRESERVE — design §2.1] No role gate; `fallback_to_rag` pinned False — RAG
    is the fallback TARGET, it has no onward fallback.
    """
    if events:
        await events.emit("tool:rag", "active", "Searching case documents")

    if _FAIL_TRIGGER in tool_input.query_text:
        if events:
            await events.emit("tool:rag", "error", "Document search failed")
        return RagToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message="Stub RAG failure."),
            retries_used=1,
            evaluator_verdict="not_relevant",
        )

    if _EMPTY_TRIGGER in tool_input.query_text:
        if events:
            await events.emit("tool:rag", "done", "Document search returned no relevant passages")
        return RagToolResult(
            status=ToolStatus.EMPTY, retries_used=1, evaluator_verdict="not_relevant"
        )

    chunks = _fixtures.rag_chunks()
    if tool_input.top_k is not None:
        chunks = chunks[: tool_input.top_k]
    if events:
        await events.emit("tool:rag", "done", f"Retrieved {len(chunks)} passages")
    return RagToolResult(
        status=ToolStatus.OK, chunks=chunks, retries_used=0, evaluator_verdict="relevant"
    )


# ── GRAPH / GRAPH_HYBRID ─────────────────────────────────────────────────

async def graph_tool(
    tool_input: GraphToolInput, events: Optional[EventRecorder] = None,
) -> GraphToolResult:
    """
    Stub within-case graph traversal.

    [RESOLVED-1a] With `hybrid=True`, emitted chunks carry
    `source_tool="GRAPH_HYBRID"` — structurally distinguishable from plain
    GRAPH, not merely differently scored.

    [PRESERVE — design §2.2] Sets `fallback_to_rag=True` on failure or empty
    result. The tool does NOT perform the fallback; it reports one is warranted.
    """
    label = "GRAPH_HYBRID" if tool_input.hybrid else "GRAPH"
    step = "tool:graph_hybrid" if tool_input.hybrid else "tool:graph"
    if events:
        await events.emit(step, "active", f"Traversing case graph ({label})")

    if _FAIL_TRIGGER in tool_input.query_text:
        if events:
            await events.emit(step, "retry", f"{label} failed — falling back to document search")
        return GraphToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=f"Stub {label} failure."),
            fallback_to_rag=True,
        )

    if _EMPTY_TRIGGER in tool_input.query_text:
        if events:
            await events.emit(step, "retry", f"{label} found nothing — falling back to document search")
        return GraphToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True)

    chunks = _fixtures.graph_chunks(hybrid=tool_input.hybrid)
    if events:
        await events.emit(step, "done", f"{label} returned {len(chunks)} linked items")
    return GraphToolResult(
        status=ToolStatus.OK,
        chunks=chunks,
        fallback_to_rag=False,
        hop_count=min(tool_input.max_hops, 2),
        chain_confidence=0.34,
        seed_entities=[{"entity_id": "VEH-0091", "type": "Vehicle", "name": "silver pickup"}],
        conflicts_included=False,
    )


# ── XGRAPH ───────────────────────────────────────────────────────────────

async def xgraph_tool(
    tool_input: XGraphToolInput, gateway: Any = None, events: Optional[EventRecorder] = None,
) -> XGraphToolResult:
    """
    Stub cross-case entity traversal.

    [PRESERVE — design §2.3] Role gate first, audit on denial, scope armed only
    after. Never falls back to RAG — `fallback_to_rag` is pinned False by
    `CrossCaseToolResult`.
    """
    denial = await _check_cross_case_role("XGRAPH", tool_input, gateway, events)
    if denial:
        return XGraphToolResult(status=ToolStatus.DENIED, error=denial)

    if events:
        await events.emit("tool:xgraph", "active", "Searching across cases")

    if _EMPTY_TRIGGER in tool_input.query_text:
        if events:
            await events.emit("tool:xgraph", "done", "No cross-case connections found")
        return XGraphToolResult(status=ToolStatus.EMPTY)

    chunks = _fixtures.xgraph_chunks()
    case_ids = sorted({c.metadata.case_id for c in chunks if c.metadata.case_id})
    if events:
        await events.emit("tool:xgraph", "done", f"Found connections across {len(case_ids)} cases")
    return XGraphToolResult(
        status=ToolStatus.OK,
        chunks=chunks,
        case_ids_touched=case_ids,
        unconfirmed_links=[
            {"from": "VEH-0091", "to": "VEH-0204", "basis": "name match, unverified"}
        ],
        hop_count=2,
        chain_confidence=0.55,
    )


# ── XAGG ─────────────────────────────────────────────────────────────────

async def xagg_tool(
    tool_input: XAggToolInput, gateway: Any = None, events: Optional[EventRecorder] = None,
) -> XAggToolResult:
    """
    Stub cross-case aggregate.

    [PRESERVE — design §2.4] `raw_summary_text` is populated so the
    Verifier-rejection path can serve the deterministic aggregate rather than a
    generic abstention — the aggregate is correct by construction.
    """
    denial = await _check_cross_case_role("XAGG", tool_input, gateway, events)
    if denial:
        return XAggToolResult(status=ToolStatus.DENIED, error=denial)

    if events:
        await events.emit("tool:xagg", "active", "Computing cross-case aggregate")

    rows = _fixtures.xagg_rows()
    raw = "; ".join(f"{r['key']}: {r['count']} cases" for r in rows)
    if events:
        await events.emit("tool:xagg", "done", f"Aggregated {len(rows)} recurring entities")
    return XAggToolResult(
        status=ToolStatus.OK,
        aggregate_kind="graph_recurrence",
        raw_summary_text=raw,
        case_ids_touched=["CASE-A1B2C3D4", "CASE-9F8E7D6C", "CASE-5B4A3C2D"],
    )


# ── XNETWORK ─────────────────────────────────────────────────────────────

async def xnetwork_tool(
    tool_input: XNetworkToolInput, gateway: Any = None, events: Optional[EventRecorder] = None,
) -> XNetworkToolResult:
    """
    Stub cross-case thematic synthesis over precomputed community summaries.

    [PRESERVE — design §2.5] Never falls back to RAG. `raw_summary_text` backs
    the XNETWORK-specific final fallback after its one forced-cloud
    regeneration retry — that retry lives in the sub-agent/generation layer, not
    here.
    """
    denial = await _check_cross_case_role("XNETWORK", tool_input, gateway, events)
    if denial:
        return XNetworkToolResult(status=ToolStatus.DENIED, error=denial)

    if events:
        await events.emit("tool:xnetwork", "active", "Synthesizing cross-case patterns")

    chunks = _fixtures.xnetwork_chunks()[: tool_input.top_k]
    if events:
        await events.emit("tool:xnetwork", "done", f"Retrieved {len(chunks)} community summaries")
    return XNetworkToolResult(
        status=ToolStatus.OK,
        chunks=chunks,
        community_ids=["C-014"],
        raw_summary_text=chunks[0].text if chunks else None,
        case_ids_touched=["CASE-A1B2C3D4", "CASE-9F8E7D6C", "CASE-5B4A3C2D"],
    )


# ── SQL ──────────────────────────────────────────────────────────────────

async def sql_tool(
    tool_input: SqlToolInput, events: Optional[EventRecorder] = None,
) -> SqlToolResult:
    """
    Stub structured reference lookup.

    [PRESERVE — design §2.6] Reference data, not case evidence: emitted chunks
    carry `case_id=None`, which correctly makes them inert to the Verifier's
    leakage check. `fallback_to_rag=True` on empty rows or any exception.
    """
    if events:
        await events.emit("tool:sql", "active", "Looking up penal-code reference data")

    if _EMPTY_TRIGGER in tool_input.query_text or _FAIL_TRIGGER in tool_input.query_text:
        if events:
            await events.emit("tool:sql", "retry", "No reference rows — falling back to document search")
        return SqlToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True, row_count=0)

    rows = _fixtures.sql_rows()
    from src.pipeline.harness.contracts import ChunkMetadata, EvidenceChunk

    chunks = [
        EvidenceChunk(
            id=f"chunk-sql-{i:03d}",
            text=f"{r['section_ref']} — {r['subject']} ({r['category']}). "
                 f"Cognizable: {'yes' if r['cognizable'] else 'no'}.",
            score=1.0,
            metadata=ChunkMetadata(
                source_tool="SQL", case_id=None, source_file="police_reference_data"
            ),
        )
        for i, r in enumerate(rows, start=1)
    ]
    if events:
        await events.emit("tool:sql", "done", f"Found {len(rows)} reference rows")
    return SqlToolResult(
        status=ToolStatus.OK,
        chunks=chunks,
        row_count=len(rows),
        extracted_params={"category": "theft", "subject": None, "section_ref": None},
    )


# ── WEB ──────────────────────────────────────────────────────────────────

async def web_tool(
    tool_input: WebToolInput, events: Optional[EventRecorder] = None,
    air_gap_mode: bool = False,
) -> WebToolResult:
    """
    Stub guarded external search.

    [PRESERVE — design §2.7] `air_gap_mode` disables the tool entirely BEFORE
    either provider tier is reached — the check is first, not per-provider,
    because a wrapper that checks once at the top and then calls through to a
    second provider on failure has silently reopened outbound egress.
    Web results are NEVER case evidence: chunks carry `case_id=None`.
    """
    if air_gap_mode:
        if events:
            await events.emit("tool:web", "skipped", "Web search disabled under air-gap mode")
        return WebToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message="Web search disabled (AIR_GAP_MODE)."),
            fallback_to_rag=True,
        )

    if events:
        await events.emit("tool:web", "active", "Searching allowlisted external sources")

    if _EMPTY_TRIGGER in tool_input.query_text or _FAIL_TRIGGER in tool_input.query_text:
        if events:
            await events.emit("tool:web", "retry", "Both search tiers failed — falling back")
        return WebToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True)

    from src.pipeline.harness.contracts import ChunkMetadata, EvidenceChunk

    results = _fixtures.web_results()[: tool_input.max_results]
    chunks = [
        EvidenceChunk(
            id=f"chunk-web-{i:03d}",
            text=f"{r['title']} — {r['content']}",
            score=r["score"],
            metadata=ChunkMetadata(source_tool="WEB", case_id=None, source_file=r["url"]),
        )
        for i, r in enumerate(results, start=1)
    ]
    if events:
        await events.emit("tool:web", "done", f"Retrieved {len(chunks)} external results")
    return WebToolResult(
        status=ToolStatus.OK, chunks=chunks, provider_used="primary_search", fallback_to_rag=False
    )
