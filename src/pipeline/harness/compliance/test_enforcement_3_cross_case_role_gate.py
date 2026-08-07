"""
Enforcement point 3 (AGENT_HARNESS_DESIGN.md §2.3/§4.3) — per-tool
cross-case role checks, independently, inside
`graph_retriever.py::retrieve_graph()`, `xagg.py::run_aggregate()`, and
`xnetwork.py::run_network_query()` — each raises `PermissionError` and
writes an `authorization_violation` audit log BEFORE
`current_cross_case`/`current_rls_active` are armed.

[PRESERVE — SUBAGENT_INTERFACES.md §1.1 CrossCaseToolInput] "A harness
restructuring that hoists scope resolution 'up' to the supervisor or a
shared middleware, so that scope is armed before dispatch, REINTRODUCES
THIS BUG." The XGRAPH/XAGG/XNETWORK tool wrappers' entire job on this axis
is to translate the wrapped function's PermissionError into
`status=DENIED` — nothing more. Two things must both hold, checked
independently below:

  (a) STATIC — no wrapper file re-implements or duplicates the role check
      itself (that copy could drift out of sync with the real one, or
      run BEFORE it, at a point where scope resolution has already
      started — the exact "hoisted up" failure mode quoted above).
  (b) BEHAVIORAL — a PermissionError raised by the wrapped function
      actually surfaces as status=DENIED, for every one of the three
      cross-case tools, independently (design §4.3: "none of the
      enforcement points supersedes another" — a passing check on XGRAPH
      says nothing about XAGG or XNETWORK).
"""

import pytest

from src.pipeline.harness.compliance._ast_scan import strip_docstrings_and_comments
from src.pipeline.harness.compliance._source_scan import (
    CROSS_CASE_TOOL_MODULE_NAMES,
    module_source,
)
from src.pipeline.harness.types import CallerContext, ToolStatus

_FORBIDDEN_ROLE_CHECK_PATTERNS = (
    "CROSS_CASE_ROLES",
    "caller.role not in",
    "caller.role ==",
    "caller.role !=",
    "caller.role in",
)


@pytest.mark.parametrize("module_name", CROSS_CASE_TOOL_MODULE_NAMES)
def test_cross_case_tool_wrapper_adds_no_duplicate_role_check(module_name):
    code_only = strip_docstrings_and_comments(module_source(module_name))
    for pattern in _FORBIDDEN_ROLE_CHECK_PATTERNS:
        assert pattern not in code_only, (
            f"{module_name} appears to check caller.role itself ({pattern!r} "
            "found) — the role gate must live ONLY inside the wrapped "
            "function (retrieve_graph/run_aggregate/run_network_query); a "
            "second copy in the wrapper risks running at the wrong point "
            "in the call chain or drifting out of sync with the real check."
        )


def _denied_caller() -> CallerContext:
    return CallerContext(user_id="u1", role="investigator", active_case_id=None)


@pytest.mark.asyncio
async def test_xgraph_tool_surfaces_permission_error_as_denied(monkeypatch):
    import src.pipeline.harness.tools.xgraph as xgraph_mod

    async def _denied(*a, **kw):
        raise PermissionError("denied")

    monkeypatch.setattr(xgraph_mod, "retrieve_graph", _denied)

    result = await xgraph_mod.xgraph_tool(
        xgraph_mod.XGraphToolInput(query_text="q", caller=_denied_caller())
    )
    assert result.status == ToolStatus.DENIED
    assert result.fallback_to_rag is False


@pytest.mark.asyncio
async def test_xagg_tool_surfaces_permission_error_as_denied(monkeypatch):
    import src.pipeline.harness.tools.xagg as xagg_mod

    async def _get_gateway():
        return object()

    async def _denied(*a, **kw):
        raise PermissionError("denied")

    monkeypatch.setattr(xagg_mod, "get_gateway", _get_gateway)
    monkeypatch.setattr(xagg_mod, "run_aggregate", _denied)

    result = await xagg_mod.xagg_tool(
        xagg_mod.XAggToolInput(query_text="q", caller=_denied_caller())
    )
    assert result.status == ToolStatus.DENIED
    assert result.fallback_to_rag is False


@pytest.mark.asyncio
async def test_xnetwork_tool_surfaces_permission_error_as_denied(monkeypatch):
    import src.pipeline.harness.tools.xnetwork as xnetwork_mod

    async def _get_gateway():
        return object()

    async def _denied(*a, **kw):
        raise PermissionError("denied")

    monkeypatch.setattr(xnetwork_mod, "get_gateway", _get_gateway)
    monkeypatch.setattr(xnetwork_mod, "run_network_query", _denied)

    result = await xnetwork_mod.xnetwork_tool(
        xnetwork_mod.XNetworkToolInput(query_text="q", caller=_denied_caller())
    )
    assert result.status == ToolStatus.DENIED
    assert result.fallback_to_rag is False
