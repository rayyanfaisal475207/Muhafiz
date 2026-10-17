"""Every term in a question, written every way the records might write it.

MEASURED, and the reason this layer exists: asked "how many complainants are
recorded", the correct plan filters on the predicate `complainant of` and
returns 71. It was rejected because nothing connected the plural in the
question to the singular in the records -- the reader had simply not emitted
the singular that run. It emitted it on other runs, which is exactly why the
same question answered sometimes and failed sometimes.

A plural is not a judgement call and does not belong to a model.

These forms are CANDIDATES. `ground_forms` keeps only what occurs, so a
wrong candidate costs a row in one query while a missing one costs an
answer. That asymmetry is why generation is eager -- and why the only thing
worth testing strictly is that the forms that MATTER are produced, and that
obvious waste is not.
"""
import pytest

from src.evidence.forms import expand, expand_all, inflections, pieces

MUDAI = "مدعی"            # مدعی — complainant
THANA = "تھانہ"      # تھانہ — police station


class TestTheRegression:
    """The exact pair that cost three questions."""

    def test_a_plural_question_word_reaches_its_singular(self):
        assert "complainant" in expand("complainants")

    def test_a_singular_question_word_reaches_its_plural(self):
        assert "complainants" in expand("complainant")


@pytest.mark.parametrize("term,wanted", [
    ("stations", "station"),
    ("station", "stations"),
    ("cases", "case"),
    ("case", "cases"),
    ("witness", "witnesses"),       # not "witnesss"
    ("FIRs", "fir"),
    ("entries", "entry"),
    ("entry", "entries"),
])
def test_inflections_in_both_directions(term, wanted):
    assert wanted in expand(term), f"{term} should reach {wanted}"


def test_a_phrase_reaches_its_own_words_and_their_inflections():
    """"police station" must reach a record that writes only "stations"."""
    out = expand("police station")
    for wanted in ("police station", "police stations", "station", "stations"):
        assert wanted in out, f"missing {wanted}"


def test_the_term_as_asked_always_comes_first():
    """A caller that truncates must keep the reading the model meant."""
    for term in ("complainants", "police station", MUDAI):
        assert expand(term)[0] == term


class TestUrdu:
    """The records are written in both scripts, so both must inflect."""

    def test_an_urdu_term_gets_urdu_endings_not_english_ones(self):
        out = expand(MUDAI)
        assert out[0] == MUDAI
        assert len(out) > 1, "an Urdu term must still expand"
        assert not any(f.endswith("s") for f in out), (
            "an English plural is not an Urdu plural")

    def test_an_urdu_plural_ending_can_be_removed(self):
        plural = THANA + "وں"            # تھانہوں
        assert THANA in expand(plural)


class TestWasteIsNotFree:
    """A junk candidate occupies a row in a query that can already time out.

    MEASURED: the grounding query was cancelled at the statement timeout on
    a question producing 31 forms, and every form was then dropped as
    unconfirmed. So generation is eager but not careless.
    """

    @pytest.mark.parametrize("term,junk", [
        ("complainants", "complainantses"),   # already plural
        ("FIRs", "firses"),
        ("registered", "registereds"),        # a participle
        ("2026", "2026s"),                    # a number
        ("June 2026", "june 2026s"),
        ("witness", "witnesss"),              # three s's
    ])
    def test_the_obviously_wrong_form_is_not_generated(self, term, junk):
        assert junk not in expand(term)

    def test_one_term_cannot_spend_the_whole_budget(self):
        """Round-robin, so a wordy term does not starve the plain ones."""
        out = expand_all(["a very long multi word phrase here", "mudai", "PPC"],
                         total=8)
        assert "mudai" in out and "PPC" in out

    def test_every_term_as_given_survives_truncation(self):
        """Derived forms compete for the budget; the originals do not."""
        originals = [f"term{i}" for i in range(12)]
        out = expand_all(originals, total=4)
        for term in originals:
            assert term in out, "an original term must never be dropped"

    def test_the_total_is_bounded(self):
        out = expand_all(["complainants", "police station", "accused persons",
                          MUDAI, "roznamcha entries"], total=20)
        assert len(out) <= 20


class TestEdges:
    def test_nothing_in_nothing_out(self):
        assert expand("") == [] and expand(None) == []
        assert expand_all([]) == [] and expand_all(None) == []

    def test_a_short_word_is_not_inflected(self):
        """"of" and "in" produce nothing worth checking."""
        assert inflections("of") == []

    def test_a_single_word_has_no_pieces(self):
        assert pieces("station") == []

    def test_a_form_is_never_its_own_duplicate(self):
        out = expand("police station")
        assert len(out) == len(set(out))
