"""
GRAPH / GRAPH_HYBRID tool — src/pipeline/harness/tools/graph.py (Phase 0,
foundation layer).

Thin adapter over `retrieve_graph(query_text, target_entity, case_id,
cross_case=False, max_hops, user_id, user_role)` (AGENT_HARNESS_DESIGN.md
§2.2) plus the same cross-rerank + evaluator gate orchestrator.py's
GRAPH/GRAPH_HYBRID routes already run before generation. No new
retrieval/generation logic — every call below is the same function,
called the same way, orchestrator.py already calls.

[PRESERVE — design §2.2] `fallback_to_rag=True` on: GRAPH finding neither
entity-linked evidence nor conflict chunks, GRAPH_HYBRID producing no
combined result, or an evaluator "not relevant" verdict on the (graph or
graph+hybrid) chunks. All three degrade to RAG today; the tool does not
perform the fallback itself, it reports one is warranted (`ToolResult.
fallback_to_rag`) and the calling sub-agent acts on it.

[PRESERVE — design §2.2] Within-case only at this call shape
(`cross_case=False`, always, regardless of `hybrid`). Cross-case traversal
is XGRAPH (tools/xgraph.py) — a separate tool with its own role gate, not
a flag on this one. No role gate beyond the existing case-assignment
check: any role that can see the case can traverse its graph.

[PRESERVE — design §2.2, SUBAGENT_INTERFACES.md §1.3 RESOLVED-1]
GRAPH_HYBRID (`hybrid=True`) is graph discovery fused with RAG retrieval —
composed from the GRAPH call above plus
`src.pipeline.harness.tools.rag._retrieve_candidates()` (the same
query-expansion/cross-script-variant/vector-search/BM25-pool steps RAG
itself uses), RRF-merged, NOT a third, independently re-implemented
retrieval mechanism. This is the "avoid the current duplication" fix
RESOLVED-1 calls for.

[RESOLVED-1a] GRAPH_HYBRID is user-visible, not an implementation detail:
every chunk emitted with `hybrid=True` carries
`metadata.source_tool="GRAPH_HYBRID"` — including chunks that came from
the vector-search leg, not just the graph leg — never folded into "GRAPH"
for display. See `types.SOURCE_TOOL_DISPLAY_LABELS`.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import Field

from src import config
from src.pipeline.evaluator import evaluate_relevance
from src.pipeline.harness.tools.rag import _build_where, _retrieve_candidates
from src.pipeline.harness.types import (
    ChunkMetadata,
    EvidenceChunk,
    SourceTool,
    ToolInput,
    ToolResult,
    ToolStatus,
)
from src.retrieval.cross_reranker import cross_rerank
from src.retrieval.graph_retriever import DEFAULT_HOPS, MAX_HOPS, retrieve_graph
from src.retrieval.reranker import rerank_results

logger = logging.getLogger(__name__)


class GraphToolInput(ToolInput):
    """
    Within-case traversal of the evidence graph from an entity seed.

    [PRESERVE — design §2.2] Within-case only. Cross-case traversal is
    XGRAPH — a separate tool with a role gate, NOT a flag on this one.
    """

    target_entity: Optional[str] = Field(
        default=None,
        description=(
            "Entity seed. None is valid and meaningful: it means case-wide "
            "enumeration ('who are the witnesses in this case') rather than "
            "single-entity lookup."
        ),
    )
    max_hops: int = Field(default=DEFAULT_HOPS, ge=1, le=MAX_HOPS, description="Traversal depth cap.")
    hybrid: bool = Field(
        default=False,
        description=(
            "False -> GRAPH. True -> GRAPH_HYBRID: graph discovery fused "
            "with RAG retrieval. Stays a flag, not a separate tool — fusion "
            "mechanics are retrieval-internal, but the FACT that hybrid ran "
            "is user-visible (see module docstring, RESOLVED-1a)."
        ),
    )


class GraphToolResult(ToolResult):
    """
    [PRESERVE — design §2.2] Sets `fallback_to_rag=True` when traversal
    fails or yields nothing usable — specifically: GRAPH finding neither
    evidence chunks nor conflict records, GRAPH_HYBRID producing no
    combined result, or the relevance evaluator rejecting the graph
    evidence. All three degrade to RAG today and must continue to.
    """

    hop_count: int = Field(default=0, description="Deepest hop actually reached.")
    chain_confidence: Optional[float] = Field(
        default=None,
        description=(
            "Weakest link across returned chains. [PRESERVE] Drives the "
            "Verifier's hedging requirement — low confidence obliges hedged "
            "phrasing in the answer."
        ),
    )
    seed_entities: list[dict[str, Any]] = Field(
        default_factory=list, description="Entities traversal started from. Observability."
    )
    conflicts_included: bool = Field(
        default=False,
        description=(
            "Whether detected within-case conflicts contributed chunks. "
            "Conflict records can be the ONLY result when no entity seed "
            "matched — that is status=OK, not EMPTY."
        ),
    )


def _to_evidence_chunk(raw: dict, source_tool: SourceTool) -> EvidenceChunk:
    meta = dict(raw.get("metadata") or {})
    extra = {k: v for k, v in meta.items() if k not in ("case_id", "source", "conflict_basis")}
    if raw.get("conflict_basis") is not None:
        extra.setdefault("conflict_basis", raw["conflict_basis"])

    graph_confidence = raw.get("graph_confidence")
    confidence_kwargs: dict = {}
    if graph_confidence is not None:
        confidence_kwargs = {"confidence_status": "computed", "confidence": float(graph_confidence)}

    return EvidenceChunk(
        id=raw["id"],
        text=raw.get("text", ""),
        score=raw.get("rerank_score", raw.get("rrf_score")),
        metadata=ChunkMetadata(
            source_tool=source_tool,
            case_id=meta.get("case_id"),
            source_file=meta.get("source"),
            **confidence_kwargs,
            **extra,
        ),
    )


def _conflicts_included(chunks: list[dict]) -> bool:
    """A chunk tagged via_entity='conflict_detection' came from
    `_fetch_case_conflicts()`, not entity traversal — see graph_retriever.py."""
    return any(c.get("via_entity") == "conflict_detection" for c in chunks)


async def graph_tool(tool_input: GraphToolInput) -> GraphToolResult:
    """The GRAPH / GRAPH_HYBRID primitive."""
    caller = tool_input.execution.caller
    source_tool: SourceTool = "GRAPH_HYBRID" if tool_input.hybrid else "GRAPH"

    if not tool_input.hybrid:
        graph_result = await retrieve_graph(
            tool_input.query_text,
            tool_input.target_entity,
            caller.active_case_id,
            cross_case=False,
            max_hops=tool_input.max_hops,
            user_id=caller.user_id,
            user_role=caller.role.value,
        )
        chunks = graph_result["chunks"]
        if not chunks:
            return GraphToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True)
        candidate_pool = chunks
    else:
        where = _build_where(caller, include_global=True)
        top_k = config.TOP_K_RETRIEVAL
        is_cross_case = not caller.active_case_id
        fetch_top_k = top_k * config.CROSS_CASE_RETRIEVAL_MULTIPLIER if is_cross_case else top_k

        import asyncio

        graph_result, (vector_results, bm25_results) = await asyncio.gather(
            retrieve_graph(
                tool_input.query_text,
                tool_input.target_entity,
                caller.active_case_id,
                cross_case=False,
                max_hops=tool_input.max_hops,
                user_id=caller.user_id,
                user_role=caller.role.value,
            ),
            _retrieve_candidates(tool_input.query_text, where, fetch_top_k, top_k, is_cross_case),
        )

        combined_semantic = vector_results + graph_result["chunks"]
        if not combined_semantic:
            return GraphToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True)

        candidate_pool = rerank_results(combined_semantic, bm25_results, top_k=top_k)

    try:
        reranked = await cross_rerank(tool_input.query_text, candidate_pool, top_k=config.TOP_K_RERANK)
    except Exception as exc:
        logger.error("%s tool: cross-encoder rerank failed: %s. Falling back to unranked order.", source_tool, exc)
        reranked = candidate_pool[: config.TOP_K_RERANK]

    try:
        evaluation = await evaluate_relevance(tool_input.query_text, tool_input.query_text, reranked)
    except Exception as exc:
        logger.error("%s tool: evaluator failed: %s", source_tool, exc)
        evaluation = {"relevant": True, "reason": "Evaluator failed, proceeding"}

    if not evaluation.get("relevant", False):
        return GraphToolResult(
            status=ToolStatus.EMPTY,
            fallback_to_rag=True,
            hop_count=graph_result["hop_count"],
            chain_confidence=graph_result["compounded_confidence"],
            seed_entities=graph_result["seed_entities"],
            conflicts_included=_conflicts_included(graph_result["chunks"]),
        )

    return GraphToolResult(
        status=ToolStatus.OK,
        chunks=[_to_evidence_chunk(c, source_tool) for c in reranked],
        fallback_to_rag=False,
        hop_count=graph_result["hop_count"],
        chain_confidence=graph_result["compounded_confidence"],
        seed_entities=graph_result["seed_entities"],
        conflicts_included=_conflicts_included(graph_result["chunks"]),
    )


graph_tool.name = "GRAPH"
