"""
Layer 2 — routable sub-agents.

Each hands the supervisor a BOUNDED, SUMMARIZED payload ([PRESERVE — design
§3]) — never raw retrieved chunks, raw rows, or full conversation history.

Wired so far: Semantic Search. The remaining six from SUBAGENT_INTERFACES.md
§2.1 (Case Summarization, Report Drafting, Investigative Analysis, Timeline
Building, Cross-Case Linkage, Large-Scale Aggregate) are specified but not yet
implemented.
"""
