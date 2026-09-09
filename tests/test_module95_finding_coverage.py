"""
[Gold-QA fix — Module 95] G2 and G5 name findings their gold answers do not.

Module 87's re-judge of Module 27's FROZEN answers (the system held
constant) dropped G2 from 0.5/0.5/0.5 to 0.3/0.3/0.3 and G5 from
0.5/0.6/0.6 to 0.4/0.4/0.4, both for COVERAGE: the answers were fluent,
true and about different findings than gold's. This file pins the six gold
findings — the four that were not computable before this module, and the
two data figures gold asserts — so a regression shows up as a red test
rather than as a judge score six modules later.

Every test here fails on the merge-base:
  - the `_missing_custody_controls` / `_departure_chronology_conflicts` /
    `_zimni_typing_coverage` tests fail with AttributeError (the functions
    did not exist);
  - the renderer tests fail on the missing lines;
  - the projection tests fail because the properties were never written;
  - `test_gold_figures_reproduce_from_the_api_snapshot` PASSES on the
    merge-base, deliberately — it is a gold-verification test, not a
    change-detector, and it is the evidence that no gold correction is due.
"""
import json
from pathlib import Path

from src.pipeline import xagg
from scripts.backfill_incident_completeness_fields import plan_updates

SNAPSHOT = Path(__file__).resolve().parent / "fixtures" / "muhafiz_api_snapshot.json"


def _fir_rows() -> list[dict]:
    payload = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    fir = payload["endpoints"]["fir"]
    return fir["data"] if isinstance(fir, dict) and "data" in fir else fir


class _Rows:
    """age_client stub returning a canned row list per query substring."""

    def __init__(self, by_substring: dict[str, list[dict]]):
        self._by = by_substring

    async def execute_cypher(self, q, params=None, columns=("result",), graph=None):
        for needle, rows in self._by.items():
            if needle in q:
                return rows
        return []


class _Gateway:
    def __init__(self, cases):
        self._cases = cases

    async def get_cases(self, user_id=None, user_role=None):
        return list(self._cases)


# ── Phase 1 evidence: gold's own figures, measured ───────────────────────

def test_gold_figures_reproduce_from_the_api_snapshot():
    """Five gold answers have been CORRECTED on this programme (G1, G6, KB9,
    CP6, KB1) because gold asserted more than the data supported. G2's
    "9 FIRs with no incident date", "most zimni entries no type" and
    "13 of 73" and G5's "30 of 32 (94%)" were all unverified. They are
    verified here, against the recorded API snapshot, and every one holds —
    so Module 95 is a code fix, not a sixth gold correction."""
    from datetime import datetime

    firs = _fir_rows()
    assert len(firs) == 73

    # G2 (1a): 9 FIRs record no incident date.
    assert sum(1 for f in firs if not f.get("incident_datetime")) == 9

    # G2 (1b): "most" zimni entries carry no type — 188 of 259 (73%).
    zimni = [z for f in firs for z in (f.get("fir_zimni") or [])]
    assert len(zimni) == 259
    assert sum(1 for z in zimni if not z.get("entry_type")) == 188

    # G2 (2): 13 of 73 record departure EARLIER than the report time.
    def _p(v):
        return datetime.fromisoformat(v.replace("Z", "+00:00"))

    pairs = [
        f for f in firs
        if f.get("station_departure_datetime") and f.get("report_datetime")
    ]
    assert len(pairs) == 44
    conflicts = [
        f for f in pairs
        if _p(f["station_departure_datetime"]) < _p(f["report_datetime"])
    ]
    assert len(conflicts) == 13

    # G2 (3): the CMS<->FIR join is an OPTIONAL tag — 5 of 73 FIRs carry one.
    assert sum(1 for f in firs if f.get("e_tag_number")) == 5

    # G5 (1): 30 of 32 recovered weapons (94%) recorded without a licence.
    weapons = [w for f in firs for w in (f.get("weapon_register") or [])]
    assert len(weapons) == 32
    assert sum(1 for w in weapons if w.get("license_status") == "بغیر لائسنس") == 30


def test_weapon_register_inventory_matches_the_live_api_shape():
    """`_WEAPON_REGISTER_FIELDS` is the authority G5's finding (2) is
    computed from, so it must not drift from the register it claims to
    describe. If the API ever adds a column, this fails and the finding gets
    re-derived rather than silently going stale."""
    weapons = [w for f in _fir_rows() for w in (f.get("weapon_register") or [])]
    observed = {k for w in weapons for k in w}
    assert observed == set(xagg._WEAPON_REGISTER_FIELDS)


# ── G5 finding (2): a statement about the register's SHAPE ───────────────

def test_missing_custody_controls_names_all_three_gold_gaps():
    missing = xagg._missing_custody_controls()
    assert missing == ["packaging or sealing", "photographs", "chain of custody / handover"]


def test_missing_custody_controls_retires_a_finding_when_a_column_appears():
    """Not a canned list: adding the column upstream removes the claim."""
    fields = xagg._WEAPON_REGISTER_FIELDS + ("photograph_ref", "chain_of_custody_log", "packaging_seal_no")
    assert xagg._missing_custody_controls(fields) == []


# ── G2 finding (2): the departure/report chronology contradiction ────────

async def test_departure_chronology_flags_only_a_true_contradiction():
    rows = [
        # departure BEFORE the report exists — the contradiction.
        {"case_id": "fir-118-26", "departure": "2026-02-12T17:45:00Z",
         "report": "2026-02-12T18:30:00Z"},
        # departure after the report — ordinary and correct.
        {"case_id": "fir-1001-26", "departure": "2024-09-25T17:35:00Z",
         "report": "2024-09-25T17:25:00Z"},
        # identical stamps: sloppy, not contradictory — NOT counted.
        {"case_id": "fir-9-26", "departure": "2026-01-01T10:00:00Z",
         "report": "2026-01-01T10:00:00Z"},
        # unparseable — excluded from both numerator and denominator.
        {"case_id": "fir-8-26", "departure": "not-a-timestamp",
         "report": "2026-01-01T10:00:00Z"},
    ]
    xagg.age_client = _Rows({"station_departure_datetime": rows})
    try:
        conflicts, recorded = await xagg._departure_chronology_conflicts()
    finally:
        from src.graph import age_client as real
        xagg.age_client = real
    assert conflicts == ["fir-118-26"]
    assert recorded == 3


async def test_zimni_typing_coverage_sums_the_projected_counts():
    rows = [{"total": 5, "typed": 0}, {"total": 3, "typed": 3}, {"total": 4, "typed": 1}]
    xagg.age_client = _Rows({"zimni_entry_count": rows})
    try:
        total, typed = await xagg._zimni_typing_coverage()
    finally:
        from src.graph import age_client as real
        xagg.age_client = real
    assert (total, typed) == (12, 4)


# ── The rendered briefings ───────────────────────────────────────────────

async def test_case_completeness_scan_reports_all_three_g2_findings(monkeypatch):
    cases = [
        {"case_id": f"fir-{i}-26", "incident_date": None if i < 9 else "2026-01-01",
         "investigation_status": "open"}
        for i in range(73)
    ]
    monkeypatch.setattr(xagg, "age_client", _Rows({
        "zimni_entry_count": [{"total": 259, "typed": 71}],
        "station_departure_datetime": [
            {"case_id": f"fir-{i}-26", "departure": "2026-02-12T17:45:00Z",
             "report": "2026-02-12T18:30:00Z"}
            for i in range(13)
        ] + [
            {"case_id": f"fir-{i}-26", "departure": "2026-02-12T19:45:00Z",
             "report": "2026-02-12T18:30:00Z"}
            for i in range(13, 44)
        ],
        "record_type = 'cms_complaint' RETURN count": [{"n": 4}],
        "RETURN r.case_tag_number": [
            {"tag": f"CMS-{i}", "case_id": f"fir-{i}-26", "cnic": None} for i in range(4)
        ],
    }))
    r = await xagg._case_completeness_scan(_Gateway(cases))
    assert r["zimni_entry_total"] == 259 and r["zimni_entry_typed"] == 71
    assert len(r["departure_before_report"]) == 13
    assert r["departure_recorded_count"] == 44
    assert r["complaint_total"] == 4 and r["complaint_unlinked"] == 0

    text = "\n".join(xagg.render_case_completeness_scan(r))
    # gold (1): both halves
    assert "9 of 73 FIRs record no incident date" in text
    assert "188 of 259" in text and "entry TYPE" in text
    # gold (2): the chronology contradiction
    assert "13 of 73" in text and "EARLIER than the report time" in text
    # gold (3): the two disconnected systems
    assert "SEPARATE systems" in text and "OPTIONAL shared tag" in text


async def test_case_completeness_scan_degrades_to_pre_module95_output(monkeypatch):
    """A graph projected before Module 95's backfill must not assert a zero
    ratio — it must simply not make the claim."""
    monkeypatch.setattr(xagg, "age_client", _Rows({}))
    cases = [{"case_id": "fir-1-26", "incident_date": None, "investigation_status": "open"}]
    r = await xagg._case_completeness_scan(_Gateway(cases))
    text = "\n".join(xagg.render_case_completeness_scan(r))
    assert "entry TYPE" not in text
    assert "EARLIER than the report time" not in text
    assert "SEPARATE systems" not in text
    assert "1 of 1 FIRs record no incident date" in text


async def test_weapon_compliance_scan_reports_all_three_g5_findings(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", _Rows({
        "RETURN w.license_status": [{"status": "بغیر لائسنس"}] * 30 + [{"status": None}] * 2,
        "count(DISTINCT w)": [{"n": 32}],
    }))
    r = await xagg._weapon_compliance_scan()
    assert r["unlicensed_count"] == 30
    assert r["orphaned_weapon_count"] == 0
    assert len(r["missing_custody_controls"]) == 3

    text = "\n".join(xagg.render_weapon_compliance_scan(r))
    assert "30 of 32" in text and "94%" in text          # gold (1)
    assert "chain of custody / handover" in text          # gold (2)
    assert "SOFT match on the FIR code" in text           # gold (3)


async def test_weapon_compliance_scan_reports_a_real_orphan_when_one_exists(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", _Rows({
        "RETURN w.license_status": [{"status": "بغیر لائسنس"}] * 32,
        "count(DISTINCT w)": [{"n": 30}],
    }))
    r = await xagg._weapon_compliance_scan()
    assert r["orphaned_weapon_count"] == 2
    text = "\n".join(xagg.render_weapon_compliance_scan(r))
    assert "2 of 32 already resolve to no case" in text


# ── The backfill's own arithmetic, against the real snapshot ─────────────

def test_backfill_plan_reproduces_the_gold_figures():
    planned = plan_updates(_fir_rows())
    assert len(planned) == 73
    assert sum(1 for u in planned if u["departure_dt"]) == 44
    assert sum(u["zimni_total"] for u in planned) == 259
    assert sum(u["zimni_typed"] for u in planned) == 71


# ── The dispatch equality control ────────────────────────────────────────

GOLD = Path(__file__).resolve().parent.parent / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"

# Recorded on the merge-base (origin/main @ 730b4dc) BEFORE any Module 95
# change, by scripts/module95_aggregate_baseline.py. Module 95 adds NO
# aggregate kind and touches no line of `resolve_aggregate_kind()`, so every
# one of the 32 must still resolve exactly here.
_BASELINE_KINDS = {
    "D1": "total_count", "S2": "station_or_category_counts",
    "S3": "graph_recurrence_person", "A1": "gender_breakdown",
    "A7": "reporting_delay_count", "CP6": "placeholder_officer_count",
    "CR2": "graph_recurrence_person", "CR3": "station_or_category_counts",
    "CR4": "weapon_evidence_chain", "CR6": "cms_fir_linkage",
    "CR7": "criminal_record_court_crosscheck", "CR8": "dv_report_fir_match",
    "CS4": "criminal_record_local_match_gap",
    "CP1": "weapon_recovery_rate_by_district", "M1": "statute_mix_by_year",
    "M2": "station_caseload_by_specialisation", "M4": "statute_court_stage_join",
    "M5": "weapon_statute_cooccurrence_by_year",
    "M7": "incident_to_report_minutes_by_year", "G1": "case_completeness_scan",
    "G2": "case_completeness_scan", "G3": "court_readiness_scan",
    "G5": "weapon_compliance_scan", "G6": "station_or_category_counts",
    "KB1": "station_or_category_counts", "KB2": "graph_recurrence_person",
    "KB3": "officer_role_pair_overlap", "KB4": "station_or_category_counts",
    "KB5": "gender_breakdown", "KB6": "graph_recurrence_weapon",
    "KB8": "station_or_category_counts", "KB9": "graph_recurrence_person",
}


def test_all_32_dispatch_unchanged():
    questions = json.loads(GOLD.read_text(encoding="utf-8"))
    actual = {q["id"]: xagg.resolve_aggregate_kind(q["question"]) for q in questions}
    assert actual == _BASELINE_KINDS
