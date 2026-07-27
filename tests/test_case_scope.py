"""
Tests for src/graph/case_scope.py (Phase 2 — AGE case-scoping chokepoint).

age_client is monkeypatched with a fake — no real Postgres/AGE. See that
module's own docstring for why only a subset of the codebase's Cypher
templates are routed through this chokepoint (the rest are deliberately
cross-case by design, not oversights).
"""
import pytest

import src.graph.case_scope as case_scope


class FakeAgeClient:
    def __init__(self, rows=None):
        self.rows = rows if rows is not None else []
        self.calls: list[dict] = []

    async def execute_cypher(self, cypher_query, params=None, columns=("result",), graph=None):
        self.calls.append({"cypher": cypher_query, "params": params or {}, "columns": columns})
        return self.rows


@pytest.fixture
def fake_age(monkeypatch):
    client = FakeAgeClient()
    monkeypatch.setattr(case_scope, "age_client", client)
    return client


@pytest.mark.asyncio
async def test_scoped_cypher_rejects_empty_case_id(fake_age):
    with pytest.raises(ValueError, match="non-empty case_id"):
        await case_scope.scoped_cypher("MATCH (n) WHERE n.case_id = $case_id RETURN n", "")


@pytest.mark.asyncio
async def test_scoped_cypher_rejects_none_case_id(fake_age):
    with pytest.raises(ValueError, match="non-empty case_id"):
        await case_scope.scoped_cypher("MATCH (n) WHERE n.case_id = $case_id RETURN n", None)


@pytest.mark.asyncio
async def test_scoped_cypher_rejects_template_missing_case_id_placeholder(fake_age):
    """
    The core hygiene property this module exists for: a template that
    doesn't reference $case_id at all fails loudly instead of silently
    running unscoped.
    """
    with pytest.raises(ValueError, match=r"\$case_id"):
        await case_scope.scoped_cypher("MATCH (n:Person) RETURN n", "CASE-001")
    assert fake_age.calls == [], "execute_cypher must never be reached for a template with no $case_id"


@pytest.mark.asyncio
async def test_scoped_cypher_rejects_params_already_containing_case_id(fake_age):
    with pytest.raises(ValueError, match="case_id"):
        await case_scope.scoped_cypher(
            "MATCH (n)-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id}) RETURN n",
            "CASE-001",
            params={"case_id": "CASE-999"},
        )


@pytest.mark.asyncio
async def test_scoped_cypher_merges_case_id_into_params_and_delegates(fake_age):
    fake_age.rows = [{"n": {"id": 1}}]
    result = await case_scope.scoped_cypher(
        "MATCH (n)-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id}) WHERE n.entity_id = $entity_id RETURN n",
        "CASE-001",
        params={"entity_id": "P-001"},
        columns=["n"],
    )
    assert result == [{"n": {"id": 1}}]
    assert len(fake_age.calls) == 1
    assert fake_age.calls[0]["params"] == {"entity_id": "P-001", "case_id": "CASE-001"}
    assert fake_age.calls[0]["columns"] == ["n"]
