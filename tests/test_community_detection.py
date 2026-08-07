"""
Tests for src/graph/community_detection.py's Person-node noise filter.

Priority 1 of the 2026-08-06 open-gaps audit: the XNETWORK community-noise
filter had been patched three separate times with new exact-phrase/suffix
blocklist entries, each round finding a new category of junk. This test
guards the fourth-round audit's structural fix — a document-rendering
artifact (a table/field boundary collapsing with no separating punctuation)
that appends an adjacent field's text directly onto a real extracted name,
live-confirmed against the real graph on 2026-08-06 (see the fix commit):
"Inspector Fariha Saeed Bhara" (1 occurrence) sitting alongside the
correctly-extracted "Inspector Fariha Saeed" (7 occurrences).
"""
from src.graph.community_detection import (
    _compute_prefix_contaminated_names,
    _is_plausible_person_name,
)


class TestStructuralArtifactChars:
    """A real extracted person name never contains rendering-boundary
    characters — newline, carriage return, a markdown table pipe, or a
    parenthesis."""

    def test_rejects_newline_bleed(self):
        # Live-observed: "Inspector Tariq Khan\n\nStatus" — the next
        # field's label bled across a blank line into the captured span.
        assert _is_plausible_person_name("Inspector Tariq Khan\n\nStatus") is False

    def test_rejects_another_newline_bleed(self):
        assert _is_plausible_person_name("Yusra Nawaz\n\nApplicant") is False

    def test_rejects_parenthetical(self):
        # Live-observed: "Inspector (Golra)" — the officer's real name was
        # dropped entirely and a station name substituted in parens.
        assert _is_plausible_person_name("Inspector (Golra)") is False

    def test_rejects_table_pipe(self):
        assert _is_plausible_person_name("Inspector Fariha | Saeed") is False

    def test_accepts_ordinary_two_word_name(self):
        assert _is_plausible_person_name("Irfan Mirza") is True

    def test_accepts_ordinary_role_plus_name(self):
        assert _is_plausible_person_name("Inspector Fariha Saeed") is True


class TestPrefixContamination:
    """A candidate name is a contaminated superstring of a real name, not a
    real longer name in its own right, when dropping its last token yields
    a shorter string that both exists in the corpus and occurs more often."""

    def test_flags_low_frequency_superstring_of_common_name(self):
        # "Inspector Fariha Saeed" appears 7x; the station-name-suffixed
        # variant appears once — the exact live-observed shape.
        person_names = {f"p{i}": "Inspector Fariha Saeed" for i in range(7)}
        person_names["p_bad"] = "Inspector Fariha Saeed Bhara"
        contaminated = _compute_prefix_contaminated_names(person_names)
        assert "Inspector Fariha Saeed Bhara" in contaminated
        assert "Inspector Fariha Saeed" not in contaminated

    def test_does_not_flag_when_no_shorter_prefix_exists(self):
        # No independently-common shorter prefix in the corpus — nothing to
        # compare against, so this must NOT be treated as contamination.
        person_names = {"p1": "Inspector Rare Standalone Name"}
        contaminated = _compute_prefix_contaminated_names(person_names)
        assert contaminated == set()

    def test_does_not_flag_when_longer_name_is_more_common(self):
        # A genuinely more common longer name isn't contamination even if
        # a rarer shorter prefix happens to coincide.
        person_names = {f"p{i}": "Irfan Mirza Junior" for i in range(5)}
        person_names["p_short"] = "Irfan Mirza"
        contaminated = _compute_prefix_contaminated_names(person_names)
        assert contaminated == set()

    def test_two_word_names_never_flagged(self):
        # The check requires 3+ tokens (a role/first/last minimum) —
        # ordinary two-word names have no "trailing fragment" to drop.
        person_names = {"p1": "Irfan Mirza", "p2": "Irfan"}
        contaminated = _compute_prefix_contaminated_names(person_names)
        assert contaminated == set()

    def test_multiple_contaminated_variants_across_population(self):
        # Mirrors the live audit: several different officers, each with
        # both a clean, frequent name and a rarer contaminated variant.
        person_names = {}
        for i in range(11):
            person_names[f"hamza{i}"] = "Inspector Hamza Latif"
        person_names["hamza_bad1"] = "Inspector Hamza Latif Kohsar"
        person_names["hamza_bad2"] = "Inspector Hamza Latif Kohsar"
        for i in range(8):
            person_names[f"rashid{i}"] = "Inspector Rashid Gondal"
        person_names["rashid_bad1"] = "Inspector Rashid Gondal Shahzad"
        person_names["rashid_bad2"] = "Inspector Rashid Gondal Shahzad"

        contaminated = _compute_prefix_contaminated_names(person_names)
        assert contaminated == {
            "Inspector Hamza Latif Kohsar",
            "Inspector Rashid Gondal Shahzad",
        }
