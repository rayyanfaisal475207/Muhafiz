# ============================================================
# Apache AGE client — thin wrapper over asyncpg issuing parameterized
# Cypher via AGE's cypher() SQL function (Phase 4.7).
#
# ── Connection lifecycle (see docs/graph_schema.md) ──
# AGE requires `LOAD 'age'; SET search_path = ag_catalog, "$user", public;`
# active for the session a cypher() call runs on. This module owns its OWN
# dedicated asyncpg connection pool — separate from
# src/database/postgres.py's SQLAlchemy engine used for every relational
# table — since a graph query and a relational query have no business
# sharing a connection/transaction here.
#
# LOAD/SET are re-run at the START OF EVERY execute_cypher() CALL, on
# whichever connection the pool happens to hand back — not once via
# asyncpg's `init=` pool callback. That was the first design (documented
# in an earlier revision of this comment) and it is wrong in practice, not
# just in theory: empirically, a pool with more than one physical
# connection intermittently fails a cypher() call on a connection that
# *was* `init`-ed with LOAD/SET at creation time, with errors ranging from
# `function cypher(unknown, unknown, unknown) does not exist` to `type
# "agtype" does not exist` — AGE's catalog/type registration for a session
# is evidently not something that reliably survives being set up once and
# left alone across a pooled connection's later reuse. Re-asserting both
# statements immediately before every query, on the connection actually in
# hand, closed every failure mode reproduced during development. Two extra
# no-op statements per call is a trivial cost next to a graph write
# silently failing.
#
# ── Parameterization and injection safety ──
# AGE's cypher() function signature is effectively
#     cypher(graph_name name, query_string cstring, params agtype)
# `query_string` is a `cstring` — a Postgres pseudo-type that cannot be
# sent as a wire-protocol bind parameter by any client driver, asyncpg
# included. This is a real AGE limitation, not a design choice here: the
# graph name and Cypher query TEXT are therefore always Python string
# constants written in this codebase (e.g. entity_resolution.py's fixed
# Cypher templates) — never built by concatenating request/user input.
# The one argument that IS a real bindable Postgres type is `params`
# (agtype), and it is ALWAYS how a caller passes a value that varies per
# call (a case_id, an entity name, a CNIC) — referenced inside the Cypher
# template as `$paramname`, never string-interpolated into the template
# itself. This is what makes Phase 7's case-scope injection into
# traversals safe: the case filter becomes another bound `params` entry,
# not a rebuilt query string.
# ============================================================

from __future__ import annotations

import json
import logging
from typing import Any, Optional, Sequence

import asyncpg

from src import config

logger = logging.getLogger(__name__)

GRAPH_NAME = "evidence_graph"

_pool: Optional[asyncpg.Pool] = None


def _dsn() -> str:
    """asyncpg wants a plain postgresql:// URL, not SQLAlchemy's +asyncpg form."""
    raw = config.DATABASE_URL or ""
    return raw.replace("postgresql+asyncpg://", "postgresql://")


async def _load_age(conn: asyncpg.Connection) -> None:
    """Re-asserted at the top of every execute_cypher() call — see module docstring."""
    await conn.execute("LOAD 'age'")
    await conn.execute('SET search_path = ag_catalog, "$user", public')


async def get_pool() -> asyncpg.Pool:
    """Process-wide singleton pool, created lazily on first use."""
    global _pool
    if _pool is None:
        dsn = _dsn()
        if not dsn:
            raise RuntimeError(
                "DATABASE_URL is not configured — Apache AGE requires the same "
                "Postgres instance the relational tables use."
            )
        _pool = await asyncpg.create_pool(
            dsn, min_size=1, max_size=10,
            # AGE's cypher() function hooks into Postgres's planner in a
            # way that breaks under asyncpg's default statement caching —
            # confirmed empirically: two DIFFERENT cypher() calls on the
            # same cached-statement connection, the second fails with
            # "function cypher(unknown, unknown, unknown) does not exist"
            # even though the same query works fine in isolation.
            # statement_cache_size=0 forces asyncpg to always use an
            # unnamed statement (no server-side plan reuse across calls),
            # which avoids it. This is an AGE-specific workaround, not a
            # general asyncpg tuning choice — the relational engine
            # (src/database/postgres.py) is untouched and keeps its
            # normal caching.
            statement_cache_size=0,
        )
    return _pool


async def close_pool() -> None:
    """Test/shutdown-only: close and drop the singleton so the next get_pool() reopens it."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _parse_agtype(raw: Any) -> Any:
    """
    AGE returns `agtype` columns to asyncpg as plain text — a JSON value
    optionally suffixed with `::vertex` / `::edge` / `::path`. Strips the
    suffix (if present) and parses the JSON; a vertex/edge's dict shape
    (`id`/`label`/`properties`) is returned as-is rather than further
    unpacked, since callers generally want to know which kind of AGE
    object they got.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        return raw
    for suffix in ("::vertex", "::edge", "::path"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]
            break
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return raw


async def execute_cypher(
    cypher_query: str,
    params: Optional[dict] = None,
    columns: Sequence[str] = ("result",),
    graph: str = GRAPH_NAME,
) -> list[dict]:
    """
    Execute a parameterized Cypher query against `graph` via AGE's
    cypher() SQL function.

    Args:
        cypher_query: A literal Cypher template — written in this
            codebase, never built from request/user input. Reference
            varying values as `$paramname` inside it.
        params: Bound values for every `$paramname` the template
            references — the ONLY safe place a caller-supplied value
            belongs (see module docstring).
        columns: Names of every column the query's RETURN clause
            produces, in order. AGE requires the SQL-level output shape
            declared statically (`AS (col1 agtype, col2 agtype, ...)`) —
            it cannot be inferred from the Cypher text at runtime the way
            an ordinary SQL SELECT's shape can.
        graph: Graph name — defaults to the one graph this build uses.

    Returns:
        One dict per result row, keyed by `columns`, with each agtype
        value already parsed into a plain Python value (see
        `_parse_agtype`).
    """
    # graph/cypher_query/columns are embedded directly into the SQL text
    # (not bound as $n parameters) — confirmed empirically, not assumed:
    # AGE's cypher() declares query_string as `cstring`, a Postgres
    # pseudo-type asyncpg (and libpq's extended query protocol generally)
    # cannot bind as a wire parameter — `SELECT * FROM cypher($1, $2, $3)`
    # fails with "a name constant is expected" before it ever reaches AGE.
    # This is safe ONLY because all three are always trusted constants
    # from this codebase (see module docstring) — `params` below is the
    # one argument a caller-supplied value is ever allowed to flow through.
    # $1 is explicitly cast to ::agtype — without it, PostgreSQL's
    # untyped-literal inference for cypher()'s third argument is unreliable
    # on a connection that hasn't just done a cypher() call (reproduced
    # empirically: omitting the cast intermittently raises `function
    # cypher(unknown, unknown, unknown) does not exist` even though graph/
    # cypher_query are ordinary string literals, not parameters).
    col_list = ", ".join(f"{c} agtype" for c in columns)
    sql = f"""
        SELECT * FROM cypher('{graph}', $cypher${cypher_query}$cypher$, $1::agtype) AS ({col_list})
    """
    pool = await get_pool()
    async with pool.acquire() as conn:
        await _load_age(conn)
        rows = await conn.fetch(sql, json.dumps(params or {}))

    return [
        {col: _parse_agtype(row[col]) for col in columns}
        for row in rows
    ]
