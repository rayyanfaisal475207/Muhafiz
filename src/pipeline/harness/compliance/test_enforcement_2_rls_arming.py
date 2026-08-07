"""
Enforcement point 2 (AGENT_HARNESS_DESIGN.md §4.2) — RLS context arming.

`set_case_scope(case_id)` (src/auth/rls_context.py) is called once in
main.py immediately after enforcement point 1 passes. `process_query()`
does not self-arm this today, and per `CallerContext`'s own docstring
(types.py, [PRESERVE — design §4.1/§4.2]) neither may any harness tool —
arming stays the caller's (the API boundary's) responsibility. A tool
wrapper that armed RLS itself would let scope get armed at the wrong point
in the call chain (or twice, redundantly, with no single source of truth
for whether it already happened).

Cross-case tools (XGRAPH/XAGG/XNETWORK) DO end up arming
`current_cross_case`/`current_rls_active` — but that arming happens
strictly inside `retrieve_graph()`/`run_aggregate()`/`run_network_query()`
themselves (existing code, already covered by enforcement point 3), AFTER
their own role check passes. None of the 7 wrapper files in
src/pipeline/harness/tools/ may reference these symbols directly — doing
so would mean the wrapper is arming (or re-arming) scope itself, which is
exactly the ordering hazard §4.2 exists to prevent.
"""

import pytest

from src.pipeline.harness.compliance._ast_scan import strip_docstrings_and_comments
from src.pipeline.harness.compliance._source_scan import all_tool_wrapper_sources

_FORBIDDEN_PATTERNS = (
    "current_rls_active",
    "current_case_id",
    "current_cross_case",
    "set_case_scope",
    "set_cross_case_scope",
)


@pytest.mark.parametrize("module_name,source", all_tool_wrapper_sources().items())
def test_tool_wrapper_never_self_arms_rls(module_name, source):
    # Code tokens only — several wrapper docstrings legitimately NAME these
    # symbols in [PRESERVE] prose explaining why the wrapper must not call
    # them; that prose must not itself trip this check (see _ast_scan.py).
    code_only = strip_docstrings_and_comments(source)
    for pattern in _FORBIDDEN_PATTERNS:
        assert pattern not in code_only, (
            f"{module_name} references {pattern!r} — RLS/cross-case scope "
            "arming must stay the caller's (API boundary's) responsibility "
            "(design §4.2), or, for cross-case tools, the wrapped "
            "function's own responsibility (design §4.3, already covered "
            "by enforcement point 3). A tool wrapper arming scope itself "
            "reintroduces the exact ordering hazard this design closes."
        )
