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


async def test_age_question_returns_unsupported_aggregate_not_a_wrong_number(monkeypatch):
    result = await xagg.run_aggregate(
        "what is the average age of the accused", None, gateway=None, user_role="supervisor"
    )

    assert result["kind"] == "unsupported_aggregate"
    assert "age" in result["message"].lower()


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
