"""Module 54 — the two provider-failure gaps Module 46 did not close.

Both defects here share one shape, the same one Modules 42 and 45 named: **a
failed call that leaves no trace looks exactly like a successful one.** A
provider outage does not raise to the reader; it produces an ordinary-looking
answer down a degraded path, and every check that was supposed to catch it
returns a clean zero.

Two halves:

  1. **The log check.** Module 46 (prompted by Module 42's transient Gemini
     429) already widened the eval preflight from a bare `rate limit` to
     `rate limit|RESOURCE_EXHAUSTED|429|quota`, having established that *Groq
     says "rate limit"; Gemini says `RESOURCE_EXHAUSTED`*. That half is not
     re-litigated here. What Module 50 hit and neither pattern covered is
     `503 UNAVAILABLE ... 'This model is currently experiencing high demand.'`
     — a transient **capacity** failure rather than a quota one, with the same
     consequence and the same invisibility. Pinned below: all four provider
     signatures match, ordinary log noise does not, and the prescriptive docs
     agree with the one canonical pattern in `evaluation/gold32_score.py`.

  2. **The cutover fallback.** `src/main.py`'s
     `Cutover classification failed, falling back to orchestrator.py` path is
     correct behaviour — but it means a **different sub-agent answers than the
     harness would have chosen**, and it used to be visible only in
     `backend.log`. Pinned below: the SSE stream says so, and the fallback
     itself is unchanged.

No network, no live stack: the router is stubbed to raise, the pipeline is a
fake async generator, and the gateway is the standard `FakeGateway`.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import uuid

import pytest
from fastapi.testclient import TestClient

from src import config
from src.main import app
from src.auth.routes import get_current_user
from src.data_gateway import get_gateway as real_get_gateway

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)


def _load(rel_path, name):
    path = os.path.join(_ROOT, *rel_path)
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


gs = _load(("evaluation", "gold32_score.py"), "gold32_score")


# ─────────────────────────────────────────────────────────────────────────
# 1. The widened provider-failure pattern
# ─────────────────────────────────────────────────────────────────────────

# The four signatures, verbatim as the providers emit them. The first two are
# Module 46's (via Module 42); the fourth is Module 50's, captured live.
PROVIDER_FAILURES = {
    "groq-rate-limit":
        "Exception: Failed to call groq after 3 attempts due to rate limits.",
    "gemini-resource-exhausted":
        "google.genai.errors.ClientError: RESOURCE_EXHAUSTED: You exceeded your "
        "current quota, please check your plan and billing details.",
    "bare-429":
        "HTTP error 429 returned by the provider",
    "gemini-503-unavailable":
        "503 UNAVAILABLE. {'message': 'This model is currently experiencing "
        "high demand. Please try again later.', 'status': 'UNAVAILABLE'}",
}

# Lines that occur in a HEALTHY backend.log. If the pattern matches any of
# these it stops being a signal — a check that always fires certifies nothing,
# the same way a check that never fires certifies nothing.
ORDINARY_LOG_NOISE = [
    "INFO: 127.0.0.1:52134 - \"POST /api/chat HTTP/1.1\" 200 OK",
    "INFO:src.pipeline.router:Routed query: route='META_ANALYSIS' confidence=0.9",
    "INFO:src.retrieval.embedder:Embedded 12 texts in 0.43s",
    "INFO:src.pipeline.harness.supervisor:Sub-agent returned 5 citations",
    "WARNING:src.pipeline.verifier:Claim 3 unsupported, dropping",
    "INFO: Application startup complete.",
]


def _grep_ciE(pattern, lines):
    """`grep -ciE <pattern>` — the count of lines matching, case-insensitively."""
    rx = re.compile(pattern, re.IGNORECASE)
    return sum(1 for line in lines if rx.search(line))


@pytest.mark.parametrize("name", sorted(PROVIDER_FAILURES))
def test_widened_pattern_matches_every_provider_signature(name):
    assert _grep_ciE(gs.LOG_GREP_PATTERN, [PROVIDER_FAILURES[name]]) == 1, (
        f"{name} is invisible to the prescribed log check — a run containing it "
        f"would be certified clean"
    )


def test_widened_pattern_does_not_match_ordinary_log_noise():
    assert _grep_ciE(gs.LOG_GREP_PATTERN, ORDINARY_LOG_NOISE) == 0


def test_the_bare_pattern_is_the_thing_being_fixed():
    """The regression this module exists for, stated as an assertion.

    `grep -c "rate limit"` sees ONE of the four. Three of them — including both
    that Module 50 hit live — sail past it as a clean run.
    """
    bare = "rate limit"
    missed = [n for n, line in PROVIDER_FAILURES.items()
              if _grep_ciE(bare, [line]) == 0]
    assert sorted(missed) == ["bare-429", "gemini-503-unavailable",
                              "gemini-resource-exhausted"]


def test_judge_error_classifier_agrees_with_the_grep_pattern():
    """The in-code retry classifier and the documented grep must not drift.

    `gold32_score.py` retries a provider failure on its own budget instead of
    burning the null-retry budget reserved for genuine judge failures. If the
    grep learns a signature the classifier does not, a 503 costs a row its
    score.
    """
    for name, line in PROVIDER_FAILURES.items():
        assert gs._is_rate_limit(RuntimeError(line)), name
    for line in ORDINARY_LOG_NOISE:
        assert not gs._is_rate_limit(RuntimeError(line)), line


# ─────────────────────────────────────────────────────────────────────────
# 2. Every file that prescribes the check prescribes the SAME check
# ─────────────────────────────────────────────────────────────────────────

# Files that TELL a reader how to certify a live run. Result files under
# docs/gold-qa-wave2-results/ are deliberately excluded: they are the record of
# what was actually measured at the time, and rewriting the check they ran
# would falsify the record.
PRESCRIPTIVE_DOCS = [
    ("HOW_TO_REPRODUCE_THIS_EVALUATION.md",),
    ("WAVE2_ORCHESTRATION_PROMPT.md",),
    ("docs", "gold-qa-wave2-results", "MODULE27_PREFLIGHT.md"),
]


@pytest.mark.parametrize("parts", PRESCRIPTIVE_DOCS,
                         ids=[p[-1] for p in PRESCRIPTIVE_DOCS])
def test_prescriptive_docs_prescribe_the_widened_pattern(parts):
    text = open(os.path.join(_ROOT, *parts), encoding="utf-8").read()
    assert gs.LOG_GREP_PATTERN in text, (
        f"{parts[-1]} prescribes a log check that is not the canonical "
        f"LOG_GREP_PATTERN in evaluation/gold32_score.py"
    )


@pytest.mark.parametrize("parts", PRESCRIPTIVE_DOCS,
                         ids=[p[-1] for p in PRESCRIPTIVE_DOCS])
def test_no_prescriptive_doc_still_prescribes_the_bare_grep(parts):
    """A bare `grep -c "rate limit"` in a prescriptive file is the defect.

    Prose *about* the bare form is fine and is in fact how these files explain
    the fix; what must not survive is a runnable command prescribing it.
    """
    text = open(os.path.join(_ROOT, *parts), encoding="utf-8").read()
    bare_command = re.compile(r'grep\s+-\w*c\w*\s+"rate limit"')
    for line in text.splitlines():
        if bare_command.search(line) and not line.lstrip().startswith(("-", "*", ">")):
            # Allowed only inside prose that is explicitly disavowing it.
            assert "NOT sufficient" in line or "used to" in line or "bare" in line, (
                f"{parts[-1]} still prescribes the bare check: {line.strip()}"
            )


# ─────────────────────────────────────────────────────────────────────────
# 3. The cutover fallback announces itself on the SSE stream
# ─────────────────────────────────────────────────────────────────────────

class _User:
    def __init__(self, user_id):
        self.id = uuid.UUID(user_id)
        self.email = "test@example.com"
        self.is_admin = False
        self.role = "investigator"
        self.company_name = "TestCo"
        self.plan = "free"
        self.police_station = None


@pytest.fixture
def chat_client(gateway, user_id, monkeypatch):
    async def _get_gateway():
        return gateway

    for module in ("src.data_gateway", "src.data_gateway.selector", "src.main"):
        monkeypatch.setattr(f"{module}.get_gateway", _get_gateway, raising=False)
    monkeypatch.setattr("src.main.set_case_scope", lambda case_id: None)

    app.dependency_overrides[get_current_user] = lambda: _User(user_id)
    app.dependency_overrides[real_get_gateway] = _get_gateway
    yield TestClient(app)
    app.dependency_overrides.clear()


def _sse_events(body):
    out = []
    for line in body.splitlines():
        if line.startswith("data:"):
            try:
                out.append(json.loads(line[5:]))
            except Exception:  # noqa: BLE001
                pass
    return out


def _fake_legacy_pipeline(monkeypatch, calls):
    async def _process_query(session_id, message, **kwargs):
        calls.append(kwargs)
        yield {"step": "response", "status": "success",
               "answer": "The legacy orchestrator answered this one."}

    monkeypatch.setattr("src.main.process_query", _process_query)


def test_failed_cutover_classification_is_visible_on_the_sse_stream(
        chat_client, monkeypatch):
    """The defect: this used to be a `logger.warning` and nothing else.

    Module 50 hit it twice on one transient provider outage. The answers came
    back looking ordinary, from a sub-agent the harness never chose, and the
    only trace was a line in `backend.log`.
    """
    monkeypatch.setattr(config, "HARNESS_CUTOVER_ROUTES", {"RAG"})

    async def _boom(message):
        raise RuntimeError(
            "503 UNAVAILABLE. {'message': 'This model is currently "
            "experiencing high demand.', 'status': 'UNAVAILABLE'}")

    monkeypatch.setattr("src.main.route_query", _boom)
    calls = []
    _fake_legacy_pipeline(monkeypatch, calls)

    resp = chat_client.post("/api/chat", json={
        "session_id": str(uuid.uuid4()), "message": "How many cases involve a weapon?"})
    assert resp.status_code == 200
    events = _sse_events(resp.text)

    flagged = [e for e in events if e.get("cutover_classification_failed")]
    assert len(flagged) == 1, (
        "a reader of the stream cannot tell the harness path was skipped")
    assert "orchestrator.py" in flagged[0]["detail"]
    # The underlying provider failure is carried through, so the reader learns
    # WHY as well as THAT — and so the same widened pattern matches the stream.
    assert _grep_ciE(gs.LOG_GREP_PATTERN, [flagged[0]["detail"]]) == 1

    # The marker precedes the answer: a run that later times out mid-stream
    # still shows it.
    assert events.index(flagged[0]) == 0

    # The fallback itself is UNCHANGED — this module surfaces it, it does not
    # restructure it. The legacy pipeline still answered, and still received
    # the precomputed route (None, because classification never returned one).
    assert any(e.get("step") == "response" for e in events)
    assert len(calls) == 1 and calls[0]["precomputed_route"] is None


def test_a_successful_classification_emits_no_fallback_marker(
        chat_client, monkeypatch):
    """The other half of a signal: it must be silent when nothing went wrong."""
    monkeypatch.setattr(config, "HARNESS_CUTOVER_ROUTES", {"RAG"})

    async def _ok(message):
        return {"route": "META_ANALYSIS", "output_format": "chat"}

    monkeypatch.setattr("src.main.route_query", _ok)
    calls = []
    _fake_legacy_pipeline(monkeypatch, calls)

    resp = chat_client.post("/api/chat", json={
        "session_id": str(uuid.uuid4()), "message": "How many cases involve a weapon?"})
    assert resp.status_code == 200
    events = _sse_events(resp.text)
    assert not [e for e in events if e.get("cutover_classification_failed")]


# ─────────────────────────────────────────────────────────────────────────
# 4. The runner records it, so the artefact can be read on its own
# ─────────────────────────────────────────────────────────────────────────

gr = _load(("evaluation", "gold32_run.py"), "gold32_run")


def _sse(*events):
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events)


def test_runner_records_the_fallback_per_row():
    sse = _sse(
        {"step": "routing", "status": "warning",
         "cutover_classification_failed": True,
         "detail": "Cutover classification failed, falling back to orchestrator.py: "
                   "503 UNAVAILABLE"},
        {"step": "response", "status": "success",
         "answer": "An answer that looks entirely ordinary."},
    )
    assert gr.parse(sse)["cutover_classification_failed"] is True


def test_runner_records_a_clean_row_as_false_not_missing():
    """Explicit False, never absent — Module 42's principle.

    A missing key is indistinguishable from an older artefact that never
    recorded it, which is how "we did not measure this" gets read as "this did
    not happen".
    """
    sse = _sse({"step": "response", "status": "success",
                "answer": "An answer that looks entirely ordinary."})
    parsed = gr.parse(sse)
    assert "cutover_classification_failed" in parsed
    assert parsed["cutover_classification_failed"] is False


def test_the_marker_event_does_not_disturb_answer_or_route_capture():
    """It must not become a second defect by polluting what is already parsed."""
    sse = _sse(
        {"step": "routing", "status": "warning",
         "cutover_classification_failed": True,
         "detail": "Cutover classification failed, falling back to orchestrator.py: boom"},
        {"step": "router", "detail": "Routed query: route='META_ANALYSIS'"},
        {"step": "response", "status": "success",
         "answer": "An answer that looks entirely ordinary."},
    )
    parsed = gr.parse(sse)
    assert parsed["route"] == "META_ANALYSIS"
    assert parsed["actual_answer"] == "An answer that looks entirely ordinary."
