"""
AST-based helpers for the compliance suite.

Two jobs neither a plain substring search nor a naive `text.find()` can do
safely:

  1. `strip_docstrings_and_comments()` — a tool wrapper's own [PRESERVE]
     docstrings legitimately NAME forbidden symbols in prose (e.g.
     explaining "this wrapper must never call current_rls_active.set()").
     A raw substring scan can't tell that prose apart from a real call
     site, and would flag the very documentation written to prevent the
     violation. Stripping every string literal and comment down to real
     code tokens removes that false-positive source without weakening the
     check on actual code.

  2. `find_function()` / `call_line_numbers()` — verifying "A happens
     before B" inside one specific function needs real line numbers from
     the actual call sites, not the position of the first substring match
     anywhere in the file (a comment mentioning `process_query()` earlier
     in the file, outside the function being checked, would otherwise
     produce a false failure — or worse, mask a true one).
"""

from __future__ import annotations

import ast
import io
import tokenize
from typing import Optional


def strip_docstrings_and_comments(source: str) -> str:
    """
    Returns `source` with every string-literal token and comment token
    replaced by a same-shaped blank — real code tokens (names, operators,
    keywords) are untouched, so a forbidden identifier used in actual code
    still matches; the same identifier only ever mentioned in a docstring
    or comment no longer does.
    """
    out_tokens = []
    try:
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT, tokenize.FSTRING_START, tokenize.FSTRING_MIDDLE, tokenize.FSTRING_END):
                continue
            out_tokens.append(tok.string)
    except AttributeError:
        # Python < 3.12 has no FSTRING_* token types.
        out_tokens = []
        for tok in tokenize.generate_tokens(io.StringIO(source).readline):
            if tok.type in (tokenize.STRING, tokenize.COMMENT):
                continue
            out_tokens.append(tok.string)
    return " ".join(out_tokens)


def find_function(tree: ast.AST, name: str) -> Optional[ast.AST]:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def call_line_numbers(func_node: ast.AST, attr_or_name: str) -> list[int]:
    """Line numbers of every `attr_or_name(...)` or `x.attr_or_name(...)` call inside func_node."""
    lines: list[int] = []
    for node in ast.walk(func_node):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute) and f.attr == attr_or_name:
                lines.append(node.lineno)
            elif isinstance(f, ast.Name) and f.id == attr_or_name:
                lines.append(node.lineno)
    return lines
