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
