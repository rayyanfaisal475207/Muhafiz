"""
Shadow mode: the harness runs on real traffic and shows nobody.

The invariant every test here defends is that **a shadow run cannot change,
delay, or break the answer a user receives**. Most of these are written as
sabotage tests — they make the harness fail in a specific way and assert the
request path is unaffected — because "it worked when I tried it" is not
evidence for a code path that only runs in production, on someone else's query,
where nobody is watching.
"""
from __future__ import annotations

import asyncio

import pytest

from src import config
from src.pipeline.harness import shadow


class _FakeGateway:
    """Records shadow rows instead of writing them."""

    def __init__(self, fail: bool = False):
        self.rows: list[dict] = []
        self.fail = fail
        self.step_logs: list[tuple] = []

    async def log_harness_shadow_run(self, data: dict):
        if self.fail:
            raise RuntimeError("database unavailable")
        self.rows.append(data)
        return "shadow-1"

    async def log_step(self, *args, **kwargs):
        # If shadow mode ever routes events here, admin analytics would start
        # counting queries no user ever saw.
        self.step_logs.append((args, kwargs))


@pytest.fixture
def shadow_on(monkeypatch):
    """Enable shadow mode deterministically: every eligible query is sampled."""
    monkeypatch.setattr(config, "HARNESS_SHADOW_MODE", True)
    monkeypatch.setattr(config, "HARNESS_SHADOW_SAMPLE_RATE", 1.0)
    monkeypatch.setattr(config, "HARNESS_SHADOW_MAX_CONCURRENCY", 1)
    monkeypatch.setattr(
        config, "HARNESS_SHADOW_ROUTES", frozenset({"RAG", "GRAPH", "GRAPH_HYBRID"})
    )


@pytest.fixture(autouse=True)
def _reset_in_flight():
    """A leaked slot would wedge shadow mode off for every later test."""
    shadow._in_flight = 0
    yield
    shadow._in_flight = 0


def _kwargs(gateway, **over):
    base = dict(
        gateway=gateway,
        session_id="11111111-2222-3333-4444-555555555555",
        user_id="4939e74f-543b-4143-a997-49a86bc98da6",
        user_role="investigator",
        query_text="what was stolen",
        case_id="CASE-1",
        legacy_route="RAG",
        legacy_outcome="done",
    )
    base.update(over)
    return base


# ══════════════════════════════════════════════════════════════════════════
# Off by default — the single most important property
# ══════════════════════════════════════════════════════════════════════════

def test_shadow_mode_is_off_by_default():
    """
    Enabling shadow mode doubles retrieval and generation load against the same
    model server the live path depends on. That must be a deliberate operator
    decision, never something a deployment turns on by existing.
    """
    assert config.HARNESS_SHADOW_MODE is False


def test_spawn_does_nothing_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "HARNESS_SHADOW_MODE", False)
    spawned = []
    monkeypatch.setattr(
        shadow.asyncio, "get_running_loop",
        lambda: pytest.fail("must not touch the event loop when disabled"),
    )
    shadow.maybe_spawn_shadow(**_kwargs(_FakeGateway()))
    assert spawned == []


# ══════════════════════════════════════════════════════════════════════════
# Sampling and route eligibility
# ══════════════════════════════════════════════════════════════════════════

def test_ineligible_routes_are_never_shadowed(shadow_on):
    """
    Cross-case routes arm cross-case RLS scope. Doing that outside the request
    that authorized it, for output nobody reads, is not a trade worth making.
    """
    for route in ("XGRAPH", "XAGG", "XNETWORK", "DIRECT"):
        should, reason = shadow._sampled(route)
        assert should is False, f"{route} must not be eligible"
        assert "not eligible" in reason


def test_eligible_route_is_sampled_at_full_rate(shadow_on):
    should, reason = shadow._sampled("RAG")
    assert should is True
    assert "sampled" in reason


def test_sample_rate_zero_selects_nothing(shadow_on, monkeypatch):
    monkeypatch.setattr(config, "HARNESS_SHADOW_SAMPLE_RATE", 0.0)
    assert shadow._sampled("RAG")[0] is False


# ══════════════════════════════════════════════════════════════════════════
# The concurrency cap
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_concurrency_cap_skips_rather_than_queues(shadow_on):
    """
    Skipping, never queuing. A queue would let shadow work outlive the traffic
    that produced it, turning a burst into a backlog that competes with live
    requests for minutes afterwards.
    """
    assert await shadow._acquire_slot() is True
    assert await shadow._acquire_slot() is False, "second run must be refused"
    await shadow._release_slot()
    assert await shadow._acquire_slot() is True


@pytest.mark.asyncio
async def test_slot_is_released_even_when_the_harness_crashes(shadow_on, monkeypatch):
    """
    A leaked slot wedges shadow mode off until the process restarts — a silent
    failure that looks identical to the feature being disabled.
    """
    async def exploding_invoke(*args, **kwargs):
        raise RuntimeError("harness exploded")

    monkeypatch.setattr(shadow.supervisor, "invoke", exploding_invoke)
    monkeypatch.setattr(shadow, "_route_for", None, raising=False)

    gw = _FakeGateway()
    await shadow.run_shadow(**_kwargs(gw, route_result={"route": "RAG"}))

    assert shadow._in_flight == 0, "the slot must be released on the failure path"
    assert await shadow._acquire_slot() is True


# ══════════════════════════════════════════════════════════════════════════
# Failure containment
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_harness_crash_is_recorded_not_raised(shadow_on, monkeypatch):
    """
    A shadow crash is a FINDING — it means this query shape would have failed
    for a real user had the harness been serving. Recorded, never propagated.
    """
    async def exploding_invoke(*args, **kwargs):
        raise RuntimeError("harness exploded")

    monkeypatch.setattr(shadow.supervisor, "invoke", exploding_invoke)
    gw = _FakeGateway()

    await shadow.run_shadow(**_kwargs(gw, route_result={"route": "RAG"}))

    assert len(gw.rows) == 1
    assert "harness exploded" in gw.rows[0]["error"]


@pytest.mark.asyncio
async def test_a_logging_failure_is_swallowed(shadow_on, monkeypatch):
    """
    By the time this runs the user's answer is already delivered. Losing a
    diagnostic row is always preferable to raising from a path they cannot see.
    """
    async def ok_invoke(*args, **kwargs):
        class _S:
            selected_agent = "semantic_search"
            result = None
            events = []
        return _S()

    monkeypatch.setattr(shadow.supervisor, "invoke", ok_invoke)
    gw = _FakeGateway(fail=True)

    # Must not raise.
    await shadow.run_shadow(**_kwargs(gw, route_result={"route": "RAG"}))


@pytest.mark.asyncio
async def test_an_unrecognised_role_is_refused_not_guessed(shadow_on, monkeypatch):
    """
    The harness's cross-case gates are driven by this value. Defaulting an
    unknown role to anything would be a silent privilege decision.
    """
    called = []

    async def spy_invoke(*args, **kwargs):
        called.append(True)

    monkeypatch.setattr(shadow.supervisor, "invoke", spy_invoke)
    gw = _FakeGateway()

    await shadow.run_shadow(
        **_kwargs(gw, user_role="root", route_result={"route": "RAG"})
    )

    assert called == [], "the harness must not run for an unrecognised role"


# ══════════════════════════════════════════════════════════════════════════
# Isolation from the live pipeline's own telemetry
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_shadow_events_never_reach_pipeline_steps(shadow_on, monkeypatch):
    """
    `pipeline_steps` is what admin analytics read. A shadow run appearing there
    would corrupt route mix, verifier pass rate and latency with traffic no user
    ever saw.
    """
    seen = {}

    async def capture_invoke(agent_input, route_result, events=None, **kwargs):
        seen["recorder"] = events

        class _S:
            selected_agent = "semantic_search"
            result = None
            events = []
        return _S()

    monkeypatch.setattr(shadow.supervisor, "invoke", capture_invoke)
    gw = _FakeGateway()

    await shadow.run_shadow(**_kwargs(gw, route_result={"route": "RAG"}))

    recorder = seen["recorder"]
    assert recorder is not None
    # Both conditions independently prevent persistence; assert both, because
    # either one silently changing would re-enable the write.
    assert recorder.run_id is None
    assert recorder._gateway is None
    assert gw.step_logs == [], "no shadow step may be written to pipeline_steps"


@pytest.mark.asyncio
async def test_shadow_never_requests_a_file_output(shadow_on, monkeypatch):
    """
    A shadow run taking the file path would write a real PDF to disk and a real
    row to generated_files, for a report nobody asked for.
    """
    seen = {}

    async def capture_invoke(agent_input, route_result, events=None, **kwargs):
        seen["output_format"] = agent_input.output_format

        class _S:
            selected_agent = "semantic_search"
            result = None
            events = []
        return _S()

    monkeypatch.setattr(shadow.supervisor, "invoke", capture_invoke)

    await shadow.run_shadow(
        **_kwargs(_FakeGateway(), route_result={"route": "RAG"})
    )

    assert seen["output_format"] == "chat"


# ══════════════════════════════════════════════════════════════════════════
# What gets recorded
# ══════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_a_successful_run_records_the_comparable_fields(shadow_on, monkeypatch):
    from src.pipeline.harness.contracts import (
        Citation, SubAgentResult, SubAgentStatus,
    )

    async def ok_invoke(*args, **kwargs):
        class _S:
            selected_agent = "semantic_search"
            events = []
            result = SubAgentResult(
                status=SubAgentStatus.PARTIAL,
                answer_text="An answer [Document 1]",
                citations=[Citation(document_index=1, source_tool="RAG")],
                tools_used=["RAG"],
                degraded_from=["GRAPH"],
                caveats=["a stated gap"],
            )
        return _S()

    monkeypatch.setattr(shadow.supervisor, "invoke", ok_invoke)
    gw = _FakeGateway()

    await shadow.run_shadow(**_kwargs(gw, route_result={"route": "RAG"}))

    row = gw.rows[0]
    assert row["harness_status"] == "partial"
    assert row["harness_answer"] == "An answer [Document 1]"
    assert row["citation_count"] == 1
    assert row["tools_used"] == ["RAG"]
    assert row["degraded_from"] == ["GRAPH"]
    assert row["caveats"] == ["a stated gap"]
    assert row["legacy_route"] == "RAG"
    assert row["duration_ms"] is not None


def test_outcome_agreement_is_coarse_by_design():
    """
    Compares whether both paths ANSWERED, not whether the prose matches — no
    automatic check can judge that, and a false "they agree" would bury the
    disagreements the table exists to surface.
    """
    assert shadow._outcomes_agree("done", "ok") is True
    assert shadow._outcomes_agree("done", "partial") is True
    assert shadow._outcomes_agree("done", "abstained") is False
    assert shadow._outcomes_agree("error", "abstained") is True


# ══════════════════════════════════════════════════════════════════════════
# Configuration validation
# ══════════════════════════════════════════════════════════════════════════

def test_config_validation_catches_a_rate_that_samples_nothing(monkeypatch):
    """
    Shadow mode is fire-and-forget, so a bad setting would otherwise present as
    "it logs nothing" — indistinguishable from being switched off.
    """
    monkeypatch.setattr(config, "HARNESS_SHADOW_MODE", True)
    monkeypatch.setattr(config, "HARNESS_SHADOW_SAMPLE_RATE", 0.0)
    warnings, _critical = config.validate_config()
    assert any("HARNESS_SHADOW_SAMPLE_RATE" in w for w in warnings)


def test_config_validation_catches_a_concurrency_cap_below_one(monkeypatch):
    monkeypatch.setattr(config, "HARNESS_SHADOW_MODE", True)
    monkeypatch.setattr(config, "HARNESS_SHADOW_MAX_CONCURRENCY", 0)
    warnings, _critical = config.validate_config()
    assert any("HARNESS_SHADOW_MAX_CONCURRENCY" in w for w in warnings)


def test_config_validation_is_silent_when_shadow_mode_is_off(monkeypatch):
    """An operator who never enabled the feature must not be warned about it."""
    monkeypatch.setattr(config, "HARNESS_SHADOW_MODE", False)
    monkeypatch.setattr(config, "HARNESS_SHADOW_SAMPLE_RATE", 0.0)
    warnings, critical = config.validate_config()
    assert not any("HARNESS_SHADOW" in m for m in [*warnings, *critical])
