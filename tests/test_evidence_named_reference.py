"""A named person or place must reach every record of it, and only it.

Three failures, all measured on the live corpus on 19 October 2026, all the
same mistake in different spellings: a restriction the reader NAMED was
dropped, and the answer was then presented with full-coverage wording.

    تھانہ راجہ بازار میں کتنی FIR درج ہیں؟
        planned `recorded at police station = <one station UUID>`
        answered 1, and on an earlier run 73
        truth 4

    How many cases is Kashif involved in?
        planned `accused in exists` with object_name "کاشف" beside it,
        where `exists` discards the name
        answered 68 -- every FIR with any accused at all
        truth 4

    How many cases is Kashif involved in?   (before that)
        returned no outputs and a question back, offering the reader a
        choice between ten candidates all named کاشف
        truth: answerable without asking anything

What connects them is the corpus, not the questions. `canonical_id` is NULL
for all 564 entities here and 71 labels cover 408 of them, so one real
station or person is recorded once per DOCUMENT. An id therefore names one
record of a thing and never the thing, and a name that repeats is normally
one thing recorded many times rather than an ambiguity.
"""
from uuid import UUID

import pytest

from src.evidence.compiler import compile_operation
from src.evidence.runtime import breadth_answers_instead
from src.evidence.types import (Concept, PredicateFilter, QueryOperation,
                                QuestionPlan)

FIR = UUID("aaaaaaaa-1111-5111-8111-111111111111")
STATION = UUID("bbbbbbbb-2222-5222-8222-222222222222")
AT_STATION = UUID("cccccccc-3333-5333-8333-333333333333")
ONE_RECORD_OF_THE_STATION = UUID("dddddddd-4444-5444-8444-444444444444")

CONCEPTS = {
    FIR: Concept(concept_id=FIR, label="FIR", kind="entity_type",
                 meaning="one first information report", grain="one FIR"),
    STATION: Concept(concept_id=STATION, label="police station",
                     kind="entity_type", meaning="a police station",
                     grain="one station"),
    AT_STATION: Concept(concept_id=AT_STATION, label="recorded at police station",
                        kind="relation", value_type="entity",
                        meaning="the station that registered this record",
                        grain="one station per record"),
}


def counting_firs(*filters):
    return QueryOperation(output_id="n", operation="count_entities",
                          subject_type=FIR, filters=list(filters))


def at_the_station(**extra):
    return PredicateFilter(predicate_id=AT_STATION,
                           object_entity_id=ONE_RECORD_OF_THE_STATION, **extra)


# ── An id names one record of a thing; the filter means the thing ─────────

def test_an_entity_id_filter_reaches_every_record_of_that_entity():
    """The Raja Bazar regression: 1 of 4, with "all 73 checked" on top."""
    sql = compile_operation(counting_firs(at_the_station()), CONCEPTS).sql
    assert "evidence.entities q" in sql, (
        "the filter must compare against the entities co-referent with the "
        "given id, not against the id alone")
    assert "q.canonical_id=t.canonical_id" in sql, (
        "a populated canonical_id is the strongest identity available and "
        "must win, so a later resolution pass improves this for free")
    assert "lower(btrim(q.label))=lower(btrim(t.label))" in sql, (
        "with canonical_id NULL everywhere, the recorded name is what "
        "identifies the thing")


def test_co_reference_keeps_two_people_of_one_name_apart():
    """10 entities are labelled کاشف and they hold 6 distinct CNICs.

    Expanding on the name alone would merge six men into one; the
    identifier comparison is the whole reason this is safe.
    """
    sql = compile_operation(counting_firs(at_the_station()), CONCEPTS).sql
    assert "q.identifiers=t.identifiers" in sql, (
        "identifiers must be compared, or records of different people who "
        "share a name are folded together")
    assert "q.identifiers IS NULL OR t.identifiers IS NULL" in sql, (
        "an absent identifier must not block co-reference, or the station "
        "records this exists for stop matching each other")


def test_an_entity_id_filter_still_restricts():
    """Breadth must not become no filter at all."""
    compiled = compile_operation(counting_firs(at_the_station()), CONCEPTS)
    reached = [p for p in compiled.params
               if p == ONE_RECORD_OF_THE_STATION
               or (isinstance(p, list) and ONE_RECORD_OF_THE_STATION in p)]
    assert reached, "the named station must reach the query"
    assert str(AT_STATION) in [str(p) for p in compiled.params], (
        "the relation being filtered must reach the query too")


def test_a_negated_entity_id_filter_negates_the_whole_set():
    """Excluding a station must exclude all four of its records."""
    sql = compile_operation(
        counting_firs(at_the_station(operator="ne")), CONCEPTS).sql
    assert "NOT EXISTS" in sql, (
        "negating one id while the other three records of that station still "
        "match is how an exclusion silently includes what it excluded")


def test_entity_ids_from_a_lookup_also_expand():
    """A lookup returns the records it happened to find, not the things."""
    sql = compile_operation(
        QueryOperation(output_id="n", operation="count_entities",
                       subject_type=FIR,
                       entity_ids=[ONE_RECORD_OF_THE_STATION]),
        CONCEPTS).sql
    assert "evidence.entities q" in sql, (
        "entity_ids pinned to the returned rows reproduces the Raja Bazar "
        "undercount wherever a lookup feeds a filter")


# ── A filter that names something means equality, not mere presence ───────

def test_exists_beside_a_name_becomes_equality():
    """The Kashif 68. `exists` ignores any value the compiler is handed."""
    f = PredicateFilter(predicate_id=AT_STATION, operator="exists",
                        object_name="کاشف")
    assert f.operator == "eq", (
        "a filter carrying a name must restrict to it; left as `exists` it "
        "reads as restricted and runs over everything")
    assert f.object_name == "کاشف"


def test_not_exists_beside_a_name_becomes_inequality():
    f = PredicateFilter(predicate_id=AT_STATION, operator="not_exists",
                        object_name="کاشف")
    assert f.operator == "ne"


@pytest.mark.parametrize("field,value", [
    ("object_entity_id", ONE_RECORD_OF_THE_STATION),
    ("value", "PPC"),
])
def test_exists_beside_any_kind_of_value_becomes_equality(field, value):
    f = PredicateFilter(predicate_id=AT_STATION, operator="exists",
                        **{field: value})
    assert f.operator == "eq"


def test_a_bare_exists_is_left_alone():
    """Counting the records that carry a field is what `exists` is for."""
    f = PredicateFilter(predicate_id=AT_STATION, operator="exists")
    assert f.operator == "exists", (
        "counting the records that carry a field is a real question and the "
        "only thing `exists` is for")


# ── Several records of one name is not a question worth asking ────────────

def clarifying(text, language="en"):
    # The contract requires a clarification to be written in the language the
    # question was asked in, so an Urdu name in the text means an Urdu plan.
    return QuestionPlan(question="q", language=language, standalone_question="q",
                        outputs=[], clarification=text)


def candidate(label, term):
    return {"label": label, "matched_term": term, "match": "exact"}


def test_identical_names_are_answered_broadly_instead_of_asked_about():
    """The regression: ten candidates, all کاشف, offered as a choice."""
    name = breadth_answers_instead(
        clarifying("Which person named کاشف do you mean? Pick one by CNIC."),
        [candidate("کاشف", "کاشف")] * 10)
    assert name == "کاشف", (
        "every candidate carries the same name, so the choice offered is "
        "between indistinguishable options and the question asks about all")


def test_two_different_names_remain_a_real_ambiguity():
    """کاشف and کاشف محمود are different people; that is worth asking."""
    assert breadth_answers_instead(
        clarifying("کیا آپ کا مطلب کاشف ہے یا کاشف محمود؟", language="ur"),
        [candidate("کاشف", "کاشف"), candidate("کاشف محمود", "کاشف")]) is None


def test_a_single_candidate_is_not_a_clarification_to_refuse():
    assert breadth_answers_instead(
        clarifying("Which کاشف do you mean? Please give a CNIC."),
        [candidate("کاشف", "کاشف")]) is None


def test_an_unrelated_clarification_is_left_alone():
    """The question may name a duplicated entity and ask something else.

    Nothing about a date range is answered by broadening a name, so the
    term has to appear in what the planner actually asked.
    """
    assert breadth_answers_instead(
        clarifying("Do you mean this month or last month?"),
        [candidate("کاشف", "کاشف")] * 10) is None


def test_a_plan_that_has_outputs_is_never_touched():
    """A note travelling with a real answer is not a refusal to answer."""
    plan = QuestionPlan(
        question="q", language="en", standalone_question="q",
        clarification="Which کاشف do you mean? Please give a CNIC.",
        outputs=[counting_firs()])
    assert breadth_answers_instead(plan, [candidate("کاشف", "کاشف")] * 10) is None


def test_no_candidates_is_safe():
    """An empty clarification cannot be built -- the contract refuses it --
    so the only degenerate case left is having nothing resolved to broaden.
    """
    asked = clarifying("Which کاشف do you mean? Please give a CNIC.")
    assert breadth_answers_instead(asked, []) is None
    assert breadth_answers_instead(asked, None) is None
