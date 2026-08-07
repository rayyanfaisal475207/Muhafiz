"""
SQL tool — src/pipeline/harness/tools/sql.py (Phase 0, foundation layer).

Thin adapter over `extract_sql_params(query) -> dict` (LLM param
extraction) -> `gateway.query_police_reference_data(category, subject,
section_ref) -> list[dict]` (direct parameterized Postgres query) —
AGENT_HARNESS_DESIGN.md §2.6. No new logic: the same two calls,
in the same order, orchestrator.py's SQL route already makes.

[PRESERVE — design §2.6] This is the ONLY in-scope SQL path.
`src/mcp/client.py`'s `execute_query()` (the standalone MCP Postgres demo,
`POST /api/admin/mcp-demo`) is a separate, unrelated integration — never
called from here, never merged with this tool.

[PRESERVE — design §2.6] `fallback_to_rag=True` on an empty row set OR on
any exception during extraction/query — both degrade to RAG today.

[PRESERVE — design §2.6] Reference data, not case evidence: NO case
scoping, NO role gate. Emitted chunks carry no owning case_id — inert to
the Verifier's leakage check, same reasoning already applied to XAGG's
synthetic chunk and to WEB.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from pydantic import Field

from src.data_gateway import get_gateway
from src.pipeline.harness.types import (
    ChunkMetadata,
    EvidenceChunk,
    ToolError,
    ToolInput,
    ToolResult,
    ToolStatus,
)
from src.pipeline.sql_extractor import extract_sql_params

logger = logging.getLogger(__name__)


class SqlToolInput(ToolInput):
    """
    Parameterized lookup against the structured police reference dataset
    (penal-code sections, cognizability).

    [PRESERVE — design §2.6] No case scoping and no role gate — reference
    data, not case evidence.
    """


class SqlToolResult(ToolResult):
    """
    [PRESERVE — design §2.6] Sets `fallback_to_rag=True` on an empty row
    set OR on any exception during parameter extraction or the query
    itself. Both degrade to RAG today.
    """

    row_count: int = 0
    extracted_params: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Filters derived from the query. Observability — lets an "
            "operator see whether a miss was bad extraction or genuinely "
            "absent data."
        ),
    )


def _to_evidence_chunk(idx: int, row: dict) -> EvidenceChunk:
    return EvidenceChunk(
        id=f"sql-row-{idx}",
        text=str(row),
        metadata=ChunkMetadata(source_tool="SQL", source_file=f"police_reference_data row {idx}"),
    )


async def sql_tool(tool_input: SqlToolInput) -> SqlToolResult:
    """The SQL primitive: extract params -> parameterized lookup, falling back to RAG on empty/error."""
    try:
        params = await extract_sql_params(tool_input.query_text)
        gateway = await get_gateway()
        db_results = await gateway.query_police_reference_data(
            category=(params or {}).get("category"),
            subject=(params or {}).get("subject"),
            section_ref=(params or {}).get("section_ref"),
        )
    except Exception as exc:
        logger.error("SQL tool: extraction/query failed: %s", exc)
        return SqlToolResult(
            status=ToolStatus.FAILED,
            error=ToolError(kind="upstream_failure", message=str(exc)),
            fallback_to_rag=True,
        )

    if not db_results:
        return SqlToolResult(
            status=ToolStatus.EMPTY,
            fallback_to_rag=True,
            row_count=0,
            extracted_params=params,
        )

    return SqlToolResult(
        status=ToolStatus.OK,
        chunks=[_to_evidence_chunk(idx, row) for idx, row in enumerate(db_results, start=1)],
        fallback_to_rag=False,
        row_count=len(db_results),
        extracted_params=params,
    )


sql_tool.name = "SQL"
