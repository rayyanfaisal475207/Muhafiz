"""Four questions from one real user's session, and why each one failed.

A teammate logged in, asked five questions, and reported that nothing
worked. Their credentials were fine and the backend was fine. Four of the
five questions failed, and the fifth answered correctly, which is what made
the cause findable:

    13:21  unavailable  "How many citizen-service applications and how many
                         walk-in complaint records are on file?"
    13:24  unavailable  "How many records exist in the criminal records
                         system, and how many are still under trial?"
    13:26  partial      "How many items are logged in the evidence store
                         (malkhana) vs. the weapons register?"
    13:32  pending      "Are all FIRs registered on the standard
                         cognizable-offense form (Form 154)?"
    13:44  answered     "how many police stations are recorded"

Two distinct defects were behind it, and this file pins both so neither can
come back quietly.
"""
import pytest

from src.evidence.runtime import (_is_known_word, asks_about_nothing_recorded,
                                  should_name_unrecorded,
                                  widen_scope_that_excludes_everything)
from src.evidence.types import (Concept, QueryOperation, QuestionPlan,
                                QueryUnderstanding, TermReading)
from uuid import UUID, uuid4

CRIMINAL_RECORD = UUID("11111111-1111-5111-8111-111111111111")
FIR = UUID("22222222-2222-5222-8222-222222222222")


def concept(cid, label, kind="entity_type"):
    return Concept(concept_id=cid, label=label, kind=kind,
                   meaning=f"what {label} records", grain="one record")


CONCEPTS = {CRIMINAL_RECORD: concept(CRIMINAL_RECORD, "criminal record"),
            FIR: concept(FIR, "FIR")}


def plan_counting(subject, scope="case_documents"):
    return QuestionPlan(
        question="q", language="en", standalone_question="q",
        outputs=[QueryOperation(output_id="n", operation="count_entities",
                                subject_type=subject, source_scope=scope)])


class CountingStore:
    """A store that reports a population inside and outside case documents."""

    def __init__(self, in_cases, anywhere):
        self.answer = {"in_case_documents": in_cases, "anywhere": anywhere}
        self.asked = []

    async def population_by_scope(self, subject_type):
        self.asked.append(subject_type)
        return self.answer


class TestScopeThatExcludedWholeRecordTypes:
    """Defect 1: a case-scoped count of a record type with no case link.

    `source_scope="case_documents"` compiles to `cardinality(case_ids)>0`.
    MEASURED on this corpus:

        endpoint            records   attached to a case
        fir                      73   all
        roznamcha                74   all but one
        criminal-records         33   NONE
        pkm                      14   NONE
        cms                       4   NONE

    So a plan counting criminal records that way asked for the intersection
    of "criminal record" and "attached to a case", which is empty. It
    returned 0 over a population of 0 documents, which the runtime rightly
    refuses to call a factual zero -- so the whole run failed, for a
    question whose answer is 33.
    """

    @pytest.mark.asyncio
    async def test_a_population_with_no_case_link_is_widened(self):
        plan = plan_counting(CRIMINAL_RECORD)
        store = CountingStore(in_cases=0, anywhere=33)

        notes = await widen_scope_that_excludes_everything(plan, CONCEPTS, store)

        assert plan.outputs[0].source_scope == "all"
        assert notes and "criminal record" in notes[0]
        assert "33" in notes[0], "the note must say how many records were at stake"

    @pytest.mark.asyncio
    async def test_a_population_that_does_have_case_links_is_left_alone(self):
        """FIRs are attached to cases, so case scope is correct for them."""
        plan = plan_counting(FIR)
        store = CountingStore(in_cases=73, anywhere=75)

        notes = await widen_scope_that_excludes_everything(plan, CONCEPTS, store)

        assert plan.outputs[0].source_scope == "case_documents"
        assert notes == []

    @pytest.mark.asyncio
    async def test_a_genuinely_empty_population_stays_a_real_zero(self):
        """Empty everywhere is a finding, and must not be widened away.

        Widening on an empty population would turn "there are none of these"
        into a different query, and a real zero is the one answer this
        system must never fabricate around.
        """
        plan = plan_counting(CRIMINAL_RECORD)
        store = CountingStore(in_cases=0, anywhere=0)

        notes = await widen_scope_that_excludes_everything(plan, CONCEPTS, store)

        assert plan.outputs[0].source_scope == "case_documents"
        assert notes == []

    @pytest.mark.asyncio
    async def test_a_store_that_cannot_answer_changes_nothing(self):
        """An unknown is never grounds for rewriting a plan."""
        class Broken:
            async def population_by_scope(self, subject_type):
                raise RuntimeError("no connection")

        plan = plan_counting(CRIMINAL_RECORD)
        assert await widen_scope_that_excludes_everything(
            plan, CONCEPTS, Broken()) == []
        assert plan.outputs[0].source_scope == "case_documents"


def understanding(*terms):
    """terms as (text, role) pairs."""
    return QueryUnderstanding(
        language="en", parts=[],
        terms=[TermReading(term=t, role=r) for t, r in terms])


VOCABULARY = ["FIR", "FIR number", "criminal record", "police station",
              "person", "weapon register item detail", "PKM application",
              "CMS complaint", "conviction status"]


class TestAskingAboutSomethingNotRecorded:
    """Defect 2: a question about a field that does not exist.

    MEASURED: "Are all FIRs registered on the standard cognizable-offense
    form (Form 154)?" spent 133 seconds and seven planning attempts before
    reporting "an answer has not yet been established". There is no form
    field in these records -- none of the 41 predicates mentions one -- so
    no plan could ever have answered it, and the reply told the reader
    nothing they could act on.
    """

    def test_a_field_the_records_do_not_have_is_named(self):
        got = asks_about_nothing_recorded(
            understanding(("Form 154", "attribute")), VOCABULARY, [], [], [])
        assert got == ["Form 154"]

    def test_only_a_field_is_ever_reported_missing(self):
        """"No field for that" is a claim about FIELDS, so only attributes.

        MEASURED, two false denials from accepting other roles. "how many
        people are recorded as accused" answered 92 correctly and then
        added "These records carry no field for 'people'" -- `person` is a
        recorded population with 216 entities. And "does Bhara Kahu appear
        anywhere" said "no field for 'Bhara Kahu'", displacing the accurate
        sentence the absence machinery had already produced. A place name is
        not a field.
        """
        # ENTITY is the role that must stay out: both measured false
        # denials were entity terms, and an entity the records lack is
        # answered properly by the absence and clarification paths.
        for term in ("people", "Bhara Kahu", "کاشف"):
            assert asks_about_nothing_recorded(
                understanding((term, "entity")), VOCABULARY, [], [], []) == [], (
                "an entity term must not be reported as a missing field")
        # An attribute or a value IS reported. Both are accepted because the
        # reader tags the same term either way: MEASURED, "Form 154" came
        # back as an attribute on one run and a value on the next, and
        # accepting only attributes made the honest answer appear about half
        # the time.
        for role in ("attribute", "value"):
            assert asks_about_nothing_recorded(
                understanding(("Form 154", role)),
                VOCABULARY, [], [], []) == ["Form 154"], (
                f"a missing field asked as a {role} must still be named")

    def test_a_population_the_records_do_have_is_never_named(self):
        """The dangerous failure: telling an officer FIRs are not recorded.

        MEASURED: the first version did exactly that. `_same_word` has a
        four-character floor so that "car" cannot stand in for "carried",
        and "FIR" is three characters -- so the question's "FIRs" never
        matched the recorded population "FIR", and the answer opened by
        denying a field with 75 records behind it.
        """
        for term in ("FIRs", "FIR", "criminal records", "police stations",
                     "conviction status", "FIR number"):
            assert asks_about_nothing_recorded(
                understanding((term, "attribute")), VOCABULARY, [], [], []) == [], (
                f"{term!r} IS recorded and must never be reported missing")

    def test_filler_is_not_reported_as_a_missing_field(self):
        """Only what the question is ABOUT.

        MEASURED: without the role check the answer opened "These records
        carry no field for 'on file'" -- true, and filler, and it made a
        correct answer look foolish.
        """
        got = asks_about_nothing_recorded(
            understanding(("on file", "other"), ("logged", "other"),
                          ("recently", "time"), ("people", "entity")),
            VOCABULARY, [], [], [])
        assert got == []

    def test_a_recorded_value_counts_as_answerable(self):
        """A value is not a field and is still answerable with a filter.

        "PPC" is no concept's label; it is a VALUE that `statute act`
        holds. Reporting it as unrecorded would be wrong.
        """
        locations = [{"form": "PPC", "holds_this_value": "statute act"}]
        assert asks_about_nothing_recorded(
            understanding(("PPC", "value")), VOCABULARY, locations, [], []) == []

    def test_an_entity_name_counts_as_answerable(self):
        candidates = [{"label": "کاشف"}]       # کاشف
        assert asks_about_nothing_recorded(
            understanding(("کاشف", "attribute")),
            VOCABULARY, [], candidates, []) == []

    def test_nothing_is_claimed_when_there_is_nothing_to_compare_against(self):
        """An empty vocabulary means the check could not run, not that
        everything is missing."""
        assert asks_about_nothing_recorded(
            understanding(("Form 154", "attribute")), [], [], [], []) == []

    def test_a_word_in_a_passage_is_not_a_field(self):
        """Grounded forms are deliberately excluded from the known set.

        MEASURED: the first version included them and never fired at all,
        because the word "form" appears in FIR narrative text and
        "malkhana" appears in passages too. Occurring in prose is not the
        same as having a field, which is the whole distinction this check
        draws.
        """
        got = asks_about_nothing_recorded(
            understanding(("Form 154", "attribute")), VOCABULARY, [], [],
            grounded=["form", "154", "malkhana"])
        assert got == ["Form 154"]

    def test_how_many_terms_can_be_named_is_bounded(self):
        many = understanding(*[(f"unheardof{i}", "attribute") for i in range(9)])
        assert len(asks_about_nothing_recorded(many, VOCABULARY, [], [], [])) <= 4


class TestKnownWord:
    @pytest.mark.parametrize("word", ["fir", "firs", "stations", "records",
                                      "persons", "weapons", "complaints"])
    def test_recorded_things_are_known_including_plurals(self, word):
        known = [w.lower() for label in VOCABULARY for w in label.split()]
        assert _is_known_word(word, known)

    @pytest.mark.parametrize("word", ["malkhana", "form", "forms", "fingerprint"])
    def test_unrecorded_things_are_not_known(self, word):
        known = [w.lower() for label in VOCABULARY for w in label.split()]
        assert not _is_known_word(word, known)



class TestWhenTheMissingFieldNoteIsWorthMaking:
    """The note explains why something could NOT be counted.

    So it belongs on a run that failed to count, and nowhere else. Both
    boundaries were learned from live answers, not reasoned about:

      * The Urdu question asking how many FIRs cite the PPC answered 61
        correctly and then appended "These records carry no field for
        'حوالہ'" -- true, since حوالہ ("reference") is no field's name, and
        pure noise on a complete answer.

      * "does Bhara Kahu appear anywhere in the records" had already
        produced the accurate sentence from an exhaustive check over every
        passage, entity name and value. The note displaced it with a vaguer
        claim about fields, which is the wrong claim about a place name.
    """

    @pytest.mark.parametrize("status", ["partial", "unavailable", "not_extracted"])
    def test_a_run_that_could_not_count_says_why(self, status):
        assert should_name_unrecorded(["Form 154"], status, already_measured=False)

    @pytest.mark.parametrize("status", ["answered", "clarification"])
    def test_a_run_that_answered_gets_no_footnote(self, status):
        assert not should_name_unrecorded(["\u062d\u0648\u0627\u0644\u06c1"], status,
                                          already_measured=False)

    def test_a_measured_absence_outranks_the_note(self):
        """The exhaustive check produced the better sentence; leave it."""
        assert not should_name_unrecorded(["Bhara Kahu"], "partial",
                                          already_measured=True)

    def test_nothing_unrecorded_means_nothing_to_say(self):
        assert not should_name_unrecorded([], "unavailable", already_measured=False)
        assert not should_name_unrecorded(None, "unavailable", already_measured=False)


class TestTheCheckStaysInsideOneScript:
    """A lexical comparison against an English vocabulary needs English.

    The recorded field names are English -- `reporting delay reason`,
    `recovered from` -- so an Urdu or Roman-Urdu phrase cannot match them by
    letters, and reporting it as missing says nothing true about the
    records. MEASURED, both on real questions:

        "30 بور پستول کس کے پاس سے برآمد ہوا؟"
            -> "no field for 'کس کے پاس سے برآمد ہوا'",
               while `recovered from` holds 31 values

        "Kitne cases mein mudai ne ... wajah batai?"
            -> "no field for 'kuch arse baad aane ki wajah'",
               while `reporting delay reason` holds 8
    """

    def test_an_urdu_term_is_never_reported_missing(self):
        recovered = "\u06a9\u0633 \u06a9\u06d2 \u067e\u0627\u0633 \u0633\u06d2 \u0628\u0631\u0622\u0645\u062f"
        assert asks_about_nothing_recorded(
            understanding((recovered, "attribute")), VOCABULARY, [], [], []) == []

    def test_a_roman_urdu_question_is_never_reported_missing(self):
        """Roman Urdu is Latin letters, so only the language tag separates it."""
        said = QueryUnderstanding(
            language="roman_ur", parts=[],
            terms=[TermReading(term="kuch arse baad aane ki wajah", role="attribute")])
        assert asks_about_nothing_recorded(said, VOCABULARY, [], [], []) == []

    def test_an_english_question_still_reports_a_missing_field(self):
        """The check must keep working where it can actually be made."""
        assert asks_about_nothing_recorded(
            understanding(("Form 154", "attribute")), VOCABULARY, [], [], []) == ["Form 154"]
