"""
Phase 1, Module 1.2 — MCP least-privilege + injection cleanup.

Guards:
  * The admin mcp-demo route builds its SQL through SQLAlchemy's own
    .ilike()/literal-bind renderer, not hand-rolled string concatenation
    with manual quote-doubling — a single quote in user-influenced input
    must come out escaped, never break out of its string literal.
  * src/mcp/client.py's execute_query() requires MCP_DATABASE_URL (the
    least-privilege role from migrations/009_mcp_readonly_role.sql) and
    raises immediately if it's unset — no superuser DATABASE_URL fallback
    (removed once the role was verified end-to-end against a live
    Postgres instance, per the Phase 0-3 closeout's Task 1).

None of this requires a live Postgres/MCP process — the SQL-building test
is pure Python, and the URL-selection test intercepts stdio_client before
any real process is spawned.
"""
import pytest

import src.mcp.client as mcp_client
from src.api.admin import _build_police_reference_sql


# ── mcp_demo's SQL construction ──────────────────────────────────────────────

def test_no_filters_returns_unfiltered_select():
    sql = _build_police_reference_sql(None)
    assert "FROM police_reference_data" in sql
    assert "WHERE" not in sql


def test_empty_params_dict_returns_unfiltered_select():
    sql = _build_police_reference_sql({})
    assert "WHERE" not in sql


def test_filters_render_as_ilike_conditions():
    sql = _build_police_reference_sql({"category": "theft", "subject": "fraud", "section_ref": "379"})
    assert "police_reference_data.category ILIKE '%theft%'" in sql
    assert "police_reference_data.subject ILIKE '%fraud%'" in sql
    assert "police_reference_data.section_ref ILIKE '%379%'" in sql


def test_single_quote_in_input_is_escaped_not_concatenated_raw():
    """A category value containing a single quote and a statement terminator
    used to reach the query via `.replace("'", "''")` string concatenation —
    fragile, manual reimplementation of parameterization. This must instead
    go through SQLAlchemy's literal renderer, which escapes the quote by
    doubling it (SQL's standard escape) so it can never terminate the
    ILIKE string literal early."""
    payload = "theft'; DROP TABLE users; --"
    sql = _build_police_reference_sql({"category": payload})

    # The quote must be doubled (escaped), keeping the whole payload inside
    # the ILIKE string literal.
    assert "theft''; DROP TABLE users; --" in sql
    # And the literal must still be closed by the wildcard + closing quote
    # the helper itself adds — i.e. nothing broke out of the string early.
    assert "theft''; DROP TABLE users; --%'" in sql


def test_no_percent_doubling_artifact():
    """Regression guard: an earlier dialect choice caused SQLAlchemy to
    double every literal '%' (a printf-paramstyle escaping artifact,
    functionally harmless for LIKE but not what the rendered SQL should
    look like) — the wildcard must render as a single '%'."""
    sql = _build_police_reference_sql({"category": "theft"})
    assert "%%" not in sql


# ── execute_query()'s connection-string selection ────────────────────────────

class _StopEarly(Exception):
    """Raised by the fake stdio_client to short-circuit before spawning a
    real process — we only need to inspect what URL was about to be used."""


def _capture_stdio_client(captured):
    def _fake(server_params):
        captured["args"] = server_params.args
        raise _StopEarly()
    return _fake


async def test_execute_query_uses_mcp_database_url(monkeypatch):
    monkeypatch.setattr(mcp_client, "MCP_DATABASE_URL", "postgresql://muhafiz_mcp_readonly:pw@localhost:5432/muhafiz")

    captured = {}
    monkeypatch.setattr(mcp_client, "stdio_client", _capture_stdio_client(captured))

    with pytest.raises(_StopEarly):
        await mcp_client.execute_query("SELECT 1")

    assert captured["args"][-1] == "postgresql://muhafiz_mcp_readonly:pw@localhost:5432/muhafiz"


async def test_execute_query_raises_when_mcp_database_url_unset(monkeypatch):
    monkeypatch.setattr(mcp_client, "MCP_DATABASE_URL", "")

    captured = {}
    monkeypatch.setattr(mcp_client, "stdio_client", _capture_stdio_client(captured))

    with pytest.raises(RuntimeError, match="MCP_DATABASE_URL"):
        await mcp_client.execute_query("SELECT 1")

    # Must fail before ever spawning the stdio process.
    assert captured == {}
