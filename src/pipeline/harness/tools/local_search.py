"""
LOCAL_SEARCH tool — src/pipeline/harness/tools/local_search.py
(findings.md Module 8, "Local Search — entity-based reasoning").

MS GraphRAG's Local Search, adapted to this codebase's existing machinery
rather than reimplemented: embed the query, match it against entity
DESCRIPTION embeddings (src/retrieval/entity_vector_store.py — genuinely new,
nothing like it exists elsewhere in this codebase) to find semantically
similar "access point" entities, fan out from each into `retrieve_graph()`'s
EXISTING traversal (unchanged — see below) plus a new community-report join,
rank/filter with the SAME `cross_rerank()` GRAPH_HYBRID already uses, gate
through the SAME `evaluate_relevance()` evaluator GRAPH/GRAPH_HYBRID already
use. Same shape as tools/graph.py: one retrieval mechanism per tool file,
composed by the sub-agent (src/pipeline/harness/agents/local_search.py).

THE ONE TRICK THAT MAKES "REUSE retrieve_graph()'S EXISTING TRAVERSAL"
LITERAL, NOT ASPIRATIONAL: `retrieve_graph(query_text, target_entity, ...)`
re-derives its own seed candidates from `target_entity` via
`_seed_candidates()` -> `_find_seed_nodes()`'s literal `CONTAINS` match.
Feeding a SEMANTICALLY matched entity's own `canonical_name` in as
`target_entity` makes that literal lookup trivially succeed (it is now
searching for a string the graph does contain) — the entire existing
hop-traversal, confirmed-SAME_AS folding, and `_NOTABLE_PROPERTIES`
evidence-text machinery then runs completely unmodified.
`src/retrieval/graph_retriever.py` is NOT touched by this module.

WITHIN-CASE ONLY, NO ROLE GATE. Same discipline as GRAPH (SUBAGENT_
INTERFACES.md §2.2) — case-assignment-based scoping, not role-based. No
cross-case path exists here at all: `entity_vector_store.query_similar_entities()`
is hard-scoped to `caller.active_case_id` and returns empty with no query at
all when that is None (see its own docstring). This introduces no new
enforcement point, which is why it is deliberately NOT added to
compliance/_source_scan.py's TOOL_WRAPPER_MODULE_NAMES list — that list
exists for tools with an independent role gate or RLS-arming responsibility
to statically verify; this tool has neither, it inherits retrieve_graph()'s
existing case-scoping chokepoint unchanged.

SourceTool TAGGING DECISION (flagged in the approved plan, not silently
picked): `SourceTool` is a closed 8-value Literal (types.py) that does not
include "LOCAL_SEARCH" — extending it would touch ChunkMetadata, Citation,
and SOURCE_TOOL_DISPLAY_LABELS everywhere they're consumed. Local Search's
own identity lives at the sub-agent/dispatch level only
(supervisor.py's SUB_AGENT_NAMES). Every chunk this tool emits — both from
graph traversal AND from the community-report join — is tagged
`source_tool="GRAPH"` (it genuinely is graph-derived data, just semantically
seeded), with an additional open `metadata["evidence_kind"]` key
("local_search_graph" | "community_report") for in-prompt/debug
disambiguation only (ChunkMetadata.model_config = {"extra": "allow"}
already supports this — no schema change).

KNOWN GAP, not fixed here (see plan): the community-report join looks up
`community_membership` by the matched entity's OWN entity_id directly, not
canonicalized through confirmed SAME_AS. A non-canonical duplicate of a
community member will miss that community. Fully closing this needs the
same fetch_confirmed_same_as()+canon() pass community_summarization.py
already does — deliberately not pulled into this per-query hot path (a
graph-wide fetch, wasteful to redo on every Local Search call).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal, Optional

from pydantic import Field
from sqlalchemy import text

from src import config
from src.database.postgres import get_session
from src.pipeline.evaluator import evaluate_relevance
from src.pipeline.harness.tools.graph import _to_evidence_chunk
from src.pipeline.harness.types import ToolError, ToolInput, ToolResult, ToolStatus
from src.retrieval.cross_reranker import cross_rerank
from src.retrieval.entity_vector_store import query_similar_entities
from src.retrieval.graph_retriever import DEFAULT_HOPS, MAX_HOPS, retrieve_graph

logger = logging.getLogger(__name__)


# Closed set, same Literal-alias convention ToolError.kind already uses in
# types.py rather than a new Enum class. Deliberately does NOT distinguish an
# empty entity index from case-scope filtering: `query_similar_entities()` is
# hard-scoped server-side, so both surface identically as zero scoped matches
# and separating them would need an unscoped lookup this tool must not make.
EmptyReason = Literal[
    "no_case_scope",
    "no_entity_match",
    "no_linked_evidence",
    "evaluator_rejected",
]


class LocalSearchToolInput(ToolInput):
    """
    Semantic entity-access-point search within the caller's active case.
    Takes no `target_entity` — unlike GRAPH, the whole point of this tool is
    finding the entity FROM the query text via semantic match, not being
    told one.
    """

    max_hops: int = Field(default=DEFAULT_HOPS, ge=1, le=MAX_HOPS, description="Traversal depth cap per matched entity.")
    top_k_entities: Optional[int] = Field(
        default=None,
        description="Semantically-matched entities to fan out from. None = config.LOCAL_SEARCH_TOP_K_ENTITIES.",
    )


class LocalSearchToolResult(ToolResult):
    """
    [PRESERVE — mirrors GraphToolResult's own contract] Sets
    `fallback_to_rag=True` when nothing usable comes back: no case in scope,
    no semantic entity match, no traversal/community chunks, or the
    relevance evaluator rejects what was found. All degrade to RAG, same as
    GRAPH/GRAPH_HYBRID today.
    """

    empty_reason: Optional[EmptyReason] = Field(
        default=None,
        description=(
            "Which EMPTY condition fired, so the caller can describe the degradation "
            "truthfully instead of collapsing every EMPTY to 'no semantic match'. "
            "Observability only — never influences retrieval."
        ),
    )
    hop_count: int = Field(default=0, description="Deepest hop reached across every matched entity's traversal.")
    chain_confidence: Optional[float] = Field(
        default=None, description="Weakest link across every matched entity's traversal."
    )
    matched_entities: list[dict[str, Any]] = Field(
        default_factory=list, description="Semantic access-point matches. Observability."
    )
    community_reports_included: bool = Field(default=False)


def _dedupe_chunks(chunks: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for c in chunks:
        cid = c.get("id")
        if cid and cid not in seen:
            seen.add(cid)
            out.append(c)
    return out


async def _fetch_community_chunks(entity_ids: list[str], case_id: Optional[str]) -> list[dict]:
    """
    Direct entity_id lookup against community_membership/community_reports —
    see this module's docstring "KNOWN GAP" note for the canonicalization
    limitation. Returns [] (not an error) when none of the matched entities
    belong to any community — a legitimate outcome (non-Person labels never
    have one; a singleton Person community is never summarized at all, per
    community_detection.MIN_MEMBERS_FOR_SUMMARY).
    """
    if not entity_ids:
        return []
    async with get_session() as db:
        res = await db.execute(
            text(
                "SELECT DISTINCT r.community_id, r.summary_text, r.case_ids, r.member_count "
                "FROM community_membership m "
                "JOIN community_reports r ON r.community_id = m.community_id "
                "WHERE m.entity_id = ANY(:entity_ids)"
            ),
            {"entity_ids": entity_ids},
        )
        rows = [dict(row) for row in res.mappings()]

    chunks = []
    for row in rows:
        chunks.append({
            "id": f"community:{row['community_id']}",
            "text": row["summary_text"],
            "metadata": {
                "case_id": case_id,
                "source": f"community:{row['community_id']}",
                "evidence_kind": "community_report",
                "member_count": row.get("member_count"),
            },
        })
    return chunks


async def local_search_tool(tool_input: LocalSearchToolInput) -> LocalSearchToolResult:
    """The LOCAL_SEARCH primitive."""
    caller = tool_input.execution.caller
    if not caller.active_case_id:
        # Fail closed — within-case only, no case in scope means nothing to
        # search. Mirrors retrieve_graph()'s own rule for the same shape.
        return LocalSearchToolResult(status=ToolStatus.EMPTY, empty_reason="no_case_scope")

    top_k_entities = tool_input.top_k_entities or config.LOCAL_SEARCH_TOP_K_ENTITIES
    try:
        matches = await query_similar_entities(tool_input.query_text, caller.active_case_id, top_k_entities)
    except Exception as exc:
        # Same shape every sibling tool already uses for an infrastructure
        # failure. Without this the exception escapes the tool/agent contract
        # entirely and Local Search can never emit a caveat at all.
        logger.error("LOCAL_SEARCH tool: entity retrieval failed: %s", exc)
        return LocalSearchToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
        )
    if not matches:
        return LocalSearchToolResult(
            status=ToolStatus.EMPTY, fallback_to_rag=True, empty_reason="no_entity_match"
        )

    graph_results = await asyncio.gather(*[
        retrieve_graph(
            tool_input.query_text,
            m["canonical_name"],
            caller.active_case_id,
            cross_case=False,
            max_hops=tool_input.max_hops,
            user_id=caller.user_id,
            user_role=caller.role.value,
        )
        for m in matches
    ])

    graph_chunks: list[dict] = []
    for r in graph_results:
        graph_chunks.extend(r["chunks"])
    graph_chunks = _dedupe_chunks(graph_chunks)

    hop_count = max((r["hop_count"] for r in graph_results), default=0)
    confidences = [r["compounded_confidence"] for r in graph_results if r["chunks"]]
    chain_confidence = min(confidences) if confidences else None

    community_chunks = await _fetch_community_chunks(
        [m["entity_id"] for m in matches], caller.active_case_id
    )

    candidate_pool = graph_chunks + community_chunks
    if not candidate_pool:
        return LocalSearchToolResult(
            status=ToolStatus.EMPTY, fallback_to_rag=True, empty_reason="no_linked_evidence",
            matched_entities=matches,
            hop_count=hop_count, chain_confidence=chain_confidence,
        )

    try:
        reranked = await cross_rerank(tool_input.query_text, candidate_pool, top_k=config.TOP_K_RERANK)
    except Exception as exc:
        logger.error("LOCAL_SEARCH tool: cross-encoder rerank failed: %s. Falling back to unranked order.", exc)
        reranked = candidate_pool[: config.TOP_K_RERANK]

    try:
        evaluation = await evaluate_relevance(tool_input.query_text, tool_input.query_text, reranked)
    except Exception as exc:
        logger.error("LOCAL_SEARCH tool: evaluator failed: %s", exc)
        evaluation = {"relevant": True, "reason": "Evaluator failed, proceeding"}

    if not evaluation.get("relevant", False):
        return LocalSearchToolResult(
            status=ToolStatus.EMPTY, fallback_to_rag=True, empty_reason="evaluator_rejected",
            matched_entities=matches,
            hop_count=hop_count, chain_confidence=chain_confidence,
        )

    community_ids_present = {c["id"] for c in community_chunks}
    return LocalSearchToolResult(
        status=ToolStatus.OK,
        chunks=[_to_evidence_chunk(c, "GRAPH") for c in reranked],
        fallback_to_rag=False,
        matched_entities=matches,
        hop_count=hop_count,
        chain_confidence=chain_confidence,
        community_reports_included=any(c.get("id") in community_ids_present for c in reranked),
    )


local_search_tool.name = "GRAPH"
