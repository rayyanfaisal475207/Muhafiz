"""The structured source contract must not repeat the extraction's mistakes.

Each test below pins a defect that was measured in the live data, so a
future change that reintroduces it fails here rather than in an answer.
No database and no model: these are properties of the contract itself.
"""

from __future__ import annotations

import json
from datetime import timezone

import pytest

from src.evidence.structured import (
    CRIMINAL_FIELDS, FIR_FIELDS, WEAPON_FIELDS, canonical, is_absent,
    locate_value, parse_time, pointer_value,
)


def field(contracts, label):
    for contract in contracts:
        if contract.label == label:
            return contract
    raise AssertionError(f"no contract labelled {label!r}")


# ── The bore/quantity corruption ────────────────────────────────────────
#
# The extracted legacy table holds weapons.quantity = 30 where 30 is a
# pistol's BORE, giving SUM(quantity) = 972 over 96 rows. The source keeps
# `caliber_or_bore` and `quantity` in separate fields and its quantity is
# free text such as "1 عدد + 6 گولیاں".

def test_quantity_is_text_and_can_never_be_summed():
    quantity = field(WEAPON_FIELDS, "quantity as recorded")
    assert quantity.value_type == "text"
    assert quantity.unit is None


def test_bore_is_a_separate_text_concept_from_quantity():
    bore = field(WEAPON_FIELDS, "calibre or bore")
    quantity = field(WEAPON_FIELDS, "quantity as recorded")
    assert bore.pointer != quantity.pointer
    assert bore.value_type == "text"


def test_weapon_grain_describes_a_register_entry_not_a_weapon():
    """A child-row count is not a count of physical objects."""
    detail = field(WEAPON_FIELDS, "weapon register item detail")
    assert "ENTRY" in detail.grain or "entry" in detail.grain
    assert "ammunition" in detail.grain


# ── Registration versus reporting ───────────────────────────────────────
#
# rebuild_cases.registered_at is 0/73 and the snapshot carries no
# registration field at all. report_datetime is a different fact.

def test_reported_at_states_it_is_not_registration():
    reported = field(FIR_FIELDS, "reported at")
    assert "NOT the registration time" in reported.meaning


def test_no_contract_claims_to_supply_a_registration_time():
    for contract in FIR_FIELDS:
        assert "registered at" != contract.label
        assert "registration time" not in contract.label


# ── Time interpretation ─────────────────────────────────────────────────

def test_offset_bearing_timestamp_is_kept_as_an_instant():
    parsed, how = parse_time("2024-09-25T17:25:00Z")
    assert how == "instant"
    assert parsed.tzinfo is not None
    assert parsed.astimezone(timezone.utc).hour == 17


def test_date_only_is_kept_but_marked_as_having_no_time():
    parsed, how = parse_time("2024-09-25")
    assert how == "date_only"
    assert parsed.year, parsed.month == (2024, 9)


def test_naive_datetime_is_refused_rather_than_assumed_utc():
    """Assuming a timezone moves an event across a day boundary, which is
    exactly what a monthly count would then get wrong."""
    parsed, how = parse_time("2024-09-25T17:25:00")
    assert parsed is None
    assert how == "unusable"


# ── Stringified nulls ───────────────────────────────────────────────────
#
# The export wrote Python None as the four characters "None".

@pytest.mark.parametrize("value", ["None", "null", "NULL", "N/A", "-", "", None, [], {}])
def test_stringified_nulls_count_as_absent(value):
    assert is_absent(value)


@pytest.mark.parametrize("value", ["0", "none of the above", "1 عدد", 0])
def test_real_values_are_not_treated_as_absent(value):
    assert not is_absent(value)


# ── Free text that looks like a link ────────────────────────────────────

def test_source_case_reference_stays_free_text():
    """'FIR 891/24, PS Jhang Road Faisalabad' is written by a person, not a
    foreign key. Resolving it would be a guess presented as a link."""
    ref = field(CRIMINAL_FIELDS, "source case reference (as written)")
    assert ref.value_type == "text"
    assert "not a resolved link" in ref.meaning


# ── Provenance ──────────────────────────────────────────────────────────

def test_span_points_at_the_field_not_a_coincidental_match():
    """A value repeated elsewhere in the record must still be cited from
    its own field."""
    record = {"fir_display_code": "1001/26",
              "narrative_text": "some text mentioning 1001/26 again",
              "police_station": {"id": "PS-ISB-CYBER"}}
    text = canonical(record)
    extent = locate_value(text, "/fir_display_code", "1001/26")
    assert extent is not None
    start, end = extent
    assert text[start:end] == json.dumps("1001/26", ensure_ascii=False)
    # the cited characters must sit next to their own key
    assert "fir_display_code" in text[max(0, start - 60):start]


def test_serialization_is_stable_so_ids_are_stable():
    a = canonical({"b": 1, "a": {"y": 2, "x": 3}})
    b = canonical({"a": {"x": 3, "y": 2}, "b": 1})
    assert a == b


def test_pointer_resolves_nested_values_and_tolerates_absence():
    record = {"police_station": {"district": {"id": "DIST-06"}}}
    assert pointer_value(record, "/police_station/district/id") == "DIST-06"
    assert pointer_value(record, "/police_station/missing/id") is None


# ── Step D: extraction repair must not invent meaning ───────────────────

from src.evidence.ingestion import prune_unreferenced


def _catalog_fact(predicate, **fact):
    """One declared bore concept, one declared quantity concept, one fact."""
    raw = {
        "concepts": [
            {"key": "bore_size", "label": "Bore Size", "kind": "property",
             "meaning": "calibre of the barrel", "value_type": "number",
             "unit": "caliber", "grain": "per weapon", "aliases": []},
            {"key": "item_quantity", "label": "Item Quantity", "kind": "property",
             "meaning": "how many items", "value_type": "number",
             "unit": "unit", "grain": "per entry", "aliases": []},
            {"key": "firearm", "label": "Firearm", "kind": "entity_type",
             "meaning": "a gun", "value_type": "entity", "grain": "one",
             "aliases": []},
        ],
        "entities": [{"key": "w1", "concept": "firearm", "label": "pistol",
                      "quote": "30 bore pistol", "start": 0}],
        "facts": [{"key": "f1", "subject": "w1", "predicate": predicate,
                   "quote": "30 bore pistol", "start": 0,
                   "value_quote": "30", "role_quote": "bore", **fact}],
    }
    return prune_unreferenced(raw)


def test_a_bore_never_becomes_a_quantity_by_unit_matching():
    """The live failure: the model writes a generic predicate and the
    repair used to pick the single concept sharing its type and unit.
    Two measurements can share a unit; choosing one converts meaning."""
    pruned, dropped = _catalog_fact("has_property", value_number=30, unit="unit")
    assert pruned["facts"] == []
    assert any("undeclared predicate" in d for d in dropped)


def test_a_predicate_named_by_its_own_alias_is_still_accepted():
    """Repairing a REFERENCE is allowed; reinterpreting a MEANING is not."""
    pruned, dropped = _catalog_fact("has_bore_size", value_number=30,
                                    unit="caliber")
    assert [f["predicate"] for f in pruned["facts"]] == ["bore_size"]
    assert dropped == []


def test_a_missing_unit_is_not_filled_in_from_the_concept():
    """An omitted unit is a missing statement, not an implied one."""
    pruned, dropped = _catalog_fact("bore_size", value_number=30)
    assert pruned["facts"] == []
    assert any("no unit stated" in d for d in dropped)


def test_a_disagreeing_unit_is_refused():
    pruned, dropped = _catalog_fact("bore_size", value_number=30, unit="unit")
    assert pruned["facts"] == []
    assert any("unit" in d for d in dropped)


def test_facts_pruned_before_review_are_reported_not_swallowed():
    """They must reach the caller, because coverage completeness depends
    on knowing that something was removed."""
    _, dropped = _catalog_fact("has_property", value_number=30, unit="unit")
    assert dropped, "a pruned fact must be reported to the caller"


def test_a_parsed_instant_keeps_the_offset_it_was_given():
    """MEASURED: FIR 1001/26's report_datetime is "2024-09-25T17:25:00Z",
    and the store returned 12:25Z -- five hours early.

    put_assertion wrote `parsed.replace(tzinfo=None)`, so the instant
    reached Postgres naive and was read in the DATABASE SESSION timezone.
    With the session on Asia/Karachi, 17:25 became 12:25Z. The same FIR
    then carried two incident timestamps five hours apart, because the
    other ingestion path kept its offset.

    A stored instant must not depend on a session setting.
    """
    parsed, how = parse_time("2024-09-25T17:25:00Z")
    assert how == "instant"
    assert parsed.tzinfo is not None, "an instant must never be stored naive"
    assert parsed.utcoffset() == timezone.utc.utcoffset(None)
    assert parsed.astimezone(timezone.utc).hour == 17


def test_a_bare_calendar_date_is_anchored_explicitly():
    """MEASURED: "2024-09-25" was stored as 2024-09-24 19:00Z -- midnight
    Karachi -- which moves the fact to the PREVIOUS DAY when read back as
    a date. A bare date on a Pakistani record is a local date, so the
    anchoring is decided here and recorded, not left to the session."""
    parsed, how = parse_time("2024-09-25")
    assert how == "date_only"
    assert parsed.tzinfo is not None
    assert (parsed.year, parsed.month, parsed.day) == (2024, 9, 25)
    assert parsed.hour == 0


def test_a_naive_datetime_is_still_refused():
    """The fix must not turn the refusal into a guess."""
    assert parse_time("2024-09-25T17:25:00") == (None, "unusable")


def test_a_figure_from_the_question_is_not_treated_as_invented():
    """MEASURED: "What weapon was recorded in FIR 1001/26?" was answered
    "30 بور پستول بمعہ 6 گولیاں" with four quotes, every one an exact
    substring of its source span -- and discarded, because naming the FIR
    the user asked about put "1001/26" in the text but not in the quotes.

    The grounding gate must reject figures the model produced from
    nowhere, not figures the questioner supplied.
    """
    from src.evidence.composition import numbers

    question = "What weapon was recorded in FIR 1001/26?"
    text = "The weapon recorded in FIR 1001/26 is 30 bore with 6 bullets."
    quotes = '"item_detail": "30 بور پستول بمعہ 6 گولیاں"'

    supported = numbers(quotes) | numbers(question)
    assert not numbers(text) <= numbers(quotes), "this is the defect being pinned"
    assert numbers(text) <= supported

    # The gate must still catch a figure that came from neither.
    assert not numbers("There were 47 such cases.") <= supported
