# -*- coding: utf-8 -*-
"""
[Gold-QA fix — Module 151] The record inventory and the schema-absence
claim grounding — checked in both directions, and pinned to the sources
that make it an authority rather than a guess.

No network, no graph: `declared_inventory()` only, plus a hand-built
"graph added a field" inventory where the union matters.
"""
from __future__ import annotations

import io
import json
from pathlib import Path

import pytest

from src.pipeline import schema_inventory as si
from src.pipeline import schema_claims as sc

SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "muhafiz_api_snapshot.json"


def _snapshot():
    return json.load(io.open(SNAPSHOT, encoding="utf-8"))["endpoints"]


def _observed_row_keys(rows: list[dict]) -> set[str]:
    """Scalar and nested-object keys of a row family (child ARRAYS are their
    own families and are excluded here)."""
    keys: set[str] = set()
    for r in rows:
        for k, v in r.items():
            if isinstance(v, list):
                continue
            keys.add(k)
    return keys


# ── 1. The declared inventory is pinned to the recorded API snapshot ────────

def test_fir_row_shape_matches_the_api_snapshot():
    obs = _observed_row_keys(_snapshot()["fir"])
    assert obs == set(si.DECLARED_RECORD_FAMILIES["fir"])


@pytest.mark.parametrize("family", [
    "fir_section", "fir_accused", "fir_witness", "fir_investigating_officer",
    "fir_position", "fir_zimni", "fir_zimni_index", "chalaan_dispatch",
    "chalaan_outcome", "malkhana_register", "weapon_register",
])
def test_fir_child_table_shape_matches_the_api_snapshot(family):
    rows = [row for fir in _snapshot()["fir"] for row in (fir.get(family) or [])]
    assert rows, f"snapshot has no {family} rows"
    assert _observed_row_keys(rows) == set(si.DECLARED_RECORD_FAMILIES[family])


def test_roznamcha_cms_pkm_and_criminal_record_shapes_match_the_snapshot():
    ep = _snapshot()
    assert _observed_row_keys(ep["roznamcha"]) == set(si.DECLARED_RECORD_FAMILIES["roznamcha"])
    assert _observed_row_keys(ep["cms"]) == set(si.DECLARED_RECORD_FAMILIES["cms_complaint"])
    assert _observed_row_keys(ep["pkm"]) == set(si.DECLARED_RECORD_FAMILIES["pkm_application"])
    assert _observed_row_keys(ep["criminal-records"]) == set(si.DECLARED_RECORD_FAMILIES["criminal_record"])
    wvr = [r["women_violence_report"] for r in ep["pkm"] if r.get("women_violence_report")]
    assert wvr
    assert _observed_row_keys(wvr) == set(si.DECLARED_RECORD_FAMILIES["women_violence_report"])
    persons = [r["complainant"] for r in ep["cms"] if r.get("complainant")]
    persons += [r["applicant"] for r in ep["pkm"] if r.get("applicant")]
    assert persons
    assert _observed_row_keys(persons) <= set(si.DECLARED_RECORD_FAMILIES["person"])


def test_witness_rows_carry_no_statement_field_in_the_snapshot_either():
    """KB2's gold, checked against the data rather than asserted: not one
    witness row the API ever returned has a field whose name speaks of a
    statement, confession, interview or testimony."""
    rows = [row for fir in _snapshot()["fir"] for row in (fir.get("fir_witness") or [])]
    keys = _observed_row_keys(rows)
    for kw in ("statement", "confession", "interview", "testimony", "deposition"):
        assert not any(kw in k for k in keys), (kw, keys)


# ── 2. Module 95's constant is reused, not re-derived ─────────────────────

def test_weapon_register_family_equals_module95_constant():
    from src.pipeline import xagg
    assert set(si.DECLARED_RECORD_FAMILIES["weapon_register"]) == set(xagg._WEAPON_REGISTER_FIELDS)


def test_fields_serving_generalises_missing_custody_controls():
    """For every custody control Module 95 declares, the generalised scan
    over the weapon register agrees with `_missing_custody_controls()`:
    empty scan <=> control reported missing."""
    from src.pipeline import xagg
    inv = si.declared_inventory()
    missing = set(xagg._missing_custody_controls())
    for label, tokens in xagg._CUSTODY_CONTROLS:
        hits = si.fields_serving(tokens, "weapon_register", inv)
        assert hits is not None
        assert (hits == []) == (label in missing), (label, hits)


# ── 3. Matching — confirming direction ─────────────────────────────────────

def test_a_confirmed_absence_scans_empty():
    inv = si.declared_inventory()
    assert si.fields_serving(["statement", "confession", "interview"], "fir_witness", inv) == []
    assert si.fields_serving(["statement", "confession", "interview"], "any", inv) == []
    assert si.fields_serving(["inquest", "postmortem", "autopsy", "cause_of_death"], "any", inv) == []
    assert si.fields_serving(["custody", "handover", "chain"], "women_violence_report", inv) == []


# ── 4. Matching — refuting direction (the side that must never weaken) ────

def test_an_existing_field_refutes_the_absence():
    inv = si.declared_inventory()
    assert si.fields_serving(["age"], "fir_accused", inv) == ["fir_accused.age"]
    hits = si.fields_serving(["custody"], "any", inv)
    assert "chalaan_dispatch.custody_classification" in hits
    assert "fir_accused.custody_position" in hits


def test_a_family_name_refutes_a_claim_that_no_such_register_exists():
    inv = si.declared_inventory()
    hits = si.fields_serving(["weapon"], "any", inv)
    assert "weapon_register" in hits


def test_a_field_the_graph_added_refutes_too():
    """The graph can only ADD fields: a property that exists only on the
    projected node still counts as a field our records have."""
    inv = si.declared_inventory()
    inv.families["fir"].add("zimni_entry_count")
    assert si.fields_serving(["zimni"], "fir", inv) == ["fir.zimni_entry_count"]


def test_matching_is_separator_and_case_insensitive():
    inv = si.declared_inventory()
    assert si.fields_serving(["Inquest", "Cause-Of-Death"], "any", inv) == []
    assert si.fields_serving(["Licence", "LICENSE-STATUS"], "weapon_register", inv) == ["weapon_register.license_status"]


# ── 5. Degraded paths can only fail to confirm ─────────────────────────────

def test_unknown_scope_cannot_confirm():
    inv = si.declared_inventory()
    assert si.fields_serving(["statement"], "no_such_family", inv) is None


def test_stoplisted_or_empty_keywords_cannot_confirm():
    inv = si.declared_inventory()
    assert si.fields_serving(["text", "field", "record"], "any", inv) is None
    assert si.fields_serving([], "any", inv) is None
    # Urdu keywords normalise to nothing usable — unconfirmable, not confirmed.
    assert si.fields_serving(["خانہ", "بیان"], "fir_witness", inv) is None


def test_usable_keywords_keeps_short_but_meaningful_stems():
    assert si.usable_keywords(["age", "dob", "id", "no"]) == ["age", "dob"]


def test_scope_fields_any_covers_every_family():
    inv = si.declared_inventory()
    assert inv.scope_fields("any") == inv.families
    assert inv.scope_fields(None) == inv.families
    assert inv.scope_fields("fir_witness") == {"fir_witness": inv.families["fir_witness"]}


def test_render_lists_every_family_for_the_classifier():
    text = si.declared_inventory().render()
    for fam in si.DECLARED_RECORD_FAMILIES:
        assert f"- {fam}" in text


# ── 6. The classifier's parse and grounding matrix (no LLM) ────────────────

_ANSWER = (
    "Our witness records hold identity and contact details only [Document 1]; "
    "there is no field for what the witness said. Our accused records hold no "
    "field for the accused's age [Document 2]. Nowhere in our system is custody "
    "recorded in any field [Document 2]."
)


def _claim(kind, scope=None, keywords=(), matching=(), claim="c", sentence=""):
    return sc.ClassifiedClaim(
        claim=claim, kind=kind, scope=scope, concept="x",
        field_keywords=list(keywords), matching_fields=list(matching),
        answer_sentence=sentence,
    )


_WITNESS_CLAIM = "The claim that witness records have no field for what the witness said is unsupported."
_WITNESS_SENTENCE = "there is no field for what the witness said."
_AGE_CLAIM = "The claim that accused records hold no field for the accused's age is unsupported."
_AGE_SENTENCE = "Our accused records hold no field for the accused's age [Document 2]."
_CUSTODY_CLAIM = "The claim that nowhere in the system is custody recorded in any field is unsupported."
_CUSTODY_SENTENCE = "Nowhere in our system is custody recorded in any field [Document 2]."


def test_ground_confirms_only_a_schema_absence_no_reader_refutes():
    inv = si.declared_inventory()
    c = _claim(sc.KIND_SCHEMA_ABSENCE, "fir_witness", ["statement", "confession"],
               claim=_WITNESS_CLAIM, sentence=_WITNESS_SENTENCE)
    sc.ground_against_inventory([c], inv, _ANSWER)
    assert c.confirmed_absent and c.verdict == "confirmed_absent"


@pytest.mark.parametrize("kind", [sc.KIND_DATA_ABSENCE, sc.KIND_SCHEMA_PRESENCE, sc.KIND_OTHER])
def test_ground_never_confirms_a_non_schema_kind(kind):
    inv = si.declared_inventory()
    c = _claim(kind, "any", ["statement"])
    sc.ground_against_inventory([c], inv)
    assert not c.confirmed_absent and c.verdict.startswith("not_schema")


def test_ground_refutes_when_the_classifier_names_an_existing_field():
    inv = si.declared_inventory()
    c = _claim(sc.KIND_SCHEMA_ABSENCE, "fir_accused", ["birthyear"], matching=["fir_accused.age"],
               claim=_AGE_CLAIM, sentence=_AGE_SENTENCE)
    sc.ground_against_inventory([c], inv, _ANSWER)
    assert not c.confirmed_absent and c.verdict == "refuted:classifier_named_existing_field"


def test_ground_refutes_when_the_keyword_scan_finds_a_field():
    inv = si.declared_inventory()
    c = _claim(sc.KIND_SCHEMA_ABSENCE, "any", ["custody"], claim=_CUSTODY_CLAIM, sentence=_CUSTODY_SENTENCE)
    sc.ground_against_inventory([c], inv, _ANSWER)
    assert not c.confirmed_absent and c.verdict == "refuted:field_exists"


# ── 8. The sentence guard — measured on the held-out set, cut 2 ───────────

def test_guard_passes_the_gold_shaped_sentences_in_three_scripts():
    en = "The system lacks a field to track compliance with this requirement."
    ru = "Magar schema mein kahin koi inquest, post-mortem ya cause-of-death record nahi."
    ur = "جو درج نہیں ہوتا وہ یہ کہ چین آف کسٹڈی برقرار رکھی گئی یا نہیں — اس کے لیے کوئی متعلقہ خانہ موجود نہیں۔"
    assert sc.sentence_guard("The system lacks a field to track compliance — not in any chunk.", en, "x " + en) is None
    assert sc.sentence_guard("Yeh daawa ke schema mein koi inquest record nahi, support nahi hota.", ru, ru) is None
    assert sc.sentence_guard("یہ دعویٰ کہ چین آف کسٹڈی کے لیے کوئی متعلقہ خانہ موجود نہیں، ثابت نہیں ہوتا۔", ur, ur) is None


def test_guard_stops_an_event_negative_even_when_no_field_exists():
    """N9/N10: 'the chain of custody was not maintained', 'none of the
    weapons was photographed' — events, not shape. No field for either
    exists, so only the guard stands between them and a false confirmation."""
    inv = si.declared_inventory()
    a = "In none of the 8 women-violence cases was the chain of custody maintained [Document 5]."
    b = "None of the 32 recovered weapons was photographed [Document 1]."
    for claim, sent in (
        ("The claim that the chain of custody was not maintained in any of the 8 cases is unsupported.", a),
        ("The claim that none of the recovered weapons was photographed is unsupported.", b),
    ):
        c = _claim(sc.KIND_SCHEMA_ABSENCE, "any", ["custody", "photograph"], claim=claim, sentence=sent)
        sc.ground_against_inventory([c], inv, sent)
        assert not c.confirmed_absent and c.verdict.startswith("unconfirmable:")


def test_guard_stops_a_data_negative_whose_verb_is_records():
    """N11: 'no zimni entry records an arrest' — "records" as a verb is the
    data-negative shape and is not schema vocabulary."""
    inv = si.declared_inventory()
    sent = "No zimni entry in any case records an arrest [Document 3]."
    c = _claim(sc.KIND_SCHEMA_ABSENCE, "fir_zimni", ["arrest"],
               claim="The claim that no zimni entry records an arrest is unsupported.", sentence=sent)
    sc.ground_against_inventory([c], inv, sent)
    assert not c.confirmed_absent and c.verdict.startswith("unconfirmable")


def test_guard_stops_a_positive_claim_the_classifier_mislabels():
    """S2/S3: 'our weapon register records packaging and photographs' was
    classified schema_absence live. No negation in the sentence — stopped."""
    inv = si.declared_inventory()
    sent = "Our weapon register records the packaging and photographs of each weapon [Document 1]."
    c = _claim(sc.KIND_SCHEMA_ABSENCE, "weapon_register", ["packaging", "photograph"],
               claim="The assertion that our weapon register records packaging and photographs is unsupported.",
               sentence=sent)
    sc.ground_against_inventory([c], inv, sent)
    assert not c.confirmed_absent and c.verdict in ("unconfirmable:claim_is_not_negative", "unconfirmable:no_negation_in_sentence")


def test_guard_stops_a_quote_that_is_not_in_the_answer():
    assert sc.sentence_guard(_WITNESS_CLAIM, "there is no field for anything at all here", _ANSWER) == "quote_not_in_answer"
    assert sc.sentence_guard(_WITNESS_CLAIM, "", _ANSWER) == "quote_not_in_answer"


def test_guard_tests_the_judges_own_claim_with_its_evidence_wrapper_stripped():
    """A positive flagged claim cannot be rescued by quoting a different,
    negative sentence of the same answer: the judge's claim itself, minus
    "…is unsupported", must be a negative statement about a field."""
    pos = "The assertion that our weapon register records packaging and photographs is unsupported."
    assert sc.claim_core(pos) == "The assertion that our weapon register records packaging and photographs"
    assert sc.sentence_guard(pos, _WITNESS_SENTENCE, _ANSWER) == "claim_is_not_negative"
    ev = "The claim that no post-mortem was conducted in any of the 10 murder cases is not supported by any chunk."
    assert sc.sentence_guard(ev, _WITNESS_SENTENCE, _ANSWER) == "claim_has_no_schema_vocabulary"
    # The wrapper's own negation must not count: "not supported", "no chunk".
    assert sc.claim_core("Case records do not explicitly track X — this is not stated in Document 6.") == "Case records do not explicitly track X"
    assert sc.claim_core("The system lacks a field to track compliance — no chunk discusses the structure of case records.") == "The system lacks a field to track compliance"
    assert sc.claim_core("Claims about missing data fields in case records are not supported by any cited chunk, which only addresses FIR linkage.") == "Claims about missing data fields in case records"


def test_guard_is_whitespace_and_markup_tolerant():
    sent = "**No field** in the case records explicitly tracks inquest reports."
    answer = "Intro.\n- **No field** in the case records   explicitly tracks inquest reports.\n"
    assert sc.sentence_guard("Case records do not explicitly track inquest reports — not stated.", sent, answer) is None


def test_ground_cannot_confirm_an_unknown_scope_or_generic_keywords():
    inv = si.declared_inventory()
    a = _claim(sc.KIND_SCHEMA_ABSENCE, "witness_table", ["statement"])
    b = _claim(sc.KIND_SCHEMA_ABSENCE, "any", ["text", "data"])
    sc.ground_against_inventory([a, b], inv)
    assert not a.confirmed_absent and a.verdict.startswith("unconfirmable")
    assert not b.confirmed_absent and b.verdict.startswith("unconfirmable")


def test_parse_requires_one_object_per_flagged_claim_with_a_known_kind():
    flagged = ["a", "b"]
    assert sc._parse({"claims": [{"kind": "other"}]}, flagged) is None
    assert sc._parse({"claims": [{"kind": "other"}, {"kind": "made_up"}]}, flagged) is None
    assert sc._parse("not a dict", flagged) is None
    ok = sc._parse({"claims": [
        {"kind": "schema_absence", "scope": "FIR_WITNESS", "field_keywords": ["statement"], "matching_fields": []},
        {"kind": "other"},
    ]}, flagged)
    assert ok is not None and ok[0].scope == "fir_witness" and ok[1].kind == "other"
    assert ok[0].claim == "a" and ok[1].claim == "b"


# ── 7. [Measured live, §6] compound-only keyword lists cannot confirm ──────

def test_compound_only_keywords_cannot_confirm():
    """KB5-P1 run 2 (verifier arm, first cut): the classifier returned
    'investigative_team', 'expedited_time', 'magistrate_referral' — invented
    compounds that match no column by construction, so the scan 'confirmed'
    four vague absences for free. At least one usable SINGLE-word stem is
    now required; otherwise the claim is unconfirmable."""
    inv = si.declared_inventory()
    assert si.has_single_stem_keyword(["investigative_team", "expedited_time"]) is False
    assert si.fields_serving(["investigative_team", "magistrate_referral"], "any", inv) is None
    c = sc.ClassifiedClaim(claim="c", kind=sc.KIND_SCHEMA_ABSENCE, scope="any", concept="x",
                           field_keywords=["investigative_team", "expedited_time"])
    sc.ground_against_inventory([c], inv)
    assert not c.confirmed_absent and c.verdict.startswith("unconfirmable")


def test_a_single_stem_beside_compounds_is_enough():
    inv = si.declared_inventory()
    assert si.has_single_stem_keyword(["inquest", "cause_of_death"]) is True
    assert si.fields_serving(["inquest", "cause_of_death"], "any", inv) == []
    # ...and the compound is still matched as a whole when it does occur.
    assert si.fields_serving(["licen", "license_status"], "weapon_register", inv) == ["weapon_register.license_status"]


def test_a_single_stem_that_is_generic_does_not_count():
    assert si.has_single_stem_keyword(["date", "investigative_team"]) is False
