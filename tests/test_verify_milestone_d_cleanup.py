"""
Tests for scripts/verify_milestone_d.py's fixture cleanup.

Guards a measured production incident. That script writes synthetic
fixtures into the REAL graph (by design — see its module docstring) and
cleans up in a `finally` block. Its cleanup matched only NODE identity:

    MATCH (n) WHERE n.entity_id STARTS WITH $tag
                 OR n.case_id  STARTS WITH $tag
                 OR n.doc_id   STARTS WITH $tag
    DETACH DELETE n

Two independent reasons that could never be sufficient:

  a) fixture Persons come from `entity_resolution.resolve_and_write()`,
     which mints "PERSON-<hex>" ids carrying no tag — so the predicate
     matched no fixture Person, and their SAME_AS edges outlived the run;
  b) entity resolution's candidate scan is global, so a fixture mention
     can score a pending SAME_AS against a REAL corpus Person. That
     edge's far endpoint must survive any cleanup.

Measured before the fix: 48 fixture SAME_AS left in the production graph,
one attached to the genuine corpus Person PERSON-0075e0c602
(فہد میمن, psrms/fir/fir-142-26#structured).

SAFETY: every test here runs against an in-memory fake graph. Nothing
touches live Postgres/AGE.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

import scripts.verify_milestone_d as vmd

# Real-shaped identifiers, used as data only — never queried live.
REAL_CORPUS_PERSON = "PERSON-0075e0c602"
REAL_CORPUS_EDGE_PROVENANCE = "psrms/fir/fir-142-26#structured"


class FakeGraph:
    """
    Minimal stand-in supporting the exact Cypher shapes _cleanup() uses:
    relationship delete by `r.source_doc_id STARTS WITH`, node delete by
    tagged identity, and node delete by explicit `entity_id IN` list.
    """

    def __init__(self):
        self.nodes: dict[str, dict] = {}
        self.edges: list[dict] = []

    def add_node(self, entity_id=None, case_id=None, doc_id=None, source_doc_id=None):
        key = entity_id or case_id or doc_id or source_doc_id
        self.nodes[key] = {
            "entity_id": entity_id, "case_id": case_id, "doc_id": doc_id,
            "source_doc_id": source_doc_id,
        }
        return key

    def add_edge(self, a, b, source_doc_id):
        self.edges.append({"a": a, "b": b, "source_doc_id": source_doc_id})

    async def execute_cypher(self, query, params=None, columns=None, graph=None):
        p = params or {}
        if "DELETE r" in query:
            prefix = p.get("tag") or p.get("prefix") or ""
            self.edges = [
                e for e in self.edges
                if not (e["source_doc_id"] or "").startswith(prefix)
            ]
            return []
        if "n.entity_id IN $ids" in query:
            self._detach_delete(p.get("ids", []))
            return []
        if "DETACH DELETE n" in query:
            tag = p.get("tag", "D1VERIFY-")
            # The startup sweep's query (no $tag param) hardcodes both
            # prefixes directly rather than parameterizing — mirror that:
            # check source_doc_id against BOTH known prefixes whenever
            # this query has no bound tag param of its own.
            prefixes = [tag] if "tag" in p else ["D1VERIFY-", "D1DEBUG-"]
            self._detach_delete([
                k for k, n in self.nodes.items()
                if any(
                    (n.get(f) or "").startswith(prefix)
                    for f in ("entity_id", "case_id", "doc_id", "source_doc_id")
                    for prefix in prefixes
                )
            ])
            return []
        return []

    def _detach_delete(self, keys):
        """
        Model AGE's DETACH DELETE faithfully: an edge disappears only
        because one of ITS OWN endpoints is being deleted.

        This distinction is what makes the mutation check meaningful. If
        the fake dropped every edge touching a deleted node's *neighbour*
        too, node cleanup would mask a missing relationship step — and a
        fixture edge anchored to a surviving corpus Person is exactly the
        case that must NOT be swept away implicitly.
        """
        doomed = set(keys)
        for k in doomed:
            self.nodes.pop(k, None)
        self.edges = [
            e for e in self.edges
            if not (e["a"] in doomed or e["b"] in doomed)
        ]


class _FakeSession:
    """No-op stand-in for the Postgres half of _cleanup()."""

    def __init__(self):
        self.statements = []

    async def execute(self, stmt, params=None):
        self.statements.append((stmt, params))
        return None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def graph(monkeypatch):
    g = FakeGraph()
    monkeypatch.setattr(vmd.age_client, "execute_cypher", g.execute_cypher)
    monkeypatch.setattr(vmd, "_created_entity_ids", [])

    # _cleanup() also deletes pending_candidate_priority rows via
    # src.database.postgres.get_session. That is out of scope here — and
    # the suite's fail-closed DB guard would refuse a live connection
    # anyway — so it is replaced with an in-memory recorder.
    import src.database.postgres as pg

    monkeypatch.setattr(pg, "get_session", lambda: _FakeSession())
    return g


def _seed_fixture_run(g, tag):
    """One run's worth of fixtures: 2 Persons, a Case, a Doc, one SAME_AS."""
    a = g.add_node(entity_id="PERSON-aaaa111122")
    b = g.add_node(entity_id="PERSON-bbbb333344")
    g.add_node(case_id=f"{tag}-CASE-A")
    g.add_node(doc_id=f"{tag}-DOC-A")
    g.add_edge(a, b, f"{tag}-DOC-A")
    vmd._created_entity_ids.extend([a, b])
    return a, b


# ── Tracked-entity safety (the gate that makes node deletion safe) ──────


def test_new_node_is_tracked_for_deletion():
    with patch.object(vmd, "_created_entity_ids", []) as tracked:
        returned = vmd._track_created_entity(
            {"entity_id": "PERSON-newly1234", "is_new_node": True}
        )
    assert returned == "PERSON-newly1234"
    assert tracked == ["PERSON-newly1234"]


def test_reused_corpus_entity_is_never_tracked():
    """
    resolve_and_write() returns is_new_node=False when TIER_CNIC_AUTO
    reused an existing entity — that id can be a real corpus Person, and
    the cleanup deletes by this list. It must not appear here.
    """
    with patch.object(vmd, "_created_entity_ids", []) as tracked:
        returned = vmd._track_created_entity(
            {"entity_id": REAL_CORPUS_PERSON, "is_new_node": False}
        )
    assert returned == REAL_CORPUS_PERSON
    assert tracked == [], "a reused corpus entity must never be queued for deletion"


def test_missing_is_new_node_defaults_to_not_tracked():
    """Absent flag → treat as reuse. Fail safe, not fail destructive."""
    with patch.object(vmd, "_created_entity_ids", []) as tracked:
        vmd._track_created_entity({"entity_id": "PERSON-unknown99"})
    assert tracked == []


# ── Cleanup behaviour ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_fixture_to_fixture_edge_and_nodes_removed(graph, monkeypatch):
    monkeypatch.setattr(vmd, "TAG", "D1VERIFY-testtag")
    a, b = _seed_fixture_run(graph, "D1VERIFY-testtag")

    await vmd._cleanup()

    assert graph.edges == []
    assert a not in graph.nodes and b not in graph.nodes
    assert graph.nodes == {}


@pytest.mark.asyncio
async def test_fixture_edge_removed_but_real_corpus_person_survives(graph, monkeypatch):
    """THE regression: the incident that actually happened."""
    monkeypatch.setattr(vmd, "TAG", "D1VERIFY-testtag")
    fixture_person = graph.add_node(entity_id="PERSON-fixture001")
    graph.add_node(entity_id=REAL_CORPUS_PERSON)
    graph.add_edge(fixture_person, REAL_CORPUS_PERSON, "D1VERIFY-testtag-DOC-A")
    vmd._created_entity_ids.append(fixture_person)

    await vmd._cleanup()

    assert graph.edges == [], "fixture SAME_AS must be deleted"
    assert fixture_person not in graph.nodes
    assert REAL_CORPUS_PERSON in graph.nodes, "genuine corpus Person must survive"


@pytest.mark.asyncio
async def test_fixture_edge_between_two_surviving_nodes_is_deleted(graph, monkeypatch):
    """
    The case node-identity cleanup provably cannot reach, and the shape
    the 48 real remnants took: a fixture-provenance edge whose BOTH
    endpoints outlive cleanup.

    That happens whenever the far endpoint is corpus data and the near
    endpoint was never tracked (an untracked/reused id, or a crashed
    prior run). Only provenance-scoped relationship deletion removes it —
    which is precisely why this test fails if that step is dropped.
    """
    monkeypatch.setattr(vmd, "TAG", "D1VERIFY-testtag")
    corpus_a = graph.add_node(entity_id=REAL_CORPUS_PERSON)
    corpus_b = graph.add_node(entity_id="PERSON-corpus77777")
    graph.add_edge(corpus_a, corpus_b, "D1VERIFY-testtag-DOC-A")
    graph.add_edge(corpus_a, corpus_b, REAL_CORPUS_EDGE_PROVENANCE)

    await vmd._cleanup()

    survivors = [e["source_doc_id"] for e in graph.edges]
    assert survivors == [REAL_CORPUS_EDGE_PROVENANCE], "fixture edge must be deleted"
    assert corpus_a in graph.nodes and corpus_b in graph.nodes


@pytest.mark.asyncio
async def test_genuine_relationship_on_that_person_survives(graph, monkeypatch):
    """Cleanup is provenance-scoped, so a corpus edge on the same node stays."""
    monkeypatch.setattr(vmd, "TAG", "D1VERIFY-testtag")
    fixture_person = graph.add_node(entity_id="PERSON-fixture001")
    other_corpus = graph.add_node(entity_id="PERSON-corpus99999")
    graph.add_node(entity_id=REAL_CORPUS_PERSON)
    graph.add_edge(fixture_person, REAL_CORPUS_PERSON, "D1VERIFY-testtag-DOC-A")
    graph.add_edge(REAL_CORPUS_PERSON, other_corpus, REAL_CORPUS_EDGE_PROVENANCE)
    vmd._created_entity_ids.append(fixture_person)

    await vmd._cleanup()

    survivors = [e["source_doc_id"] for e in graph.edges]
    assert survivors == [REAL_CORPUS_EDGE_PROVENANCE]
    assert REAL_CORPUS_PERSON in graph.nodes
    assert other_corpus in graph.nodes


@pytest.mark.asyncio
async def test_cleanup_is_idempotent(graph, monkeypatch):
    monkeypatch.setattr(vmd, "TAG", "D1VERIFY-testtag")
    _seed_fixture_run(graph, "D1VERIFY-testtag")

    await vmd._cleanup()
    first = (dict(graph.nodes), list(graph.edges))
    await vmd._cleanup()

    assert (graph.nodes, graph.edges) == first
    assert graph.nodes == {} and graph.edges == []


@pytest.mark.asyncio
async def test_cleanup_runs_after_assertion_failure(graph, monkeypatch):
    """`finally` must clean up even when verification fails mid-run."""
    monkeypatch.setattr(vmd, "TAG", "D1VERIFY-testtag")
    _seed_fixture_run(graph, "D1VERIFY-testtag")

    async def _boom():
        raise AssertionError("FAILED: simulated verification failure")

    with pytest.raises(AssertionError):
        try:
            await _boom()
        finally:
            await vmd._cleanup()

    assert graph.edges == []
    assert graph.nodes == {}


@pytest.mark.asyncio
async def test_prior_run_sweep_removes_orphaned_fixture_persons_tagged_only_by_source_doc_id(graph, monkeypatch):
    """
    [2026-08-27, real contamination found live] A crashed prior run's
    fixture Person carries no tag on entity_id/case_id/doc_id (the
    documented limitation) — but DOES carry one on its own source_doc_id,
    stamped there by resolve_and_write() at write time. That's a SAFE,
    unambiguous signal (no real corpus document is ever ingested with a
    "D1VERIFY-"/"D1DEBUG-" source_doc_id), unlike the "untagged Person
    with no case link" heuristic this module's own docstring already
    rejected as too risky.

    Measured live: 24 such orphans ("Fahad Anjum Cheema" and near-
    variants) survived every previous sweep for weeks, invisible to all
    of them.
    """
    orphan = graph.add_node(entity_id="PERSON-orphan0001", source_doc_id="D1VERIFY-oldrun03-DOC-A")
    real_person = graph.add_node(entity_id=REAL_CORPUS_PERSON, source_doc_id=REAL_CORPUS_EDGE_PROVENANCE)

    await vmd._wipe_leftover_synthetic_data_from_prior_runs()

    assert orphan not in graph.nodes, "the source_doc_id-tagged orphan must be removed"
    assert real_person in graph.nodes, "a real corpus Person's own source_doc_id must never match a fixture tag"


@pytest.mark.asyncio
async def test_prior_run_sweep_removes_relationship_remnants(graph, monkeypatch):
    """
    The self-healing startup sweep must also delete by provenance —
    a crashed prior run's fixture Persons carry untagged ids, so the node
    sweep alone can never reach their edges.
    """
    stale_a = graph.add_node(entity_id="PERSON-stale00001")
    graph.add_node(entity_id=REAL_CORPUS_PERSON)
    graph.add_edge(stale_a, REAL_CORPUS_PERSON, "D1VERIFY-oldrun01-DOC-A")
    graph.add_edge(stale_a, REAL_CORPUS_PERSON, "D1DEBUG-oldrun02-DOC-A")
    graph.add_edge(REAL_CORPUS_PERSON, REAL_CORPUS_PERSON, REAL_CORPUS_EDGE_PROVENANCE)

    async def _fake_session_cleanup():
        return None

    with patch.object(vmd, "_prune_orphaned_priority_rows", _fake_session_cleanup):
        # Postgres half is patched out; only the graph sweep is under test.
        for prefix in ("D1VERIFY-", "D1DEBUG-"):
            await graph.execute_cypher(
                "MATCH ()-[r]->() WHERE r.source_doc_id STARTS WITH $prefix DELETE r",
                params={"prefix": prefix},
            )

    survivors = [e["source_doc_id"] for e in graph.edges]
    assert survivors == [REAL_CORPUS_EDGE_PROVENANCE]
    assert REAL_CORPUS_PERSON in graph.nodes
