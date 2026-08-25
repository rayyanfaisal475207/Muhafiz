"""
Shared helper for the compliance suite: reads back the source text of every
Phase 0 tool wrapper module, so each enforcement-point check can grep it
for forbidden/required patterns. Static-source checks catch a class of
regression a purely behavioral test can miss — e.g. "this file must never
import age_client directly" has no observable behavior to assert on until
someone actually adds a call site, by which point the chokepoint is
already bypassed in production code, not just in a hypothetical.
"""

from __future__ import annotations

import importlib
from pathlib import Path

TOOL_WRAPPER_MODULE_NAMES: list[str] = [
    "src.pipeline.harness.tools.rag",
    "src.pipeline.harness.tools.graph",
    "src.pipeline.harness.tools.xgraph",
    "src.pipeline.harness.tools.xagg",
    "src.pipeline.harness.tools.xnetwork",
    "src.pipeline.harness.tools.sql",
    "src.pipeline.harness.tools.web",
    # [AMENDMENT — findings.md Module 9, "Global Search"] Registered here
    # (not just written) so enforcement points 2/4/5 below actually
    # parametrize over it — see tools/global_search.py's own module
    # docstring.
    "src.pipeline.harness.tools.global_search",
]

# [PRESERVE — design §2.3/§2.4/§2.5] The tools with an independent,
# in-function cross-case role gate (XGRAPH/XAGG/XNETWORK, plus
# [AMENDMENT — findings.md Module 9] global_search) — GRAPH/GRAPH_HYBRID
# is within-case-only and carries no such gate; RAG/SQL/WEB carry no role
# gate at all (design §2.1/§2.6/§2.7).
CROSS_CASE_TOOL_MODULE_NAMES: list[str] = [
    "src.pipeline.harness.tools.xgraph",
    "src.pipeline.harness.tools.xagg",
    "src.pipeline.harness.tools.xnetwork",
    "src.pipeline.harness.tools.global_search",
]


def module_source(module_name: str) -> str:
    module = importlib.import_module(module_name)
    return Path(module.__file__).read_text(encoding="utf-8")


def all_tool_wrapper_sources() -> dict[str, str]:
    return {name: module_source(name) for name in TOOL_WRAPPER_MODULE_NAMES}
