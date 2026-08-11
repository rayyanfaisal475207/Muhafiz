"""
Serve a query through the agent harness, as an SSE stream the existing
frontend already understands.

WHAT THIS IS FOR
────────────────
`src/pipeline/orchestrator.py::process_query()` is an async generator yielding
pipeline events, and the whole frontend is built around the events it emits.
This module is the harness's equivalent: same event vocabulary, same shapes, so
the UI renders a harness answer with no frontend change at all.

THE EVENT CONTRACT THE FRONTEND ACTUALLY DEPENDS ON
────────────────────────────────────────────────────
Read from `frontend/src/store/chatStore.ts`, not assumed:

  * `{step, status, detail, ms}` — every pipeline step card.
  * `{step: "response", status: "streaming", detail: "<text>"}` — the ANSWER.
    The store appends `detail` to the message body for these and these only;
    an answer sent any other way renders as an empty message.
  * `{step: "retrieval"|"web_search", status: "done", sources: [...]}` — the
    source chips. `extractSources()` reads no other step.
  * `trace` on any event — the degradation trace panel (migration 018).
  * status is one of `active|done|error|retry|skipped|streaming`.

WHY THE ANSWER IS CHUNKED RATHER THAN SENT WHOLE
─────────────────────────────────────────────────
`SubAgentResult` carries a COMPLETED answer — sub-agents generate, then verify,
then return, because an answer that fails the grounding gate must never have
been shown. So there is no token stream to forward: the harness genuinely knows
the whole answer before it can safely emit any of it.

Chunking it here is presentation only. It keeps the frontend's existing
streaming path working (one `streaming` event with a 2000-character `detail`
would render, but as a single jarring paint) without pretending the tokens were
generated incrementally. The verification-before-delivery guarantee is worth
more than true token streaming, and this is the honest way to reconcile them.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, AsyncGenerator, Optional

from src.memory.conversation import async_save_history
from src.pipeline.harness import classifier, supervisor
from src.pipeline.harness.contracts import (
    CallerContext,
    Role,
    SubAgentInput,
    SubAgentResult,
    SubAgentStatus,
)
from src.pipeline.harness.events import EventRecorder

logger = logging.getLogger(__name__)

# Characters per `streaming` event. Small enough to look like generation,
# large enough that a long answer does not flood the SSE channel with
# hundreds of frames.
_CHUNK = 24

# What the user sees when the harness declines to answer. Deliberately not an
# error: abstention is the system working — it means no evidence supported an
# answer, and saying so is more useful than a plausible guess.
_ABSTAIN_FALLBACK = (
    "I could not find evidence in the available documents that supports an "
    "answer to that question."
)


def _event(
    step: str,
    status: str,
    detail: str = "",
    ms: Optional[int] = None,
    **extra: Any,
) -> dict:
    """One SSE frame, in the exact shape the frontend's store expects."""
    payload: dict = {"step": step, "status": status, "detail": detail}
    if ms is not None:
        payload["ms"] = ms
    payload.update({k: v for k, v in extra.items() if v is not None})
    return payload


def _sources_from(result: SubAgentResult) -> list[dict]:
    """
    Citations -> the frontend's `Source` shape ({filename, score, type}).

    `display_label()` rather than the raw enum: [RESOLVED-1a] requires the
    investigator-facing label ("document search", not "RAG"). `type` carries
    the tool so the UI can group by provenance if it wants to.
    """
    sources: list[dict] = []
    for citation in result.citations or []:
        sources.append({
            "filename": citation.source_file or f"Document {citation.document_index}",
            "score": citation.confidence if citation.confidence is not None else 1.0,
            "type": citation.display_label(),
        })
    return sources


async def _stream_answer(text: str) -> AsyncGenerator[dict, None]:
    """Emit a completed answer as the frontend's streaming events."""
    for i in range(0, len(text), _CHUNK):
        yield _event("response", "streaming", text[i:i + _CHUNK])
        # Yield to the loop so frames actually flush to the client rather than
        # arriving as one batch after the generator completes.
        await asyncio.sleep(0)


async def process_query_harness(
    session_id: str,
    user_message: str,
    *,
    project_id: Optional[str] = None,
    case_id: Optional[str] = None,
    user_id: Optional[str] = None,
    user_role: str = "investigator",
    user_profile: Optional[dict] = None,
    gateway: Any = None,
    **_ignored: Any,
) -> AsyncGenerator[dict, None]:
    """
    Answer one query through the harness, yielding frontend-shaped events.

    Signature-compatible with `orchestrator.process_query()` for the arguments
    the endpoint passes, so switching between them is a one-line change at the
    call site. Extra keyword arguments are accepted and ignored rather than
    raising — the legacy generator takes several this path has no use for
    (`enable_web_search`, and anything added to it later), and a TypeError at
    the call site would take down the whole chat endpoint.

    Never raises: an unexpected failure is reported as a `system`/`error` event
    and a safe message, because by the time this runs the user is watching an
    empty message bubble waiting for text.
    """
    started = time.monotonic()
    gw = gateway
    if gw is None:
        from src.data_gateway import get_gateway

        gw = await get_gateway()

    answer_text = ""
    result: Optional[SubAgentResult] = None

    # Function-local, exactly as tools/real.py does it: importing the harness
    # package must stay free of the production dependency tree
    # (tests/harness/test_isolation.py), and the router is live-pipeline code.
    from src.pipeline.router import route_query

    try:
        # ── Routing ──
        yield _event("router", "active", "Classifying the query...")
        route_result = await route_query(user_message)
        route = str(route_result.get("route") or "")
        decision = classifier.describe(route_result, user_message)
        sub_agent = decision.get("sub_agent") or ""
        yield _event("router", "done", route)

        yield _event(
            "supervisor", "done",
            f"{sub_agent.replace('_', ' ')}" if sub_agent else "no sub-agent",
        )

        # ── DIRECT: no retrieval, no sub-agent ──
        # [PRESERVE — design §1] DIRECT answers from general knowledge with no
        # retrieval, and the Verifier deliberately never gates it, so it is
        # outside the harness's retrieval-and-verification scope entirely.
        #
        # Signalled to the CALLER rather than handled here. This module must not
        # import the legacy orchestrator: the harness runs alongside the legacy
        # path, never through it (enforced by tests/harness/test_isolation.py),
        # and reaching into it here would couple the two exactly where they are
        # meant to stay separable. `route_result` rides along so the caller does
        # not have to route the query a second time.
        if sub_agent == classifier.NO_SUB_AGENT:
            yield _event(
                "supervisor", "skipped",
                "DIRECT — answered without retrieval, no sub-agent",
                route=route, delegate_to_legacy=True,
            )
            return

        try:
            role = Role(user_role)
        except ValueError:
            # Never guess a role: the cross-case gates are driven by it.
            logger.error("Unrecognised role %r — refusing to run", user_role)
            yield _event("system", "error", "Your account role is not recognised.")
            return

        caller = CallerContext(
            user_id=user_id, role=role,
            active_case_id=case_id, project_id=project_id,
        )
        # The ROUTER decides the output format ("draft a report" -> file_docx,
        # "export as PDF" -> file_pdf), exactly as it does for the legacy path.
        # Dropping it and defaulting to "chat" made Report Drafting refuse every
        # request with invalid_input, which then rendered as the generic
        # "could not find evidence" abstention — a working sub-agent reported as
        # having found nothing.
        output_format = str(route_result.get("output_format") or "chat")
        if output_format not in ("chat", "file_pdf", "file_xlsx", "file_docx"):
            output_format = "chat"

        agent_input = SubAgentInput(
            query_text=user_message,
            caller=caller,
            session_id=session_id,
            output_format=output_format,
            target_entity=route_result.get("target_entity"),
        )

        # ── Run the sub-agent ──
        # The recorder mirrors every tool/sub-agent transition into the stream,
        # which is what fills the pipeline panel while the answer is produced.
        recorder = EventRecorder(run_id=None, gateway=None)
        yield _event("retrieval", "active", "Searching case evidence...")

        state = await supervisor.invoke(
            agent_input, route_result, events=recorder, gateway=gw,
        )
        result = state.result

        # Replay what the harness recorded, so the UI shows the real steps
        # rather than one opaque pause. `active` frames are dropped: the
        # matching terminal frame carries the outcome, and emitting both after
        # the fact would double every step card.
        for ev in recorder.events:
            if ev.status == "active":
                continue
            payload = _event(ev.step, ev.status, ev.detail or "", ev.ms)
            trace = getattr(ev, "trace", None)
            if trace:
                payload["trace"] = trace
            yield payload

        if result is None:
            yield _event("system", "error", "The query could not be handled.")
            return

        # ── A generated file, if this was a report request ──
        # Emitted with the same `file_generation`/`done` + `sources` shape the
        # legacy path uses (orchestrator.py:2110-2115), because the frontend's
        # FileResultCard renders a download link from `file_id` on a source and
        # reads it from nowhere else. Without this the report is written to disk
        # and recorded in `generated_files`, but the user has no way to reach it.
        generated = getattr(result, "generated_file", None)
        if generated is not None:
            yield _event(
                "file_generation", "done",
                f"File ready: {generated.file_name}",
                sources=[{
                    "filename": generated.file_name,
                    "type": (generated.file_name.rsplit(".", 1) + ["file"])[1],
                    "file_id": str(generated.file_id),
                }],
            )

        # ── Sources ──
        # Emitted as `retrieval`/`done` because that is the only step
        # `extractSources()` reads. Sending them under any other step name
        # renders no source chips at all.
        sources = _sources_from(result)
        elapsed = int((time.monotonic() - started) * 1000)
        yield _event(
            "retrieval", "done",
            f"{len(sources)} source(s)" if sources else "No supporting evidence",
            elapsed, sources=sources or None,
        )

        # ── The answer ──
        answer_text = result.answer_text or ""
        if result.status is SubAgentStatus.DENIED:
            answer_text = (
                result.error.message if result.error
                else "You do not have permission to run this query."
            )
        elif not answer_text:
            answer_text = _ABSTAIN_FALLBACK

        # Caveats are appended to the ANSWER, not left as metadata: a stated
        # gap the user never sees is the same as no gap at all.
        #
        # Skipping any caveat whose text the answer ALREADY contains. Some
        # sub-agents deliberately carry a qualification in both places —
        # Case Summarization injects the GRAPH-only disclosure into the answer
        # for the reader (§2.1.2 step 4) AND mirrors it into `caveats` as
        # structured data for the supervisor. Both are correct; rendering both
        # printed the same paragraph twice, once at the top and once in italics
        # at the bottom.
        new_caveats = [
            c for c in (result.caveats or [])
            if c.strip() and c.strip() not in answer_text
        ]
        if new_caveats:
            answer_text += "\n\n" + "\n".join(f"_{c}_" for c in new_caveats)

        yield _event("response", "active", "Generating response...")
        async for frame in _stream_answer(answer_text):
            yield frame
        yield _event(
            "response", "done",
            f"Response generated ({len(answer_text)} chars)",
            int((time.monotonic() - started) * 1000),
        )

    except Exception as exc:
        logger.error("Harness pipeline error: %s", exc, exc_info=True)
        yield _event("system", "error", str(exc))
        if not answer_text:
            answer_text = (
                "Something went wrong while answering that. Please try again."
            )
            async for frame in _stream_answer(answer_text):
                yield frame

    # ── Persist, so the answer survives a reload ──
    # Best-effort and last: a save failure must not blank an answer the user
    # can already read on screen.
    try:
        trace = None
        if result is not None:
            trace = {
                "sub_agent": getattr(result, "sub_agent", None),
                "status": result.status.value,
                "tools_used": list(result.tools_used or []),
                "degraded_from": list(result.degraded_from or []),
                "caveats": list(result.caveats or []),
            }
        await async_save_history(
            session_id, user_message, answer_text, user_id,
            project_id=project_id, degradation_trace=trace,
        )
        yield _event("memory", "done", "Saved to session")
    except Exception as exc:
        logger.warning("Could not save harness turn to history: %s", exc)
