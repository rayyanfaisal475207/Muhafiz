"""
Tests for the evaluation script itself.

The script is a reporting tool, and a reporting tool that quietly starts
flattering the system is worse than no tool at all. These lock down the two
things it must never get wrong:

  * a security failure or a dangling citation must NEVER read as a pass
  * every scenario must name a sub-agent that actually exists

They deliberately do NOT run the script against live infrastructure — that is
what `python scripts/evaluate_harness.py` is for, and it needs Postgres, AGE,
Chroma and the model server. These are pure-function tests over its verdict
logic and its scenario table.
"""
from __future__ import annotations

import importlib.util
import os

import pytest

_SCRIPT = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "scripts", "evaluate_harness.py",
)


def _load_module():
    """
    Import the script by path — `scripts/` is not a package.

    Registered in `sys.modules` BEFORE `exec_module`, because `@dataclass`
    resolves annotations via `sys.modules[cls.__module__].__dict__`; without
    that the decorator raises on a module that is still mid-import.
    """
    import sys

    if "evaluate_harness" in sys.modules:
        return sys.modules["evaluate_harness"]
    spec = importlib.util.spec_from_file_location("evaluate_harness", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["evaluate_harness"] = module
    spec.loader.exec_module(module)
    return module


ev = _load_module()


def _outcome(**kwargs):
    base = dict(
        scenario_id="s", question="q", demonstrates="d", role="investigator",
        routed_as_expected=True, status="ok",
    )
    base.update(kwargs)
    return ev.Outcome(**base)


def _scenario(**kwargs):
    base = dict(
        id="s", question="q", role=ev.Role.INVESTIGATOR, case_id="CASE-1",
        expect_sub_agent="semantic_search", demonstrates="d",
    )
    base.update(kwargs)
    return ev.Scenario(**base)


# ── The verdict logic must not hide failures ──────────────────────────────

def test_answering_a_query_that_should_be_denied_is_a_security_failure():
    """
    The inverted case. If a role gate stops working, an investigator gets
    cross-case data — and a naive "did it return OK" check would call that a
    pass. This is the single most important assertion in this file.
    """
    outcome = _outcome(status="ok", answer_text="here are other cases")
    verdict = ev.verdict_for(outcome, _scenario(expect_denial=True))
    assert verdict == "SECURITY-FAIL"


def test_being_denied_when_denial_is_expected_is_a_pass():
    outcome = _outcome(status="denied", error="permission_denied: needs supervisor")
    assert ev.verdict_for(outcome, _scenario(expect_denial=True)) == "PASS"


def test_dangling_citation_markers_fail():
    """A [Document 7] that resolves to nothing is a claim with no source."""
    outcome = _outcome(citation_markers_resolve=False, dangling_markers=[7])
    assert ev.verdict_for(outcome, _scenario()) == "BAD-CITES"


def test_reaching_the_wrong_sub_agent_fails():
    outcome = _outcome(routed_as_expected=False)
    assert ev.verdict_for(outcome, _scenario()) == "MISROUTED"


def test_an_exception_fails_even_if_other_fields_look_healthy():
    outcome = _outcome(status="ok", exception="RuntimeError: boom")
    assert ev.verdict_for(outcome, _scenario()) == "CRASH"


def test_partial_is_not_reported_as_a_clean_pass():
    """
    PARTIAL means a source was missing and the sub-agent said so. Rendering it
    as PASS would hide the very thing the status exists to communicate.
    """
    outcome = _outcome(status="partial", degraded_from=["RAG"])
    assert ev.verdict_for(outcome, _scenario()) == "PARTIAL"


def test_empty_distinguishes_no_match_from_no_data():
    """
    "We searched and found nothing" and "we could not search" are different
    facts. The first is the system working; the second is a deployment gap.
    """
    found_leads = _outcome(status="empty", cross_case_links=3)
    assert ev.verdict_for(found_leads, _scenario()) == "NO-MATCH"

    could_not_search = _outcome(status="empty", degraded_from=["XNETWORK"])
    assert ev.verdict_for(could_not_search, _scenario()) == "NO-DATA"


def test_direct_route_passes_without_a_sub_agent_result():
    """
    DIRECT deliberately produces no SubAgentResult. Treating a missing result
    as a failure would penalise the route for behaving as designed.
    """
    outcome = _outcome(status="NO_SUB_AGENT")
    assert ev.verdict_for(outcome, _scenario(expect_sub_agent=None)) == "PASS"


# ── Citation marker checking ──────────────────────────────────────────────

@pytest.mark.parametrize("answer,count,expected,dangling", [
    ("Claim [Document 1] and [Document 2].", 2, True, []),
    ("Claim [Document 1] and [Document 5].", 2, False, [5]),
    ("Claim [Document 0].", 2, False, [0]),
    ("No markers at all.", 2, None, []),      # nothing to verify
    ("Claim [Document 1].", 0, None, []),     # no citations to check against
    ("", 3, None, []),                        # no prose
])
def test_check_citation_markers(answer, count, expected, dangling):
    resolves, found = ev.check_citation_markers(answer, count)
    assert resolves is expected
    assert found == dangling


def test_no_markers_is_not_reported_as_a_pass():
    """
    "Nothing to verify" must stay distinguishable from "verified clean" — an
    answer citing nothing has not demonstrated grounding.
    """
    resolves, _ = ev.check_citation_markers("A claim with no citation.", 3)
    assert resolves is None
    assert resolves is not True


# ── The scenario table ────────────────────────────────────────────────────

def test_every_scenario_targets_a_real_sub_agent():
    """A typo in `expect_sub_agent` would report a permanent false MISROUTED."""
    from src.pipeline.harness import classifier, supervisor

    valid = set(supervisor._NODES) | {classifier.NO_SUB_AGENT, None}
    for scenario in ev.SCENARIOS:
        assert scenario.expect_sub_agent in valid, (
            f"scenario {scenario.id!r} expects {scenario.expect_sub_agent!r}, "
            f"which is not a registered sub-agent"
        )


def test_scenarios_cover_every_sub_agent():
    """
    An evaluation that silently stopped exercising a sub-agent would still
    report all-green. Coverage is asserted, not assumed.
    """
    from src.pipeline.harness import supervisor

    covered = {s.expect_sub_agent for s in ev.SCENARIOS}
    missing = set(supervisor._NODES) - covered
    assert not missing, f"no scenario exercises: {sorted(missing)}"


def test_the_role_gate_is_actually_exercised():
    """
    Security behaviour must be demonstrated, not assumed. A cross-case scenario
    run as an investigator is the only thing here that proves the gate holds.
    """
    denials = [s for s in ev.SCENARIOS if s.expect_denial]
    assert denials, "no scenario asserts a role-gate denial"
    for scenario in denials:
        assert scenario.role is ev.Role.INVESTIGATOR, (
            "a denial scenario must run as the UNDER-privileged role, or it "
            "proves nothing"
        )


def test_scenario_ids_are_unique():
    ids = [s.id for s in ev.SCENARIOS]
    assert len(ids) == len(set(ids))
