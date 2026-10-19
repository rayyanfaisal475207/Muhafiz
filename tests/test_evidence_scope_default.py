"""What a bare reference means, and why the same question is never asked twice.

MEASURED in one session on 19 October 2026, two turns apart, with all 73
cases in scope and no case ever named:

    In this case, what item was reported stolen, and what does section 379
    PPC actually cover ...
        -> Which specific case do you mean by "this case"? Provide the FIR
           number or the exact source case reference as written in ...

    i am taking genrally in whihc case the items were stolen etc
        -> Which specific case do you mean by "this case"? Provide the FIR
           number or the exact source case reference as written in ...

The reader answered -- they said they meant all of them -- and was asked the
identical question again. Two separate defects:

  * the planner was never told what the reader has open, so it could not
    know that no case was selected and that "this case" therefore has no
    antecedent. Scope now reaches it as `what_the_reader_has_open`.

  * nothing stopped a clarification repeating. Each planner call is fresh,
    so the loop is invisible from inside one; it has to be refused outside.

The scope rule itself: one case in scope means that case, more than one
means none is selected and a bare reference means the whole scope.
"""
from uuid import UUID, uuid4

import pytest

from src.evidence.runtime import asked_this_already
from src.evidence.types import (ConversationState, QueryOperation,
                                QuestionPlan, Scope)

FIR = UUID("aaaaaaaa-1111-5111-8111-111111111111")


def scope_over(n):
    return Scope(user_id=uuid4(), session_id=uuid4(),
                 case_ids=[f"case-{i}" for i in range(n)])


def state_having_asked(question, scope=None):
    scope = scope or scope_over(73)
    return ConversationState(scope_fingerprint=scope.fingerprint(),
                             pending_clarification=question)


def clarifying(text):
    return QuestionPlan(question="q", language="en", standalone_question="q",
                        outputs=[], clarification=text)


def answering():
    return QuestionPlan(
        question="q", language="en", standalone_question="q",
        outputs=[QueryOperation(output_id="n", operation="count_entities",
                                subject_type=FIR)])


# ── One case selected, or none: the scope answers a bare reference ────────

def test_one_case_in_scope_means_a_case_was_selected():
    """The reader opened it, so every bare reference means that case."""
    assert len(scope_over(1).case_ids) == 1


def test_many_cases_in_scope_means_no_case_was_selected():
    """`case_id` given yields exactly one case; the whole roster yields many.

    So the count alone distinguishes "this case" from "no case", which is
    what the planner needs and never used to be told.
    """
    assert len(scope_over(73).case_ids) > 1


# ── Never ask the same thing twice ────────────────────────────────────────

ALREADY_ASKED = 'Which specific case do you mean by "this case"?'


def test_a_repeated_clarification_is_refused():
    """The regression, verbatim: the same question two turns running."""
    assert asked_this_already(
        clarifying(ALREADY_ASKED),
        state_having_asked(ALREADY_ASKED)) == ALREADY_ASKED


def test_any_second_clarification_is_refused_however_it_is_worded():
    """Rewording is not progress; the reader still cannot answer it.

    The check is on there being a pending question at all, not on the two
    texts matching, so a planner that paraphrases cannot slip past.
    """
    assert asked_this_already(
        clarifying("Could you tell me which FIR number you have in mind?"),
        state_having_asked(ALREADY_ASKED)) == ALREADY_ASKED


def test_a_first_clarification_is_allowed():
    """Nothing pending means nothing has been asked yet. Asking once is fine."""
    scope = scope_over(73)
    fresh = ConversationState(scope_fingerprint=scope.fingerprint())
    assert asked_this_already(clarifying(ALREADY_ASKED), fresh) is None


def test_a_clarification_on_the_very_first_turn_is_allowed():
    assert asked_this_already(clarifying(ALREADY_ASKED), None) is None


def test_a_plan_that_answers_is_never_refused():
    """The guard must only ever block a question back, never an answer."""
    assert asked_this_already(answering(), state_having_asked(ALREADY_ASKED)) is None


def test_a_note_travelling_with_an_answer_is_not_a_clarification():
    """Outputs plus a note is a real answer with a caveat, which is fine."""
    plan = QuestionPlan(
        question="q", language="en", standalone_question="q",
        clarification="I read this as covering every case.",
        outputs=[QueryOperation(output_id="n", operation="count_entities",
                                subject_type=FIR)])
    assert asked_this_already(plan, state_having_asked(ALREADY_ASKED)) is None


@pytest.mark.parametrize("pending", ["", "   ", None])
def test_a_blank_pending_question_does_not_count_as_having_asked(pending):
    """Only a real previous question means the reader has replied to one."""
    scope = scope_over(73)
    state = ConversationState(scope_fingerprint=scope.fingerprint(),
                              pending_clarification=pending)
    assert asked_this_already(clarifying(ALREADY_ASKED), state) is None
