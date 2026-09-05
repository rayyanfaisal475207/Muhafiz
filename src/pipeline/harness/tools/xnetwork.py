"""
XNETWORK tool — src/pipeline/harness/tools/xnetwork.py (Phase 0,
foundation layer).

Thin adapter over `run_network_query(query_text, gateway, user_id,
user_role, top_k)` (AGENT_HARNESS_DESIGN.md §2.5), backed by
`query_similar_communities()` — embedding search over a separate,
precomputed community-summary Chroma collection, not the case graph. No
new logic: this module only translates `run_network_query()`'s return
shape / raised `PermissionError` into the standard `ToolResult` shape.

[PRESERVE — design §2.5] NEVER falls back to RAG (`XNetworkToolResult`
inherits `fallback_to_rag: Literal[False]`).

[PRESERVE — design §2.5] `raw_summary_text` is the deterministic,
already-grounded community-summary text — the fallback a SUB-AGENT serves
if its own generation step's paraphrase fails verification. This tool
always populates it; generation and verification (including XNETWORK's
own one-shot cloud-regeneration-before-fallback retry) are NOT this
primitive's job — the trust layer (Verifier) is an explicitly later phase
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §5/§8), and design §2.5's own wrap
boundary for this tool stops at `run_network_query()`. The XNETWORK-
specific cloud-retry behavior belongs to whichever sub-agent (Cross-Case
Linkage) eventually composes this tool with the Verifier — do not
implement it here, and do not generalize it to XGRAPH/XAGG without the
same live-failure evidence that justified it for XNETWORK specifically.

[PRESERVE — design §2.3/§4.3, same pattern as XGRAPH/XAGG] All
authorization logic stays inside `run_network_query()` itself; this
wrapper only translates its `PermissionError` into `status=DENIED`.
"""

from __future__ import annotations

import logging
from typing import Optional

from pydantic import Field

from src.data_gateway import get_gateway
from src.pipeline.harness.types import (
    ChunkMetadata,
    CrossCaseToolInput,
    CrossCaseToolResult,
    EvidenceChunk,
    ToolError,
    ToolStatus,
)
from src.pipeline.xnetwork import DEFAULT_TOP_K, run_network_query

logger = logging.getLogger(__name__)


class XNetworkToolInput(CrossCaseToolInput):
    """
    Open-ended cross-case pattern/theme synthesis over precomputed
    community summaries.

    STALENESS: because summaries are precomputed offline
    (community_detection.py / community_summarization.py), results reflect
    the corpus as of the last summarization run, not live graph state.
    """

    top_k: int = Field(default=DEFAULT_TOP_K, ge=1, description="Community summaries to retrieve.")


class XNetworkToolResult(CrossCaseToolResult):
    """
    [PRESERVE — design §2.5] Never falls back to RAG (inherited, pinned
    False).
    """

    community_ids: list[str] = Field(default_factory=list)
    community_case_ids: list[list[str]] = Field(
        default_factory=list,
        description=(
            "[findings.md CCL-C3] Per-community case IDs, index-aligned with "
            "`chunks`/`community_ids`: entry i is the case set of community i, "
            "and ONLY that community. Distinct from `case_ids_touched`, which "
            "is deliberately the UNION across every contributing community "
            "and must stay that way — it feeds the Verifier's "
            "allowed-cross-case-ID list (see CrossCaseToolResult's own "
            "[PRESERVE] note).\n\n"
            "Exists because the union alone cannot answer 'which cases does "
            "THIS community span?'. Stamping the union onto each item made a "
            "single-case community read as spanning every case the whole "
            "query touched — measured live: 17 of 19 real communities are "
            "single-case, and a 3-result query presented all three as "
            "spanning 4 cases.\n\n"
            "Empty when a caller does not supply it. Consumers MUST treat an "
            "absent entry as 'not known' and fall back to [] — NEVER to "
            "`case_ids_touched`, which would silently recreate the bug."
        ),
    )
    raw_summary_text: Optional[str] = Field(
        default=None,
        description="[PRESERVE] Raw community-summary text — the sub-agent-level fallback.",
    )
    no_relevant_reason: Optional[str] = Field(
        default=None,
        description=(
            "[Module 12 — RC-1] Set (status=EMPTY) when `run_network_query()`'s "
            "relevance gate found nearest communities but none cleared the "
            "relevance cutoff — distinct from an EMPTY caused by the "
            "community-report collection genuinely having nothing to return. "
            "Callers should surface this text (or their own wording built from "
            "it) rather than a generic 'no information available' message, and "
            "never recite the filtered-out communities as if they answered the "
            "question."
        ),
    )


def _render_raw_summary(results: list[dict]) -> Optional[str]:
    if not results:
        return None
    return "\n\n".join(f"({r['community_id']}) {r['summary_text']}" for r in results)


async def xnetwork_tool(tool_input: XNetworkToolInput) -> XNetworkToolResult:
    """The XNETWORK primitive: cross-case thematic synthesis, never falls back to RAG."""
    caller = tool_input.execution.caller
    gateway = await get_gateway()

    try:
        net_result = await run_network_query(
            tool_input.query_text,
            gateway,
            user_id=caller.user_id,
            user_role=caller.role.value,
            top_k=tool_input.top_k,
        )
    except PermissionError as exc:
        # run_network_query() has already written the authorization_violation
        # audit record (see xnetwork.py) — this wrapper only translates the
        # outcome, it does not re-derive or duplicate it.
        return XNetworkToolResult(
            status=ToolStatus.DENIED,
            error=ToolError(kind="permission_denied", message=str(exc)),
        )
    except Exception as exc:
        logger.error("XNETWORK tool: cross-case network query failed: %s", exc)
        return XNetworkToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
        )

    results = net_result["results"]
    chunks = [
        EvidenceChunk(
            id=f"community-{i}",
            text=r["summary_text"],
            metadata=ChunkMetadata(source_tool="XNETWORK", source_file=r["community_id"]),
        )
        for i, r in enumerate(results, start=1)
    ]

    return XNetworkToolResult(
        status=ToolStatus.OK if results else ToolStatus.EMPTY,
        chunks=chunks,
        # [PRESERVE] The UNION across every contributing community — the
        # Verifier's allowed-cross-case-ID list. Deliberately unchanged by
        # CCL-C3: narrowing this would make the leakage backstop reject
        # legitimate cross-case answers.
        case_ids_touched=net_result["case_ids"],
        community_ids=net_result["community_ids"],
        # [findings.md CCL-C3] The per-community truth, which
        # `run_network_query()` already computes per result and which was
        # previously dropped here. Built from the SAME `results` list, in
        # the same order, as `chunks` and `community_ids` above, so index i
        # refers to the same community in all three.
        community_case_ids=[list(r.get("case_ids") or []) for r in results],
        raw_summary_text=_render_raw_summary(results),
        no_relevant_reason=net_result.get("no_relevant_reason"),
    )


xnetwork_tool.name = "XNETWORK"
