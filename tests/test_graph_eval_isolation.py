"""
Tests for Phase 3, Module 3.1 (eval/production Apache AGE graph isolation).

No live Postgres/AGE instance is available in this environment (see
MANUAL_RLS_VERIFICATION.md's equivalent caveat for Phase 2) — these tests
mock src.graph.age_client.execute_cypher and assert every call records
the `graph` argument it was actually given. They prove the parameter
threading is wired correctly end-to-end (versioning.py ->
entity_resolution.py -> scripts/eval_entity_resolution.py), not that a
real evidence_graph_eval graph behaves correctly under real AGE — that
half needs a live instance and is left to a manual verification
procedure, same pattern as Phase 2's RLS work.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.graph import age_client, case_scope, entity_resolution as er, versioning

EVAL_GRAPH = "evidence_graph_eval"


class RecordingCypher:
    """
    Fake age_client.execute_cypher: records every call's `graph` argument
    (plus enough of the query shape to fake a plausible response) without
    needing a real AGE instance.

    Dispatch is deliberately crude (MERGE -> write_node response, a
    from/to CREATE -> write_edge response, everything else -> no rows) —
    this suite is testing parameter propagation, not resolution logic.
    """

    def __init__(self):
        self.calls: list[dict] = []
        self._next_id = 1

    async def __call__(self, cypher_query, params=None, columns=("result",), graph=age_client.GRAPH_NAME):
        self.calls.append({
            "cypher": cypher_query, "params": dict(params or {}),
            "columns": tuple(columns), "graph": graph,
        })
        text = cypher_query.strip()
        if text.startswith("MERGE"):
            node_id = self._next_id
            self._next_id += 1
            return [{"n": {"id": node_id, "label": "Fake", "properties": {}}}]
        if "CREATE (a)-[r:" in text:
            edge_id = self._next_id
            self._next_id += 1
            return [{"r": {"id": edge_id, "label": "Fake", "properties": {}}}]
        if text.startswith("MATCH ()-[old]->()"):
            # write_edge's post-supersede "mark old edge" update
            return [{"old": {"id": params.get("old_id"), "properties": {}}}]
        if text.startswith("MATCH ()-[r]->()"):
            # get_edge: return a plausible unlocked, not-yet-superseded
            # prior edge so write_edge's supersedes_edge_id path proceeds
            # instead of refusing the write.
            return [{"r": {"id": params.get("edge_id"), "properties": {}}}]
        # every other MATCH-only read (find_by_primary_id, fetch_all_nodes,
        # shares_case): no candidates found
        return []

    def graphs_used(self) -> set[str]:
        return {c["graph"] for c in self.calls}


@pytest.fixture
def fake_cypher(monkeypatch):
    fake = RecordingCypher()
    monkeypatch.setattr(age_client, "execute_cypher", fake)
    mock_gateway = AsyncMock()
    monkeypatch.setattr(versioning, "get_gateway", AsyncMock(return_value=mock_gateway))
    return fake


# ── versioning.py ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_write_node_default_graph(fake_cypher):
    await versioning.write_node("Person", {"entity_id": "P-1"}, {}, source_doc_id="D-1")
    assert fake_cypher.calls[-1]["graph"] == age_client.GRAPH_NAME


@pytest.mark.asyncio
async def test_write_node_explicit_graph(fake_cypher):
    await versioning.write_node("Person", {"entity_id": "P-1"}, {}, source_doc_id="D-1", graph=EVAL_GRAPH)
    assert fake_cypher.calls[-1]["graph"] == EVAL_GRAPH


@pytest.mark.asyncio
async def test_write_edge_explicit_graph(fake_cypher):
    edge = await versioning.write_edge(
        "BELONGS_TO_CASE", "Person", {"entity_id": "P-1"}, "Case", {"case_id": "CASE-1"},
        {}, source_doc_id="D-1", confidence=1.0, graph=EVAL_GRAPH,
    )
    assert edge is not None
    assert fake_cypher.calls[-1]["graph"] == EVAL_GRAPH


@pytest.mark.asyncio
async def test_write_edge_supersede_uses_same_graph_for_get_edge_and_update(fake_cypher):
    calls_before = len(fake_cypher.calls)
    edge = await versioning.write_edge(
        "OCCURRED_ON", "Person", {"entity_id": "P-1"}, "Date", {"date": "2026-01-01"},
        {}, source_doc_id="D-1", supersedes_edge_id=42, graph=EVAL_GRAPH,
    )
    assert edge is not None

    new_calls = fake_cypher.calls[calls_before:]
    # Expect: get_edge(42) read, the CREATE write, and the old-edge update.
    assert len(new_calls) == 3, new_calls
    assert all(c["graph"] == EVAL_GRAPH for c in new_calls), new_calls


@pytest.mark.asyncio
async def test_get_edge_default_and_explicit_graph(fake_cypher):
    await versioning.get_edge(1)
    assert fake_cypher.calls[-1]["graph"] == age_client.GRAPH_NAME

    await versioning.get_edge(1, graph=EVAL_GRAPH)
    assert fake_cypher.calls[-1]["graph"] == EVAL_GRAPH


# ── case_scope.py ────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_scoped_cypher_default_and_explicit_graph(fake_cypher):
    await case_scope.scoped_cypher(
        "MATCH (n {case_id: $case_id}) RETURN n", "CASE-1", columns=["n"],
    )
    assert fake_cypher.calls[-1]["graph"] == age_client.GRAPH_NAME

    await case_scope.scoped_cypher(
        "MATCH (n {case_id: $case_id}) RETURN n", "CASE-1", columns=["n"], graph=EVAL_GRAPH,
    )
    assert fake_cypher.calls[-1]["graph"] == EVAL_GRAPH


# ── entity_resolution.py: resolve_and_write must keep reads and writes ──
# ── on the SAME graph, not just the writes (the gap flagged during     ──
# ── Module 3.1 implementation — see PR description / report).          ──

@pytest.mark.asyncio
async def test_resolve_and_write_default_graph_touches_only_production(fake_cypher):
    await er.resolve_and_write("person", {"canonical_name": "Ahmed Raza"}, "CASE-1", "D-1")
    assert fake_cypher.graphs_used() == {age_client.GRAPH_NAME}


@pytest.mark.asyncio
async def test_resolve_and_write_eval_graph_touches_only_eval_reads_and_writes(fake_cypher):
    """
    The critical assertion for Module 3.1: EVERY query resolve_and_write
    triggers — both the candidate-lookup READS (_find_by_primary_id /
    _fetch_all_nodes via resolve_mention) and the versioning.py WRITES —
    must land on evidence_graph_eval, not a mix of the two.
    """
    await er.resolve_and_write(
        "person", {"canonical_name": "Ahmed Raza"}, "CASE-1", "D-1", graph=EVAL_GRAPH,
    )
    assert len(fake_cypher.calls) >= 3, "expected at least a read + node write + edge write"
    assert fake_cypher.graphs_used() == {EVAL_GRAPH}, fake_cypher.calls


# ── scripts/eval_entity_resolution.py ────────────────────────────────────

def test_eval_graph_guard_passes_for_real_eval_graph():
    import scripts.eval_entity_resolution as eval_script
    assert "eval" in eval_script.EVAL_GRAPH
    eval_script._assert_eval_graph()  # must not raise


def test_eval_graph_guard_raises_if_pointed_at_production(monkeypatch):
    import scripts.eval_entity_resolution as eval_script
    monkeypatch.setattr(eval_script, "EVAL_GRAPH", "evidence_graph")
    with pytest.raises(RuntimeError, match="Refusing to run"):
        eval_script._assert_eval_graph()


@pytest.mark.asyncio
async def test_eval_script_resolve_roster_never_touches_production_graph(fake_cypher):
    """
    A tiny one-row roster run through the real resolve_roster() — every
    graph call it triggers (Case pre-create, Document write, the
    BELONGS_TO_CASE edge, and resolve_and_write's own reads/writes) must
    land on evidence_graph_eval, never the default evidence_graph.
    """
    import scripts.eval_entity_resolution as eval_script

    rows = [{
        "entity_id": "P-TEST-1",
        "type": "person",
        "attrs": {},
        "case_id_list": ["CASE-EVAL-TEST"],
        "cnic_shown_in_list": [],
        "surface_variant_list": ["Test Person"],
    }]

    await eval_script.resolve_roster(rows)

    assert fake_cypher.calls, "expected resolve_roster to issue graph queries"
    assert fake_cypher.graphs_used() == {EVAL_GRAPH}, (
        f"resolve_roster touched graph(s) {fake_cypher.graphs_used()}, "
        f"expected only {{{EVAL_GRAPH!r}}} — a call site is missing graph=EVAL_GRAPH"
    )
