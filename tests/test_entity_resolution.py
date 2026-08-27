"""
Tests for src/graph/entity_resolution.py (Phase 4.8, plus Milestone A1's
identity-index wiring).

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


class FakeIdentityIndex:
    """
    Stand-in for src.graph.identity_index — no real Postgres. Defaults to
    "always a miss / nothing excluded", which reproduces the exact
    pre-A1 behavior (every resolve_mention()/_generate_candidates() call
    falls straight through to the AGE fake) for every test that doesn't
    explicitly opt into exercising the index-hit path.
    """

    def __init__(self):
        self.lookup_calls: list[tuple] = []
        self.exclude_calls: list[tuple] = []
        self.lookup_result: str | None = None
        self.exclude_result: list[str] = []

    async def lookup(self, label, id_key, id_value):
        self.lookup_calls.append((label, id_key, id_value))
        return self.lookup_result

    async def entity_ids_excluding(self, label, id_key, exclude_id_value):
        self.exclude_calls.append((label, id_key, exclude_id_value))
        return self.exclude_result


@pytest.fixture(autouse=True)
def fake_identity_index(monkeypatch):
    fake = FakeIdentityIndex()
    monkeypatch.setattr(er, "identity_index", fake)
    return fake


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

    async def write_node(self, label, match, properties=None, *, source_doc_id=None, confidence=1.0, graph=None):
        record = {"label": label, "match": match, "properties": properties or {}, "graph": graph}
        self.nodes_written.append(record)
        return {"id": 1, "label": label, "properties": {**match, **(properties or {})}}

    async def write_edge(self, edge_label, from_label, from_match, to_label, to_match,
                          properties=None, *, source_doc_id, source_chunk_id=None,
                          confidence=1.0, supersedes_edge_id=None, graph=None):
        record = {
            "edge_label": edge_label, "from_label": from_label, "from_match": from_match,
            "to_label": to_label, "to_match": to_match, "properties": properties or {}, "graph": graph,
        }
        self.edges_written.append(record)
        return {"id": 99, "label": edge_label, "properties": properties or {}}


@pytest.fixture
def fake_age(monkeypatch):
    client = FakeAgeClient()
    monkeypatch.setattr(er, "age_client", client)
    # Phase 2: _shares_case_batch() now goes through case_scope.scoped_cypher(),
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


# ── Officer/belt_no tier (Milestone B2) — same discipline as CNIC ───────

def _officer_node(entity_id, canonical_name, belt_no=None, **extra):
    props = {"entity_id": entity_id, "canonical_name": canonical_name}
    if belt_no:
        props["belt_no"] = belt_no
    props.update(extra)
    return {"id": 1, "label": "Officer", "properties": props}


@pytest.mark.asyncio
async def test_belt_no_exact_match_is_auto_merge(fake_age):
    fake_age.queue([{"n": _officer_node("OFFICER-EXISTING", "طارق جمالی", belt_no="HYD-3345")}])

    decision = await er.resolve_mention(
        "officer", {"canonical_name": "طارق جمالی", "belt_no": "HYD-3345"}, "fir-1-26",
    )
    assert decision.tier == er.TIER_CNIC_AUTO
    assert decision.target_entity_id == "OFFICER-EXISTING"
    assert decision.confidence == 1.0
    # Same exact-match tier as CNIC — never reaches the LLM, one cypher call.
    assert len(fake_age.calls) == 1


@pytest.mark.asyncio
async def test_different_belt_no_never_merges_regardless_of_name_similarity(fake_age):
    fake_age.queue([])  # belt_no lookup for the mention's own belt_no: no exact match
    fake_age.queue([{"n": _officer_node("OFFICER-OTHER", "طارق جمالی", belt_no="HYD-9999")}])

    decision = await er.resolve_mention(
        "officer", {"canonical_name": "طارق جمالی", "belt_no": "HYD-3345"}, "fir-1-26",
    )
    assert decision.tier == er.TIER_NEW
    assert decision.candidates_considered == 0


@pytest.mark.asyncio
async def test_officer_with_no_belt_no_falls_through_to_name_fallback(fake_age):
    fake_age.queue([])  # candidate scan: no nodes at all
    decision = await er.resolve_mention(
        "officer", {"canonical_name": "نامعلوم افسر"}, "fir-1-26",
    )
    assert decision.tier == er.TIER_NEW


# ── Roman-Urdu <-> Urdu-script name bridging (A-2) ─────────────────────

@pytest.mark.parametrize("roman,urdu", [
    ("Zafar Iqbal", "ظفر اقبال"),
    ("Muhammad Ali", "محمد علی"),
    ("Faisal Shahzad Qureshi", "فیصل شہزاد قریشی"),
    ("Usman Khalid Malik", "عثمان خالد ملک"),
    ("Ahmed Raza Qureshi", "احمد رضا قریشی"),
])
def test_cross_script_same_name_scores_high(roman, urdu):
    assert er._name_similarity(roman, urdu) >= er.HIGH_NAME_WITH_CONTEXT


@pytest.mark.parametrize("roman,urdu", [
    ("Ahmed Raza Qureshi", "عثمان خالد ملک"),
    ("Zafar Iqbal", "فیصل شہزاد قریشی"),
])
def test_cross_script_different_name_scores_low(roman, urdu):
    assert er._name_similarity(roman, urdu) < er.REVIEW_FLOOR


@pytest.mark.parametrize("a,b", [
    ("محمد علی", "محمد علی خان"),
    ("عثمان خالد ملک", "عثمان خالد"),
])
def test_same_script_comparison_is_unaffected_by_cross_script_path(a, b):
    """
    The cross-script skeleton path must only ever engage when the two
    names are in DIFFERENT scripts — same-script comparisons must produce
    exactly the same score as plain token_sort_ratio, unchanged.
    """
    from rapidfuzz import fuzz as _fuzz
    from src.ingestion.text_normalizer import normalize_urdu as _norm

    expected = _fuzz.token_sort_ratio(_norm(a), _norm(b)) / 100.0
    assert er._name_similarity(a, b) == pytest.approx(expected)


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


# ── Module 7.3: shares-case check is batched, not one call per candidate ──

@pytest.mark.asyncio
async def test_shares_case_check_is_batched_not_per_candidate(fake_age):
    """The audit's N+1 finding: candidate generation used to call
    _shares_case() once per surviving candidate. With 5 surviving
    candidates, the old code made 1 (fetch-all-nodes) + 5 (one per
    candidate) = 6 Cypher round trips; the batched version makes exactly
    2 — fetch-all-nodes, then one shared-case check covering every
    candidate at once — regardless of how many candidates survive."""
    candidate_nodes = [_node(f"P-{i}", "زید علی خان") for i in range(5)]
    fake_age.queue([{"n": n} for n in candidate_nodes])          # _fetch_all_nodes
    fake_age.queue([{"entity_id": "P-0"}, {"entity_id": "P-2"}])  # batched shares-case

    candidates = await er._generate_candidates(
        "Person", {"canonical_name": "زید علی خان"}, "CASE-001",
    )

    assert len(candidates) == 5
    assert len(fake_age.calls) == 2, (
        f"expected 2 Cypher calls (fetch-all-nodes + one batched shares-case "
        f"check), got {len(fake_age.calls)} — did shares-case regress to per-candidate?"
    )
    shared = {c.entity_id for c in candidates if c.shared_case}
    assert shared == {"P-0", "P-2"}
    not_shared = {c.entity_id for c in candidates if not c.shared_case}
    assert not_shared == {"P-1", "P-3", "P-4"}


@pytest.mark.asyncio
async def test_shares_case_batch_short_circuits_with_no_candidates(fake_age):
    """Zero surviving candidates must not issue a shares-case query at all —
    an empty entity_ids list has nothing to ask the database."""
    fake_age.queue([])  # _fetch_all_nodes: nothing in the graph

    candidates = await er._generate_candidates(
        "Person", {"canonical_name": "کوئی نہیں"}, "CASE-001",
    )

    assert candidates == []
    assert len(fake_age.calls) == 1  # just the fetch-all-nodes call


async def _no_case_hoist(label, id_key, mention_name, case_id, *, graph=None):
    """Stub for er._case_hoisted_id_value — always 'nothing to hoist',
    for tests that monkeypatch _generate_candidates directly and have no
    fake_age/case_scope wiring for this new, separate AGE call."""
    return None


# ── Name-fallback tiers ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_near_exact_name_flags_even_without_corroboration(fake_age):
    # The P-006 flagship case: no CNIC on either side, cross-case, no
    # shared structured id — near-identical name alone must still flag.
    # No CNIC on the mention -> _find_by_cnic() is never called, so no
    # placeholder response is queued for it (see the candidate-scan
    # response landing there instead was the original bug this fixed).
    #
    # [case-wide hoisting] No CNIC also means resolve_mention() first
    # tries _case_hoisted_id_value()'s own scoped_cypher call — queue an
    # empty result for it (no case-mate with an id found) so it correctly
    # falls through to candidate generation below, same as before that
    # addition existed.
    fake_age.queue([])  # case-hoist scan: nothing
    fake_age.queue([{"n": _node("P-006", "عدنان قریشی وحید")}])
    fake_age.queue([])  # batched shared-case query — no entity_ids come back, so none share the case

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

    async def fake_generate_candidates(label, mention, case_id, id_key=None, *, graph=None):
        return [weak]
    monkeypatch.setattr(er, "_generate_candidates", fake_generate_candidates)
    # [case-wide hoisting] No CNIC on the mention -> resolve_mention() tries
    # this first; stub it out so the test stays isolated from AGE/case_scope,
    # same as _generate_candidates above.
    monkeypatch.setattr(er, "_case_hoisted_id_value", _no_case_hoist)

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
    async def fake_generate_candidates(label, mention, case_id, id_key=None, *, graph=None):
        return [_medium_band_candidate()]
    monkeypatch.setattr(er, "_generate_candidates", fake_generate_candidates)
    monkeypatch.setattr(er, "_case_hoisted_id_value", _no_case_hoist)

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
    async def fake_generate_candidates(label, mention, case_id, id_key=None, *, graph=None):
        return [_medium_band_candidate()]
    monkeypatch.setattr(er, "_generate_candidates", fake_generate_candidates)
    monkeypatch.setattr(er, "_case_hoisted_id_value", _no_case_hoist)

    async def fake_call_llm(system_prompt, user_message, **kwargs):
        return '{"same_entity": true, "tier": "flagged_unverified", "confidence": 0.7, "reasoning": "matching father name"}'
    monkeypatch.setattr(er, "call_llm", fake_call_llm)

    decision = await er.resolve_mention("person", {"canonical_name": "کچھ ملتا جلتا نام دوسرا"}, "CASE-050")

    assert decision.tier == er.TIER_FLAGGED
    assert decision.target_entity_id == "P-CANDIDATE"
    assert decision.confidence <= er.NAME_FALLBACK_CAP  # never reaches CNIC-tier confidence


@pytest.mark.asyncio
async def test_llm_never_grants_auto_merge_tier(monkeypatch):
    async def fake_generate_candidates(label, mention, case_id, id_key=None, *, graph=None):
        return [_medium_band_candidate()]
    monkeypatch.setattr(er, "_generate_candidates", fake_generate_candidates)
    monkeypatch.setattr(er, "_case_hoisted_id_value", _no_case_hoist)

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
    # [case-wide hoisting] Queue an empty result for _case_hoisted_id_value()'s
    # own scoped_cypher call first — see the sibling resolve_mention() test above.
    fake_age.queue([])  # case-hoist scan: nothing
    fake_age.queue([{"n": _node("P-006", "عدنان قریشی وحید")}])
    fake_age.queue([])  # batched shared-case query — no entity_ids come back, so none share the case

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


# ── Ingestion Quality Control at Scale, Module G1: resolve_and_write() is
# the one chokepoint every tier decision flows through ────────────────────

@pytest.mark.asyncio
async def test_resolve_and_write_records_the_tier_on_the_production_graph(fake_age, fake_versioning, monkeypatch):
    recorded = []
    monkeypatch.setattr(er.ingestion_quality, "record_tier", lambda tier: recorded.append(tier))
    fake_age.queue([{"n": _node("P-EXISTING", "احمد رضا قریشی", cnic="00000-9119877-0")}])

    await er.resolve_and_write(
        "person", {"canonical_name": "احمد رضا قرشی", "cnic": "00000-9119877-0"},
        "CASE-DRY-001", "DOC-1",
    )

    assert recorded == [er.TIER_CNIC_AUTO]


@pytest.mark.asyncio
async def test_resolve_and_write_never_records_on_the_eval_graph(fake_age, fake_versioning, monkeypatch):
    """Milestone A1's own _PRODUCTION_GRAPH guard — an eval run against
    evidence_graph_eval must never pollute real ingestion-quality counts."""
    recorded = []
    monkeypatch.setattr(er.ingestion_quality, "record_tier", lambda tier: recorded.append(tier))
    fake_age.queue([])  # no cnic
    fake_age.queue([])  # no candidates at all

    await er.resolve_and_write(
        "person", {"canonical_name": "بالکل نیا شخص"}, "CASE-001", "DOC-1",
        graph="evidence_graph_eval",
    )

    assert recorded == []


# ── Milestone A1: identity-index wiring ─────────────────────────────────

@pytest.mark.asyncio
async def test_primary_id_index_hit_skips_the_age_scan_entirely(fake_age, fake_identity_index):
    """
    An identity_index hit must resolve cnic_auto WITHOUT ever calling
    age_client — the whole point of Milestone A1 is removing this exact
    scan from the hot path.
    """
    fake_identity_index.lookup_result = "P-EXISTING"

    decision = await er.resolve_mention(
        "person", {"canonical_name": "احمد رضا قرشی", "cnic": "00000-9119877-0"}, "CASE-001"
    )

    assert decision.tier == er.TIER_CNIC_AUTO
    assert decision.target_entity_id == "P-EXISTING"
    assert fake_identity_index.lookup_calls == [("Person", "cnic", "00000-9119877-0")]
    assert fake_age.calls == []  # no AGE query at all on an index hit


@pytest.mark.asyncio
async def test_primary_id_index_miss_falls_back_to_the_age_scan(fake_age, fake_identity_index):
    """
    fake_identity_index.lookup_result defaults to None (a miss) — the
    exact same AGE MATCH this function always ran must still fire, per
    the plan's "defends against drift" requirement.
    """
    fake_age.queue([{"n": _node("P-EXISTING", "احمد رضا قریشی", cnic="00000-9119877-0")}])

    decision = await er.resolve_mention(
        "person", {"canonical_name": "احمد رضا قرشی", "cnic": "00000-9119877-0"}, "CASE-001"
    )

    assert decision.tier == er.TIER_CNIC_AUTO
    assert decision.target_entity_id == "P-EXISTING"
    assert len(fake_age.calls) == 1


@pytest.mark.asyncio
async def test_eval_graph_never_consults_the_production_identity_index(fake_age, fake_identity_index):
    """
    resolve_mention(graph="evidence_graph_eval") must not read the shared
    production identity_index at all — an isolated eval run's resolution
    decisions must never be influenced by real production data.
    """
    fake_identity_index.lookup_result = "P-SHOULD-NEVER-BE-USED"
    fake_age.queue([])  # eval graph: no cnic match
    fake_age.queue([])  # eval graph: no candidates

    decision = await er.resolve_mention(
        "person", {"canonical_name": "کوئی نیا شخص", "cnic": "00000-1111111-1"}, "CASE-001",
        graph="evidence_graph_eval",
    )

    assert decision.tier == er.TIER_NEW
    assert fake_identity_index.lookup_calls == []


@pytest.mark.asyncio
async def test_generate_candidates_uses_index_to_exclude_known_conflicting_ids(fake_age, fake_identity_index):
    """
    When the identity index already knows P-OTHER carries a different
    CNIC, _generate_candidates() must fetch via the excluding query (not
    the plain full scan) and must never surface P-OTHER as a candidate.
    """
    fake_identity_index.lookup_result = None  # no exact match for this mention's cnic
    fake_identity_index.exclude_result = ["P-OTHER"]
    fake_age.queue([])  # cnic lookup miss (AGE fallback, since index also missed)
    fake_age.queue([{"n": _node("P-NEW-CANDIDATE", "احمد رضا قریشی")}])  # excluding-scan result

    decision = await er.resolve_mention(
        "person", {"canonical_name": "احمد رضا قرشی", "cnic": "00000-2222222-2"}, "CASE-001"
    )

    assert fake_identity_index.exclude_calls == [("Person", "cnic", "00000-2222222-2")]
    exclude_call = fake_age.calls[1]  # [0] cnic lookup miss, [1] the excluding-scan fetch
    assert "NOT n.entity_id IN $excluded" in exclude_call["cypher"]
    assert exclude_call["params"]["excluded"] == ["P-OTHER"]
    assert decision.target_entity_id == "P-NEW-CANDIDATE"


@pytest.mark.asyncio
async def test_generate_candidates_falls_back_to_full_scan_with_no_excludes(fake_age, fake_identity_index):
    """No entity_ids_excluding() results -> the original unfiltered full scan, unchanged."""
    fake_identity_index.exclude_result = []
    fake_age.queue([])  # cnic lookup miss
    fake_age.queue([{"n": _node("P-SOMEONE", "احمد رضا قریشی")}])

    await er.resolve_mention(
        "person", {"canonical_name": "احمد رضا قرشی", "cnic": "00000-3333333-3"}, "CASE-001"
    )

    scan_call = fake_age.calls[1]  # [0] cnic lookup miss, [1] the plain full scan
    assert scan_call["cypher"].strip().startswith("MATCH (n:Person) RETURN n")


# ── Case-wide CNIC hoisting (roznamcha/zimni mentions with no CNIC of
# their own, elsewhere-in-case identity carried over) ──────────────────

@pytest.mark.asyncio
async def test_case_hoisted_id_value_matches_bare_name_to_full_name(fake_age):
    fake_age.queue([{"name": "فیصل ولد محمد رمضان", "id_value": "00000-9000057-1"}])

    hoisted = await er._case_hoisted_id_value("Person", "cnic", "فیصل", "CASE-fir-1001-26")

    assert hoisted == "00000-9000057-1"


@pytest.mark.asyncio
async def test_case_hoisted_id_value_ignores_character_prefix_that_is_not_a_word_boundary(fake_age):
    """'فیصل' must not match 'فیصلآباد' just because it shares a character
    prefix — token-based, not substring."""
    fake_age.queue([{"name": "فیصلآباد", "id_value": "00000-9000057-1"}])

    hoisted = await er._case_hoisted_id_value("Person", "cnic", "فیصل", "CASE-001")

    assert hoisted is None


@pytest.mark.asyncio
async def test_resolve_mention_hoists_cnic_from_elsewhere_in_the_case(fake_age):
    """
    The roznamcha/zimni case: this mention ("فیصل") carries no CNIC of its
    own, but a full name containing it ("فیصل ولد محمد رمضان") already has
    one recorded elsewhere in the SAME case. resolve_mention() must route
    this straight to TIER_CNIC_AUTO against the SAME existing node — no
    SAME_AS, no review — exactly like a directly-observed CNIC would.
    """
    fake_age.queue([  # _case_hoisted_id_value()'s own scoped_cypher call
        {"name": "فیصل ولد محمد رمضان", "id_value": "00000-9000057-1"},
    ])
    fake_age.queue([{"n": _node("P-EXISTING", "فیصل ولد محمد رمضان", cnic="00000-9000057-1")}])

    decision = await er.resolve_mention("person", {"canonical_name": "فیصل"}, "CASE-fir-1001-26")

    assert decision.tier == er.TIER_CNIC_AUTO
    assert decision.target_entity_id == "P-EXISTING"
    assert decision.confidence == 1.0
    assert "hoisted from elsewhere in case" in decision.basis


@pytest.mark.asyncio
async def test_resolve_mention_does_not_hoist_across_two_different_people_same_given_name(fake_age):
    """
    The document already has TWO different full names sharing the given
    name "فیصل", with DIFFERENT CNICs — ambiguous, so no hoist happens
    and this falls through to ordinary name-fallback scoring instead of
    silently picking one of the two people.
    """
    fake_age.queue([  # case-hoist scan: two conflicting candidates
        {"name": "فیصل ولد محمد رمضان", "id_value": "00000-9000057-1"},
        {"name": "فیصل احمد", "id_value": "00000-1111111-1"},
    ])
    fake_age.queue([])  # falls through to _generate_candidates -> _fetch_all_nodes: nothing

    decision = await er.resolve_mention("person", {"canonical_name": "فیصل"}, "CASE-fir-1001-26")

    assert decision.tier == er.TIER_NEW  # never auto-merged against either candidate


@pytest.mark.asyncio
async def test_resolve_mention_hoist_is_case_scoped_via_scoped_cypher(fake_age):
    """Must route through case_scope.scoped_cypher (the $case_id-enforcing
    chokepoint), not a raw cross-case age_client call — confirmed by
    checking the query text actually filters on BELONGS_TO_CASE/case_id."""
    fake_age.queue([])
    fake_age.queue([])

    await er.resolve_mention("person", {"canonical_name": "کوئی شخص"}, "CASE-SCOPE-TEST")

    hoist_call = fake_age.calls[0]
    assert "$case_id" in hoist_call["cypher"]
    assert "BELONGS_TO_CASE" in hoist_call["cypher"]
    assert hoist_call["params"]["case_id"] == "CASE-SCOPE-TEST"


@pytest.mark.asyncio
async def test_organization_and_address_never_attempt_case_hoisting(fake_age):
    """Entity types with no primary id key (organization, address) have
    nothing to hoist — must skip straight to candidate generation, one
    call, not the hoist call plus a scan."""
    fake_age.queue([])  # _fetch_all_nodes for Organization

    await er.resolve_mention("organization", {"canonical_name": "کوئی ادارہ"}, "CASE-001")

    assert len(fake_age.calls) == 1
    assert fake_age.calls[0]["cypher"].strip().startswith("MATCH (n:Organization) RETURN n")
