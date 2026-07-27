"""
Tests for src/graph/entity_resolution.py (Phase 4.8).

age_client and versioning are both monkeypatched with fakes — no real
Postgres/AGE/LLM (matches the `no_network` guard, conftest, autouse). The
full pipeline (CNIC auto-merge, the hard CNIC-mismatch block, the P-006
cross-case flagged-unverified case) was verified live against a real
AGE-enabled Postgres instance and the real Qwen3-14B model server during
development; these tests guard entity_resolution.py's own decision logic
against regressions without requiring either live dependency.
"""
import pytest

import src.graph.entity_resolution as er
import src.graph.case_scope as case_scope


class FakeAgeClient:
    def __init__(self):
        self.calls: list[dict] = []
        self.responses: list[list[dict]] = []

    def queue(self, response):
        self.responses.append(response)

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        self.calls.append({"cypher": cypher_query, "params": params or {}})
        if self.responses:
            return self.responses.pop(0)
        return []


class FakeVersioning:
    def __init__(self):
        self.nodes_written: list[dict] = []
        self.edges_written: list[dict] = []

    async def write_node(self, label, match, properties=None, *, source_doc_id=None, confidence=1.0):
        record = {"label": label, "match": match, "properties": properties or {}}
        self.nodes_written.append(record)
        return {"id": 1, "label": label, "properties": {**match, **(properties or {})}}

    async def write_edge(self, edge_label, from_label, from_match, to_label, to_match,
                          properties=None, *, source_doc_id, source_chunk_id=None,
                          confidence=1.0, supersedes_edge_id=None):
        record = {
            "edge_label": edge_label, "from_label": from_label, "from_match": from_match,
            "to_label": to_label, "to_match": to_match, "properties": properties or {},
        }
        self.edges_written.append(record)
        return {"id": 99, "label": edge_label, "properties": properties or {}}


@pytest.fixture
def fake_age(monkeypatch):
    client = FakeAgeClient()
    monkeypatch.setattr(er, "age_client", client)
    # Phase 2: _shares_case() now goes through case_scope.scoped_cypher(),
    # which calls its OWN module-level `age_client` reference (a separate
    # binding from er.age_client) — both must point at the fake.
    monkeypatch.setattr(case_scope, "age_client", client)
    return client


@pytest.fixture
def fake_versioning(monkeypatch):
    v = FakeVersioning()
    monkeypatch.setattr(er, "versioning", v)
    return v


def _node(entity_id, canonical_name, cnic=None, **extra):
    props = {"entity_id": entity_id, "canonical_name": canonical_name}
    if cnic:
        props["cnic"] = cnic
    props.update(extra)
    return {"id": 1, "label": "Person", "properties": props}


# ── CNIC tier ────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_cnic_exact_match_is_auto_merge(fake_age):
    fake_age.queue([{"n": _node("P-EXISTING", "احمد رضا قریشی", cnic="00000-9119877-0")}])

    decision = await er.resolve_mention(
        "person", {"canonical_name": "احمد رضا قرشی", "cnic": "00000-9119877-0"}, "CASE-001"
    )
    assert decision.tier == er.TIER_CNIC_AUTO
    assert decision.target_entity_id == "P-EXISTING"
    assert decision.confidence == 1.0
    # CNIC-tier decisions never call the LLM — only one cypher call (the lookup).
    assert len(fake_age.calls) == 1


@pytest.mark.asyncio
async def test_no_cnic_match_falls_through_to_name_fallback(fake_age):
    fake_age.queue([])          # CNIC lookup: nothing
    fake_age.queue([])          # candidate scan: no nodes at all
    decision = await er.resolve_mention(
        "person", {"canonical_name": "کوئی نیا شخص", "cnic": "00000-1111111-1"}, "CASE-001"
    )
    assert decision.tier == er.TIER_NEW


# ── Hard CNIC-mismatch block ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_different_cnic_never_merges_regardless_of_name_similarity(fake_age):
    # CNIC lookup for the mention's own CNIC finds nothing (no exact match)...
    fake_age.queue([])
    # ...but the candidate scan finds a SAME-NAME node with a DIFFERENT CNIC.
    fake_age.queue([{"n": _node("P-OTHER", "عمران ستار", cnic="00000-5526317-6")}])

    decision = await er.resolve_mention(
        "person", {"canonical_name": "عمران ستار", "cnic": "00000-5690801-9"}, "CASE-DRY-001"
    )
    # Hard block: this candidate must never even be scored/surfaced.
    assert decision.tier == er.TIER_NEW
    assert decision.candidates_considered == 0


# ── Name-fallback tiers ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_near_exact_name_flags_even_without_corroboration(fake_age):
    # The P-006 flagship case: no CNIC on either side, cross-case, no
    # shared structured id — near-identical name alone must still flag.
    # No CNIC on the mention -> _find_by_cnic() is never called, so no
    # placeholder response is queued for it (see the candidate-scan
    # response landing there instead was the original bug this fixed).
    fake_age.queue([{"n": _node("P-006", "عدنان قریشی وحید")}])
    fake_age.queue([[]])  # _shares_case query — not sharing a case

    decision = await er.resolve_mention(
        "person", {"canonical_name": "عدنان قریشی وحید"}, "CASE-016"
    )
    assert decision.tier == er.TIER_FLAGGED
    assert decision.target_entity_id == "P-006"
    assert decision.confidence < 1.0  # never reaches CNIC-tier confidence


@pytest.mark.asyncio
async def test_weak_match_goes_to_human_review(monkeypatch):
    # A controlled score below MEDIUM_BAND_FLOOR (no LLM call needed) but
    # at/above REVIEW_FLOOR — the "weak name-only match" band.
    weak = er.Candidate(
        entity_id="P-WEAK", node=_node("P-WEAK", "زید علی خان"),
        name_similarity=0.45, shared_case=False, shared_structured_id=False,
        score=0.45,
    )
    assert er.REVIEW_FLOOR <= 0.45 < er.MEDIUM_BAND_FLOOR

    async def fake_generate_candidates(label, mention, case_id, id_key=None):
        return [weak]
    monkeypatch.setattr(er, "_generate_candidates", fake_generate_candidates)

    decision = await er.resolve_mention("person", {"canonical_name": "زید علی"}, "CASE-099")
    assert decision.tier == er.TIER_REVIEW
    assert decision.target_entity_id == "P-WEAK"


# ── Medium-band LLM adjudication ───────────────────────────────────────
#
# These three tests bypass real rapidfuzz arithmetic (fragile to target a
# specific score band via made-up strings) by monkeypatching
# _generate_candidates() directly to return one controlled Candidate
# sitting inside [MEDIUM_BAND_FLOOR, HIGH_NAME_WITH_CONTEXT) with no
# corroboration — the exact condition that routes to LLM adjudication.

def _medium_band_candidate(entity_id="P-CANDIDATE", name="کچھ ملتا جلتا نام"):
    assert er.MEDIUM_BAND_FLOOR <= 0.60 < er.HIGH_NAME_WITH_CONTEXT
    return er.Candidate(
        entity_id=entity_id, node=_node(entity_id, name),
        name_similarity=0.60, shared_case=False, shared_structured_id=False,
        score=0.60,
    )


@pytest.mark.asyncio
async def test_medium_band_calls_llm_and_respects_same_entity_false(monkeypatch):
    async def fake_generate_candidates(label, mention, case_id, id_key=None):
        return [_medium_band_candidate()]
    monkeypatch.setattr(er, "_generate_candidates", fake_generate_candidates)

    called = []

    async def fake_call_llm(system_prompt, user_message, **kwargs):
        called.append(1)
        return '{"same_entity": false, "tier": null, "confidence": 0.0, "reasoning": "different case, different role"}'
    monkeypatch.setattr(er, "call_llm", fake_call_llm)

    decision = await er.resolve_mention("person", {"canonical_name": "ثنا ملک اکرم"}, "CASE-019")

    assert called  # the LLM was actually consulted for this band
    # Must NOT merge — LLM said different people, so falls through to
    # weak-match/no-candidate, never auto-merge (which name-fallback can
    # never reach anyway).
    assert decision.tier in (er.TIER_REVIEW, er.TIER_NEW)
    assert decision.tier != er.TIER_CNIC_AUTO


@pytest.mark.asyncio
async def test_medium_band_llm_confirms_match_gets_flagged(monkeypatch):
    async def fake_generate_candidates(label, mention, case_id, id_key=None):
        return [_medium_band_candidate()]
    monkeypatch.setattr(er, "_generate_candidates", fake_generate_candidates)

    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return '{"same_entity": true, "tier": "flagged_unverified", "confidence": 0.7, "reasoning": "matching father name"}'
    monkeypatch.setattr(er, "call_llm", fake_call_llm)

    decision = await er.resolve_mention("person", {"canonical_name": "کچھ ملتا جلتا نام دوسرا"}, "CASE-050")

    assert decision.tier == er.TIER_FLAGGED
    assert decision.target_entity_id == "P-CANDIDATE"
    assert decision.confidence <= er.NAME_FALLBACK_CAP  # never reaches CNIC-tier confidence


@pytest.mark.asyncio
async def test_llm_never_grants_auto_merge_tier(monkeypatch):
    async def fake_generate_candidates(label, mention, case_id, id_key=None):
        return [_medium_band_candidate()]
    monkeypatch.setattr(er, "_generate_candidates", fake_generate_candidates)

    async def fake_call_llm(system_prompt, user_message, **kwargs):
        # A misbehaving/malformed LLM response trying to claim auto-merge.
        return '{"same_entity": true, "tier": "cnic_auto", "confidence": 1.0, "reasoning": "bad"}'
    monkeypatch.setattr(er, "call_llm", fake_call_llm)

    decision = await er.resolve_mention("person", {"canonical_name": "ملتا جلتا ناما"}, "CASE-050")
    # Defensively downgraded to human_review, never allowed to pass through
    # the LLM's own (invalid) claim of the auto-merge tier.
    assert decision.tier == er.TIER_REVIEW


# ── resolve_and_write: CNIC tier reuses the existing node, no SAME_AS ──

@pytest.mark.asyncio
async def test_resolve_and_write_cnic_auto_reuses_node_no_same_as_edge(fake_age, fake_versioning):
    fake_age.queue([{"n": _node("P-EXISTING", "احمد رضا قریشی", cnic="00000-9119877-0")}])

    result = await er.resolve_and_write(
        "person", {"canonical_name": "احمد رضا قرشی", "cnic": "00000-9119877-0"},
        "CASE-DRY-001", "DOC-1",
    )
    assert result["entity_id"] == "P-EXISTING"
    assert result["is_new_node"] is False
    edge_labels = [e["edge_label"] for e in fake_versioning.edges_written]
    assert "SAME_AS" not in edge_labels
    assert "BELONGS_TO_CASE" in edge_labels
    assert "APPEARS_IN" in edge_labels


@pytest.mark.asyncio
async def test_resolve_and_write_flagged_creates_new_node_and_same_as_edge(fake_age, fake_versioning, monkeypatch):
    # No CNIC on the mention -> no _find_by_cnic() call, no placeholder queued for it.
    fake_age.queue([{"n": _node("P-006", "عدنان قریشی وحید")}])
    fake_age.queue([[]])

    result = await er.resolve_and_write(
        "person", {"canonical_name": "عدنان قریشی وحید"}, "CASE-016", "DOC-016",
    )
    assert result["tier"] == er.TIER_FLAGGED
    assert result["is_new_node"] is True
    assert result["entity_id"] != "P-006"

    same_as_edges = [e for e in fake_versioning.edges_written if e["edge_label"] == "SAME_AS"]
    assert len(same_as_edges) == 1
    assert same_as_edges[0]["to_match"] == {"entity_id": "P-006"}
    assert same_as_edges[0]["properties"]["status"] == "pending"


@pytest.mark.asyncio
async def test_resolve_and_write_new_entity_no_same_as_edge(fake_age, fake_versioning):
    fake_age.queue([])  # no cnic
    fake_age.queue([])  # no candidates at all

    result = await er.resolve_and_write(
        "person", {"canonical_name": "بالکل نیا شخص"}, "CASE-001", "DOC-1",
    )
    assert result["tier"] == er.TIER_NEW
    assert result["is_new_node"] is True
    edge_labels = [e["edge_label"] for e in fake_versioning.edges_written]
    assert "SAME_AS" not in edge_labels
