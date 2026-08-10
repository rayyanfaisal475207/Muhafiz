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

from src.pipeline.harness import classifier
from src.pipeline.harness.agents import (
    aggregate_analysis,
    case_summary,
    cross_case_linkage,
    investigative_analysis,
    report_draft,
    semantic_search,
    timeline,
)
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

# `gateway` is threaded through because five of the seven sub-agents accept one
# (Report Drafting, Investigative Analysis, Timeline Building, Cross-Case
# Linkage, Large-Scale Aggregate) and every consumer of it falls back to
# `get_gateway()` when it is None. Production behaviour is therefore unchanged
# either way — but without forwarding it, `invoke()` could not be driven
# against a fake gateway, forcing tests to wrap a node in a binding closure
# just to inject one. Keyword-only at the call site so a node that does not
# take it is unaffected.
SubAgentCallable = Callable[..., Awaitable[SubAgentResult]]

_NODES: dict[str, SubAgentCallable] = {
    semantic_search.NAME: semantic_search.run,
    case_summary.NAME: case_summary.run,
    report_draft.NAME: report_draft.run,
    investigative_analysis.NAME: investigative_analysis.run,
    timeline.NAME: timeline.run,
    cross_case_linkage.NAME: cross_case_linkage.run,
    aggregate_analysis.NAME: aggregate_analysis.run,
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


async def _call_node(
    node: SubAgentCallable,
    agent_input: SubAgentInput,
    recorder: EventRecorder,
    gateway,
) -> SubAgentResult:
    """
    Invoke a sub-agent, passing `gateway` only if it declares the parameter.

    Five of the seven take one; Semantic Search and Case Summarization do not,
    and passing it to them unconditionally would raise TypeError. Rather than
    forcing all seven to accept a parameter most of them would ignore, this
    inspects the node once per call.

    A node wrapped in a closure (as tests do) that accepts `**kwargs` also
    receives it, which is the intended behaviour — the check is for whether the
    callee can take it, not whether it was written to.
    """
    import inspect

    if gateway is not None:
        try:
            params = inspect.signature(node).parameters
            takes_gateway = "gateway" in params or any(
                p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()
            )
        except (TypeError, ValueError):  # pragma: no cover - exotic callables
            takes_gateway = False
        if takes_gateway:
            return await node(agent_input, recorder, gateway=gateway)

    return await node(agent_input, recorder)


def _route(agent_input: SubAgentInput, route_result: dict) -> str:
    """
    Conditional edge: router decision → sub-agent name.

    `route_result` is exactly what `route_query()` returns, and is REQUIRED —
    deliberately not optional. An optional parameter would need a fallback, and
    both available fallbacks are worse than forcing the caller to supply it:
    calling `route_query()` here would mean a second LLM classification call
    per query, and defaulting to a route would silently misroute. Requiring it
    also guarantees the harness's routing can never disagree with the caller's
    own routing for the same query.

    The classification itself lives in `classifier.classify()`; this stays a
    one-line seam so the decision remains isolated in a single place.
    """
    return classifier.classify(route_result, agent_input.query_text)


async def invoke(
    agent_input: SubAgentInput,
    route_result: dict,
    events: Optional[EventRecorder] = None,
    run_id: Optional[str] = None,
    gateway=None,
) -> HarnessState:
    """
    Run one query through the harness (≈ `graph.compile().ainvoke(state)`).

    Returns the terminal `HarnessState`. The caller reads `state.result` for the
    bounded payload and `state.events` for the live trace.

    `gateway` serves two purposes and is optional for both. It backs the
    `EventRecorder`'s durable `log_step` writes, and it is FORWARDED to the
    selected sub-agent when that sub-agent declares the parameter (see
    `_call_node`). Previously it did only the first, so a sub-agent needing a
    gateway silently fell back to `get_gateway()` — correct in production, but
    it left `invoke()` undrivable against a fake, forcing tests to wrap nodes
    in binding closures purely to inject one.

    [PRESERVE — design §6] Emits one `PipelineEvent` per meaningful transition —
    supervisor dispatch, sub-agent start/end, and each tool call — never
    collapsed into a single "the harness ran" event.
    """
    recorder = events or EventRecorder(run_id=run_id, gateway=gateway)
    state = HarnessState(agent_input=agent_input)

    # ── Node: supervisor dispatch ──
    selected = _route(agent_input, route_result)
    state.selected_agent = selected

    # ── DIRECT: no sub-agent, and NOT an abstention ──
    # DIRECT answers from general knowledge with no retrieval, and the Verifier
    # deliberately never gates it (design §1). It is definitionally outside the
    # harness's retrieval-and-verification scope, so this returns early and the
    # CALLER answers it on its existing path.
    #
    # `result` is None rather than a SubAgentResult of any status: ABSTAINED
    # means "could not answer safely", which is the opposite of what happened
    # here. Forcing DIRECT into the SubAgentResult shape would also break
    # streaming — DIRECT streams tokens as they generate, while a
    # SubAgentResult carries a completed answer_text, so wrapping it would mean
    # buffering the whole response and regressing latency on the fastest route.
    #
    # `skipped` matches the legacy path's own step-log vocabulary for this
    # route, where retrieval/reranker/evaluator are all emitted as skipped.
    if selected == classifier.NO_SUB_AGENT:
        await recorder.emit(
            "supervisor:dispatch", "skipped",
            "DIRECT — answered without retrieval, no sub-agent",
        )
        state.result = None
        state.events = recorder.events
        return state

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

    # `describe()` explains WHY this sub-agent was chosen (route, a file-format
    # request, the timeline tier, or a case_scope demotion). Without it the
    # trace shows only the destination, which is the least useful half when
    # someone is asking why a query went where it did.
    decision = classifier.describe(route_result, agent_input.query_text)
    await recorder.emit(
        "supervisor:dispatch", "done",
        f"Routed to sub-agent: {selected} ({decision['basis']})",
    )

    # ── Node: the selected sub-agent ──
    # `gateway` is forwarded only to nodes that declare it. Semantic Search and
    # Case Summarization take just (agent_input, events), so passing it
    # unconditionally would TypeError — the same stub/real signature-divergence
    # class of bug this threading was added alongside. Introspecting the node
    # keeps `SubAgentCallable` a loose protocol rather than forcing all seven
    # to grow a parameter five of them use and two do not.
    state.result = await _call_node(node, agent_input, recorder, gateway)

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
