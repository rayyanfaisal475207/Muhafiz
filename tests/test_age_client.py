"""
Tests for src/graph/age_client.py (Phase 4.7).

No real Postgres/AGE connection — asyncpg.create_pool is monkeypatched
with a fake pool/connection that records calls, matching the `no_network`
guard (conftest, autouse). Live-connection behavior (the AGE-specific
quirks documented in age_client.py's module docstring — statement caching,
the ::agtype cast, per-call LOAD/SET) was verified empirically against a
real AGE-enabled Postgres instance during development, not re-asserted
here; these tests guard the wrapper's own logic (SQL shape, param
encoding, agtype parsing, pool lifecycle).
"""
import json

import pytest

import src.graph.age_client as age_client


class FakeConnection:
    def __init__(self, fetch_result=None):
        self.fetch_result = fetch_result or []
        self.executed: list[str] = []
        self.fetch_calls: list[tuple] = []

    async def execute(self, sql, *args):
        self.executed.append(sql)

    async def fetch(self, sql, *args):
        self.fetch_calls.append((sql, args))
        return self.fetch_result


class FakeAcquireContext:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class FakePool:
    def __init__(self, conn):
        self._conn = conn
        self.closed = False

    def acquire(self):
        return FakeAcquireContext(self._conn)

    async def close(self):
        self.closed = True


class FakeRecord(dict):
    """asyncpg Record supports dict-style __getitem__ by column name."""
    pass


@pytest.fixture(autouse=True)
def reset_pool():
    age_client._pool = None
    yield
    age_client._pool = None


@pytest.fixture
def fake_conn_and_pool(monkeypatch):
    conn = FakeConnection()
    pool = FakePool(conn)

    async def fake_create_pool(*args, **kwargs):
        return pool

    monkeypatch.setattr(age_client.asyncpg, "create_pool", fake_create_pool)
    monkeypatch.setattr(age_client.config, "DATABASE_URL", "postgresql+asyncpg://u:p@localhost/db")
    return conn, pool


# ── SQL shape / LOAD-SET ordering ──────────────────────────────────────

@pytest.mark.asyncio
async def test_execute_cypher_runs_load_age_before_fetch(fake_conn_and_pool):
    conn, pool = fake_conn_and_pool
    await age_client.execute_cypher("MATCH (n) RETURN n", columns=["n"])

    assert any("LOAD 'age'" in stmt for stmt in conn.executed)
    assert any("search_path" in stmt for stmt in conn.executed)


@pytest.mark.asyncio
async def test_cypher_query_and_graph_embedded_as_literals_not_bind_params(fake_conn_and_pool):
    conn, pool = fake_conn_and_pool
    await age_client.execute_cypher(
        "MATCH (p:Person {case_id: $case_id}) RETURN p",
        params={"case_id": "CASE-001"},
        columns=["p"],
        graph="evidence_graph",
    )

    sql, args = conn.fetch_calls[0]
    assert "cypher('evidence_graph'" in sql
    assert "MATCH (p:Person {case_id: $case_id}) RETURN p" in sql
    assert "$1::agtype" in sql
    # Only the params JSON is a real bind argument — matches the module's
    # documented injection-safety contract (graph/query are never $n).
    assert len(args) == 1
    assert json.loads(args[0]) == {"case_id": "CASE-001"}


@pytest.mark.asyncio
async def test_missing_params_defaults_to_empty_object(fake_conn_and_pool):
    conn, pool = fake_conn_and_pool
    await age_client.execute_cypher("MATCH (n) RETURN n", columns=["n"])

    _, args = conn.fetch_calls[0]
    assert json.loads(args[0]) == {}


@pytest.mark.asyncio
async def test_multi_column_declaration(fake_conn_and_pool):
    conn, pool = fake_conn_and_pool
    await age_client.execute_cypher(
        "MATCH (p)-[r]->(d) RETURN p, r, d", columns=["p", "r", "d"]
    )
    sql, _ = conn.fetch_calls[0]
    assert "AS (p agtype, r agtype, d agtype)" in sql


# ── agtype result parsing ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_vertex_result_parsed_and_suffix_stripped(fake_conn_and_pool):
    conn, pool = fake_conn_and_pool
    conn.fetch_result = [
        FakeRecord(n='{"id": 1, "label": "Person", "properties": {"canonical_name": "X"}}::vertex')
    ]
    result = await age_client.execute_cypher("MATCH (n) RETURN n", columns=["n"])

    assert result == [{"n": {"id": 1, "label": "Person", "properties": {"canonical_name": "X"}}}]


@pytest.mark.asyncio
async def test_edge_result_parsed():
    parsed = age_client._parse_agtype('{"id": 2, "label": "BELONGS_TO_CASE"}::edge')
    assert parsed == {"id": 2, "label": "BELONGS_TO_CASE"}


@pytest.mark.asyncio
async def test_scalar_agtype_without_suffix():
    assert age_client._parse_agtype('{"count": 3}') == {"count": 3}


@pytest.mark.asyncio
async def test_none_agtype_stays_none():
    assert age_client._parse_agtype(None) is None


@pytest.mark.asyncio
async def test_unparseable_agtype_returned_raw():
    assert age_client._parse_agtype("not json") == "not json"


# ── Pool lifecycle ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_get_pool_is_a_singleton(fake_conn_and_pool):
    p1 = await age_client.get_pool()
    p2 = await age_client.get_pool()
    assert p1 is p2


@pytest.mark.asyncio
async def test_close_pool_resets_singleton(fake_conn_and_pool):
    conn, pool = fake_conn_and_pool
    await age_client.get_pool()
    await age_client.close_pool()
    assert pool.closed
    assert age_client._pool is None


@pytest.mark.asyncio
async def test_missing_database_url_raises(monkeypatch):
    monkeypatch.setattr(age_client.config, "DATABASE_URL", "")
    with pytest.raises(RuntimeError):
        await age_client.get_pool()
