"""Parts of a compound question must not share state, or lose their trace.

A compound question is split and each part is run as a whole question. Three
things were wrong with doing that one after another on one Runtime.

  * The parts share ONE deadline. Run end to end, a long first part leaves
    the rest nothing, so the question returns "unavailable" having answered
    no part -- while each part answers fine asked on its own.

  * `_run` records stage, failures, trace and unrecorded terms on the
    instance, and the store is handed its trace by assignment. Parts
    sharing one Runtime interleave all of it.

  * A part built its own trace and nothing folded it back, so the Trace
    panel showed none of the work behind the answer.

MEASURED, and worth recording because it corrects the expectation: running
the parts together is NOT faster. Three pairs on one two-part question gave
94s, 91s and 109s sequentially against 113s, 97s and 71s together -- equal
inside the noise, because a part waits on the same single cloud planner
every other part waits on. Overlapping them moves the queue rather than
shortening it. The budget is what improves, not the throughput.
"""
import asyncio
from uuid import UUID, uuid4

import pytest

from src.evidence.runtime import Runtime, _PartStore
from src.evidence.types import Answer, Scope


class FakeStore:
    """A store that opens a connection per call, as ScopedStore does."""

    def __init__(self):
        self.trace = None

    async def case_documents(self):
        return 73


class BoundStore(FakeStore):
    """A store holding ONE connection, which cannot have two readers."""

    def __init__(self):
        super().__init__()
        self.conn = object()


def scope_of(n=73):
    return Scope(user_id=uuid4(), session_id=uuid4(),
                 case_ids=[f"case-{i}" for i in range(n)])


# ── The per-part store view ───────────────────────────────────────────────

def test_a_part_store_keeps_its_own_trace():
    """Two views of one store must not overwrite each other's trace."""
    shared = FakeStore()
    first, second = _PartStore(shared), _PartStore(shared)
    first.trace = "part one"
    second.trace = "part two"
    assert first.trace == "part one"
    assert second.trace == "part two", (
        "assignment on one view must not reach the other, or the Trace panel "
        "shows whichever part happened to write last")
    assert shared.trace is None, "the shared store must be left alone"


def test_a_part_store_delegates_everything_else():
    shared = FakeStore()
    view = _PartStore(shared)
    assert asyncio.run(view.case_documents()) == 73, (
        "every store method must reach the real store")


def test_a_part_store_does_not_invent_attributes():
    with pytest.raises(AttributeError):
        _PartStore(FakeStore()).no_such_method


# ── Each part gets its own Runtime, and the traces come back ──────────────

def run_parts(store, parts, answers, *, concurrent=True, monkeypatch=None):
    """Drive _each_part with _run stubbed, recording the instance it ran on."""
    scope = scope_of()
    parent = Runtime(store)
    parent.failures = []
    parent.trace = None
    seen = []

    async def fake_run(self, run_id, question, *a, **kw):
        seen.append((question, id(self)))
        self.stage = "ran " + question
        self.failures.append({"part": question})
        outcome = answers[question]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(Runtime, "_run", fake_run, raising=True)
    monkeypatch.setenv("EVIDENCE_PARTS_CONCURRENT",
                       "true" if concurrent else "false")
    answer = asyncio.run(parent._each_part(
        uuid4(), "whole question", parts, scope, "auto", deadline=None))
    return answer, seen, parent


def answered(text):
    return Answer(run_id=uuid4(), status="answered", text=text)


def test_each_part_runs_on_its_own_runtime(monkeypatch):
    """Parts sharing one instance interleave stage, failures and trace."""
    parts = ["who was it recovered from", "how many rounds"]
    answer, seen, parent = run_parts(
        FakeStore(), parts,
        {parts[0]: answered("recovered from Kashif"),
         parts[1]: answered("6 rounds")},
        monkeypatch=monkeypatch)
    instances = {ident for _, ident in seen}
    assert len(instances) == len(parts), (
        "each part needs its own Runtime; one shared instance would have "
        "them overwriting each other's stage and trace")
    assert id(parent) not in instances, (
        "a part must not run on the Runtime that is assembling the answer")


def test_both_parts_reach_the_answer_in_order(monkeypatch):
    parts = ["first part", "second part"]
    answer, _, _ = run_parts(
        FakeStore(), parts,
        {parts[0]: answered("ONE"), parts[1]: answered("TWO")},
        monkeypatch=monkeypatch)
    assert answer.status == "answered"
    assert answer.text.index("ONE") < answer.text.index("TWO"), (
        "parts finish in whatever order they finish; the answer must still "
        "read in the order the question asked them")


def test_part_failures_are_collected_not_lost(monkeypatch):
    parts = ["works", "explodes"]
    answer, _, parent = run_parts(
        FakeStore(), parts,
        {parts[0]: answered("fine"), parts[1]: RuntimeError("boom")},
        monkeypatch=monkeypatch)
    assert answer.status == "partial", (
        "one part answering and one failing is partial, not a total loss")
    assert "fine" in answer.text
    assert any(f.get("error_type") == "RuntimeError"
               for f in parent.failures), "the failure must be recorded"


def test_the_health_record_says_whether_they_ran_together(monkeypatch):
    parts = ["a", "b"]
    outcomes = {"a": answered("A"), "b": answered("B")}
    together, _, _ = run_parts(FakeStore(), parts, outcomes,
                               concurrent=True, monkeypatch=monkeypatch)
    assert together.health["ran_concurrently"] is True
    one_by_one, _, _ = run_parts(FakeStore(), parts, outcomes,
                                 concurrent=False, monkeypatch=monkeypatch)
    assert one_by_one.health["ran_concurrently"] is False


def test_a_single_connection_store_stays_sequential(monkeypatch):
    """One connection cannot have two readers, whatever the flag says."""
    parts = ["a", "b"]
    answer, _, _ = run_parts(
        BoundStore(), parts, {"a": answered("A"), "b": answered("B")},
        concurrent=True, monkeypatch=monkeypatch)
    assert answer.health["ran_concurrently"] is False, (
        "a store holding one connection must not be read by two parts at "
        "once, however EVIDENCE_PARTS_CONCURRENT is set")


def test_every_part_is_still_attempted_when_the_first_fails(monkeypatch):
    """Serially, a part that raises must not abandon the ones after it."""
    parts = ["explodes", "works"]
    answer, seen, _ = run_parts(
        FakeStore(), parts,
        {parts[0]: RuntimeError("boom"), parts[1]: answered("still fine")},
        concurrent=False, monkeypatch=monkeypatch)
    assert len(seen) == 2, "the second part must still be run"
    assert "still fine" in answer.text


def test_nothing_established_is_not_reported_as_answered(monkeypatch):
    parts = ["a", "b"]
    answer, _, _ = run_parts(
        FakeStore(), parts,
        {"a": RuntimeError("x"), "b": RuntimeError("y")},
        monkeypatch=monkeypatch)
    assert answer.status == "unavailable"
    assert "No factual zero was inferred" in answer.text, (
        "two failed parts must never read as a measured absence")


# ── The stream must not go silent while the parts run ─────────────────────

class RecordingTrace:
    """A trace that only remembers; enough to see whether anything is said."""

    def __init__(self):
        self.steps = []
        self.labels = {}

    async def step(self, stage, title, **detail):
        self.steps.append({"stage": stage, "title": title, "detail": detail})


def test_a_heartbeat_is_emitted_while_the_parts_are_still_running(monkeypatch):
    """Silence is read by the browser as a dead connection.

    The stream carries one SSE chunk per NEW trace step, and the parts
    trace into their own, so while they run nothing new reaches the client.
    The frontend treats 150s of silence as a dead connection
    (STREAM_STALL_TIMEOUT_MS in frontend/src/lib/api.ts) and replaces the
    answer with "Connection seems stalled".

    MEASURED: the two-part question these run concurrently for took 142s.
    Eight seconds of margin on a question that already works.
    """
    scope = scope_of()
    parent = Runtime(FakeStore())
    parent.trace = RecordingTrace()

    async def slow_run(self, run_id, question, *a, **kw):
        await asyncio.sleep(0.25)
        return answered("done: " + question)

    monkeypatch.setattr(Runtime, "_run", slow_run, raising=True)
    monkeypatch.setenv("EVIDENCE_PARTS_CONCURRENT", "true")
    monkeypatch.setenv("EVIDENCE_PARTS_HEARTBEAT_SECONDS", "0.05")

    answer = asyncio.run(parent._each_part(
        uuid4(), "whole", ["part one", "part two"], scope, "auto", deadline=None))

    assert answer.status == "answered"
    beats = [s for s in parent.trace.steps
             if s["title"] == "Still answering the parts"]
    assert beats, (
        "nothing was said for the whole duration of the parts, which is "
        "exactly what the frontend gives up on")
    assert all(b["detail"]["of"] == 2 for b in beats)
    assert beats[0]["detail"]["finished"] < 2, (
        "a heartbeat should report work still outstanding, not completion")


def test_no_heartbeat_once_the_parts_are_done(monkeypatch):
    """A heartbeat after the work finished would be noise in the trace."""
    scope = scope_of()
    parent = Runtime(FakeStore())
    parent.trace = RecordingTrace()

    async def quick_run(self, run_id, question, *a, **kw):
        return answered("done: " + question)

    monkeypatch.setattr(Runtime, "_run", quick_run, raising=True)
    monkeypatch.setenv("EVIDENCE_PARTS_CONCURRENT", "true")
    monkeypatch.setenv("EVIDENCE_PARTS_HEARTBEAT_SECONDS", "5")

    asyncio.run(parent._each_part(
        uuid4(), "whole", ["a", "b"], scope, "auto", deadline=None))
    assert not [s for s in parent.trace.steps
                if s["title"] == "Still answering the parts"]
