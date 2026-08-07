"""
XGRAPH tool — src/pipeline/harness/tools/xgraph.py (Phase 0, foundation
layer).

Thin adapter over `retrieve_graph(..., cross_case=True, ...)` — the SAME
function GRAPH wraps (src/pipeline/harness/tools/graph.py), different flag
(AGENT_HARNESS_DESIGN.md §2.3). No new logic: this module's only job is to
translate `retrieve_graph()`'s return shape / raised `PermissionError` into
the standard `ToolResult` discriminated-status shape.

[PRESERVE — design §2.3] NEVER falls back to RAG — cross-case evidence
must never blend into a case-scoped RAG stream. `XGraphToolResult`
inherits `fallback_to_rag: Literal[False]` from `CrossCaseToolResult`
(types.py), so this is enforced at the type level, not just by omission
here.

[PRESERVE — design §2.3, §4.3, SUBAGENT_INTERFACES.md §1.1]
Role-gate-before-scope-arming ordering is entirely `retrieve_graph()`'s
own responsibility (see graph_retriever.py: role check -> audit log ->
ONLY THEN arm `current_cross_case`/`current_rls_active`) — this wrapper
must not "help" by checking the role itself first or resolving scope
before calling `retrieve_graph()`. Doing either would let an unauthorized
caller's request start touching cross-case machinery before
`retrieve_graph()`'s own gate ever runs, which is exactly the historical
bug (§4.3) this ordering exists to prevent. This wrapper's ONLY job on the
authorization axis is to catch the `PermissionError` `retrieve_graph()`
raises on denial and translate it to `status=DENIED` — it adds no
authorization logic of its own.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import Field

from src.pipeline.harness.types import (
    ChunkMetadata,
    CrossCaseToolInput,
    CrossCaseToolResult,
    EvidenceChunk,
    ToolError,
    ToolStatus,
)
from src.retrieval.graph_retriever import DEFAULT_HOPS, MAX_HOPS, retrieve_graph

logger = logging.getLogger(__name__)


class XGraphToolInput(CrossCaseToolInput):
    """
    Cross-case traversal for entity connections and recurrence.

    Covers both "find connections for this named entity" (a literal name/
    CNIC/phone/plate appears in the query) and "has any entity of type X
    recurred across cases" (no literal name, but a typed entity is still
    the answer shape — `target_entity=None`). See
    SUBAGENT_INTERFACES.md §1.4 for the full XGRAPH-vs-XNETWORK dispatch
    rule (owned by the Cross-Case Linkage sub-agent, not this tool).
    """

    target_entity: Optional[str] = Field(
        default=None,
        description=(
            "Named entity to trace, if the query names one. None means "
            "typed-entity recurrence discovery — still XGRAPH."
        ),
    )
    max_hops: int = Field(default=DEFAULT_HOPS, ge=1, le=MAX_HOPS)


class XGraphToolResult(CrossCaseToolResult):
    """
    [PRESERVE — design §2.3] Never falls back to RAG (inherited, pinned
    False). An empty result with no unconfirmed links is a definite "no
    connections found" answer — status=EMPTY, and the caller must present
    it as a real finding, not degrade to a case-scoped search. A result
    can be status=EMPTY on chunks while still carrying unconfirmed links
    worth reporting.
    """

    unconfirmed_links: list[dict[str, Any]] = Field(
        default_factory=list,
        description=(
            "Pending, unconfirmed identity links. [PRESERVE — design §3] "
            "These are CAVEATS and must be surfaced as such — never "
            "asserted as confirmed fact."
        ),
    )
    hop_count: int = 0
    chain_confidence: Optional[float] = None


def _to_evidence_chunk(raw: dict) -> EvidenceChunk:
    meta = dict(raw.get("metadata") or {})
    extra = {k: v for k, v in meta.items() if k not in ("case_id", "source")}
    graph_confidence = raw.get("graph_confidence")
    confidence_kwargs: dict = {}
    if graph_confidence is not None:
        confidence_kwargs = {"confidence_status": "computed", "confidence": float(graph_confidence)}
    return EvidenceChunk(
        id=raw["id"],
        text=raw.get("text", ""),
        score=raw.get("rrf_score"),
        metadata=ChunkMetadata(
            source_tool="XGRAPH",
            case_id=meta.get("case_id"),
            source_file=meta.get("source"),
            **confidence_kwargs,
            **extra,
        ),
    )


async def xgraph_tool(tool_input: XGraphToolInput) -> XGraphToolResult:
    """The XGRAPH primitive: cross-case entity traversal, never falls back to RAG."""
    caller = tool_input.caller
    try:
        graph_result = await retrieve_graph(
            tool_input.query_text,
            tool_input.target_entity,
            case_id=None,
            cross_case=True,
            max_hops=tool_input.max_hops,
            user_id=caller.user_id,
            user_role=caller.role.value,
        )
    except PermissionError as exc:
        # retrieve_graph() has already written the authorization_violation
        # audit record (see graph_retriever.py) — this wrapper only
        # translates the outcome, it does not re-derive or duplicate it.
        return XGraphToolResult(
            status=ToolStatus.DENIED,
            error=ToolError(kind="permission_denied", message=str(exc)),
        )
    except Exception as exc:
        logger.error("XGRAPH tool: cross-case traversal failed: %s", exc)
        return XGraphToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
        )

    chunks = graph_result["chunks"]
    case_ids_touched = sorted({
        (c.get("metadata") or {}).get("case_id")
        for c in chunks if (c.get("metadata") or {}).get("case_id")
    })

    return XGraphToolResult(
        status=ToolStatus.OK if chunks else ToolStatus.EMPTY,
        chunks=[_to_evidence_chunk(c) for c in chunks],
        case_ids_touched=case_ids_touched,
        unconfirmed_links=graph_result["unconfirmed_links"],
        hop_count=graph_result["hop_count"],
        chain_confidence=graph_result["compounded_confidence"],
    )


xgraph_tool.name = "XGRAPH"
