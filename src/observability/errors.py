"""
Error capture — persists backend failures so they can be reviewed.

Before this, the only record of an error was a line on stdout: if you weren't
watching the terminal at the time, the failure was simply gone. The admin
dashboard needs an error *history* and a *trend*, which means errors have to be
written down.

Design constraints, in order of importance:

1. **Never break the request.** A failure in error-logging must not raise, must
   not block the event loop, and must not turn a handled error into a 500.
2. **Never recurse.** Writing an error to the database can itself fail; if that
   failure were logged through the same handler we would spin forever. A
   re-entrancy guard plus a logger denylist prevents this.
3. **Never block.** Records go onto an in-memory queue; a single background task
   drains it. If the queue is full we drop the record (and say so) rather than
   applying backpressure to a user request.
"""
from __future__ import annotations

import asyncio
import logging
import re
import traceback
from contextvars import ContextVar
from typing import Optional

logger = logging.getLogger(__name__)

# Groq/Gemini rate-limit exhaustion is an *expected* operational condition under
# load (the key rotation in src/llm/client.py tried every configured key and all
# were throttled), not an application bug. We still record it — the demo needs to
# know if it happens live — but at `warning` severity with a stable error_type so
# the admin Errors view can filter it out of the real-bug stream instead of it
# burying genuine errors under a wall of red. Matching is on the message text
# because most catch sites log the wrapped string without exc_info.
_RATE_LIMIT_RE = re.compile(
    r"due to rate limits|rate limit exceeded|resource_exhausted|\b429\b",
    re.IGNORECASE,
)
_RATE_LIMIT_TYPE = "RateLimitExhausted"


def _reclassify_rate_limit(severity: str, error_type: Optional[str], message: str):
    """Downgrade rate-limit-exhaustion records to a filterable warning.

    Returns (severity, error_type). Leaves `critical` records untouched — a
    critical never means "expected backoff".
    """
    if severity == "critical":
        return severity, error_type
    if _RATE_LIMIT_RE.search(message or "") or _RATE_LIMIT_RE.search(error_type or ""):
        return "warning", (error_type or _RATE_LIMIT_TYPE)
    return severity, error_type

# Loggers that must never be captured — writing their errors would recurse
# through the very code path that is failing.
_DENYLIST = (
    "src.observability.errors",
    "src.data_gateway",
    "httpx",
    "httpcore",
    "hpack",
)

_MAX_QUEUE = 500
_queue: Optional[asyncio.Queue] = None
_worker: Optional[asyncio.Task] = None
_dropped = 0

# Re-entrancy guard: True while we are inside the DB write for an error.
_writing: ContextVar[bool] = ContextVar("_writing", default=False)

# Correlation context, set by the pipeline so an error can be traced to a run.
_run_id: ContextVar[Optional[str]] = ContextVar("_run_id", default=None)
_session_id: ContextVar[Optional[str]] = ContextVar("_session_id", default=None)
_user_id: ContextVar[Optional[str]] = ContextVar("_user_id", default=None)


def set_error_context(run_id: str = None, session_id: str = None, user_id: str = None) -> None:
    """Tag subsequent errors on this task with the run they belong to."""
    if run_id is not None:
        _run_id.set(run_id)
    if session_id is not None:
        _session_id.set(session_id)
    if user_id is not None:
        _user_id.set(user_id)


async def _drain() -> None:
    """Single background consumer: writes queued errors to the database."""
    from src.data_gateway import get_gateway

    while True:
        record = await _queue.get()
        try:
            token = _writing.set(True)
            try:
                gateway = await get_gateway()
                await gateway.log_error(record)
            finally:
                _writing.reset(token)
        except Exception:
            # Deliberately silent: if we cannot record an error, saying so
            # through the logger would come straight back here.
            pass
        finally:
            _queue.task_done()


def _ensure_worker() -> bool:
    """Start the queue + drain task lazily, on the running loop."""
    global _queue, _worker
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return False  # no loop (e.g. import time, sync scripts) — nothing to do

    if _queue is None:
        _queue = asyncio.Queue(maxsize=_MAX_QUEUE)
    if _worker is None or _worker.done():
        _worker = loop.create_task(_drain())
    return True


def capture(
    message: str,
    *,
    severity: str = "error",
    error_type: str = None,
    module: str = None,
    stack_trace: str = None,
    context: dict = None,
) -> None:
    """Queue one error for persistence. Never raises, never blocks."""
    global _dropped

    if _writing.get():
        return  # we are already inside an error write — do not recurse

    if not _ensure_worker():
        return

    severity, error_type = _reclassify_rate_limit(severity, error_type, message)

    record = {
        "severity": severity,
        "error_type": error_type,
        "module": module,
        "message": (message or "")[:4000],
        "stack_trace": (stack_trace or None) and stack_trace[:8000],
        "run_id": _run_id.get(),
        "session_id": _session_id.get(),
        "user_id": _user_id.get(),
        "context": context,
    }

    try:
        _queue.put_nowait(record)
    except asyncio.QueueFull:
        _dropped += 1  # shed load rather than stall a user request


def capture_exception(exc: BaseException, *, module: str = None, context: dict = None) -> None:
    """Convenience wrapper for an exception object."""
    capture(
        str(exc) or exc.__class__.__name__,
        severity="error",
        error_type=exc.__class__.__name__,
        module=module,
        stack_trace="".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        context=context,
    )


class DatabaseErrorHandler(logging.Handler):
    """
    Logging handler that mirrors ERROR/CRITICAL records into `error_logs`.

    Attached at startup, so every `logger.error(...)` already in the codebase
    becomes a dashboard row with no call-site changes.
    """

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if record.levelno < logging.ERROR:
                return
            if record.name.startswith(_DENYLIST):
                return

            exc_type = None
            stack = None
            if record.exc_info and record.exc_info[0]:
                exc_type = record.exc_info[0].__name__
                stack = "".join(traceback.format_exception(*record.exc_info))

            capture(
                record.getMessage(),
                severity="critical" if record.levelno >= logging.CRITICAL else "error",
                error_type=exc_type,
                module=record.name,
                stack_trace=stack,
            )
        except Exception:
            pass  # a logging handler must never raise


def install() -> None:
    """Attach the handler to the root logger. Safe to call more than once."""
    root = logging.getLogger()
    if any(isinstance(h, DatabaseErrorHandler) for h in root.handlers):
        return
    handler = DatabaseErrorHandler()
    handler.setLevel(logging.ERROR)
    root.addHandler(handler)


def stats() -> dict:
    """Queue health, surfaced on the dashboard so silent drops are visible."""
    return {
        "queued": _queue.qsize() if _queue else 0,
        "dropped": _dropped,
        "worker_running": bool(_worker and not _worker.done()),
    }
