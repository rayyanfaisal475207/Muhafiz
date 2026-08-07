"""
Agent Harness (Phase 0 — foundation layer).

Additive, coexists with src/pipeline/orchestrator.py and src/pipeline/router.py
(see AGENT_HARNESS_IMPLEMENTATION_PLAN.md §6, Rollout strategy). Nothing in
this package is wired into the live chat pipeline yet — it does not replace,
call, or get called by orchestrator.py/router.py in this phase.
"""
