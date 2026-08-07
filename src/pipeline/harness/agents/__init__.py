"""
Agent Harness — sub-agents (`src/pipeline/harness/agents/`).

Each module here implements one of the 8 sub-agent names from
AGENT_HARNESS_IMPLEMENTATION_PLAN.md §4 and registers itself into
`src.pipeline.harness.supervisor`'s module-level registry at import time
(the pattern documented in `supervisor.py`'s own module docstring). This
package intentionally has no other content — importing a sub-agent module
is what makes it dispatchable; this `__init__.py` does not import them
itself, so a caller opts in to exactly the sub-agents it wants live.
"""
