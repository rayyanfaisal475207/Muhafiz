# -*- coding: utf-8 -*-
"""
Phase 5D — the execution boundary for compiler-produced AGE queries.

WHY A SEPARATE ENTRY POINT. `age_client.execute_cypher()` takes a string,
and 152 trusted call sites across the codebase depend on that. This module
does not change or wrap them. What it adds is a door that only accepts a
`CompiledAgeQuery` — an object nothing but `age_compiler.compile_plan()`
constructs — so the aggregate AGE route physically cannot pass model text
to production, even by a future careless edit. The type IS the check.

WHAT THIS EXECUTES AGAINST. Production `muhafiz.evidence_graph`, resolved
entirely from trusted application configuration. There is no graph or
database parameter, because after Phase 5D there is no untrusted text in
the query — but the model must still never select a target, so the target
is not selectable at all. That is a Phase 5C property worth keeping even
though its original justification has changed.

DEFENCE IN DEPTH, EXPLICITLY SECOND. The `cypher_guard` pass below is a
REGRESSION ASSERTION, not the boundary: compiler output should never trip
it, because the compiler has no mutation emitter. If it ever does, that
means the trusted compiler emitted something forbidden — a defect far more
serious than a bad model response — so it is logged at CRITICAL and the
query does not run.

RESOURCE LIMITS SURVIVE THE PIVOT. Read-only queries cannot corrupt the
graph but can still pin a connection over 13,467 edges, so the statement
timeout, row cap and bounded pool carried over from Phase 5C apply here,
now against production.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

import asyncpg

from src import config
from src.pipeline.aggregate import cypher_guard
from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate.age_compiler import CompiledAgeQuery

logger = logging.getLogger(__name__)

#: The production graph. A module constant, never an argument — see the
#: module docstring.
PRODUCTION_GRAPH = "evidence_graph"


class AgeExecutionError(RuntimeError):
    """Execution failed. Never falls back to another target."""


class CompilerDefect(RuntimeError):
    """The trusted compiler emitted something the guard rejects.

    This should be unreachable. It exists so that if it ever happens, it
    surfaces as a named, logged defect rather than as a query that runs.
    """


_pool: Optional[asyncpg.Pool] = None


def _dsn() -> str:
    raw = config.DATABASE_URL or ""
    if not raw:
        raise AgeExecutionError("DATABASE_URL is not configured.")
    return raw.replace("postgresql+asyncpg://", "postgresql://")


async def get_pool() -> asyncpg.Pool:
    """Bounded, timeout-enforced pool for aggregate AGE reads.

    Separate from `age_client`'s pool so that a slow aggregate cannot
    exhaust the connection slots the rest of the application needs — the
    bounded-concurrency property Phase 5C established, kept.
    """
    global _pool
    if _pool is None:
        timeout_ms = int(config.AGE_QUERY_STATEMENT_TIMEOUT_MS)
        _pool = await asyncpg.create_pool(
            _dsn(),
            min_size=1,
            max_size=int(config.AGE_QUERY_MAX_POOL_SIZE),
            # AGE's cypher() breaks under asyncpg's prepared-statement
            # cache — same workaround age_client documents.
            statement_cache_size=0,
            command_timeout=max(1.0, timeout_ms / 1000.0) + 5.0,
            server_settings={"statement_timeout": str(timeout_ms)},
        )
    return _pool


async def close_pool() -> None:
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def _parse_agtype(raw):
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


def assert_read_only(
    compiled: CompiledAgeQuery, snapshot: reg.RegistrySnapshot
) -> tuple[str, ...]:
    """Run the existing guard over compiler output as a regression check.

    Returns the guard's violation codes (normally empty). Raises
    `CompilerDefect` if the guard objects on SAFETY grounds — which would
    mean the trusted compiler emitted a forbidden construction.
    """
    report = cypher_guard.guard(compiled.cypher, snapshot)
    if report.safe:
        return ()

    codes = report.codes()
    if report.worst_severity() == "SAFETY":
        logger.critical(
            "COMPILER DEFECT: age_compiler emitted Cypher the safety guard "
            "rejects (%s). Query not executed. This is a trusted-code bug, "
            "not a bad model response. Cypher: %s",
            report.summary(), compiled.cypher,
        )
        raise CompilerDefect(
            f"Compiler emitted a query the safety guard rejects: "
            f"{report.summary()}"
        )

    # Semantic-only findings are advisory here: the compiler already
    # injected the registry's correctness predicates, and the guard's
    # semantic rules are heuristics over TEXT that can misread a
    # legitimately-compiled construction.
    logger.warning(
        "Guard raised semantic findings on compiled AGE query (%s): %s",
        codes, compiled.cypher,
    )
    return codes


async def execute_compiled_age_query(
    compiled: CompiledAgeQuery,
) -> list[dict]:
    """Execute a compiler-produced query against production `evidence_graph`.

    Accepts ONLY a `CompiledAgeQuery`. There is no string overload, no
    `graph=`, no `database=` — the target comes from trusted configuration
    and the query text came from the deterministic compiler.
    """
    if not isinstance(compiled, CompiledAgeQuery):
        raise AgeExecutionError(
            f"execute_compiled_age_query accepts only CompiledAgeQuery "
            f"objects produced by age_compiler.compile_plan(); got "
            f"{type(compiled).__name__}. Raw Cypher has no route to "
            f"production through this function."
        )

    max_rows = int(config.AGE_QUERY_MAX_ROWS)
    col_list = ", ".join(f"{c} agtype" for c in compiled.columns)
    sql = f"""
        SELECT * FROM cypher('{PRODUCTION_GRAPH}', $cypher${compiled.cypher}$cypher$, $1::agtype)
        AS ({col_list})
        LIMIT {max_rows + 1}
    """

    try:
        pool = await get_pool()
        async with pool.acquire() as conn:
            try:
                await conn.execute("LOAD 'age'")
            except asyncpg.InsufficientPrivilegeError:
                pass
            await conn.execute('SET search_path = ag_catalog, "$user", public')
            rows = await conn.fetch(sql, json.dumps(compiled.params or {}))
    except (asyncpg.PostgresError, OSError) as exc:
        raise AgeExecutionError(
            f"Compiled AGE query failed ({type(exc).__name__}): {exc}"
        ) from exc

    if len(rows) > max_rows:
        raise AgeExecutionError(
            f"Compiled query returned more than AGE_QUERY_MAX_ROWS "
            f"({max_rows}) rows; refusing to materialise an unbounded result."
        )

    return [
        {col: _parse_agtype(row[col]) for col in compiled.columns}
        for row in rows
    ]
