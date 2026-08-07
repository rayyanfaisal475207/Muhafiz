"""
docs/SUBAGENT_INTERFACES.md and contracts.py must not drift apart.

`contracts.py` is the executable transcription of that document; the doc is the
human-readable specification. Both halves are only useful if they agree, and
"update both in the same commit" is a rule nothing enforces — so this enforces
it.

Two levels of check, because they catch different failures:

  * NAME level — a type or constant declared in the doc but never transcribed.
    This is how GRAPH_ONLY_SUMMARY_DISCLOSURE and
    PARTIAL_EVIDENCE_DISCLOSURE_TEMPLATE went missing: both were fully
    specified in the doc and simply never made it into code, which only
    surfaced when a sub-agent tried to import one.

  * FIELD level — a class that exists but is quietly missing a field the doc
    declares. Strictly the nastier failure: the import succeeds, the type
    checks, and the gap only appears when something reads the absent attribute.

Deliberate, documented exceptions are listed below rather than being silently
skipped, so adding one is a visible decision.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parents[2]
_DOC = _ROOT / "docs" / "SUBAGENT_INTERFACES.md"
_CONTRACTS = _ROOT / "src" / "pipeline" / "harness" / "contracts.py"


# Names the doc declares that deliberately live outside contracts.py. Each is a
# BOUNDARY INTERFACE (something that acts) rather than a contract type
# (a shape data must have) — see contracts.py's module docstring.
_LIVES_ELSEWHERE = {
    "verify_grounding": "verifier_gate.py — the grounding gate's real signature",
    "log_step": "an existing DataGateway method, called by events.py",
}


def _python_blocks(markdown: str) -> list[str]:
    return re.findall(r"```python\n(.*?)```", markdown, re.S)


def _top_level_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _class_fields(tree: ast.Module) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        fields: list[str] = []
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fields.append(stmt.target.id)
            elif isinstance(stmt, ast.Assign):
                fields.extend(
                    t.id for t in stmt.targets
                    if isinstance(t, ast.Name) and t.id != "model_config"
                )
        out.setdefault(node.name, [])
        for f in fields:
            if f not in out[node.name]:
                out[node.name].append(f)
    return out


def _doc_declarations() -> tuple[set[str], dict[str, list[str]]]:
    """
    Names and per-class fields declared across the doc's Python blocks.

    Blocks are often fragments (a class body shown without its header), so a
    block that will not parse is skipped rather than failing the run — the
    parseable ones still carry the declarations that matter.
    """
    names: set[str] = set()
    fields: dict[str, list[str]] = {}
    for block in _python_blocks(_DOC.read_text(encoding="utf-8")):
        try:
            tree = ast.parse(block)
        except SyntaxError:
            continue
        names |= _top_level_names(tree)
        for cls, flds in _class_fields(tree).items():
            fields.setdefault(cls, [])
            for f in flds:
                if f not in fields[cls]:
                    fields[cls].append(f)
    return names, fields


def _contract_declarations() -> tuple[set[str], dict[str, list[str]]]:
    tree = ast.parse(_CONTRACTS.read_text(encoding="utf-8"))
    return _top_level_names(tree), _class_fields(tree)


def test_doc_has_parseable_python_blocks():
    """Guard the guard: if extraction silently yields nothing, the rest passes vacuously."""
    names, _ = _doc_declarations()
    assert len(names) > 20, f"only found {len(names)} declarations — extraction is broken"


def test_every_documented_type_exists_in_contracts():
    doc_names, _ = _doc_declarations()
    code_names, _ = _contract_declarations()

    missing = sorted(doc_names - code_names - set(_LIVES_ELSEWHERE))

    assert not missing, (
        f"declared in SUBAGENT_INTERFACES.md but absent from contracts.py: {missing}. "
        "The doc and its executable transcription must change together. If one of "
        "these deliberately belongs elsewhere, add it to _LIVES_ELSEWHERE with the "
        "reason rather than deleting this assertion."
    )


def test_every_documented_field_exists_on_its_class():
    """
    The nastier drift: the class imports fine and only breaks when something
    reads the field the doc promised.
    """
    _, doc_fields = _doc_declarations()
    _, code_fields = _contract_declarations()

    problems: list[str] = []
    for cls, fields in sorted(doc_fields.items()):
        if cls not in code_fields:
            if cls not in _LIVES_ELSEWHERE:
                problems.append(f"{cls}: class missing entirely")
            continue
        missing = [f for f in fields if f not in code_fields[cls]]
        if missing:
            problems.append(f"{cls}: missing field(s) {missing}")

    assert not problems, (
        "contracts.py is missing fields the doc declares:\n  "
        + "\n  ".join(problems)
    )


@pytest.mark.parametrize("name,reason", sorted(_LIVES_ELSEWHERE.items()))
def test_documented_exceptions_are_real(name: str, reason: str):
    """
    An exception list rots silently once its entries stop being true. This
    fails if a name in _LIVES_ELSEWHERE gets added to contracts.py after all —
    at which point the exception should be removed, not left to mislead.
    """
    code_names, _ = _contract_declarations()
    assert name not in code_names, (
        f"{name!r} is listed in _LIVES_ELSEWHERE ({reason}) but now exists in "
        "contracts.py. Remove the exception."
    )


def test_web_result_restates_its_fallback_polarity():
    """
    `WebToolResult.fallback_to_rag` duplicates the base-class default on
    purpose: it sits beside three results that pin the flag to Literal[False],
    and stating WEB's opposite polarity explicitly keeps that contrast visible
    where a reader is comparing them. Asserted so a future "remove the
    redundant line" cleanup has to be a deliberate choice.
    """
    _, code_fields = _contract_declarations()
    assert "fallback_to_rag" in code_fields["WebToolResult"]


def test_web_result_fallback_default_tracks_its_base_class():
    """
    Close the hole the re-declaration above opens.

    An explicit override does NOT track the base class. If
    `ToolResult.fallback_to_rag`'s default ever changed, `WebToolResult` would
    silently keep its own — Pydantic treats the override as intentional, so
    nothing would error. The failure mode is invisible in a way that matters:
    WEB would stop signalling fallback, queries would quietly not degrade to
    RAG, and no behavioural test would catch it, because
    `test_real_web_falls_back_to_rag_only_after_both_tiers` asserts on a value
    the tool sets EXPLICITLY and so passes regardless of the base default.

    The three cross-case results need no equivalent guard: `Literal[False]`
    makes a conflicting base change a type error. `WebToolResult` is a plain
    `bool`, so this assertion is what makes its redundancy self-checking rather
    than a silent-drift hazard held together by a docstring.

    If this fails, do not "fix" it by editing the expected value — decide
    whether the base default was meant to change, and whether WEB should follow.
    """
    from src.pipeline.harness.contracts import ToolResult, WebToolResult

    base_default = ToolResult.model_fields["fallback_to_rag"].default
    web_default = WebToolResult.model_fields["fallback_to_rag"].default

    assert web_default == base_default, (
        f"WebToolResult.fallback_to_rag defaults to {web_default!r} but its base "
        f"ToolResult defaults to {base_default!r}. The re-declaration on "
        "WebToolResult is meant to RESTATE the inherited default for readability, "
        "not to diverge from it."
    )
