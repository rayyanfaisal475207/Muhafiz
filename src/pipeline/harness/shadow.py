"""
Shadow mode — run the harness on real traffic, show nobody, log everything.

WHAT THIS DOES
──────────────
After the legacy pipeline has finished streaming its answer to the user, a
sampled fraction of queries are re-run through the agent harness in the
background. The harness's answer is written to `harness_shadow_runs`
(migration 020) so it can be compared against what the user actually got.

THE ONE INVARIANT
─────────────────
**A shadow run must never be able to change, delay, or break the answer a user
receives.** Everything below follows from that, and it is why this module looks
paranoid:

  * It is spawned AFTER the response generator has yielded its last event, so
    there is no path by which it can add latency to the stream.
  * `run_shadow()` catches BaseException, not Exception. An unhandled error in
    a detached task is logged and dropped; it never propagates.
  * Every logging write is itself wrapped. A failure to record a shadow result
    is a lost diagnostic, never an error the user could observe.
  * The concurrency guard releases in a `finally`, so a crash cannot leak the
    slot and wedge shadow mode off until the process restarts.
  * Cross-case routes are excluded (see `config.HARNESS_SHADOW_ROUTES`): they
    arm cross-case RLS scope, and doing that outside the request that
    authorized it — for output no one reads — is not a trade worth making.

WHY SAMPLING AND CONCURRENCY ARE SEPARATE LIMITS
────────────────────────────────────────────────
They bound different things and fail differently. The sample RATE bounds how
many queries are eligible over time; the CONCURRENCY cap bounds how many run at
once. A traffic burst can put many samples in flight simultaneously even at a
low rate, and each shadow run holds a model-server slot that the live path is
also competing for. The default cap of 1 makes shadowing single-flight: if a
run is already in progress the next eligible query is SKIPPED, never queued.
Queuing would let shadow work outlive the traffic that produced it and turn a
burst into a backlog that competes with live requests for minutes afterwards.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from typing import Any, Optional

from src import config
from src.pipeline.harness import classifier, supervisor
from src.pipeline.harness.contracts import CallerContext, Role, SubAgentInput
from src.pipeline.harness.events import EventRecorder

logger = logging.getLogger(__name__)

# Process-wide in-flight count. A plain int rather than a Semaphore because the
# required behaviour is "skip if busy", not "wait for a slot" — a semaphore's
# natural `acquire()` blocks, which is the failure mode this is preventing.
_in_flight: int = 0
_lock = asyncio.Lock()


def _sampled(route: str) -> tuple[bool, str]:
    """
    Should this query be shadowed? Returns (decision, reason).

    The reason is recorded on the row so a sparse log can be read correctly
    later: "few rows" means something different when the rate is 5% than when a
    route filter is excluding most traffic.
    """
    if not config.HARNESS_SHADOW_MODE:
        return False, "disabled"
    if route not in config.HARNESS_SHADOW_ROUTES:
        return False, f"route {route} not eligible"
    if random.random() >= config.HARNESS_SHADOW_SAMPLE_RATE:
        return False, "not sampled"
    return True, f"sampled at {config.HARNESS_SHADOW_SAMPLE_RATE:.0%}"


async def _acquire_slot() -> bool:
    """Take an in-flight slot if one is free. Never waits."""
    global _in_flight
    async with _lock:
        if _in_flight >= config.HARNESS_SHADOW_MAX_CONCURRENCY:
            return False
        _in_flight += 1
        return True


async def _release_slot() -> None:
    global _in_flight
    async with _lock:
        _in_flight = max(0, _in_flight - 1)


async def _log_shadow_run(gateway: Any, row: dict) -> None:
    """
    Persist one shadow result.

    Wrapped by the caller's own exception handling as well: losing a diagnostic
    row is always preferable to surfacing an error from a code path the user
    cannot see and did not ask for.
    """
    await gateway.log_harness_shadow_run(row)


async def run_shadow(
    *,
    gateway: Any,
    session_id: str,
    user_id: Optional[str],
    user_role: str,
    query_text: str,
    case_id: Optional[str] = None,
    project_id: Optional[str] = None,
    run_id: Optional[str] = None,
    legacy_route: str = "",
    legacy_outcome: Optional[str] = None,
    sampled_reason: str = "",
    route_result: Optional[dict] = None,
) -> None:
    """
    Execute one shadow run and record it. Never raises.

    Callers spawn this detached (`asyncio.create_task`) after the user's
    response has been fully delivered. It returns None in every case — success,
    skip, and failure are all reported through the log table and the logger,
    never to the caller, because by the time this runs there is no caller left
    to tell.
    """
    started = time.monotonic()
    row: dict = {
        "run_id": run_id,
        "session_id": session_id,
        "user_id": user_id,
        "case_id": case_id,
        "original_query": query_text,
        "legacy_route": legacy_route,
        "legacy_outcome": legacy_outcome,
        "sampled_reason": sampled_reason,
    }

    if not await _acquire_slot():
        # Deliberately NOT logged to the table: a skipped run produced no
        # result to compare, and writing a row for it would make the shadow log
        # mostly empty rows under load, burying the results that matter.
        logger.debug("Shadow run skipped for session %s: concurrency cap reached",
                     session_id)
        return

    try:
        try:
            role = Role(user_role)
        except ValueError:
            # An unrecognised role must not be silently upgraded. Refusing to
            # guess is the only safe reading: the harness's cross-case gates are
            # driven by this value.
            logger.warning("Shadow run skipped: unrecognised role %r", user_role)
            return

        # The harness routes the query ITSELF rather than inheriting legacy's
        # decision. Routing is one of the things shadow mode exists to compare,
        # and reusing the legacy route would silently agree by construction —
        # the log would show perfect routing agreement while proving nothing.
        # `legacy_route` is recorded on the row for exactly that comparison.
        if route_result is None:
            from src.pipeline.router import route_query

            route_result = await route_query(query_text)

        caller = CallerContext(
            user_id=user_id,
            role=role,
            active_case_id=case_id,
            project_id=project_id,
        )
        agent_input = SubAgentInput(
            query_text=query_text,
            caller=caller,
            # Chat only. A shadow run must never take the file-generating path:
            # it would write a real PDF to disk and a real row to
            # generated_files for a report no one requested.
            output_format="chat",
            target_entity=route_result.get("target_entity"),
        )

        decision = classifier.describe(route_result, query_text)
        row["harness_sub_agent"] = decision.get("sub_agent")
        row["routing_basis"] = decision.get("basis")

        # An EventRecorder with NO run_id. This is what keeps shadow events out
        # of `pipeline_steps`: `EventRecorder._persist()` returns early unless
        # BOTH a gateway and a run_id are present, and shadow runs never have
        # a run_id of their own. Constructed explicitly here, rather than
        # letting `invoke()` build one from its `run_id=None` default, so the
        # guarantee is stated at the point that depends on it — a future change
        # to invoke()'s defaults cannot silently start writing shadow steps into
        # the table admin analytics reads.
        #
        # `gateway` is still passed to invoke() as its own argument, because the
        # SUB-AGENTS need it for data access (case lookups, file records).
        recorder = EventRecorder(run_id=None, gateway=None)

        state = await supervisor.invoke(
            agent_input,
            route_result,
            events=recorder,
            gateway=gateway,
        )

        result = state.result
        row["harness_sub_agent"] = state.selected_agent or row.get("harness_sub_agent")
        if result is None:
            # DIRECT — no sub-agent ran. Recorded rather than dropped, because
            # "the harness declined to handle this" is itself a comparison
            # result worth seeing next to what legacy did.
            row["harness_status"] = "no_sub_agent"
        else:
            row["harness_status"] = result.status.value
            row["harness_answer"] = result.answer_text
            row["citation_count"] = len(result.citations or [])
            row["tools_used"] = list(result.tools_used or [])
            row["degraded_from"] = list(result.degraded_from or [])
            row["caveats"] = list(result.caveats or [])

        # Two things could be called "agreement", and only one is worth an
        # index. Whether both paths ANSWERED (vs both declined) is the coarse
        # signal a human triages on; whether they picked the same route is
        # recorded via `legacy_route` + `harness_sub_agent` for anyone reading
        # the row. Comparing prose is deliberately not attempted — no automatic
        # check can judge it, and a false "they agree" would bury the
        # disagreements this table exists to surface.
        harness_route = str(route_result.get("route") or "")
        if legacy_route and harness_route and legacy_route != harness_route:
            row["routing_basis"] = (
                f"{row.get('routing_basis') or ''} "
                f"[legacy routed {legacy_route}, harness routed {harness_route}]"
            ).strip()

        if legacy_outcome:
            row["routes_agree"] = _outcomes_agree(legacy_outcome, row["harness_status"])

    except BaseException as exc:  # noqa: BLE001 - see module docstring
        # A shadow crash is a FINDING: it means this query shape would have
        # failed for a real user had the harness been serving traffic. Recorded
        # on the row so it shows up in the disagreement index, not swallowed.
        row["error"] = f"{type(exc).__name__}: {exc}"
        logger.warning(
            "Shadow run failed for session %s: %s", session_id, exc, exc_info=True,
        )
    finally:
        await _release_slot()

    row["duration_ms"] = int((time.monotonic() - started) * 1000)

    try:
        await _log_shadow_run(gateway, row)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not record shadow run for session %s: %s",
                       session_id, exc)


def _outcomes_agree(legacy_outcome: str, harness_status: Optional[str]) -> bool:
    """
    Did both paths reach the same KIND of conclusion?

    Deliberately coarse. This compares whether both answered, or both declined —
    not whether the prose matches, which no automatic check can judge and which
    is exactly what a human reads the shadow log to assess. A false "they
    agree" from an over-eager string comparison would hide the disagreements
    this table exists to surface.
    """
    # "done" is what the legacy SSE stream actually emits for a delivered
    # response (`event("response", "done", ...)`), so it is the value that
    # matters here; the others are accepted because the outcome is also
    # recorded under different vocabularies elsewhere in the pipeline
    # (`response_type`, `final_outcome`), and this must not silently read a
    # successful run as a failure because the caller passed the other spelling.
    answered_legacy = legacy_outcome in ("done", "success", "ok", "answered", "rag")
    answered_harness = harness_status in ("ok", "partial")
    return answered_legacy == answered_harness


def maybe_spawn_shadow(**kwargs: Any) -> None:
    """
    Sample, and if selected, spawn a detached shadow run.

    Safe to call unconditionally from the request path: when shadow mode is off
    (the default) this is a config read and a return. It never awaits, so it
    adds no latency to the response even when it does spawn.

    Eligibility is decided from `legacy_route` — the route the legacy pipeline
    reported — because that is known synchronously here, and route filtering is
    a LOAD-CONTROL decision that must be made before committing to any work.
    The harness then routes the query independently inside the run (see
    `run_shadow`), so a routing DISAGREEMENT is still captured; what the filter
    prevents is spending a shadow slot on a route class that was excluded on
    purpose, such as the cross-case routes.
    """
    if not config.HARNESS_SHADOW_MODE:
        return

    route = str(kwargs.get("legacy_route") or "").strip().upper()
    should_run, reason = _sampled(route)
    if not should_run:
        return

    kwargs["sampled_reason"] = reason
    try:
        asyncio.get_running_loop().create_task(run_shadow(**kwargs))
    except RuntimeError:
        # No running loop (a sync caller, or a loop shutting down). Shadow mode
        # is strictly best-effort; there is nothing to recover here.
        logger.debug("No running loop available to spawn a shadow run")
