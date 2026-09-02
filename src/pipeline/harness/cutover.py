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
# Importing the agent modules is what registers each sub-agent into the
# Supervisor's registry (see agents/__init__.py: registration happens at
# each module's import time, and the package deliberately does not import
# them itself so a caller opts in). This module is the live cutover entry
# point, so importing them here makes every implemented sub-agent
# dispatchable whenever the harness actually runs a query — without this,
# the Supervisor classifies correctly but every route returns "capability
# not available yet" because nothing ever imported the handlers.
from src.pipeline.harness import agents as _harness_agents  # noqa: F401
import importlib as _importlib
import pkgutil as _pkgutil
for _m in _pkgutil.iter_modules(_harness_agents.__path__):
    if not _m.name.startswith("_"):
        _importlib.import_module(f"src.pipeline.harness.agents.{_m.name}")
from src.pipeline.harness.types import (
    SOURCE_TOOL_DISPLAY_LABELS,
    CallerContext,
    ConversationContext,
    ExecutionContext,
    PipelineEvent,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
)

# [Reconciliation merge — durable per-message trace, migration 019] Schema
# version for the structured trace payload persisted on `messages.
# degradation_trace`. Bump when the SHAPE changes in a way a reader must
# adapt to — persisted rows are historical records, so a renderer will
# encounter payloads written by older code indefinitely.
_TRACE_PAYLOAD_VERSION = 1


def _build_degradation_trace(result: SubAgentResult) -> dict:
    """
    The durable, investigator-facing "what I checked" payload for one
    query, written into `messages.degradation_trace` (migration 019) so it
    survives a page reload — previously this kind of detail lived only in
    the live SSE stream and was admin-only on the Run History page.

    ONE SHAPE FOR ALL SUB-AGENTS: reads only `tools_used`/`degraded_from`/
    `caveats`/`status`, fields every `SubAgentResult` carries regardless of
    which sub-agent produced it.

    THE OVERLAP CASE, WHICH READERS MUST HANDLE: a tool can appear in BOTH
    `tools_used` and `degraded_from` — it CONTRIBUTED DATA but degraded
    internally while doing so (e.g. RAG's relevance gate was unavailable,
    Unit 3). `degraded_from` membership does NOT imply "did not
    contribute". `contributed_only`/`degraded_and_contributed`/
    `degraded_only` are precomputed here so every consumer derives the
    three-way split identically rather than each reimplementing the set
    arithmetic.
    """
    used = list(result.tools_used)
    degraded = list(result.degraded_from)
    used_set, degraded_set = set(used), set(degraded)

    return {
        "v": _TRACE_PAYLOAD_VERSION,
        "sub_agent_status": result.status.value,
        "tools_used": used,
        "degraded_from": degraded,
        "contributed_only": [t for t in used if t not in degraded_set],
        "degraded_and_contributed": [t for t in used if t in degraded_set],
        "degraded_only": [t for t in degraded if t not in used_set],
        # Pre-labelled for the frontend — the canonical label map lives in
        # types.py's SOURCE_TOOL_DISPLAY_LABELS, so a client never needs its
        # own copy of it (a confirmed drift risk if it did).
        "labels": {
            "contributed_only": [
                SOURCE_TOOL_DISPLAY_LABELS.get(t, t) for t in used if t not in degraded_set
            ],
            "degraded_and_contributed": [
                SOURCE_TOOL_DISPLAY_LABELS.get(t, t) for t in used if t in degraded_set
            ],
            "degraded_only": [
                SOURCE_TOOL_DISPLAY_LABELS.get(t, t) for t in degraded if t not in used_set
            ],
        },
        "caveats": list(result.caveats),
        "disclosure_rendered": (
            result.generated_file.disclosure_rendered
            if result.generated_file is not None else None
        ),
    }


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
            parts.append(f"- {ln.description}{_case_suffix(ln)}")
    if unconfirmed:
        parts.append("\n**Possible matches — unverified leads**\n")
        for ln in unconfirmed:
            conf = f" _(similarity {ln.confidence:.0%})_" if ln.confidence is not None else ""
            parts.append(f"- {ln.description}{_case_suffix(ln)}{conf}")
    return answer_text + "\n" + "\n".join(parts)


def _case_suffix(link) -> str:
    """
    The " — case-a, case-b" tail appended after a link's description.

    Returns empty when the description already spells those case ids out.
    `cross_case_linkage.py` builds descriptions like "'X' appears across 13
    case(s): fir-201-26, fir-202-26, … (traversal depth 1 hop(s))", so
    appending the same ids again printed the whole list twice in one bullet
    (verify-log Finding V). Only links whose description omits the ids —
    e.g. a shorter identity-match phrasing — still get the suffix.
    """
    case_ids = getattr(link, "case_ids", None) or []
    if not case_ids:
        return ""
    description = getattr(link, "description", "") or ""
    if all(case_id in description for case_id in case_ids):
        return ""
    return f" — {', '.join(case_ids)}"


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

    # Live event bridge: Supervisor.handle() is an awaited coroutine that
    # calls `on_event` synchronously as it progresses, but a generator can't
    # `yield` from inside that callback. So push each event onto a queue and
    # run handle() as a task, draining the queue AS events arrive — this is
    # what makes `supervisor:dispatch` show its "active" spinner DURING the
    # sub-agent's work instead of flashing straight to done at the end (the
    # whole run's events used to be buffered and flushed only after handle()
    # returned). A sentinel marks completion. The sub-agent and supervisor
    # are untouched; only this adapter's delivery changed.
    event_queue: asyncio.Queue = asyncio.Queue()
    _DONE = object()

    def _on_event(evt: PipelineEvent) -> None:
        event_queue.put_nowait(evt)

    yield {"step": "supervisor", "status": "active", "detail": "Routing through the agent harness..."}

    supervisor = Supervisor()

    async def _run() -> SubAgentResult:
        try:
            return await supervisor.handle(agent_input, on_event=_on_event, gateway=gateway)
        finally:
            event_queue.put_nowait(_DONE)

    handle_task = asyncio.create_task(_run())

    # Drain events live until the task signals completion via the sentinel.
    while True:
        evt = await event_queue.get()
        if evt is _DONE:
            break
        out: dict = {"step": evt.step, "status": evt.status, "detail": evt.detail}
        if evt.ms is not None:
            out["ms"] = evt.ms
        if evt.sources:
            out["sources"] = evt.sources
        yield out

    result = await handle_task

    # The Supervisor's own "active" step (emitted before the queue drain
    # above) has no completion event of its own — routing + dispatch are the
    # observable phases, and both have now finished. Mark it done so the UI's
    # Supervisor row resolves from spinner to check instead of spinning
    # forever.
    yield {"step": "supervisor", "status": "done", "detail": "Harness routing complete"}

    # [Merge reconciliation — harness-reconciliation Unit 12 follow-up]
    # DIRECT classifies to NO_SUB_AGENT (supervisor.py) — the honest thing
    # for this adapter to do is hand the query back to the caller's own
    # legacy DIRECT path, not render a generic abstention. Not currently
    # reachable (DIRECT is never in `config.HARNESS_CUTOVER_ROUTES`, so
    # `main.py` never routes a DIRECT-classified query here at all), but
    # the signal is real and checked so this boundary can't silently
    # misbehave the moment that config is ever widened.
    if result.status == SubAgentStatus.ABSTAINED and result.error is not None and result.error.kind == "invalid_input":
        yield {
            "step": "supervisor",
            "status": "skipped",
            "detail": "DIRECT route — handing back to the legacy path",
            "delegate_to_legacy": True,
        }
        return

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
        # [Scenario-test UX note] DENIED is an authorization boundary, not a
        # failed search — say so. It previously shared ABSTAINED's generic
        # "could not produce a grounded answer" wording, so a user below the
        # cross-case role floor was told the system found nothing, when in
        # fact the data exists and they simply aren't permitted to see it.
        # The denial itself is unchanged and still fail-closed; only the
        # wording differs, and it reveals nothing about the withheld data.
        if result.status is SubAgentStatus.DENIED:
            detail = (
                "This question requires searching across multiple cases, which "
                "needs a supervisor-level role or higher. Your account doesn't "
                "have that access, so I can't run it. You can still ask about "
                "any case you're assigned to — select it from the case list "
                "and ask again."
            )
        else:
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
        # [Reconciliation merge — durable per-message trace, migration 019]
        # Persisted alongside the message so an investigator sees "what I
        # checked" for their OWN query after a page reload, not just live
        # while it streams.
        await async_save_history(
            session_id, user_message, delivered_text, user_id,
            project_id=project_id, degradation_trace=_build_degradation_trace(result),
        )
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
