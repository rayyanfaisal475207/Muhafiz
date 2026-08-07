"""
Tests for src/pipeline/harness/tools/sql.py (Phase 0, foundation layer).

Covers:
  (a) standardized SqlToolResult/EvidenceChunk shape;
  (b) fallback_to_rag=True on empty rows AND on extraction/query exception
      (status differs: EMPTY vs FAILED, fallback_to_rag is True either way);
  (c) no case scoping / no role gate (no CallerContext.role branching at
      all — any role's SqlToolInput produces the same result);
  (d) emitted chunks carry no case_id (inert to the leakage check).
"""
import pytest

import src.pipeline.harness.tools.sql as sql_mod
from src.pipeline.harness.tools.sql import SqlToolInput, SqlToolResult, sql_tool
from src.pipeline.harness.types import CallerContext, ToolStatus


def _caller(role="investigator"):
    return CallerContext(user_id="u1", role=role, active_case_id=None)


class _FakeGateway:
    def __init__(self, rows):
        self._rows = rows

    async def query_police_reference_data(self, category=None, subject=None, section_ref=None):
        return self._rows


@pytest.mark.asyncio
async def test_rows_found_returns_ok(monkeypatch):
    async def _extract(query):
        return {"category": "theft", "subject": None, "section_ref": None}

    rows = [{"ref_id": "1", "section_ref": "379", "description": "Theft"}]

    async def _get_gateway():
        return _FakeGateway(rows)

    monkeypatch.setattr(sql_mod, "extract_sql_params", _extract)
    monkeypatch.setattr(sql_mod, "get_gateway", _get_gateway)

    result = await sql_tool(SqlToolInput(query_text="what section covers theft", caller=_caller()))

    assert isinstance(result, SqlToolResult)
    assert result.status == ToolStatus.OK
    assert result.fallback_to_rag is False
    assert result.row_count == 1
    assert result.extracted_params == {"category": "theft", "subject": None, "section_ref": None}
    assert len(result.chunks) == 1
    assert result.chunks[0].metadata.source_tool == "SQL"
    assert result.chunks[0].metadata.case_id is None


@pytest.mark.asyncio
async def test_empty_rows_falls_back_to_rag(monkeypatch):
    async def _extract(query):
        return {"category": "nonexistent", "subject": None, "section_ref": None}

    async def _get_gateway():
        return _FakeGateway([])

    monkeypatch.setattr(sql_mod, "extract_sql_params", _extract)
    monkeypatch.setattr(sql_mod, "get_gateway", _get_gateway)

    result = await sql_tool(SqlToolInput(query_text="q", caller=_caller()))

    assert result.status == ToolStatus.EMPTY
    assert result.fallback_to_rag is True
    assert result.row_count == 0
    assert result.chunks == []


@pytest.mark.asyncio
async def test_extraction_exception_falls_back_and_fails(monkeypatch):
    async def _extract(query):
        raise RuntimeError("LLM extraction broke")

    monkeypatch.setattr(sql_mod, "extract_sql_params", _extract)

    result = await sql_tool(SqlToolInput(query_text="q", caller=_caller()))

    assert result.status == ToolStatus.FAILED
    assert result.fallback_to_rag is True
    assert result.error.kind == "upstream_failure"


@pytest.mark.asyncio
async def test_extraction_returning_none_does_not_crash(monkeypatch):
    # extract_sql_params() can legitimately return None (json_extract.py's
    # own documented failure mode) — must not raise on `(None or {})`.
    async def _extract(query):
        return None

    async def _get_gateway():
        return _FakeGateway([])

    monkeypatch.setattr(sql_mod, "extract_sql_params", _extract)
    monkeypatch.setattr(sql_mod, "get_gateway", _get_gateway)

    result = await sql_tool(SqlToolInput(query_text="q", caller=_caller()))

    assert result.status == ToolStatus.EMPTY
    assert result.fallback_to_rag is True


@pytest.mark.asyncio
async def test_no_role_gate_same_result_for_every_role(monkeypatch):
    async def _extract(query):
        return {"category": "theft", "subject": None, "section_ref": None}

    async def _get_gateway():
        return _FakeGateway([{"ref_id": "1"}])

    monkeypatch.setattr(sql_mod, "extract_sql_params", _extract)
    monkeypatch.setattr(sql_mod, "get_gateway", _get_gateway)

    investigator_result = await sql_tool(SqlToolInput(query_text="q", caller=_caller("investigator")))
    admin_result = await sql_tool(SqlToolInput(query_text="q", caller=_caller("platform-admin")))

    assert investigator_result.status == admin_result.status == ToolStatus.OK
