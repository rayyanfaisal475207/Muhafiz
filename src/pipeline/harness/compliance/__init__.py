"""
Agent Harness compliance suite (Phase 0, foundation layer).

Automated checks for the 5 independent enforcement points
AGENT_HARNESS_DESIGN.md §4 requires every harness tool to respect. None of
these 5 points supersedes another and none is a substitute for the others
— see §4's own framing ("There is no unified access-control layer to hook
into"). This suite checks each independently, matching that structure:
none of these test modules assumes another one already covers its ground.

Run with:  pytest src/pipeline/harness/compliance/

(Not under tests/ and not added to pytest.ini's `testpaths` — this suite
checks harness-internal code shape, not application behavior, and is
meant to be run explicitly, e.g. as its own CI/merge-gate step per
AGENT_HARNESS_IMPLEMENTATION_PLAN.md §8's "wire compliance suite into CI"
item — which is its own, later checklist item, not part of this phase.)
"""
