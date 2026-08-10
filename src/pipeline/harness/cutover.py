"""
Live-traffic cutover adapter — src/pipeline/harness/cutover.py
(AGENT_HARNESS_IMPLEMENTATION_PLAN.md §6, "Rollout strategy": "Each
sub-agent... is wired in behind a route/flag and only takes over its slice
of traffic once proven — never a single big-bang cutover.")

WHAT THIS IS. The thin bridge between `main.py::chat_endpoint()` (the real
API boundary, unchanged) and `Supervisor.handle()` (the harness). Everything
this module does was previously done INSIDE `src.pipeline.orchestrator.
process_query()` for the routes it now intercepts — this module is not new
business logic, it is a translation layer between two already-existing
contracts (the live SSE/Postgres event shape `main.py`'s frontend and admin
dashboard already depend on, and `SubAgentInput`/`SubAgentResult`'s bounded
harness shape).

SCOPE THIS SESSION, EXPLICITLY (resolved via AskUserQuestion, not guessed):
  - First slice: `config.HARNESS_CUTOVER_ROUTES` gates this per CLASSIFIED
    ROUTE (router.py's route string, e.g. "RAG"), checked by the caller
    (`chat_endpoint`) — this module does not read that config itself, it
    only runs once the caller has already decided to cut over.
  - Gate mechanism: env-var route allowlist. Flipping it is a deploy-time
    config change, not a code change — restart with a different
    `HARNESS_CUTOVER_ROUTES` value to add/remove a route.
  - Live-trace granularity: ACCEPTED to be coarser for a cut-over route.
    `Supervisor.handle()` emits exactly 2 `PipelineEvent`s (classify,
    outcome) — this module translates those plus one final "response" event
    into the SSE stream, not the full per-retrieval-step trace
    `orchestrator.py` produces today. This is a known, accepted UX
    difference for the first cutover slice, not an oversight — building the
    full per-step bridge (wiring `on_event` through to the same granularity
    as `orchestrator.py`'s `event()`/`log_step()` calls) is tracked future
    work, per `Supervisor.handle()`'s own docstring ("wiring on_event
    through to the existing event()/log_step() machinery is a later phase's
    job").
  - Postgres run-level parity IS included (not skipped): `gateway.create_run()`
    / `gateway.update_run()` — the admin dashboard's Run History LIST view
    reads `pipeline_runs`, not `pipeline_steps`, so a cutover request still
    shows up there with a real `final_outcome`/`total_duration_ms`. Only the
    per-STEP `pipeline_steps` trace (the Run DETAIL drill-down) is coarser,
    matching the accepted trade-off above.
  - Conversation memory IS preserved: `async_load_history()` /
    `format_history_for_prompt()` (read) and `async_save_history()` (write)
    are called exactly as `orchestrator.py` calls them, so a cutover
    sub-agent doesn't regress multi-turn conversations. `ConversationContext.
    summary` carries the same `history_text` string
    `orchestrator.py` builds into its own prompts — SubAgentInput's own
    contract requires this to already be pre-bounded by the caller
    (SUBAGENT_INTERFACES.md §2.0), which is exactly this module's job to
    provide; no sub-agent fetches its own history.
  - Project memory IS preserved: `gateway.get_project_memory()`, same as
    `orchestrator.py`, threaded into `ConversationContext.project_memory`.

WHAT THIS MODULE DOES NOT DO: classify (that's `Supervisor.handle()`'s own
job, which calls `route_query()` a SECOND time internally — see the "KNOWN,
ACCEPTED INEFFICIENCY" comment in `main.py::chat_endpoint()`, right where it
makes its own first classification call, for why this double-classification
is accepted rather than fixed by changing `Supervisor.handle()`'s
signature); re-implement any tool logic; touch RLS/case-access (both already
happened in `chat_endpoint` before this module is ever called, per design
§4.1/§4.2 — this module's `CallerContext` construction does NOT grant
access, it only carries through what was already checked).
"""

from __future__ import annotations

import asyncio
import time
from typing import AsyncGenerator, Optional

from src.data_gateway.base import DataGateway
from src.memory.conversation import async_load_history, async_save_history, format_history_for_prompt
from src.pipeline.harness.supervisor import Supervisor
from src.pipeline.harness.types import (
    CallerContext,
    ConversationContext,
    ExecutionContext,
    PipelineEvent,
    Role,
    SubAgentInput,
    SubAgentStatus,
)


async def run_cutover_query(
    *,
    session_id: str,
    user_message: str,
    project_id: Optional[str],
    case_id: Optional[str],
    user_id: str,
    user_role: str,
    preferred_language: Optional[str],
    gateway: DataGateway,
) -> AsyncGenerator[dict, None]:
    """
    Runs one query through the Supervisor and yields the same
    `{"step", "status", "detail", "ms"?, **kwargs}` SSE event-dict shape
    `orchestrator.py::process_query()` yields — `chat_endpoint()` treats
    this generator identically to that one, per design §6's own logging
    [PRESERVE] rule (the SSE shape must not change regardless of how
    control flow is restructured).

    [PRESERVE — design §4.4] `role` here is `user_role`, the caller's real
    RBAC role passed as `chat_endpoint`'s own `current_user.role` — never
    read from `user_profile` (which carries no role key at all). This
    function takes it as an explicit, separate parameter for exactly that
    reason; do not collapse it with anything profile-shaped at the call
    site.
    """
    query_start = time.monotonic()

    try:
        role = Role(user_role)
    except ValueError:
        # A role string that doesn't match one of the four canonical
        # values would be a real, upstream auth bug — not something this
        # adapter should paper over by defaulting. Fail this one request
        # loudly rather than silently mis-scoping cross-case access.
        yield {
            "step": "system",
            "status": "error",
            "detail": f"Unrecognized role for harness cutover: {user_role!r}",
        }
        return

    # ── Fetch history/project-memory exactly as orchestrator.py does —
    # this is the "pre-bounding" SubAgentInput's own contract requires the
    # CALLER to perform (SUBAGENT_INTERFACES.md §2.0); no sub-agent fetches
    # its own history. ──────────────────────────────────────────────────
    history, project_memory = await asyncio.gather(
        async_load_history(session_id, user_id),
        gateway.get_project_memory(project_id) if project_id else _none(),
    )
    history_text = format_history_for_prompt(history)
    project_memory_text = (
        project_memory["summary_text"].strip()
        if project_memory and project_memory.get("summary_text")
        else None
    )

    execution = ExecutionContext(
        caller=CallerContext(
            user_id=user_id,
            role=role,
            active_case_id=case_id,
            preferred_language=preferred_language,
        ),
        project_id=project_id,
        session_id=session_id,
    )
    agent_input = SubAgentInput(
        query_text=user_message,
        execution=execution,
        output_format="chat",
        conversation_context=ConversationContext(
            summary=history_text or None,
            project_memory=project_memory_text,
        ),
    )

    # Postgres run-level record — admin dashboard Run History LIST parity.
    # Per-step `pipeline_steps` detail stays coarser this slice (see module
    # docstring); this is the run-level row, not a step row.
    pg_run_id: Optional[str] = None
    try:
        pg_run_id = await gateway.create_run(session_id, user_message)
    except Exception:
        pg_run_id = None  # Never let observability failure block the query.

    events: list[PipelineEvent] = []

    def _on_event(evt: PipelineEvent) -> None:
        events.append(evt)

    yield {"step": "supervisor", "status": "active", "detail": "Routing through the agent harness..."}

    supervisor = Supervisor()
    result = await supervisor.handle(agent_input, on_event=_on_event, gateway=gateway)

    for evt in events:
        out: dict = {"step": evt.step, "status": evt.status, "detail": evt.detail}
        if evt.ms is not None:
            out["ms"] = evt.ms
        if evt.sources:
            out["sources"] = evt.sources
        yield out

    total_ms = int((time.monotonic() - query_start) * 1000)

    if result.status in (SubAgentStatus.ABSTAINED, SubAgentStatus.DENIED) or result.answer_text is None:
        detail = (
            "; ".join(result.caveats)
            if result.caveats
            else "The system could not produce a grounded answer to this question."
        )
        yield {"step": "response", "status": "error", "detail": detail, "ms": total_ms}
        if pg_run_id:
            await _bg_update_run(gateway, pg_run_id, final_outcome=result.status.value, total_duration_ms=total_ms)
        return

    yield {"step": "response", "status": "streaming", "detail": result.answer_text}
    yield {"step": "response", "status": "done", "detail": f"Response generated ({len(result.answer_text)} chars)", "ms": total_ms}

    try:
        await async_save_history(session_id, user_message, result.answer_text, user_id, project_id=project_id)
        yield {"step": "memory", "status": "done", "detail": "Saved to session"}
    except Exception as exc:
        yield {"step": "memory", "status": "error", "detail": f"Failed to save conversation memory: {exc}"}

    if pg_run_id:
        await _bg_update_run(
            gateway, pg_run_id, final_outcome=result.status.value, total_duration_ms=total_ms
        )


async def _none():
    return None


async def _bg_update_run(gateway: DataGateway, run_id: str, **kwargs) -> None:
    try:
        await gateway.update_run(run_id, **kwargs)
    except Exception:
        pass  # Same "never let logging crash the query" posture as orchestrator.py's own _bg().
