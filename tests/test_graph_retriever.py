"""
Tests for src/retrieval/graph_retriever.py (Phase 5.2).

A small in-memory fake graph stands in for Apache AGE (matches the
`no_network` guard, conftest, autouse) — real traversal against a live
AGE instance is out of scope for this suite (see tests/README.md), but
the fake models nodes/edges precisely enough to exercise the actual
BFS/identity-fold logic in graph_retriever.py, not just its plumbing.

Guards the rules this module exists to enforce:
  * only CONFIRMED SAME_AS edges are followed as identity — pending/
    rejected are never traversed (P-006 fixture: surfaced as a caveat,
    never merged; ADDR-002 fixture: a shared attribute is never a
    relationship)
  * traversal is capped at max_hops and confidence compounds per hop
  * within-case traversal never leaks into another case
  * a graph hop whose document was never ingested (missing from Chroma)
    is dropped, never handed to the generator text-less
"""
import pytest

import src.retrieval.graph_retriever as gr
import src.graph.case_scope as case_scope


class FakeGraph:
    """In-memory stand-in for evidence_graph, dispatching on Cypher shape."""

    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.belongs_to_case: dict[str, set] = {}
        self.associated_with: list[tuple] = []
        self.same_as: list[tuple] = []
        self.appears_in: list[tuple] = []
        self.conflicts: list[tuple] = []

    def add_node(self, entity_id, label, **props):
        self.nodes[entity_id] = {"id": entity_id, "label": label, "properties": {"entity_id": entity_id, **props}}

    def add_case(self, entity_id, case_id):
        self.belongs_to_case.setdefault(entity_id, set()).add(case_id)

    def add_associated(self, a, b, confidence=1.0, superseded_by=None):
        self.associated_with.append((a, b, {"confidence": confidence, "superseded_by": superseded_by}))

    def add_same_as(self, a, b, status="pending", tier="flagged_unverified", confidence=0.5, superseded_by=None):
        self.same_as.append((a, b, {
            "status": status, "tier": tier, "confidence": confidence, "superseded_by": superseded_by,
        }))

    def add_appears_in(self, entity_id, source_chunk_id, confidence=1.0, superseded_by=None):
        self.appears_in.append((entity_id, {
            "source_chunk_id": source_chunk_id, "confidence": confidence, "superseded_by": superseded_by,
        }))

    def add_conflict(self, a, b, basis="Contradiction detected.", confidence=1.0):
        self.conflicts.append((a, b, {"basis": basis, "confidence": confidence}))

    def _matches_candidate(self, node, cand_lower):
        props = node["properties"]
        for key in ("canonical_name", "plate", "number", "name", "cnic"):
            val = props.get(key)
            if val and cand_lower in str(val).lower():
                return True
        return False

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        params = params or {}

        if "APPEARS_IN" in cypher_query:
            ids = set(params.get("ids", []))
            return [
                {"n": self.nodes[eid], "r": {"properties": props}, "d": {"properties": {"doc_id": "D1"}}}
                for eid, props in self.appears_in
                if eid in ids and props.get("superseded_by") is None
            ]

        if "ASSOCIATED_WITH" in cypher_query:
            ids = set(params.get("ids", []))
            rows = []
            for a, b, props in self.associated_with:
                if props.get("superseded_by") is not None:
                    continue
                if a in ids:
                    rows.append({"a": self.nodes[a], "r": {"properties": props}, "b": self.nodes[b]})
                if b in ids:
                    rows.append({"a": self.nodes[b], "r": {"properties": props}, "b": self.nodes[a]})
            return rows

        if "SAME_AS" in cypher_query:
            ids = set(params.get("ids", []))
            status_wanted = "confirmed" if "confirmed" in cypher_query else "pending"
            rows = []
            for a, b, props in self.same_as:
                if props.get("status") != status_wanted or props.get("superseded_by") is not None:
                    continue
                if a in ids:
                    rows.append({"a": self.nodes[a], "r": {"properties": props}, "b": self.nodes[b]})
                if b in ids:
                    rows.append({"a": self.nodes[b], "r": {"properties": props}, "b": self.nodes[a]})
            return rows

        if "CONFLICTS_WITH" in cypher_query:
            case_id = params.get("case_id")
            return [
                {"a": self.nodes[a], "r": {"properties": props}, "b": self.nodes[b]}
                for a, b, props in self.conflicts
                if case_id in self.belongs_to_case.get(b, set())
            ]

        if "BELONGS_TO_CASE" in cypher_query and "entity_id IN $ids" in cypher_query:
            ids = set(params.get("ids", []))
            case_id = params.get("case_id")
            return [{"n": self.nodes[eid]} for eid in ids if case_id in self.belongs_to_case.get(eid, set())]

        if "BELONGS_TO_CASE" in cypher_query and "RETURN n, c" in cypher_query:
            # Cross-case recurrence lookup (_find_recurring_entities_for_query) —
            # every node of this label across every case, no case_id filter.
            label = cypher_query.split("MATCH (n:")[1].split(")")[0]
            return [
                {"n": node, "c": {"properties": {"case_id": cid}}}
                for eid, node in self.nodes.items()
                if node["label"] == label
                for cid in self.belongs_to_case.get(eid, set())
            ]

        if "BELONGS_TO_CASE" in cypher_query and "MATCH (n:" in cypher_query and "WHERE" not in cypher_query:
            # Case-wide enumeration seed lookup (_find_all_case_entities) —
            # no candidate string, just "every node of this label in this case".
            label = cypher_query.split("MATCH (n:")[1].split(")")[0]
            case_id = params.get("case_id")
            return [
                {"n": node} for eid, node in self.nodes.items()
                if node["label"] == label and case_id in self.belongs_to_case.get(eid, set())
            ]

        if "BELONGS_TO_CASE" in cypher_query:
            # Within-case seed lookup.
            label = cypher_query.split("MATCH (n:")[1].split(")")[0]
            cand = str(params.get("cand", "")).lower()
            case_id = params.get("case_id")
            return [
                {"n": node} for eid, node in self.nodes.items()
                if node["label"] == label
                and case_id in self.belongs_to_case.get(eid, set())
                and self._matches_candidate(node, cand)
            ]

        if "toLower(n." in cypher_query:
            # Cross-case seed lookup — deliberately unscoped.
            label = cypher_query.split("MATCH (n:")[1].split(")")[0]
            cand = str(params.get("cand", "")).lower()
            return [
                {"n": node} for eid, node in self.nodes.items()
                if node["label"] == label and self._matches_candidate(node, cand)
            ]

        return []


@pytest.fixture
def fake_graph(monkeypatch):
    graph = FakeGraph()
    monkeypatch.setattr(gr, "age_client", graph)
    # Phase 2: several within-case queries now go through
    # case_scope.scoped_cypher(), which calls its OWN module-level
    # `age_client` reference (a separate binding from gr.age_client) —
    # both must point at the fake or the case-scoped call sites would
    # hit the real (unpatched) age_client module instead.
    monkeypatch.setattr(case_scope, "age_client", graph)
    return graph


@pytest.fixture
def fake_chunks(monkeypatch):
    """chunk_id -> {"id", "text", "metadata"} store, standing in for Chroma."""
    store: dict[str, dict] = {}

    async def fake_get_chunks_by_ids(ids):
        return [store[i] for i in ids if i in store]

    monkeypatch.setattr(gr, "get_chunks_by_ids", fake_get_chunks_by_ids)
    return store


# ── Seed resolution ─────────────────────────────────────────────────────────

async def test_within_case_seed_lookup_finds_person_by_name(fake_graph, fake_chunks):
    fake_graph.add_node("P-002", "Person", canonical_name="Waqas Ali Niazi")
    fake_graph.add_case("P-002", "CASE-007")
    fake_chunks["c1"] = {"id": "c1", "text": "Waqas named as accused.", "metadata": {"case_id": "CASE-007", "source": "fir.pdf"}}
    fake_graph.add_appears_in("P-002", "c1", confidence=1.0)

    result = await gr.retrieve_graph("Tell me about Waqas Ali Niazi", "Waqas Ali Niazi", "CASE-007")

    assert result["seed_entities"][0]["entity_id"] == "P-002"
    assert len(result["chunks"]) == 1
    assert result["chunks"][0]["id"] == "c1"
    assert result["hop_count"] == 0


async def test_within_case_query_without_case_id_fails_closed(fake_graph, fake_chunks):
    """A within-case graph query with no active case must never search unscoped."""
    fake_graph.add_node("P-001", "Person", canonical_name="Bilal Shahzad")
    result = await gr.retrieve_graph("who is Bilal Shahzad", "Bilal Shahzad", case_id=None, cross_case=False)
    assert result["chunks"] == []
    assert result["seed_entities"] == []


async def test_seed_candidates_extracts_phone_and_plate_from_query_text():
    candidates = gr._seed_candidates(None, "Has phone 0372-1590538 appeared with vehicle ICT-LE-309?")
    assert "0372-1590538" in candidates
    assert "ICT-LE-309" in candidates


# ── Within-case traversal ────────────────────────────────────────────────────

async def test_associated_with_hop_reaches_a_second_entity_within_case(fake_graph, fake_chunks):
    fake_graph.add_node("P-010", "Person", canonical_name="Accused Person")
    fake_graph.add_node("P-011", "Person", canonical_name="Known Associate")
    fake_graph.add_case("P-010", "CASE-009")
    fake_graph.add_case("P-011", "CASE-009")
    fake_graph.add_associated("P-010", "P-011", confidence=0.9)
    fake_chunks["c2"] = {"id": "c2", "text": "Known Associate named in the case diary.", "metadata": {"case_id": "CASE-009", "source": "diary.pdf"}}
    fake_graph.add_appears_in("P-011", "c2", confidence=1.0)

    result = await gr.retrieve_graph("Who is connected to the accused?", "Accused Person", "CASE-009", max_hops=2)

    assert result["hop_count"] == 1
    assert result["chunks"][0]["via_entity"] == "Accused Person"
    assert result["chunks"][0]["graph_confidence"] == pytest.approx(0.9)


async def test_traversal_never_crosses_into_another_case(fake_graph, fake_chunks):
    """A 1-hop ASSOCIATED_WITH neighbor who belongs to a DIFFERENT case must be excluded."""
    fake_graph.add_node("P-020", "Person", canonical_name="Seed Person")
    fake_graph.add_node("P-021", "Person", canonical_name="Other Case Person")
    fake_graph.add_case("P-020", "CASE-020")
    fake_graph.add_case("P-021", "CASE-099")  # different case
    fake_graph.add_associated("P-020", "P-021", confidence=0.9)
    fake_chunks["c3"] = {"id": "c3", "text": "irrelevant", "metadata": {"case_id": "CASE-099"}}
    fake_graph.add_appears_in("P-021", "c3", confidence=1.0)

    result = await gr.retrieve_graph("who is connected to seed person", "Seed Person", "CASE-020", max_hops=2)

    assert result["chunks"] == []  # the only chunk belongs to the excluded neighbor


async def test_hop_cap_and_compounded_confidence(fake_graph, fake_chunks):
    """A 3-hop chain of 0.9-confidence edges compounds to ~0.9^3, per the spec's own example."""
    fake_graph.add_node("P-030", "Person", canonical_name="Seed")
    fake_graph.add_node("P-031", "Person", canonical_name="Hop1")
    fake_graph.add_node("P-032", "Person", canonical_name="Hop2")
    fake_graph.add_node("P-033", "Person", canonical_name="Hop3")
    for eid in ("P-030", "P-031", "P-032", "P-033"):
        fake_graph.add_case(eid, "CASE-030")
    fake_graph.add_associated("P-030", "P-031", confidence=0.9)
    fake_graph.add_associated("P-031", "P-032", confidence=0.9)
    fake_graph.add_associated("P-032", "P-033", confidence=0.9)
    fake_chunks["c4"] = {"id": "c4", "text": "Hop3 mention.", "metadata": {"case_id": "CASE-030"}}
    fake_graph.add_appears_in("P-033", "c4", confidence=1.0)

    result = await gr.retrieve_graph("network of seed", "Seed", "CASE-030", max_hops=3)

    assert result["hop_count"] == 3
    assert result["compounded_confidence"] == pytest.approx(0.9 ** 3, rel=1e-6)


# ── SAME_AS confirmed-only identity rule ────────────────────────────────────

async def test_confirmed_same_as_is_followed_as_identity_cross_case(fake_graph, fake_chunks):
    """A CONFIRMED SAME_AS edge across cases is the recurring-entity mechanism (ORG-001/002 pattern)."""
    fake_graph.add_node("P-002", "Person", canonical_name="Waqas Ali Niazi")
    fake_graph.add_node("P-002-dup", "Person", canonical_name="Waqas A. Niazi")
    fake_graph.add_case("P-002", "CASE-007")
    fake_graph.add_case("P-002-dup", "CASE-009")
    fake_graph.add_same_as("P-002", "P-002-dup", status="confirmed", tier="cnic_auto", confidence=0.99)
    fake_chunks["c5"] = {"id": "c5", "text": "Waqas mentioned in CASE-009.", "metadata": {"case_id": "CASE-009"}}
    fake_graph.add_appears_in("P-002-dup", "c5", confidence=1.0)

    result = await gr.retrieve_graph(
        "which cases involve Waqas Ali Niazi", "Waqas Ali Niazi", case_id=None, cross_case=True,
        user_role="supervisor",
    )

    chunk_ids = {c["id"] for c in result["chunks"]}
    assert "c5" in chunk_ids
    assert result["unconfirmed_links"] == []


async def test_pending_same_as_is_surfaced_but_never_followed_as_identity(fake_graph, fake_chunks):
    """
    The P-006 flagship rule: a pending/flagged_unverified SAME_AS must
    appear in unconfirmed_links, but must NOT pull the other node's
    chunks in as if it were a confirmed identity.
    """
    fake_graph.add_node("P-006", "Person", canonical_name="Adnan Qureshi Waheed")
    fake_graph.add_node("P-006-case016", "Person", canonical_name="Adnan Qureshi")
    fake_graph.add_case("P-006", "CASE-015")
    fake_graph.add_case("P-006-case016", "CASE-016")
    fake_graph.add_same_as("P-006", "P-006-case016", status="pending", tier="flagged_unverified", confidence=0.55)
    fake_chunks["c6"] = {"id": "c6", "text": "Adnan mentioned in CASE-016.", "metadata": {"case_id": "CASE-016"}}
    fake_graph.add_appears_in("P-006-case016", "c6", confidence=1.0)

    result = await gr.retrieve_graph(
        "is this repeat fraud offender linked elsewhere", "Adnan Qureshi Waheed", case_id=None, cross_case=True,
        user_role="supervisor",
    )

    chunk_ids = {c["id"] for c in result["chunks"]}
    assert "c6" not in chunk_ids, "a pending SAME_AS must never be treated as confirmed identity"
    assert len(result["unconfirmed_links"]) == 1
    assert result["unconfirmed_links"][0]["status"] == "pending"
    assert result["unconfirmed_links"][0]["tier"] == "flagged_unverified"


async def test_rejected_same_as_is_never_surfaced(fake_graph, fake_chunks):
    fake_graph.add_node("P-100", "Person", canonical_name="Person A")
    fake_graph.add_node("P-101", "Person", canonical_name="Person B")
    fake_graph.add_case("P-100", "CASE-100")
    fake_graph.add_case("P-101", "CASE-101")
    fake_graph.add_same_as("P-100", "P-101", status="rejected", tier="human_review", confidence=0.2)

    result = await gr.retrieve_graph(
        "Person A elsewhere", "Person A", case_id=None, cross_case=True, user_role="supervisor"
    )

    assert result["unconfirmed_links"] == []


async def test_cross_case_traversal_by_investigator_is_hard_blocked(fake_graph, fake_chunks):
    """
    Phase 7 RBAC: cross-case traversal requires supervisor role or higher.
    An investigator (the default role) must get a hard PermissionError, never
    a silent downgrade to a within-case result.
    """
    with pytest.raises(PermissionError):
        await gr.retrieve_graph(
            "Person A elsewhere", "Person A", case_id=None, cross_case=True, user_role="investigator"
        )


async def test_denied_cross_case_traversal_never_arms_the_rls_bypass(fake_graph, fake_chunks):
    """
    Phase 2 regression test for issues.md's High "cross-case RLS bypass
    flag is armed before its own role check" finding. Before the fix,
    current_cross_case was set to True by the orchestrator the instant the
    router classified a query as cross-case — before this function's role
    check ran, and it was never reset on a PermissionError. Now it's armed
    HERE, only after the role check passes, so a denied attempt must leave
    it False — there's no window where an unauthorized caller had it
    armed and then had it (not) cleared; it was never armed for them.
    """
    from src.database.postgres import current_cross_case

    current_cross_case.set(False)
    with pytest.raises(PermissionError):
        await gr.retrieve_graph(
            "Person A elsewhere", "Person A", case_id=None, cross_case=True, user_role="investigator"
        )
    assert current_cross_case.get() is False


async def test_authorized_cross_case_traversal_arms_the_rls_bypass(fake_graph, fake_chunks):
    """Mirror of the above: an authorized (supervisor+) cross-case call DOES arm the bypass, after its role check passes."""
    from src.database.postgres import current_cross_case

    fake_graph.add_node("P-200", "Person", canonical_name="Person C")
    fake_graph.add_case("P-200", "CASE-200")

    current_cross_case.set(False)
    await gr.retrieve_graph(
        "Person C elsewhere", "Person C", case_id=None, cross_case=True, user_role="supervisor"
    )
    assert current_cross_case.get() is True


# ── ADDR-002 negative test ───────────────────────────────────────────────────

async def test_shared_address_is_not_presented_as_a_relationship(fake_graph, fake_chunks):
    """
    Two people who merely share an address (no ASSOCIATED_WITH edge
    between them) must never be connected by traversal — LOCATED_AT is
    deliberately never used to hop between two different entities (see
    graph_retriever.py module docstring).
    """
    fake_graph.add_node("P-200", "Person", canonical_name="Occupant One")
    fake_graph.add_node("P-201", "Person", canonical_name="Occupant Two")
    fake_graph.add_case("P-200", "CASE-010")
    fake_graph.add_case("P-201", "CASE-013")
    # No ASSOCIATED_WITH edge is added — only a shared address would exist
    # in the real graph, which this module never queries for hop expansion.
    fake_chunks["c7"] = {"id": "c7", "text": "Occupant Two's own mention.", "metadata": {"case_id": "CASE-013"}}
    fake_graph.add_appears_in("P-201", "c7", confidence=1.0)

    result = await gr.retrieve_graph("who is connected to Occupant One", "Occupant One", "CASE-010", max_hops=2)

    chunk_ids = {c["id"] for c in result["chunks"]}
    assert "c7" not in chunk_ids, "a shared address must never surface as a relationship"


# ── Missing-chunk provenance rule ────────────────────────────────────────────

async def test_hop_with_no_ingested_document_is_dropped_not_passed_text_less(fake_graph, fake_chunks):
    fake_graph.add_node("P-300", "Person", canonical_name="Seed Only")
    fake_graph.add_case("P-300", "CASE-300")
    # source_chunk_id "missing" was never ingested into Chroma (fake_chunks has no entry for it).
    fake_graph.add_appears_in("P-300", "missing", confidence=1.0)

    result = await gr.retrieve_graph("Seed Only", "Seed Only", "CASE-300")

    assert result["chunks"] == []


# ── Conflict surfacing (CONFLICTS_WITH edges) ───────────────────────────────

async def test_conflicts_surfaced_when_target_entity_matches_no_seed_node(fake_graph, fake_chunks):
    """
    A case/FIR-anchored query (e.g. target_entity is a case number, not a
    Person/Vehicle/Phone/Org) matches no seed node — but pre-computed
    conflicts for the case must still be returned instead of an empty
    result.
    """
    fake_graph.add_node("INC-01", "Incident", description="Burglary reported ~7pm")
    fake_graph.add_node("INC-02", "Incident", description="Burglary reported ~10pm")
    fake_graph.add_case("INC-01", "CASE-BUR-009")
    fake_graph.add_case("INC-02", "CASE-BUR-009")
    fake_graph.add_conflict("INC-01", "INC-02", basis="Witnesses disagree on the time of the burglary.", confidence=0.85)
    fake_chunks["w1"] = {"id": "w1", "text": "Around 7pm the suspect entered.", "metadata": {"case_id": "CASE-BUR-009"}}
    fake_chunks["w2"] = {"id": "w2", "text": "Around 10pm the suspect entered.", "metadata": {"case_id": "CASE-BUR-009"}}
    fake_graph.add_appears_in("INC-01", "w1", confidence=1.0)
    fake_graph.add_appears_in("INC-02", "w2", confidence=1.0)

    result = await gr.retrieve_graph(
        "Do the witness statements agree on when the burglary happened?",
        "FIR-2026-BUR-009", "CASE-BUR-009",
    )

    chunk_ids = {c["id"] for c in result["chunks"]}
    assert chunk_ids == {"w1", "w2"}
    assert result["seed_entities"] == []
    for c in result["chunks"]:
        assert c["metadata"]["conflict_basis"] == "Witnesses disagree on the time of the burglary."


async def test_conflicts_merged_alongside_normal_traversal_chunks(fake_graph, fake_chunks):
    """When a real seed entity IS matched, conflict chunks ride along with the normal ASSOCIATED_WITH traversal chunks, not instead of them."""
    fake_graph.add_node("P-050", "Person", canonical_name="Seed Suspect")
    fake_graph.add_node("P-051", "Person", canonical_name="Associate")
    fake_graph.add_case("P-050", "CASE-050")
    fake_graph.add_case("P-051", "CASE-050")
    fake_graph.add_associated("P-050", "P-051", confidence=0.9)
    fake_chunks["c9"] = {"id": "c9", "text": "Associate named in case diary.", "metadata": {"case_id": "CASE-050"}}
    fake_graph.add_appears_in("P-051", "c9", confidence=1.0)

    fake_graph.add_node("INC-10", "Incident", description="Incident A")
    fake_graph.add_node("INC-11", "Incident", description="Incident B")
    fake_graph.add_case("INC-10", "CASE-050")
    fake_graph.add_case("INC-11", "CASE-050")
    fake_graph.add_conflict("INC-10", "INC-11", basis="Conflicting dates.", confidence=1.0)
    fake_chunks["w10"] = {"id": "w10", "text": "Event on Jan 1.", "metadata": {"case_id": "CASE-050"}}
    fake_chunks["w11"] = {"id": "w11", "text": "Event on Jan 2.", "metadata": {"case_id": "CASE-050"}}
    fake_graph.add_appears_in("INC-10", "w10", confidence=1.0)
    fake_graph.add_appears_in("INC-11", "w11", confidence=1.0)

    result = await gr.retrieve_graph("Who is connected to Seed Suspect, and is anything inconsistent?", "Seed Suspect", "CASE-050", max_hops=2)

    chunk_ids = {c["id"] for c in result["chunks"]}
    assert {"c9", "w10", "w11"} <= chunk_ids


# ── Case-wide enumeration (no named entity) ─────────────────────────────────

async def test_case_wide_enumeration_seeds_from_all_case_entities(fake_graph, fake_chunks):
    """
    "How many accused are involved in CASE-009?" names no specific entity —
    target_entity is None and nothing in the query text matches a CNIC/
    phone/plate regex. This must still seed from every entity belonging to
    the case (not return empty just because nothing was named).
    """
    fake_graph.add_node("P-002", "Person", canonical_name="Waqas Ali Niazi")
    fake_graph.add_node("P-003", "Person", canonical_name="Kamran Sheikh")
    fake_graph.add_case("P-002", "CASE-009")
    fake_graph.add_case("P-003", "CASE-009")
    fake_chunks["c20"] = {"id": "c20", "text": "Waqas named as accused.", "metadata": {"case_id": "CASE-009"}}
    fake_chunks["c21"] = {"id": "c21", "text": "Kamran named as co-accused.", "metadata": {"case_id": "CASE-009"}}
    fake_graph.add_appears_in("P-002", "c20", confidence=1.0)
    fake_graph.add_appears_in("P-003", "c21", confidence=1.0)

    result = await gr.retrieve_graph(
        "How many accused are involved in CASE-009 and how are they connected?",
        target_entity=None, case_id="CASE-009",
    )

    seeded_ids = {e["entity_id"] for e in result["seed_entities"]}
    assert seeded_ids == {"P-002", "P-003"}
    chunk_ids = {c["id"] for c in result["chunks"]}
    assert chunk_ids == {"c20", "c21"}


async def test_case_wide_enumeration_never_crosses_cases(fake_graph, fake_chunks):
    """The case-wide seed fallback must stay scoped to the active case, same as the literal-match path."""
    fake_graph.add_node("P-500", "Person", canonical_name="In Case")
    fake_graph.add_node("P-501", "Person", canonical_name="Other Case")
    fake_graph.add_case("P-500", "CASE-500")
    fake_graph.add_case("P-501", "CASE-501")

    result = await gr.retrieve_graph("Who is involved in this case?", target_entity=None, case_id="CASE-500")

    seeded_ids = {e["entity_id"] for e in result["seed_entities"]}
    assert seeded_ids == {"P-500"}


async def test_case_wide_enumeration_not_triggered_cross_case(fake_graph, fake_chunks):
    """No case_id (cross-case, target_entity None) must not trigger the case-wide fallback — nothing to scope to."""
    result = await gr.retrieve_graph("who else is involved?", target_entity=None, case_id=None, cross_case=False)

    assert result["chunks"] == []
    assert result["seed_entities"] == []


# ── Cross-case recurrence fallback (no named instance) ──────────────────────

async def test_cross_case_recurrence_seeds_phone_numbers_appearing_in_multiple_cases(fake_graph, fake_chunks):
    """
    "Has any phone number been used across multiple cyber fraud cases?"
    names no specific number — target_entity is None and the query text
    contains no literal phone number. Must still seed from PhoneNumber
    nodes that recur across 2+ cases, picked via the "phone"/"number"
    keyword hint in the query text.
    """
    fake_graph.add_node("PH-001", "PhoneNumber", number="0372-1590538")
    fake_graph.add_node("PH-002", "PhoneNumber", number="0308-8899730")
    fake_graph.add_node("PH-999", "PhoneNumber", number="0300-0000000")  # only one case — not recurring
    fake_graph.add_case("PH-001", "CASE-004")
    fake_graph.add_case("PH-001", "CASE-005")
    fake_graph.add_case("PH-002", "CASE-005")
    fake_graph.add_case("PH-002", "CASE-006")
    fake_graph.add_case("PH-999", "CASE-004")
    fake_chunks["p1"] = {"id": "p1", "text": "0372-1590538 used in fraud.", "metadata": {"case_id": "CASE-004"}}
    fake_graph.add_appears_in("PH-001", "p1", confidence=1.0)

    result = await gr.retrieve_graph(
        "Has any phone number been used across multiple cyber fraud cases?",
        target_entity=None, case_id=None, cross_case=True, user_role="supervisor",
    )

    seeded_ids = {e["entity_id"] for e in result["seed_entities"]}
    assert seeded_ids == {"PH-001", "PH-002"}
    assert "PH-999" not in seeded_ids


async def test_cross_case_recurrence_matches_the_everyday_urdu_word_for_people(fake_graph, fake_chunks):
    """
    Regression guard: "لوگوں" ("people", the common everyday Urdu word) used
    to be missing from _LABEL_KEYWORDS' Person entry even though the more
    formal synonym "افراد" was present — a query phrased with the everyday
    word matched no label at all and silently returned empty instead of
    seeding from recurring Person nodes. Uses a recurrence-shaped query (no
    _ENUMERATION_KEYWORDS present) to isolate the keyword-matching fix from
    the separate enumeration behavior covered below.
    """
    fake_graph.add_node("P-700", "Person", canonical_name="Waqas Ali Niazi")
    fake_graph.add_node("P-701", "Person", canonical_name="Bilal Shahzad")
    fake_graph.add_node("P-999", "Person", canonical_name="Only One Case")
    fake_graph.add_case("P-700", "CASE-700")
    fake_graph.add_case("P-700", "CASE-701")
    fake_graph.add_case("P-701", "CASE-701")
    fake_graph.add_case("P-701", "CASE-702")
    fake_graph.add_case("P-999", "CASE-700")

    result = await gr.retrieve_graph(
        "کیا کوئی لوگوں کا تعلق متعدد مقدمات میں ہے؟",
        target_entity=None, case_id=None, cross_case=True, user_role="supervisor",
    )

    seeded_ids = {e["entity_id"] for e in result["seed_entities"]}
    assert seeded_ids == {"P-700", "P-701"}
    assert "P-999" not in seeded_ids


async def test_cross_case_enumeration_returns_every_instance_not_just_recurring(fake_graph, fake_chunks):
    """
    Gap 3 fix: "list of all people mentioned in the cases" is an
    ENUMERATION question, not a recurrence question — it must return every
    Person across every case, including P-999 which appears in only one
    case and would be excluded by the default min_cases=2 recurrence path.
    This is the exact query that surfaced the "no connections found" UX
    problem live: XGRAPH's old recurrence-only behavior legitimately found
    nothing here (no one recurs), which is a different, narrower answer
    than what was actually asked.
    """
    fake_graph.add_node("P-700", "Person", canonical_name="Waqas Ali Niazi")
    fake_graph.add_node("P-701", "Person", canonical_name="Bilal Shahzad")
    fake_graph.add_node("P-999", "Person", canonical_name="Only One Case")
    fake_graph.add_case("P-700", "CASE-700")
    fake_graph.add_case("P-700", "CASE-701")
    fake_graph.add_case("P-701", "CASE-701")
    fake_graph.add_case("P-701", "CASE-702")
    fake_graph.add_case("P-999", "CASE-700")

    result = await gr.retrieve_graph(
        "مقدمات میں مذکور تمام لوگوں کی فہرست",
        target_entity=None, case_id=None, cross_case=True, user_role="supervisor",
    )

    seeded_ids = {e["entity_id"] for e in result["seed_entities"]}
    assert seeded_ids == {"P-700", "P-701", "P-999"}


async def test_cross_case_enumeration_is_capped_and_favors_higher_case_counts(fake_graph, fake_chunks):
    """
    An unbounded "list everyone" over a large corpus would trigger a
    hop-traversal/chunk-fetch pass over every single entity — cap it, and
    make the cut deterministic (higher case-recurrence count survives
    first) rather than an arbitrary Cypher row order.
    """
    for i in range(60):
        node_id = f"P-{i:03d}"
        fake_graph.add_node(node_id, "Person", canonical_name=f"Person {i}")
        fake_graph.add_case(node_id, f"CASE-{i:03d}")
    # Two entities that recur across 2 cases each — must survive the cap
    # ahead of the 60 single-case entities above.
    fake_graph.add_node("P-HIGH-1", "Person", canonical_name="High Recurrence 1")
    fake_graph.add_case("P-HIGH-1", "CASE-900")
    fake_graph.add_case("P-HIGH-1", "CASE-901")
    fake_graph.add_node("P-HIGH-2", "Person", canonical_name="High Recurrence 2")
    fake_graph.add_case("P-HIGH-2", "CASE-902")
    fake_graph.add_case("P-HIGH-2", "CASE-903")

    result = await gr.retrieve_graph(
        "list all the people mentioned in the cases",
        target_entity=None, case_id=None, cross_case=True, user_role="supervisor",
    )

    seeded_ids = {e["entity_id"] for e in result["seed_entities"]}
    assert len(seeded_ids) == 50, "must be capped at the 50-entity limit"
    assert {"P-HIGH-1", "P-HIGH-2"} <= seeded_ids, (
        "higher-recurrence entities must survive the cap ahead of single-case ones"
    )


async def test_cross_case_recurrence_stays_empty_with_no_type_hint(fake_graph, fake_chunks):
    """
    ADDR-002-style negative test: "Are the occupants of the shared boarding
    house in CASE-010 and CASE-013 related to each other?" names no entity
    and hints at no tracked label (person/vehicle/phone/org) — must return
    empty, not scan the whole graph and surface unrelated recurring
    entities from other cases as if they were relevant.
    """
    fake_graph.add_node("P-900", "Person", canonical_name="Unrelated Recurring Person")
    fake_graph.add_case("P-900", "CASE-900")
    fake_graph.add_case("P-900", "CASE-901")  # recurs, but nothing to do with this query

    result = await gr.retrieve_graph(
        "Are the occupants of the shared boarding house in CASE-010 and CASE-013 related to each other?",
        target_entity=None, case_id=None, cross_case=True, user_role="supervisor",
    )

    assert result["chunks"] == []
    assert result["seed_entities"] == []


# ── _SEED_LABELS property keys (M8 of the Muhafiz Data API migration,
# docs/decisions/0001-muhafiz-api-migration.md) ─────────────────────────

def test_seed_labels_phone_number_matches_what_is_actually_written():
    """
    Regression: _SEED_LABELS["PhoneNumber"] used to check only `number`,
    a property no writer in this codebase has ever set —
    _run_graph_extraction's phone-writing loop (src/ingestion/service.py)
    writes {"canonical_name": phone, "phone": phone}. The seed lookup
    could therefore never match a real PhoneNumber node.
    """
    id_props, _display = gr._SEED_LABELS["PhoneNumber"]
    assert "number" not in id_props
    assert "canonical_name" in id_props or "phone" in id_props


def test_seed_labels_organization_matches_what_is_actually_written():
    """Same bug, Organization: every write uses canonical_name only,
    never `name`."""
    id_props, _display = gr._SEED_LABELS["Organization"]
    assert "name" not in id_props
    assert "canonical_name" in id_props
