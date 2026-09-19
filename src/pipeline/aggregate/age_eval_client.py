# -*- coding: utf-8 -*-
"""
Phase 5C — the fixed-target Apache AGE evaluator client.

WHY THIS MODULE EXISTS. `src/graph/age_client.py` takes a `graph=`
argument defaulting to production's `evidence_graph`, and resolves its DSN
from `DATABASE_URL`. That is correct for the application. It is wrong for
model-generated Cypher: AGE 1.5.0 provides no read-only boundary (Phase 5
measured `SET`, `REMOVE` and `DETACH DELETE` all succeeding under
SELECT-only grants AND `default_transaction_read_only=on`), so a generated
query that slips past the guard executes as a write against whatever graph
it was pointed at. Phase 5 established that empirically, destructively.

THE BOUNDARY IS THE CONNECTION, NOT THE QUERY. This client connects as
`muhafiz_age_eval_app` to the `muhafiz_age_eval` database. That role has no
CONNECT privilege on `muhafiz`, so a generated query cannot reach production
even if every other check in the stack fails — PostgreSQL refuses at
connection time, before any Cypher is parsed. Note that the cluster's
pg_hba uses `trust` on host lines: the evaluator's PASSWORD is therefore not
the boundary and must not be mistaken for one. The CONNECT denial is, and it
holds under trust auth (verified).

WHAT IS DELIBERATELY NOT IN THE PUBLIC API. No `graph=`, no `database=`,
no `database_url=`, no `role=`. The model controls the Cypher body and its
bound parameters; it controls nothing about where that Cypher runs. A
parameter that let it choose a target would reintroduce exactly the failure
this module exists to prevent, and "the guard would catch it" is not an
argument — the guard is defence in depth, not the boundary.

NO PRODUCTION FALLBACK. Every failure path here raises or refuses. There is
no branch that retries against `DATABASE_URL`, and there must never be one:
a fallback would convert an evaluator outage into a silent production write.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Optional, Sequence

import asyncpg

from src import config

logger = logging.getLogger(__name__)

#: The one graph this client will ever address. Not a parameter, not a
#: default — a module constant, so there is no call site that can change it.
EVAL_GRAPH_NAME = "evidence_graph_eval"

#: The database the evaluator DSN must name. Checked at pool creation so a
#: mis-set AGE_EVAL_DATABASE_URL fails closed instead of connecting
#: somewhere unintended.
EXPECTED_EVAL_DB_NAME = "muhafiz_age_eval"

#: The database this client must NEVER reach. Checked explicitly rather
#: than inferred, because the failure it guards against is the one that
#: already happened once.
FORBIDDEN_DB_NAME = "muhafiz"


class EvaluatorUnavailable(RuntimeError):
    """The evaluator cannot be used, for any reason.

    Raised rather than falling back. The AGE route turns this into a
    refusal; nothing in this codebase may turn it into a production query.
    """


_pool: Optional[asyncpg.Pool] = None


def _database_name_of(dsn: str) -> str:
    """Last path segment of a DSN, minus any query string."""
    if not dsn:
        return ""
    try:
        return dsn.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1]
    except (AttributeError, IndexError):
        return ""


def _eval_dsn() -> str:
    """The evaluator DSN, validated.

    Three checks, each guarding a distinct way this could go wrong:
      1. unset            -> the route is disabled, not defaulted
      2. names production -> a copy-paste of DATABASE_URL, refused loudly
      3. names something else -> a typo; refused rather than guessed at
    """
    raw = (config.AGE_EVAL_DATABASE_URL or "").strip()
    if not raw:
        raise EvaluatorUnavailable(
            "AGE_EVAL_DATABASE_URL is not configured. The independent AGE "
            "route is disabled. It does NOT fall back to DATABASE_URL — "
            "provision the evaluator database (see "
            "scripts/rebuild_age_eval.py) and set AGE_EVAL_DATABASE_URL."
        )

    dsn = raw.replace("postgresql+asyncpg://", "postgresql://")
    name = _database_name_of(dsn)

    if name == FORBIDDEN_DB_NAME:
        raise EvaluatorUnavailable(
            f"AGE_EVAL_DATABASE_URL names the production database "
            f"({FORBIDDEN_DB_NAME!r}). Refusing to run model-generated "
            f"Cypher against production. The evaluator must point at "
            f"{EXPECTED_EVAL_DB_NAME!r}."
        )
    if name != EXPECTED_EVAL_DB_NAME:
        raise EvaluatorUnavailable(
            f"AGE_EVAL_DATABASE_URL names database {name!r}, but the "
            f"evaluator target is fixed to {EXPECTED_EVAL_DB_NAME!r}. "
            f"Refusing to connect to an unrecognised target."
        )
    return dsn


async def get_eval_pool() -> asyncpg.Pool:
    """Process-wide evaluator pool, bounded and timeout-enforced.

    `statement_timeout` is set as a server setting rather than trusted to a
    client-side cancel: a generated query that plans badly over 13k edges
    should be killed by PostgreSQL itself, not left running while asyncpg
    waits. `command_timeout` is the client-side backstop for the case where
    the server never answers at all.
    """
    global _pool
    if _pool is None:
        dsn = _eval_dsn()
        timeout_ms = int(config.AGE_EVAL_STATEMENT_TIMEOUT_MS)
        _pool = await asyncpg.create_pool(
            dsn,
            min_size=1,
            max_size=int(config.AGE_EVAL_MAX_POOL_SIZE),
            # Same AGE-specific workaround as age_client: cypher() breaks
            # under asyncpg's prepared-statement cache.
            statement_cache_size=0,
            command_timeout=max(1.0, timeout_ms / 1000.0) + 5.0,
            server_settings={"statement_timeout": str(timeout_ms)},
        )
    return _pool


async def close_eval_pool() -> None:
    """Test/shutdown-only: close and drop the singleton."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


async def _prepare(conn: asyncpg.Connection) -> None:
    """Re-assert AGE session state, for the reasons age_client documents."""
    try:
        await conn.execute("LOAD 'age'")
    except asyncpg.InsufficientPrivilegeError:
        pass
    await conn.execute('SET search_path = ag_catalog, "$user", public')


def _parse_agtype(raw: Any) -> Any:
    """Identical semantics to age_client._parse_agtype."""
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


async def assert_isolated() -> dict:
    """Prove, at runtime, that this connection is not production.

    Cheap enough to run before a batch of evaluation queries, and it turns
    a misconfiguration into an exception at a known point rather than a
    surprise write somewhere else. Returns the identity it observed so a
    caller can record it in provenance.
    """
    pool = await get_eval_pool()
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT current_database() AS db, current_user AS usr"
        )
    db, usr = row["db"], row["usr"]
    if db != EXPECTED_EVAL_DB_NAME:
        raise EvaluatorUnavailable(
            f"Evaluator connection resolved to database {db!r}, expected "
            f"{EXPECTED_EVAL_DB_NAME!r}. Refusing to proceed."
        )
    return {"database": db, "user": usr, "graph": EVAL_GRAPH_NAME}


async def execute_eval_cypher(
    cypher: str,
    params: Optional[dict] = None,
    columns: Sequence[str] = ("result",),
) -> list[dict]:
    """Run Cypher against the evaluator graph. Fixed target, bounded result.

    Args:
        cypher: the query body. Guarded by `cypher_guard` BEFORE it gets
            here — this function does not re-guard, and does not repair.
        params: bound values, the only channel caller data may use.
        columns: RETURN aliases, in order (AGE cannot infer output shape).

    Note the absent arguments: no graph, no database, no role. That is the
    contract, not an oversight.

    Raises:
        EvaluatorUnavailable: misconfigured, unreachable, or not the
            expected database. Never falls back to production.
    """
    if not isinstance(params, (dict, type(None))):
        raise EvaluatorUnavailable("Evaluator params must be a mapping.")

    col_list = ", ".join(f"{c} agtype" for c in columns)
    max_rows = int(config.AGE_EVAL_MAX_ROWS)

    # The graph name is this module's constant, never an argument; the
    # query text is interpolated for the same unavoidable reason
    # age_client documents (cypher()'s cstring cannot be bound). The
    # difference is where it runs: a disposable database whose role cannot
    # reach production.
    sql = f"""
        SELECT * FROM cypher('{EVAL_GRAPH_NAME}', $cypher${cypher}$cypher$, $1::agtype)
        AS ({col_list})
        LIMIT {max_rows + 1}
    """

    try:
        pool = await get_eval_pool()
        async with pool.acquire() as conn:
            await _prepare(conn)
            rows = await conn.fetch(sql, json.dumps(params or {}))
    except EvaluatorUnavailable:
        raise
    except (asyncpg.PostgresError, OSError) as exc:
        # Connection refused, database absent, permission denied, timeout.
        # All of them mean "no evaluator", none of them mean "use
        # production".
        raise EvaluatorUnavailable(
            f"Evaluator query failed ({type(exc).__name__}): {exc}"
        ) from exc

    if len(rows) > max_rows:
        raise EvaluatorUnavailable(
            f"Evaluator query returned more than AGE_EVAL_MAX_ROWS "
            f"({max_rows}) rows. Refusing to materialise an unbounded "
            f"result; narrow the aggregate."
        )

    return [{col: _parse_agtype(row[col]) for col in columns} for row in rows]


# ══════════════════════════════════════════════════════════════════════
# SNAPSHOT IDENTITY
# ══════════════════════════════════════════════════════════════════════

#: Where the rebuild script records what it copied. Plain relational table
#: in the evaluator database — deliberately not a graph node, so reading it
#: never depends on AGE working.
SNAPSHOT_TABLE = "public.age_eval_snapshot"


async def read_snapshot() -> Optional[dict]:
    """The current evaluator snapshot record, or None if never built.

    A missing or unverified snapshot is not an error here — it is a fact
    the caller must act on (the AGE route refuses; reconciliation marks the
    result not-comparable).
    """
    try:
        pool = await get_eval_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow(
                f"""
                SELECT snapshot_id, created_at, source_database, source_graph,
                       case_count, person_count, same_as_count,
                       total_vertices, total_edges, verified
                FROM {SNAPSHOT_TABLE}
                ORDER BY created_at DESC
                LIMIT 1
                """
            )
    except EvaluatorUnavailable:
        raise
    except asyncpg.UndefinedTableError:
        return None
    except (asyncpg.PostgresError, OSError) as exc:
        raise EvaluatorUnavailable(
            f"Could not read evaluator snapshot metadata: {exc}"
        ) from exc

    if row is None:
        return None
    out = dict(row)
    ts = out.get("created_at")
    out["created_at"] = ts.isoformat() if hasattr(ts, "isoformat") else str(ts)
    return out


async def require_verified_snapshot() -> dict:
    """The snapshot, or EvaluatorUnavailable if it is missing/unverified.

    This is what makes "the evaluator has no valid snapshot" a refusal
    rather than a silently-stale number. An unverified snapshot is worse
    than none: it looks like data.
    """
    snap = await read_snapshot()
    if snap is None:
        raise EvaluatorUnavailable(
            "The evaluator graph has no snapshot record. Run "
            "`python scripts/rebuild_age_eval.py --execute` before using "
            "the independent AGE route."
        )
    if not snap.get("verified"):
        raise EvaluatorUnavailable(
            f"Evaluator snapshot {snap.get('snapshot_id')} did not pass "
            f"verification and must not be compared against production "
            f"figures. Rebuild it."
        )
    return snap
