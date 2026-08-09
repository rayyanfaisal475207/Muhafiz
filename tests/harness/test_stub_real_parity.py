"""
Stub and real tool implementations must be signature-interchangeable.

`registry.py` swaps `stubs.<tool>` for `real.<tool>` wholesale, so every caller
binds to one signature and gets whichever module is installed. If the two
diverge, a caller passing an argument only one of them accepts raises
`TypeError` — and only under the configuration that installs that module.

This is not hypothetical. `real.sql_tool` took `gateway` and `stubs.sql_tool`
did not, so Investigative Analysis (the only sub-agent passing a gateway to
SQL) crashed the instant `registry.use_stubs()` was active:

    TypeError: sql_tool() got an unexpected keyword argument 'gateway'

Its own tests never caught it because they run against the REAL tools. Auditing
the rest found two more latent copies of the same defect — `rag_tool` and
`graph_tool` had gained `project_id` on the real side only, and would have
failed identically the moment anything passed it.

Same class of problem as the contract/doc parity guard: two artefacts that must
agree, with nothing enforcing it.
"""
from __future__ import annotations

import inspect

import pytest

from src.pipeline.harness.tools import real, registry, stubs

_TOOLS = (
    "rag_tool", "graph_tool", "xgraph_tool", "xagg_tool",
    "xnetwork_tool", "sql_tool", "web_tool",
)


def _params(fn) -> list[str]:
    return list(inspect.signature(fn).parameters)


def test_registry_covers_every_tool():
    """Guard the guard: a tool missing from _TOOLS would be silently unchecked."""
    assert set(_TOOLS) == set(registry._TOOL_NAMES)


@pytest.mark.parametrize("name", _TOOLS)
def test_stub_and_real_accept_the_same_parameters(name: str):
    """
    Identical parameter NAMES in identical ORDER. Order matters because callers
    may pass positionally, so a reordering is as breaking as a rename even when
    the set matches.
    """
    stub_params = _params(getattr(stubs, name))
    real_params = _params(getattr(real, name))

    assert stub_params == real_params, (
        f"{name} signatures diverge — registry swaps these as interchangeable, "
        f"so a caller passing an argument only one accepts raises TypeError "
        f"under whichever module is installed.\n"
        f"  stub: {stub_params}\n"
        f"  real: {real_params}"
    )


@pytest.mark.parametrize("name", _TOOLS)
def test_optional_parameters_stay_optional_in_both(name: str):
    """
    A parameter required on one side and defaulted on the other passes the
    name check above while still breaking callers that omit it.
    """
    stub_sig = inspect.signature(getattr(stubs, name))
    real_sig = inspect.signature(getattr(real, name))

    for param in stub_sig.parameters:
        stub_required = stub_sig.parameters[param].default is inspect.Parameter.empty
        real_required = real_sig.parameters[param].default is inspect.Parameter.empty
        assert stub_required == real_required, (
            f"{name}'s '{param}' is "
            f"{'required' if stub_required else 'optional'} on the stub but "
            f"{'required' if real_required else 'optional'} on the real "
            f"implementation — a caller omitting it breaks against one of them."
        )


@pytest.mark.parametrize("name", _TOOLS)
def test_both_are_coroutine_functions(name: str):
    """Every tool is awaited at its call site; a sync stub would fail at runtime."""
    assert inspect.iscoroutinefunction(getattr(stubs, name))
    assert inspect.iscoroutinefunction(getattr(real, name))


async def test_the_original_crash_no_longer_reproduces(gateway):
    """
    The concrete defect this file exists for: SQL called WITH a gateway while
    stubs are installed. Pinned as a behavioural test, not just a signature
    one, so the fix is verified end-to-end rather than by introspection alone.
    """
    from src.pipeline.harness.contracts import CallerContext, Role, SqlToolInput

    registry.use_stubs()
    try:
        result = await registry.sql_tool(
            SqlToolInput(
                query_text="what section covers theft",
                caller=CallerContext(user_id="u1", role=Role.INVESTIGATOR),
            ),
            gateway=gateway,
        )
    finally:
        registry.use_real()

    assert result is not None


async def test_every_tool_is_callable_with_its_full_argument_set(gateway):
    """
    Signature parity proves the shapes match; this proves both actually RUN
    when handed every optional argument. Stubs only — the real tools need
    infrastructure — but the stub is the side that was silently wrong.
    """
    from src.pipeline.harness.contracts import (
        CallerContext, GraphToolInput, RagToolInput, Role, SqlToolInput,
        WebToolInput, XAggToolInput, XGraphToolInput, XNetworkToolInput,
    )

    caller = CallerContext(user_id="u1", role=Role.SUPERVISOR, active_case_id="CASE-A")
    registry.use_stubs()
    try:
        await registry.rag_tool(
            RagToolInput(query_text="q", caller=caller), events=None, project_id="P1")
        await registry.graph_tool(
            GraphToolInput(query_text="q", caller=caller), events=None, project_id="P1")
        await registry.xgraph_tool(
            XGraphToolInput(query_text="q", caller=caller), gateway=gateway, events=None)
        await registry.xagg_tool(
            XAggToolInput(query_text="q", caller=caller), gateway=gateway, events=None)
        await registry.xnetwork_tool(
            XNetworkToolInput(query_text="q", caller=caller), gateway=gateway, events=None)
        await registry.sql_tool(
            SqlToolInput(query_text="q", caller=caller), gateway=gateway, events=None)
        await registry.web_tool(
            WebToolInput(query_text="q", caller=caller), events=None, air_gap_mode=True)
    finally:
        registry.use_real()
