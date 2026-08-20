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
    # no doc_type key at all; the SECOND (classification-driven) write
    # must also omit doc_type — never set it to None.
    classification_write = written_properties[-1]
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
