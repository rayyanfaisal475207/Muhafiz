"""
Tests for src/pipeline/xagg.py (Phase 5.4 — cross-case aggregate queries).

age_client and the gateway are both faked — no real Postgres/AGE (matches
the `no_network` guard, conftest, autouse).
"""
import pytest

import src.pipeline.xagg as xagg


class FakeAgeClient:
    def __init__(self, rows):
        self.rows = rows

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        return self.rows


class FakeGateway:
    def __init__(self, cases):
        self._cases = cases

    async def get_cases(self, user_id=None, user_role=None):
        return self._cases

    async def log_audit_event(self, **kwargs):
        pass


def _node(entity_id, label, **props):
    return {"id": entity_id, "label": label, "properties": {"entity_id": entity_id, **props}}


def _case(case_id):
    return {"id": case_id, "label": "Case", "properties": {"case_id": case_id}}


@pytest.fixture(autouse=True)
def _stub_confirmed_same_as(monkeypatch):
    """
    Default: no confirmed SAME_AS pairs, so canonicalization is a no-op and
    every pre-existing test's raw entity_ids pass through unchanged. Tests
    that care about canonicalization override this explicitly.
    """
    async def _empty():
        return []

    monkeypatch.setattr(xagg, "fetch_confirmed_same_as", _empty)


# ── Graph recurrence (vehicle/person keyword) ───────────────────────────────

async def test_vehicle_query_ranks_recurring_vehicles_by_case_count(monkeypatch):
    rows = [
        {"n": _node("V-001", "Vehicle", plate="ICT-LE-309"), "c": _case("CASE-007")},
        {"n": _node("V-001", "Vehicle", plate="ICT-LE-309"), "c": _case("CASE-008")},
        {"n": _node("V-001", "Vehicle", plate="ICT-LE-309"), "c": _case("CASE-009")},
        {"n": _node("V-999", "Vehicle", plate="XYZ-1"), "c": _case("CASE-050")},  # appears in only one case
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "what are the top recurring vehicles across cases", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "graph_recurrence"
    assert result["entity_type"] == "Vehicle"
    assert len(result["results"]) == 1  # the single-case vehicle is not "recurring"
    assert result["results"][0]["entity_id"] == "V-001"
    assert result["results"][0]["case_count"] == 3
    assert set(result["results"][0]["case_ids"]) == {"CASE-007", "CASE-008", "CASE-009"}


async def test_person_query_routes_to_person_recurrence(monkeypatch):
    rows = [
        {"n": _node("P-004", "Person", canonical_name="Repeat Offender"), "c": _case("CASE-010")},
        {"n": _node("P-004", "Person", canonical_name="Repeat Offender"), "c": _case("CASE-011")},
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "who is the top repeat offender across cases", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "graph_recurrence"
    assert result["entity_type"] == "Person"
    assert result["results"][0]["case_count"] == 2


async def test_person_recurrence_folds_confirmed_duplicate_entity_ids(monkeypatch):
    """
    Physical Person duplicates (e.g. the fir-1001-26 کاشف component's 139
    fresh entity_ids for one real human) must fold into ONE recurrence
    bucket, not fragment across several low-count entity_id buckets that
    each fall below the "recurring" bar. P-A and P-B are confirmed SAME_AS
    and each appears in only one distinct case — without folding, neither
    would even qualify as "recurring" (case_count > 1); folded, they must
    sum to 2 distinct cases under one canonical id.
    """
    rows = [
        {"n": _node("P-A", "Person", canonical_name="کاشف"), "c": _case("CASE-100")},
        {"n": _node("P-B", "Person", canonical_name="کاشف"), "c": _case("CASE-101")},
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    async def _confirmed_pair():
        return [("P-A", "P-B")]

    monkeypatch.setattr(xagg, "fetch_confirmed_same_as", _confirmed_pair)

    result = await xagg.run_aggregate(
        "who is the top repeat offender across cases", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "graph_recurrence"
    assert len(result["results"]) == 1
    assert result["results"][0]["case_count"] == 2
    assert set(result["results"][0]["case_ids"]) == {"CASE-100", "CASE-101"}


async def test_urdu_word_for_people_routes_to_person_recurrence(monkeypatch):
    """
    Regression guard: "لوگوں" ("people", the everyday Urdu word) used to be
    missing from _PERSON_KEYWORDS even though "شخص" was present — mirrors
    the same fix in src/retrieval/graph_retriever.py's _LABEL_KEYWORDS.
    """
    rows = [
        {"n": _node("P-700", "Person", canonical_name="Waqas Ali Niazi"), "c": _case("CASE-700")},
        {"n": _node("P-700", "Person", canonical_name="Waqas Ali Niazi"), "c": _case("CASE-701")},
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "مقدمات میں مذکور تمام لوگوں کی فہرست", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "graph_recurrence"
    assert result["entity_type"] == "Person"
    assert result["results"][0]["case_count"] == 2


# ── Graph recurrence (weapon keyword) — findings.md Module 4 ────────────────
#
# Weapon nodes are FIR-scoped by construction (see
# structured_projection._write_weapons()), so recurrence has to be counted
# by normalized weapon TYPE, not by node identity the way Vehicle/Person
# are above — these rows use the scalar (weapon_name/case_id) shape
# _top_recurring_weapon_types() actually queries for, not the nested
# node/case shape _top_recurring_nodes() uses.

async def test_weapon_query_merges_ammunition_suffix_variants_across_cases(monkeypatch):
    """Two different cases, each carrying a "30 بور پستول"-shaped weapon
    with a DIFFERENT ammunition-count suffix, must be counted as ONE
    recurring weapon type across 2 cases — not two separate single-case
    weapons. Real shape sampled from the live graph."""
    rows = [
        {"weapon_name": "30 بور پستول بمعہ 3 گولیاں", "case_id": "CASE-100"},
        {"weapon_name": "30 بور پستول بمعہ 6 گولیاں", "case_id": "CASE-101"},
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "which type of weapon appears most often across all cases", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "graph_recurrence"
    assert result["entity_type"] == "Weapon"
    assert len(result["results"]) == 1
    assert result["results"][0]["name"] == "30 بور پستول"
    assert result["results"][0]["case_count"] == 2
    assert set(result["results"][0]["case_ids"]) == {"CASE-100", "CASE-101"}


async def test_weapon_type_in_only_one_case_is_excluded(monkeypatch):
    """Mirrors _top_recurring_nodes's existing len(cases) > 1 recurrence
    bar: a weapon type that only appears in a single case is not
    "recurring" and must not be returned."""
    rows = [
        {"weapon_name": "30 بور پستول بمعہ 3 گولیاں", "case_id": "CASE-100"},
        {"weapon_name": "30 بور پستول بمعہ 6 گولیاں", "case_id": "CASE-101"},
        {"weapon_name": "عام لکڑی کی چھڑی، ایک عدد", "case_id": "CASE-102"},  # single-case, excluded
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "which type of weapon appears most often across all cases", None, gateway=None, user_role="supervisor"
    )

    assert len(result["results"]) == 1
    assert result["results"][0]["name"] == "30 بور پستول"


async def test_weapon_recurrence_fails_without_ammunition_suffix_normalization():
    """Proves the normalization step is load-bearing, not decorative:
    without stripping the "بمعہ N گولیاں" suffix, the two ammunition-count
    variants below are treated as two DIFFERENT weapon types (one case
    each) instead of one recurring type across two cases."""
    raw_names = ["30 بور پستول بمعہ 3 گولیاں", "30 بور پستول بمعہ 6 گولیاں"]
    assert len(set(raw_names)) == 2  # distinct strings pre-normalization
    normalized = {xagg._normalize_weapon_type(n) for n in raw_names}
    assert normalized == {"30 بور پستول"}  # merge only happens post-normalization


async def test_weapon_jurisdiction_case_ids_narrows_graph_recurrence(monkeypatch):
    rows = [
        {"weapon_name": "30 بور پستول", "case_id": "CASE-A"},
        {"weapon_name": "30 بور پستول بمعہ 3 گولیاں", "case_id": "CASE-B"},
    ]
    captured_params = {}

    class CapturingAgeClient:
        async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
            captured_params.update(params or {})
            assert "case_ids" in cypher_query
            allowed = (params or {}).get("case_ids", [])
            return [r for r in rows if r["case_id"] in allowed]

    monkeypatch.setattr(xagg, "age_client", CapturingAgeClient())

    result = await xagg.run_aggregate(
        "which type of weapon appears most often across all cases", None, gateway=None, user_role="supervisor",
        jurisdiction_case_ids=["CASE-A"],
    )

    assert captured_params["case_ids"] == ["CASE-A"]
    # Only CASE-A survives the narrowed match — "30 بور پستول" no longer
    # recurs (appears in >1 case) once CASE-B is excluded.
    assert result["results"] == []


# ── Relational aggregate (station/category) ─────────────────────────────────

async def test_station_query_groups_by_police_station(monkeypatch):
    gateway = FakeGateway([
        {"police_station": "Kohsar", "crime_category": "theft", "investigation_status": "open"},
        {"police_station": "Kohsar", "crime_category": "theft", "investigation_status": "open"},
        {"police_station": "Ramna", "crime_category": "burglary", "investigation_status": "closed"},
    ])

    result = await xagg.run_aggregate(
        "which police stations have the most open theft cases", None, gateway, user_role="supervisor"
    )

    assert result["kind"] == "relational_aggregate"
    assert result["group_by"] == "police_station"
    counts = {c["key"]: c["count"] for c in result["counts"]}
    assert counts.get("Kohsar") == 2
    # "open" + "theft" filters should have dropped the closed burglary case entirely
    assert "Ramna" not in counts


# ── Milestone E1: jurisdiction-narrowed candidate set ───────────────────────

async def test_jurisdiction_case_ids_narrows_relational_family_before_group_by(monkeypatch):
    gateway = FakeGateway([
        {"case_id": "CASE-A", "police_station": "Kohsar", "crime_category": "theft", "investigation_status": "open"},
        {"case_id": "CASE-B", "police_station": "Ramna", "crime_category": "theft", "investigation_status": "open"},
    ])

    result = await xagg.run_aggregate(
        "which police stations have the most open theft cases", None, gateway, user_role="supervisor",
        jurisdiction_case_ids=["CASE-A"],
    )

    counts = {c["key"]: c["count"] for c in result["counts"]}
    assert counts == {"Kohsar": 1}
    assert "Ramna" not in counts


async def test_jurisdiction_case_ids_narrows_graph_recurrence_family(monkeypatch):
    rows = [
        {"n": _node("V-001", "Vehicle", plate="ICT-LE-309"), "c": _case("CASE-A")},
        {"n": _node("V-001", "Vehicle", plate="ICT-LE-309"), "c": _case("CASE-B")},
    ]
    captured_params = {}

    class CapturingAgeClient:
        async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
            captured_params.update(params or {})
            assert "case_ids" in cypher_query
            return [r for r in rows if r["c"]["properties"]["case_id"] in (params or {}).get("case_ids", [])]

    monkeypatch.setattr(xagg, "age_client", CapturingAgeClient())

    result = await xagg.run_aggregate(
        "top recurring vehicles across cases", None, gateway=None, user_role="supervisor",
        jurisdiction_case_ids=["CASE-A"],
    )

    assert captured_params["case_ids"] == ["CASE-A"]
    # Only CASE-A's occurrence survives the narrowed match — V-001 no longer
    # "recurs" (appears in >1 case) once CASE-B is excluded, so it drops out.
    assert result["results"] == []


async def test_jurisdiction_case_ids_none_leaves_behavior_unchanged(monkeypatch):
    """The default (None) must reproduce the exact pre-E1 unscoped query."""
    captured_cypher = {}

    class CapturingAgeClient:
        async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
            captured_cypher["cypher"] = cypher_query
            return []

    monkeypatch.setattr(xagg, "age_client", CapturingAgeClient())

    await xagg.run_aggregate(
        "top recurring vehicles across cases", None, gateway=None, user_role="supervisor",
    )

    assert "case_ids" not in captured_cypher["cypher"]


async def test_default_relational_aggregate_groups_by_crime_category(monkeypatch):
    gateway = FakeGateway([
        {"police_station": "Kohsar", "crime_category": "fraud", "investigation_status": "open"},
        {"police_station": "Ramna", "crime_category": "fraud", "investigation_status": "open"},
    ])

    result = await xagg.run_aggregate(
        "how many cases of fraud this year", None, gateway, user_role="supervisor"
    )

    assert result["kind"] == "relational_aggregate"
    assert result["group_by"] == "crime_category"
    counts = {c["key"]: c["count"] for c in result["counts"]}
    assert counts.get("fraud") == 2


# ── Grand total (Priority 3 of the 2026-08-06 open-gaps audit) ──────────────
# demotestfinal.md §7: "کل کتنے کیسز ہیں؟" ("how many cases in total")
# returned a category-by-category breakdown instead of one total number —
# XAGG's keyword dispatch had no "no grouping" path at all.

async def test_english_total_query_returns_bare_total_not_breakdown(monkeypatch):
    gateway = FakeGateway([
        {"police_station": "Kohsar", "crime_category": "fraud", "investigation_status": "open"},
        {"police_station": "Ramna", "crime_category": "theft", "investigation_status": "closed"},
        {"police_station": "Ramna", "crime_category": "burglary", "investigation_status": "open"},
    ])

    result = await xagg.run_aggregate(
        "how many cases are there in total", None, gateway, user_role="supervisor"
    )

    assert result["kind"] == "total_count"
    assert result["total_cases"] == 3


async def test_urdu_total_query_returns_bare_total(monkeypatch):
    """The exact live-observed failing query from demotestfinal.md §7."""
    gateway = FakeGateway([
        {"police_station": "Kohsar", "crime_category": "fraud", "investigation_status": "open"},
        {"police_station": "Ramna", "crime_category": "theft", "investigation_status": "closed"},
    ])

    result = await xagg.run_aggregate(
        "کل کتنے کیسز ہیں؟", None, gateway, user_role="supervisor"
    )

    assert result["kind"] == "total_count"
    assert result["total_cases"] == 2


async def test_total_query_still_honors_a_status_filter(monkeypatch):
    """"How many cases in total" + an explicit status word should still
    filter by that status, just skip the group-by breakdown."""
    gateway = FakeGateway([
        {"police_station": "Kohsar", "crime_category": "fraud", "investigation_status": "open"},
        {"police_station": "Ramna", "crime_category": "theft", "investigation_status": "closed"},
        {"police_station": "Ramna", "crime_category": "burglary", "investigation_status": "closed"},
    ])

    result = await xagg.run_aggregate(
        "how many closed cases in total", None, gateway, user_role="supervisor"
    )

    assert result["kind"] == "total_count"
    assert result["total_cases"] == 2


async def test_total_keyword_yields_to_explicit_group_by_request(monkeypatch):
    """"Total cases by station" names a grouping dimension explicitly — it
    must still get the breakdown, not a bare number, even though "total"
    is present."""
    gateway = FakeGateway([
        {"police_station": "Kohsar", "crime_category": "fraud", "investigation_status": "open"},
        {"police_station": "Kohsar", "crime_category": "fraud", "investigation_status": "open"},
    ])

    result = await xagg.run_aggregate(
        "total cases per police station", None, gateway, user_role="supervisor"
    )

    assert result["kind"] == "relational_aggregate"
    assert result["group_by"] == "police_station"


# ── Legal-code semantic layer ───────────────────────────────────────────────

def test_split_crime_category_splits_and_trims():
    assert xagg.split_crime_category("PPC, Arms Ordinance 1965") == ["PPC", "Arms Ordinance 1965"]


def test_split_crime_category_single_act():
    assert xagg.split_crime_category("PPC") == ["PPC"]


def test_split_crime_category_none_and_blank():
    assert xagg.split_crime_category(None) == []
    assert xagg.split_crime_category("") == []


def test_split_crime_category_dedupes_preserving_order():
    assert xagg.split_crime_category("PPC, PPC, CNSA 1997") == ["PPC", "CNSA 1997"]


async def test_counts_by_act_collapses_differently_combined_cases():
    """The real gap this closes: 'PPC, Arms Ordinance 1965' and 'CNSA 1997,
    Arms Ordinance 1965' are two disconnected raw-string buckets, but both
    are real Arms-Ordinance cases — counts_by_act must show them as one
    number. The existing raw 'counts' field must stay exactly as it was
    (regression guard — no existing caller/renderer should see a change)."""
    gateway = FakeGateway([
        {"police_station": "Kohsar", "crime_category": "PPC, Arms Ordinance 1965", "investigation_status": "open"},
        {"police_station": "Ramna", "crime_category": "CNSA 1997, Arms Ordinance 1965", "investigation_status": "open"},
        {"police_station": "Kohsar", "crime_category": "PPC", "investigation_status": "open"},
    ])

    result = await xagg.run_aggregate(
        "how many cases by category", None, gateway, user_role="supervisor"
    )

    # Unchanged: raw-string grouping still fragments the two combinations.
    raw_counts = {c["key"]: c["count"] for c in result["counts"]}
    assert raw_counts == {
        "PPC, Arms Ordinance 1965": 1,
        "CNSA 1997, Arms Ordinance 1965": 1,
        "PPC": 1,
    }

    # New: per-act breakdown collapses both Arms-Ordinance combinations.
    by_act = {c["key"]: c["count"] for c in result["counts_by_act"]}
    assert by_act == {"Arms Ordinance 1965": 2, "PPC": 2, "CNSA 1997": 1}


async def test_counts_by_act_absent_when_grouping_by_station():
    gateway = FakeGateway([
        {"police_station": "Kohsar", "crime_category": "PPC, Arms Ordinance 1965", "investigation_status": "open"},
    ])

    result = await xagg.run_aggregate(
        "cases by station", None, gateway, user_role="supervisor"
    )

    assert result["group_by"] == "police_station"
    assert "counts_by_act" not in result


async def test_legal_code_act_keyword_filters_by_real_act_membership(monkeypatch):
    """_LEGAL_CODE_ACT_KEYWORDS is empty by design until a real, sourced
    description exists for an act (see its own comment in xagg.py) — this
    proves the FILTERING MECHANISM itself is correct once populated,
    without asserting anything about which acts are covered today.

    Deliberately uses "narcotics", not weapon vocabulary — a keyword
    overlapping _WEAPON_KEYWORDS would dispatch to the graph-based weapon
    recurrence path before ever reaching this filter (see
    _LEGAL_CODE_ACT_KEYWORDS' own caveat comment); this test isolates the
    relational/_filtered_cases() path specifically."""
    monkeypatch.setitem(xagg._LEGAL_CODE_ACT_KEYWORDS, "CNSA 1997", ("narcotics",))
    gateway = FakeGateway([
        {"police_station": "Kohsar", "crime_category": "CNSA 1997, Arms Ordinance 1965", "investigation_status": "open"},
        {"police_station": "Ramna", "crime_category": "PPC", "investigation_status": "open"},
    ])

    result = await xagg.run_aggregate(
        "how many narcotics cases are there", None, gateway, user_role="supervisor"
    )

    assert result["kind"] == "relational_aggregate"
    assert result["total_cases_considered"] == 1


async def test_legal_code_act_keyword_no_match_leaves_cases_unfiltered(monkeypatch):
    monkeypatch.setitem(xagg._LEGAL_CODE_ACT_KEYWORDS, "CNSA 1997", ("narcotics",))
    gateway = FakeGateway([
        {"police_station": "Kohsar", "crime_category": "CNSA 1997, Arms Ordinance 1965", "investigation_status": "open"},
        {"police_station": "Ramna", "crime_category": "PPC", "investigation_status": "open"},
    ])

    result = await xagg.run_aggregate(
        "how many cases by category", None, gateway, user_role="supervisor"
    )

    assert result["total_cases_considered"] == 2


# ── "List all cases" vs. a specific-act count query [Bug fix] ─────────────────
#
# Regression coverage for the DeepEval eval finding (xagg-01): "How many
# cases involve the Arms Ordinance ACROSS ALL CASES? Give a count." answered
# 79 (the whole corpus) instead of 29 (the real Arms-Ordinance count),
# because "across all cases" contains the literal substring "all cases",
# which matched _LIST_ALL_KEYWORDS and returned every case completely
# unfiltered — the guard on that branch excluded station/status/category
# keyword collisions but never a legal-code-act collision. No test at all
# previously covered this branch (`case_listing`), which is exactly how the
# bug shipped unnoticed.

async def test_plain_list_all_with_no_act_mentioned_still_lists_every_case(monkeypatch):
    """Non-regression: a genuine "list every case" request, naming no
    specific act, must still return the full unfiltered case_listing — the
    fix only needed to add ONE more exclusion, not weaken this branch."""
    gateway = FakeGateway([
        {"case_id": "C-1", "fir_number": "1/26", "crime_category": "PPC", "investigation_status": "open", "police_station": "Kohsar"},
        {"case_id": "C-2", "fir_number": "2/26", "crime_category": "CNSA 1997", "investigation_status": "open", "police_station": "Ramna"},
    ])

    result = await xagg.run_aggregate(
        "list all cases", None, gateway, user_role="supervisor"
    )

    assert result["kind"] == "case_listing"
    assert len(result["cases"]) == 2


async def test_act_query_phrased_with_all_cases_is_not_misrouted_to_unfiltered_listing(monkeypatch):
    """The actual fix: a query naming a specific act (here "narcotics",
    already keyword-mapped — see this module's own caveat on why "Arms
    Ordinance 1965" itself needs a separate keyword-table fix, not this
    routing fix alone) must never fall into the unfiltered "list every case"
    branch just because it also happens to say "all cases"."""
    monkeypatch.setitem(xagg._LEGAL_CODE_ACT_KEYWORDS, "CNSA 1997", ("narcotics",))
    gateway = FakeGateway([
        {"case_id": "C-1", "fir_number": "1/26", "crime_category": "CNSA 1997", "investigation_status": "open", "police_station": "Kohsar"},
        {"case_id": "C-2", "fir_number": "2/26", "crime_category": "PPC", "investigation_status": "open", "police_station": "Ramna"},
        {"case_id": "C-3", "fir_number": "3/26", "crime_category": "PPC, Illegal Dispossession Act 2005", "investigation_status": "open", "police_station": "Saddar"},
    ])

    result = await xagg.run_aggregate(
        "How many cases involve narcotics across all cases? Give a count.",
        None, gateway, user_role="supervisor",
    )

    assert result["kind"] != "case_listing"
    assert result["kind"] == "total_count"
    assert result["total_cases"] == 1


# ── "Arms Ordinance 1965" missing from _LEGAL_CODE_ACT_KEYWORDS [Bug fix] ──────
#
# The second, independent half of the same eval finding (xagg-01):
# _LEGAL_CODE_ACT_KEYWORDS had no entry at all for "Arms Ordinance 1965",
# despite it being one of the most common acts in the corpus — so even a
# query that named the act directly, with no _LIST_ALL_KEYWORDS collision,
# never filtered to just those cases. This reproduces the real eval query
# verbatim, now with the keyword entry populated by the actual fix.

async def test_arms_ordinance_query_now_reaches_and_narrows_via_the_act_filter():
    """The real eval query, byte-for-byte, against a fixture mirroring the
    corpus's actual comma-joined crime_category shape (a case can carry
    more than one act) -- ground truth for the live corpus was 29; this
    fixture uses a smaller equivalent (3 real matches out of 5 cases,
    including one where Arms Ordinance is joined with a second act)."""
    gateway = FakeGateway([
        {"case_id": "C-1", "fir_number": "1/26", "crime_category": "PPC, Arms Ordinance 1965", "investigation_status": "open", "police_station": "Kohsar"},
        {"case_id": "C-2", "fir_number": "2/26", "crime_category": "CNSA 1997, Arms Ordinance 1965", "investigation_status": "open", "police_station": "Ramna"},
        {"case_id": "C-3", "fir_number": "3/26", "crime_category": "Arms Ordinance 1965", "investigation_status": "open", "police_station": "Saddar"},
        {"case_id": "C-4", "fir_number": "4/26", "crime_category": "PPC", "investigation_status": "open", "police_station": "Kohsar"},
        {"case_id": "C-5", "fir_number": "5/26", "crime_category": "PECA 2016, PPC", "investigation_status": "open", "police_station": "Ramna"},
    ])

    result = await xagg.run_aggregate(
        "How many cases involve the Arms Ordinance across all cases? Give a count.",
        None, gateway, user_role="supervisor",
    )

    assert result["kind"] == "total_count"
    assert result["total_cases"] == 3


async def test_arms_ordinance_keyword_does_not_collide_with_weapon_dispatch():
    """The chosen keywords ("arms ordinance", "illegal arms", "unlicensed
    arms", "arms act") must not overlap _WEAPON_KEYWORDS ("weapon",
    "pistol", "gun", "firearm", ...) -- an overlap would shadow this act
    filter behind the earlier graph-based weapon-recurrence dispatch (see
    _LEGAL_CODE_ACT_KEYWORDS' own CAVEAT comment). Confirms the query
    actually reaches the relational path, not xagg.age_client."""
    gateway = FakeGateway([
        {"case_id": "C-1", "fir_number": "1/26", "crime_category": "Arms Ordinance 1965", "investigation_status": "open", "police_station": "Kohsar"},
        {"case_id": "C-2", "fir_number": "2/26", "crime_category": "PPC", "investigation_status": "open", "police_station": "Ramna"},
    ])

    result = await xagg.run_aggregate(
        "how many cases involve illegal arms", None, gateway, user_role="supervisor"
    )

    # "how many cases..." also matches _TOTAL_KEYWORDS, so this correctly
    # lands on the bare-total path (kind total_count) rather than the
    # grouped-count path -- what matters here is that it went through
    # _filtered_cases()'s act filter at all (total_cases == 1, not 2),
    # proving it wasn't shadowed by the weapon-recurrence dispatch above.
    assert result["kind"] == "total_count"
    assert result["total_cases"] == 1


# ── RBAC gate ────────────────────────────────────────────────────────────────

async def test_investigator_cannot_run_cross_case_aggregate():
    """
    XAGG is cross-case exactly like XGRAPH — it must carry the same
    supervisor-or-higher role gate, not silently answer for any role.
    """
    with pytest.raises(PermissionError):
        await xagg.run_aggregate(
            "how many cases of fraud this year", None, gateway=None, user_role="investigator"
        )


async def test_denied_aggregate_never_arms_the_rls_bypass():
    """
    Phase 2 regression test — same fix/rationale as
    test_graph_retriever.py's equivalent: current_cross_case must not be
    armed by a denied XAGG attempt (issues.md's High "cross-case RLS
    bypass flag is armed before its own role check" finding).
    """
    from src.database.postgres import current_cross_case

    current_cross_case.set(False)
    with pytest.raises(PermissionError):
        await xagg.run_aggregate(
            "how many cases of fraud this year", None, gateway=None, user_role="investigator"
        )
    assert current_cross_case.get() is False


async def test_authorized_aggregate_arms_the_rls_bypass():
    from src.database.postgres import current_cross_case

    current_cross_case.set(False)
    monkeypatch_gateway = FakeGateway(cases=[])
    await xagg.run_aggregate(
        "how many cases are open right now", None, gateway=monkeypatch_gateway, user_role="supervisor"
    )
    assert current_cross_case.get() is True


# ── Gold-QA fix — ROOT_CAUSE_AND_FIXES.md Module 1 ──────────────────────────
# A bare "how many accused in total" used to reach _top_recurring_nodes
# ("Person") above (it matches _PERSON_KEYWORDS on "accused"), which only
# ever returns people appearing in MORE than one case — live-confirmed to
# answer 4 for a real headcount far higher. These tests cover the new
# total-vs-recurring split, the unsupported-aggregate refusal, the district
# rollup, and the gender breakdown's pre-/post-backfill shape.

def _accused_row(entity_id, case_id, **props):
    return {"p": _node(entity_id, "Person", **props), "c": _case(case_id)}


async def test_bare_accused_total_counts_every_distinct_person_not_just_recurring(monkeypatch):
    """The exact "4 vs 94" bug: a single-case-only accused must still be
    counted here, unlike the recurring-persons path."""
    rows = [
        _accused_row("P-001", "CASE-001"),  # appears in only one case
        _accused_row("P-002", "CASE-002"),  # appears in only one case
        _accused_row("P-003", "CASE-003"),
        _accused_row("P-003", "CASE-004"),  # recurring — still counted once here
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "how many accused persons are there in total", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "total_accused_count"
    assert result["total_accused"] == 3


async def test_recurrence_language_still_routes_to_recurring_persons_path(monkeypatch):
    """A query naming BOTH "accused" and recurrence language must still hit
    the existing recurring-persons path, not the new total path."""
    rows = [
        {"n": _node("P-004", "Person", canonical_name="Repeat Offender"), "c": _case("CASE-010")},
        {"n": _node("P-004", "Person", canonical_name="Repeat Offender"), "c": _case("CASE-011")},
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "which accused appear in multiple cases", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "graph_recurrence"
    assert result["entity_type"] == "Person"


async def test_age_question_on_an_ageless_corpus_says_so_instead_of_a_wrong_number(monkeypatch):
    """[Gold-QA fix — Module 31] REWRITTEN, deliberately. This used to assert
    the hard `_UNSUPPORTED_AGE` refusal for EVERY age question. Module 31
    demoted that refusal: `Person.age` has been projected since Module 1d,
    so refusing unconditionally asserted something false about the data
    model. The property this test still has to protect is the one it was
    written for — an age question must never be answered by an unrelated
    family — so it now pins the honest empty-corpus message instead."""
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient([]))

    result = await xagg.run_aggregate(
        "what is the average age of the accused", None,
        gateway=FakeGateway([]), user_role="supervisor",
    )

    assert result["kind"] == "offender_age_profile"
    assert result["unsupported"] is True
    assert "age" in result["message"].lower()
    # The specific regression: NOT the person-recurrence family, which
    # "accused" would otherwise match.
    assert result["kind"] != "graph_recurrence"


async def test_officer_question_returns_unsupported_aggregate(monkeypatch):
    result = await xagg.run_aggregate(
        "which investigating officer has the most cases", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "unsupported_aggregate"


async def test_trend_question_returns_unsupported_aggregate(monkeypatch):
    result = await xagg.run_aggregate(
        "what is the reporting delay trend over time", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "unsupported_aggregate"


class _RaisingAgeClient:
    """Fails any execute_cypher call — used to prove a query never reaches
    the DB at all, not just that its result was discarded."""

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        raise AssertionError("age_client.execute_cypher should not have been called")


class _SequentialAgeClient:
    """Returns one rows-list per call, in order — for a function like
    _reporting_delay_count() that issues more than one distinct query."""

    def __init__(self, rows_per_call):
        self._rows_per_call = list(rows_per_call)
        self.calls = 0

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        rows = self._rows_per_call[self.calls]
        self.calls += 1
        return rows


# [Gold-QA fix — Module 2, A7] Reporting-delay COUNT is a distinct shape from
# the reporting-delay TREND test above, and the two must never collide — see
# xagg.py's own comment above _REPORTING_DELAY_COUNT_KEYWORDS for the exact
# regression this pair of tests guards against (a prior attempt at this fix
# shipped a keyword collision that misrouted a trend question into a live
# count query).

async def test_reporting_delay_count_question_routes_to_the_count_path(monkeypatch):
    fake = _SequentialAgeClient([[{"n": 73}], [{"n": 8}]])
    monkeypatch.setattr(xagg, "age_client", fake)

    result = await xagg.run_aggregate(
        "how many FIRs gave a delay reason for reporting late", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "reporting_delay_count"
    assert result["unsupported"] is False
    assert result["with_delay_reason"] == 8
    assert result["total_firs"] == 73
    assert fake.calls == 2


async def test_reporting_delay_trend_question_never_reaches_the_count_path(monkeypatch):
    """Regression-proofing: even though this phrasing contains reporting-
    delay vocabulary, it also says "trend over time" and must fall straight
    through to the honest unsupported-trend message WITHOUT ever calling
    age_client — using a client that fails on any call proves this, rather
    than just checking the returned kind (which a coincidentally-correct
    fallback could satisfy while still having made a wrong query first)."""
    monkeypatch.setattr(xagg, "age_client", _RaisingAgeClient())

    result = await xagg.run_aggregate(
        "what is the reporting delay reason trend over time", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "unsupported_aggregate"


async def test_reporting_delay_count_pre_backfill_is_an_honest_not_yet_synced(monkeypatch):
    """Pre-backfill: no Incident node carries reporting_delay_reason yet —
    must say so, not silently return a wrong zero."""
    fake = _SequentialAgeClient([[{"n": 73}], [{"n": 0}], [{"n": 0}]])
    monkeypatch.setattr(xagg, "age_client", fake)

    result = await xagg.run_aggregate(
        "how many FIRs gave a delay reason for reporting late", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "reporting_delay_count"
    assert result["unsupported"] is True


# [Gold-QA fix — Module 7, CP6] Placeholder-officer count.

def _officer_row(name, superseded_by=None):
    return {"name": name, "superseded_by": superseded_by}


async def test_placeholder_officer_count_question_routes_and_counts_correctly(monkeypatch):
    rows = [
        _officer_row("(نامزد ASI)"),                     # current placeholder, ASI
        _officer_row("(نامزد ASI)"),                     # current placeholder, ASI
        _officer_row("(نامزد SI)"),                       # current placeholder, SI
        _officer_row("طارق"),                              # current, real name — not a placeholder
        _officer_row("(نامزد ASI)", superseded_by=12345),  # HISTORICAL placeholder, since replaced
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "Kitne cases abhi tak bina kisi tafteeshi afsar ke asal tor par muqarrar kiye pade hue hain?",
        None, gateway=None, user_role="supervisor",
    )

    assert result["kind"] == "placeholder_officer_count"
    assert result["current_count"] == 3
    assert result["ever_count"] == 4
    assert result["asi_count"] == 2
    assert result["si_count"] == 1


async def test_placeholder_officer_asi_is_not_double_counted_into_si_bucket(monkeypatch):
    """"(نامزد ASI)" contains "SI" as a substring — the SI bucket must not
    also count every ASI placeholder."""
    rows = [_officer_row("(نامزد ASI)")]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "how many cases have a placeholder investigating officer",
        None, gateway=None, user_role="supervisor",
    )

    assert result["asi_count"] == 1
    assert result["si_count"] == 0


async def test_general_officer_identity_question_is_still_unsupported(monkeypatch):
    """Regression guard: the new placeholder-specific keywords must not
    swallow a general "which officer" identity question — that still has
    no data path and must keep its honest hard refusal."""
    result = await xagg.run_aggregate(
        "which investigating officer has the most cases", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "unsupported_aggregate"


async def test_gender_question_without_populated_data_is_an_honest_not_yet_synced(monkeypatch):
    """Pre-backfill: no Person node carries a gender property yet — must
    say so, not silently return zero or an unrelated number."""
    rows = [_accused_row("P-001", "CASE-001"), _accused_row("P-002", "CASE-002")]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "how many of the accused are women", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "gender_breakdown"
    assert result["unsupported"] is True


async def test_gender_question_with_populated_data_returns_a_real_breakdown(monkeypatch):
    rows = [
        _accused_row("P-001", "CASE-001", gender="female"),
        _accused_row("P-002", "CASE-002", gender="male"),
        _accused_row("P-003", "CASE-003", gender="male"),
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "are there more male or female accused", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "gender_breakdown"
    assert result["unsupported"] is False
    counts = {c["key"]: c["count"] for c in result["counts"]}
    assert counts == {"female": 1, "male": 2}


async def test_district_question_returns_a_district_rollup(monkeypatch):
    rows = [
        {"district": "Lahore", "n_count": 5},
        {"district": "Karachi", "n_count": 2},
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "which district has the most FIRs", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "district_breakdown"
    assert result["entity_label"] is None
    assert result["counts"][0] == {"district": "Lahore", "count": 5}


async def test_station_count_question_counts_distinct_stations_not_cases_per_station(monkeypatch):
    """
    Module 2a: "how many police stations are there" must count distinct
    PoliceStation nodes, not fall into the group-by-station case-count path
    (which would answer a different question, or nothing for an empty
    corpus).
    """
    rows = [
        {"station_id": "ST-001"}, {"station_id": "ST-002"}, {"station_id": "ST-002"},
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "how many police stations are there", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "station_total_count"
    assert result["total_stations"] == 2


async def test_district_weapon_question_scopes_to_weapon_label(monkeypatch):
    rows = [{"district": "Lahore", "n_count": 3}]
    captured = {}

    class CapturingAgeClient:
        async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
            captured["query"] = cypher_query
            return rows

    monkeypatch.setattr(xagg, "age_client", CapturingAgeClient())

    result = await xagg.run_aggregate(
        "which district recovers the most weapons", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "district_breakdown"
    assert result["entity_label"] == "Weapon"
    assert "Weapon" in captured["query"]


# ── Module 13 — derived-aggregate primitives (RC-2) ─────────────────────────
#
# _rate_breakdown()/_extract_year()/_count_breakdown_by_year() tested
# directly first (pure functions, no DB), then each of CP1/M1/M7/A1's own
# aggregates through run_aggregate() same as every other question above.

def test_rate_breakdown_computes_subset_over_total_per_group():
    subset = {"Lahore": 3, "Karachi": 1}
    total = {"Lahore": 10, "Karachi": 4, "Multan": 5}
    ranked = xagg._rate_breakdown(subset, total)

    by_key = {r["key"]: r for r in ranked}
    assert by_key["Lahore"] == {"key": "Lahore", "subset_count": 3, "total_count": 10, "rate": 0.3}
    assert by_key["Karachi"] == {"key": "Karachi", "subset_count": 1, "total_count": 4, "rate": 0.25}
    # Multan has no subset entry at all — must default to 0, not KeyError.
    assert by_key["Multan"] == {"key": "Multan", "subset_count": 0, "total_count": 5, "rate": 0.0}
    # Sorted by rate descending by default.
    assert [r["key"] for r in ranked] == ["Lahore", "Karachi", "Multan"]


def test_rate_breakdown_skips_zero_total_groups():
    """A group with total_count == 0 has an undefined rate — must be
    dropped entirely, never fabricated as 0/0 == 0."""
    ranked = xagg._rate_breakdown({"Ghost District": 2}, {"Ghost District": 0, "Lahore": 5})
    assert [r["key"] for r in ranked] == ["Lahore"]


def test_extract_year_parses_date_prefix_and_rejects_junk():
    assert xagg._extract_year("2024-03-15") == 2024
    assert xagg._extract_year("2024-03-15T10:00:00Z") == 2024
    assert xagg._extract_year(None) is None
    assert xagg._extract_year("") is None
    assert xagg._extract_year("unknown") is None


def test_count_breakdown_by_year_partitions_and_supports_multi_key_rows():
    rows = [
        {"incident_date": "2024-01-10", "acts": ["PPC"]},
        {"incident_date": "2024-06-20", "acts": ["PPC", "Arms Ordinance 1965"]},
        {"incident_date": "2026-02-01", "acts": ["CNSA 1997"]},
        {"incident_date": None, "acts": ["PPC"]},  # unparseable — must be skipped, not mis-bucketed
    ]
    buckets = xagg._count_breakdown_by_year(rows, "incident_date", lambda r: r["acts"])

    assert set(buckets.keys()) == {2024, 2026}
    assert buckets[2024] == {"PPC": 2, "Arms Ordinance 1965": 1}
    assert buckets[2026] == {"CNSA 1997": 1}


# [Gold-QA fix — Module 13, question CP1]

async def test_cp1_weapon_recovery_rate_routes_and_computes_rate(monkeypatch):
    total_rows = [{"district": "Lahore", "n_count": 10}, {"district": "Karachi", "n_count": 4}]
    subset_rows = [{"district": "Lahore", "n_count": 2}, {"district": "Karachi", "n_count": 2}]
    fake = _SequentialAgeClient([total_rows, subset_rows])
    monkeypatch.setattr(xagg, "age_client", fake)

    result = await xagg.run_aggregate(
        "Kaunsa zila apne case load ke lihaz se sab se zyada hathiyar baramad karta hai?",
        None, gateway=None, user_role="supervisor",
    )

    assert result["kind"] == "rate_breakdown"
    assert result["dimension"] == "weapon_recovery_rate_by_district"
    by_district = {c["district"]: c for c in result["counts"]}
    assert by_district["Lahore"] == {
        "district": "Lahore", "cases_with_weapon": 2, "total_cases": 10, "rate": 0.2,
    }
    # Karachi has the higher RATE (0.5) despite fewer absolute recoveries
    # than Lahore — the whole point of this being a rate, not a flat count.
    assert by_district["Karachi"]["rate"] == 0.5
    assert fake.calls == 2


async def test_district_weapon_question_without_rate_signal_stays_a_flat_count(monkeypatch):
    """Regression guard: a plain 'which district recovers the most weapons'
    (no rate/relative-to-caseload language) must keep its existing
    flat-count behavior — CP1's rate dispatch is additive, not a
    replacement, and only fires when a rate signal is also present."""
    rows = [{"district": "Lahore", "n_count": 3}]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "which district recovers the most weapons", None, gateway=None, user_role="supervisor",
    )

    assert result["kind"] == "district_breakdown"


# [Gold-QA fix — Module 13, question M1]

async def test_m1_statute_mix_by_year_routes_and_buckets_by_year(monkeypatch):
    # [Module 13 fix] crime_category is NOT a graph property (confirmed by
    # reading structured_projection.py — it's Postgres-only, on the
    # gateway.get_cases() row) — age_client supplies only the incident year
    # per case_id; the gateway supplies crime_category per case_id, joined
    # by case_id inside _statute_mix_by_year() itself.
    year_rows = [
        {"incident_date": "2024-02-01", "case_id": "CASE-1"},
        {"incident_date": "2024-05-15", "case_id": "CASE-2"},
        {"incident_date": "2026-01-10", "case_id": "CASE-3"},
    ]
    cases = [
        {"case_id": "CASE-1", "crime_category": "PPC"},
        {"case_id": "CASE-2", "crime_category": "PPC, Arms Ordinance 1965"},
        {"case_id": "CASE-3", "crime_category": "CNSA 1997"},
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(year_rows))

    result = await xagg.run_aggregate(
        "What kinds of cases are we dealing with now compared to a couple of years back?",
        None, gateway=FakeGateway(cases), user_role="supervisor",
    )

    assert result["kind"] == "time_bucketed_breakdown"
    assert result["dimension"] == "statute_by_year"
    by_year = {b["year"]: {c["key"]: c["count"] for c in b["counts"]} for b in result["buckets"]}
    assert by_year[2024] == {"PPC": 2, "Arms Ordinance 1965": 1}
    assert by_year[2026] == {"CNSA 1997": 1}


async def test_m1_time_comparison_wins_over_the_generic_trend_refusal(monkeypatch):
    """'year over year' is also a literal _TREND_KEYWORDS entry — this
    proves the new time-comparison dispatch (a real aggregate) wins over
    the older hard 'not available' refusal for the shapes it now covers."""
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient([]))

    result = await xagg.run_aggregate(
        "how does the statute mix compare year over year",
        None, gateway=FakeGateway([]), user_role="supervisor",
    )

    assert result["kind"] == "time_bucketed_breakdown"


# [Gold-QA fix — Module 13, question M7 — SUPERSEDED BY MODULE 22]
#
# Module 13 could only offer the delay-REASON rate as an honest proxy for
# M7, because no sub-day timestamp was projected anywhere queryable — see
# _reporting_delay_rate_by_year()'s own `note`, which said so. Module 22
# projects Incident.incident_datetime/.report_datetime, so M7 now dispatches
# to the true mean-minutes aggregate instead. The rate aggregate itself is
# NOT removed — it answers the A7-family delay-reason question, covered by
# its own direct test below.

async def test_m7_routes_to_true_mean_minutes_not_the_delay_reason_rate(monkeypatch):
    """M7's literal gold text must now reach the mean-minutes aggregate."""
    rows = [
        {"incident_datetime": "2024-09-25T17:10:00Z", "report_datetime": "2024-09-25T17:25:00Z"},
        {"incident_datetime": "2026-01-01T10:00:00Z", "report_datetime": "2026-01-02T09:00:00Z"},
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "Kya log 2026 mein waqiaat ki police ko itni hi jaldi ittila de rahe hain jitni 2024 mein dete the?",
        None, gateway=None, user_role="supervisor",
    )

    assert result["kind"] == "time_bucketed_mean"
    assert result["dimension"] == "incident_to_report_minutes_by_year"
    assert result["unit"] == "minutes"
    by_year = {b["year"]: b for b in result["buckets"]}
    assert by_year[2024]["mean_minutes"] == 15.0
    assert by_year[2024]["case_count"] == 1
    assert by_year[2026]["mean_minutes"] == 1380.0  # 23h
    assert by_year[2026]["case_count"] == 1


async def test_reporting_delay_rate_aggregate_still_works_for_its_own_question(monkeypatch):
    """Regression guard: Module 22 re-pointed M7's dispatch but must not
    have broken the delay-REASON rate aggregate, which answers a different
    (A7-family) question and is still reachable directly."""
    rows = [
        {"incident_date": "2024-03-01", "reporting_delay_reason": "late report"},
        {"incident_date": "2024-04-01", "reporting_delay_reason": None},
        {"incident_date": "2026-01-01", "reporting_delay_reason": None},
        {"incident_date": "2026-02-01", "reporting_delay_reason": None},
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg._reporting_delay_rate_by_year()

    assert result["kind"] == "time_bucketed_rate"
    assert result["dimension"] == "reporting_delay_rate_by_year"
    by_year = {b["year"]: b for b in result["buckets"]}
    assert by_year[2024] == {"year": 2024, "delayed_count": 1, "total_count": 2, "rate": 0.5}
    assert by_year[2026] == {"year": 2026, "delayed_count": 0, "total_count": 2, "rate": 0.0}


# [Gold-QA fix — Module 22, question M7] mean incident->report minutes

def test_parse_iso_datetime_tolerates_z_suffix_and_agtype_quoting():
    """AGE returns the property as a quoted agtype string, and the API's own
    values carry a `Z` suffix — both must parse."""
    parsed = xagg._parse_iso_datetime("2024-09-25T17:10:00Z")
    assert parsed is not None and parsed.year == 2024 and parsed.minute == 10
    assert xagg._parse_iso_datetime('"2024-09-25T17:10:00Z"') == parsed
    assert xagg._parse_iso_datetime("") is None
    assert xagg._parse_iso_datetime(None) is None
    assert xagg._parse_iso_datetime("not-a-timestamp") is None


def test_minutes_between_rejects_out_of_order_pairs_instead_of_averaging_them():
    """A report earlier than its incident is a data defect, not a negative
    delay — averaging it in would silently drag the mean down and hide the
    bad row, so it must be excluded (None), not returned as a negative."""
    assert xagg._minutes_between("2024-09-25T17:10:00Z", "2024-09-25T17:25:00Z") == 15.0
    assert xagg._minutes_between("2024-09-25T17:25:00Z", "2024-09-25T17:10:00Z") is None
    assert xagg._minutes_between(None, "2024-09-25T17:25:00Z") is None
    assert xagg._minutes_between("2024-09-25T17:10:00Z", "bad") is None


async def test_incident_to_report_minutes_excludes_unusable_rows_and_reports_coverage(monkeypatch):
    """Rows missing/reversing a timestamp must be EXCLUDED from the mean and
    counted in missing_timestamp_count — never silently treated as a zero
    delay, which would pull every average toward 0."""
    rows = [
        {"incident_datetime": "2024-01-01T10:00:00Z", "report_datetime": "2024-01-01T10:10:00Z"},
        {"incident_datetime": "2024-01-02T10:00:00Z", "report_datetime": "2024-01-02T10:20:00Z"},
        # reversed pair — a data defect, must not become a negative delay
        {"incident_datetime": "2024-01-03T12:00:00Z", "report_datetime": "2024-01-03T11:00:00Z"},
        # unparseable
        {"incident_datetime": "not-a-date", "report_datetime": "2024-01-04T10:00:00Z"},
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg._incident_to_report_minutes_by_year()

    assert result["buckets"] == [{"year": 2024, "mean_minutes": 15.0, "case_count": 2}]
    assert result["missing_timestamp_count"] == 2


def test_render_time_bucketed_mean_states_scale_direction_and_coverage():
    rendered = "\n".join(xagg.render_time_bucketed_mean({
        "kind": "time_bucketed_mean",
        "buckets": [
            {"year": 2024, "mean_minutes": 15.0, "case_count": 13},
            {"year": 2026, "mean_minutes": 1401.3, "case_count": 51},
        ],
        "missing_timestamp_count": 9,
    }))
    # The gold answer's own numbers and scale ("1401.3 minute (~23.4 ghante)").
    assert "15.0 minutes" in rendered and "13 FIRs" in rendered
    assert "1401.3 minutes" in rendered and "23.4 hours" in rendered and "51 FIRs" in rendered
    assert "slower in 2026" in rendered
    assert "9 FIR(s) excluded" in rendered


class TestReportingSpeedComparisonBoundary:
    """
    [Gold-QA fix — Module 22] The original keyword list was pinned to M7's
    literal phrasing, so the required non-gold paraphrase ("How long does it
    typically take someone to report a crime to us these days versus a
    couple of years ago?") matched nothing — the capability was curve-fit to
    one gold string. Widening it is a two-signal AND, because a one-signal
    widening collides with A7 (a COUNT-of-delay-reasons question checked
    AFTER this one in run_aggregate(), so an over-broad match silently
    hijacks it) and with KB8 (whose "pehle" means "before completion", not
    "years before" — matching it would regress PR #9's KB-corpus routing).
    """

    def test_m7_gold_text_matches(self):
        assert xagg._is_reporting_speed_comparison(
            "kya log 2026 mein waqiaat ki police ko itni hi jaldi ittila de "
            "rahe hain jitni 2024 mein dete the?"
        )

    @pytest.mark.parametrize("paraphrase", [
        "How long does it typically take someone to report a crime to us "
        "these days versus a couple of years ago?",
        "Are people informing police faster now compared to 2024?",
        "Has reporting speed changed since 2024?",
    ])
    def test_non_gold_paraphrases_match(self, paraphrase):
        assert xagg._is_reporting_speed_comparison(paraphrase.lower())

    def test_a7_delay_reason_count_is_not_hijacked(self):
        """A7 asks HOW MANY complainants gave a reason for reporting late —
        a count, answered by a different aggregate checked after this one."""
        assert not xagg._is_reporting_speed_comparison(
            "kitne cases mein mudai ne police ke paas waqe ke kuch arse baad "
            "aane ki koi wajah batai, bajaye foran aane ke?"
        )

    def test_kb8_before_completion_is_not_a_time_period_comparison(self):
        """KB8's "pehle" means "before the investigation completes", not "a
        few years before" — caught live during this module's negative
        control, and it must stay excluded or PR #9's KB routing regresses."""
        assert not xagg._is_reporting_speed_comparison(
            "agar kisi case ki tafteesh lambi ho jaye, to kya qanoon police "
            "ko iske mukammal hone se pehle adaalat ko kuch report karna "
            "zaroori karta hai — aur kya hamara case-tracking data batayega "
            "ke aisa hua ya nahi?"
        )

    def test_matches_m7_and_no_other_gold_question(self):
        """The all-32 negative control. This single assertion is what would
        have caught every one of the three historical pattern collisions on
        this plan (Module 8c's 0-of-7, CR8 hijacking KB1, "عدالت" hijacking
        M4)."""
        import json
        from pathlib import Path

        # This used to look for a bare `Gold_QA_Dataset_Final32.json` at the
        # repo root and `pytest.skip()` when it was missing. That file has
        # never existed in this repo -- only the `_With_Answers` variant under
        # `evaluation/` is tracked -- so this control SILENTLY SKIPPED on every
        # run, in CI included, from the day it was written. It protected
        # nothing. Found 2026-09-08 while verifying Modules 23 and 28, whose
        # own all-32 controls resolve the correct path and do run.
        #
        # Resolved against candidates now, and a MISSING file is a FAILURE
        # rather than a skip: a negative control that quietly stops running is
        # worse than no control at all, because it reads as passing.
        root = Path(__file__).resolve().parent.parent
        candidates = [
            root / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json",
            root / "Gold_QA_Dataset_Final32.json",
        ]
        gold_path = next((p for p in candidates if p.exists()), None)
        assert gold_path is not None, (
            "Gold-32 dataset not found -- looked for: "
            + ", ".join(str(p) for p in candidates)
        )
        payload = json.loads(gold_path.read_text(encoding="utf-8"))
        items = payload if isinstance(payload, list) else payload.get("questions", payload)
        assert len(items) == 32
        matched = [
            (it.get("id") or "").upper()
            for it in items
            if xagg._is_reporting_speed_comparison(it["question"].lower())
        ]
        assert matched == ["M7"], f"expected only M7, got {matched}"


def test_render_time_bucketed_mean_handles_no_usable_rows():
    rendered = "\n".join(xagg.render_time_bucketed_mean({
        "kind": "time_bucketed_mean", "buckets": [], "missing_timestamp_count": 4,
    }))
    assert "cannot be computed" in rendered


# [Gold-QA fix — Module 13, question M2]

async def test_m2_station_type_question_is_an_honest_unsupported_not_a_flat_count(monkeypatch):
    """No station-type dimension exists anywhere in this data model — must
    say so plainly rather than silently answering a per-station case count
    (a different, easier question than the one actually asked). Uses a
    client that fails on any call to prove this never reaches the DB."""
    monkeypatch.setattr(xagg, "age_client", _RaisingAgeClient())

    result = await xagg.run_aggregate(
        "Is caseload growing faster at our general-purpose stations, or at the handful "
        "set up for one specific type of crime?",
        None, gateway=None, user_role="supervisor",
    )

    assert result["kind"] == "unsupported_aggregate"
    assert result["message"] == xagg._UNSUPPORTED_STATION_TYPE


# [Gold-QA fix — Module 10.1 / Module 13, question A1]

async def test_gender_breakdown_counts_accused_edges_not_distinct_persons(monkeypatch):
    """The exact A1 bug: a recidivist accused in two separate FIRs (same
    entity_id, two accused edges) must be counted TWICE here, matching the
    gold answer's own edge-level derivation (94 total, not 92) — see
    evaluation/GROUND_TRUTH_NOTES.md §4. The previous version keyed a dict
    by entity_id and silently collapsed this down to one entry."""
    rows = [
        _accused_row("P-RECIDIVIST", "CASE-001", gender="male"),
        _accused_row("P-RECIDIVIST", "CASE-002", gender="male"),  # same person, second FIR
        _accused_row("P-002", "CASE-003", gender="male"),
        _accused_row("P-003", "CASE-004", gender="female"),
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "what is the male to female ratio among named accused", None, gateway=None, user_role="supervisor",
    )

    assert result["kind"] == "gender_breakdown"
    assert result["unsupported"] is False
    counts = {c["key"]: c["count"] for c in result["counts"]}
    assert counts == {"male": 3, "female": 1}
    assert result["total_accused"] == 4


# ── [Gold-QA fix — CR7, Module 14] criminal-record × court-outcome cross-check ──

class _QueryAwareAgeClient:
    """Returns different rows depending on which record_type the Cypher asks
    for — the CR7 aggregate issues two distinct reads (criminal_record, then
    chalaan_outcome)."""
    def __init__(self, criminal_rows, court_rows):
        self.criminal_rows = criminal_rows
        self.court_rows = court_rows

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        if "criminal_record" in cypher_query:
            return self.criminal_rows
        if "chalaan_outcome" in cypher_query:
            return self.court_rows
        return []


def test_fir_key_normalizes_differently_formatted_refs():
    assert xagg._fir_key("FIR 891/24, PS Jhang Road") == "891-24"
    assert xagg._fir_key("psrms/fir/fir-891-24#structured") == "891-24"
    assert xagg._fir_key("FIR 891/2024") == "891-24"
    assert xagg._fir_key("no fir number here") is None


def test_conviction_is_settled():
    assert xagg._conviction_is_settled("Convicted, on bail pending appeal")
    assert xagg._conviction_is_settled("سزا یافتہ")
    assert not xagg._conviction_is_settled("Under trial")
    assert not xagg._conviction_is_settled(None)


async def test_cr7_status_breakdown_and_consistent_crosscheck(monkeypatch):
    criminal = [
        {"status": "Under trial", "case_ref": "FIR 100/26", "subject": "A"},
        {"status": "Under trial", "case_ref": "FIR 101/26", "subject": "B"},
        {"status": "Convicted, on bail pending appeal",
         "case_ref": "FIR 891/24, PS Jhang Road", "subject": "شہزیب عرف شابی"},
    ]
    court = [
        {"doc_id": "psrms/fir/fir-891-24#structured",
         "outcome": "5 سال قید بامشقت زیر دفعہ 392 ت.پ سزایاب",
         "detail": "سیشن کورٹ فیصل آباد"},
    ]
    monkeypatch.setattr(xagg, "age_client", _QueryAwareAgeClient(criminal, court))

    result = await xagg._criminal_record_court_crosscheck()
    assert result["kind"] == "criminal_record_court_crosscheck"
    assert result["total_records"] == 3
    assert result["settled_count"] == 1
    assert result["in_progress_count"] == 2
    counts = {s["status"]: s["count"] for s in result["status_breakdown"]}
    assert counts["Under trial"] == 2
    # the one case with both records is cross-checked and consistent
    assert len(result["crosschecks"]) == 1
    cc = result["crosschecks"][0]
    assert cc["fir"] == "891-24"
    assert cc["consistent"] is True


async def test_cr7_detects_inconsistency(monkeypatch):
    # criminal record says settled/convicted, court outcome says still pending
    criminal = [
        {"status": "Convicted", "case_ref": "FIR 891/24", "subject": "X"},
    ]
    court = [
        {"doc_id": "fir-891-24", "outcome": "زیرِ سماعت", "detail": None},
    ]
    monkeypatch.setattr(xagg, "age_client", _QueryAwareAgeClient(criminal, court))
    result = await xagg._criminal_record_court_crosscheck()
    assert len(result["crosschecks"]) == 1
    assert result["crosschecks"][0]["consistent"] is False


async def test_cr7_no_crosscheck_when_no_shared_case(monkeypatch):
    criminal = [{"status": "Under trial", "case_ref": "FIR 100/26", "subject": "A"}]
    court = [{"doc_id": "fir-891-24", "outcome": "سزایاب", "detail": None}]
    monkeypatch.setattr(xagg, "age_client", _QueryAwareAgeClient(criminal, court))
    result = await xagg._criminal_record_court_crosscheck()
    assert result["crosschecks"] == []


def test_cr7_renderer_settled_singular_and_consistency():
    result = {
        "total_records": 33, "settled_count": 1, "in_progress_count": 32,
        "status_breakdown": [{"status": "Under trial", "count": 30},
                             {"status": "Convicted, on bail pending appeal", "count": 1}],
        "crosschecks": [{"fir": "891-24", "subject": "شہزیب عرف شابی",
                         "criminal_status": "Convicted, on bail pending appeal",
                         "court_outcome": "5 سال قید بامشقت", "consistent": True}],
    }
    text = "\n".join(xagg.render_criminal_record_crosscheck(result))
    assert "33 criminal records" in text
    assert "1 has reached a verdict" in text  # singular
    assert "consistent" in text
    assert "891-24" in text


# ── [Gold-QA fix — CR6/CR8, Module 15] cross-record field-consistency ──

class _RecordTypeAwareAgeClient:
    """Routes each read by what the Cypher targets (count vs. edge query)."""
    def __init__(self, total_n, edge_rows):
        self.total_n = total_n
        self.edge_rows = edge_rows

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        if "count(r)" in cypher_query:
            return [{"n": self.total_n}]
        return self.edge_rows


async def test_cr6_cms_fir_linkage_all_linked(monkeypatch):
    edges = [
        {"tag": "CMS-KHI-2026-0417", "case_id": "fir-417-26", "cnic": "x"},
        {"tag": "CMS-ISB-2026-0341", "case_id": "fir-64-26", "cnic": "y"},
    ]
    monkeypatch.setattr(xagg, "age_client", _RecordTypeAwareAgeClient(2, edges))
    r = await xagg._cms_fir_linkage()
    assert r["kind"] == "cms_fir_linkage"
    assert r["total_complaints"] == 2
    assert r["linked_count"] == 2
    assert r["unlinked_count"] == 0
    text = "\n".join(xagg.render_cms_fir_linkage(r))
    assert "linked in practice" in text
    assert "fir-417-26" in text


async def test_cr6_cms_partial_link(monkeypatch):
    edges = [{"tag": "CMS-KHI-2026-0417", "case_id": "fir-417-26", "cnic": "x"}]
    monkeypatch.setattr(xagg, "age_client", _RecordTypeAwareAgeClient(3, edges))
    r = await xagg._cms_fir_linkage()
    assert r["total_complaints"] == 3
    assert r["linked_count"] == 1
    assert r["unlinked_count"] == 2


async def test_cr8_dv_report_fir_match(monkeypatch):
    edges = [
        {"rid": "pkm_application:pkm-app-c9-02", "case_id": "fir-97-26"},
        {"rid": "pkm_application:PKMAPP-C316-2", "case_id": "fir-416-26"},
        {"rid": "pkm_application:PKMAPP-C326-1", "case_id": "fir-426-26"},
        {"rid": "pkm_application:PKMAPP-C336-2", "case_id": "fir-436-26"},
    ]
    monkeypatch.setattr(xagg, "age_client", _RecordTypeAwareAgeClient(8, edges))
    r = await xagg._dv_report_fir_match()
    assert r["kind"] == "dv_report_fir_match"
    assert r["total_reports"] == 8
    assert r["confirmed_count"] == 4
    assert r["unconfirmed_count"] == 4
    text = "\n".join(xagg.render_dv_report_fir_match(r))
    assert "confirmed by the case records" in text
    assert "fir-97-26" in text


# ── [Gold-QA fix — G2/G5, Module 15] completeness + weapon-compliance scans ──

class _GatewayCases:
    def __init__(self, cases): self._cases = cases
    async def get_cases(self, user_id=None, user_role=None): return self._cases


async def test_g2_completeness_excludes_test_rows(monkeypatch):
    cases = [
        {"case_id": "fir-1-26", "incident_date": None, "investigation_status": "open", "fir_number": "1/26"},
        {"case_id": "fir-2-26", "incident_date": "2026-01-01", "investigation_status": None, "fir_number": "2/26"},
        {"case_id": "CASE-TEST-abc", "incident_date": None, "investigation_status": None, "fir_number": None},
    ]
    r = await xagg._case_completeness_scan(_GatewayCases(cases))
    assert r["kind"] == "case_completeness_scan"
    assert r["total_cases"] == 2  # test row excluded
    assert r["missing_incident_date"] == ["fir-1-26"]
    assert r["missing_status"] == ["fir-2-26"]
    text = "\n".join(xagg.render_case_completeness_scan(r))
    assert "1 of 2 FIRs record no incident date" in text


async def test_g5_weapon_compliance_counts_unlicensed(monkeypatch):
    rows = (
        [{"status": "بغیر لائسنس"}] * 30
        + [{"status": None}] * 2
    )
    class _AC:
        async def execute_cypher(self, q, params=None, columns=("result",), graph=None):
            return rows
    monkeypatch.setattr(xagg, "age_client", _AC())
    r = await xagg._weapon_compliance_scan()
    assert r["kind"] == "weapon_compliance_scan"
    assert r["total_weapons"] == 32
    assert r["unlicensed_count"] == 30
    assert r["no_status_count"] == 2
    text = "\n".join(xagg.render_weapon_compliance_scan(r))
    assert "30 of 32" in text
    assert "94%" in text


# ── [Gold-QA fix — G3, Module 15/16] court-readiness completeness scan ──

async def test_g3_court_readiness_combines_three_signals(monkeypatch):
    # Graph reads: accused count, accused-with-relationship count, weapon rows.
    class _AC:
        async def execute_cypher(self, q, params=None, columns=("result",), graph=None):
            if "role = 'accused'" in q and "RELATED_TO" not in q:
                return [{"n": 93}]
            if "RELATED_TO" in q:
                return [{"n": 12}]
            if "Weapon" in q:
                return [{"status": "بغیر لائسنس"}] * 30 + [{"status": None}] * 2
            if "zimni" in q:
                return []
            return []
    monkeypatch.setattr(xagg, "age_client", _AC())
    cases = [{"case_id": f"fir-{i}-26", "incident_date": None if i < 9 else "2026-01-01",
              "investigation_status": "open", "fir_number": f"{i}/26"} for i in range(73)]
    r = await xagg._court_readiness_scan(_GatewayCases(cases))
    assert r["kind"] == "court_readiness_scan"
    assert r["accused_no_relationship"] == 81       # 93 - 12
    assert r["weapons_no_licence_status"] == 2
    assert r["firs_no_incident_date"] == 9
    text = "\n".join(xagg.render_court_readiness_scan(r))
    assert "81 of 93" in text
    assert "2 of 32" in text
    assert "9 FIRs record no incident date" in text


# ── Gold-QA fix — M4 vs. G3 keyword collision (Module 18 rerun) ─────────────
#
# Live regression: _COURT_READINESS_KEYWORDS used to include the bare Urdu
# word "عدالت" ("court") on its own. M4 ("...وہ مقدمے عدالت میں کہاں تک
# پہنچے" — how far cases have progressed IN COURT, an unrelated
# statute×court-stage comparison question) contains that word incidentally
# and got hijacked into this court-file-readiness scan instead of its own
# route. Fixed by keeping only the actual handover/readiness-framing
# phrases (G3's own shape: "عدالت کو حوالگی", "کیس فائل تیار", etc.), never
# the bare word alone.

def test_m4_does_not_collide_with_court_readiness_keywords():
    m4 = (
        "ایک طرف یہ دیکھیں کہ لوگوں پر کن دفعات میں مقدمے بن رہے ہیں، اور "
        "دوسری طرف یہ کہ وہ مقدمے عدالت میں کہاں تک پہنچے — کیا دونوں سے "
        "کیس لوڈ کی سنگینی کا ایک ہی اندازہ ہوتا ہے؟"
    )
    assert not xagg._matches_any(m4.lower(), xagg._COURT_READINESS_KEYWORDS)


def test_g3_still_matches_court_readiness_keywords_after_narrowing():
    g3 = (
        "آپ عدالت کو حوالگی کے لیے ایک کیس فائل تیار کر رہے ہیں — ڈیٹا کی "
        "روشنی میں، کن چیزوں کے نامکمل قرار پانے کا سب سے زیادہ امکان ہے؟"
    )
    assert xagg._matches_any(g3.lower(), xagg._COURT_READINESS_KEYWORDS)


# ── Gold-QA fix — Module 23, question M5 ───────────────────────────────────
#
# M5: "ہتھیار عام طور پر کس نوعیت کے مقدمات میں سامنے آتے ہیں، اور کیا 2024
# کے مقابلے میں اب یہ نوعیت بدل گئی ہے؟" — in what KINDS of cases do weapons
# turn up, and has that changed since 2024?
#
# Before this module M5 matched `_TIME_COMPARISON_KEYWORDS` ("کے مقابلے میں"
# is a literal entry there) and was answered by `_statute_mix_by_year()` —
# a per-year statute ranking over ALL cases, with no weapon dimension at all,
# so it could never say what a weapon charge pairs with. The tests below pin
# both halves: the new aggregate's arithmetic, and the dispatch boundary that
# keeps M1, G5, CP1 and the bare weapon-recurrence path exactly where they
# were.

# One weapon-bearing case per year pair, deliberately mirroring the real
# corpus's shape in miniature: two 2024 armed-robbery cases, one 2026
# narcotics case carrying the same weapons-law section, one 2026 case with a
# non-firearm weapon and no weapons-law charge, one weapon case with no
# resolvable incident date, and one non-weapon case that must not leak in.
_M5_YEAR_ROWS = [
    {"incident_date": "2024-09-22", "case_id": "CASE-A"},
    {"incident_date": "2024-09-25", "case_id": "CASE-B"},
    {"incident_date": "2026-02-14", "case_id": "CASE-C"},
    {"incident_date": "2026-03-11", "case_id": "CASE-D"},
    # CASE-E deliberately absent — a weapon case with no OCCURRED_ON edge.
    {"incident_date": "2026-04-01", "case_id": "CASE-F"},
]
_M5_WEAPON_ROWS = [
    {"weapon_name": "30 بور پستول", "case_id": "CASE-A"},
    # Same weapon TYPE as CASE-A's, differing only by the ammunition-count
    # suffix — must fold together via _normalize_weapon_type().
    {"weapon_name": "30 بور پستول بمعہ 3 گولیاں", "case_id": "CASE-B"},
    {"weapon_name": "30 بور پستول", "case_id": "CASE-C"},
    {"weapon_name": "عام لکڑی کی چھڑی", "case_id": "CASE-D"},
    {"weapon_name": "30 بور پستول", "case_id": "CASE-E"},
]
_M5_STATUTE_ROWS = [
    {"act": "Arms Ordinance 1965", "section_code": "13", "case_id": "CASE-A"},
    {"act": "PPC", "section_code": "34", "case_id": "CASE-A"},
    {"act": "PPC", "section_code": "392", "case_id": "CASE-A"},
    {"act": "Arms Ordinance 1965", "section_code": "13", "case_id": "CASE-B"},
    {"act": "PPC", "section_code": "34", "case_id": "CASE-B"},
    {"act": "PPC", "section_code": "392", "case_id": "CASE-B"},
    {"act": "Arms Ordinance 1965", "section_code": "13", "case_id": "CASE-C"},
    {"act": "CNSA 1997", "section_code": "9(c)", "case_id": "CASE-C"},
    {"act": "PPC", "section_code": "328A", "case_id": "CASE-D"},
    # CASE-F has statutes and a year but NO weapon — must not appear anywhere.
    {"act": "PPC", "section_code": "302", "case_id": "CASE-F"},
]

_M5_GOLD_TEXT = (
    "ہتھیار عام طور پر کس نوعیت کے مقدمات میں سامنے آتے ہیں، اور کیا 2024 کے "
    "مقابلے میں اب یہ نوعیت بدل گئی ہے؟"
)


def _m5_age_client():
    return _SequentialAgeClient([_M5_YEAR_ROWS, _M5_WEAPON_ROWS, _M5_STATUTE_ROWS])


async def test_m5_gold_text_routes_to_the_weapon_statute_cooccurrence_aggregate(monkeypatch):
    """The regression this module exists for: M5's LITERAL gold text must no
    longer be answered by `_statute_mix_by_year()`."""
    monkeypatch.setattr(xagg, "age_client", _m5_age_client())

    result = await xagg.run_aggregate(
        _M5_GOLD_TEXT, None, gateway=FakeGateway([]), user_role="supervisor",
    )

    assert result["kind"] == "weapon_statute_cooccurrence"
    assert result["kind"] != "time_bucketed_breakdown"


async def test_m5_aggregate_counts_statutes_per_year_for_weapon_cases_only(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", _m5_age_client())

    result = await xagg.run_aggregate(
        _M5_GOLD_TEXT, None, gateway=FakeGateway([]), user_role="supervisor",
    )

    by_year = {b["year"]: b for b in result["buckets"]}
    assert sorted(by_year) == [2024, 2026]

    assert by_year[2024]["case_count"] == 2
    assert {s["key"]: s["count"] for s in by_year[2024]["statutes"]} == {
        "Arms Ordinance 1965 §13": 2, "PPC §34": 2, "PPC §392": 2,
    }
    # CASE-F carries PPC §302 in 2026 but has no Weapon — it must not appear.
    assert {s["key"]: s["count"] for s in by_year[2026]["statutes"]} == {
        "Arms Ordinance 1965 §13": 1, "CNSA 1997 §9(c)": 1, "PPC §328A": 1,
    }
    # One weapon case has no resolvable incident year: excluded from every
    # bucket, but still counted in the stated total.
    assert result["total_weapon_cases"] == 5
    assert result["undated_weapon_cases"] == 1


async def test_m5_cooccurrence_view_is_scoped_to_cases_carrying_a_weapons_law_charge(monkeypatch):
    """The narrower view M5's answer actually turns on: what does the WEAPON
    CHARGE itself pair with? CASE-D has a weapon but no Arms-Ordinance
    section, so its PPC §328A must not enter 2026's co-occurrence set."""
    monkeypatch.setattr(xagg, "age_client", _m5_age_client())

    result = await xagg.run_aggregate(
        _M5_GOLD_TEXT, None, gateway=FakeGateway([]), user_role="supervisor",
    )

    by_year = {b["year"]: b for b in result["buckets"]}
    assert by_year[2024]["weapon_charge_case_count"] == 2
    assert {c["key"]: c["count"] for c in by_year[2024]["cooccurring"]} == {
        "PPC §34": 2, "PPC §392": 2,
    }
    assert by_year[2026]["weapon_charge_case_count"] == 1
    assert {c["key"]: c["count"] for c in by_year[2026]["cooccurring"]} == {
        "CNSA 1997 §9(c)": 1,
    }


async def test_m5_weapon_type_variants_fold_into_one_pairing(monkeypatch):
    """The ammunition-count suffix variant is the same weapon
    type — the same normalization `_top_recurring_weapon_types()` already
    applies, reused here rather than reinvented."""
    monkeypatch.setattr(xagg, "age_client", _m5_age_client())

    result = await xagg.run_aggregate(
        _M5_GOLD_TEXT, None, gateway=FakeGateway([]), user_role="supervisor",
    )

    by_year = {b["year"]: b for b in result["buckets"]}
    assert {w["key"]: w["count"] for w in by_year[2024]["weapon_types"]} == {
        "30 بور پستول": 2,
    }
    pairs_2024 = {(p["weapon_type"], p["statute"]): p["count"] for p in by_year[2024]["pairs"]}
    assert pairs_2024[("30 بور پستول", "PPC §392")] == 2


async def test_m5_renderer_states_the_widening_it_actually_measured(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", _m5_age_client())

    result = await xagg.run_aggregate(
        _M5_GOLD_TEXT, None, gateway=FakeGateway([]), user_role="supervisor",
    )
    rendered = "\n".join(xagg.render_weapon_statute_cooccurrence(result))

    assert "**2024**" in rendered and "**2026**" in rendered
    assert "Arms Ordinance 1965 §13: 2" in rendered
    assert "CNSA 1997 §9(c)" in rendered
    # Derived from the buckets, never a fixed narrative.
    assert "Change 2024 to 2026" in rendered
    assert "1 case(s) with a recovered weapon are excluded" in rendered


def test_m5_renderer_does_not_claim_a_change_that_did_not_happen():
    """Guards the derived-not-hardcoded property directly: identical
    co-occurrence sets across years must render as UNCHANGED."""
    agg = {
        "kind": "weapon_statute_cooccurrence",
        "total_weapon_cases": 2, "undated_weapon_cases": 0,
        "buckets": [
            {"year": 2024, "case_count": 1, "statutes": [{"key": "PPC §392", "count": 1}],
             "weapon_types": [], "weapon_charge_case_count": 1,
             "cooccurring": [{"key": "PPC §392", "count": 1}], "pairs": []},
            {"year": 2026, "case_count": 1, "statutes": [{"key": "PPC §392", "count": 1}],
             "weapon_types": [], "weapon_charge_case_count": 1,
             "cooccurring": [{"key": "PPC §392", "count": 1}], "pairs": []},
        ],
    }
    rendered = "\n".join(xagg.render_weapon_statute_cooccurrence(agg))
    assert "unchanged between 2024 and 2026" in rendered
    assert "Change 2024 to 2026" not in rendered


class TestWeaponStatuteCooccurrenceBoundary:
    """
    [Gold-QA fix — Module 23] The three-signal AND, tested at its edges.
    Every neighbour named here is a family this dispatch chain already
    serves, and each one was a real collision site for an earlier module —
    see `_is_weapon_statute_cooccurrence()`'s own comment.
    """

    def test_m5_gold_text_matches(self):
        assert xagg._is_weapon_statute_cooccurrence(_M5_GOLD_TEXT.lower())

    @pytest.mark.parametrize("paraphrase", [
        # The required non-gold paraphrase: plain English, no Urdu statute
        # vocabulary, no shared keyword with M5's literal phrasing.
        "Are guns turning up in different types of cases than they used to?",
        "Have the kinds of offences where firearms are recovered shifted since 2024?",
        "Is the type of crime we recover weapons in changing?",
    ])
    def test_non_gold_paraphrases_match(self, paraphrase):
        assert xagg._is_weapon_statute_cooccurrence(paraphrase.lower())

    @pytest.mark.parametrize("other", [
        # M1 — case-type + change, but no weapon term. Must stay with
        # _statute_mix_by_year().
        "What kinds of cases are we dealing with now compared to a couple of years back?",
        # G5 — weapon + compliance, no case-type/change signal. Scores 1.0
        # today; must stay with _weapon_compliance_scan().
        "Baramad shuda hathiyaron ki record keeping ko dekhte hue, kya koi "
        "aisi baat hai jo compliance ke lihaz se flag karne layak ho?",
        # Bare weapon recurrence, and Module 1c's district+weapon paths.
        "Which weapons show up across more than one case?",
        "Which district recovers the most weapons?",
        "Which district recovers the most weapons relative to its case load?",
        # M7's reporting-speed shape.
        "kya log 2026 mein waqiaat ki police ko itni hi jaldi ittila de rahe "
        "hain jitni 2024 mein dete the?",
    ])
    def test_neighbouring_families_are_not_captured(self, other):
        assert not xagg._is_weapon_statute_cooccurrence(other.lower())

    def test_matches_m5_and_no_other_gold_question(self):
        """The all-32 negative control, same discipline (and same rationale)
        as `TestReportingSpeedComparisonBoundary`'s. Reads whichever copy of
        the gold set this checkout actually has — the bare file at the repo
        root is not tracked, but `evaluation/`'s answered copy is."""
        import json
        from pathlib import Path

        root = Path(__file__).resolve().parent.parent
        for candidate in (
            root / "Gold_QA_Dataset_Final32.json",
            root / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json",
        ):
            if candidate.exists():
                gold_path = candidate
                break
        else:
            pytest.skip("no Gold-32 dataset present in this checkout")

        payload = json.loads(gold_path.read_text(encoding="utf-8"))
        items = payload if isinstance(payload, list) else payload.get("questions", payload)
        matched = [
            (it.get("id") or "").upper()
            for it in items
            if xagg._is_weapon_statute_cooccurrence(it["question"].lower())
        ]
        assert matched == ["M5"], f"expected only M5, got {matched}"


async def test_m1_still_reaches_the_statute_mix_aggregate_after_module_23(monkeypatch):
    """Negative control, end to end rather than at the predicate: M1's own
    gold text must still be answered by `_statute_mix_by_year()`."""
    year_rows = [{"incident_date": "2024-02-01", "case_id": "CASE-1"}]
    cases = [{"case_id": "CASE-1", "crime_category": "PPC"}]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(year_rows))

    result = await xagg.run_aggregate(
        "What kinds of cases are we dealing with now compared to a couple of years back?",
        None, gateway=FakeGateway(cases), user_role="supervisor",
    )

    assert result["kind"] == "time_bucketed_breakdown"
    assert result["dimension"] == "statute_by_year"


async def test_g5_still_reaches_the_weapon_compliance_scan_after_module_23(monkeypatch):
    """Negative control: G5 scores 1.0 today and shares this module's whole
    keyword space."""
    rows = [{"status": "بغیر لائسنس"}, {"status": "لائسنس یافتہ"}]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        "Baramad shuda hathiyaron ki record keeping ko dekhte hue, kya koi "
        "aisi baat hai jo compliance ke lihaz se flag karne layak ho?",
        None, gateway=FakeGateway([]), user_role="supervisor",
    )

    assert result["kind"] == "weapon_compliance_scan"


async def test_m5_jurisdiction_case_ids_narrow_every_one_of_the_three_reads(monkeypatch):
    """All three graph reads must honour the jurisdiction allow-list — a
    filter applied to two of three would silently over-count."""
    captured = []

    class _Capturing:
        async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
            captured.append((cypher_query, params))
            return []

    monkeypatch.setattr(xagg, "age_client", _Capturing())

    await xagg.run_aggregate(
        _M5_GOLD_TEXT, None, gateway=FakeGateway([]), user_role="supervisor",
        jurisdiction_case_ids=["CASE-A"],
    )

    assert len(captured) == 3
    for query, params in captured:
        assert "$case_ids" in query
        assert params == {"case_ids": ["CASE-A"]}
# ── [Gold-QA fix — CR4, Module 28] weapon -> FIR -> accused -> status chain ──

class _WeaponChainAgeClient:
    """Routes the three reads _weapon_evidence_chain() makes, by their own
    distinctive Cypher fragments — same fake style as the G3 test above."""

    def __init__(self, weapons, statuses, criminal_records):
        self.weapons, self.statuses, self.criminal_records = weapons, statuses, criminal_records

    async def execute_cypher(self, q, params=None, columns=("result",), graph=None):
        if "OWNS" in q:
            return self.weapons
        if "role = 'accused'" in q:
            return self.statuses
        if "criminal_record" in q:
            return self.criminal_records
        raise AssertionError(f"unexpected cypher: {q}")


def _weapon_chain_fixture():
    """Mirrors the shape of the real live graph (captured 2026-09-08): the
    gold-answer chain for FIR 891/24, a second chain for the same person on a
    newer FIR, and a crime-scene weapon with no `recovered_from` match."""
    weapons = [
        {"weapon_id": "WEAPON-WR-C2-1-fir-214-26", "weapon": "30 بور پستول بمعہ 3 گولیاں",
         "license_status": "بغیر لائسنس", "case_id": "fir-214-26",
         "person": "شہزیب عرف شابی", "person_id": "PERSON-685fc54914"},
        {"weapon_id": "WEAPON-WR-C1-1-fir-891-24", "weapon": "30 بور پستول",
         "license_status": "بغیر لائسنس", "case_id": "fir-891-24",
         "person": "شہزیب عرف شابی", "person_id": "PERSON-685fc54914"},
        {"weapon_id": "WEAPON-WR-117-1-fir-117-26", "weapon": "عام لکڑی کی چھڑی، ایک عدد",
         "license_status": None, "case_id": "fir-117-26", "person": None, "person_id": None},
    ]
    statuses = [
        {"person_id": "PERSON-685fc54914", "case_id": "fir-214-26", "arrest_status": "گرفتار"},
        {"person_id": "PERSON-685fc54914", "case_id": "fir-891-24",
         "arrest_status": "گرفتار، بعد ازاں سزا یافتہ"},
    ]
    criminal_records = [
        {"subject": "شہزیب عرف شابی", "case_ref": "FIR 891/24, PS Jhang Road Faisalabad",
         "conviction_status": "Convicted, on bail pending appeal"},
    ]
    return weapons, statuses, criminal_records


async def test_cr4_weapon_evidence_chain_returns_gold_chain(monkeypatch):
    """Regression test pinned to CR4's literal gold ANSWER: the 30-bore
    pistol logged in FIR 891/24, recovered from شہزیب عرف شابی, whose
    recorded status on that case is گرفتار، بعد ازاں سزا یافتہ."""
    monkeypatch.setattr(xagg, "age_client", _WeaponChainAgeClient(*_weapon_chain_fixture()))
    r = await xagg._weapon_evidence_chain()
    assert r["kind"] == "weapon_evidence_chain"
    assert r["total_weapons"] == 3
    assert r["traceable_count"] == 2
    assert r["untraceable_count"] == 1

    # The chain whose status runs all the way to a decided outcome is the
    # worked example — exactly the one gold picks.
    example = r["example"]
    assert example["fir"] == "891-24"
    assert example["recovered_from"] == "شہزیب عرف شابی"
    assert example["status"] == "گرفتار، بعد ازاں سزا یافتہ"
    assert example["conviction_status"] == "Convicted, on bail pending appeal"

    text = "\n".join(xagg.render_weapon_evidence_chain(r))
    assert "FIR 891/24" in text          # rendered the way the source records write it
    assert "شہزیب عرف شابی" in text
    assert "گرفتار، بعد ازاں سزا یافتہ" in text
    # Gold's own hedge must be stated, not hidden.
    assert "not" in text.lower() and "enforced database key" in text.lower()
    assert "weapon -> FIR number -> accused" in text
    # Weapons with no attributable owner are disclosed, not dropped.
    assert "1 of the 3 record no person at all" in text


async def test_cr4_chain_reports_honestly_when_nothing_is_attributable(monkeypatch):
    weapons = [{"weapon_id": "W1", "weapon": "چھڑی", "license_status": None,
                "case_id": "fir-117-26", "person": None, "person_id": None}]
    monkeypatch.setattr(xagg, "age_client", _WeaponChainAgeClient(weapons, [], []))
    r = await xagg._weapon_evidence_chain()
    assert r["traceable_count"] == 0
    text = "\n".join(xagg.render_weapon_evidence_chain(r))
    assert "none records who it was recovered from" in text


async def test_cr4_dispatches_from_run_aggregate(monkeypatch):
    """The dispatch conjunction (weapon term AND attribution term) reaches
    the new aggregate for CR4's literal gold text."""
    monkeypatch.setattr(xagg, "age_client", _WeaponChainAgeClient(*_weapon_chain_fixture()))
    r = await xagg.run_aggregate(
        "If we've got a weapon logged as evidence, can we tell who it was "
        "taken off and what happened to them?",
        None, FakeGateway([]), user_role="platform-admin",
    )
    assert r["kind"] == "weapon_evidence_chain"


async def test_g5_still_reaches_the_compliance_scan_not_the_chain(monkeypatch):
    """Regression guard named in Module 28's brief: G5 (currently 1.0) lives
    in the same weapon keyword space and must keep its compliance answer."""
    class _AC:
        async def execute_cypher(self, q, params=None, columns=("result",), graph=None):
            return [{"status": "بغیر لائسنس"}] * 30 + [{"status": None}] * 2
    monkeypatch.setattr(xagg, "age_client", _AC())
    r = await xagg.run_aggregate(
        "Baramad shuda hathiyaron ki record keeping ko dekhte hue, kya koi "
        "aisi baat hai jo compliance ke lihaz se flag karne layak ho?",
        None, FakeGateway([]), user_role="platform-admin",
    )
    assert r["kind"] == "weapon_compliance_scan"


def test_cr4_attribution_terms_negative_control_over_gold32():
    """The xagg dispatch half of the all-32 negative control: the weapon
    term AND attribution term conjunction must select CR4 alone, and G5's
    own weapon-AND-compliance conjunction must still select G5 alone."""
    import json
    import os

    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "evaluation",
        "Gold_QA_Dataset_Final32_With_Answers.json",
    )
    gold = json.load(open(path, encoding="utf-8"))
    assert len(gold) == 32

    chain_ids, compliance_ids = [], []
    for item in gold:
        ql = item["question"].lower()
        if xagg._matches_any(ql, xagg._WEAPON_TERMS):
            if xagg._matches_any(ql, xagg._WEAPON_ATTRIBUTION_TERMS):
                chain_ids.append(item["id"])
            if xagg._matches_any(ql, xagg._COMPLIANCE_TERMS):
                compliance_ids.append(item["id"])

    assert chain_ids == ["CR4"], f"weapon-chain dispatch also selected {chain_ids}"
    assert compliance_ids == ["G5"], f"G5's compliance dispatch changed: {compliance_ids}"


# ── [Gold-QA fix — Module 24, question M4] statute × court-stage join ──

_M4_GOLD_TEXT = (
    "ایک طرف یہ دیکھیں کہ لوگوں پر کن دفعات میں مقدمے بن رہے ہیں، اور دوسری "
    "طرف یہ کہ وہ مقدمے عدالت میں کہاں تک پہنچے — کیا دونوں سے کیس لوڈ کی "
    "سنگینی کا ایک ہی اندازہ ہوتا ہے؟"
)

_G3_GOLD_TEXT = (
    "آپ عدالت کو حوالگی کے لیے ایک کیس فائل تیار کر رہے ہیں — ڈیٹا کی روشنی "
    "میں، کن چیزوں کے نامکمل قرار پانے کا سب سے زیادہ امکان ہے؟"
)

_CR7_GOLD_TEXT = (
    "کرمنل ریکارڈ سسٹم میں کتنے کیس مکمل ہو چکے ہیں اور کتنے ابھی زیرِ کارروائی "
    "ہیں — اور جہاں کسی ایک کیس کا الگ عدالتی ریکارڈ بھی موجود ہے، کیا دونوں "
    "ایک دوسرے سے مطابقت رکھتے ہیں؟"
)


class _M4AgeClient:
    """Routes each of the four reads `_statute_court_stage_join()` issues by
    what its Cypher targets. Kept deliberately literal rather than reusing
    `_QueryAwareAgeClient` — this aggregate reads sections and Cases too, and
    a fake that silently returned [] for an unrecognized query would let a
    dropped read pass as an empty corpus."""

    def __init__(self, sections, criminal, court, cases):
        self.sections, self.criminal, self.court, self.cases = (
            sections, criminal, court, cases,
        )
        self.queries = []

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        self.queries.append((cypher_query, params))
        if "fir_section" in cypher_query:
            return self.sections
        if "criminal_record" in cypher_query:
            return self.criminal
        if "chalaan_outcome" in cypher_query:
            return self.court
        if "MATCH (c:Case)" in cypher_query:
            return self.cases
        raise AssertionError(f"unexpected read: {cypher_query}")


def _m4_fixture(criminal=None):
    """A miniature of the real corpus: three charged cases, and a court side
    where almost nothing has been decided."""
    sections = [
        {"act": "PPC", "section_code": "302", "case_id": "fir-77-26"},
        {"act": "PPC", "section_code": "34", "case_id": "fir-77-26"},
        # the same case charged twice under one section must count once
        {"act": "PPC", "section_code": "34", "case_id": "fir-77-26"},
        {"act": "PPC", "section_code": "34", "case_id": "fir-64-26"},
        {"act": "Arms Ordinance 1965", "section_code": "13", "case_id": "fir-891-24"},
        {"act": "PPC", "section_code": "392", "case_id": "fir-891-24"},
    ]
    if criminal is None:
        criminal = [
            {"status": "Under trial", "case_ref": "FIR 77/26", "subject": "A"},
            {"status": "Under trial", "case_ref": None, "subject": "B"},
            {"status": "Convicted, on bail pending appeal",
             "case_ref": "FIR 891/24, PS Jhang Road", "subject": "C"},
        ]
    cases = [
        {"case_id": "fir-77-26", "fir_number": None},
        {"case_id": "fir-64-26", "fir_number": None},
        {"case_id": "fir-891-24", "fir_number": None},
    ]
    return _M4AgeClient(sections, criminal, [], cases)


async def test_m4_aggregate_reports_both_halves_and_an_agreement_verdict(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", _m4_fixture())

    result = await xagg._statute_court_stage_join()

    assert result["kind"] == "statute_court_stage_join"
    # Half A — section-level, counted in CASES (PPC §34 is on two cases, and
    # the duplicate row on fir-77-26 must not inflate it to three).
    assert result["charged_case_count"] == 3
    counts = {s["key"]: s["case_count"] for s in result["statutes"]}
    assert counts == {
        "PPC §34": 2, "PPC §302": 1, "PPC §392": 1, "Arms Ordinance 1965 §13": 1,
    }
    # Half B — CR7's own reader, untouched.
    assert result["court"]["kind"] == "criminal_record_court_crosscheck"
    assert result["court"]["total_records"] == 3
    assert result["court"]["settled_count"] == 1
    assert result["court"]["in_progress_count"] == 2
    # The verdict: 1 of 3 decided is not a majority, so the two disagree.
    assert result["agree"] is False
    assert result["settled_share"] == pytest.approx(1 / 3)


async def test_m4_agreement_verdict_flips_when_the_courts_have_caught_up(monkeypatch):
    """The rule is a majority test over whatever the data says — not a
    threshold tuned to this corpus. Same fixture, a decided court side."""
    criminal = [
        {"status": "Convicted", "case_ref": "FIR 77/26", "subject": "A"},
        {"status": "Acquitted", "case_ref": None, "subject": "B"},
        {"status": "Under trial", "case_ref": "FIR 891/24", "subject": "C"},
    ]
    monkeypatch.setattr(xagg, "age_client", _m4_fixture(criminal=criminal))

    result = await xagg._statute_court_stage_join()

    assert result["court"]["settled_count"] == 2
    assert result["agree"] is True
    rendered = "\n".join(xagg.render_statute_court_stage_join(result))
    assert "Do the two agree? Yes." in rendered


async def test_m4_joins_a_criminal_record_to_the_sections_of_its_own_case(monkeypatch):
    """The join is on the FIR number, through `_fir_key()` — a criminal
    record naming no FIR (the majority of the real ones) is excluded rather
    than guessed at."""
    monkeypatch.setattr(xagg, "age_client", _m4_fixture())

    result = await xagg._statute_court_stage_join()

    assert result["joinable_record_count"] == 2
    joined = {j["fir"]: j for j in result["joined_records"]}
    assert set(joined) == {"77-26", "891-24"}
    assert joined["77-26"]["statutes"] == ["PPC §302", "PPC §34"]
    assert joined["77-26"]["settled"] is False
    assert joined["891-24"]["statutes"] == ["Arms Ordinance 1965 §13", "PPC §392"]
    assert joined["891-24"]["settled"] is True


async def test_m4_renderer_states_both_halves_and_the_disagreement(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", _m4_fixture())
    result = await xagg._statute_court_stage_join()

    rendered = "\n".join(xagg.render_statute_court_stage_join(result))

    # Half A present, section-level.
    assert "PPC §34: 2 case(s)" in rendered
    # Half B present, and rendered by CR7's own renderer (so the two can
    # never drift) — its signature sentence is the proof.
    assert "Of 3 criminal records, 2 are still in progress" in rendered
    # The comparison itself, stated rather than left to the model.
    assert "Do the two agree? No." in rendered
    assert "lag behind" in rendered


def test_m4_renderer_caps_the_section_list_without_dropping_the_tail():
    statutes = [{"key": f"PPC §{i}", "case_count": 40 - i} for i in range(20)]
    rendered = "\n".join(xagg.render_statute_court_stage_join({
        "charged_case_count": 20, "section_entry_count": 20,
        "distinct_statute_count": 20, "statutes": statutes,
        "court": {"total_records": 0, "settled_count": 0, "in_progress_count": 0,
                  "status_breakdown": [], "crosschecks": []},
        "joined_records": [], "settled_share": None, "agree": False,
    }))
    assert "PPC §14: 26 case(s)" in rendered          # the 15th, kept
    assert "PPC §15: 25 case(s)" not in rendered      # the 16th, folded
    assert "and 5 further section(s)" in rendered


async def test_m4_reports_an_empty_charging_side_instead_of_inventing_one(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", _M4AgeClient([], [], [], []))
    result = await xagg._statute_court_stage_join()
    rendered = "\n".join(xagg.render_statute_court_stage_join(result))
    assert result["charged_case_count"] == 0
    assert "No case in scope has a recorded FIR section" in rendered
    assert "no single case can be read on both sides at once" in rendered


async def test_m4_jurisdiction_case_ids_narrow_the_section_and_case_reads(monkeypatch):
    """The criminal-record read is deliberately corpus-wide (a person's
    history spans cases — CR7's own docstring), but the two case-scoped reads
    must honour the allow-list or the charging side over-counts."""
    fake = _m4_fixture()
    monkeypatch.setattr(xagg, "age_client", fake)

    await xagg._statute_court_stage_join(jurisdiction_case_ids=["fir-77-26"])

    scoped = [
        (q, p) for q, p in fake.queries
        if "fir_section" in q or "MATCH (c:Case)" in q
    ]
    assert len(scoped) == 2
    for q, p in scoped:
        assert "$case_ids" in q
        assert p["case_ids"] == ["fir-77-26"]


async def test_m4_gold_text_reaches_the_statute_court_stage_join(monkeypatch):
    """THE REGRESSION PINNED TO M4's LITERAL URDU GOLD TEXT. Before this
    module it landed on `graph_recurrence`/Person — "لوگوں" contains the
    literal `_PERSON_KEYWORDS` entry "لوگ", so the ordered dispatch handed a
    two-halves statute/court question to the repeat-accused ranking."""
    monkeypatch.setattr(xagg, "age_client", _m4_fixture())

    result = await xagg.run_aggregate(
        _M4_GOLD_TEXT, None, gateway=FakeGateway([]), user_role="supervisor",
    )

    assert result["kind"] == "statute_court_stage_join"
    assert result["kind"] != "graph_recurrence"


class TestStatuteCourtStageJoinBoundary:
    """
    [Gold-QA fix — Module 24] The two-signal AND, tested at its edges. The
    signal that separates M4 is COURT PROGRESSION, not the word "court" —
    `_COURT_READINESS_KEYWORDS`' own comment records the live collision
    where a bare Urdu "عدالت" hijacked M4 into G3's readiness scan, and
    matching on "court" alone here would run that collision backwards.
    """

    def test_m4_gold_text_matches(self):
        assert xagg._is_statute_court_stage_join(_M4_GOLD_TEXT.lower())

    @pytest.mark.parametrize("paraphrase", [
        # The required non-gold paraphrase: plain English, no Urdu, sharing
        # no phrase with M4's literal text.
        "Do the sections people are charged under and how far those cases "
        "have got in court give the same picture of how serious our "
        "caseload is?",
        "What stage have our cases reached in court?",
        "How many of our cases have actually ended in a conviction in court?",
    ])
    def test_non_gold_paraphrases_match(self, paraphrase):
        assert xagg._is_statute_court_stage_join(paraphrase.lower())

    @pytest.mark.parametrize("other", [
        # G3 — a court question with no progression signal. Scores 1.0
        # today; must stay with `_court_readiness_scan()`.
        _G3_GOLD_TEXT,
        # CR7 — criminal-record vs. court RECORD consistency, not a stage.
        _CR7_GOLD_TEXT,
        # M1/M5 — statute questions with no court dimension at all.
        "What kinds of cases are we dealing with now compared to a couple "
        "of years back?",
        "Are guns turning up in different types of cases than they used to?",
        # A progression word with no court term is not this family.
        "Which investigations have progressed the furthest this month?",
    ])
    def test_neighbouring_families_are_not_captured(self, other):
        assert not xagg._is_statute_court_stage_join(other.lower())

    def test_matches_m4_and_no_other_gold_question(self):
        """The all-32 negative control, same discipline as
        `TestWeaponStatuteCooccurrenceBoundary`'s. Reads
        `evaluation/Gold_QA_Dataset_Final32_With_Answers.json` — the bare
        `Gold_QA_Dataset_Final32.json` some older tests look for is NOT
        tracked in this repo, so a test pinned to it silently skips and has
        never actually run."""
        import json
        from pathlib import Path

        gold_path = (
            Path(__file__).resolve().parent.parent
            / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
        )
        assert gold_path.exists(), gold_path
        items = json.loads(gold_path.read_text(encoding="utf-8"))
        assert len(items) == 32
        matched = [
            (it.get("id") or "").upper()
            for it in items
            if xagg._is_statute_court_stage_join(it["question"].lower())
        ]
        assert matched == ["M4"], f"expected only M4, got {matched}"


async def test_g3_still_reaches_the_court_readiness_scan_after_module_24(monkeypatch):
    """Negative control, end to end rather than at the predicate: G3 scores
    1.0 today and shares this module's entire court vocabulary. This is the
    single most likely thing Module 24 breaks."""
    class _AC:
        async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
            return []

    monkeypatch.setattr(xagg, "age_client", _AC())

    result = await xagg.run_aggregate(
        _G3_GOLD_TEXT, None,
        gateway=FakeGateway([{"case_id": "fir-1-26", "incident_date": None}]),
        user_role="supervisor",
    )

    assert result["kind"] == "court_readiness_scan"


async def test_cr7_still_reaches_the_criminal_record_crosscheck_after_module_24(monkeypatch):
    """Negative control: CR7 must keep reaching Module 14's reader directly,
    not the join that merely wraps it."""
    criminal = [{"status": "Under trial", "case_ref": "FIR 100/26", "subject": "A"}]
    monkeypatch.setattr(xagg, "age_client", _QueryAwareAgeClient(criminal, []))

    result = await xagg.run_aggregate(
        _CR7_GOLD_TEXT, None, gateway=FakeGateway([]), user_role="supervisor",
    )

    assert result["kind"] == "criminal_record_court_crosscheck"


async def test_m5_still_reaches_the_cooccurrence_aggregate_after_module_24(monkeypatch):
    """Module 23 landed in this same dispatch chain immediately above this
    module's entry; its own question must be unaffected by the insertion."""
    class _AC:
        async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
            return []

    monkeypatch.setattr(xagg, "age_client", _AC())

    result = await xagg.run_aggregate(
        _M5_GOLD_TEXT, None, gateway=FakeGateway([]), user_role="supervisor",
    )

    assert result["kind"] == "weapon_statute_cooccurrence"


# ── [Gold-QA fix — Module 31, question G1] offender age profile ────────────
#
# G1 is a broad "review the caseload" question that reaches XAGG only after
# Meta-Analysis decomposes it (Module 29). The literal text below is the
# sub-question that carries G1's offender-profile element, written in the
# same "..., across all cases?" shape as every other sub-query in
# `meta_analysis.py`'s `_DECOMPOSITION_PLANS`. Pinning it here is what stops
# a later edit to `_AGE_KEYWORDS` from silently re-breaking the dispatch.
_G1_SQ_ACCUSED_AGE = (
    "How many cases involve an accused person, and what is their age range "
    "and average age, across all cases?"
)
_G1_GOLD_TEXT = (
    "Acting as a crime analyst, review our current caseload and flag "
    "anything that looks unusual or worth monitoring."
)


def _age_rows(pairs):
    """(entity_id, age, case_id) triples -> the shape `_offender_age_profile()`
    reads. `age=None` is an accused with no age recorded."""
    rows = []
    for entity_id, age, case_id in pairs:
        props = {"entity_id": entity_id}
        if age is not None:
            props["age"] = age
        rows.append({"p": {"id": entity_id, "label": "Person", "properties": props},
                     "case_id": case_id})
    return rows


async def test_offender_age_profile_reports_range_mean_and_both_denominators(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(_age_rows([
        ("P-1", 24, "fir-1-26"),
        ("P-2", 30, "fir-1-26"),
        ("P-3", 49, "fir-2-26"),
        # A recidivist: two accused ENTRIES, one distinct person. The two
        # denominators must diverge here or the caveat is meaningless.
        ("P-3", 49, "fir-3-26"),
        ("P-4", None, "fir-4-26"),
        ("P-5", None, "fir-5-26"),
    ])))

    r = await xagg._offender_age_profile()

    assert r["kind"] == "offender_age_profile"
    assert r["unsupported"] is False
    assert (r["min_age"], r["max_age"]) == (24, 49)
    assert r["mean_age"] == pytest.approx((24 + 30 + 49) / 3)
    assert r["with_age_count"] == 3
    assert r["distinct_accused_count"] == 5
    assert r["entries_with_age_count"] == 4      # P-3 counted twice
    assert r["accused_entry_count"] == 6


async def test_offender_age_profile_ignores_unusable_ages(monkeypatch):
    """A free-text or out-of-range age must be treated exactly like a
    missing one — counted in the coverage gap, never in the mean."""
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(_age_rows([
        ("P-1", 30, "fir-1-26"),
        ("P-2", "32", "fir-2-26"),      # numeric string — usable
        ("P-3", "اکتیس", "fir-3-26"),    # words — not usable
        ("P-4", 0, "fir-4-26"),          # out of range
        ("P-5", 900, "fir-5-26"),        # out of range
    ])))

    r = await xagg._offender_age_profile()

    assert r["with_age_count"] == 2
    assert (r["min_age"], r["max_age"]) == (30, 32)
    assert r["distinct_accused_count"] == 5


async def test_offender_age_profile_canonicalises_confirmed_duplicates(monkeypatch):
    """Two entity ids confirmed to be the same person are one accused, the
    same rule `_total_accused_count()` applies."""
    async def _pairs():
        return [("P-1", "P-1-DUP")]

    monkeypatch.setattr(xagg, "fetch_confirmed_same_as", _pairs)
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(_age_rows([
        ("P-1", 30, "fir-1-26"),
        ("P-1-DUP", 30, "fir-2-26"),
    ])))

    r = await xagg._offender_age_profile()

    assert r["distinct_accused_count"] == 1
    assert r["with_age_count"] == 1
    assert r["accused_entry_count"] == 2


async def test_offender_age_profile_says_so_when_no_accused_carries_an_age(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(_age_rows([
        ("P-1", None, "fir-1-26"), ("P-2", None, "fir-2-26"),
    ])))

    r = await xagg._offender_age_profile()

    assert r["unsupported"] is True
    assert "no accused record in this corpus carries a recorded age" in r["message"].lower()


async def test_offender_age_profile_honours_the_jurisdiction_allow_list(monkeypatch):
    rows = _age_rows([("P-1", 30, "fir-1-26")])
    seen = {}

    async def _exec(cypher_query, params=None, columns=("result",), graph=None):
        seen["q"], seen["p"] = cypher_query, params
        return rows

    monkeypatch.setattr(
        xagg, "age_client", type("_A", (), {"execute_cypher": staticmethod(_exec)}),
    )

    await xagg._offender_age_profile(jurisdiction_case_ids=["fir-1-26"])

    assert "$case_ids" in seen["q"]
    assert seen["p"]["case_ids"] == ["fir-1-26"]


def test_render_offender_age_profile_states_coverage_and_the_nationality_gap():
    rendered = "\n".join(xagg.render_offender_age_profile({
        "kind": "offender_age_profile", "unsupported": False,
        "min_age": 24, "max_age": 49, "mean_age": 31.470588,
        "with_age_count": 17, "distinct_accused_count": 92,
        "entries_with_age_count": 19, "accused_entry_count": 94,
        "ages": [], "nationality_note": xagg._NATIONALITY_NOT_MODELED,
    }))

    assert "24 to 49" in rendered
    assert "31.5" in rendered
    assert "17 of 92" in rendered
    assert "19 of 94" in rendered
    # The caveat that stops "every accused is 24-49" being read out of an
    # 18%-coverage sample — gold G1 makes exactly that overclaim.
    assert "75 of 92" in rendered
    assert "not evidence that no accused is younger or older" in rendered
    # Gold G1 also claims "all are Pakistani nationals". The field does not
    # exist; the answer must say so rather than let it be inferred.
    assert "nationality is not recorded" in rendered.lower()


def test_render_offender_age_profile_passes_the_empty_corpus_message_through():
    rendered = "\n".join(xagg.render_offender_age_profile(
        {"kind": "offender_age_profile", "unsupported": True, "message": xagg._UNSUPPORTED_AGE}
    ))
    assert rendered == xagg._UNSUPPORTED_AGE


async def test_g1_age_sub_query_reaches_the_age_profile_not_person_recurrence(monkeypatch):
    """THE REGRESSION PINNED TO G1's LITERAL AGE SUB-QUERY. Measured before
    this module (2026-09-08): this exact string returned
    `{"kind": "unsupported_aggregate"}` carrying the stale claim that age is
    "not currently extracted into this system's data model" — false since
    Module 1d."""
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(_age_rows([("P-1", 31, "fir-1-26")])))

    result = await xagg.run_aggregate(
        _G1_SQ_ACCUSED_AGE, None, gateway=FakeGateway([]), user_role="supervisor",
    )

    assert result["kind"] == "offender_age_profile"
    assert result["unsupported"] is False
    assert result["kind"] != "graph_recurrence"
    assert result["kind"] != "unsupported_aggregate"


class TestOffenderAgeProfileBoundary:
    """
    [Gold-QA fix — Module 31] `_AGE_KEYWORDS` is checked FIRST in
    `run_aggregate()`, ahead of every entity family, so anything it matches
    it takes outright. That precedence is why this family gets the strictest
    negative control in the file.
    """

    def test_the_g1_age_sub_query_matches(self):
        assert xagg._matches_any(_G1_SQ_ACCUSED_AGE.lower(), xagg._AGE_KEYWORDS)

    @pytest.mark.parametrize("paraphrase", [
        # The required non-gold paraphrases — no phrase shared with the
        # dispatched sub-query above.
        "How old are the people we are charging?",
        "Give me the age profile of our offenders.",
        "Mulzimon ki umar kya hai?",
        "ملزمان کی اوسط عمر کیا ہے؟",
    ])
    def test_non_gold_paraphrases_match(self, paraphrase):
        assert xagg._matches_any(paraphrase.lower(), xagg._AGE_KEYWORDS)

    @pytest.mark.parametrize("other", [
        # Neighbouring families this must not swallow, given it wins first.
        "How many cases does each accused person appear in, and which FIR "
        "numbers, across all cases?",
        "How many of the accused are men and how many are women, across all cases?",
        "How many accused persons are there in total?",
        "Which district recovers the most weapons?",
    ])
    def test_neighbouring_families_are_not_captured(self, other):
        assert not xagg._matches_any(other.lower(), xagg._AGE_KEYWORDS)

    def test_matches_no_gold_question_at_all(self):
        """The all-32 negative control. `_AGE_KEYWORDS` must match NONE of
        the 32 gold questions: G1 itself is a broad review that only reaches
        XAGG through Meta-Analysis decomposition, so a direct match on any
        gold text would mean this family had grown too wide.

        Reads `evaluation/Gold_QA_Dataset_Final32_With_Answers.json` — the
        bare `Gold_QA_Dataset_Final32.json` is NOT tracked in this repo, and
        a test pinned to it silently skips (PR #21)."""
        import json
        from pathlib import Path

        gold_path = (
            Path(__file__).resolve().parent.parent
            / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
        )
        assert gold_path.exists(), gold_path
        items = json.loads(gold_path.read_text(encoding="utf-8"))
        assert len(items) == 32
        matched = [
            (it.get("id") or "").upper()
            for it in items
            if xagg._matches_any(it["question"].lower(), xagg._AGE_KEYWORDS)
        ]
        assert matched == [], f"expected no gold question to match, got {matched}"


# ── [Gold-QA fix — Module 32, question G1] accused↔complainant relationship ─
#
# The literal G1 sub-question this family exists to answer, in the same
# "..., across all cases?" shape as `meta_analysis.py`'s other sub-queries.
_G1_SQ_RELATIONSHIP = (
    "How many cases record a relationship between the accused and the "
    "complainant, and which relationship is it, across all cases?"
)


class _RelationshipAgeClient:
    """Routes the two reads `_accused_relationship_breakdown()` issues."""

    def __init__(self, rel_rows, accused_rows):
        self.rel_rows = rel_rows
        self.accused_rows = accused_rows
        self.queries = []

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        self.queries.append((cypher_query, params))
        if "RELATED_TO" in cypher_query:
            return self.rel_rows
        return self.accused_rows


def _rel(role, case, a_id, b_id):
    return {
        "role": role,
        "source_doc_id": f"psrms/fir/{case}#structured",
        "a_id": a_id,
        "b_id": b_id,
    }


def _accused(*p_ids):
    return [{"p_id": p} for p in p_ids]


async def test_relationship_breakdown_ranks_values_and_names_the_dominant_one(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", _RelationshipAgeClient(
        rel_rows=[
            _rel("اجنبی", "fir-1-26", 1, 90),
            _rel("اجنبی", "fir-2-26", 2, 91),
            _rel("اجنبی", "fir-2-26", 3, 91),
            _rel("بھائی", "fir-3-26", 4, 92),
        ],
        accused_rows=_accused(1, 2, 3, 4, 5, 5, 6),
    ))

    r = await xagg._accused_relationship_breakdown()

    assert r["kind"] == "accused_relationship_breakdown"
    assert r["total_relationships"] == 4
    assert r["distinct_value_count"] == 2
    assert r["case_count"] == 3
    assert r["dominant"]["role"] == "اجنبی"
    assert r["dominant"]["count"] == 3
    assert r["dominant"]["gloss"] == "stranger"
    # Per-value FIR counts: اجنبی spans two FIRs, not three edges' worth.
    assert r["counts"][0]["case_count"] == 2
    # Coverage: 4 of the 6 distinct accused carry a relationship.
    assert r["distinct_accused_count"] == 6
    assert r["accused_entry_count"] == 7
    assert r["accused_with_relationship"] == 4


async def test_relationship_breakdown_reports_the_duplicate_pair_double_count(monkeypatch):
    """One accused row writes TWO edges when the victim and the complainant
    are the same person. The raw edge count stays the headline (gold's own
    denominator), but the duplication must be visible."""
    monkeypatch.setattr(xagg, "age_client", _RelationshipAgeClient(
        rel_rows=[
            _rel("اجنبی", "fir-1-26", 1, 90),
            _rel("اجنبی", "fir-1-26", 1, 90),   # same pair, same role
            _rel("شوہر", "fir-2-26", 2, 91),
        ],
        accused_rows=_accused(1, 2),
    ))

    r = await xagg._accused_relationship_breakdown()

    assert r["total_relationships"] == 3
    assert r["distinct_pair_count"] == 2
    rendered = "\n".join(xagg.render_accused_relationship_breakdown(r))
    assert "3 entries cover 2 distinct" in rendered


async def test_relationship_breakdown_scopes_by_the_edges_own_source_document(monkeypatch):
    """The jurisdiction allow-list is applied to the FIR the relationship was
    RECORDED on, not to every case its accused happens to touch — walking
    `(a)-[:BELONGS_TO_CASE]->(:Case)` multiplies an edge by that person's
    case count (measured live: 24 edges became 27 rows)."""
    monkeypatch.setattr(xagg, "age_client", _RelationshipAgeClient(
        rel_rows=[
            _rel("اجنبی", "fir-1-26", 1, 90),
            _rel("بھائی", "fir-2-26", 2, 91),
        ],
        accused_rows=_accused(1),
    ))

    r = await xagg._accused_relationship_breakdown(jurisdiction_case_ids=["fir-1-26"])

    assert r["total_relationships"] == 1
    assert r["counts"][0]["role"] == "اجنبی"


async def test_relationship_breakdown_drops_unresolvable_edges_only_when_scoped(monkeypatch):
    """An edge whose source document names no case cannot be shown to be in
    scope, so it is dropped under an allow-list and kept without one."""
    rows = [{"role": "اجنبی", "source_doc_id": None, "a_id": 1, "b_id": 90}]
    monkeypatch.setattr(xagg, "age_client", _RelationshipAgeClient(rows, _accused(1)))
    assert (await xagg._accused_relationship_breakdown())["total_relationships"] == 1

    monkeypatch.setattr(xagg, "age_client", _RelationshipAgeClient(rows, _accused(1)))
    scoped = await xagg._accused_relationship_breakdown(jurisdiction_case_ids=["fir-1-26"])
    assert scoped["total_relationships"] == 0


async def test_relationship_breakdown_on_an_empty_corpus_says_so(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", _RelationshipAgeClient([], _accused(1, 2)))

    r = await xagg._accused_relationship_breakdown()

    assert r["total_relationships"] == 0
    rendered = "\n".join(xagg.render_accused_relationship_breakdown(r))
    assert "no accused↔complainant relationship is recorded" in rendered.lower()


def test_render_relationship_breakdown_glosses_urdu_and_states_coverage():
    rendered = "\n".join(xagg.render_accused_relationship_breakdown({
        "kind": "accused_relationship_breakdown",
        "total_relationships": 24, "distinct_pair_count": 24,
        "distinct_value_count": 6, "case_count": 10,
        "counts": [
            {"role": "اجنبی", "gloss": "stranger", "count": 15, "case_count": 6},
            {"role": "بھائی", "gloss": "brother", "count": 1, "case_count": 1},
        ],
        "dominant": {"role": "اجنبی", "gloss": "stranger", "count": 15, "case_count": 6},
        "accused_entry_count": 94, "distinct_accused_count": 92,
        "accused_with_relationship": 12,
    }))

    assert "اجنبی (stranger): 15 of 24" in rendered
    assert "10 FIR(s)" in rendered
    assert "dominant recorded relationship is اجنبی (stranger)" in rendered
    # The coverage caveat that stops "stranger dominates" being read as a
    # statement about the whole caseload — it describes 12 of 92 accused.
    assert "12 of 92 distinct accused" in rendered
    assert "not a profile of the whole caseload" in rendered


def test_render_relationship_breakdown_passes_unmapped_values_through_verbatim():
    """The gloss never substitutes a guess for a value it does not know."""
    rendered = "\n".join(xagg.render_accused_relationship_breakdown({
        "kind": "accused_relationship_breakdown",
        "total_relationships": 1, "distinct_pair_count": 1,
        "distinct_value_count": 1, "case_count": 1,
        "counts": [{"role": "کوئی نیا رشتہ", "gloss": None, "count": 1, "case_count": 1}],
        "dominant": {"role": "کوئی نیا رشتہ", "gloss": None, "count": 1, "case_count": 1},
        "accused_entry_count": 1, "distinct_accused_count": 1,
        "accused_with_relationship": 1,
    }))

    assert "کوئی نیا رشتہ: 1 of 1" in rendered
    # No gloss parenthetical is attached to the raw value anywhere.
    assert "کوئی نیا رشتہ (" not in rendered


async def test_g1_relationship_sub_query_no_longer_falls_through_to_person_recurrence(monkeypatch):
    """THE REGRESSION PINNED TO G1's LITERAL RELATIONSHIP SUB-QUERY.
    Measured before this module (2026-09-08): this exact string returned
    `{"kind": "graph_recurrence", "entity_type": "Person"}` — a ranked list
    of repeat accused, confidently answering a question nobody asked. The
    fall-through happened because "accused" is in `_PERSON_KEYWORDS`."""
    monkeypatch.setattr(xagg, "age_client", _RelationshipAgeClient(
        [_rel("اجنبی", "fir-1-26", 1, 90)], _accused(1),
    ))

    result = await xagg.run_aggregate(
        _G1_SQ_RELATIONSHIP, None, gateway=FakeGateway([]), user_role="supervisor",
    )

    assert result["kind"] == "accused_relationship_breakdown"
    assert result["kind"] != "graph_recurrence"


async def test_g3_still_reaches_the_court_readiness_scan_after_module_32(monkeypatch):
    """The single most likely thing Module 32 breaks: G3's gold answer is
    about this SAME `RELATED_TO` data read as a completeness gap, and G3
    scores 1.0 today. Its branch is checked first, structurally."""
    class _AC:
        async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
            return []

    monkeypatch.setattr(xagg, "age_client", _AC())

    result = await xagg.run_aggregate(
        _G3_GOLD_TEXT, None,
        gateway=FakeGateway([{"case_id": "fir-1-26", "incident_date": None}]),
        user_role="supervisor",
    )

    assert result["kind"] == "court_readiness_scan"


class TestAccusedRelationshipBoundary:
    """[Gold-QA fix — Module 32] The keyword family at its edges."""

    def test_the_g1_relationship_sub_query_matches(self):
        assert xagg._matches_any(_G1_SQ_RELATIONSHIP.lower(), xagg._RELATIONSHIP_KEYWORDS)

    @pytest.mark.parametrize("paraphrase", [
        # The required non-gold paraphrases — no phrase shared with the
        # dispatched sub-query above.
        "Do our accused usually know the people they offend against, or are "
        "they strangers?",
        "Kya mulzim aur mudai ek doosre ko jaante hain ya ajnabi hote hain?",
        "ملزم اور مدعی کا آپس میں کیا تعلق ہوتا ہے؟",
    ])
    def test_non_gold_paraphrases_match(self, paraphrase):
        assert xagg._matches_any(paraphrase.lower(), xagg._RELATIONSHIP_KEYWORDS)

    @pytest.mark.parametrize("other", [
        # The bare Urdu "تعلق" was deliberately excluded: it is a substring
        # of "متعلق" ("regarding"), which KB4 uses. This is that collision,
        # pinned.
        "جب پولیس کسی مقدمے سے متعلق اشیاء اپنی تحویل میں لیتی ہے، تو کیا "
        "اِس بارے میں کوئی باقاعدہ معیار موجود ہے؟",
        # Neighbouring XAGG families that carry person vocabulary.
        "How many cases does each accused person appear in, and which FIR "
        "numbers, across all cases?",
        "How many of the accused are men and how many are women, across all cases?",
        "How many accused persons are there in total?",
    ])
    def test_neighbouring_families_are_not_captured(self, other):
        assert not xagg._matches_any(other.lower(), xagg._RELATIONSHIP_KEYWORDS)

    def test_matches_no_gold_question_at_all(self):
        """The all-32 negative control, same discipline as
        `TestOffenderAgeProfileBoundary`'s: G1 reaches XAGG only through
        Meta-Analysis decomposition, so a direct match on any gold text
        would mean this family had grown too wide. Reads
        `evaluation/Gold_QA_Dataset_Final32_With_Answers.json` — the bare
        `Gold_QA_Dataset_Final32.json` is NOT tracked in this repo, and a
        test pinned to it silently skips (PR #21)."""
        import json
        from pathlib import Path

        gold_path = (
            Path(__file__).resolve().parent.parent
            / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
        )
        assert gold_path.exists(), gold_path
        items = json.loads(gold_path.read_text(encoding="utf-8"))
        assert len(items) == 32
        matched = [
            (it.get("id") or "").upper()
            for it in items
            if xagg._matches_any(it["question"].lower(), xagg._RELATIONSHIP_KEYWORDS)
        ]
        assert matched == [], f"expected no gold question to match, got {matched}"


# ── [Gold-QA fix — Module 33, question G1] seized-property disposition ─────
#
# The literal G1 sub-question this family exists to answer, in the same
# "..., across all cases?" shape as `meta_analysis.py`'s other sub-queries.
_G1_SQ_SEIZED_PROPERTY = (
    "How many cases record seized property, and what happens to it — how "
    "many items were sent to a forensic laboratory or held for a deceased's "
    "heirs, across all cases?"
)
_MALKHANA_FORENSIC = "سیل بند، نمونہ فرانزک لیبارٹری بھجوایا گیا"
_MALKHANA_HEIRS = "ورثاء کے حوالے کیا جائے گا"
_MALKHANA_FORENSIC_EVIDENCE = "فرانزک شواہد کے طور پر محفوظ"


def _malkhana(condition, case_id, item_detail="چیز"):
    return {"condition": condition, "item_detail": item_detail, "case_id": case_id}


async def test_seized_property_groups_by_disposition_with_item_and_fir_counts(monkeypatch):
    """Item count and FIR count differ whenever one FIR seizes two items —
    conflating them is the easiest way to report a wrong number here."""
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient([
        _malkhana(_MALKHANA_FORENSIC, "fir-1-26"),
        _malkhana(_MALKHANA_FORENSIC, "fir-1-26"),   # same FIR, second item
        _malkhana(_MALKHANA_FORENSIC, "fir-2-26"),
        _malkhana(_MALKHANA_HEIRS, "fir-3-26"),
        _malkhana("ضبط شدہ", "fir-4-26"),
    ]))

    r = await xagg._seized_property_disposition()

    assert r["kind"] == "seized_property_disposition"
    assert r["total_items"] == 5
    assert r["case_count"] == 4
    assert r["distinct_disposition_count"] == 3
    top = r["counts"][0]
    assert top["condition"] == _MALKHANA_FORENSIC
    assert top["item_count"] == 3
    assert top["case_count"] == 2          # NOT 3
    assert top["gloss"] == "sealed, sample sent to the forensic laboratory"
    assert r["forensic_dispatch_items"] == 3
    assert r["forensic_dispatch_cases"] == 2
    assert r["heirs_items"] == 1
    assert r["heirs_cases"] == 1


async def test_seized_property_separates_dispatched_from_merely_forensic(monkeypatch):
    """The classification rule Module 33 publishes: gold's "13 sent to a
    forensic lab" is the LITERAL dispatch reading. Entries that mention a
    forensic process in some other wording are counted separately, never
    folded into the headline."""
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient([
        _malkhana(_MALKHANA_FORENSIC, "fir-1-26"),
        _malkhana(_MALKHANA_FORENSIC_EVIDENCE, "fir-2-26"),
        _malkhana("مالخانہ میں مہر بند، فرانزک معائنہ مطلوب", "fir-3-26"),
    ]))

    r = await xagg._seized_property_disposition()

    assert r["forensic_dispatch_items"] == 1
    assert r["forensic_any_items"] == 3
    rendered = "\n".join(xagg.render_seized_property_disposition(r))
    assert "3 entries mention a forensic process" in rendered
    assert "only 1 record the item as actually sent to the laboratory" in rendered
    assert "literal 'sent to the lab' reading" in rendered


async def test_seized_property_reports_entries_with_no_disposition_separately(monkeypatch):
    """A register entry with a blank disposition is a real state; putting it
    in any bucket would inflate that bucket."""
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient([
        _malkhana(_MALKHANA_FORENSIC, "fir-1-26"),
        _malkhana("", "fir-2-26"),
        _malkhana(None, "fir-3-26"),
    ]))

    r = await xagg._seized_property_disposition()

    assert r["total_items"] == 3
    assert r["unrecorded_disposition_count"] == 2
    assert r["distinct_disposition_count"] == 1
    rendered = "\n".join(xagg.render_seized_property_disposition(r))
    assert "2 register entr(ies) record no disposition" in rendered


async def test_seized_property_honours_the_jurisdiction_allow_list(monkeypatch):
    seen = {}

    async def _exec(cypher_query, params=None, columns=("result",), graph=None):
        seen["q"], seen["p"] = cypher_query, params
        return [_malkhana(_MALKHANA_FORENSIC, "fir-1-26")]

    monkeypatch.setattr(
        xagg, "age_client", type("_A", (), {"execute_cypher": staticmethod(_exec)}),
    )

    await xagg._seized_property_disposition(jurisdiction_case_ids=["fir-1-26"])

    assert "$case_ids" in seen["q"]
    assert seen["p"]["case_ids"] == ["fir-1-26"]
    assert "malkhana_register" in seen["q"]


async def test_seized_property_on_an_empty_corpus_says_so(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient([]))

    r = await xagg._seized_property_disposition()

    assert r["total_items"] == 0
    rendered = "\n".join(xagg.render_seized_property_disposition(r))
    assert "no seized-property (malkhana) register entry is recorded" in rendered.lower()


def test_render_seized_property_passes_unmapped_conditions_through_verbatim():
    rendered = "\n".join(xagg.render_seized_property_disposition({
        "kind": "seized_property_disposition", "total_items": 1, "case_count": 1,
        "distinct_disposition_count": 1,
        "counts": [{"condition": "کوئی نئی حالت", "gloss": None,
                    "item_count": 1, "case_count": 1}],
        "unrecorded_disposition_count": 0,
        "forensic_dispatch_items": 0, "forensic_dispatch_cases": 0,
        "forensic_any_items": 0, "heirs_items": 0, "heirs_cases": 0,
    }))

    assert "کوئی نئی حالت: 1 item(s)" in rendered
    assert "کوئی نئی حالت (" not in rendered


def test_render_seized_property_truncates_a_long_tail():
    counts = [
        {"condition": f"c{i}", "gloss": None, "item_count": 1, "case_count": 1}
        for i in range(xagg._DISPOSITION_RENDER_LIMIT + 3)
    ]
    rendered = "\n".join(xagg.render_seized_property_disposition({
        "kind": "seized_property_disposition", "total_items": len(counts),
        "case_count": 5, "distinct_disposition_count": len(counts),
        "counts": counts, "unrecorded_disposition_count": 0,
        "forensic_dispatch_items": 0, "forensic_dispatch_cases": 0,
        "forensic_any_items": 0, "heirs_items": 0, "heirs_cases": 0,
    }))

    assert "(+3 further disposition(s), 1 item each)" in rendered


async def test_g1_seized_property_sub_query_no_longer_dumps_the_whole_corpus(monkeypatch):
    """THE REGRESSION PINNED TO G1's LITERAL SEIZED-PROPERTY SUB-QUERY.
    Measured before this module (2026-09-08): this exact string returned
    `kind="case_listing"` — every case in the corpus, unfiltered — because
    "across all cases" contains the literal `_LIST_ALL_KEYWORDS` entry "all
    cases"."""
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient([
        _malkhana(_MALKHANA_FORENSIC, "fir-1-26"),
    ]))

    result = await xagg.run_aggregate(
        _G1_SQ_SEIZED_PROPERTY, None,
        gateway=FakeGateway([{"case_id": "fir-9-26"}]), user_role="supervisor",
    )

    assert result["kind"] == "seized_property_disposition"
    assert result["kind"] != "case_listing"
    assert result["kind"] != "graph_recurrence"


async def test_g5_still_reaches_the_weapon_compliance_scan_after_module_33(monkeypatch):
    """G5 scores 1.0 today and shares the seized/recovered vocabulary this
    module adds. Its branch is checked first, structurally."""
    class _AC:
        async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
            return []

    monkeypatch.setattr(xagg, "age_client", _AC())

    result = await xagg.run_aggregate(
        "Baramad shuda hathiyaron ki record keeping ko dekhte hue, kya koi "
        "aisi baat hai jo compliance ke lihaz se flag karne layak ho?",
        None, gateway=FakeGateway([]), user_role="supervisor",
    )

    assert result["kind"] == "weapon_compliance_scan"


class TestSeizedPropertyBoundary:
    """[Gold-QA fix — Module 33] The keyword family at its edges."""

    def test_the_g1_seized_property_sub_query_matches(self):
        assert xagg._matches_any(
            _G1_SQ_SEIZED_PROPERTY.lower(), xagg._SEIZED_PROPERTY_KEYWORDS
        )

    @pytest.mark.parametrize("paraphrase", [
        # The required non-gold paraphrases — no phrase shared with the
        # dispatched sub-query above.
        "Where does the stuff we take into the malkhana end up?",
        "Give me a breakdown of the case property register by disposition.",
        "مالخانہ میں رکھی اشیاء کا آخر کیا بنتا ہے؟",
    ])
    def test_non_gold_paraphrases_match(self, paraphrase):
        assert xagg._matches_any(paraphrase.lower(), xagg._SEIZED_PROPERTY_KEYWORDS)

    @pytest.mark.parametrize("other", [
        # KB6 uses "forensics guidelines" — the family deliberately matches
        # only "forensic lab"/"forensic laboratory", never a bare "forensic".
        "Kya forensics guidelines mein is bare mein kuch makhsoos likha hai "
        "ke baramad shuda aslaha darj hone se pehle kaise handle kiya jaye?",
        # G5 — the weapon register, an adjacent but different subject.
        "Baramad shuda hathiyaron ki record keeping ko dekhte hue, kya koi "
        "aisi baat hai jo compliance ke lihaz se flag karne layak ho?",
        # Neighbouring XAGG families.
        "How many cases involve a recovered weapon with no licence recorded, "
        "across all cases?",
        "Give me the list of all cases.",
    ])
    def test_neighbouring_families_are_not_captured(self, other):
        assert not xagg._matches_any(other.lower(), xagg._SEIZED_PROPERTY_KEYWORDS)

    def test_matches_no_gold_question_at_all(self):
        """The all-32 negative control, same discipline as
        `TestOffenderAgeProfileBoundary`'s. Reads
        `evaluation/Gold_QA_Dataset_Final32_With_Answers.json` — the bare
        `Gold_QA_Dataset_Final32.json` is NOT tracked in this repo, and a
        test pinned to it silently skips (PR #21)."""
        import json
        from pathlib import Path

        gold_path = (
            Path(__file__).resolve().parent.parent
            / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
        )
        assert gold_path.exists(), gold_path
        items = json.loads(gold_path.read_text(encoding="utf-8"))
        assert len(items) == 32
        matched = [
            (it.get("id") or "").upper()
            for it in items
            if xagg._matches_any(it["question"].lower(), xagg._SEIZED_PROPERTY_KEYWORDS)
        ]
        assert matched == [], f"expected no gold question to match, got {matched}"


# ── [Gold-QA fix — Module 34, question G1] incident time-of-day ────────────
#
# The literal G1 sub-question this family exists to answer, in the same
# "..., across all cases?" shape as `meta_analysis.py`'s other sub-queries.
_G1_SQ_TIME_OF_DAY = (
    "How many cases record an incident time, and at what time of day do "
    "those incidents happen, across all cases?"
)

_NIGHT = "night (00:00-05:59)"
_MORNING = "morning (06:00-11:59)"
_AFTERNOON = "afternoon (12:00-17:59)"
_EVENING = "evening (18:00-23:59)"


class _TimeOfDayAgeClient:
    """Routes the two reads `_incident_time_of_day()` issues."""

    def __init__(self, total, rows):
        self.total = total
        self.rows = rows
        self.queries = []

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        self.queries.append((cypher_query, params))
        if "count(DISTINCT i)" in cypher_query:
            return [{"n": self.total}]
        return self.rows


def _incident(dt, case_id):
    return {"incident_datetime": dt, "case_id": case_id}


async def test_incident_time_of_day_buckets_by_hour_band_and_names_the_peak(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", _TimeOfDayAgeClient(6, [
        _incident("2026-01-01T01:00:00Z", "fir-1-26"),   # night
        _incident("2026-01-02T09:30:00Z", "fir-2-26"),   # morning
        _incident("2026-01-03T14:00:00Z", "fir-3-26"),   # afternoon
        _incident("2026-01-04T19:00:00Z", "fir-4-26"),   # evening
        _incident("2026-01-05T21:15:00Z", "fir-5-26"),   # evening
    ]))

    r = await xagg._incident_time_of_day()

    assert r["kind"] == "incident_time_of_day"
    assert r["total_incidents"] == 6
    assert r["with_datetime_count"] == 5
    assert r["with_clock_time_count"] == 5
    counts = {b["band"]: b["count"] for b in r["buckets"]}
    assert counts == {_NIGHT: 1, _MORNING: 1, _AFTERNOON: 1, _EVENING: 2}
    assert r["peak_band"]["band"] == _EVENING
    assert r["buckets"][3]["share"] == pytest.approx(2 / 5)
    # Bands are rendered chronologically, not by size.
    assert [b["band"] for b in r["buckets"]] == [_NIGHT, _MORNING, _AFTERNOON, _EVENING]


async def test_incident_time_of_day_excludes_exact_midnight_as_date_only(monkeypatch):
    """THE DECISION THIS MODULE HAD TO MAKE, pinned. A 00:00:00 value is a
    date with no clock time, not a real midnight. Counting it as "night" is
    what makes the day look evenly covered."""
    monkeypatch.setattr(xagg, "age_client", _TimeOfDayAgeClient(5, [
        _incident("2026-01-01T00:00:00Z", "fir-1-26"),
        _incident("2026-01-02T00:00:00Z", "fir-2-26"),
        _incident("2026-01-03T00:00:00Z", "fir-3-26"),
        _incident("2026-01-04T19:00:00Z", "fir-4-26"),
    ]))

    r = await xagg._incident_time_of_day()

    assert r["with_datetime_count"] == 4
    assert r["date_only_count"] == 3
    assert r["with_clock_time_count"] == 1
    assert {b["band"]: b["count"] for b in r["buckets"]}[_NIGHT] == 0
    # The naive reading is still returned, so the difference is auditable.
    naive = {b["band"]: b["count"] for b in r["naive_bucket_counts"]}
    assert naive[_NIGHT] == 3

    rendered = "\n".join(xagg.render_incident_time_of_day(r))
    assert "3 of those record exactly 00:00:00" in rendered
    assert f"would put 3 in the {_NIGHT} band" in rendered


async def test_incident_time_of_day_keeps_a_real_00_30_incident(monkeypatch):
    """Only EXACT midnight is treated as date-only. 00:30 is a real
    overnight incident and must stay in the night band."""
    monkeypatch.setattr(xagg, "age_client", _TimeOfDayAgeClient(1, [
        _incident("2026-01-01T00:30:00Z", "fir-1-26"),
    ]))

    r = await xagg._incident_time_of_day()

    assert r["date_only_count"] == 0
    assert {b["band"]: b["count"] for b in r["buckets"]}[_NIGHT] == 1


async def test_incident_time_of_day_counts_each_case_once(monkeypatch):
    """A FIR whose Incident is reachable twice must not be double-counted."""
    monkeypatch.setattr(xagg, "age_client", _TimeOfDayAgeClient(2, [
        _incident("2026-01-01T19:00:00Z", "fir-1-26"),
        _incident("2026-01-01T19:00:00Z", "fir-1-26"),
        _incident("2026-01-02T09:00:00Z", "fir-2-26"),
    ]))

    r = await xagg._incident_time_of_day()

    assert r["with_datetime_count"] == 2
    assert r["with_clock_time_count"] == 2


async def test_incident_time_of_day_reports_unparsed_values_rather_than_dropping_them(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", _TimeOfDayAgeClient(3, [
        _incident("2026-01-01T19:00:00Z", "fir-1-26"),
        _incident("not-a-timestamp", "fir-2-26"),
    ]))

    r = await xagg._incident_time_of_day()

    assert r["unparsed_count"] == 1
    assert r["with_clock_time_count"] == 1
    rendered = "\n".join(xagg.render_incident_time_of_day(r))
    assert "1 incident date/time value(s) could not be parsed" in rendered


async def test_incident_time_of_day_honours_the_jurisdiction_allow_list(monkeypatch):
    fake = _TimeOfDayAgeClient(1, [_incident("2026-01-01T19:00:00Z", "fir-1-26")])
    monkeypatch.setattr(xagg, "age_client", fake)

    await xagg._incident_time_of_day(jurisdiction_case_ids=["fir-1-26"])

    assert len(fake.queries) == 2
    for query, params in fake.queries:
        assert "$case_ids" in query
        assert params["case_ids"] == ["fir-1-26"]


async def test_incident_time_of_day_with_only_date_only_rows_says_so(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", _TimeOfDayAgeClient(2, [
        _incident("2026-01-01T00:00:00Z", "fir-1-26"),
    ]))

    r = await xagg._incident_time_of_day()

    assert r["with_clock_time_count"] == 0
    rendered = "\n".join(xagg.render_incident_time_of_day(r))
    assert "no incident in this corpus records a clock time" in rendered.lower()


def test_render_incident_time_of_day_states_coverage_and_the_midnight_rule():
    rendered = "\n".join(xagg.render_incident_time_of_day({
        "kind": "incident_time_of_day", "total_incidents": 73,
        "with_datetime_count": 64, "date_only_count": 14, "unparsed_count": 0,
        "with_clock_time_count": 50,
        "buckets": [
            {"band": _NIGHT, "count": 1, "share": 1 / 50},
            {"band": _MORNING, "count": 14, "share": 14 / 50},
            {"band": _AFTERNOON, "count": 16, "share": 16 / 50},
            {"band": _EVENING, "count": 19, "share": 19 / 50},
        ],
        "naive_bucket_counts": [
            {"band": _NIGHT, "count": 15}, {"band": _MORNING, "count": 14},
            {"band": _AFTERNOON, "count": 16}, {"band": _EVENING, "count": 19},
        ],
        "hour_histogram": [],
        "peak_band": {"band": _EVENING, "count": 19, "share": 19 / 50},
    }))

    assert "evening (18:00-23:59): 19 (~38%)" in rendered
    assert "busiest band is evening" in rendered
    assert "64 of 73 incidents record an incident date/time" in rendered
    # Gold G1 reads this data as "fairly flat across the day". That reading
    # only holds if the date-only rows are counted as real midnights, and
    # the answer has to say so rather than quietly agree or quietly differ.
    assert "14 of those record exactly 00:00:00" in rendered
    assert "overnight is close to empty" in rendered


async def test_g1_time_of_day_sub_query_no_longer_dumps_the_whole_corpus(monkeypatch):
    """THE REGRESSION PINNED TO G1's LITERAL TIMING SUB-QUERY. Measured
    before this module (2026-09-08): this exact string returned
    `kind="case_listing"` — every case in the corpus, unfiltered — because
    "across all cases" contains the literal `_LIST_ALL_KEYWORDS` entry "all
    cases"."""
    monkeypatch.setattr(xagg, "age_client", _TimeOfDayAgeClient(1, [
        _incident("2026-01-01T19:00:00Z", "fir-1-26"),
    ]))

    result = await xagg.run_aggregate(
        _G1_SQ_TIME_OF_DAY, None,
        gateway=FakeGateway([{"case_id": "fir-9-26"}]), user_role="supervisor",
    )

    assert result["kind"] == "incident_time_of_day"
    assert result["kind"] != "case_listing"
    assert result["kind"] != "graph_recurrence"


async def test_m7_still_reaches_the_reporting_speed_aggregate_after_module_34(monkeypatch):
    """M7 is the other time-shaped question in this chain — elapsed time,
    not clock time. Its branch is checked first, structurally."""
    class _AC:
        async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
            return []

    monkeypatch.setattr(xagg, "age_client", _AC())

    result = await xagg.run_aggregate(
        "kya log 2026 mein waqiaat ki police ko itni hi jaldi ittila de "
        "rahe hain jitni 2024 mein dete the?",
        None, gateway=FakeGateway([]), user_role="supervisor",
    )

    assert result["kind"] == "time_bucketed_mean"


class TestIncidentTimeOfDayBoundary:
    """[Gold-QA fix — Module 34] The keyword family at its edges."""

    def test_the_g1_time_of_day_sub_query_matches(self):
        assert xagg._matches_any(_G1_SQ_TIME_OF_DAY.lower(), xagg._TIME_OF_DAY_KEYWORDS)

    @pytest.mark.parametrize("paraphrase", [
        # The required non-gold paraphrases — no phrase shared with the
        # dispatched sub-query above.
        "Do most of our crimes happen at night or during the day?",
        "Give me the hourly spread of incidents.",
        "واقعات دن کے کس وقت زیادہ ہوتے ہیں؟",
    ])
    def test_non_gold_paraphrases_match(self, paraphrase):
        assert xagg._matches_any(paraphrase.lower(), xagg._TIME_OF_DAY_KEYWORDS)

    @pytest.mark.parametrize("other", [
        # M7 — elapsed time between incident and report, not clock time.
        "How long does it typically take someone to report a crime to us "
        "these days versus a couple of years ago?",
        # M1 — a year-over-year case-mix comparison.
        "What kinds of cases are we dealing with now compared to a couple of "
        "years back?",
        # A7 — reporting-delay REASONS.
        "How many FIRs recorded a reason for a reporting delay?",
        "Give me the list of all cases.",
    ])
    def test_neighbouring_families_are_not_captured(self, other):
        assert not xagg._matches_any(other.lower(), xagg._TIME_OF_DAY_KEYWORDS)

    def test_matches_no_gold_question_at_all(self):
        """The all-32 negative control, same discipline as
        `TestOffenderAgeProfileBoundary`'s. This family needed the most
        tuning to pass it: the bare Urdu "رات" is a substring of "کراتا"
        (CR6) and the bare "شام" of "شامل" (KB5), so only the bound forms
        are matched. Reads
        `evaluation/Gold_QA_Dataset_Final32_With_Answers.json` — the bare
        `Gold_QA_Dataset_Final32.json` is NOT tracked in this repo, and a
        test pinned to it silently skips (PR #21)."""
        import json
        from pathlib import Path

        gold_path = (
            Path(__file__).resolve().parent.parent
            / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
        )
        assert gold_path.exists(), gold_path
        items = json.loads(gold_path.read_text(encoding="utf-8"))
        assert len(items) == 32
        matched = [
            (it.get("id") or "").upper()
            for it in items
            if xagg._matches_any(it["question"].lower(), xagg._TIME_OF_DAY_KEYWORDS)
        ]
        assert matched == [], f"expected no gold question to match, got {matched}"

    @pytest.mark.parametrize("bare, gold_word", [("رات", "کراتا"), ("شام", "شامل")])
    def test_the_two_urdu_substring_collisions_stay_excluded(self, bare, gold_word):
        """Pinned so a later widening cannot silently reintroduce them."""
        assert bare in gold_word
        assert bare not in xagg._TIME_OF_DAY_KEYWORDS


# ── [Gold-QA fix — Modules 31-34] the whole G1 plan, end to end ────────────

async def test_every_current_g1_plan_sub_query_still_reaches_its_own_aggregate(monkeypatch):
    """REGRESSION GUARD for the five sub-queries `meta_analysis.py`'s
    `caseload_review` plan emits TODAY. Four new keyword families were
    inserted into `run_aggregate()`'s ordered, first-match-wins chain; this
    is the test that proves none of them stole an existing dispatch.

    Pinned to the literal strings, copied from
    `harness/agents/meta_analysis.py`'s `_SQ_*` constants — that file is
    owned by another track this wave and is deliberately NOT imported, so
    a drift between the two is a test failure here rather than a silent
    live regression."""
    class _AC:
        async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
            if "count(" in cypher_query:
                return [{"n": 0}]
            return []

    monkeypatch.setattr(xagg, "age_client", _AC())
    gateway = FakeGateway([{"case_id": "fir-1-26", "incident_date": None}])

    expected = {
        "How many cases have incomplete or missing record fields, across all "
        "cases?": "case_completeness_scan",
        "How many cases does each accused person appear in, and which FIR "
        "numbers, across all cases?": "graph_recurrence",
        "How many cases involve a recovered weapon with no licence recorded, "
        "across all cases?": "weapon_compliance_scan",
        "What kinds of cases are we dealing with now compared to a couple of "
        "years back?": "time_bucketed_breakdown",
        "How many cases in the criminal record system have a court outcome "
        "that matches the recorded conviction status, across all cases?":
            "criminal_record_court_crosscheck",
    }
    for sub_query, kind in expected.items():
        result = await xagg.run_aggregate(
            sub_query, None, gateway=gateway, user_role="supervisor",
        )
        assert result["kind"] == kind, (sub_query, result["kind"])


async def test_the_four_new_g1_sub_queries_each_reach_their_own_aggregate(monkeypatch):
    """The mirror of the test above: each of Modules 31-34's sub-queries
    reaches ITS aggregate and no other. Together the two tests are the
    proof that the ordered chain still partitions correctly."""
    class _AC:
        async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
            if "count(" in cypher_query:
                return [{"n": 0}]
            if "RELATED_TO" in cypher_query:
                return [_rel("اجنبی", "fir-1-26", 1, 90)]
            if "malkhana_register" in cypher_query:
                return [_malkhana(_MALKHANA_FORENSIC, "fir-1-26")]
            if "incident_datetime" in cypher_query:
                return [_incident("2026-01-01T19:00:00Z", "fir-1-26")]
            if "r.role = 'accused'" in cypher_query:
                return _age_rows([("P-1", 31, "fir-1-26")])
            return []

    monkeypatch.setattr(xagg, "age_client", _AC())
    gateway = FakeGateway([{"case_id": "fir-1-26"}])

    expected = {
        _G1_SQ_ACCUSED_AGE: "offender_age_profile",
        _G1_SQ_RELATIONSHIP: "accused_relationship_breakdown",
        _G1_SQ_SEIZED_PROPERTY: "seized_property_disposition",
        _G1_SQ_TIME_OF_DAY: "incident_time_of_day",
    }
    for sub_query, kind in expected.items():
        result = await xagg.run_aggregate(
            sub_query, None, gateway=gateway, user_role="supervisor",
        )
        assert result["kind"] == kind, (sub_query, result["kind"])
        # The specific bug Modules 31-34 exist to kill.
        assert result["kind"] != "graph_recurrence"
        assert result["kind"] != "case_listing"


def test_the_four_new_keyword_families_are_mutually_exclusive_on_their_own_sub_queries():
    """No sub-query may match more than one of the four new families —
    otherwise the answer it gets would depend on chain order alone."""
    families = {
        "age": xagg._AGE_KEYWORDS,
        "relationship": xagg._RELATIONSHIP_KEYWORDS,
        "seized_property": xagg._SEIZED_PROPERTY_KEYWORDS,
        "time_of_day": xagg._TIME_OF_DAY_KEYWORDS,
    }
    sub_queries = {
        "age": _G1_SQ_ACCUSED_AGE,
        "relationship": _G1_SQ_RELATIONSHIP,
        "seized_property": _G1_SQ_SEIZED_PROPERTY,
        "time_of_day": _G1_SQ_TIME_OF_DAY,
    }
    for owner, text in sub_queries.items():
        matched = [
            name for name, keywords in families.items()
            if xagg._matches_any(text.lower(), keywords)
        ]
        assert matched == [owner], (text, matched)


def test_each_new_g1_sub_query_deterministically_routes_to_xagg():
    """[Gold-QA fix — Modules 31-34] The half a keyword family alone cannot
    prove. A sub-query only reaches `run_aggregate()` at all if
    `router.py::_deterministic_route_override()` sends it to XAGG first —
    Module 29 phrased every one of its sub-queries for exactly that, and
    said so in `meta_analysis.py`'s own comment ("verified live: all 9
    return det=Y route=XAGG").

    CAUGHT LIVE, not in review. The first drafts of these four strings began
    "What relationship is recorded...", "What happens to seized
    property...", "At what time of day...". Sent through `/api/chat` on
    2026-09-08 all three came back `route='XGRAPH'` — `_XGRAPH_OVERRIDE_
    PATTERNS`' `across.{0,15}cases` matched the "across all cases"
    suffix and won, so the new aggregates were never reached and the answer
    was a cross-case traversal that answered nothing. They were rephrased to
    lead with "How many cases ...", which `_XAGG_OVERRIDE_PATTERNS` matches
    outright, rather than by widening router.py (owned by another track).

    This test is what stops a future reword from silently undoing that."""
    from src.pipeline import router

    for sub_query in (
        _G1_SQ_ACCUSED_AGE, _G1_SQ_RELATIONSHIP,
        _G1_SQ_SEIZED_PROPERTY, _G1_SQ_TIME_OF_DAY,
        # [Gold-QA fix — Module 35] G6's arrest-rate sub-query, phrased to
        # the same "How many cases ..." rule for the same reason.
        _G6_SQ_ARREST_RATE,
        # [Gold-QA fix — Module 36] CR3's FIR-listing sub-query, same rule.
        _CR3_SQ_FIR_LISTING,
    ):
        override = router._deterministic_route_override(sub_query)
        assert override is not None, sub_query
        assert override["route"] == "XAGG", (sub_query, override["route"])


def test_every_new_aggregate_kind_is_accepted_by_the_harness_tool_result():
    """[Gold-QA fix — Modules 31-34] `XAggToolResult.aggregate_kind` is a
    hand-maintained `Literal` in `harness/tools/xagg.py`, NOT derived from
    `run_aggregate()`. A kind missing from it raises a Pydantic
    `literal_error` the instant XAGG reaches the harness wrapper, and
    main.py's top-level handler swallows it into an EMPTY answer with no
    user-visible error — which is exactly how Module 31 first failed live
    while every unit test passed. Four earlier modules hit this same trap;
    this test closes it for good."""
    from typing import get_args

    from src.pipeline.harness.tools.xagg import AggregateKind

    accepted = set(get_args(AggregateKind))
    for kind in (
        "offender_age_profile", "accused_relationship_breakdown",
        "seized_property_disposition", "incident_time_of_day",
        # [Gold-QA fix — Module 35] fifth family to depend on this Literal.
        "arrest_rate",
        # [Gold-QA fix — Module 36] sixth.
        "filtered_fir_listing",
    ):
        assert kind in accepted, kind


# ── [Gold-QA fix — Module 35, question G6] arrest rate ──────────────────────
#
# The literal G6 sub-question this family exists to answer, in the same
# "..., across all cases?" shape as `meta_analysis.py`'s other sub-queries,
# and leading with "How many cases" for the reason
# `test_each_new_g1_sub_query_deterministically_routes_to_xagg` records.
_G6_SQ_ARREST_RATE = (
    "How many cases record an arrest of an accused person, and on how many "
    "is no arrest recorded, across all cases?"
)
_S3_GOLD_TEXT = "کیا کسی شخص کو ایک سے زیادہ بار گرفتار کیا گیا ہے؟"


class _ArrestAgeClient:
    """Routes the two reads `_arrest_rate()` issues: the accused roster and
    the Case roster (the rate's denominator)."""

    def __init__(self, accused_rows, case_rows):
        self.accused_rows = accused_rows
        self.case_rows = case_rows
        self.queries = []

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        self.queries.append((cypher_query, params))
        if "INVOLVED_IN" in cypher_query:
            return self.accused_rows
        return self.case_rows


def _accused_arrest(status, case_id, p_id):
    return {"arrest_status": status, "case_id": case_id, "p_id": p_id}


def _cases(*case_ids):
    return [{"case_id": c} for c in case_ids]


# The four measured live status values (2026-09-08) that a naive substring
# rule gets wrong, pinned individually so the classification rule cannot
# drift silently.
class TestArrestStatusClassification:
    def test_bare_arrest_token_is_an_arrest(self):
        assert xagg._classify_arrest_status("گرفتار") == "arrested"

    @pytest.mark.parametrize("status", [
        "موقع پر گرفتار",
        "گرفتار، بعد ازاں سزا یافتہ",
        "گرفتار، ڈی این اے مطابقت پر",
    ])
    def test_a_qualifier_does_not_stop_it_being_an_arrest(self, status):
        assert xagg._classify_arrest_status(status) == "arrested"

    @pytest.mark.parametrize("status", [
        # BOTH of these CONTAIN گرفتار and mean the opposite of an arrest.
        # This is the trap the whole module is about.
        "تاحال مفرور، گرفتار نہیں ہوا",
        "نامزد، گرفتاری کی نوبت نہ آئی",
    ])
    def test_a_negated_arrest_is_not_an_arrest(self, status):
        assert xagg._classify_arrest_status(status) == "not_arrested_explicit"

    def test_an_arrest_in_an_earlier_case_gets_its_own_bucket(self):
        assert xagg._classify_arrest_status(
            "پہلے سے کیس 10 میں گرفتار، اس مقدمے میں بھی نامزد"
        ) == "arrested_in_another_case"

    @pytest.mark.parametrize("status", [
        "زیر تفتیش",
        "مفرور، اشتہاری کارروائی جاری",
        "مقام معلوم کرنے کی کارروائی جاری",
        "نامزد، تفتیش جاری",
        # "already in custody" carries no arrest token at all — custody is
        # not an arrest record, and the rule does not infer one.
        "پہلے سے زیر حراست، اس مقدمے میں بھی نامزد",
        "",
        None,
    ])
    def test_everything_else_records_no_arrest(self, status):
        assert xagg._classify_arrest_status(status) == "no_arrest_recorded"


async def test_arrest_rate_counts_firs_not_accused_entries(monkeypatch):
    """Two accused arrested on the SAME FIR is one arrest FIR, not two.
    Conflating the two denominators is the easiest way to report a wrong
    rate here — the live corpus has 14 arrested ENTRIES across 11 FIRs."""
    monkeypatch.setattr(xagg, "age_client", _ArrestAgeClient(
        accused_rows=[
            _accused_arrest("گرفتار", "fir-1-26", 1),
            _accused_arrest("موقع پر گرفتار", "fir-1-26", 2),
            _accused_arrest("گرفتار", "fir-2-26", 3),
            _accused_arrest("زیر تفتیش", "fir-3-26", 4),
        ],
        case_rows=_cases("fir-1-26", "fir-2-26", "fir-3-26", "fir-4-26"),
    ))

    result = await xagg._arrest_rate()

    assert result["kind"] == "arrest_rate"
    assert result["arrest_entry_count"] == 3
    assert result["arrest_fir_count"] == 2
    assert result["fir_count"] == 4
    assert result["one_in"] == 2.0
    assert result["no_arrest_fir_count"] == 2


async def test_arrest_rate_denominator_includes_firs_with_no_accused(monkeypatch):
    """5 of the live corpus's 73 Cases carry no accused entry at all. They
    are FIRs on which no arrest is recorded, so they belong in the
    denominator; both readings are returned so the choice is checkable."""
    monkeypatch.setattr(xagg, "age_client", _ArrestAgeClient(
        accused_rows=[_accused_arrest("گرفتار", "fir-1-26", 1)],
        case_rows=_cases("fir-1-26", "fir-2-26", "fir-3-26", "fir-4-26"),
    ))

    result = await xagg._arrest_rate()

    assert result["fir_count"] == 4
    assert result["fir_count_with_accused"] == 1
    assert result["one_in"] == 4.0
    assert result["one_in_accused_firs"] == 1.0


async def test_arrest_rate_does_not_count_a_negated_status_as_an_arrest(monkeypatch):
    """The module's headline trap: both negations CONTAIN گرفتار."""
    monkeypatch.setattr(xagg, "age_client", _ArrestAgeClient(
        accused_rows=[
            _accused_arrest("تاحال مفرور، گرفتار نہیں ہوا", "fir-1-26", 1),
            _accused_arrest("نامزد، گرفتاری کی نوبت نہ آئی", "fir-2-26", 2),
            _accused_arrest("گرفتار", "fir-3-26", 3),
        ],
        case_rows=_cases("fir-1-26", "fir-2-26", "fir-3-26"),
    ))

    result = await xagg._arrest_rate()

    assert result["arrest_fir_count"] == 1
    assert result["explicit_no_arrest_fir_count"] == 2
    # What a naive substring rule would have reported instead.
    assert result["naive_substring_fir_count"] == 3


async def test_arrest_rate_publishes_the_bare_token_reading_for_comparison(monkeypatch):
    """Gold G6's "1 in 9" is reproducible ONLY by an exact-string match on a
    bare گرفتار, which discards three entries that record an arrest in as
    many words. That reading is returned for comparison and is never the
    headline — this test pins the distinction so it cannot be quietly
    swapped in to make gold match."""
    monkeypatch.setattr(xagg, "age_client", _ArrestAgeClient(
        accused_rows=[
            _accused_arrest("گرفتار", "fir-1-26", 1),
            _accused_arrest("موقع پر گرفتار", "fir-2-26", 2),
        ],
        case_rows=_cases("fir-1-26", "fir-2-26", "fir-3-26", "fir-4-26"),
    ))

    result = await xagg._arrest_rate()

    assert result["arrest_fir_count"] == 2
    assert result["bare_token_fir_count"] == 1
    assert result["bare_token_one_in"] == 4.0
    assert result["one_in"] == 2.0


async def test_arrest_rate_renderer_states_the_counting_rule(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", _ArrestAgeClient(
        accused_rows=[
            _accused_arrest("گرفتار", "fir-1-26", 1),
            _accused_arrest("تاحال مفرور، گرفتار نہیں ہوا", "fir-2-26", 2),
            _accused_arrest("زیر تفتیش", "fir-3-26", 3),
        ],
        case_rows=_cases("fir-1-26", "fir-2-26", "fir-3-26"),
    ))

    text = "\n".join(xagg.render_arrest_rate(await xagg._arrest_rate()))

    assert "Counting rule:" in text
    assert "گرفتار نہیں ہوا" in text
    assert "mean the opposite" in text
    assert "1 of 3 FIR(s)" in text


async def test_arrest_rate_renderer_is_bounded(monkeypatch):
    """Module 29 reverted an unfiltered 73-row listing because rendering it
    starved its sibling sub-queries into the 60 s timeout. Every listing
    this file renders is capped for that reason."""
    monkeypatch.setattr(xagg, "age_client", _ArrestAgeClient(
        accused_rows=[
            _accused_arrest(f"زیر تفتیش {i}", f"fir-{i}-26", i) for i in range(40)
        ],
        case_rows=_cases(*[f"fir-{i}-26" for i in range(40)]),
    ))

    lines = xagg.render_arrest_rate(await xagg._arrest_rate())

    status_lines = [ln for ln in lines if ln.startswith("  - ") and "entries [" in ln]
    assert len(status_lines) == xagg._ARREST_STATUS_RENDER_LIMIT
    assert any("further status value(s)" in ln for ln in lines)


async def test_arrest_rate_says_so_when_no_accused_is_recorded(monkeypatch):
    monkeypatch.setattr(xagg, "age_client", _ArrestAgeClient(
        accused_rows=[], case_rows=_cases("fir-1-26"),
    ))

    text = "\n".join(xagg.render_arrest_rate(await xagg._arrest_rate()))

    assert "No accused person is recorded" in text


async def test_g6_arrest_sub_query_reaches_the_arrest_rate_not_person_recurrence(monkeypatch):
    """[Gold-QA fix — Module 35] The regression pinned to the LITERAL
    dispatched sub-query text.

    Measured pre-fix on 2026-09-08 (`scratchpad/dispatch.py`, live stack):

        arrest -> graph_recurrence / Person

    i.e. a ranked list of repeat offenders — "فیصل and طارق both appear in
    fir-202-26 and fir-401-26" — presented as the answer to a question about
    arrests. The plan predicted exactly this fall-through and it reproduced.
    """
    monkeypatch.setattr(xagg, "age_client", _ArrestAgeClient(
        accused_rows=[_accused_arrest("گرفتار", "fir-1-26", 1)],
        case_rows=_cases("fir-1-26", "fir-2-26"),
    ))

    result = await xagg.run_aggregate(
        _G6_SQ_ARREST_RATE, None, gateway=FakeGateway([]), user_role="supervisor",
    )

    assert result["kind"] == "arrest_rate"
    assert result["kind"] != "graph_recurrence"
    assert result["kind"] != "case_listing"


class TestArrestRateBoundary:
    """
    [Gold-QA fix — Module 35] `_is_arrest_rate()` sits ABOVE
    `_PERSON_KEYWORDS` in `run_aggregate()`'s chain, and its bare vocabulary
    collides with a gold question OUTRIGHT — S3 contains گرفتار. That is why
    this family is a three-signal predicate rather than a keyword tuple, and
    why the negative control below is an equality.
    """

    def test_the_g6_arrest_sub_query_matches(self):
        assert xagg._is_arrest_rate(_G6_SQ_ARREST_RATE.lower())

    @pytest.mark.parametrize("paraphrase", [
        # The required non-gold paraphrases — no phrase shared with the
        # dispatched sub-query above.
        "What share of our FIRs actually end in someone being taken into custody?",
        "How often do we actually arrest anyone?",
        "Kitne FIRs mein mulzim giraftar hua?",
        "کتنی ایف آئی آر میں ملزم گرفتار ہوا؟",
    ])
    def test_non_gold_paraphrases_match(self, paraphrase):
        assert xagg._is_arrest_rate(paraphrase.lower())

    def test_s3_the_repeat_arrest_gold_question_is_not_captured(self):
        """S3 CONTAINS گرفتار and is a person-RECURRENCE question answered
        correctly today by `_top_recurring_nodes("Person")`. Both guards are
        asserted separately so a future edit that drops either one fails
        here rather than live."""
        lowered = _S3_GOLD_TEXT.lower()
        assert xagg._matches_any(lowered, xagg._ARREST_TERMS), (
            "S3 does contain the arrest vocabulary — that is the whole point"
        )
        assert xagg._matches_any(lowered, xagg._ARREST_RECURRENCE_EXCLUSIONS)
        assert not xagg._matches_any(lowered, xagg._ARREST_RATE_SIGNALS)
        assert not xagg._is_arrest_rate(lowered)

    @pytest.mark.parametrize("other", [
        # Neighbouring families this must not swallow, given where it sits.
        "How many cases involve an accused person, and what is their age "
        "range and average age, across all cases?",
        "How many of the accused are men and how many are women, across all cases?",
        "How many accused persons are there in total?",
        "Has anyone been arrested more than once?",
        "Kya koi shakhs ek se zyada baar giraftar hua hai?",
    ])
    def test_neighbouring_families_are_not_captured(self, other):
        assert not xagg._is_arrest_rate(other.lower())

    def test_matches_no_gold_question_at_all(self):
        """The all-32 negative control, asserted as an EQUALITY.

        Reads `evaluation/Gold_QA_Dataset_Final32_With_Answers.json` — the
        bare `Gold_QA_Dataset_Final32.json` is NOT tracked in this repo, and
        a test pinned to it silently skips (PR #21)."""
        import json
        from pathlib import Path

        gold_path = (
            Path(__file__).resolve().parent.parent
            / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
        )
        assert gold_path.exists(), gold_path
        items = json.loads(gold_path.read_text(encoding="utf-8"))
        assert len(items) == 32
        matched = [
            (it.get("id") or "").upper()
            for it in items
            if xagg._is_arrest_rate(it["question"].lower())
        ]
        assert matched == [], f"expected no gold question to match, got {matched}"

    def test_the_arrest_vocabulary_alone_would_have_matched_s3(self):
        """The negative control above is only meaningful because the naive
        version of this family FAILS it. Pinned so nobody 'simplifies'
        `_is_arrest_rate()` back into a bare keyword tuple."""
        import json
        from pathlib import Path

        gold_path = (
            Path(__file__).resolve().parent.parent
            / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
        )
        items = json.loads(gold_path.read_text(encoding="utf-8"))
        matched = [
            (it.get("id") or "").upper()
            for it in items
            if xagg._matches_any(it["question"].lower(), xagg._ARREST_TERMS)
        ]
        assert matched == ["S3"], matched


async def test_s3_still_reaches_person_recurrence_after_module_35(monkeypatch):
    """End to end with S3's LITERAL gold text, not at the predicate level —
    the guard Module 33 used for G5, for the same reason: this family sits
    above `_PERSON_KEYWORDS` and S3 is the question it could break."""
    rows = [
        {"n": _node("P-004", "Person", canonical_name="شہزیب"), "c": _case("fir-214-26")},
        {"n": _node("P-004", "Person", canonical_name="شہزیب"), "c": _case("fir-891-24")},
    ]
    monkeypatch.setattr(xagg, "age_client", FakeAgeClient(rows))

    result = await xagg.run_aggregate(
        _S3_GOLD_TEXT, None, gateway=FakeGateway([]), user_role="supervisor",
    )

    assert result["kind"] == "graph_recurrence"
    assert result["entity_type"] == "Person"


# ── [Gold-QA fix — Module 36, question CR3] subject-filtered FIR listing ────
#
# The literal sub-question this family exists to answer. Leads with "How
# many cases ..." for exactly the reason
# `test_each_new_g1_sub_query_deterministically_routes_to_xagg` records —
# `_XGRAPH_OVERRIDE_PATTERNS`' `across.{0,15}cases` would otherwise steal it.
_CR3_SQ_FIR_LISTING = (
    "How many cases are registered under the cybercrime act at a cyber "
    "crime circle station, and what are their FIR numbers and current status?"
)
# S2's literal gold text. It is the question this family is most at risk of
# swallowing — "which" + "police station" — and it is answered correctly
# today by the grouped station count, so it is asserted end to end below.
_S2_GOLD_TEXT = "Which police station handles the most cases?"


def _fir_row(case_id, fir_number, station, acts, status=None):
    return {
        "case_id": case_id,
        "fir_number": fir_number,
        "police_station": station,
        "crime_category": acts,
        "investigation_status": status,
    }


_CYBER_ISB = "سائبر کرائم سرکل، اسلام آباد"
_CYBER_RWP = "سائبر کرائم سرکل، راولپنڈی"


def _cr3_corpus():
    """The shape of the live corpus this family was probed against on
    2026-09-08 — 9 PECA 2016 cases, 9 cases at a سائبر کرائم سرکل station,
    intersecting in exactly `fir-64-26` and `fir-65-26`. Real values, so a
    change in the Urdu station strings breaks the test rather than passing
    on a sanitised English stand-in."""
    return [
        # PECA 2016, elsewhere.
        _fir_row("fir-208-26", "208/26", "تھانہ ماڈل ٹاؤن، لاہور", "PECA 2016, PPC"),
        _fir_row("fir-417-26", "417/26", "تھانہ شاہ فیصل کالونی، کراچی", "PECA 2016, PPC"),
        _fir_row("fir-418-26", "418/26", "تھانہ راجہ بازار، راولپنڈی", "PECA 2016, PPC"),
        _fir_row("fir-427-26", "427/26", "موٹروے پولیس اسٹیشن ایم ٹو، لاہور", "PECA 2016, PPC"),
        _fir_row("fir-428-26", "428/26", "تھانہ کوتوالی، فیصل آباد", "PECA 2016, PPC"),
        _fir_row("fir-453-26", "453/26", "تھانہ جھنگ روڈ، فیصل آباد", "PECA 2016, PPC"),
        _fir_row("fir-462-26", "462/26", "تھانہ نیو کراچی، کراچی", "PECA 2016, PPC"),
        # The CR3 pair — PECA 2016 AND a cyber-crime circle.
        _fir_row("fir-64-26", "64/26", _CYBER_ISB, "PECA 2016, PPC",
                 "ملزم جسمانی ریمانڈ پر، مزید برآمدگی کے لیے"),
        _fir_row("fir-65-26", "65/26", _CYBER_RWP, "PECA 2016, PPC",
                 "ملزم جسمانی ریمانڈ پر، مقدمہ نمبر 10 کا وہی ملزم"),
        # Cyber-crime circle, but NOT PECA.
        _fir_row("fir-1001-26", "1001/26", _CYBER_ISB, "PPC, Arms Ordinance 1965"),
        _fir_row("fir-204-26", "204/26", _CYBER_RWP, "PPC, Arms Ordinance 1965"),
        _fir_row("fir-410-26", "410/26", _CYBER_RWP, "PPC, Arms Ordinance 1965"),
        _fir_row("fir-421-26", "421/26", _CYBER_ISB, "PPC, Arms Ordinance 1965"),
        _fir_row("fir-424-26", "424/26", _CYBER_ISB, "CNSA 1997"),
        _fir_row("fir-435-26", "435/26", _CYBER_RWP, "PPC"),
        _fir_row("fir-457-26", "457/26", _CYBER_ISB, "PPC"),
        # Neither.
        _fir_row("fir-301-26", "301/26", "تھانہ نیو کراچی، کراچی",
                 "CNSA 1997, Arms Ordinance 1965", "دونوں ملزمان ریمانڈ پر"),
        _fir_row("fir-118-26", "118/26", "تھانہ لطیف آباد، حیدر آباد", "CNSA 1997"),
    ]


async def test_cr3_sub_query_returns_exactly_the_two_ground_truth_firs():
    """[Gold-QA fix — Module 36] The regression pinned to the LITERAL
    dispatched sub-query text.

    Ground truth, probed live 2026-09-08 before this aggregate was written:
    PECA 2016 ∩ سائبر کرائم سرکل = `fir-64-26` (64/26, Islamabad) and
    `fir-65-26` (65/26, Rawalpindi), and nothing else.

    Measured pre-fix (`scratchpad/dispatch.py`, live stack): this sub-query
    returned `relational_aggregate` grouped by `police_station` — nine PECA
    cases counted per station, with NO FIR number anywhere in the result and
    the station filter never applied at all."""
    result = await xagg.run_aggregate(
        _CR3_SQ_FIR_LISTING, None,
        gateway=FakeGateway(_cr3_corpus()), user_role="supervisor",
    )

    assert result["kind"] == "filtered_fir_listing"
    assert result["filtered"] is True
    assert result["matched_count"] == 2
    assert [r["fir_number"] for r in result["cases"]] == ["64/26", "65/26"]
    assert [r["case_id"] for r in result["cases"]] == ["fir-64-26", "fir-65-26"]
    assert any(f.startswith("statute: PECA 2016") for f in result["filters_applied"])
    assert any(f.startswith("station: ") for f in result["filters_applied"])


async def test_cr3_listing_renders_the_fir_number_and_the_status():
    """The whole point of the module: Module 29's sub-answers carried the
    facts but never said WHICH FIRs, so synthesis had to infer the pair —
    and once paired `fir-64-26` with the wrong FIR entirely."""
    result = await xagg.run_aggregate(
        _CR3_SQ_FIR_LISTING, None,
        gateway=FakeGateway(_cr3_corpus()), user_role="supervisor",
    )

    text = "\n".join(xagg.render_filtered_fir_listing(result))

    assert "64/26" in text and "65/26" in text
    assert "ملزم جسمانی ریمانڈ پر، مزید برآمدگی کے لیے" in text
    assert "ملزم جسمانی ریمانڈ پر، مقدمہ نمبر 10 کا وہی ملزم" in text
    # Nothing outside the intersection leaks in.
    assert "208/26" not in text and "1001/26" not in text


async def test_a_station_only_question_is_not_silently_narrowed_to_peca():
    """[Gold-QA fix — Module 36] The substring collision this module paid
    for, CAUGHT LIVE on 2026-09-08 against the real corpus, not in review.

    PECA 2016's keyword tuple contains `"cyber crime"`; the station alias is
    `"cyber crime circle"`. Before `_mask_station_aliases()`, this question —
    which names NO statute — silently acquired a `statute: PECA 2016` filter
    and answered 2 FIRs where the corpus holds 9 cyber-circle FIRs. Same
    class as this module's four existing Urdu collisions."""
    result = await xagg.run_aggregate(
        "Which FIR numbers are registered at a cyber crime circle station?",
        None, gateway=FakeGateway(_cr3_corpus()), user_role="supervisor",
    )

    assert result["kind"] == "filtered_fir_listing"
    assert result["filters_applied"] == [
        f"station: {_CYBER_ISB}, {_CYBER_RWP}"
    ], result["filters_applied"]
    assert result["matched_count"] == 9
    assert "fir-64-26" in {r["case_id"] for r in result["cases"]}
    assert "fir-1001-26" in {r["case_id"] for r in result["cases"]}


def test_mask_station_aliases_keeps_a_statute_the_question_really_names():
    """The mask must not swing the other way and disarm CR3's own statute
    filter: the sub-query says "under the CYBERCRIME act at a CYBER CRIME
    CIRCLE station", so the one-word form survives the phrase removal."""
    masked = xagg._mask_station_aliases(_CR3_SQ_FIR_LISTING.lower())

    assert "cyber crime circle" not in masked
    assert "cybercrime" in masked
    assert xagg._matches_any(masked, xagg._LEGAL_CODE_ACT_KEYWORDS["PECA 2016"])


def test_mask_station_aliases_strips_every_occurrence():
    masked = xagg._mask_station_aliases(
        "cyber crime circle and another cyber crime circle"
    )
    assert "cyber crime circle" not in masked.lower()


async def test_urdu_question_naming_the_circle_by_its_real_name_is_answered():
    """Station names are Urdu-only in the data and neither of the two
    families that matter carries "تھانہ" at all, so `_STATION_KEYWORDS`
    alone missed this question entirely and it fell through to the
    grouped-count default — measured against the live corpus while writing
    this module."""
    result = await xagg.run_aggregate(
        "کون سی ایف آئی آر سائبر کرائم سرکل میں درج ہیں؟",
        None, gateway=FakeGateway(_cr3_corpus()), user_role="supervisor",
    )

    assert result["kind"] == "filtered_fir_listing"
    assert result["matched_count"] == 9


async def test_a_recognised_station_word_naming_no_real_station_refuses_to_dump():
    """Module 29 reverted the unfiltered 73-row listing because ~4.6 KB of
    generation starved its two concurrent siblings into the 60 s
    `META_ANALYSIS_SUBQUERY_TIMEOUT`. When the filter cannot be resolved this
    family reports the corpus size and the filters available instead of
    listing anything."""
    result = await xagg.run_aggregate(
        "Which FIR numbers are registered at the Chichawatni police station?",
        None, gateway=FakeGateway(_cr3_corpus()), user_role="supervisor",
    )

    assert result["kind"] == "filtered_fir_listing"
    assert result["filtered"] is False
    assert result["cases"] == []
    assert result["total_in_scope"] == len(_cr3_corpus())

    text = "\n".join(xagg.render_filtered_fir_listing(result))
    assert "needs a subject filter" in text
    assert xagg._UNRECOGNIZED_STATION in text
    assert "64/26" not in text


async def test_the_rendered_listing_is_capped_for_the_module_29_budget():
    corpus = _cr3_corpus() + [
        _fir_row(f"fir-9{i:02d}-26", f"9{i:02d}/26", "تھانہ برکی، لاہور", "CNSA 1997")
        for i in range(20)
    ]
    result = await xagg.run_aggregate(
        "Give me the FIR numbers for the narcotics cases and their status.",
        None, gateway=FakeGateway(corpus), user_role="supervisor",
    )
    lines = xagg.render_filtered_fir_listing(result)

    assert result["matched_count"] == 23
    listed = [ln for ln in lines if ln.startswith("  - FIR ")]
    assert len(listed) == xagg._FIR_LISTING_RENDER_LIMIT
    assert any("further matching FIR(s)" in ln for ln in lines)


async def test_a_filter_that_matches_nothing_says_so_rather_than_listing():
    result = await xagg.run_aggregate(
        "Which FIR numbers are registered under the Illegal Dispossession Act?",
        None, gateway=FakeGateway(_cr3_corpus()), user_role="supervisor",
    )

    assert result["matched_count"] == 0
    assert "No FIR matches" in "\n".join(xagg.render_filtered_fir_listing(result))


def test_station_matches_is_driven_by_the_stored_value_not_a_hardcoded_list():
    assert xagg._station_matches(_CYBER_ISB, "cyber crime circle station")
    assert xagg._station_matches(_CYBER_ISB, "سائبر کرائم سرکل کے مقدمات")
    assert xagg._station_matches(_CYBER_ISB, "cases in اسلام آباد")
    assert not xagg._station_matches(_CYBER_ISB, "cases at the women police station")
    # The generic prefix is stripped, so a bare "thana" question cannot
    # match every station in the corpus.
    assert not xagg._station_matches("تھانہ برکی، لاہور", "how many cases per thana")
    assert not xagg._station_matches(None, "anything")


class TestFilteredFirListingBoundary:
    """
    [Gold-QA fix — Module 36] This family sits above `_DISTRICT_KEYWORDS`,
    `_LIST_ALL_KEYWORDS`, `_TOTAL_KEYWORDS` and the
    `_station_or_category_counts()` fallback, and its FILTER half alone
    matches five gold questions. The IDENTIFICATION half is what keeps it
    off them, so the negative control below is an equality.
    """

    def test_the_cr3_sub_query_matches(self):
        assert xagg._is_filtered_fir_listing(_CR3_SQ_FIR_LISTING.lower())

    @pytest.mark.parametrize("paraphrase", [
        # The required non-gold paraphrases — no phrase shared with the
        # dispatched sub-query above.
        "Which FIRs are registered under the Arms Ordinance, and what are "
        "their FIR numbers and status?",
        "Give me the FIR numbers for the narcotics cases and where each one "
        "has got to.",
        "Name the FIRs booked at a cyber crime circle.",
        "کون سی ایف آئی آر سائبر کرائم سرکل میں درج ہیں؟",
        "منشیات کے مقدمات کے نمبر کیا ہیں؟",
    ])
    def test_non_gold_paraphrases_match(self, paraphrase):
        assert xagg._is_filtered_fir_listing(paraphrase.lower())

    def test_s2_the_station_ranking_gold_question_is_not_captured(self):
        """S2 is "Which police station handles the most cases?" — "which" +
        "police station", the exact shape this family would swallow if the
        identification half were the bare "which cases". Both halves are
        asserted separately so a future edit that drops either fails here
        rather than live."""
        lowered = _S2_GOLD_TEXT.lower()
        assert xagg._matches_any(
            lowered,
            xagg._STATION_KEYWORDS + xagg._STATION_NAME_HINTS,
        ), "S2 does carry the station filter signal — that is the point"
        assert not xagg._matches_any(lowered, xagg._FIR_IDENTIFICATION_KEYWORDS)
        assert not xagg._is_filtered_fir_listing(lowered)

    @pytest.mark.parametrize("other", [
        # Neighbouring families this must not swallow, given where it sits.
        "How many cases are registered at each police station?",
        "How many police stations are there in total?",
        "Which district recovers the most weapons relative to its caseload?",
        "List all cases",
        "How many cases are there in total?",
        "How many cases record an arrest of an accused person, and on how "
        "many is no arrest recorded, across all cases?",
    ])
    def test_neighbouring_families_are_not_captured(self, other):
        assert not xagg._is_filtered_fir_listing(other.lower())

    def test_an_identification_question_with_no_filter_is_declined(self):
        """Without a filter there is nothing to narrow to, and the answer
        would be the 73-row dump Module 29 reverted. It is left to the
        existing listing/count families instead."""
        assert not xagg._is_filtered_fir_listing("which fir numbers do we have?")

    def test_matches_no_gold_question_at_all(self):
        """The all-32 negative control, asserted as an EQUALITY.

        Reads `evaluation/Gold_QA_Dataset_Final32_With_Answers.json` — the
        bare `Gold_QA_Dataset_Final32.json` is NOT tracked in this repo, and
        a test pinned to it silently skips (PR #21)."""
        import json
        from pathlib import Path

        gold_path = (
            Path(__file__).resolve().parent.parent
            / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
        )
        assert gold_path.exists(), gold_path
        items = json.loads(gold_path.read_text(encoding="utf-8"))
        assert len(items) == 32
        matched = [
            (it.get("id") or "").upper()
            for it in items
            if xagg._is_filtered_fir_listing(it["question"].lower())
        ]
        assert matched == [], f"expected no gold question to match, got {matched}"

    def test_the_filter_half_alone_would_have_matched_five_gold_questions(self):
        """The negative control above is only meaningful because the naive
        version of this family FAILS it. Measured against the live gold set
        on 2026-09-08. Pinned so nobody 'simplifies' the identification half
        away."""
        import json
        from pathlib import Path

        gold_path = (
            Path(__file__).resolve().parent.parent
            / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"
        )
        items = json.loads(gold_path.read_text(encoding="utf-8"))
        signal = (
            xagg._STATION_KEYWORDS + xagg._DISTRICT_KEYWORDS
            + xagg._CATEGORY_KEYWORDS + xagg._STATION_NAME_HINTS
        )
        matched = sorted(
            (it.get("id") or "").upper()
            for it in items
            if xagg._matches_any(it["question"].lower(), signal)
            or any(
                xagg._matches_any(it["question"].lower(), kws)
                for kws in xagg._LEGAL_CODE_ACT_KEYWORDS.values()
            )
        )
        assert matched == ["CP1", "CR3", "CR8", "M2", "S2"], matched


async def test_s2_still_reaches_the_station_ranking_after_module_36():
    """End to end with S2's LITERAL gold text, not at the predicate level —
    the guard Modules 33 and 35 used, for the same reason: this family sits
    above the grouped-count fallback and S2 is the question it could
    break."""
    result = await xagg.run_aggregate(
        _S2_GOLD_TEXT, None,
        gateway=FakeGateway(_cr3_corpus()), user_role="supervisor",
    )

    assert result["kind"] == "relational_aggregate"
    assert result["group_by"] == "police_station"
