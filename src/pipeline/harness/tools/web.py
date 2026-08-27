"""
WEB tool — src/pipeline/harness/tools/web.py (Phase 0, foundation layer).

Thin adapter over `perform_web_search(query, max_results=5)` (Tavily,
domain-allowlisted) -> on failure/empty, `call_gemini_with_search()`
(Gemini grounded search, same domain filter applied post-hoc via
`_filter_allowed_domains()`) — AGENT_HARNESS_DESIGN.md §2.7. No new
logic: the same two-tier sequence, in the same order, orchestrator.py's
WEB route already runs.

[PRESERVE — design §2.7] `fallback_to_rag=True` only after BOTH Tavily
AND the Gemini fallback have failed/returned nothing. Two-tier
fallback-within-a-fallback — never collapsed to a single provider check.

[PRESERVE — design §2.7] `AIR_GAP_MODE` disabled entirely, at BOTH call
sites, before either provider is reached. `perform_web_search()` already
self-guards on `config.AIR_GAP_MODE` (returns `[]` without any HTTP call),
but `call_gemini_with_search()` does NOT — it has no AIR_GAP_MODE check of
its own (src/llm/client.py). This wrapper's single early
`config.AIR_GAP_MODE` check, before either tier runs, is what closes that
gap: since neither provider has been reached at that point, one check
upstream of both satisfies "before either provider is reached" exactly as
orchestrator.py's WEB route does today (one route-level check, not two
call-site-level ones) — do not "fix" this into two separate checks without
re-verifying `call_gemini_with_search()` still has none of its own; adding
a second check after the first already gates both tiers would be a no-op,
but REMOVING this one without adding an equivalent before the Gemini call
reopens outbound egress on the fallback path.

[PRESERVE — design §2.7] No case scope, no role gate. Web results are
NEVER cited as case evidence — emitted chunks carry no case_id.
"""

from __future__ import annotations

import logging
from typing import Literal, Optional

from pydantic import Field

from src import config
from src.llm.client import call_gemini_with_search
from src.pipeline.url_safety import is_domain_allowed
from src.pipeline.harness.types import (
    ChunkMetadata,
    EvidenceChunk,
    ToolError,
    ToolInput,
    ToolResult,
    ToolStatus,
)
from src.retrieval.web_search import perform_web_search

logger = logging.getLogger(__name__)


class WebToolInput(ToolInput):
    """
    Domain-allowlisted external search, with a grounded-search provider as
    a second tier.

    [PRESERVE — design §2.7] No case scope, no role gate.
    """

    max_results: int = Field(default=5, ge=1)


class WebToolResult(ToolResult):
    """
    [PRESERVE — design §2.7] Sets `fallback_to_rag=True` only after BOTH
    tiers have failed. Preserve the two-tier fallback-within-a-fallback
    shape — do not collapse it to a single provider check.
    """

    provider_used: Optional[Literal["primary_search", "grounded_search_fallback"]] = None
    fallback_to_rag: bool = False


def _filter_allowed_domains(sources: list[dict]) -> list[dict]:
    """
    Verbatim port of orchestrator.py's `_filter_allowed_domains()`: Gemini's
    google_search tool has no domain-restriction parameter (unlike
    Tavily's include_domains), so its grounding sources are filtered
    post-hoc against the same `config.WEB_ALLOWED_DOMAINS` allowlist
    Tavily is restricted to at request time.

    Matches on the parsed hostname (exact or dot-boundary subdomain), not a
    raw substring test — audit finding F-04, fixed identically here since
    this is a verbatim port of the same function.
    """
    return [s for s in sources if is_domain_allowed(s.get("url", ""), config.WEB_ALLOWED_DOMAINS)]


def _tavily_chunks(web_results: list[dict]) -> list[EvidenceChunk]:
    return [
        EvidenceChunk(
            id=f"web-{idx}",
            text=r.get("content", ""),
            score=r.get("score"),
            metadata=ChunkMetadata(source_tool="WEB", source_file=r.get("url", f"web-result-{idx}")),
        )
        for idx, r in enumerate(web_results, start=1)
    ]


def _gemini_chunks(gemini_sources: list[dict], full_response: str) -> list[EvidenceChunk]:
    if gemini_sources:
        return [
            EvidenceChunk(
                id=f"gemini-{idx}",
                text=r.get("content", r.get("url", "")),
                metadata=ChunkMetadata(source_tool="WEB", source_file=r.get("url", f"gemini-result-{idx}")),
            )
            for idx, r in enumerate(gemini_sources, start=1)
        ]
    if full_response:
        return [
            EvidenceChunk(
                id="gemini-0",
                text=full_response,
                metadata=ChunkMetadata(source_tool="WEB", source_file="gemini-search"),
            )
        ]
    return []


async def web_tool(tool_input: WebToolInput) -> WebToolResult:
    """The WEB primitive: Tavily -> Gemini-grounded-search fallback, falling back to RAG only if both fail."""
    if config.AIR_GAP_MODE:
        # Neither provider reached — see module docstring's AIR_GAP_MODE note.
        return WebToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True)

    try:
        web_results = await perform_web_search(tool_input.query_text, max_results=tool_input.max_results)
    except Exception as exc:
        logger.warning("WEB tool: Tavily search raised: %s. Trying Gemini fallback...", exc)
        web_results = []

    if web_results:
        return WebToolResult(
            status=ToolStatus.OK,
            chunks=_tavily_chunks(web_results),
            fallback_to_rag=False,
            provider_used="primary_search",
        )

    # ── Tier 2: Gemini grounded search ──────────────────────────────────
    try:
        full_response, gemini_sources = await call_gemini_with_search(
            user_message=tool_input.query_text, max_tokens=1500
        )
    except Exception as exc:
        logger.error("WEB tool: Gemini search fallback failed: %s", exc)
        return WebToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
            fallback_to_rag=True,
        )

    gemini_sources = _filter_allowed_domains(gemini_sources)
    chunks = _gemini_chunks(gemini_sources, full_response)

    if not chunks:
        return WebToolResult(status=ToolStatus.EMPTY, fallback_to_rag=True, provider_used="grounded_search_fallback")

    return WebToolResult(
        status=ToolStatus.OK,
        chunks=chunks,
        fallback_to_rag=False,
        provider_used="grounded_search_fallback",
    )


web_tool.name = "WEB"
