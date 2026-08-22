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
