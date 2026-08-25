"""
GLOBAL SEARCH tool — src/pipeline/harness/tools/global_search.py
(findings.md Module 9, Stage 1 — "Global Search: whole-dataset map-reduce
reasoning").

Thin adapter over `run_global_search_query(query_text, gateway, user_id,
user_role, hierarchy_level, jurisdiction_case_ids)`
(src/pipeline/global_search.py) — same "role gate + RLS arming + fetch,
nothing else" boundary xnetwork.py draws for run_network_query(). No
generation, no verification, no batching/shuffling/map-reduce logic here:
that belongs entirely to the sub-agent that composes this tool
(src/pipeline/harness/agents/global_search.py), same split
xnetwork.py/cross_case_linkage.py already establish.

[PRESERVE — mirrors design §2.5's XNETWORK rule] NEVER falls back to RAG
(`GlobalSearchToolResult` inherits `fallback_to_rag: Literal[False]`) —
Global Search is cross-case by definition (a whole-dataset aggregation
question has no single-case RAG equivalent to fall back to).

[PRESERVE — mirrors design §2.3/§4.3, same pattern as XGRAPH/XAGG/XNETWORK]
All authorization logic stays inside `run_global_search_query()` itself;
this wrapper only translates its `PermissionError` into `status=DENIED`.
This module is registered in
src/pipeline/harness/compliance/_source_scan.py's
TOOL_WRAPPER_MODULE_NAMES and CROSS_CASE_TOOL_MODULE_NAMES — the harness
compliance suite's enforcement points 2/3/4/5 parametrize over those two
lists, so registering here is what actually makes those checks cover this
new cross-case surface (no duplicate role check, no RLS self-arming, a
PermissionError really surfaces as DENIED, user_role really derives from
CallerContext.role, no direct graph-database access bypassing the
scoped-Cypher chokepoint) rather than the compliance suite merely staying
green because it never looked. (Enforcement point 5 scans this file's raw
source, docstrings included, for the literal graph-client/query-execution
symbol names — deliberately not spelled out here so this very sentence
doesn't trip that same check.)

SourceTool TAGGING — decision, flagged explicitly per this session's own
discipline (mirrors local_search.py's own "SourceTool TAGGING" section):
every chunk this tool emits is tagged `source_tool="XNETWORK"`, NOT a new
"GLOBAL_SEARCH" SourceTool literal value. Rationale: this tool reads the
exact same community_reports corpus XNETWORK's own SourceTool tag already
represents (SOURCE_TOOL_DISPLAY_LABELS's "cross-case pattern synthesis"
label describes this evidence at least as well as it describes today's
top-5 XNETWORK cut) — only how it's PROCESSED downstream (map-reduce
across every report for a level, vs. a top-k similarity cut) differs, not
what kind of evidence it is. Minting a new SourceTool value would also
require adding it everywhere that Literal is threaded (ChunkMetadata,
Citation, SubAgentResult.tools_used/degraded_from,
SOURCE_TOOL_DISPLAY_LABELS) for a distinction `tools_used`/`citations`
were never meant to carry in the first place — the caller already knows
which SUB-AGENT ran (Supervisor classification), so `SourceTool` only
needs to say which underlying primitive/corpus contributed, which
"XNETWORK" already says accurately.
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import Field

from src.data_gateway import get_gateway
from src.pipeline.global_search import run_global_search_query
from src.pipeline.harness.types import (
    ChunkMetadata,
    CrossCaseToolInput,
    CrossCaseToolResult,
    EvidenceChunk,
    ToolError,
    ToolStatus,
)

logger = logging.getLogger(__name__)


class GlobalSearchToolInput(CrossCaseToolInput):
    """
    Whole-dataset map-reduce input. Unlike XNetworkToolInput's `top_k`,
    there is no similarity cut here — `hierarchy_level` selects WHICH
    partition to fetch every report from, not how many to return.
    """

    hierarchy_level: Optional[int] = Field(
        default=None,
        description=(
            "Which Louvain hierarchy level's reports to fetch. None "
            "resolves to a middle level among whatever is actually "
            "summarized (see run_global_search_query()'s own docstring). "
            "Pre-Stage-2 there is only ever level 0."
        ),
    )


class GlobalSearchToolResult(CrossCaseToolResult):
    """
    [PRESERVE — mirrors design §2.5's XNETWORK rule] Never falls back to
    RAG (inherited, pinned False).
    """

    hierarchy_level: Optional[int] = None
    community_ids: list[str] = Field(default_factory=list)
    report_count_total: int = Field(
        default=0,
        description=(
            "How many community reports this level actually had, BEFORE "
            "any cap/sample the composing sub-agent's map step applies — "
            "lets that sub-agent build an honest 'answered from a sample "
            "of N of M reports' caveat rather than only knowing the "
            "post-cap count."
        ),
    )


async def global_search_tool(tool_input: GlobalSearchToolInput) -> GlobalSearchToolResult:
    """The Global Search primitive: fetch every community report for one hierarchy level, never falls back to RAG."""
    caller = tool_input.execution.caller
    gateway = await get_gateway()

    try:
        result = await run_global_search_query(
            tool_input.query_text,
            gateway,
            user_id=caller.user_id,
            user_role=caller.role.value,
            hierarchy_level=tool_input.hierarchy_level,
        )
    except PermissionError as exc:
        # run_global_search_query() has already written the
        # authorization_violation audit record — this wrapper only
        # translates the outcome, it does not re-derive or duplicate it.
        return GlobalSearchToolResult(
            status=ToolStatus.DENIED,
            error=ToolError(kind="permission_denied", message=str(exc)),
        )
    except Exception as exc:
        logger.error("Global Search tool: whole-dataset report fetch failed: %s", exc)
        return GlobalSearchToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
        )

    reports = result["reports"]
    chunks = [
        EvidenceChunk(
            id=f"community-{i}",
            text=r["summary_text"],
            metadata=ChunkMetadata(source_tool="XNETWORK", source_file=r["community_id"]),
        )
        for i, r in enumerate(reports, start=1)
    ]

    return GlobalSearchToolResult(
        status=ToolStatus.OK if reports else ToolStatus.EMPTY,
        chunks=chunks,
        case_ids_touched=result["case_ids"],
        hierarchy_level=result["hierarchy_level"],
        community_ids=result["community_ids"],
        report_count_total=len(reports),
    )


global_search_tool.name = "XNETWORK"
