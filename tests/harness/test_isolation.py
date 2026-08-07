"""
The harness must stay buildable and testable in isolation from the live
pipeline.

This is a structural guard, not a style preference: the harness is a
restructuring of the orchestrator, and the moment it imports the thing it is
replacing at MODULE SCOPE, the two can no longer be developed or tested
independently.

`tools/real.py` is the one deliberate exception. Its entire job is to adapt
production retrieval/graph/gateway/web code behind the harness contracts, so it
must reach that code — but it does so via FUNCTION-LOCAL imports, so importing
the harness package still costs nothing and still works without the production
dependency tree present. The check below is therefore scoped to module-level
imports, which is exactly the property that matters.

`orchestrator.py` remains off-limits to every harness module including
`real.py`: the harness runs alongside the legacy path, never through it.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_HARNESS = Path(__file__).resolve().parents[2] / "src" / "pipeline" / "harness"

# Production modules the harness must not reach into at this stage.
_FORBIDDEN_PREFIXES = (
    "src.retrieval",
    "src.graph",
    "src.pipeline.orchestrator",
    "src.pipeline.router",
    "src.pipeline.verifier",
    "src.mcp",
)


def _harness_modules() -> list[Path]:
    return sorted(_HARNESS.rglob("*.py"))


def test_harness_has_modules_to_check():
    assert _harness_modules(), "no harness modules found — path is wrong"


def _module_level_imports(tree: ast.Module) -> list[str]:
    """
    Imports at MODULE scope only — not those nested inside a function body.

    Function-local imports are how `real.py` reaches production code without
    making the harness package depend on it at import time.
    """
    imported: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            imported.append(node.module)
    return imported


@pytest.mark.parametrize("path", _harness_modules(), ids=lambda p: p.name)
def test_harness_module_does_not_import_live_pipeline(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    violations = [
        name for name in _module_level_imports(tree)
        if any(name == p or name.startswith(p + ".") for p in _FORBIDDEN_PREFIXES)
    ]

    assert not violations, (
        f"{path.name} imports live pipeline code at module scope: {violations}. "
        "The harness must remain independently buildable and testable — use a "
        "function-local import (as tools/real.py does) if a real adapter needs it."
    )


def test_no_harness_module_imports_the_legacy_orchestrator_at_all():
    """
    The harness runs ALONGSIDE orchestrator.py, never through it — so unlike the
    other production modules, the orchestrator is forbidden even as a
    function-local import.
    """
    offenders: list[str] = []
    for path in _harness_modules():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            if any(n == "src.pipeline.orchestrator" for n in names):
                offenders.append(path.name)

    assert not offenders, (
        f"{offenders} import the legacy orchestrator. The harness must not depend "
        "on the code path it will eventually replace."
    )


def test_harness_package_imports_without_production_dependencies():
    """
    Importing the harness must not pull in retrieval/graph/gateway at module
    scope — the property that keeps `tests/harness/` runnable with no database,
    no model server, and no network.
    """
    import subprocess
    import sys

    repo_root = Path(__file__).resolve().parents[2]
    code = (
        "import sys;"
        "import src.pipeline.harness.supervisor;"
        "import src.pipeline.harness.tools.registry;"
        "bad=[m for m in sys.modules if m.startswith(('src.retrieval','src.graph','src.data_gateway'))];"
        "print('LEAKED:'+','.join(sorted(bad)) if bad else 'CLEAN')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=repo_root, capture_output=True, text=True
    )
    assert "CLEAN" in result.stdout, (
        f"Importing the harness pulled in production modules: {result.stdout.strip()} "
        f"{result.stderr.strip()}"
    )


def test_harness_does_not_import_langgraph():
    """
    LangGraph is deliberately not a dependency yet — the supervisor is
    LangGraph-SHAPED but vendor-neutral. If this starts failing, the dependency
    decision was made; update requirements.txt and this test together.
    """
    for path in _harness_modules():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(a.name.startswith("langgraph") for a in node.names), path.name
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith("langgraph"), path.name
