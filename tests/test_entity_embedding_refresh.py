"""
Tests for src/graph/entity_embedding_refresh.py (findings.md Module 8, Local
Search).

Covers:
  (a) _describe_entity() text formatting — the exact strings live-sampled
      and shown in the approved plan (Officer with belt/designation/role,
      Person with cnic/phone, Vehicle with plate, PhoneNumber bare);
  (b) refresh_entity_embeddings() diff logic: new entity -> upserted;
      unchanged entity -> NOT re-upserted; changed description -> re-
      upserted; entity no longer present in the graph -> deleted. All
      against a fake age_client.execute_cypher and a fake entity_vector_
      store (module-level monkeypatches, same pattern
      tests/test_harness_tool_graph.py uses for retrieve_graph/cross_rerank).
"""
from __future__ import annotations

import pytest

import src.graph.entity_embedding_refresh as refresh_mod
from src.graph.entity_embedding_refresh import _describe_entity, refresh_entity_embeddings


# ═══════════════════════════════════════════════════════════════════════
# (a) _describe_entity() — real sampled shapes from the approved plan
# ═══════════════════════════════════════════════════════════════════════

class TestDescribeEntity:
    def test_officer_with_belt_and_designation_no_role(self):
        text = _describe_entity("Officer", "راحیل شہزاد", {"belt_no": "889", "designation": "SI"})
        assert text == "Officer راحیل شہزاد, belt number 889, designation SI."

    def test_officer_with_phone_designation_and_role(self):
        text = _describe_entity(
            "Officer", "ندیم",
            {"belt_no": "GEN-0113", "phone": "0333-4000033", "designation": "ASI"},
            role="investigating",
        )
        assert text == (
            "Officer ندیم, belt number GEN-0113, phone number 0333-4000033, "
            "designation ASI, investigating officer for this case."
        )

    def test_person_with_cnic_only(self):
        text = _describe_entity("Person", "طارق", {"cnic": "00000-9000004-1"})
        assert text == "Person طارق, CNIC 00000-9000004-1."

    def test_person_with_cnic_and_phone(self):
        text = _describe_entity("Person", "حرا", {"cnic": "00000-9000077-1", "phone": "0369-4000069"})
        assert text == "Person حرا, CNIC 00000-9000077-1, phone number 0369-4000069."

    def test_vehicle_with_plate(self):
        text = _describe_entity("Vehicle", "ICT-LE-309", {"plate": "ICT-LE-309"})
        assert text == "Vehicle ICT-LE-309, plate number ICT-LE-309."

    def test_phonenumber_bare(self):
        text = _describe_entity("PhoneNumber", "0300-1234567", {"phone": "0300-1234567"})
        # PhoneNumber carries no _NOTABLE_PROPERTIES entry -- falls through
        # to just the label + name, per the approved plan's own sample.
        assert text == "PhoneNumber 0300-1234567."

    def test_organization_bare(self):
        text = _describe_entity("Organization", "Some NGO", {})
        assert text == "Organization Some NGO."


# ═══════════════════════════════════════════════════════════════════════
# (b) refresh_entity_embeddings() diff logic
# ═══════════════════════════════════════════════════════════════════════

def _node(entity_id, canonical_name, **extra_props):
    props = {"entity_id": entity_id, "canonical_name": canonical_name, **extra_props}
    return {"properties": props, "label": None}


@pytest.fixture
def stub_candidates(monkeypatch):
    """Replaces _fetch_candidate_entities() wholesale -- the diff logic
    under test doesn't care how candidates were derived, only what
    refresh_entity_embeddings() does with them."""

    def _set(candidates):
        async def _fake():
            return candidates
        monkeypatch.setattr(refresh_mod, "_fetch_candidate_entities", _fake)

    return _set


@pytest.fixture
def stub_store(monkeypatch):
    """Fake entity_vector_store surface: existing_documents is the
    pre-refresh state; upserted/deleted capture what refresh_entity_
    embeddings() actually called."""
    state = {"existing": {}, "upserted": [], "deleted": []}

    async def _get_all_entity_documents():
        return dict(state["existing"])

    async def _upsert_entities(entities):
        state["upserted"].extend(entities)

    async def _delete_entities(entity_ids):
        state["deleted"].extend(entity_ids)

    monkeypatch.setattr(refresh_mod, "get_all_entity_documents", _get_all_entity_documents)
    monkeypatch.setattr(refresh_mod, "upsert_entities", _upsert_entities)
    monkeypatch.setattr(refresh_mod, "delete_entities", _delete_entities)
    return state


@pytest.mark.asyncio
async def test_new_entity_is_upserted(stub_candidates, stub_store):
    stub_candidates([
        {"entity_id": "e1", "label": "Person", "case_id": "CASE-001", "canonical_name": "طارق",
         "description_text": "Person طارق."},
    ])
    stub_store["existing"] = {}

    result = await refresh_entity_embeddings()

    assert result == {"scanned": 1, "upserted": 1, "deleted": 0}
    assert [e["entity_id"] for e in stub_store["upserted"]] == ["e1"]


@pytest.mark.asyncio
async def test_unchanged_entity_is_not_reupserted(stub_candidates, stub_store):
    stub_candidates([
        {"entity_id": "e1", "label": "Person", "case_id": "CASE-001", "canonical_name": "طارق",
         "description_text": "Person طارق."},
    ])
    stub_store["existing"] = {"e1": "Person طارق."}

    result = await refresh_entity_embeddings()

    assert result == {"scanned": 1, "upserted": 0, "deleted": 0}
    assert stub_store["upserted"] == []


@pytest.mark.asyncio
async def test_changed_description_is_reupserted(stub_candidates, stub_store):
    stub_candidates([
        {"entity_id": "e1", "label": "Officer", "case_id": "CASE-001", "canonical_name": "ندیم",
         "description_text": "Officer ندیم, investigating officer for this case."},
    ])
    # Same entity_id, but its previously-embedded text reflects a stale
    # ASSIGNED_TO role (e.g. the case reassigned officers since the last run).
    stub_store["existing"] = {"e1": "Officer ندیم, recording officer for this case."}

    result = await refresh_entity_embeddings()

    assert result == {"scanned": 1, "upserted": 1, "deleted": 0}
    assert stub_store["upserted"][0]["description_text"] == "Officer ندیم, investigating officer for this case."


@pytest.mark.asyncio
async def test_entity_no_longer_in_graph_is_deleted(stub_candidates, stub_store):
    stub_candidates([])  # graph has nothing live anymore -- e.g. cleaned up
    stub_store["existing"] = {"e1": "Person طارق."}

    result = await refresh_entity_embeddings()

    assert result == {"scanned": 0, "upserted": 0, "deleted": 1}
    assert stub_store["deleted"] == ["e1"]


@pytest.mark.asyncio
async def test_mixed_new_unchanged_and_stale(stub_candidates, stub_store):
    stub_candidates([
        {"entity_id": "e1", "label": "Person", "case_id": "CASE-001", "canonical_name": "طارق",
         "description_text": "Person طارق."},  # unchanged
        {"entity_id": "e2", "label": "Person", "case_id": "CASE-001", "canonical_name": "بلال",
         "description_text": "Person بلال."},  # new
    ])
    stub_store["existing"] = {"e1": "Person طارق.", "e3": "Person گم شدہ."}  # e3 is stale

    result = await refresh_entity_embeddings()

    assert result == {"scanned": 2, "upserted": 1, "deleted": 1}
    assert [e["entity_id"] for e in stub_store["upserted"]] == ["e2"]
    assert stub_store["deleted"] == ["e3"]


# ═══════════════════════════════════════════════════════════════════════
# _fetch_candidate_entities() itself -- fake age_client, confirms the
# ASSIGNED_TO role join actually reaches _describe_entity() correctly.
# ═══════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_fetch_candidate_entities_includes_officer_role(monkeypatch):
    async def _fake_execute_cypher(query, params=None, columns=None):
        if "Officer" in query:
            return [{
                "n": _node("off1", "ندیم", belt_no="GEN-0113", phone="0333-4000033", designation="ASI"),
                "case_id": "CASE-001",
                "role": "investigating",
            }]
        if "Person" in query:
            return [{"n": _node("p1", "طارق", cnic="00000-9000004-1"), "case_id": "CASE-001"}]
        return []

    monkeypatch.setattr(refresh_mod.age_client, "execute_cypher", _fake_execute_cypher)

    candidates = await refresh_mod._fetch_candidate_entities()

    officer = next(c for c in candidates if c["entity_id"] == "off1")
    assert officer["description_text"] == (
        "Officer ندیم, belt number GEN-0113, phone number 0333-4000033, "
        "designation ASI, investigating officer for this case."
    )
    person = next(c for c in candidates if c["entity_id"] == "p1")
    assert person["description_text"] == "Person طارق, CNIC 00000-9000004-1."


@pytest.mark.asyncio
async def test_fetch_candidate_entities_officer_with_no_assigned_to_role(monkeypatch):
    async def _fake_execute_cypher(query, params=None, columns=None):
        if "Officer" in query:
            return [{
                "n": _node("off1", "راحیل شہزاد", belt_no="889", designation="SI"),
                "case_id": "CASE-001",
                "role": None,
            }]
        return []

    monkeypatch.setattr(refresh_mod.age_client, "execute_cypher", _fake_execute_cypher)

    candidates = await refresh_mod._fetch_candidate_entities()

    officer = next(c for c in candidates if c["entity_id"] == "off1")
    assert officer["description_text"] == "Officer راحیل شہزاد, belt number 889, designation SI."
