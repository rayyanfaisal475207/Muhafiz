"""
Enforcement point 4 (AGENT_HARNESS_DESIGN.md §4.4) — `user_role`
provenance. Must always come from `current_user.role` (threaded through
the harness as `CallerContext.role` — see types.py's own [PRESERVE —
design §4.4] docstring), NEVER from `user_profile`
(`{context_text, preferred_language, llm_mode}` — no role key at all).

This is THE documented historical bug: `user_profile.get("role",
"investigator")` used to silently default every cross-case check to
"investigator" for every user, denying real supervisors/admins their own
access. Two things checked independently:

  (a) STATIC — no tool wrapper file references `user_profile` or reads a
      "role" key out of a dict (the exact shape of the historical bug),
      and every `user_role=` keyword argument passed to a wrapped function
      is textually derived from `caller.role` — never a hardcoded default
      string.
  (b) BEHAVIORAL — a CallerContext carrying a real elevated role (e.g.
      platform-admin) actually reaches the wrapped function as that same
      role, not silently downgraded to "investigator".
"""

import re

import pytest

from src.pipeline.harness.compliance._source_scan import all_tool_wrapper_sources
from src.pipeline.harness.types import CallerContext

# NOTE: this module deliberately scans RAW source, not the code-only-stripped
# text enforcement points 2/3 use — `.get("role"` (the exact shape of the
# historical bug) is itself a string literal in real violating code, so
# stripping string tokens would hide the one thing this check exists to
# catch. Confirmed none of the 7 tool wrapper files' docstrings mention
# "user_profile" or `.get("role"` in prose, so raw scanning has no
# false-positive source here the way enforcement point 2 did.

_FORBIDDEN_PROFILE_PATTERNS = ('user_profile', '.get("role"', ".get('role'")

_ROLE_KWARG_RE = re.compile(r"user_role\s*=\s*([^\s,)]+)")


@pytest.mark.parametrize("module_name,source", all_tool_wrapper_sources().items())
def test_tool_wrapper_never_reads_role_from_a_profile_object(module_name, source):
    for pattern in _FORBIDDEN_PROFILE_PATTERNS:
        assert pattern not in source, (
            f"{module_name} references {pattern!r} — user_role must come "
            "from CallerContext.role only. This is the literal shape of "
            "the historical bug design §4.4 documents: reading role out of "
            "a profile/preferences object (which carries no role key at "
            "all) silently defaults every cross-case check to "
            "'investigator', denying real supervisors/admins their own "
            "access."
        )


@pytest.mark.parametrize("module_name,source", all_tool_wrapper_sources().items())
def test_every_user_role_argument_derives_from_caller_role(module_name, source):
    matches = _ROLE_KWARG_RE.findall(source)
    for value_expr in matches:
        assert "caller.role" in value_expr, (
            f"{module_name} passes user_role={value_expr!r}, which does "
            "not derive from caller.role — role provenance must always "
            "trace back to CallerContext.role, never a hardcoded default "
            "or any other source (design §4.4)."
        )


@pytest.mark.asyncio
async def test_elevated_role_actually_reaches_xgraph_wrapped_function(monkeypatch):
    import src.pipeline.harness.tools.xgraph as xgraph_mod

    captured = {}

    async def _capture(query_text, target_entity, case_id, cross_case, max_hops, user_id, user_role):
        captured["user_role"] = user_role
        return {"chunks": [], "hop_count": 0, "compounded_confidence": 1.0, "seed_entities": [], "unconfirmed_links": []}

    monkeypatch.setattr(xgraph_mod, "retrieve_graph", _capture)

    caller = CallerContext(user_id="u1", role="platform-admin", active_case_id=None)
    await xgraph_mod.xgraph_tool(xgraph_mod.XGraphToolInput(query_text="q", caller=caller))

    assert captured["user_role"] == "platform-admin", (
        "An elevated role did not reach the wrapped function unchanged — "
        "this is exactly the historical bug's observable symptom."
    )


@pytest.mark.asyncio
async def test_elevated_role_actually_reaches_xagg_wrapped_function(monkeypatch):
    import src.pipeline.harness.tools.xagg as xagg_mod

    captured = {}

    async def _get_gateway():
        return object()

    async def _capture(query_text, target_entity, gateway, user_id, user_role):
        captured["user_role"] = user_role
        return {"kind": "total_count", "total_cases": 0}

    monkeypatch.setattr(xagg_mod, "get_gateway", _get_gateway)
    monkeypatch.setattr(xagg_mod, "run_aggregate", _capture)

    caller = CallerContext(user_id="u1", role="station-admin", active_case_id=None)
    await xagg_mod.xagg_tool(xagg_mod.XAggToolInput(query_text="q", caller=caller))

    assert captured["user_role"] == "station-admin"


@pytest.mark.asyncio
async def test_elevated_role_actually_reaches_xnetwork_wrapped_function(monkeypatch):
    import src.pipeline.harness.tools.xnetwork as xnetwork_mod

    captured = {}

    async def _get_gateway():
        return object()

    async def _capture(query_text, gateway, user_id, user_role, top_k):
        captured["user_role"] = user_role
        return {"kind": "network_synthesis", "results": [], "community_ids": [], "case_ids": []}

    monkeypatch.setattr(xnetwork_mod, "get_gateway", _get_gateway)
    monkeypatch.setattr(xnetwork_mod, "run_network_query", _capture)

    caller = CallerContext(user_id="u1", role="supervisor", active_case_id=None)
    await xnetwork_mod.xnetwork_tool(xnetwork_mod.XNetworkToolInput(query_text="q", caller=caller))

    assert captured["user_role"] == "supervisor"
