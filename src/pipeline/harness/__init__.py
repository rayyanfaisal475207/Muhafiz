"""
Agent harness — two-layer supervisor / sub-agent / tool architecture.

Structure per AGENT_HARNESS_DESIGN.md §1:

    supervisor.py     routes a query to exactly ONE sub-agent
    agents/           Layer 2 — routable sub-agents (compose tools)
    tools/            Layer 1 — primitives, NOT independently routable
    contracts.py      executable interface contracts (SUBAGENT_INTERFACES.md)
    events.py         the §2.2 logging contract
    verifier_gate.py  grounding gate (stub; real signature)

STATUS: skeleton. All seven tools are stubs, and Semantic Search is the only
sub-agent wired through the supervisor. Nothing here imports from
`src.retrieval`, `src.graph`, or `src.pipeline.orchestrator` — the harness is
buildable and testable in complete isolation from the live pipeline, and that
isolation is a property worth keeping.
"""
