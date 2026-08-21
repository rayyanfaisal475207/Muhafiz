"""
Tests for src/retrieval/fulltext_index.py (Graph Scale & Schema Expansion,
Milestone A2).

get_session is monkeypatched with a fake session — no real Postgres
(matches the `no_network` guard, conftest, autouse). Live behavior (the
real upsert/candidate-pool round trip against Postgres, and the query
latency improvement) was verified against a real Postgres instance during
development — see docs/decisions/0002-graph-schema-expansion-and-scale.md.
"""
import pytest

import src.retrieval.fulltext_index as fulltext_index


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _Row:
    """Minimal stand-in for a SQLAlchemy Row — attribute access by column name."""
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class _FakeSession:
    def __init__(self, result_rows=None, raise_on_execute=False):
        self.executed: list[tuple] = []
        self._result_rows = result_rows or []
        self._raise = raise_on_execute

    async def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        if self._raise:
            raise RuntimeError("simulated Postgres failure")
        return _FakeResult(self._result_rows)

    async def commit(self):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _fake_get_session(session):
    def _factory():
        return session
    return _factory


# ── maintain() ──────────────────────────────────────────────────────────

async def test_maintain_skips_empty_text(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(fulltext_index, "get_session", _fake_get_session(session))

    await fulltext_index.maintain("c1", "   ", {"doc_id": "D-1"})

    assert session.executed == []


async def test_maintain_upserts_with_tokenized_tsvector_input(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(fulltext_index, "get_session", _fake_get_session(session))

    await fulltext_index.maintain(
        "c1", "Theft of movable property.",
        {"doc_id": "D-1", "source": "PPC.pdf", "project_id": "PROJ-1", "case_id": "CASE-1", "is_global": False},
    )

    assert len(session.executed) == 1
    sql, params = session.executed[0]
    assert "INSERT INTO chunk_fulltext" in sql
    assert "ON CONFLICT" in sql
    assert params["chunk_id"] == "c1"
    assert params["doc_id"] == "D-1"
    assert params["source"] == "PPC.pdf"
    assert params["project_id"] == "PROJ-1"
    assert params["case_id"] == "CASE-1"
    assert params["is_global"] is False
    assert params["raw_text"] == "Theft of movable property."
    # tokenized_text feeds to_tsvector — must be the SAME tokenizer BM25
    # itself uses, not raw text or Postgres's own tokenization.
    assert params["tokenized_text"] == " ".join(["theft", "of", "movable", "property"])
    import json
    assert json.loads(params["metadata_json"])["source"] == "PPC.pdf"


async def test_maintain_swallows_db_failure_without_raising(monkeypatch):
    session = _FakeSession(raise_on_execute=True)
    monkeypatch.setattr(fulltext_index, "get_session", _fake_get_session(session))

    await fulltext_index.maintain("c1", "some text", {"doc_id": "D-1"})  # must not raise


# ── delete_by_ids() / delete_by_source() ─────────────────────────────────

async def test_delete_by_ids_no_op_on_empty_list(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(fulltext_index, "get_session", _fake_get_session(session))

    await fulltext_index.delete_by_ids([])

    assert session.executed == []


async def test_delete_by_ids_issues_delete(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(fulltext_index, "get_session", _fake_get_session(session))

    await fulltext_index.delete_by_ids(["c1", "c2"])

    sql, params = session.executed[0]
    assert "DELETE FROM chunk_fulltext" in sql
    assert params["chunk_ids"] == ["c1", "c2"]


async def test_delete_by_source_swallows_failure(monkeypatch):
    session = _FakeSession(raise_on_execute=True)
    monkeypatch.setattr(fulltext_index, "get_session", _fake_get_session(session))

    await fulltext_index.delete_by_source("doc.pdf")  # must not raise


# ── candidate_pool() ─────────────────────────────────────────────────────

async def test_candidate_pool_empty_query_returns_empty_without_a_db_call(monkeypatch):
    session = _FakeSession()
    monkeypatch.setattr(fulltext_index, "get_session", _fake_get_session(session))

    result = await fulltext_index.candidate_pool("   ", where=None)

    assert result == []
    assert session.executed == []


async def test_candidate_pool_returns_id_text_metadata(monkeypatch):
    rows = [_Row(chunk_id="c1", text="Theft of movable property.", metadata={"source": "PPC.pdf", "record_date": "2026-01-01"})]
    session = _FakeSession(result_rows=rows)
    monkeypatch.setattr(fulltext_index, "get_session", _fake_get_session(session))

    result = await fulltext_index.candidate_pool("theft property", where=None)

    assert result == [{"id": "c1", "text": "Theft of movable property.",
                        "metadata": {"source": "PPC.pdf", "record_date": "2026-01-01"}}]


async def test_candidate_pool_decodes_metadata_when_returned_as_json_string(monkeypatch):
    """Some driver/version combinations return a raw JSONB column as text
    rather than an already-decoded dict from a plain text() query — both
    must work, since this is exactly the shape uncertainty a raw SQL
    SELECT (rather than an ORM-typed column) has."""
    import json
    rows = [_Row(chunk_id="c1", text="x", metadata=json.dumps({"source": "PPC.pdf"}))]
    session = _FakeSession(result_rows=rows)
    monkeypatch.setattr(fulltext_index, "get_session", _fake_get_session(session))

    result = await fulltext_index.candidate_pool("x", where=None)

    assert result[0]["metadata"] == {"source": "PPC.pdf"}


async def test_candidate_pool_builds_or_tsquery_across_tokens(monkeypatch):
    session = _FakeSession(result_rows=[])
    monkeypatch.setattr(fulltext_index, "get_session", _fake_get_session(session))

    await fulltext_index.candidate_pool("theft property", where=None)

    _, params = session.executed[0]
    assert params["tsquery"] == "theft | property"


async def test_candidate_pool_scopes_by_project_or_global(monkeypatch):
    session = _FakeSession(result_rows=[])
    monkeypatch.setattr(fulltext_index, "get_session", _fake_get_session(session))

    await fulltext_index.candidate_pool("theft", where={"project_id": "PROJ-1"})

    sql, params = session.executed[0]
    assert "project_id = :project_id OR is_global = TRUE" in sql
    assert params["project_id"] == "PROJ-1"


async def test_candidate_pool_scopes_by_case_id(monkeypatch):
    session = _FakeSession(result_rows=[])
    monkeypatch.setattr(fulltext_index, "get_session", _fake_get_session(session))

    await fulltext_index.candidate_pool("theft", where={"case_id": "CASE-1"})

    sql, params = session.executed[0]
    assert "case_id = :case_id" in sql
    assert params["case_id"] == "CASE-1"


async def test_candidate_pool_no_filter_scans_no_scope_restriction(monkeypatch):
    session = _FakeSession(result_rows=[])
    monkeypatch.setattr(fulltext_index, "get_session", _fake_get_session(session))

    await fulltext_index.candidate_pool("theft", where=None)

    sql, _ = session.executed[0]
    assert "WHERE TRUE AND tsv @@" in sql


async def test_candidate_pool_failure_returns_empty_list_not_raise(monkeypatch):
    session = _FakeSession(raise_on_execute=True)
    monkeypatch.setattr(fulltext_index, "get_session", _fake_get_session(session))

    result = await fulltext_index.candidate_pool("theft", where=None)

    assert result == []
