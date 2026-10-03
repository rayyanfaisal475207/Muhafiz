"""Relative dates need a reference point, and ranges need a convention.

Before this, the planner payload carried no current time at all, so
"this month" was resolved against whatever date the model inferred from
its training data. That is not a reference point; it is a guess that
moves a date range silently, and a monthly count is exactly the question
that guess gets wrong.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock
from uuid import uuid4
from zoneinfo import ZoneInfo

import pytest

from src import config
from src.evidence.runtime import Runtime
from src.evidence.types import QuestionPlan, Scope


class CapturingModel:
    """Records the payload the planner was given, then declines to plan."""

    def __init__(self):
        self.payload = None
        self.diagnostics = []

    async def structured(self, contract, system, payload, **kwargs):
        # Reading the question is a separate contract from planning it; only
        # the PLANNER's payload is the one these tests inspect.
        # CLASSIFICATION is its own call now, decided before the question is
        # read, so like QueryUnderstanding it is answered here instead of
        # consuming a scripted plan -- otherwise every scripted output
        # shifts by one.
        if contract.__name__ == "QuestionClass":
            return contract.model_validate(
                {"shape": "single", "category": "other", "asks": 1})
        if contract.__name__ == "QueryUnderstanding":
            return contract.model_validate({"language": "en", "parts": [], "terms": []})
        self.payload = payload
        return QuestionPlan(question=payload["question"],
                            standalone_question=payload["question"],
                            conversational_response="noted")


def run_with(question, when):
    store = AsyncMock()
    store.catalog = AsyncMock(return_value={})
    store.resolve = AsyncMock(return_value=[])
    store.resolve_mentions = AsyncMock(return_value=[])
    store.ground_forms = AsyncMock(return_value={})
    model = CapturingModel()
    scope = Scope(user_id=uuid4(), session_id=uuid4())
    asyncio.run(Runtime(store, model).run(uuid4(), question, scope, now=when))
    return model.payload


FROZEN = datetime(2026, 6, 15, 9, 30, tzinfo=ZoneInfo("Asia/Karachi"))


def test_the_planner_is_told_what_time_it_is():
    payload = run_with("How many FIRs this month?", FROZEN)
    assert payload["current_time"].startswith("2026-06-15")


def test_the_planner_is_told_which_timezone_applies():
    """'This month' must mean the month where the station is, not where
    the server runs."""
    payload = run_with("How many FIRs this month?", FROZEN)
    assert payload["timezone"] == config.EVIDENCE_TIMEZONE
    assert "+05:00" in payload["current_time"]


def test_half_open_ranges_are_stated_so_a_boundary_event_is_counted_once():
    """An event at midnight on 1 July belongs to July, not to both."""
    payload = run_with("How many FIRs in June 2026?", FROZEN)
    convention = payload["date_range_convention"]
    assert "half-open" in convention
    assert "NOT including" in convention


def test_an_injected_clock_overrides_the_wall_clock():
    """Date-boundary behaviour must be testable against a fixed instant,
    not against the day the suite happens to run."""
    other = datetime(2024, 1, 1, 0, 0, tzinfo=ZoneInfo("Asia/Karachi"))
    assert run_with("q", other)["current_time"].startswith("2024-01-01")
    assert run_with("q", FROZEN)["current_time"].startswith("2026-06-15")
