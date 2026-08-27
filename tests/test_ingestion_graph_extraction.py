"""
Tests for the Phase 4.10 graph-extraction step wired into
src/ingestion/service.py (_run_graph_extraction, _write_unresolved_mention,
_attach_chunk_identifiers).

No real LLM/AGE — src.extraction.doc_classifier/ner/domain_entities and
src.graph.entity_resolution/versioning are all monkeypatched. End-to-end
correctness against a real AGE instance + real Qwen3-14B model server (a
real FIR PDF ingested with entities resolved, doc_type classified, and
edges written) was verified live during development; these tests guard
the wiring/control-flow in service.py against regressions.
"""
from types import SimpleNamespace

import pytest

import src.ingestion.service as service


class FakeChunk:
    def __init__(self, doc_id, text, metadata=None):
        self.doc_id = doc_id
        self.text = text
        self.metadata = metadata or {}


@pytest.fixture
def stub_graph_deps(monkeypatch):
    """Stub every extraction/graph submodule _run_graph_extraction imports locally."""
    calls = {"nodes": [], "edges": [], "resolved": [], "unresolved": []}

    async def fake_write_node(label, match, properties=None, *, source_doc_id=None, confidence=1.0):
        calls["nodes"].append({"label": label, "match": match})
        return {"id": 1, "label": label, "properties": {**match, **(properties or {})}}

    async def fake_write_edge(edge_label, from_label, from_match, to_label, to_match,
                               properties=None, *, source_doc_id, source_chunk_id=None,
                               confidence=1.0, supersedes_edge_id=None):
        calls["edges"].append({"edge_label": edge_label, "from_match": from_match, "to_match": to_match})
        return {"id": 2, "label": edge_label, "properties": properties or {}}

    async def fake_resolve_and_write(entity_type, mention, case_id, source_doc_id, source_chunk_id=None):
        calls["resolved"].append({"type": entity_type, "mention": mention})
        return {"entity_id": "X-1", "tier": "new", "confidence": 0.0, "basis": "", "is_new_node": True}

    async def fake_classify_document(text):
        return {"doc_type": "FIR", "confidence": 0.9, "reasoning": "", "date_registered": "2026-01-01"}

    async def fake_extract_entities(text, source_chunk_id=None):
        return [
            {"text": "احمد رضا قریشی", "type": "person", "char_span": [0, 5],
             "source_chunk_id": source_chunk_id, "confidence": 0.9, "attributes": {}},
        ]

    async def fake_extract_domain_entities(text, source_chunk_id=None):
        return [
            {"text": "بور 30 پستول", "type": "weapon", "char_span": [10, 20],
             "source_chunk_id": source_chunk_id, "confidence": 0.8, "attributes": {}},
        ]

    import src.graph.versioning as versioning_mod
    import src.graph.entity_resolution as er_mod
    import src.extraction.doc_classifier as doc_classifier_mod
    import src.extraction.ner as ner_mod
    import src.extraction.domain_entities as domain_entities_mod

    monkeypatch.setattr(versioning_mod, "write_node", fake_write_node)
    monkeypatch.setattr(versioning_mod, "write_edge", fake_write_edge)
    monkeypatch.setattr(er_mod, "resolve_and_write", fake_resolve_and_write)
    monkeypatch.setattr(doc_classifier_mod, "classify_document", fake_classify_document)
    monkeypatch.setattr(ner_mod, "extract_entities", fake_extract_entities)
    monkeypatch.setattr(domain_entities_mod, "extract_domain_entities", fake_extract_domain_entities)

    return calls


@pytest.mark.asyncio
async def test_run_graph_extraction_writes_case_and_document_nodes(stub_graph_deps):
    documents = [SimpleNamespace(text="مقدمہ FIR-2026-ARMS-001 کے تحت۔")]
    chunks = [FakeChunk("DOC-1_c0", "احمد رضا قریشی نے بیان دیا۔ بور 30 پستول برآمد ہوا۔")]

    stats = await service._run_graph_extraction(
        "test.pdf", documents, chunks, "CASE-001", "DOC-1"
    )

    node_labels = [n["label"] for n in stub_graph_deps["nodes"]]
    assert "Case" in node_labels
    assert "Document" in node_labels
    assert stats["doc_type"] == "FIR"
    assert stats["errors"] == []


@pytest.mark.asyncio
async def test_unrecognized_doc_type_does_not_clobber_document_node_with_null(monkeypatch, stub_graph_deps):
    """
    M7 (Muhafiz Data API migration, docs/decisions/0001-muhafiz-api-migration.md):
    when doc_classifier returns doc_type=None (out-of-vocabulary type, see
    that module's own test coverage), _run_graph_extraction's Document
    write must OMIT doc_type entirely, not set it to null — write_node()
    MERGEs, so writing null here would clobber a real doc_type from an
    earlier successful classification on a document re-run.
    """
    import src.extraction.doc_classifier as doc_classifier_mod

    async def fake_classify_document(text):
        return {
            "doc_type": None, "confidence": 0.5, "reasoning": "",
            "date_registered": "2026-01-20", "date_registered_confidence": "labeled",
        }
    monkeypatch.setattr(doc_classifier_mod, "classify_document", fake_classify_document)

    written_properties = []
    import src.graph.versioning as versioning_mod

    async def spying_write_node(label, match, properties=None, *, source_doc_id=None, confidence=1.0):
        if label == "Document" and match == {"doc_id": "DOC-1"}:
            written_properties.append(properties or {})
        return {"id": 1, "label": label, "properties": {**match, **(properties or {})}}
    monkeypatch.setattr(versioning_mod, "write_node", spying_write_node)

    documents = [SimpleNamespace(text="text")]
    chunks = [FakeChunk("DOC-1_c0", "کچھ متن")]

    stats = await service._run_graph_extraction("t.pdf", documents, chunks, "CASE-001", "DOC-1")

    # The FIRST Document write (filename, from the top of the function) has
    # no doc_type key at all; the classification-driven write must also omit
    # doc_type — never set it to None.
    #
    # Selected by content rather than position: a clean run now also stamps
    # a `projection_complete` marker onto this same Document node as its
    # LAST write (findings.md legacy re-ingestion), so `[-1]` is no longer
    # the classification write.
    classification_write = next(
        p for p in written_properties if "date_registered" in p
    )
    assert "doc_type" not in classification_write
    assert classification_write["date_registered"] == "2026-01-20"
    assert stats["doc_type"] is None


@pytest.mark.asyncio
async def test_resolvable_mention_goes_through_entity_resolution(stub_graph_deps):
    documents = [SimpleNamespace(text="text")]
    chunks = [FakeChunk("DOC-1_c0", "احمد رضا قریشی نے بیان دیا۔")]

    await service._run_graph_extraction("t.pdf", documents, chunks, "CASE-001", "DOC-1")

    resolved_types = [r["type"] for r in stub_graph_deps["resolved"]]
    assert "person" in resolved_types


@pytest.mark.asyncio
async def test_same_person_mentioned_twice_in_one_document_reuses_one_entity_id(stub_graph_deps):
    """
    [findings.md Module 11] The exact same canonical_name mentioned in TWO
    different chunks of ONE document must resolve to the SAME entity_id —
    resolve_and_write() (which mints a fresh node + BELONGS_TO_CASE edge +
    a near-certain-duplicate pending SAME_AS proposal on every call) must
    only run ONCE for this name, not once per chunk. Live-reproduced root
    cause this guards against: one real document minted 368 near-duplicate
    Person nodes for what the community summarizer itself recognized as
    one real person.

    stub_graph_deps's fake_extract_entities() always returns the same
    "احمد رضا قریشی" person regardless of chunk text — passing two chunks
    already reproduces the "same name, two chunks" shape without a custom
    fixture.
    """
    documents = [SimpleNamespace(text="احمد رضا قریشی نے بیان دیا۔ دوبارہ احمد رضا قریشی نے کہا۔")]
    chunks = [
        FakeChunk("DOC-1_c0", "احمد رضا قریشی نے بیان دیا۔"),
        FakeChunk("DOC-1_c1", "دوبارہ احمد رضا قریشی نے کہا۔"),
    ]

    stats = await service._run_graph_extraction(
        "t.pdf", documents, chunks, "CASE-001", "DOC-1"
    )

    person_resolutions = [c for c in stub_graph_deps["resolved"] if c["type"] == "person"]
    assert len(person_resolutions) == 1, "resolve_and_write() must run exactly once for a same-document exact-string repeat"

    # fake_resolve_and_write itself writes no edges (fully stubbed) — the
    # only PERSON APPEARS_IN edge that can show up here comes from the
    # dedup short-circuit's own write, for the SECOND (reused) occurrence
    # (the weapon domain-entity mention that fires every chunk via
    # stub_graph_deps's own fake_extract_domain_entities() writes its own,
    # unrelated APPEARS_IN edges through _write_unresolved_mention —
    # filtered out here by entity_id).
    appears_in_edges = [
        e for e in stub_graph_deps["edges"]
        if e["edge_label"] == "APPEARS_IN" and e["from_match"] == {"entity_id": "X-1"}
    ]
    assert len(appears_in_edges) == 1
    assert appears_in_edges[0]["from_match"] == {"entity_id": "X-1"}
    assert appears_in_edges[0]["to_match"] == {"doc_id": "DOC-1"}

    assert stats["entities_resolved"] >= 2  # both occurrences still counted as resolved


@pytest.mark.asyncio
async def test_different_documents_same_name_are_unaffected(stub_graph_deps):
    """[findings.md Module 11] The dedup cache is document-scoped (a fresh
    dict per _run_graph_extraction call) — a second, SEPARATE document
    ingested afterward with the same person name must go through the
    normal (unchanged) name-fallback resolution path again, proving A1
    does not widen cross-document matching at all."""
    documents = [SimpleNamespace(text="text")]
    chunks = [FakeChunk("DOC-1_c0", "احمد رضا قریشی نے بیان دیا۔")]

    await service._run_graph_extraction("t1.pdf", documents, chunks, "CASE-001", "DOC-1")
    await service._run_graph_extraction("t2.pdf", documents, chunks, "CASE-002", "DOC-2")

    person_resolutions = [c for c in stub_graph_deps["resolved"] if c["type"] == "person"]
    assert len(person_resolutions) == 2, "a second, separate document must resolve independently, not reuse the first document's cache"


@pytest.mark.asyncio
async def test_weapon_mention_bypasses_resolution(stub_graph_deps):
    documents = [SimpleNamespace(text="text")]
    chunks = [FakeChunk("DOC-1_c0", "بور 30 پستول برآمد ہوا۔")]

    stats = await service._run_graph_extraction(
        "t.pdf", documents, chunks, "CASE-001", "DOC-1"
    )

    # Weapon is not in _RESOLVABLE_MENTION_TYPES — it must never reach
    # entity_resolution.resolve_and_write.
    assert all(r["type"] != "weapon" for r in stub_graph_deps["resolved"])
    assert stats["entities_unresolved"] >= 1
    weapon_nodes = [n for n in stub_graph_deps["nodes"] if n["label"] == "Weapon"]
    assert weapon_nodes


@pytest.mark.asyncio
async def test_graph_extraction_failure_is_caught_and_reported(monkeypatch, stub_graph_deps):
    import src.extraction.doc_classifier as doc_classifier_mod

    async def boom(text):
        raise RuntimeError("model server unreachable")
    monkeypatch.setattr(doc_classifier_mod, "classify_document", boom)

    documents = [SimpleNamespace(text="text")]
    chunks = [FakeChunk("DOC-1_c0", "کچھ متن")]

    # Must not raise — degrades to a stats dict with the error recorded.
    stats = await service._run_graph_extraction(
        "t.pdf", documents, chunks, "CASE-001", "DOC-1"
    )
    assert any("doc_classifier" in e for e in stats["errors"])
    assert stats["doc_type"] is None


@pytest.mark.asyncio
async def test_attach_chunk_identifiers_only_fires_for_single_person_single_cnic():
    mention = {"canonical_name": "احمد رضا قریشی"}
    text_with_one_cnic = "شناختی کارڈ نمبر 00000-9119877-0"

    with_cnic = service._attach_chunk_identifiers(mention, text_with_one_cnic, person_mentions_in_chunk=1)
    assert with_cnic.get("cnic") == "00000-9119877-0"

    # Two person mentions in the same chunk -> too ambiguous to attach.
    without_cnic = service._attach_chunk_identifiers(mention, text_with_one_cnic, person_mentions_in_chunk=2)
    assert "cnic" not in without_cnic


# ── Document-wide CNIC hoisting (the "if his CNIC is in the document, all
# his mentions in it belong to him" gap) ────────────────────────────────

def test_document_cnic_for_name_matches_bare_given_name_to_full_name():
    name_cnics = {"فیصل ولد محمد رمضان": "00000-9000057-1"}
    assert service._document_cnic_for_name("فیصل", name_cnics) == "00000-9000057-1"
    # Symmetric: the full name looking up the bare name's CNIC works too.
    assert service._document_cnic_for_name(
        "فیصل ولد محمد رمضان", {"فیصل": "00000-9000057-1"}
    ) == "00000-9000057-1"


def test_document_cnic_for_name_refuses_to_guess_between_two_different_people():
    """Two different people in the same document share a given name, with
    different CNICs — both are reachable via containment, so this must
    return None rather than pick one."""
    name_cnics = {
        "فیصل ولد محمد رمضان": "00000-9000057-1",
        "فیصل احمد": "00000-1111111-1",
    }
    assert service._document_cnic_for_name("فیصل", name_cnics) is None


def test_document_cnic_for_name_no_match():
    assert service._document_cnic_for_name("کاشف", {"فیصل ولد محمد رمضان": "00000-9000057-1"}) is None


def test_document_cnic_for_name_ignores_character_prefix_that_is_not_a_word_boundary():
    """'فیصل' must not match 'فیصلآباد' (a place name) just because it
    shares a character prefix — the check is token-based, not substring."""
    assert service._document_cnic_for_name("فیصل", {"فیصلآباد": "00000-9000057-1"}) is None


@pytest.mark.asyncio
async def test_full_name_cnic_hoisted_onto_later_bare_name_mention(monkeypatch, stub_graph_deps):
    """
    The exact scenario this fix targets: chunk 1 has the narrative's own
    identification line (full name + CNIC, alone in its chunk); chunk 2
    later refers to the same person by given name alone, with no CNIC
    nearby. The bare-name mention must resolve with the hoisted CNIC
    attached, so entity_resolution routes it through TIER_CNIC_AUTO
    against the SAME node instead of minting a name-fallback duplicate.
    """
    import src.extraction.ner as ner_mod

    async def fake_extract_entities(text, source_chunk_id=None):
        if source_chunk_id == "DOC-1_c0":
            return [{"text": "فیصل ولد محمد رمضان", "type": "person", "char_span": [0, 5],
                      "source_chunk_id": source_chunk_id, "confidence": 0.9, "attributes": {}}]
        return [{"text": "فیصل", "type": "person", "char_span": [0, 5],
                  "source_chunk_id": source_chunk_id, "confidence": 0.9, "attributes": {}}]
    monkeypatch.setattr(ner_mod, "extract_entities", fake_extract_entities)

    documents = [SimpleNamespace(text="text")]
    chunks = [
        FakeChunk("DOC-1_c0", "فیصل ولد محمد رمضان، شناختی کارڈ نمبر 00000-9000057-1۔"),
        FakeChunk("DOC-1_c1", "فیصل نے دوبارہ بیان دیا۔"),
    ]

    await service._run_graph_extraction("t.pdf", documents, chunks, "CASE-001", "DOC-1")

    person_mentions = [c["mention"] for c in stub_graph_deps["resolved"] if c["type"] == "person"]
    assert len(person_mentions) == 2
    assert person_mentions[0]["cnic"] == "00000-9000057-1"
    assert person_mentions[1]["canonical_name"] == "فیصل"
    assert person_mentions[1]["cnic"] == "00000-9000057-1", \
        "the bare given-name mention must inherit the document's already-observed CNIC"


@pytest.mark.asyncio
async def test_cnic_found_after_the_fact_is_backfilled_onto_the_already_resolved_node(stub_graph_deps, monkeypatch):
    """
    Reverse order: the bare given name is mentioned FIRST (resolves to a
    new node with no CNIC), and the full identification line with the
    CNIC only appears in a LATER chunk, on an exact repeat of that same
    string. Because the dedup cache short-circuits resolve_and_write for
    exact-string repeats, the only place this CNIC can still land is a
    backfill write onto the node the first mention already created.
    """
    import src.extraction.ner as ner_mod
    import src.graph.versioning as versioning_mod

    async def fake_extract_entities(text, source_chunk_id=None):
        return [{"text": "فیصل", "type": "person", "char_span": [0, 5],
                  "source_chunk_id": source_chunk_id, "confidence": 0.9, "attributes": {}}]
    monkeypatch.setattr(ner_mod, "extract_entities", fake_extract_entities)

    backfills = []
    original_write_node = versioning_mod.write_node

    async def spying_write_node(label, match, properties=None, *, source_doc_id=None, confidence=1.0):
        if properties and "cnic" in properties:
            backfills.append({"label": label, "match": match, "properties": properties})
        return await original_write_node(label, match, properties, source_doc_id=source_doc_id, confidence=confidence)
    monkeypatch.setattr(versioning_mod, "write_node", spying_write_node)

    documents = [SimpleNamespace(text="text")]
    chunks = [
        FakeChunk("DOC-1_c0", "فیصل نے بیان دیا۔"),
        FakeChunk("DOC-1_c1", "فیصل، شناختی کارڈ نمبر 00000-9000057-1۔"),
    ]

    await service._run_graph_extraction("t.pdf", documents, chunks, "CASE-001", "DOC-1")

    person_resolutions = [c for c in stub_graph_deps["resolved"] if c["type"] == "person"]
    assert len(person_resolutions) == 1, "still only ONE resolve_and_write call for the exact-string repeat"

    assert len(backfills) == 1
    assert backfills[0]["match"] == {"entity_id": "X-1"}
    assert backfills[0]["properties"]["cnic"] == "00000-9000057-1"


# ── Relationship extraction (4.6) — within-chunk and cross-chunk (Priority
# 4 of the 2026-08-06 open-gaps audit) ───────────────────────────────────

def _stub_people_per_chunk(monkeypatch, people_by_chunk: dict[str, list[str]]):
    """Make ner.extract_entities return a fixed set of "person" mentions
    per chunk_id, and domain_entities.extract_domain_entities return
    nothing — isolates these tests to the relationship-extraction step."""
    import src.extraction.ner as ner_mod
    import src.extraction.domain_entities as domain_entities_mod

    async def fake_extract_entities(text, source_chunk_id=None):
        names = people_by_chunk.get(source_chunk_id, [])
        return [
            {"text": name, "type": "person", "char_span": [0, len(name)],
             "source_chunk_id": source_chunk_id, "confidence": 0.9, "attributes": {}}
            for name in names
        ]

    async def fake_extract_domain_entities(text, source_chunk_id=None):
        return []

    monkeypatch.setattr(ner_mod, "extract_entities", fake_extract_entities)
    monkeypatch.setattr(domain_entities_mod, "extract_domain_entities", fake_extract_domain_entities)


def _stub_resolve_by_name(monkeypatch):
    """entity_id = the mention's own name, so assertions can identify which
    person a written edge connects without threading synthetic ids
    through the fixture."""
    import src.graph.entity_resolution as er_mod

    async def fake_resolve_and_write(entity_type, mention, case_id, source_doc_id, source_chunk_id=None):
        name = mention["canonical_name"]
        return {"entity_id": f"P-{name}", "tier": "new", "confidence": 0.0, "basis": "", "is_new_node": True}

    monkeypatch.setattr(er_mod, "resolve_and_write", fake_resolve_and_write)


@pytest.mark.asyncio
async def test_relationship_found_within_a_single_chunk(monkeypatch, stub_graph_deps):
    _stub_people_per_chunk(monkeypatch, {"DOC-1_c0": ["Irfan Mirza", "Bilal Malik"]})
    _stub_resolve_by_name(monkeypatch)

    import src.extraction.relationship_extraction as rel_mod

    async def fake_extract_relationships(text, person_names):
        assert set(person_names) == {"Irfan Mirza", "Bilal Malik"}
        return [{"person_a": "Irfan Mirza", "person_b": "Bilal Malik", "basis": "father/son", "confidence": 0.9}]
    monkeypatch.setattr(rel_mod, "extract_relationships", fake_extract_relationships)

    documents = [SimpleNamespace(text="text")]
    chunks = [FakeChunk("DOC-1_c0", "Irfan Mirza's son Bilal Malik was present.")]

    stats = await service._run_graph_extraction(
        "t.pdf", documents, chunks, "CASE-001", "DOC-1"
    )

    assert stats["relationships_written"] == 1
    edges = [e for e in stub_graph_deps["edges"] if e["edge_label"] == "ASSOCIATED_WITH"]
    assert len(edges) == 1


@pytest.mark.asyncio
async def test_relationship_found_across_adjacent_chunks(monkeypatch, stub_graph_deps):
    """The exact gap this fix closes: two people who never co-occur in any
    single chunk, but do in the concatenated text of two adjacent chunks."""
    _stub_people_per_chunk(monkeypatch, {
        "DOC-1_c0": ["Irfan Mirza"],
        "DOC-1_c1": ["Bilal Malik"],
    })
    _stub_resolve_by_name(monkeypatch)

    import src.extraction.relationship_extraction as rel_mod
    calls = []

    async def fake_extract_relationships(text, person_names):
        calls.append(set(person_names))
        # Only the combined two-person call (the cross-chunk pass) finds
        # anything — mirrors a real single-chunk call never seeing both
        # names and so never proposing a pair.
        if set(person_names) == {"Irfan Mirza", "Bilal Malik"}:
            return [{"person_a": "Irfan Mirza", "person_b": "Bilal Malik", "basis": "father/son", "confidence": 0.9}]
        return []
    monkeypatch.setattr(rel_mod, "extract_relationships", fake_extract_relationships)

    documents = [SimpleNamespace(text="text")]
    chunks = [
        FakeChunk("DOC-1_c0", "Irfan Mirza filed the complaint."),
        FakeChunk("DOC-1_c1", "His son Bilal Malik corroborated the account."),
    ]

    stats = await service._run_graph_extraction(
        "t.pdf", documents, chunks, "CASE-001", "DOC-1"
    )

    # Both single-chunk calls were skipped entirely (each chunk has only 1
    # person — _extract_and_write_relationships's own len(persons)<2 guard)
    # — only the adjacent-pair call ran, and it's the one that found the edge.
    assert calls == [{"Irfan Mirza", "Bilal Malik"}]
    assert stats["relationships_written"] == 1
    edges = [e for e in stub_graph_deps["edges"] if e["edge_label"] == "ASSOCIATED_WITH"]
    assert len(edges) == 1


@pytest.mark.asyncio
async def test_relationship_not_found_when_people_are_two_chunks_apart(monkeypatch, stub_graph_deps):
    """Documents this fix's own stated residual limitation: only ADJACENT
    chunk pairs are windowed, not the whole document."""
    _stub_people_per_chunk(monkeypatch, {
        "DOC-1_c0": ["Irfan Mirza"],
        "DOC-1_c1": [],
        "DOC-1_c2": ["Bilal Malik"],
    })
    _stub_resolve_by_name(monkeypatch)

    import src.extraction.relationship_extraction as rel_mod

    async def fake_extract_relationships(text, person_names):
        if set(person_names) == {"Irfan Mirza", "Bilal Malik"}:
            return [{"person_a": "Irfan Mirza", "person_b": "Bilal Malik", "basis": "father/son", "confidence": 0.9}]
        return []
    monkeypatch.setattr(rel_mod, "extract_relationships", fake_extract_relationships)

    documents = [SimpleNamespace(text="text")]
    chunks = [
        FakeChunk("DOC-1_c0", "Irfan Mirza filed the complaint."),
        FakeChunk("DOC-1_c1", "The station received the report."),
        FakeChunk("DOC-1_c2", "Bilal Malik was later questioned."),
    ]

    stats = await service._run_graph_extraction(
        "t.pdf", documents, chunks, "CASE-001", "DOC-1"
    )

    assert stats["relationships_written"] == 0
    edges = [e for e in stub_graph_deps["edges"] if e["edge_label"] == "ASSOCIATED_WITH"]
    assert edges == []


@pytest.mark.asyncio
async def test_same_pair_from_within_chunk_and_cross_chunk_written_once(monkeypatch, stub_graph_deps):
    """written_pairs dedup: the same two people can legitimately be
    proposed by both the within-chunk pass (they co-occur in chunk 1) and
    the adjacent-pair pass (chunk 1 + chunk 2 combined) — must not produce
    two ASSOCIATED_WITH edges for one document."""
    _stub_people_per_chunk(monkeypatch, {
        "DOC-1_c0": ["Irfan Mirza", "Bilal Malik"],
        "DOC-1_c1": ["Bilal Malik"],
    })
    _stub_resolve_by_name(monkeypatch)

    import src.extraction.relationship_extraction as rel_mod

    async def fake_extract_relationships(text, person_names):
        if {"Irfan Mirza", "Bilal Malik"} <= set(person_names):
            return [{"person_a": "Irfan Mirza", "person_b": "Bilal Malik", "basis": "father/son", "confidence": 0.9}]
        return []
    monkeypatch.setattr(rel_mod, "extract_relationships", fake_extract_relationships)

    documents = [SimpleNamespace(text="text")]
    chunks = [
        FakeChunk("DOC-1_c0", "Irfan Mirza's son Bilal Malik was present."),
        FakeChunk("DOC-1_c1", "Bilal Malik gave a statement."),
    ]

    stats = await service._run_graph_extraction(
        "t.pdf", documents, chunks, "CASE-001", "DOC-1"
    )

    assert stats["relationships_written"] == 1
    edges = [e for e in stub_graph_deps["edges"] if e["edge_label"] == "ASSOCIATED_WITH"]
    assert len(edges) == 1


@pytest.mark.asyncio
async def test_ingest_file_skips_graph_extraction_without_case_id(monkeypatch, stub_graph_deps):
    """ingest_file() must not call _run_graph_extraction at all when case_id is None."""
    called = []

    async def fake_run_graph_extraction(*args, **kwargs):
        called.append(1)
        return {}
    monkeypatch.setattr(service, "_run_graph_extraction", fake_run_graph_extraction)

    # Stub everything else ingest_file needs so it reaches the graph-step
    # decision point without touching real loaders/embeddings/DBs.
    from src.ingestion.document import Document as IngestDoc

    def fake_route_and_load(path):
        return [IngestDoc(text="some content", metadata={}, doc_id="d1")]
    monkeypatch.setattr(service, "route_and_load", fake_route_and_load)

    class FakeChunkObj:
        def __init__(self):
            self.doc_id = "d1_c0"
            self.text = "some content"
            self.metadata = {"doc_id": "d1"}

    def fake_chunk_documents(documents, chunk_size, chunk_overlap):
        return [FakeChunkObj()]
    monkeypatch.setattr(service, "chunk_documents", fake_chunk_documents)

    async def fake_embed_texts(texts, task_type=None):
        return [[0.1, 0.2] for _ in texts]
    monkeypatch.setattr(service, "embed_texts", fake_embed_texts)

    async def fake_upsert_documents(**kwargs):
        return None
    monkeypatch.setattr(service, "upsert_documents", fake_upsert_documents)

    from pathlib import Path
    await service.ingest_file(Path("fake.pdf"), case_id=None)

    assert called == []
