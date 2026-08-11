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
  - Full-result translation IS included (not just status/answer_text):
    [AMENDMENT — pre-Timeline/Cross-Case/Data-Quality/Report-Drafting
    cutover] `run_cutover_query()` originally only read `result.status` /
    `result.answer_text` — correct for Semantic Search (this module's only
    cut-over route to date, which sets neither `.links`, `.events`,
    `.metrics`, nor `.generated_file`), but a silent data-loss trap for any
    route cut over after it: a sub-agent could return fully correct
    `.events`/`.links`/`.metrics`/`.generated_file` and this boundary would
    drop all of it on the floor before it ever reached the SSE stream. Now
    translated: `.generated_file` and `.links` mirror an already-shipped
    orchestrator.py event shape exactly (`file_generation`/
    `cross_case_finding` — see the inline comments at each yield site for
    the precedent each one mirrors); `.events`/`.metrics` have no
    pre-harness precedent at all (Timeline Building / Data-Quality never
    existed before the harness) and were resolved via AskUserQuestion this
    session to new, additive `timeline_building`/`data_quality` SSE steps,
    frontend rendering left as explicit future work. This closes the gap
    for all four but does NOT itself cut any of them over — none of
    Timeline Building, Cross-Case Linkage, Data-Quality, or Report Drafting
    is in `HARNESS_CUTOVER_ROUTES` as of this change; it only makes doing so
    safe whenever classification reachability lands.

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
    SubAgentResult,
    SubAgentStatus,
)


# [Reconciliation fix — harness-reconciliation Unit 12] The caveats contract
# (SubAgentResult.caveats' own [PRESERVE — design §3] rule) states
# qualifications "MUST survive to the final response". This adapter
# previously never read `result.caveats` at all on the success path —
# every degradation qualification a sub-agent carefully attached (partial
# evidence, unconfirmed identity links, a validation issue) was silently
# dropped before it reached the user. This renders it into the answer text
# itself (the one surface guaranteed to reach the user regardless of what
# the frontend does or doesn't render from side-channel SSE fields),
# skipping any caveat whose text the answer already contains — some
# sub-agents deliberately carry a qualification in both places (Case
# Summarization injects the GRAPH-only disclosure into the answer AND
# mirrors it into `caveats`), and printing both would show the same
# qualification twice.
def _append_caveats(answer_text: str, result: SubAgentResult) -> str:
    new_caveats = [
        c for c in (result.caveats or [])
        if c and c.strip() and c.strip() not in answer_text
    ]
    if not new_caveats:
        return answer_text
    return answer_text + "\n\n" + "\n".join(f"_{c}_" for c in new_caveats)


# [Reconciliation fix — Unit 12] Cross-Case Linkage returns its finding as
# STRUCTURED links (`SubAgentResult.links`), not prose — the `cross_case_
# finding` SSE event below carries the raw data, but the frontend's
# consumption of that step is shallow (a numeric hop_count/confidence
# badge; the link descriptions themselves are never rendered anywhere).
# Without this, the actual substance of a cross-case finding — which cases
# connect, and which possible identity matches are unconfirmed — never
# reaches anywhere the user can read it. Confirmed and unconfirmed links
# are kept visually distinct because [PRESERVE — design §3] requires that a
# consumer cannot render an unverified identity match indistinguishably
# from an established one.
def _append_cross_case_links(answer_text: str, result: SubAgentResult) -> str:
    links = result.links or []
    if not links:
        return answer_text
    confirmed = [ln for ln in links if not ln.is_unconfirmed]
    unconfirmed = [ln for ln in links if ln.is_unconfirmed]
    parts: list[str] = []
    if confirmed:
        parts.append("\n**Confirmed connections**\n")
        for ln in confirmed:
            cases = f" — {', '.join(ln.case_ids)}" if ln.case_ids else ""
            parts.append(f"- {ln.description}{cases}")
    if unconfirmed:
        parts.append("\n**Possible matches — unverified leads**\n")
        for ln in unconfirmed:
            cases = f" — {', '.join(ln.case_ids)}" if ln.case_ids else ""
            conf = f" _(similarity {ln.confidence:.0%})_" if ln.confidence is not None else ""
            parts.append(f"- {ln.description}{cases}{conf}")
    return answer_text + "\n" + "\n".join(parts)


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

    # ── Cross-Case Linkage — mirrors orchestrator.py's own XGRAPH-route
    # event("cross_case_finding", "done", ..., sources=..., case_scope=...,
    # unconfirmed_links=...) (orchestrator.py ~1182-1191). That `sources`
    # list is built there from raw evidence chunks orchestrator.py still has
    # in scope at that point; `SubAgentResult.links` deliberately carries no
    # such chunk detail (design §3's no-raw-evidence-crosses-the-boundary
    # rule), so this translation covers what the bounded `CrossCaseLink`
    # payload actually holds. Confirmed sufficient: MessageBubble.tsx/
    # chatStore.ts's own consumption of this step is shallow (a numeric
    # hop_count/graph_confidence badge; `unconfirmed_links`/`case_scope`
    # aren't read at all today), so there's no deep shape to match here that
    # this translation is missing.
    if result.links:
        yield {
            "step": "cross_case_finding",
            "status": "done",
            "detail": f"Found {len(result.links)} cross-case connection(s)",
            "case_scope": "cross_case",
            "unconfirmed_links": [link.model_dump() for link in result.links if link.is_unconfirmed],
        }

    # ── Timeline Building / Data-Quality — AskUserQuestion-resolved this
    # session: unlike `cross_case_finding`/`file_generation` above, these two
    # have NO pre-harness precedent (neither sub-agent existed before the
    # harness, and the frontend has no step-name handling for either shape
    # yet). Decision: emit new, additive SSE steps now — carrying the full
    # bounded `TimelineEvent`/`DataQualityMetric` payloads — so the wire
    # contract is ready and this boundary stops silently dropping the data,
    # even though rendering them is left as explicit future frontend work
    # rather than invented unilaterally here.
    if result.events:
        yield {
            "step": "timeline_building",
            "status": "done",
            "detail": f"{len(result.events)} event(s)",
            "events": [e.model_dump() for e in result.events],
        }
    if result.metrics:
        yield {
            "step": "data_quality",
            "status": "done",
            "detail": f"{len(result.metrics)} metric group(s)",
            "metrics": [m.model_dump() for m in result.metrics],
        }

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

    # [Reconciliation fix — Unit 12] Augment the delivered answer with the
    # cross-case findings and caveats a sub-agent attached — see
    # _append_cross_case_links()/_append_caveats()'s own comments for why
    # this must happen here rather than relying on side-channel SSE events.
    delivered_text = _append_caveats(
        _append_cross_case_links(result.answer_text, result), result
    )

    yield {"step": "response", "status": "streaming", "detail": delivered_text}
    yield {"step": "response", "status": "done", "detail": f"Response generated ({len(delivered_text)} chars)", "ms": total_ms}

    try:
        await async_save_history(session_id, user_message, delivered_text, user_id, project_id=project_id)
        yield {"step": "memory", "status": "done", "detail": "Saved to session"}
    except Exception as exc:
        yield {"step": "memory", "status": "error", "detail": f"Failed to save conversation memory: {exc}"}

    # ── Report Drafting — mirrors orchestrator.py's own `_generate_file()`
    # event: event("file_generation", "done", f"File ready: {file_name}",
    # sources=[{"filename", "type", "file_id"}]) (orchestrator.py:2117), and
    # its ordering relative to the memory-save event above (orchestrator.py
    # calls `_generate_file()` only after its own "memory" event,
    # orchestrator.py:2065-2076). Confirmed MessageBubble.tsx deeply depends
    # on this exact `sources` shape to render the download link (it filters
    # `pipelineEvents` for step=="file_generation" && status=="done" &&
    # sources, then flatMaps `sources` into `FileResultCard`) — so the field
    # names below (`filename`, `type`, `file_id`) are not incidental, they
    # are the contract. `file_type` isn't a field on `GeneratedFileRef`
    # (unlike orchestrator.py, which derives it locally from
    # `output_format.split('_')[1]`); it's recovered here from
    # `file_name`'s extension, which `_generate_file()`/`gateway.
    # log_generated_file()` guarantees is always present.
    if result.generated_file:
        gf = result.generated_file
        file_type = gf.file_name.rsplit(".", 1)[-1].lower() if "." in gf.file_name else "file"
        yield {
            "step": "file_generation",
            "status": "done",
            "detail": f"File ready: {gf.file_name}",
            "sources": [{"filename": gf.file_name, "type": file_type, "file_id": gf.file_id}],
        }

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
