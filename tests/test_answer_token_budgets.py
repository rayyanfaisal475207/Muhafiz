"""
Finding C regression — every sub-agent prose answer must set an explicit
completion budget.

`call_llm()` defaults to 1000 tokens, which truncates a multi-section case
answer mid-sentence. Because it is a DEFAULT, a call site that simply omits
`max_tokens` inherits it invisibly — nothing fails, the answer just stops.
That is why truncation kept reappearing on a different path after each
targeted fix: seven prose call sites across six sub-agents were still relying
on the default, including Case Summarization and Report Drafting.

This test fails if any sub-agent adds a prose `call_llm()` without a budget,
so the class of bug cannot come back one module at a time.

Structured/JSON generation (map steps, decomposers, extractors) is out of
scope — those use `call_llm_json()` and carry their own deliberately
different budgets and cost ceilings.
"""
from __future__ import annotations

import pathlib
import re

import pytest

AGENTS_DIR = pathlib.Path("src/pipeline/harness/agents")
_CALL_RE = re.compile(r"await call_llm\((.*?)\)\n", re.DOTALL)


def _agent_modules():
    return sorted(p for p in AGENTS_DIR.glob("*.py") if p.name != "__init__.py")


@pytest.mark.parametrize("path", _agent_modules(), ids=lambda p: p.name)
def test_every_prose_call_llm_sets_max_tokens(path):
    source = path.read_text(encoding="utf-8")
    offenders = [
        source[:m.start()].count("\n") + 1
        for m in _CALL_RE.finditer(source)
        if "max_tokens" not in m.group(1)
    ]
    assert not offenders, (
        f"{path.name} calls call_llm() without max_tokens at line(s) {offenders} — "
        f"it will silently inherit the 1000-token default and truncate long answers. "
        f"Pass max_tokens=ANSWER_MAX_TOKENS (src.pipeline.harness.types)."
    )


def test_shared_budget_is_large_enough_for_multi_section_answers():
    from src.pipeline.harness.types import ANSWER_MAX_TOKENS

    # Matches the legacy routes that produce the same shape of answer
    # (orchestrator's _RAG_ANSWER_MAX_TOKENS=3000 / _GRAPH=2600).
    assert ANSWER_MAX_TOKENS >= 2600


def test_global_search_reduce_step_has_its_own_budget():
    """Global Search synthesizes across every community report, so it produces
    the longest answer in the system — it was the last path still truncating."""
    from src.pipeline.harness.agents.global_search import _FINAL_ANSWER_MAX_TOKENS

    assert _FINAL_ANSWER_MAX_TOKENS >= 2600
