"""
scripts/build_real_entity_roster.py — M11 of the Muhafiz Data API
migration (docs/decisions/0001-muhafiz-api-migration.md).

data/memory/entity_roster.csv's confusable-pairs/name-variants were
hand-designed for the synthetic corpus. This proves the real-data
replacement derives the same shape of ground truth from actual CNIC
collisions/matches, not invented ones.
"""
import csv
import json
from pathlib import Path

import pytest

import scripts.build_real_entity_roster as builder
from src.data_gateway.muhafiz_api.models import FirRecord

FIXTURE = Path(__file__).parent / "fixtures" / "muhafiz_api_snapshot.json"


@pytest.fixture(scope="module")
def firs():
    snapshot = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return [FirRecord(r) for r in snapshot["endpoints"]["fir"]]


# ── _person_mentions ─────────────────────────────────────────────────────

def test_person_mentions_only_includes_cnic_bearing_people():
    fir = FirRecord({
        "fir_id": "fir-1-26",
        "complainant_full_name": "احمد", "complainant_cnic": "00000-1-1",
        "fir_accused": [
            {"full_name": "With CNIC", "cnic": "00000-2-1"},
            {"full_name": "No CNIC"},  # excluded
        ],
        "fir_witness": [{"full_name": "Witness", "cnic": "00000-3-1"}],
    })
    mentions = builder._person_mentions([fir])
    names = {m["full_name"] for m in mentions}
    assert names == {"احمد", "With CNIC", "Witness"}


def test_person_mentions_no_complainant_cnic_excludes_complainant():
    fir = FirRecord({"fir_id": "fir-1-26", "complainant_full_name": "احمد"})  # no CNIC
    mentions = builder._person_mentions([fir])
    assert mentions == []


# ── build_confusable_pairs ────────────────────────────────────────────────

def test_confusable_pair_requires_two_distinct_cnics_same_name():
    mentions = [
        {"full_name": "X", "cnic": "A", "father_name": None, "fir_id": "fir-1", "role": "accused"},
        {"full_name": "X", "cnic": "B", "father_name": None, "fir_id": "fir-2", "role": "accused"},
    ]
    pairs = builder.build_confusable_pairs(mentions)
    assert len(pairs) == 2
    assert {r["cnic_shown_in"] for r in pairs} == {"fir-1", "fir-2"}
    assert all(r["designed_as"] == "confusable-pair" for r in pairs)
    assert pairs[0]["pair_or_group_id"] == pairs[1]["pair_or_group_id"]


def test_same_name_same_cnic_is_not_a_confusable_pair():
    """Same person mentioned twice (e.g. accused + witness elsewhere) —
    not a name collision, must not be flagged as must-not-merge."""
    mentions = [
        {"full_name": "X", "cnic": "A", "father_name": None, "fir_id": "fir-1", "role": "accused"},
        {"full_name": "X", "cnic": "A", "father_name": None, "fir_id": "fir-2", "role": "witness"},
    ]
    assert builder.build_confusable_pairs(mentions) == []


def test_name_group_with_more_than_two_cnics_emits_one_representative_pair():
    """A 10-CNIC name group (measured live) must not explode into 45 pairs."""
    mentions = [
        {"full_name": "X", "cnic": str(i), "father_name": None, "fir_id": f"fir-{i}", "role": "accused"}
        for i in range(5)
    ]
    pairs = builder.build_confusable_pairs(mentions)
    assert len(pairs) == 2  # exactly one pair, not C(5,2)=10 pairs


def test_unique_cnic_per_name_produces_no_pairs():
    mentions = [
        {"full_name": "A", "cnic": "1", "father_name": None, "fir_id": "fir-1", "role": "accused"},
        {"full_name": "B", "cnic": "2", "father_name": None, "fir_id": "fir-1", "role": "witness"},
    ]
    assert builder.build_confusable_pairs(mentions) == []


# ── build_name_variant_rows ───────────────────────────────────────────────

def test_name_variant_requires_cnic_across_two_different_firs():
    mentions = [
        {"full_name": "X", "cnic": "A", "father_name": None, "fir_id": "fir-1", "role": "accused"},
        {"full_name": "X", "cnic": "A", "father_name": None, "fir_id": "fir-2", "role": "accused"},
    ]
    rows = builder.build_name_variant_rows(mentions)
    assert len(rows) == 1
    assert rows[0]["designed_as"] == "name-variant"
    assert rows[0]["case_ids"] == "fir-1;fir-2"


def test_same_cnic_same_fir_twice_is_not_a_cross_case_variant():
    mentions = [
        {"full_name": "X", "cnic": "A", "father_name": None, "fir_id": "fir-1", "role": "accused"},
        {"full_name": "X", "cnic": "A", "father_name": None, "fir_id": "fir-1", "role": "accused"},
    ]
    assert builder.build_name_variant_rows(mentions) == []


# ── against the real snapshot (locks in the measured counts) ────────────

def test_measured_counts_against_real_snapshot(firs):
    mentions = builder._person_mentions(firs)
    confusable = builder.build_confusable_pairs(mentions)
    variants = builder.build_name_variant_rows(mentions)

    assert len(confusable) // 2 == 41  # measured: 41 real name-collision groups
    assert len(variants) == 4  # measured: 4 real cross-FIR CNIC matches


def test_output_matches_the_existing_roster_schema(firs, tmp_path):
    """scripts/eval_entity_resolution.py's load_roster() must be able to
    read this file unchanged — same column set as data/memory/entity_roster.csv."""
    mentions = builder._person_mentions(firs)
    rows = builder.build_confusable_pairs(mentions) + builder.build_name_variant_rows(mentions)
    out = tmp_path / "real_entity_roster.csv"
    builder.write_roster(rows, out)

    with open(out, encoding="utf-8") as f:
        reader = csv.DictReader(f)
        assert tuple(reader.fieldnames) == builder.ROSTER_HEADER
        loaded = list(reader)
    assert len(loaded) == len(rows)
