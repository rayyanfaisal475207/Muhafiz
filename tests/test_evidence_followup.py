"""Follow-ups must reuse what was established and replace only what changed.

"and Karachi?" after "weapons in Lahore" is one question with one element
replaced. If the subject and measure are not carried, the follow-up is a
new question missing most of its meaning -- measured earlier as a
clarification request asking what the user wanted to know about Karachi.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.evidence.runtime import Runtime
from src.evidence.types import ConversationState, QuestionPlan, QueryOperation, Scope


class Capturing:
    """Records every payload, answers conversationally so nothing executes."""

    def __init__(self):
        self.payloads = []
        self.diagnostics = []

    async def structured(self, contract, system, payload, **kwargs):
        # Reading the question is a separate contract from planning it.
        # CLASSIFICATION is its own call now, decided before the question is
        # read, so like QueryUnderstanding it is answered here instead of
        # consuming a scripted plan -- otherwise every scripted output
        # shifts by one.
        if contract.__name__ == "QuestionClass":
            return contract.model_validate(
                {"shape": "single", "category": "other", "asks": 1})
        if contract.__name__ == "QueryUnderstanding":
            return contract.model_validate({"language": "en", "parts": [], "terms": []})
        self.payloads.append(payload)
        return QuestionPlan(question=payload["question"],
                            standalone_question=payload["question"],
                            conversational_response="noted")


def store_stub():
    store = AsyncMock()
    store.catalog = AsyncMock(return_value={})
    store.resolve = AsyncMock(return_value=[])
    store.resolve_mentions = AsyncMock(return_value=[])
    store.ground_forms = AsyncMock(return_value={})
    return store


def prior_plan():
    return QuestionPlan(
        question="weapons in Lahore", standalone_question="weapons in Lahore",
        language="en",
        outputs=[QueryOperation(output_id="a", operation="count_entities",
                                subject_type=uuid4())])


def test_the_previous_plan_reaches_the_planner_on_a_followup():
    scope = Scope(user_id=uuid4(), session_id=uuid4())
    state = ConversationState(scope_fingerprint=scope.fingerprint(),
                              plan=prior_plan())
    model = Capturing()
    asyncio.run(Runtime(store_stub(), model).run(
        uuid4(), "and Karachi?", scope, state,
        [{"role": "user", "content": "weapons in Lahore"}]))
    conversation = model.payloads[0]["conversation"]
    assert conversation is not None, "a follow-up must see the prior turn"
    assert conversation["plan"]["standalone_question"] == "weapons in Lahore"
    assert conversation["plan"]["outputs"], "the prior measure must be visible"


def test_recent_turns_reach_the_planner():
    scope = Scope(user_id=uuid4(), session_id=uuid4())
    model = Capturing()
    asyncio.run(Runtime(store_stub(), model).run(
        uuid4(), "and Karachi?", scope, None,
        [{"role": "user", "content": "weapons in Lahore"}]))
    assert model.payloads[0]["recent_turns"], "history must be supplied"


def test_a_scope_change_discards_the_previous_context():
    """A user whose access changed must not inherit a plan built under the
    old scope, or a follow-up could reach records now forbidden."""
    first = Scope(user_id=uuid4(), session_id=uuid4(), case_ids=["fir-1"])
    state = ConversationState(scope_fingerprint=first.fingerprint(),
                              plan=prior_plan())
    changed = Scope(user_id=first.user_id, session_id=first.session_id,
                    case_ids=["fir-2"])
    model = Capturing()
    asyncio.run(Runtime(store_stub(), model).run(
        uuid4(), "and Karachi?", changed, state,
        [{"role": "user", "content": "weapons in Lahore"}]))
    assert model.payloads[0]["conversation"] is None
    assert not model.payloads[0]["recent_turns"]


def test_a_pending_clarification_is_carried_into_the_next_turn():
    scope = Scope(user_id=uuid4(), session_id=uuid4())
    state = ConversationState(scope_fingerprint=scope.fingerprint(),
                              plan=prior_plan(),
                              pending_clarification="which station?")
    model = Capturing()
    asyncio.run(Runtime(store_stub(), model).run(
        uuid4(), "Model Town", scope, state, []))
    conversation = model.payloads[0]["conversation"]
    assert conversation["pending_clarification"] == "which station?"
