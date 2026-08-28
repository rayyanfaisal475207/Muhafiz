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
        self.assigned_to: list[tuple] = []
        # [Bug fix, 2026-08-27 route sweep, BUG-3/4] donor_id -> survivor_id,
        # for scripts/merge_confirmed_duplicate_persons.py's physical merge.
        # Models the net reader-visible effect of that script's redirect:
        # the donor's OWN BELONGS_TO_CASE edge is marked superseded (the
        # survivor holds the new, active one instead), so every
        # BELONGS_TO_CASE-based lookup below must treat a merged donor as
        # if it no longer belongs to any case at all — real Cypher does
        # this via `n.merged_into IS NULL AND b.superseded_by IS NULL`;
        # this fake reproduces the same outcome directly since it doesn't
        # evaluate WHERE clauses, only dispatches on cypher_query shape.
        self.merged_into: dict[str, str] = {}

    def add_node(self, entity_id, label, **props):
        self.nodes[entity_id] = {"id": entity_id, "label": label, "properties": {"entity_id": entity_id, **props}}

    def add_case(self, entity_id, case_id):
        self.belongs_to_case.setdefault(entity_id, set()).add(case_id)

    def add_merge(self, donor_id: str, survivor_id: str) -> None:
        """Mark `donor_id` as physically merged into `survivor_id` —
        see `self.merged_into`'s own docstring above for what this models."""
        self.merged_into[donor_id] = survivor_id
        self.nodes[donor_id]["properties"]["merged_into"] = survivor_id

    def add_associated(self, a, b, confidence=1.0, superseded_by=None, source_doc_id=None, basis=None):
        self.associated_with.append((a, b, {
            "confidence": confidence, "superseded_by": superseded_by,
            "source_doc_id": source_doc_id, "basis": basis,
        }))

    def add_same_as(self, a, b, status="pending", tier="flagged_unverified", confidence=0.5, superseded_by=None, basis="matched on near-identical name"):
        self.same_as.append((a, b, {
            "status": status, "tier": tier, "confidence": confidence, "superseded_by": superseded_by, "basis": basis,
        }))

    def add_appears_in(self, entity_id, source_chunk_id, confidence=1.0, superseded_by=None,
                        source_doc_id=None, surface_text=None, doc_type=None, filename=None,
                        doc_case_id=None):
        """
        `source_chunk_id=None` (with a real `source_doc_id`) models a
        structured-extraction write (src/graph/structured_projection.py —
        never populates source_chunk_id, see graph_retriever.py's own
        _synthetic_evidence_chunk() docstring) — the shape the synthetic-
        evidence-chunk bug fix tests below need, distinct from the
        existing "chunk_id given but never ingested into Chroma" case
        already covered elsewhere in this file.

        `doc_case_id`: the case THIS DOCUMENT belongs to, independent of
        which case(s) `entity_id` itself is linked to via add_case() — a
        real Document has exactly one BELONGS_TO_CASE edge of its own
        (structured_projection.py), which can genuinely differ from an
        entity's case set once the entity recurs across cases. Defaults
        to `None`, meaning "fall back to the entity's own case" — correct
        for every existing single-case test fixture, where the two are
        the same value anyway.
        """
        self.appears_in.append((entity_id, {
            "source_chunk_id": source_chunk_id, "confidence": confidence, "superseded_by": superseded_by,
            "source_doc_id": source_doc_id, "surface_text": surface_text,
            "_doc_type": doc_type, "_filename": filename, "_doc_case_id": doc_case_id,
        }))

    def add_conflict(self, a, b, basis="Contradiction detected.", confidence=1.0):
        self.conflicts.append((a, b, {"basis": basis, "confidence": confidence}))

    def add_assigned_to(self, entity_id, case_id, role, superseded_by=None):
        """[findings.md Module 8 follow-up] Models an Officer's per-case
        ASSIGNED_TO edge — see graph_retriever._fetch_appears_in()'s own
        docstring for why this is correlated to a SPECIFIC case, not
        looked up unscoped (the same officer can carry a different role on
        a different case)."""
        self.assigned_to.append((entity_id, case_id, {"role": role, "superseded_by": superseded_by}))

    def _matches_candidate(self, node, cand_lower, cand_skeleton=None):
        props = node["properties"]
        # "phone"/"belt_no" added for the Person.phone / Officer
        # _SEED_LABELS bug fix — this fake must recognize every property
        # real _SEED_LABELS id_props actually reference, not a fixed
        # subset, or a fix to the real dict would pass real code but
        # still fail here.
        for key in ("canonical_name", "plate", "number", "name", "cnic", "phone", "belt_no"):
            val = props.get(key)
            if val and cand_lower in str(val).lower():
                return True
        # Cross-lingual name matching: mirrors the real
        # `n.name_skeleton = $cand_skeleton` clause _find_seed_nodes() adds
        # for name-carrying labels.
        if cand_skeleton and props.get("name_skeleton") == cand_skeleton:
            return True
        return False

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        params = params or {}

        # [findings.md Module 8 follow-up] Checked BEFORE the "APPEARS_IN"
        # branch's substring check even though it also mentions "Case" --
        # graph_retriever._fetch_officer_roles()'s own query contains
        # "ASSIGNED_TO" but never "APPEARS_IN", so this must be its own
        # branch, not folded into the one below (that's the exact fan-out
        # bug this two-query split exists to avoid on the REAL Cypher side
        # too -- see _fetch_appears_in()'s own docstring).
        if "ASSIGNED_TO" in cypher_query and "APPEARS_IN" not in cypher_query:
            ids = set(params.get("ids", []))
            return [
                {"entity_id": eid, "case_id": case_id, "role": props.get("role")}
                for eid, case_id, props in self.assigned_to
                if eid in ids and props.get("superseded_by") is None
            ]

        if "APPEARS_IN" in cypher_query:
            ids = set(params.get("ids", []))
            rows = []
            for eid, props in self.appears_in:
                if eid not in ids or props.get("superseded_by") is not None:
                    continue
                row_case_id = props.get("_doc_case_id") or next(iter(self.belongs_to_case.get(eid, ())), None)
                rows.append({
                    "n": self.nodes[eid], "r": {"properties": props},
                    "d": {"properties": {
                        "doc_id": props.get("source_doc_id") or "D1",
                        "doc_type": props.get("_doc_type"),
                        "filename": props.get("_filename"),
                    }},
                    # [Bug fix] _fetch_appears_in()'s OPTIONAL MATCH now
                    # joins off the DOCUMENT's own BELONGS_TO_CASE, not
                    # the entity's — a document belongs to exactly one
                    # case, so no fan-out is possible, unlike an entity
                    # that genuinely recurs across many. `doc_case_id`
                    # models that per-document edge directly; falling
                    # back to the entity's own (first) case only when a
                    # test fixture never set doc_case_id, which is every
                    # existing single-case fixture — there the two values
                    # coincide anyway.
                    "case_id": row_case_id,
                })
            return rows

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

        # [Bug fix, 2026-08-27 route sweep, BUG-3/4] every branch below
        # excludes `eid in self.merged_into` — see self.merged_into's own
        # docstring for why. Mirrors the real code's `n.merged_into IS
        # NULL AND b.superseded_by IS NULL` filter (graph_retriever.py).

        if "BELONGS_TO_CASE" in cypher_query and "case_ids" in params and "RETURN n, c" not in cypher_query:
            # Milestone E1: jurisdiction-narrowed cross-case seed lookup
            # (_find_seed_nodes's cross_case branch, jurisdiction_case_ids
            # given) — same candidate matching as the plain cross-case
            # branch below, plus the case_id allow-list.
            label = cypher_query.split("MATCH (n:")[1].split(")")[0]
            cand = str(params.get("cand", "")).lower()
            cand_skeleton = params.get("cand_skeleton")
            allowed = set(params.get("case_ids", []))
            return [
                {"n": node} for eid, node in self.nodes.items()
                if node["label"] == label
                and eid not in self.merged_into
                and self._matches_candidate(node, cand, cand_skeleton)
                and self.belongs_to_case.get(eid, set()) & allowed
            ]

        if "BELONGS_TO_CASE" in cypher_query and "entity_id IN $ids" in cypher_query:
            ids = set(params.get("ids", []))
            case_id = params.get("case_id")
            return [
                {"n": self.nodes[eid]} for eid in ids
                if eid not in self.merged_into and case_id in self.belongs_to_case.get(eid, set())
            ]

        if "BELONGS_TO_CASE" in cypher_query and "RETURN n, c" in cypher_query:
            # Cross-case recurrence lookup (_find_recurring_entities_for_query) —
            # every node of this label across every case, no case_id filter.
            label = cypher_query.split("MATCH (n:")[1].split(")")[0]
            return [
                {"n": node, "c": {"properties": {"case_id": cid}}}
                for eid, node in self.nodes.items()
                if node["label"] == label and eid not in self.merged_into
                for cid in self.belongs_to_case.get(eid, set())
            ]

        if "BELONGS_TO_CASE" in cypher_query and "MATCH (n:" in cypher_query and "cand" not in params:
            # Case-wide enumeration seed lookup (_find_all_case_entities) —
            # no candidate string, just "every node of this label in this
            # case". Discriminated on the ABSENCE of a `cand` param
            # (rather than "no WHERE clause", which stopped being true
            # once the merged_into/superseded_by filter above added one) —
            # this is the one BELONGS_TO_CASE call site that never binds
            # `$cand` at all.
            label = cypher_query.split("MATCH (n:")[1].split(")")[0]
            case_id = params.get("case_id")
            return [
                {"n": node} for eid, node in self.nodes.items()
                if node["label"] == label
                and eid not in self.merged_into
                and case_id in self.belongs_to_case.get(eid, set())
            ]

        if "BELONGS_TO_CASE" in cypher_query:
            # Within-case seed lookup.
            label = cypher_query.split("MATCH (n:")[1].split(")")[0]
            cand = str(params.get("cand", "")).lower()
            cand_skeleton = params.get("cand_skeleton")
            case_id = params.get("case_id")
            return [
                {"n": node} for eid, node in self.nodes.items()
                if node["label"] == label
                and eid not in self.merged_into
                and case_id in self.belongs_to_case.get(eid, set())
                and self._matches_candidate(node, cand, cand_skeleton)
            ]

        if "toLower(n." in cypher_query:
            # Cross-case seed lookup — deliberately unscoped (case-wise;
            # still excludes a merged donor, per the note above).
            label = cypher_query.split("MATCH (n:")[1].split(")")[0]
            cand = str(params.get("cand", "")).lower()
            cand_skeleton = params.get("cand_skeleton")
            return [
                {"n": node} for eid, node in self.nodes.items()
                if node["label"] == label and eid not in self.merged_into
                and self._matches_candidate(node, cand, cand_skeleton)
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


async def test_urdu_stored_name_found_by_english_query(fake_graph, fake_chunks):
    """
    Cross-lingual graph name matching: a Person node whose canonical_name
    is in Urdu script must still be findable as a seed by an English-
    worded query, via the precomputed name_skeleton property
    (entity_resolution.resolve_and_write) and _find_seed_nodes()'s
    skeleton-equality match.
    """
    skeleton = gr._consonant_skeleton("ظفر اقبال")
    fake_graph.add_node("P-URDU", "Person", canonical_name="ظفر اقبال", name_skeleton=skeleton)
    fake_graph.add_case("P-URDU", "CASE-009")
    fake_chunks["c1"] = {"id": "c1", "text": "ظفر اقبال ملزم کے طور پر نامزد.", "metadata": {"case_id": "CASE-009", "source": "fir.pdf"}}
    fake_graph.add_appears_in("P-URDU", "c1", confidence=1.0)

    result = await gr.retrieve_graph("Tell me about Zafar Iqbal", "Zafar Iqbal", "CASE-009")

    assert result["seed_entities"][0]["entity_id"] == "P-URDU"
    assert len(result["chunks"]) == 1


async def test_english_stored_name_found_by_urdu_query(fake_graph, fake_chunks):
    """Same fix, opposite direction: an English-stored canonical_name found
    by an Urdu-script query."""
    skeleton = gr._consonant_skeleton("Zafar Iqbal")
    fake_graph.add_node("P-ENG", "Person", canonical_name="Zafar Iqbal", name_skeleton=skeleton)
    fake_graph.add_case("P-ENG", "CASE-010")
    fake_chunks["c1"] = {"id": "c1", "text": "Zafar Iqbal named as accused.", "metadata": {"case_id": "CASE-010", "source": "fir.pdf"}}
    fake_graph.add_appears_in("P-ENG", "c1", confidence=1.0)

    result = await gr.retrieve_graph("ظفر اقبال کے بارے میں بتائیں", "ظفر اقبال", "CASE-010")

    assert result["seed_entities"][0]["entity_id"] == "P-ENG"
    assert len(result["chunks"]) == 1


async def test_unrelated_name_does_not_false_positive_via_skeleton(fake_graph, fake_chunks):
    """
    The skeleton is a coarse phonetic reduction, so it must not turn into
    a loose fuzzy match — an unrelated name in the other script must NOT
    be found just because a query happens to be in that script too.
    """
    skeleton = gr._consonant_skeleton("محمد حنیف")  # unrelated to "Zafar Iqbal"
    fake_graph.add_node("P-OTHER", "Person", canonical_name="محمد حنیف", name_skeleton=skeleton)
    fake_graph.add_case("P-OTHER", "CASE-011")

    result = await gr.retrieve_graph("Tell me about Zafar Iqbal", "Zafar Iqbal", "CASE-011")

    assert result["seed_entities"] == []


async def test_within_case_seed_lookup_excludes_a_merged_donor_node(fake_graph, fake_chunks):
    """
    [Bug fix, 2026-08-27 route sweep, BUG-3/4] scripts/merge_confirmed_
    duplicate_persons.py physically merges confirmed-duplicate Person
    nodes but never deletes the donor — it stays in the graph, tagged
    merged_into, with its BELONGS_TO_CASE edge superseded by a new one on
    the survivor. Seed lookup must resolve to ONLY the survivor: a donor
    matching by name must not come back as a second, independent seed
    (this was live-confirmed on the real graph — a single canonical name
    with 141 already-merged donors returned 146 seed candidates instead
    of 1, diluting the real traversal enough that a genuine
    ASSOCIATED_WITH relationship never made it into the final evidence).
    """
    fake_graph.add_node("PERSON-donor", "Person", canonical_name="Kashif", cnic="00000-9000058-1")
    fake_graph.add_node("PERSON-survivor", "Person", canonical_name="Kashif", cnic="00000-9000058-1")
    fake_graph.add_case("PERSON-donor", "fir-1001-26")
    fake_graph.add_case("PERSON-survivor", "fir-1001-26")
    fake_graph.add_merge("PERSON-donor", "PERSON-survivor")
    fake_chunks["c1"] = {"id": "c1", "text": "Kashif named as accused.", "metadata": {"case_id": "fir-1001-26"}}
    fake_graph.add_appears_in("PERSON-survivor", "c1", confidence=1.0)

    result = await gr.retrieve_graph("Is Kashif known to associate with anyone else?", "Kashif", "fir-1001-26")

    seeded_ids = {e["entity_id"] for e in result["seed_entities"]}
    assert seeded_ids == {"PERSON-survivor"}, "the merged donor must never come back as a seed"


async def test_case_wide_enumeration_excludes_a_merged_donor_node(fake_graph, fake_chunks):
    """Same donor exclusion for the case-wide enumeration fallback (no named entity)."""
    fake_graph.add_node("PERSON-donor", "Person", canonical_name="Kashif")
    fake_graph.add_node("PERSON-survivor", "Person", canonical_name="Kashif")
    fake_graph.add_case("PERSON-donor", "fir-1001-26")
    fake_graph.add_case("PERSON-survivor", "fir-1001-26")
    fake_graph.add_merge("PERSON-donor", "PERSON-survivor")

    result = await gr.retrieve_graph(
        "How many accused are involved in this case?", target_entity=None, case_id="fir-1001-26",
    )

    seeded_ids = {e["entity_id"] for e in result["seed_entities"]}
    assert seeded_ids == {"PERSON-survivor"}


async def test_cross_case_recurrence_does_not_double_count_a_merged_donor(fake_graph, fake_chunks):
    """
    A merged donor's leftover (superseded) case membership must not make a
    single real person look like they recur across cases when they don't,
    nor should it be silently miscounted the other way — the donor simply
    should not contribute at all; only the survivor's own (consolidated)
    case memberships count toward recurrence.
    """
    fake_graph.add_node("PH-donor", "PhoneNumber", number="0372-1590538")
    fake_graph.add_node("PH-survivor", "PhoneNumber", number="0372-1590538")
    fake_graph.add_case("PH-donor", "CASE-004")
    fake_graph.add_case("PH-survivor", "CASE-004")
    fake_graph.add_case("PH-survivor", "CASE-005")
    fake_graph.add_merge("PH-donor", "PH-survivor")

    result = await gr.retrieve_graph(
        "Has any phone number been used across multiple cases?",
        target_entity=None, case_id=None, cross_case=True, user_role="supervisor",
    )

    seeded_ids = {e["entity_id"] for e in result["seed_entities"]}
    assert "PH-donor" not in seeded_ids
    assert seeded_ids == {"PH-survivor"}


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


# ── Urdu entity-extraction trailing-particle recovery ────────────────────────
#
# Confirmed live: route_query()'s LLM extraction of "ذیشان کن کیسز سے منسلک
# ہے؟" ("which cases is Zeeshan connected to?") produced
# target_entity="ذیشان کن" instead of "ذیشان" -- the trailing "کن"
# ("which") glued onto the name. That extra word alone was enough to drop
# seed-lookup results from 13 real matches to 0, on a corpus where the
# clean name already resolves correctly (this is a separate defect from
# cross-script name matching, which was already fixed and re-verified
# working here).

def test_strip_trailing_particle_removes_a_known_urdu_particle():
    assert gr._strip_trailing_particle("ذیشان کن") == "ذیشان"


def test_strip_trailing_particle_removes_a_known_english_particle():
    assert gr._strip_trailing_particle("zeeshan which") == "zeeshan"


def test_strip_trailing_particle_leaves_a_clean_name_untouched():
    assert gr._strip_trailing_particle("ذیشان") is None
    assert gr._strip_trailing_particle("zeeshan") is None


def test_strip_trailing_particle_does_not_strip_a_bare_particle_with_nothing_before_it():
    assert gr._strip_trailing_particle("کن") is None


def test_seed_candidates_includes_both_the_raw_and_particle_stripped_entity():
    candidates = gr._seed_candidates("ذیشان کن", "ذیشان کن کیسز سے منسلک ہے؟")
    assert "ذیشان کن" in candidates
    assert "ذیشان" in candidates


async def test_dirty_urdu_extraction_still_finds_seeds_via_the_stripped_fallback(fake_graph, fake_chunks):
    """
    End-to-end reproduction of the live failure: a corrupted target_entity
    ("ذیشان کن") must still resolve to the real node a clean extraction
    ("ذیشان") would have found -- the exact bug, fixed.
    """
    fake_graph.add_node("P-ZEESHAN", "Person", canonical_name="ذیشان")
    fake_graph.add_case("P-ZEESHAN", "CASE-020")
    fake_chunks["c1"] = {"id": "c1", "text": "ذیشان کیس میں ملزم کے طور پر شامل۔", "metadata": {"case_id": "CASE-020", "source": "fir.pdf"}}
    fake_graph.add_appears_in("P-ZEESHAN", "c1", confidence=1.0)

    result = await gr.retrieve_graph("ذیشان کن کیسز سے منسلک ہے؟", "ذیشان کن", "CASE-020")

    assert result["seed_entities"][0]["entity_id"] == "P-ZEESHAN"
    assert len(result["chunks"]) == 1


# ── Matched-identifier evidence text (Bug fix) ───────────────────────────────
#
# _synthetic_evidence_chunk() used to say "X appears in record Y" with no
# mention of WHICH identifier justified retrieving X at all — a query
# specifically about a phone number got evidence that never mentioned any
# phone number, and the LLM correctly refused to confirm a claim nothing
# in front of it supported. Confirmed live on the standing 0305-4000005
# cross-case repro. These tests lock in that the matched property now
# surfaces in the generated text, and that a plain name match (the
# overwhelmingly common case) is left exactly as it was.

def test_matched_seed_property_returns_first_matching_id_prop_in_order():
    props = {"canonical_name": "طارق", "cnic": "00000-9000006-1", "phone": "0305-4000005"}
    assert gr._matched_seed_property(props, ("canonical_name", "cnic", "phone"), "0305-4000005") == (
        "phone", "0305-4000005",
    )
    assert gr._matched_seed_property(props, ("canonical_name", "cnic", "phone"), "طارق") == (
        "canonical_name", "طارق",
    )


def test_matched_seed_property_returns_none_when_nothing_matches():
    props = {"canonical_name": "طارق"}
    assert gr._matched_seed_property(props, ("canonical_name", "phone"), "0301-0000000") is None


async def test_seed_matched_via_phone_number_names_it_in_the_synthetic_chunk_text(fake_graph, fake_chunks):
    fake_graph.add_node("P-410", "Person", canonical_name="طارق", phone="0305-4000005")
    fake_graph.add_case("P-410", "CASE-A")
    fake_graph.add_appears_in(
        "P-410", None, confidence=1.0,
        source_doc_id="psrms/fir/CASE-A#structured", surface_text="طارق",
        doc_type="fir_structured", filename="CASE-A", doc_case_id="CASE-A",
    )

    result = await gr.retrieve_graph("Does phone 0305-4000005 appear in this case?", "0305-4000005", "CASE-A")

    assert len(result["chunks"]) == 1
    assert "0305-4000005" in result["chunks"][0]["text"], (
        "the matched phone number must be named in the evidence text, not just implied by the seed lookup"
    )
    assert result["chunks"][0]["text"] == (
        "طارق, whose phone number is 0305-4000005, appears in fir_structured record CASE-A "
        "(psrms/fir/CASE-A#structured)."
    )


async def test_seed_matched_via_name_gets_no_redundant_clause(fake_graph, fake_chunks):
    """A plain name-matched seed (the overwhelmingly common case) must render
    exactly as before this fix — no "whose canonical_name is X" clause,
    since the name is already the sentence's own subject."""
    fake_graph.add_node("P-411", "Person", canonical_name="Waqas Ali Niazi")
    fake_graph.add_case("P-411", "CASE-B")
    fake_graph.add_appears_in(
        "P-411", None, confidence=1.0,
        source_doc_id="psrms/fir/CASE-B#structured", surface_text="Waqas Ali Niazi",
        doc_type="fir_structured", filename="CASE-B",
    )

    result = await gr.retrieve_graph("Tell me about Waqas Ali Niazi", "Waqas Ali Niazi", "CASE-B")

    assert result["chunks"][0]["text"] == (
        "Waqas Ali Niazi appears in fir_structured record CASE-B (psrms/fir/CASE-B#structured)."
    )


async def test_matched_property_not_propagated_to_a_hop_reached_entity(fake_graph, fake_chunks):
    """An entity reached via ASSOCIATED_WITH (not seeded by a literal
    property match) must never get a fabricated "whose phone number is..."
    clause — it was found via a graph edge, not a property match."""
    fake_graph.add_node("P-412", "Person", canonical_name="طارق", phone="0305-4000005")
    fake_graph.add_node("P-413", "Person", canonical_name="Co-Accused")
    fake_graph.add_case("P-412", "CASE-C")
    fake_graph.add_case("P-413", "CASE-C")
    fake_graph.add_associated("P-412", "P-413", confidence=1.0)
    fake_graph.add_appears_in(
        "P-413", None, confidence=1.0,
        source_doc_id="psrms/fir/CASE-C#structured", surface_text="Co-Accused",
        doc_type="fir_structured", filename="CASE-C",
    )

    result = await gr.retrieve_graph("Does phone 0305-4000005 appear in this case?", "0305-4000005", "CASE-C")

    hop_chunk = next(c for c in result["chunks"] if "Co-Accused" in c["text"])
    assert hop_chunk["text"] == "Co-Accused appears in fir_structured record CASE-C (psrms/fir/CASE-C#structured)."


# ── Notable-property evidence text (Module 2, findings.md) ──────────────────
#
# _synthetic_evidence_chunk() used to mention ONLY the property that
# justified the seed match (if any) — never any OTHER property the node
# carries. Confirmed live: "What is Officer ذیشان's belt number in this
# case?" found the right officer, but the cited text never named the belt
# number anywhere, so the LLM correctly refused to confirm a fact that's
# true in the graph but absent from what it's shown. These tests lock in
# that notable properties (belt_no/cnic/phone/plate, per _NOTABLE_PROPERTIES)
# now always surface, without duplicating whichever property already
# justified the seed match.

async def test_officer_seeded_by_name_surfaces_belt_number(fake_graph, fake_chunks):
    fake_graph.add_node("OFFICER-319", "Officer", canonical_name="ذیشان", belt_no="GEN-0301")
    fake_graph.add_case("OFFICER-319", "fir-401-26")
    fake_graph.add_appears_in(
        "OFFICER-319", None, confidence=1.0,
        source_doc_id="psrms/fir/fir-401-26#structured", surface_text="ذیشان",
        doc_type="fir_structured", filename="fir-401-26",
    )

    result = await gr.retrieve_graph("What is Officer ذیشان's belt number in this case?", "ذیشان", "fir-401-26")

    assert result["chunks"][0]["text"] == (
        "ذیشان appears in fir_structured record fir-401-26 (psrms/fir/fir-401-26#structured), "
        "with belt number GEN-0301 recorded there."
    )


async def test_person_seeded_by_name_surfaces_cnic_and_phone(fake_graph, fake_chunks):
    fake_graph.add_node(
        "P-420", "Person", canonical_name="Waqas Ali Niazi",
        cnic="00000-9000006-1", phone="0305-4000005",
    )
    fake_graph.add_case("P-420", "CASE-D")
    fake_graph.add_appears_in(
        "P-420", None, confidence=1.0,
        source_doc_id="psrms/fir/CASE-D#structured", surface_text="Waqas Ali Niazi",
        doc_type="fir_structured", filename="CASE-D",
    )

    result = await gr.retrieve_graph("Tell me about Waqas Ali Niazi", "Waqas Ali Niazi", "CASE-D")

    assert result["chunks"][0]["text"] == (
        "Waqas Ali Niazi appears in fir_structured record CASE-D (psrms/fir/CASE-D#structured), "
        "with CNIC 00000-9000006-1, and phone number 0305-4000005 recorded there."
    )


async def test_node_with_no_notable_properties_beyond_match_renders_unchanged(fake_graph, fake_chunks):
    """A node whose only notable property IS the one that matched the seed
    (nothing else present) must render exactly as before this fix — no
    trailing empty/dangling clause."""
    fake_graph.add_node("P-421", "Person", canonical_name="طارق", phone="0305-4000005")
    fake_graph.add_case("P-421", "CASE-E")
    fake_graph.add_appears_in(
        "P-421", None, confidence=1.0,
        source_doc_id="psrms/fir/CASE-E#structured", surface_text="طارق",
        doc_type="fir_structured", filename="CASE-E",
    )

    result = await gr.retrieve_graph("Does phone 0305-4000005 appear in this case?", "0305-4000005", "CASE-E")

    assert result["chunks"][0]["text"] == (
        "طارق, whose phone number is 0305-4000005, appears in fir_structured record CASE-E "
        "(psrms/fir/CASE-E#structured)."
    )


async def test_officer_seeded_by_belt_number_does_not_duplicate_it(fake_graph, fake_chunks):
    """An Officer seeded BY belt_no (not name) must name the belt number
    once, in the existing match_clause — not a second time in the notable-
    properties clause."""
    fake_graph.add_node("OFFICER-320", "Officer", canonical_name="ذیشان", belt_no="GEN-0301")
    fake_graph.add_case("OFFICER-320", "CASE-F")
    fake_graph.add_appears_in(
        "OFFICER-320", None, confidence=1.0,
        source_doc_id="psrms/fir/CASE-F#structured", surface_text="ذیشان",
        doc_type="fir_structured", filename="CASE-F",
    )

    result = await gr.retrieve_graph("Does belt number GEN-0301 appear in this case?", "GEN-0301", "CASE-F")

    text = result["chunks"][0]["text"]
    assert text.count("GEN-0301") == 1, f"belt number must appear exactly once, got: {text!r}"
    assert text == (
        "ذیشان, whose belt number is GEN-0301, appears in fir_structured record CASE-F "
        "(psrms/fir/CASE-F#structured)."
    )


# ── Officer ASSIGNED_TO role evidence text (findings.md Module 8 follow-up) ──
#
# _synthetic_evidence_chunk() used to never surface an Officer's role
# (investigating/recording) at all — that lives on the ASSIGNED_TO edge,
# not a node property, so Module 2's notable-properties fix above never
# reached it. Confirmed live during Local Search's own live-verification:
# the right officer (ذیشان, this exact fir-401-26/GEN-0301 fixture) was
# found via semantic match, but the relevance evaluator rejected the cited
# text verbatim because it "does not explicitly state that this individual
# is the investigating officer."

async def test_officer_investigating_role_surfaces_in_evidence_text(fake_graph, fake_chunks):
    fake_graph.add_node("OFFICER-319", "Officer", canonical_name="ذیشان", belt_no="GEN-0301")
    fake_graph.add_case("OFFICER-319", "fir-401-26")
    fake_graph.add_appears_in(
        "OFFICER-319", None, confidence=1.0,
        source_doc_id="psrms/fir/fir-401-26#structured", surface_text="ذیشان",
        doc_type="fir_structured", filename="fir-401-26",
    )
    fake_graph.add_assigned_to("OFFICER-319", "fir-401-26", "investigating")

    result = await gr.retrieve_graph("Who is the investigating officer in this case?", "ذیشان", "fir-401-26")

    assert result["chunks"][0]["text"] == (
        "ذیشان appears in fir_structured record fir-401-26 (psrms/fir/fir-401-26#structured), "
        "with belt number GEN-0301 recorded there, and is recorded as the investigating officer "
        "for this case."
    )


async def test_officer_recording_role_surfaces_as_recording_not_investigating(fake_graph, fake_chunks):
    fake_graph.add_node("OFFICER-321", "Officer", canonical_name="ندیم", belt_no="GEN-0113")
    fake_graph.add_case("OFFICER-321", "CASE-G")
    fake_graph.add_appears_in(
        "OFFICER-321", None, confidence=1.0,
        source_doc_id="psrms/fir/CASE-G#structured", surface_text="ندیم",
        doc_type="fir_structured", filename="CASE-G",
    )
    fake_graph.add_assigned_to("OFFICER-321", "CASE-G", "recording")

    result = await gr.retrieve_graph("Who recorded this FIR?", "ندیم", "CASE-G")

    assert "recorded as the recording officer for this case" in result["chunks"][0]["text"]
    assert "investigating" not in result["chunks"][0]["text"]


async def test_officer_role_scoped_to_the_specific_case_not_leaked_from_another(fake_graph, fake_chunks):
    """The same officer can be investigating on one case and have no role
    (or a different one) on another — the role clause must only ever name
    the role recorded for THIS row's own case, never one borrowed from a
    different case the officer also happens to be assigned to."""
    fake_graph.add_node("OFFICER-322", "Officer", canonical_name="عمران", belt_no="GEN-0109")
    fake_graph.add_case("OFFICER-322", "CASE-H")
    fake_graph.add_appears_in(
        "OFFICER-322", None, confidence=1.0,
        source_doc_id="psrms/fir/CASE-H#structured", surface_text="عمران",
        doc_type="fir_structured", filename="CASE-H", doc_case_id="CASE-H",
    )
    # Role recorded on a DIFFERENT case only -- must not leak into CASE-H's evidence text.
    fake_graph.add_assigned_to("OFFICER-322", "CASE-OTHER", "investigating")

    result = await gr.retrieve_graph("Who is the investigating officer?", "عمران", "CASE-H")

    text = result["chunks"][0]["text"]
    assert "investigating officer" not in text
    assert text == (
        "عمران appears in fir_structured record CASE-H (psrms/fir/CASE-H#structured), "
        "with belt number GEN-0109 recorded there."
    )


async def test_superseded_assigned_to_role_not_surfaced(fake_graph, fake_chunks):
    """A superseded ASSIGNED_TO edge (an officer reassignment,
    structured_projection.py's supersession-chain writes) must never
    surface as the CURRENT role -- only the one live, non-superseded edge
    is the actual current assignment."""
    fake_graph.add_node("OFFICER-323", "Officer", canonical_name="فیصل", belt_no="1854L")
    fake_graph.add_case("OFFICER-323", "CASE-I")
    fake_graph.add_appears_in(
        "OFFICER-323", None, confidence=1.0,
        source_doc_id="psrms/fir/CASE-I#structured", surface_text="فیصل",
        doc_type="fir_structured", filename="CASE-I",
    )
    fake_graph.add_assigned_to("OFFICER-323", "CASE-I", "investigating", superseded_by=99)

    result = await gr.retrieve_graph("Who is the investigating officer?", "فیصل", "CASE-I")

    assert "investigating officer" not in result["chunks"][0]["text"]


async def test_officer_holding_both_roles_on_same_case_names_both_deterministically(fake_graph, fake_chunks):
    """The exact live-confirmed shape (fir-401-26's real data): one
    officer holds BOTH the investigating and recording role on the same
    case simultaneously (two live, non-superseded ASSIGNED_TO edges). This
    must deterministically name BOTH roles every time — the bug this test
    guards against silently named only one, in whichever order the
    two-edge OPTIONAL MATCH fan-out happened to return them that call."""
    fake_graph.add_node("OFFICER-319", "Officer", canonical_name="ذیشان", belt_no="GEN-0301")
    fake_graph.add_case("OFFICER-319", "fir-401-26")
    fake_graph.add_appears_in(
        "OFFICER-319", None, confidence=1.0,
        source_doc_id="psrms/fir/fir-401-26#structured", surface_text="ذیشان",
        doc_type="fir_structured", filename="fir-401-26",
    )
    fake_graph.add_assigned_to("OFFICER-319", "fir-401-26", "recording")
    fake_graph.add_assigned_to("OFFICER-319", "fir-401-26", "investigating")

    # Run it several times -- a non-deterministic (dict-overwrite-order-
    # dependent) implementation would flip between runs; this must not.
    for _ in range(5):
        result = await gr.retrieve_graph("Who is the investigating officer?", "ذیشان", "fir-401-26")
        assert len(result["chunks"]) == 1
        assert result["chunks"][0]["text"] == (
            "ذیشان appears in fir_structured record fir-401-26 (psrms/fir/fir-401-26#structured), "
            "with belt number GEN-0301 recorded there, and is recorded as both the investigating and "
            "recording officer for this case."
        )


async def test_person_never_gets_a_role_clause(fake_graph, fake_chunks):
    """No ASSIGNED_TO edge is ever written for a non-Officer entity -- the
    role clause must be structurally absent, not merely coincidentally
    empty, for every other label."""
    fake_graph.add_node("P-424", "Person", canonical_name="بلال")
    fake_graph.add_case("P-424", "CASE-J")
    fake_graph.add_appears_in(
        "P-424", None, confidence=1.0,
        source_doc_id="psrms/fir/CASE-J#structured", surface_text="بلال",
        doc_type="fir_structured", filename="CASE-J",
    )

    result = await gr.retrieve_graph("Tell me about بلال", "بلال", "CASE-J")

    assert result["chunks"][0]["text"] == (
        "بلال appears in fir_structured record CASE-J (psrms/fir/CASE-J#structured)."
    )


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


async def test_associated_with_hop_generates_an_explicit_relationship_chunk(fake_graph, fake_chunks):
    """
    [Bug fix, 2026-08-27 route sweep, BUG-4] Before this fix, a real
    one-hop ASSOCIATED_WITH connection produced chunks about EACH entity
    separately ("X appears in record...", "Y appears in record...") but
    never a sentence stating they're connected — live-confirmed to make
    the evaluator correctly (!) judge the evidence insufficient to answer
    "is X associated with anyone?", because nothing in the retrieved text
    actually said so. This asserts the missing sentence now exists.
    """
    fake_graph.add_node("P-010", "Person", canonical_name="Kashif")
    fake_graph.add_node("P-011", "Person", canonical_name="Faisal")
    fake_graph.add_case("P-010", "fir-1001-26")
    fake_graph.add_case("P-011", "fir-1001-26")
    fake_graph.add_associated(
        "P-010", "P-011", confidence=0.85,
        source_doc_id="psrms/fir/fir-1001-26#structured",
        basis="co-mentioned in case fir-1001-26's incident",
    )
    fake_chunks["c2"] = {"id": "c2", "text": "Faisal named in the case diary.", "metadata": {"case_id": "fir-1001-26"}}
    fake_graph.add_appears_in("P-011", "c2", confidence=1.0)

    result = await gr.retrieve_graph("Is Kashif known to associate with anyone else?", "Kashif", "fir-1001-26")

    rel_chunks = [c for c in result["chunks"] if c["metadata"].get("doc_type") == "graph_relationship"]
    assert len(rel_chunks) == 1
    assert rel_chunks[0]["text"] == (
        "Kashif is associated with Faisal (co-mentioned in case fir-1001-26's incident)."
    )
    assert rel_chunks[0]["graph_confidence"] == pytest.approx(0.85)
    assert rel_chunks[0]["metadata"]["case_id"] == "fir-1001-26"


async def test_reversed_hop_does_not_duplicate_the_relationship_chunk(fake_graph, fake_chunks):
    """A later hop walking the SAME edge backward (once the far side is
    itself in the frontier) must not re-emit the identical fact as a
    second, reverse-worded chunk."""
    fake_graph.add_node("P-020", "Person", canonical_name="A")
    fake_graph.add_node("P-021", "Person", canonical_name="B")
    fake_graph.add_node("P-022", "Person", canonical_name="C")
    for eid in ("P-020", "P-021", "P-022"):
        fake_graph.add_case(eid, "CASE-100")
    fake_graph.add_associated("P-020", "P-021", source_doc_id="doc1", basis="co-mentioned")
    fake_graph.add_associated("P-021", "P-022", source_doc_id="doc2", basis="co-mentioned")
    fake_chunks["c3"] = {"id": "c3", "text": "C named.", "metadata": {"case_id": "CASE-100"}}
    fake_graph.add_appears_in("P-022", "c3", confidence=1.0)

    result = await gr.retrieve_graph("Who is A connected to?", "A", "CASE-100", max_hops=3)

    rel_chunks = [c for c in result["chunks"] if c["metadata"].get("doc_type") == "graph_relationship"]
    # Exactly one chunk per real edge (A<->B, B<->C) — never A<->B AND its
    # reverse B<->A both appearing once B itself becomes part of frontier.
    assert len(rel_chunks) == 2
    assert {c["id"] for c in rel_chunks} == {
        "synthetic-rel:A:B:doc1", "synthetic-rel:B:C:doc2",
    }


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


async def test_confirmed_same_as_never_leaks_another_case_within_case_traversal(fake_graph, fake_chunks):
    """
    Milestone E2 — the real gap found in the eval-only/production-chokepoint
    audit: a WITHIN-case query (cross_case=False, the default) must never
    surface another case's chunks just because the seed entity has a
    CONFIRMED SAME_AS link to a node that also belongs to a different case.
    Before this fix, _expand_confirmed_identity()'s fold ran unconditionally
    (no cross_case gate, no _filter_to_case) and added the other case's
    node straight into `visited` — this is the identical fixture shape as
    test_confirmed_same_as_is_followed_as_identity_cross_case above, run
    with cross_case=False (no supervisor role, no explicit cross-case ask)
    to prove the leak is closed, not just that the cross-case path works.
    """
    fake_graph.add_node("P-002", "Person", canonical_name="Waqas Ali Niazi")
    fake_graph.add_node("P-002-dup", "Person", canonical_name="Waqas A. Niazi")
    fake_graph.add_case("P-002", "CASE-007")
    fake_graph.add_case("P-002-dup", "CASE-009")
    fake_graph.add_same_as("P-002", "P-002-dup", status="confirmed", tier="cnic_auto", confidence=0.99)
    fake_chunks["c5"] = {"id": "c5", "text": "Waqas mentioned in CASE-009.", "metadata": {"case_id": "CASE-009"}}
    fake_graph.add_appears_in("P-002-dup", "c5", confidence=1.0)
    fake_chunks["c5b"] = {"id": "c5b", "text": "Waqas mentioned in CASE-007.", "metadata": {"case_id": "CASE-007"}}
    fake_graph.add_appears_in("P-002", "c5b", confidence=1.0)

    result = await gr.retrieve_graph(
        "tell me about Waqas Ali Niazi", "Waqas Ali Niazi", case_id="CASE-007", cross_case=False,
    )

    chunk_ids = {c["id"] for c in result["chunks"]}
    assert "c5" not in chunk_ids, "a confirmed SAME_AS must not leak another case's chunks into a within-case query"
    assert "c5b" in chunk_ids, "the seed entity's own case chunk must still be returned"


async def test_single_entity_belonging_to_two_cases_never_leaks_the_other_cases_evidence(fake_graph, fake_chunks):
    """
    [Module 7 follow-up, findings.md] A DIFFERENT leak shape from the
    SAME_AS-fold test above: a single canonical entity_id (identity
    resolution has ALREADY merged it — no SAME_AS edge involved at all)
    with TWO real BELONGS_TO_CASE edges, because the same real person
    genuinely recurs across two cases. Live-confirmed against the real
    corpus: with no literal name/CNIC/phone in the query (target_entity
    stays null for a descriptive reference like "the accused" — the
    common shape for a compound question), retrieve_graph() seeds via
    _find_all_case_entities(case_id), which correctly includes this
    entity (it DOES belong to the active case) — but _fetch_appears_in()
    is case-agnostic and, before this fix, returned ALL of that entity's
    evidence unfiltered, including its OTHER case's document. Caught only
    downstream by verify_grounding()'s leakage check (rejecting the whole
    answer), never by retrieve_graph() itself — contradicting this
    module's own docstring ("ignored entirely on the within-case path,
    which is always scoped to case_id regardless").
    """
    fake_graph.add_node("P-777", "Person", canonical_name="Shahzaib")
    fake_graph.add_case("P-777", "CASE-214")
    fake_graph.add_case("P-777", "CASE-891")  # the SAME node, genuinely in both cases
    fake_chunks["c-214"] = {"id": "c-214", "text": "Shahzaib named in CASE-214.", "metadata": {"case_id": "CASE-214"}}
    fake_graph.add_appears_in("P-777", "c-214", confidence=1.0, doc_case_id="CASE-214")
    fake_chunks["c-891"] = {"id": "c-891", "text": "Shahzaib named in CASE-891.", "metadata": {"case_id": "CASE-891"}}
    fake_graph.add_appears_in("P-777", "c-891", confidence=1.0, doc_case_id="CASE-891")

    result = await gr.retrieve_graph(
        "who is the investigating officer, and has the accused been in any other case?",
        None, case_id="CASE-214", cross_case=False,
    )

    chunk_ids = {c["id"] for c in result["chunks"]}
    assert "c-891" not in chunk_ids, "the other case's evidence for a shared entity must never leak into a within-case query"
    assert "c-214" in chunk_ids, "the seed entity's own-case evidence must still be returned"


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


async def test_pending_same_as_stays_excluded_when_flag_off(fake_graph, fake_chunks, monkeypatch):
    """Milestone D2: config.FEATURE_HEDGED_PENDING_TRAVERSAL defaults False — behavior must be byte-for-byte the prior (D1-era) exclusion, same fixture as the flagship P-006 test above."""
    monkeypatch.setattr(gr.config, "FEATURE_HEDGED_PENDING_TRAVERSAL", False)
    fake_graph.add_node("P-D2A", "Person", canonical_name="Adnan Qureshi Waheed")
    fake_graph.add_node("P-D2A-case016", "Person", canonical_name="Adnan Qureshi")
    fake_graph.add_case("P-D2A", "CASE-015")
    fake_graph.add_case("P-D2A-case016", "CASE-016")
    fake_graph.add_same_as("P-D2A", "P-D2A-case016", status="pending", tier="flagged_unverified", confidence=0.55)
    fake_chunks["c6"] = {"id": "c6", "text": "Adnan mentioned in CASE-016.", "metadata": {"case_id": "CASE-016"}}
    fake_graph.add_appears_in("P-D2A-case016", "c6", confidence=1.0)

    result = await gr.retrieve_graph(
        "is this repeat fraud offender linked elsewhere", "Adnan Qureshi Waheed", case_id=None, cross_case=True,
        user_role="supervisor",
    )
    assert "c6" not in {c["id"] for c in result["chunks"]}


async def test_pending_same_as_traversed_and_hedged_when_flag_on(fake_graph, fake_chunks, monkeypatch):
    """Milestone D2, flag ON: pending identity IS traversed (recall preserved) but every chunk reached only through it carries a disclosed-hedge tag and a confidence capped below the verifier's 0.85 threshold — never silently downweighted, never presented as confirmed."""
    monkeypatch.setattr(gr.config, "FEATURE_HEDGED_PENDING_TRAVERSAL", True)
    fake_graph.add_node("P-D2B", "Person", canonical_name="Adnan Qureshi Waheed")
    fake_graph.add_node("P-D2B-case016", "Person", canonical_name="Adnan Qureshi")
    fake_graph.add_case("P-D2B", "CASE-015")
    fake_graph.add_case("P-D2B-case016", "CASE-016")
    fake_graph.add_same_as(
        "P-D2B", "P-D2B-case016", status="pending", tier="flagged_unverified", confidence=0.98,
    )
    fake_chunks["c6"] = {"id": "c6", "text": "Adnan mentioned in CASE-016.", "metadata": {"case_id": "CASE-016"}}
    fake_graph.add_appears_in("P-D2B-case016", "c6", confidence=1.0)

    result = await gr.retrieve_graph(
        "is this repeat fraud offender linked elsewhere", "Adnan Qureshi Waheed", case_id=None, cross_case=True,
        user_role="supervisor",
    )

    chunks_by_id = {c["id"]: c for c in result["chunks"]}
    assert "c6" in chunks_by_id, "flag ON must open the pending-identity traversal path (recall preserved)"
    chunk = chunks_by_id["c6"]
    assert chunk["graph_confidence"] < 0.85, "must be capped below the verifier's hedge threshold even though the pending edge's own confidence (0.98) was high"
    assert chunk["metadata"]["same_as_status"] == "pending"
    assert chunk["metadata"]["same_as_basis"]
    # Status itself never changes — this is retrieval-time surfacing, not
    # a decision. The underlying pending edge is untouched.
    assert fake_graph.same_as[0][2]["status"] == "pending"


async def test_pending_traversal_flag_has_no_effect_within_case(fake_graph, fake_chunks, monkeypatch):
    """D2 explicitly scopes the opened traversal to cross-case (XGRAPH) only — the flag must be inert for a within-case query."""
    monkeypatch.setattr(gr.config, "FEATURE_HEDGED_PENDING_TRAVERSAL", True)
    fake_graph.add_node("P-D2C", "Person", canonical_name="Zubair Anjum")
    fake_graph.add_node("P-D2C-other", "Person", canonical_name="Kamran Farooq")
    fake_graph.add_case("P-D2C", "CASE-020")
    fake_graph.add_case("P-D2C-other", "CASE-020")
    fake_graph.add_same_as("P-D2C", "P-D2C-other", status="pending", tier="human_review", confidence=0.6)
    fake_chunks["c7"] = {"id": "c7", "text": "text", "metadata": {"case_id": "CASE-020"}}
    fake_graph.add_appears_in("P-D2C-other", "c7", confidence=1.0)

    result = await gr.retrieve_graph(
        "who is Zubair Anjum", "Zubair Anjum", case_id="CASE-020", cross_case=False,
    )
    assert "c7" not in {c["id"] for c in result["chunks"]}


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


# ── Synthetic-evidence-chunk bug fix (structured-extraction writes never
# carry a source_chunk_id — see graph_retriever.py's own
# _synthetic_evidence_chunk() docstring for the full story) ─────────────────

async def test_structured_extraction_mention_surfaces_as_a_synthetic_chunk(fake_graph, fake_chunks):
    """
    No source_chunk_id (structured-extraction shape), but a real
    source_doc_id — must surface as a synthetic evidence chunk instead of
    being dropped like the genuinely-missing-document case above.
    """
    fake_graph.add_node("P-400", "Person", canonical_name="Shahzaib alias Shabi")
    fake_graph.add_case("P-400", "CASE-400")
    fake_graph.add_appears_in(
        "P-400", None, confidence=1.0,
        source_doc_id="criminal_db/criminal_record/CR-1#structured",
        surface_text="Shahzaib alias Shabi", doc_type="criminal_record_structured", filename="CR-1",
    )

    result = await gr.retrieve_graph("Shahzaib alias Shabi", "Shahzaib alias Shabi", "CASE-400")

    assert len(result["chunks"]) == 1
    chunk = result["chunks"][0]
    assert chunk["id"] == "synthetic:P-400:criminal_db/criminal_record/CR-1#structured"
    assert chunk["text"] == (
        "Shahzaib alias Shabi appears in criminal_record_structured record "
        "CR-1 (criminal_db/criminal_record/CR-1#structured)."
    )
    assert chunk["metadata"]["synthetic_evidence"] is True
    assert chunk["metadata"]["source"] == "criminal_db/criminal_record/CR-1#structured"
    assert chunk["metadata"]["case_id"] == "CASE-400", (
        "real case_id from this document's own BELONGS_TO_CASE, not guessed from the doc_id string"
    )


async def test_synthetic_chunk_case_id_is_per_document_not_per_entity(fake_graph, fake_chunks):
    """
    [Bug fix] An entity genuinely recurring across two cases (e.g. a
    shared phone number resolved to the same Person) must have EACH of
    its synthetic evidence chunks stamped with the case its OWN document
    belongs to — never a case borrowed from the entity's other
    appearance. Confirmed live: before this fix, _fetch_appears_in()
    joined BELONGS_TO_CASE off the entity (which has one edge per case it
    recurs in), fanning out into duplicate rows that let one document's
    citation metadata get silently overwritten with the other case's id.
    """
    fake_graph.add_node("P-410", "Person", canonical_name="Recurring Person", phone="0305-4000005")
    fake_graph.add_case("P-410", "CASE-A")
    fake_graph.add_case("P-410", "CASE-B")
    fake_graph.add_appears_in(
        "P-410", None, confidence=1.0,
        source_doc_id="psrms/fir/CASE-A#structured", surface_text="Recurring Person",
        doc_type="fir_structured", filename="CASE-A", doc_case_id="CASE-A",
    )
    fake_graph.add_appears_in(
        "P-410", None, confidence=1.0,
        source_doc_id="psrms/fir/CASE-B#structured", surface_text="Recurring Person",
        doc_type="fir_structured", filename="CASE-B", doc_case_id="CASE-B",
    )

    result = await gr.retrieve_graph(
        "Has phone 0305-4000005 appeared in any other cases?", "0305-4000005",
        case_id="CASE-A", cross_case=True, user_id="u1", user_role="platform-admin",
    )

    by_source = {c["metadata"]["source"]: c["metadata"]["case_id"] for c in result["chunks"]}
    assert by_source["psrms/fir/CASE-A#structured"] == "CASE-A"
    assert by_source["psrms/fir/CASE-B#structured"] == "CASE-B"


async def test_synthetic_chunk_omits_case_id_for_a_genuinely_case_less_entity(fake_graph, fake_chunks):
    """A criminal_db-only entity with no BELONGS_TO_CASE edge at all
    (that silo is CNIC-cross-referenced, never case-anchored) must not
    have a case_id asserted onto it that doesn't exist."""
    fake_graph.add_node("P-403", "Person", canonical_name="Case-less Person")
    # Deliberately no fake_graph.add_case(...) call.
    fake_graph.add_appears_in(
        "P-403", None, confidence=1.0,
        source_doc_id="criminal_db/criminal_record/CR-2#structured",
        surface_text="Case-less Person", doc_type="criminal_record_structured", filename="CR-2",
    )

    result = await gr.retrieve_graph(
        "Case-less Person", "Case-less Person", case_id=None, cross_case=True,
        user_id="u1", user_role="platform-admin",
    )

    assert len(result["chunks"]) == 1
    assert "case_id" not in result["chunks"][0]["metadata"]


async def test_real_chunk_path_is_unaffected_by_the_synthetic_fallback(fake_graph, fake_chunks):
    """A normal, already-working narrative-chunk hop must stay bit-for-bit unchanged."""
    fake_graph.add_node("P-401", "Person", canonical_name="Real Chunk Person")
    fake_graph.add_case("P-401", "CASE-401")
    fake_chunks["c-real"] = {"id": "c-real", "text": "Real narrative text.", "metadata": {"case_id": "CASE-401"}}
    fake_graph.add_appears_in("P-401", "c-real", confidence=1.0)

    result = await gr.retrieve_graph("Real Chunk Person", "Real Chunk Person", "CASE-401")

    assert len(result["chunks"]) == 1
    chunk = result["chunks"][0]
    assert chunk["id"] == "c-real"
    assert chunk["text"] == "Real narrative text."
    assert "synthetic_evidence" not in chunk["metadata"]


async def test_no_chunk_id_and_no_doc_id_still_drops_the_hop(fake_graph, fake_chunks):
    """The true 'nothing at all to cite' case (shouldn't happen for real
    APPEARS_IN edges, but must degrade safely, not crash or fabricate)."""
    fake_graph.add_node("P-402", "Person", canonical_name="Truly Bare Mention")
    fake_graph.add_case("P-402", "CASE-402")
    fake_graph.add_appears_in("P-402", None, confidence=1.0)  # no source_doc_id either

    result = await gr.retrieve_graph("Truly Bare Mention", "Truly Bare Mention", "CASE-402")

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


def test_seed_labels_person_matches_a_real_phone_property():
    """
    Third occurrence of the same bug class: src/graph/
    structured_projection.py's person/officer mention writes put a phone
    number directly on the Person node's own `phone` property (confirmed
    live: 98 real Person nodes carry one) — Person's id_props never
    included it, so a phone-number-anchored query could never find a
    seed for virtually the entire real corpus.
    """
    id_props, _display = gr._SEED_LABELS["Person"]
    assert "phone" in id_props


def test_seed_labels_officer_exists_and_matches_what_is_actually_written():
    """
    Officer was missing from _SEED_LABELS entirely, despite being a
    real, actively-written graph label (TYPE_TO_LABEL["officer"],
    Milestone B2) — confirmed live every real FIR writes real Officer
    nodes with canonical_name/belt_no/phone/designation properties.
    """
    assert "Officer" in gr._SEED_LABELS
    id_props, _display = gr._SEED_LABELS["Officer"]
    assert "canonical_name" in id_props
    assert "belt_no" in id_props
    assert "phone" in id_props


async def test_person_is_seeded_by_phone_number_alone(fake_graph, fake_chunks):
    """End-to-end: a phone number with no matching name/CNIC text must
    still find the real Person node that carries it as a property."""
    fake_graph.add_node("P-500", "Person", canonical_name="Kashif", phone="0305-4000005")
    fake_graph.add_case("P-500", "CASE-500")
    fake_chunks["c-500"] = {"id": "c-500", "text": "Kashif mention.", "metadata": {"case_id": "CASE-500"}}
    fake_graph.add_appears_in("P-500", "c-500", confidence=1.0)

    result = await gr.retrieve_graph("has 0305-4000005 appeared elsewhere", "0305-4000005", "CASE-500")

    assert len(result["seed_entities"]) == 1
    assert result["seed_entities"][0]["entity_id"] == "P-500"


async def test_officer_is_seeded_by_belt_number(fake_graph, fake_chunks):
    """End-to-end: an Officer node, previously unreachable as a seed by
    any property at all, must now be found via its belt number."""
    fake_graph.add_node("OFF-500", "Officer", canonical_name="Muhammad Awais", belt_no="2214L")
    fake_graph.add_case("OFF-500", "CASE-501")
    fake_chunks["c-501"] = {"id": "c-501", "text": "Officer mention.", "metadata": {"case_id": "CASE-501"}}
    fake_graph.add_appears_in("OFF-500", "c-501", confidence=1.0)

    result = await gr.retrieve_graph("what is 2214L's role in this case", "2214L", "CASE-501")

    assert len(result["seed_entities"]) == 1
    assert result["seed_entities"][0]["entity_id"] == "OFF-500"


# ── Milestone B1: jurisdiction-scoped traversal reuses the SAME cross-case
# role gate as retrieve_graph(cross_case=True) — GRAPH_SCALE_SCHEMA_
# EXPANSION_PLAN.md's explicit access-control requirement, and
# SUBAGENT_INTERFACES.md's "do not add a third gate" warning. ──────────────

class _FakeGatewayForGate:
    """Minimal stand-in — only what _enforce_cross_case_role_gate() calls."""

    def __init__(self):
        self.audit_log: list[dict] = []

    async def log_audit_event(self, event_type, user_id=None, case_id=None, details=None):
        self.audit_log.append({
            "event_type": event_type, "user_id": user_id, "case_id": case_id, "details": details,
        })


@pytest.fixture
def fake_gate_gateway(monkeypatch):
    gateway = _FakeGatewayForGate()

    async def _get_gateway():
        return gateway

    monkeypatch.setattr(gr, "get_gateway", _get_gateway)
    return gateway


class TestJurisdictionScopedTraversalReusesTheGate:
    async def test_investigator_denied_same_as_cross_case_link_hop(self, fake_gate_gateway):
        """
        Not a second, looser gate: an investigator denied a single
        cross-case link hop (retrieve_graph) must be denied
        station/district-scoped enumeration exactly the same way.
        """
        with pytest.raises(PermissionError):
            await gr.retrieve_jurisdiction_cases(
                station_id="PS-1", user_id="u1", user_role="investigator",
            )
        assert len(fake_gate_gateway.audit_log) == 1
        assert fake_gate_gateway.audit_log[0]["event_type"] == "authorization_violation"
        assert fake_gate_gateway.audit_log[0]["details"]["role"] == "investigator"

    async def test_denial_writes_the_identical_audit_event_type_as_retrieve_graph(
        self, fake_graph, fake_chunks, fake_gate_gateway,
    ):
        """
        Traces both call sites to ONE function, not by assertion alone:
        the same fake gateway captures both denials, and both produce the
        literal same event_type string — if a future change forked the two
        gates, this would drift and fail.
        """
        with pytest.raises(PermissionError):
            await gr.retrieve_graph(
                "cross-case query", "X", case_id=None, cross_case=True, user_role="investigator",
            )
        with pytest.raises(PermissionError):
            await gr.retrieve_jurisdiction_cases(district_id="D-1", user_role="investigator")

        event_types = [entry["event_type"] for entry in fake_gate_gateway.audit_log]
        assert event_types == ["authorization_violation", "authorization_violation"]

    @pytest.mark.parametrize("role", ["supervisor", "station-admin", "platform-admin"])
    async def test_authorized_roles_get_case_ids_for_a_station(self, monkeypatch, fake_gate_gateway, role):
        async def fake_execute_cypher(cypher, params=None, columns=None, graph=None):
            assert "FILED_AT" in cypher
            assert params["station_id"] == "PS-1"
            return [{"case_id": "fir-1-26"}, {"case_id": "fir-2-26"}]

        monkeypatch.setattr(gr.age_client, "execute_cypher", fake_execute_cypher)

        result = await gr.retrieve_jurisdiction_cases(station_id="PS-1", user_role=role)

        assert result["case_ids"] == ["fir-1-26", "fir-2-26"]
        assert result["station_id"] == "PS-1"
        assert fake_gate_gateway.audit_log[0]["event_type"] == "graph_traversal_cross_case"

    async def test_district_only_query_shape(self, monkeypatch, fake_gate_gateway):
        async def fake_execute_cypher(cypher, params=None, columns=None, graph=None):
            assert "PART_OF" in cypher
            assert params["district_id"] == "DIST-06"
            return [{"case_id": "fir-9-26"}]

        monkeypatch.setattr(gr.age_client, "execute_cypher", fake_execute_cypher)

        result = await gr.retrieve_jurisdiction_cases(district_id="DIST-06", user_role="supervisor")
        assert result["case_ids"] == ["fir-9-26"]

    async def test_neither_station_nor_district_is_a_caller_bug(self, fake_gate_gateway):
        with pytest.raises(ValueError):
            await gr.retrieve_jurisdiction_cases(user_role="supervisor")


# ── Milestone E1: resolve_jurisdiction_case_ids() ────────────────────────────

class TestResolveJurisdictionCaseIds:
    """
    orchestrator.py's own entry point for E1's query-scope preclassification
    — turns router.py's free-text station/district into the case_id
    allow-list retrieve_graph()/run_aggregate()/run_network_query() narrow
    to, reusing retrieve_jurisdiction_cases() (and therefore the SAME
    _enforce_cross_case_role_gate(), not a second gate).
    """

    async def test_neither_station_nor_district_returns_none_without_any_lookup(self, monkeypatch, fake_gate_gateway):
        async def fake_execute_cypher(*a, **k):
            raise AssertionError("must not run any Cypher when nothing was classified")
        monkeypatch.setattr(gr.age_client, "execute_cypher", fake_execute_cypher)

        result = await gr.resolve_jurisdiction_case_ids(station=None, district=None, user_role="supervisor")
        assert result is None

    async def test_station_name_resolves_to_case_ids(self, monkeypatch, fake_gate_gateway):
        async def fake_execute_cypher(cypher, params=None, columns=None, graph=None):
            if "PoliceStation" in cypher and "FILED_AT" not in cypher:
                # [findings.md JURISDICTION] The resolver now normalizes
                # (NFKC + trim + casefold) before querying, so the bound
                # parameter is the normalized form. The resolved
                # station_id asserted below is unchanged.
                assert params["q"] == "iqbal town"
                return [{"id": "PS-LHR-IQBALTOWN"}]
            assert "FILED_AT" in cypher
            assert params["station_id"] == "PS-LHR-IQBALTOWN"
            return [{"case_id": "fir-1-26"}, {"case_id": "fir-2-26"}]

        monkeypatch.setattr(gr.age_client, "execute_cypher", fake_execute_cypher)

        result = await gr.resolve_jurisdiction_case_ids(
            station="Iqbal Town", district=None, user_role="supervisor",
        )
        assert result == ["fir-1-26", "fir-2-26"]

    async def test_unresolvable_station_text_narrows_to_none_not_empty(self, monkeypatch, fake_gate_gateway):
        """
        A station name that matches no real PoliceStation node must not
        silently zero out the query's whole candidate set — None means
        "don't narrow," which is the safe degrade here, not [] ("narrow
        to nothing").
        """
        async def fake_execute_cypher(cypher, params=None, columns=None, graph=None):
            return []  # no PoliceStation match
        monkeypatch.setattr(gr.age_client, "execute_cypher", fake_execute_cypher)

        result = await gr.resolve_jurisdiction_case_ids(
            station="Nonexistent Station", district=None, user_role="supervisor",
        )
        assert result is None

    async def test_unauthorized_role_denied_same_as_retrieve_jurisdiction_cases(self, monkeypatch, fake_gate_gateway):
        async def fake_execute_cypher(cypher, params=None, columns=None, graph=None):
            # Station resolves to a real node — the denial must come from
            # the role gate below, not from an early "nothing resolved" exit.
            return [{"id": "PS-LHR-IQBALTOWN"}]
        monkeypatch.setattr(gr.age_client, "execute_cypher", fake_execute_cypher)

        with pytest.raises(PermissionError):
            await gr.resolve_jurisdiction_case_ids(
                station="Iqbal Town", district=None, user_role="investigator",
            )
        assert fake_gate_gateway.audit_log[0]["event_type"] == "authorization_violation"


class TestJurisdictionNarrowsCrossCaseSeedLookup:
    """retrieve_graph(cross_case=True, jurisdiction_case_ids=[...]) must actually cut the candidate set, not just accept the parameter."""

    async def test_cross_case_seed_lookup_excludes_entities_outside_the_jurisdiction(self, fake_graph, fake_chunks):
        fake_graph.add_node("P-100", "Person", canonical_name="In Jurisdiction")
        fake_graph.add_node("P-101", "Person", canonical_name="In Jurisdiction Too")
        fake_graph.add_case("P-100", "CASE-100")
        fake_graph.add_case("P-101", "CASE-101")
        fake_chunks["c100"] = {"id": "c100", "text": "in jurisdiction", "metadata": {"case_id": "CASE-100"}}
        fake_graph.add_appears_in("P-100", "c100", confidence=1.0)
        fake_chunks["c101"] = {"id": "c101", "text": "out of jurisdiction", "metadata": {"case_id": "CASE-101"}}
        fake_graph.add_appears_in("P-101", "c101", confidence=1.0)

        result = await gr.retrieve_graph(
            "In Jurisdiction", "In Jurisdiction", case_id=None, cross_case=True,
            user_role="supervisor", jurisdiction_case_ids=["CASE-100"],
        )

        seed_ids = {e["entity_id"] for e in result["seed_entities"]}
        assert seed_ids == {"P-100"}
        chunk_ids = {c["id"] for c in result["chunks"]}
        assert "c101" not in chunk_ids


# ═══════════════════════════════════════════════════════════════════════
# [findings.md JURISDICTION] Bilingual jurisdiction alias resolution.
#
# District.name is Urdu-only and district_id is opaque ("DIST-04"), so an
# English district name could never match by string comparison alone —
# measured live before the fix: "Lahore" -> None, "لاہور" -> DIST-04.
#
# Fixtures mirror the nine real District nodes and the real station ids
# this corpus stores, not synthetic DISTRICT_A placeholders.
# ═══════════════════════════════════════════════════════════════════════

_REAL_DISTRICTS = {
    "DIST-01": "فیصل آباد",
    "DIST-02": "کراچی وسطی",
    "DIST-03": "حیدر آباد",
    "DIST-04": "لاہور",
    "DIST-05": "چنیوٹ",
    "DIST-06": "اسلام آباد",
    "DIST-07": "راولپنڈی",
    "DIST-08": "کراچی ایسٹ",
    "DIST-09": "ملتان",
}

_REAL_STATIONS = {
    "PS-LHR-MODELTOWN": "تھانہ ماڈل ٹاؤن، لاہور",
    "PS-KHI-NEWKARACHI": "تھانہ نیو کراچی، کراچی",
    "PS-CHN-SADDAR": "تھانہ صدر، ضلع چنیوٹ",
    "PS-RWP-SADDAR": "تھانہ صدر، راولپنڈی",
}


def _stub_jurisdiction_graph(monkeypatch):
    """Serve the real district/station vocabulary through age_client."""
    async def fake_execute_cypher(cypher, params=None, columns=None, graph=None):
        # `q` is whatever the resolver passed: the fixed code casefolds it
        # first, the pre-fix code passed it raw. Lowercasing here mirrors
        # Cypher's own toLower() on the STORED side, so this stub behaves
        # the same way for both — which is what makes the mutation check
        # meaningful rather than an artifact of the fixture.
        q = ((params or {}).get("q") or "").lower()
        if "District" in cypher:
            for did, name in _REAL_DISTRICTS.items():
                if "CONTAINS" in cypher:
                    if q and (q in did.lower() or q in name.lower()):
                        return [{"id": did}]
                elif "d.name = $q" in cypher:
                    if q == name.lower():
                        return [{"id": did}]
                elif q in (did.lower(), name.lower()):
                    return [{"id": did}]
            return []
        for sid, name in _REAL_STATIONS.items():
            if "CONTAINS" in cypher:
                if q and (q in sid.lower() or q in name.lower()):
                    return [{"id": sid}]
            elif q in (sid.lower(), name.lower()):
                return [{"id": sid}]
        return []

    monkeypatch.setattr(gr.age_client, "execute_cypher", fake_execute_cypher)


class TestJurisdictionAliasResolution:
    """[findings.md JURISDICTION] English aliases, normalization, ambiguity."""

    async def test_canonical_urdu_district_still_resolves(self, monkeypatch):
        _stub_jurisdiction_graph(monkeypatch)
        assert await gr._resolve_district_id("لاہور") == "DIST-04"

    async def test_canonical_district_id_still_resolves(self, monkeypatch):
        _stub_jurisdiction_graph(monkeypatch)
        assert await gr._resolve_district_id("DIST-04") == "DIST-04"

    async def test_english_district_alias_resolves(self, monkeypatch):
        """The defect: English named the right district but resolved to nothing."""
        _stub_jurisdiction_graph(monkeypatch)
        for form in ("Lahore", "lahore", "LAHORE"):
            assert await gr._resolve_district_id(form) == "DIST-04", form

    async def test_whitespace_is_normalized(self, monkeypatch):
        _stub_jurisdiction_graph(monkeypatch)
        assert await gr._resolve_district_id(" Lahore ") == "DIST-04"
        assert await gr._resolve_district_id(" لاہور ") == "DIST-04"

    async def test_every_unambiguous_english_district_resolves(self, monkeypatch):
        _stub_jurisdiction_graph(monkeypatch)
        expected = {
            "Rawalpindi": "DIST-07", "Faisalabad": "DIST-01", "Islamabad": "DIST-06",
            "Multan": "DIST-09", "Chiniot": "DIST-05", "Hyderabad": "DIST-03",
        }
        for name, did in expected.items():
            assert await gr._resolve_district_id(name) == did, name

    async def test_ambiguous_karachi_does_not_pick_a_district(self, monkeypatch):
        """Two real Karachi districts exist; choosing either would silently
        narrow a supervisor's query to half the city."""
        _stub_jurisdiction_graph(monkeypatch)
        assert await gr._resolve_district_id("Karachi") is None
        assert await gr._resolve_district_id(" karachi ") is None

    async def test_specific_karachi_aliases_resolve(self, monkeypatch):
        _stub_jurisdiction_graph(monkeypatch)
        assert await gr._resolve_district_id("Karachi Central") == "DIST-02"
        assert await gr._resolve_district_id("Karachi East") == "DIST-08"

    async def test_unknown_district_fails_closed(self, monkeypatch):
        _stub_jurisdiction_graph(monkeypatch)
        assert await gr._resolve_district_id("Atlantis") is None
        assert await gr._resolve_district_id("") is None

    async def test_canonical_station_forms_still_resolve(self, monkeypatch):
        _stub_jurisdiction_graph(monkeypatch)
        assert await gr._resolve_station_id("PS-LHR-MODELTOWN") == "PS-LHR-MODELTOWN"
        assert await gr._resolve_station_id(
            "تھانہ ماڈل ٹاؤن، لاہور"
        ) == "PS-LHR-MODELTOWN"

    async def test_english_station_alias_resolves(self, monkeypatch):
        _stub_jurisdiction_graph(monkeypatch)
        for form in ("Model Town", " model town ", "MODEL TOWN"):
            assert await gr._resolve_station_id(form) == "PS-LHR-MODELTOWN", form

    async def test_substring_accident_no_longer_resolves_station(self, monkeypatch):
        """"Karachi" used to resolve to PS-KHI-NEWKARACHI purely because
        "khi" sits inside that ASCII id — an accident, not an alias."""
        _stub_jurisdiction_graph(monkeypatch)
        assert await gr._resolve_station_id("Karachi") is None

    async def test_ambiguous_station_alias_fails_closed(self, monkeypatch):
        """"Saddar" names two real stations (Chiniot and Rawalpindi)."""
        _stub_jurisdiction_graph(monkeypatch)
        assert await gr._resolve_station_id("Saddar") is None
        assert await gr._resolve_station_id("Nowhere PS") is None
