"""
Harness supervisor — routes a query to exactly one sub-agent.

WHY THIS IS NOT LANGGRAPH (YET)
───────────────────────────────
This is deliberately a small, explicit async state machine rather than a
`langgraph.StateGraph`. LangGraph is not currently a dependency of this project,
and adding it (plus langchain-core and transitives) to a self-hosted,
air-gap-capable deployment is a dependency-footprint decision that was taken
separately rather than smuggled in with a skeleton.

The structure below is deliberately LangGraph-SHAPED so that swap is mechanical
and contained to this one module:

    HarnessState   ≈ the typed graph state object
    _NODES         ≈ the node registry (`graph.add_node`)
    _route()       ≈ the conditional edge function
    invoke()       ≈ `graph.compile().ainvoke()`

Swapping in real LangGraph means rewriting `invoke()` to build a `StateGraph`
over the same `_NODES` and the same `_route()`; nothing in `agents/`, `tools/`,
or `contracts.py` changes, because none of them import anything from here.

[PRESERVE — design §1] The supervisor selects a SUB-AGENT, never a tool
directly. Tools are reachable only from inside a sub-agent's composition logic —
that is what keeps the case/role enforcement points attached to specific call
chains instead of needing re-derivation here.

[PRESERVE — design §4.1, §4.2] The supervisor does NOT authorize case access and
does NOT arm RLS scope. Both are the API boundary's responsibility, performed
before `invoke()` is ever called. A new entry point must re-apply them itself.
"""
from __future__ import annotations

from typing import Awaitable, Callable, Optional

from pydantic import BaseModel, Field

from src.pipeline.harness.agents import semantic_search
from src.pipeline.harness.contracts import (
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
)
from src.pipeline.harness.events import EventRecorder, build_degradation_trace

# ── Node registry (≈ graph.add_node) ─────────────────────────────────────
#
# Only Semantic Search is wired at this stage. The remaining six sub-agents
# from SUBAGENT_INTERFACES.md §2.1 register here as they are implemented; the
# routing function and state shape do not change when they do.

SubAgentCallable = Callable[[SubAgentInput, Optional[EventRecorder]], Awaitable[SubAgentResult]]

_NODES: dict[str, SubAgentCallable] = {
    semantic_search.NAME: semantic_search.run,
}

UNROUTABLE = "__unroutable__"


class HarnessState(BaseModel):
    """
    Typed state threaded through the graph (≈ LangGraph's state object).

    Accumulates rather than replaces: `events` and `result` are written by
    nodes, `agent_input` is read-only input.
    """
    agent_input: SubAgentInput
    selected_agent: Optional[str] = None
    result: Optional[SubAgentResult] = None
    events: list = Field(default_factory=list)


def _route(agent_input: SubAgentInput) -> str:
    """
    Conditional edge: query → sub-agent name.

    STUB ROUTING. Everything currently lands on Semantic Search, because it is
    the only wired node. When the remaining sub-agents are implemented this
    becomes the real classifier — likely reusing `router.py`'s existing
    deterministic override patterns rather than a new one (design §7), which is
    why the decision is isolated in this function.
    """
    return semantic_search.NAME


async def invoke(
    agent_input: SubAgentInput,
    events: Optional[EventRecorder] = None,
    run_id: Optional[str] = None,
    gateway=None,
) -> HarnessState:
    """
    Run one query through the harness (≈ `graph.compile().ainvoke(state)`).

    Returns the terminal `HarnessState`. The caller reads `state.result` for the
    bounded payload and `state.events` for the live trace.

    [PRESERVE — design §6] Emits one `PipelineEvent` per meaningful transition —
    supervisor dispatch, sub-agent start/end, and each tool call — never
    collapsed into a single "the harness ran" event.
    """
    recorder = events or EventRecorder(run_id=run_id, gateway=gateway)
    state = HarnessState(agent_input=agent_input)

    # ── Node: supervisor dispatch ──
    selected = _route(agent_input)
    state.selected_agent = selected

    node = _NODES.get(selected)
    if node is None:
        state.selected_agent = UNROUTABLE
        state.result = SubAgentResult(status=SubAgentStatus.ABSTAINED, answer_text=None)
        # Traced too, so "nothing ran" is a recorded outcome rather than a hole
        # in the run's history — an unroutable query is exactly the kind of
        # thing someone reviewing a run later needs to see.
        await recorder.emit(
            "supervisor:dispatch", "error",
            f"No sub-agent registered for '{selected}'",
            trace=build_degradation_trace(state.result),
        )
        state.events = recorder.events
        return state

    await recorder.emit(
        "supervisor:dispatch", "done", f"Routed to sub-agent: {selected}"
    )

    # ── Node: the selected sub-agent ──
    state.result = await node(agent_input, recorder)

    # ── THE per-query trace write. One place, all seven sub-agents. ──
    #
    # This is deliberately here and not inside any sub-agent. The supervisor
    # invokes nodes generically out of `_NODES` and holds the finished
    # `SubAgentResult` regardless of which one ran, so the trace is emitted by
    # construction rather than by every author remembering to call it. Adding
    # Investigative Analysis / Timeline Building / Cross-Case Linkage requires
    # registering the node and nothing else — there is no path that reaches a
    # sub-agent while bypassing this line.
    #
    # Attached to the EXISTING completion event rather than a new one: §2.2
    # specifies one event per meaningful transition, and a separate trace event
    # would double-count the same transition.
    await recorder.emit(
        "supervisor:complete", "done",
        f"Sub-agent {selected} finished with status={state.result.status.value}",
        trace=build_degradation_trace(state.result),
    )

    state.events = recorder.events
    return state
