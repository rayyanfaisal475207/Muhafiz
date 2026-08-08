"""
`cases.conflicts_checked_at` — the conflict-detection completion marker
(migration 019).

The marker's whole value depends on WHEN it is written. Written on the
background task's return, a timeline query racing an in-flight detection finds
nothing and correctly reports "not checked". Written at schedule time, it would
assert the check had happened before it had — reintroducing the exact false
all-clear the three-state ConflictState exists to prevent.
"""
from __future__ import annotations

import pytest

from src.ingestion import conflict_bg


@pytest.fixture
def case_gateway(gateway, monkeypatch):
    gateway.cases["CASE-A"] = {"case_id": "CASE-A"}

    async def _get_gateway():
        return gateway

    monkeypatch.setattr(conflict_bg, "get_gateway", _get_gateway)
    return gateway


async def test_marker_written_when_detection_completes(case_gateway, monkeypatch):
    async def _detect(_case_id):
        return None

    monkeypatch.setattr(
        "src.graph.conflict_detection.detect_conflicts", _detect, raising=False
    )

    await conflict_bg._run_conflict_detection_bg("CASE-A", "DOC-1")

    assert case_gateway.cases["CASE-A"].get("conflicts_checked_at")


async def test_marker_written_for_the_early_return_path(case_gateway, monkeypatch):
    """
    detect_conflicts() returns early on fewer than two incidents. That is a
    COMPLETED check with nothing to find — not an unchecked case — so the
    marker must still be written. Indistinguishable from the normal path here
    because detect_conflicts() returns None either way, which is precisely why
    the marker lives at the call site rather than inside it.
    """
    async def _detect_early(_case_id):
        return None  # what the <2-incident branch does

    monkeypatch.setattr(
        "src.graph.conflict_detection.detect_conflicts", _detect_early, raising=False
    )

    await conflict_bg._run_conflict_detection_bg("CASE-A", "DOC-1")

    assert case_gateway.cases["CASE-A"].get("conflicts_checked_at")


async def test_marker_not_written_when_detection_raises(case_gateway, monkeypatch):
    """A failed check is not a check. The marker must stay absent."""
    async def _detect_boom(_case_id):
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr(
        "src.graph.conflict_detection.detect_conflicts", _detect_boom, raising=False
    )

    await conflict_bg._run_conflict_detection_bg("CASE-A", "DOC-1")

    assert not case_gateway.cases["CASE-A"].get("conflicts_checked_at")


async def test_marker_written_after_detection_not_before(case_gateway, monkeypatch):
    """
    THE RACE. Asserts ordering directly: at the moment detect_conflicts() is
    entered, the marker must not yet exist. A query arriving then must read
    "not checked".
    """
    seen: dict = {}

    async def _detect(_case_id):
        seen["marker_during_detection"] = case_gateway.cases["CASE-A"].get(
            "conflicts_checked_at"
        )

    monkeypatch.setattr(
        "src.graph.conflict_detection.detect_conflicts", _detect, raising=False
    )

    await conflict_bg._run_conflict_detection_bg("CASE-A", "DOC-1")

    assert seen["marker_during_detection"] is None, (
        "the marker was set before detection ran — a racing query would read a "
        "clean check that had not happened"
    )
    assert case_gateway.cases["CASE-A"].get("conflicts_checked_at")


async def test_marker_failure_does_not_fail_the_ingestion_job(case_gateway, monkeypatch):
    """
    A missing marker degrades Timeline Building to UNKNOWN, which is safe.
    Failing the whole ingestion job over it would not be.
    """
    async def _detect(_case_id):
        return None

    async def _mark_boom(_case_id):
        raise RuntimeError("db unavailable")

    monkeypatch.setattr(
        "src.graph.conflict_detection.detect_conflicts", _detect, raising=False
    )
    monkeypatch.setattr(case_gateway, "mark_conflicts_checked", _mark_boom)

    await conflict_bg._run_conflict_detection_bg("CASE-A", "DOC-1")

    statuses = [j.get("status") for j in case_gateway.jobs]
    assert "failed" not in statuses
