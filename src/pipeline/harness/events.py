"""
Event emission for the harness — the §2.2 logging contract.

Two sinks, both preserved from the current pipeline (SUBAGENT_INTERFACES.md
§2.2):

  1. `PipelineEvent` objects, yielded live to the chat UI's trace panel. Never
     touches a database.
  2. `log_step(...)` into Postgres `pipeline_steps`, which is the ONLY source
     the admin dashboard's Run History page reads back.

This module owns sink 1 and the remapping into sink 2's vocabulary. It does NOT
own the Postgres write itself — the gateway is injected, so the harness stays
testable with no database. At stub stage the gateway is optional and defaults
to None (events collected in memory only).

[PRESERVE — design §6] Granularity must not regress. One event per meaningful
transition, not one per sub-agent invocation.
"""
from __future__ import annotations

import time
from contextlib import asynccontextmanager
from typing import Any, Optional

from src.pipeline.harness.contracts import PipelineEvent, SubAgentResult, to_step_status


# Schema version for the structured trace payload. Bump when the SHAPE changes
# in a way a reader must adapt to — `pipeline_steps` rows are historical
# records, so a renderer will encounter payloads written by older code
# indefinitely and needs to know which contract it is looking at.
TRACE_PAYLOAD_VERSION = 1


def build_degradation_trace(result: SubAgentResult) -> dict:
    """
    Build the structured per-query trace payload from a finished sub-agent.

    ONE SHAPE FOR ALL SEVEN SUB-AGENTS. The fields read here
    (`tools_used`/`degraded_from`/`caveats`/`status`) live on `SubAgentResult`
    itself, not on any particular sub-agent, so this produces an identical
    payload regardless of which one ran. Nothing per-sub-agent belongs in here —
    a future Investigative Analysis or Timeline Building gets traced correctly
    without its author wiring anything up.

    THE OVERLAP CASE, WHICH READERS MUST HANDLE
    ───────────────────────────────────────────
    A tool can appear in BOTH `tools_used` and `degraded_from`. That is not a
    bug and must not be normalized away: it means the tool CONTRIBUTED DATA but
    degraded internally while doing so — Case Summarization already produces
    this when RAG returns evidence with its relevance gate unavailable
    (c4a06fe). So `degraded_from` membership does NOT imply "did not
    contribute", and a renderer that assumes the two lists are disjoint will
    misreport that case.

    `contributed_only` and `degraded_only` are precomputed here precisely so
    every consumer derives the three-way split identically instead of each
    reimplementing the set arithmetic — and so the overlap is impossible to
    miss by accident.

    This shape is deliberately sized for the hardest known case ahead of it:
    Investigative Analysis composes RAG + GRAPH + SQL where GRAPH and SQL both
    degrade TO RAG, so a three-tool call can collapse to one effective source
    ([RESOLVED-4]). The payload keeps "what was attempted" and "what actually
    paid off" separable, which is the whole point of tracing it — no
    payload-shape change should be needed when that sub-agent lands.
    """
    used = list(result.tools_used)
    degraded = list(result.degraded_from)
    used_set, degraded_set = set(used), set(degraded)

    return {
        "v": TRACE_PAYLOAD_VERSION,
        "sub_agent_status": result.status.value,
        # Raw contract fields, verbatim — a reader that wants the originals
        # never has to reconstruct them from the derived views below.
        "tools_used": used,
        "degraded_from": degraded,
        # Derived three-way split. Order preserved from `tools_used` /
        # `degraded_from` so output is stable across runs.
        "contributed_only": [t for t in used if t not in degraded_set],
        "degraded_and_contributed": [t for t in used if t in degraded_set],
        "degraded_only": [t for t in degraded if t not in used_set],
        "caveats": list(result.caveats),
        # Asserts the DOCUMENT itself discloses its partiality (RESOLVED-3).
        # None when this sub-agent produced no file.
        "disclosure_rendered": (
            result.generated_file.disclosure_rendered
            if result.generated_file is not None else None
        ),
    }


class EventRecorder:
    """
    Collects `PipelineEvent`s and mirrors them to a durable step log.

    Used by the supervisor, every sub-agent, and every tool. A single recorder
    instance threads through one query's execution, so the emitted sequence is
    the trace for that query.

    `gateway` is optional: when None (tests, and the current stub stage) events
    are recorded in memory only and nothing is persisted. When supplied it must
    expose the existing `log_step(run_id, step_name, step_order, status,
    duration_ms, output_summary)` signature — deliberately unchanged from the
    live pipeline so this drops into the real one without a migration.
    """

    def __init__(self, run_id: Optional[str] = None, gateway: Any = None) -> None:
        self.run_id = run_id
        self._gateway = gateway
        self.events: list[PipelineEvent] = []
        self._step_order = 0

    async def emit(
        self,
        step: str,
        status: str,
        detail: str,
        ms: Optional[int] = None,
        sources: Optional[list[dict[str, Any]]] = None,
        **kwargs: Any,
    ) -> PipelineEvent:
        """
        Record one state transition.

        `status` uses the five-value SSE vocabulary
        (`active`/`done`/`error`/`retry`/`skipped`) — the same vocabulary the
        live pipeline already emits. No new values.
        """
        event = PipelineEvent(
            step=step, status=status, detail=detail, ms=ms, sources=sources, **kwargs
        )
        self.events.append(event)
        await self._persist(event)
        return event

    async def _persist(self, event: PipelineEvent) -> None:
        """
        Mirror to the durable step log.

        [PRESERVE — design §6] `active` is a UI-only transition with no durable
        equivalent, so it is not persisted — persisting it would double-count
        every step against the four-value CHECK constraint. Everything else
        maps through `_STEP_STATUS_MAP`.

        `output_summary` is a JSONB column, so structured content needs no
        migration. `{"detail": ...}` remains the baseline shape every step
        writes; an event carrying a `trace` payload adds it alongside, never
        instead — existing readers keep finding `detail` exactly where it was.
        """
        if self._gateway is None or self.run_id is None:
            return
        if event.status == "active":
            return

        summary: dict = {"detail": event.detail}
        trace = getattr(event, "trace", None)
        if trace:
            summary["trace"] = trace

        self._step_order += 1
        await self._gateway.log_step(
            run_id=self.run_id,
            step_name=event.step,
            step_order=self._step_order,
            status=to_step_status(event.status),
            duration_ms=event.ms,
            output_summary=summary,
        )

    @asynccontextmanager
    async def step(self, step: str, detail: str):
        """
        Bracket a unit of work with `active` → `done`/`error` events, timing it.

        Guarantees the terminal event is emitted even when the body raises, so
        a failure can never leave the trace panel showing a step stuck at
        `active`.

        Yields a small handle whose `.detail` and `.status` may be overridden by
        the body — e.g. a tool that fell back sets `status="retry"`.
        """
        started = time.monotonic()
        await self.emit(step, "active", detail)
        handle = _StepHandle(detail=detail)
        try:
            yield handle
        except Exception as exc:
            elapsed = int((time.monotonic() - started) * 1000)
            await self.emit(step, "error", f"{detail} — failed: {exc}", ms=elapsed)
            raise
        else:
            elapsed = int((time.monotonic() - started) * 1000)
            await self.emit(handle.step or step, handle.status, handle.detail, ms=elapsed)


class _StepHandle:
    """Mutable handle yielded by `EventRecorder.step()`."""

    def __init__(self, detail: str) -> None:
        self.detail = detail
        self.status = "done"
        self.step: Optional[str] = None
