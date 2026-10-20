"""A correct plan must not be rejected over English morphology.

`unused_terms` exists to catch a plan that answers a different question --
the fifty-summary dump that referred to nothing the question asked about and
answered anyway. That check earns its place only if it rejects THAT and
nothing else.

MEASURED, which is why this file exists: asked "how many complainants are
recorded", gpt-5.5 planned

    count_entities(person) where `complainant of` exists

-- the correct plan, the one that returns 71 -- and the gate rejected it
eight times in a row, because the test was

    "complainants" in "... complainant of ..."

which is False: the plural has an "s" where the label has a space. The
identical question about "accused" passed, for no better reason than that
"accused" is a substring of "accused in". The repair ladder then spent all
seven rungs re-proposing the plan it already had, and the run ended
"unavailable" for a question the system answers correctly.
"""
from uuid import UUID, uuid4

import pytest

from src.evidence.runtime import _same_word, unused_terms
from src.evidence.types import (Concept, PredicateFilter, QueryOperation,
                                QuestionPlan, QueryUnderstanding, TermReading)

PERSON = UUID("11111111-1111-5111-8111-111111111111")
COMPLAINANT_OF = UUID("22222222-2222-5222-8222-222222222222")
SUMMARY = UUID("33333333-3333-5333-8333-333333333333")


def concept(cid, label, kind="property"):
    return Concept(concept_id=cid, label=label, kind=kind,
                   meaning=f"what {label} records", grain="one record")


CONCEPTS = {
    PERSON: concept(PERSON, "person", "entity_type"),
    COMPLAINANT_OF: concept(COMPLAINANT_OF, "complainant of", "relation"),
    SUMMARY: concept(SUMMARY, "FIR summary"),
}


def plan(*outputs):
    return QuestionPlan(question="q", language="en", standalone_question="q",
                        outputs=list(outputs))


def understanding(*terms):
    return QueryUnderstanding(
        language="en", parts=[],
        terms=[TermReading(term=t, variants=list(v)) for t, v in terms])


def counting_complainants():
    """The plan that returns 71 -- the one that was being thrown away."""
    return plan(QueryOperation(
        output_id="n", operation="count_entities", subject_type=PERSON,
        filters=[PredicateFilter(predicate_id=COMPLAINANT_OF, operator="exists")]))


def test_a_plural_question_word_matches_a_singular_predicate_label():
    """The regression. "complainants" IS used by a `complainant of` filter."""
    missing = unused_terms(
        counting_complainants(),
        understanding(("complainants", []), ("recorded", [])),
        CONCEPTS,
        grounded=["complainants", "recorded"])
    assert missing == [], (
        "the plan filters on `complainant of`; the term is used, in the only "
        "place a count can use it")


@pytest.mark.parametrize("term,label", [
    ("complainants", "complainant of"),      # plural noun vs singular label
    ("accused", "accused in"),               # the case that accidentally worked
    ("stations", "police station"),          # plural, and a label of two words
    ("registered", "registration date"),     # participle vs noun
])
def test_a_question_word_reaches_the_label_that_carries_it(term, label):
    """One shared stem is enough, whatever ending either side happens to wear."""
    cid = uuid4()
    concepts = dict(CONCEPTS)
    concepts[cid] = concept(cid, label)
    p = plan(QueryOperation(
        output_id="n", operation="count_entities", subject_type=PERSON,
        filters=[PredicateFilter(predicate_id=cid, operator="exists")]))
    assert unused_terms(p, understanding((term, [])), concepts,
                        grounded=[term]) == []


def test_the_fifty_summary_dump_is_still_refused():
    """The failure the gate was built for must still be caught.

    `list(FIR summary)` with no filter returned every summary in the corpus
    -- pistols, fraud, kidnapping -- as an answer about a stolen
    motorcycle. Nothing is mechanically wrong with it, which is exactly why
    a relationship check between question and plan is the only thing that
    catches it.
    """
    p = plan(QueryOperation(output_id="n", operation="list",
                            subject_type=PERSON, predicate_id=SUMMARY))
    missing = unused_terms(p, understanding(("motorcycle", ["موٹر سائیکل"])),
                           CONCEPTS, grounded=["motorcycle"])
    assert missing == ["motorcycle"], (
        "a plan that mentions nothing the question asked about is the case "
        "this gate exists to refuse")


def test_a_term_the_records_do_not_contain_is_not_the_plans_fault():
    """Absence is a real answer, reported elsewhere; it is not a bad plan."""
    assert unused_terms(counting_complainants(),
                        understanding(("Bhara Kahu", [])),
                        CONCEPTS, grounded=[]) == []


def test_a_clarification_is_exempt():
    """Asking which کاشف is meant is a response to a term, not a dropped one."""
    asking = QuestionPlan(question="q", language="en", standalone_question="q",
                          outputs=[], clarification="Which Kashif do you mean?")
    assert unused_terms(asking, understanding(("Kashif", [])),
                        CONCEPTS, grounded=["Kashif"]) == []


class TestSameWord:
    """What counts as the same word, and what must not."""

    @pytest.mark.parametrize("a,b", [
        ("complainant", "complainants"),
        ("station", "stations"),
        ("register", "registered"),
        ("مدعی", "مدعی"),
    ])
    def test_inflections_of_one_word_match(self, a, b):
        assert _same_word(a, b) and _same_word(b, a)

    @pytest.mark.parametrize("a,b", [
        ("station", "destination"),   # a substring, but shares no stem
        ("car", "carried"),           # too short to stand in for the longer
        ("accused", "complainant"),
        ("FIR", "FIRST"),             # three letters is below the floor
        ("complain", "complete"),     # five shared letters is not a stem
    ])
    def test_different_words_do_not_match(self, a, b):
        assert not _same_word(a, b) and not _same_word(b, a)


# ── A selected case is a narrowing, and it is not in the plan ─────────────

FIR_TYPE = UUID("44444444-4444-5444-8444-444444444444")
CONCEPTS_WITH_FIR = {**CONCEPTS,
                     FIR_TYPE: concept(FIR_TYPE, "FIR", "entity_type")}


def scope_over(*case_ids):
    from src.evidence.types import Scope
    from uuid import uuid4
    return Scope(user_id=uuid4(), session_id=uuid4(), case_ids=list(case_ids))


def the_summary_of_whatever_is_in_scope():
    """list(FIR, FIR summary) with nothing in the plan restricting it.

    Over the whole corpus this is the fifty-summary dump. With one case
    selected it is exactly the right answer to "Summarize this case."
    """
    return plan(QueryOperation(output_id="summary", operation="list",
                               subject_type=FIR_TYPE, predicate_id=SUMMARY))


def asked_to_summarize():
    return understanding(("Summarize", []), ("this case", []))


GROUNDED = ["summarize", "this case"]


def test_one_selected_case_narrows_an_otherwise_unrestricted_list():
    """The regression: the opening question of a case review.

    MEASURED, on "Summarize this case." with FIR 1001/26 selected: the
    planner produced this exact plan, which is correct, and the gate
    rejected it on all seven rungs, so the answer was "Candidate sources
    were retrieved; an answer has not yet been established" at 117s.
    """
    assert unused_terms(the_summary_of_whatever_is_in_scope(),
                        asked_to_summarize(), CONCEPTS_WITH_FIR, GROUNDED,
                        scope_over("fir-1001-26")) == [], (
        "the reader put the case in the case selector, not in a filter; the "
        "scope restricts this plan to one case whether the plan says so or not")


def test_the_fifty_summary_dump_is_still_refused_across_many_cases():
    """The failure this gate exists for must still be caught."""
    dropped = unused_terms(the_summary_of_whatever_is_in_scope(),
                           asked_to_summarize(), CONCEPTS_WITH_FIR, GROUNDED,
                           scope_over(*["case-%d" % i for i in range(73)]))
    assert dropped, (
        "with 73 cases in scope this enumerates the whole corpus and refers "
        "to nothing the question asked -- that is the dump")


def test_no_scope_at_all_behaves_as_it_did():
    """Callers that pass no scope must be unaffected."""
    assert unused_terms(the_summary_of_whatever_is_in_scope(),
                        asked_to_summarize(), CONCEPTS_WITH_FIR, GROUNDED)


def test_an_empty_scope_is_not_a_selected_case():
    assert unused_terms(the_summary_of_whatever_is_in_scope(),
                        asked_to_summarize(), CONCEPTS_WITH_FIR, GROUNDED,
                        scope_over())


def test_a_count_in_one_case_is_still_fine():
    """Counts were never subject to this gate and must stay that way."""
    counting = plan(QueryOperation(output_id="n", operation="count_entities",
                                   subject_type=PERSON))
    assert unused_terms(counting, asked_to_summarize(), CONCEPTS_WITH_FIR,
                        GROUNDED, scope_over("fir-1001-26")) == []
