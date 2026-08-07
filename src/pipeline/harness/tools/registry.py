"""
Tool registry — the single place that decides which implementation of each
primitive is in play.

Sub-agents call `registry.rag_tool(...)`, never a concrete module, so swapping a
stub for its real implementation is a one-line change here rather than an edit
to every call site.

DEFAULT: real implementations (`real.py`), wrapping the production retrieval,
graph, gateway, and web-search code.

The stub implementations (`stubs.py`) are retained deliberately — they are not
dead code. They back `tests/harness/`, which must keep running without a
database, a model server, or network access, and they are what makes the
contract tests (`fallback_to_rag` polarity, role-gate ordering, source_tool
tagging) meaningful without live infrastructure. `use_stubs()` is the seam
those tests use.
"""
from __future__ import annotations

from src.pipeline.harness.tools import real, stubs

# Which module backs each tool right now. Flipped wholesale by use_stubs() /
# use_real(); individual entries may be overridden for a targeted test.
_ACTIVE: dict[str, object] = {}


def _install(module_map: dict[str, object]) -> None:
    _ACTIVE.clear()
    _ACTIVE.update(module_map)


# Tools are being migrated from stub to real ONE AT A TIME, each verified
# against the full baseline before the next. Anything not yet migrated falls
# back to its stub, so the harness stays importable and testable throughout —
# `getattr(real, name, stubs_fn)` is what makes a partially-migrated state
# legal rather than a collection error.
_TOOL_NAMES = (
    "rag_tool", "graph_tool", "xgraph_tool", "xagg_tool",
    "xnetwork_tool", "sql_tool", "web_tool",
)

_STUB_MAP_BASE: dict[str, object] = {
    name: getattr(stubs, name) for name in _TOOL_NAMES
}

_REAL_MAP: dict[str, object] = {
    name: getattr(real, name, _STUB_MAP_BASE[name]) for name in _TOOL_NAMES
}

_STUB_MAP: dict[str, object] = dict(_STUB_MAP_BASE)

_install(_REAL_MAP)


def migrated_tools() -> list[str]:
    """Names of tools whose real implementation exists (migration progress)."""
    return [n for n in _TOOL_NAMES if hasattr(real, n)]


def use_stubs() -> None:
    """Point every tool at its stub. Used by the harness's own tests."""
    _install(_STUB_MAP)


def use_real() -> None:
    """Point every tool at its real implementation (the default)."""
    _install(_REAL_MAP)


def is_real() -> bool:
    """True when the real implementations are installed."""
    return _ACTIVE.get("rag_tool") is real.rag_tool


# ── Dispatchers ──────────────────────────────────────────────────────────
# Thin pass-throughs so callers bind to the registry, not to a module. Keeping
# these as functions (rather than re-exported names) is what lets the swap take
# effect for callers that imported the symbol earlier.

async def rag_tool(*args, **kwargs):
    return await _ACTIVE["rag_tool"](*args, **kwargs)


async def graph_tool(*args, **kwargs):
    return await _ACTIVE["graph_tool"](*args, **kwargs)


async def xgraph_tool(*args, **kwargs):
    return await _ACTIVE["xgraph_tool"](*args, **kwargs)


async def xagg_tool(*args, **kwargs):
    return await _ACTIVE["xagg_tool"](*args, **kwargs)


async def xnetwork_tool(*args, **kwargs):
    return await _ACTIVE["xnetwork_tool"](*args, **kwargs)


async def sql_tool(*args, **kwargs):
    return await _ACTIVE["sql_tool"](*args, **kwargs)


async def web_tool(*args, **kwargs):
    return await _ACTIVE["web_tool"](*args, **kwargs)
