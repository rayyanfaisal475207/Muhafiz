"""
Enforcement point 1 (AGENT_HARNESS_DESIGN.md §4.1) — API boundary hard 403.

`main.py`'s `chat_endpoint()` calls `gateway.check_case_access(case_id,
user_id, current_user.role)` and raises `403` BEFORE `process_query()` (or,
post-harness, the supervisor) is ever invoked. This is entirely existing
code Phase 0 does not touch — the check here is a regression guard (the
harness must not accidentally weaken it) plus a check that Phase 0's own
new code doesn't add a competing, unguarded entry point of its own.

Ordering is checked via real call-site line numbers inside `chat_endpoint`
specifically (see `_ast_scan.py`), not `str.find()` — the file has other,
unrelated mentions of both names (a comment earlier in the file, and a
SECOND, unrelated `check_case_access()` call in the file-download endpoint)
that a substring search would trip over.
"""

import ast
from pathlib import Path

import pytest

from src.pipeline.harness.compliance._ast_scan import call_line_numbers, find_function
from src.pipeline.harness.compliance._source_scan import all_tool_wrapper_sources

_REPO_ROOT = Path(__file__).resolve().parents[4]
_MAIN_PY = _REPO_ROOT / "src" / "main.py"


def test_main_py_still_exists_at_the_expected_path():
    # Fails loudly (not silently skips) if main.py moves — every other
    # assertion in this module is meaningless if this one is wrong.
    assert _MAIN_PY.is_file(), f"Expected the API boundary at {_MAIN_PY}"


def _chat_endpoint_node() -> ast.AST:
    tree = ast.parse(_MAIN_PY.read_text(encoding="utf-8"))
    func = find_function(tree, "chat_endpoint")
    assert func is not None, (
        "main.py no longer defines chat_endpoint() — has the chat entry "
        "point been renamed or moved?"
    )
    return func


def test_chat_endpoint_calls_check_case_access_before_process_query():
    func = _chat_endpoint_node()
    check_lines = call_line_numbers(func, "check_case_access")
    process_lines = call_line_numbers(func, "process_query")
    assert check_lines, (
        "chat_endpoint() no longer calls gateway.check_case_access() — "
        "enforcement point 1 (API boundary hard 403, design §4.1) appears "
        "to have been removed."
    )
    assert process_lines, "chat_endpoint() no longer calls process_query() — has the pipeline entry point moved?"
    assert min(check_lines) < min(process_lines), (
        "gateway.check_case_access() must run BEFORE process_query() is "
        "invoked inside chat_endpoint() (design §4.1) — found it running "
        "after instead, which means an unauthorized request could reach "
        "the pipeline."
    )


def test_chat_endpoint_raises_403_on_denied_access():
    func = _chat_endpoint_node()
    source = ast.get_source_segment(_MAIN_PY.read_text(encoding="utf-8"), func) or ""
    assert "403" in source, (
        "chat_endpoint() no longer raises a 403 anywhere in its body — the "
        "hard-403 requirement (design §4.1) appears to be gone."
    )


@pytest.mark.parametrize("module_name,source", all_tool_wrapper_sources().items())
def test_tool_wrapper_introduces_no_new_api_entry_point(module_name, source):
    """
    [PRESERVE — design §4.1] "If the harness adds any new entry point ...
    it needs this exact check re-applied at its own boundary — it is not
    inherited automatically." Phase 0 adds none: no tool wrapper may define
    a FastAPI route, app, or router of its own.
    """
    lowered = source.lower()
    for forbidden in ("@app.", "apirouter", "fastapi"):
        assert forbidden not in lowered, (
            f"{module_name} appears to define its own API entry point "
            f"({forbidden!r} found) — any new entry point must re-apply "
            "the check_case_access()/403 boundary itself (design §4.1); "
            "it is not inherited from main.py automatically."
        )
