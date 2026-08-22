"""
Unit tests for src/graph/candidate_reprioritization.py (Milestone D1).

Pure-function tests for the deterministic why-template/scoring/grouping
logic — no network, no DB (matches the `no_network` guard, conftest,
autouse). The full incremental/full-sweep flow against a real
Postgres/AGE instance is exercised separately by
scripts/verify_milestone_d.py (see GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md's
Milestone D §7 verification note).
"""
from src.graph.candidate_reprioritization import (
    _UnionFind,
    _fresh_signal,
    _score,
    _why,
)


# ── _why — deterministic template, never an LLM call ────────────────────────

def test_why_reports_new_shared_structured_id():
    original = {"name_similarity": 0.6, "shared_case": False, "shared_structured_id": False}
    fresh = {"name_similarity": 0.6, "shared_case": False, "shared_structured_id": True}
    why = _why("flagged_unverified", original, fresh)
    assert "structured identifier" in why
    assert "Reinforced" in why


def test_why_reports_new_shared_case():
    original = {"name_similarity": 0.6, "shared_case": False, "shared_structured_id": False}
    fresh = {"name_similarity": 0.6, "shared_case": True, "shared_structured_id": False}
    why = _why("human_review", original, fresh)
    assert "shared case" in why
    assert "Reinforced" in why


def test_why_reports_name_similarity_increase():
    original = {"name_similarity": 0.60, "shared_case": False, "shared_structured_id": False}
    fresh = {"name_similarity": 0.72, "shared_case": False, "shared_structured_id": False}
    why = _why("human_review", original, fresh)
    assert "0.72" in why
    assert "+0.12" in why


def test_why_no_reinforcement_but_still_corroborated():
    original = {"name_similarity": 0.8, "shared_case": True, "shared_structured_id": False}
    fresh = {"name_similarity": 0.8, "shared_case": True, "shared_structured_id": False}
    why = _why("flagged_unverified", original, fresh)
    assert "no new signal" in why


def test_why_no_corroboration_at_all():
    original = {"name_similarity": 0.5, "shared_case": False, "shared_structured_id": False}
    fresh = {"name_similarity": 0.5, "shared_case": False, "shared_structured_id": False}
    why = _why("human_review", original, fresh)
    assert "No corroborating signal" in why


def test_why_never_calls_an_llm():
    """Structural guarantee, not just a behavioral one: the function has no async signature and no LLM import in this module — see module docstring point 1."""
    import inspect
    assert not inspect.iscoroutinefunction(_why)
    import src.graph.candidate_reprioritization as mod
    assert "call_llm" not in dir(mod)


# ── _score ───────────────────────────────────────────────────────────────────

def test_score_reinforced_candidate_outranks_unreinforced_equal_signal():
    fresh = {"name_similarity": 0.7, "shared_case": True, "shared_structured_id": False}
    reinforced = _score(fresh, reinforced=True)
    unreinforced = _score(fresh, reinforced=False)
    assert reinforced > unreinforced


def test_score_capped_below_one():
    fresh = {"name_similarity": 1.0, "shared_case": True, "shared_structured_id": True}
    assert _score(fresh, reinforced=True) < 1.0


# ── _fresh_signal ────────────────────────────────────────────────────────────

def test_fresh_signal_detects_shared_case_from_case_map():
    mention = {"entity_id": "P-A", "canonical_name": "Ali Hassan"}
    candidate = {"entity_id": "P-B", "canonical_name": "Ali Hassan"}
    cases = {"P-A": {"CASE-1", "CASE-2"}, "P-B": {"CASE-2"}}
    signal = _fresh_signal(mention, candidate, cases)
    assert signal["shared_case"] is True
    assert signal["name_similarity"] > 0.9


def test_fresh_signal_no_shared_case():
    mention = {"entity_id": "P-A", "canonical_name": "Ali Hassan"}
    candidate = {"entity_id": "P-B", "canonical_name": "Ali Hassan"}
    cases = {"P-A": {"CASE-1"}, "P-B": {"CASE-2"}}
    signal = _fresh_signal(mention, candidate, cases)
    assert signal["shared_case"] is False


def test_fresh_signal_shared_structured_id_via_phone():
    mention = {"entity_id": "P-A", "canonical_name": "A", "phone": "0300-1234567"}
    candidate = {"entity_id": "P-B", "canonical_name": "B", "phone": "0300-1234567"}
    signal = _fresh_signal(mention, candidate, {})
    assert signal["shared_structured_id"] is True


# ── _UnionFind — connected-components grouping (point 2) ────────────────────

def test_union_find_groups_by_shared_target():
    uf = _UnionFind([1, 2, 3])
    uf.union(1, 2)  # both edge 1 and edge 2 point at the same candidate entity
    assert uf.find(1) == uf.find(2)
    assert uf.find(3) != uf.find(1)


def test_union_find_transitive_grouping():
    uf = _UnionFind([1, 2, 3, 4])
    uf.union(1, 2)
    uf.union(2, 3)
    assert uf.find(1) == uf.find(3)
    assert uf.find(4) != uf.find(1)
