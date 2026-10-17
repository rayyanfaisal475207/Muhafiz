"""The toolbelt an analysis agent uses to answer a question itself.

WHY NOT ONE COMPILED QUERY

A planner that compiles one perfect SQL statement per question handles a
bounded family and then falls off a cliff. "Compare what we are dealing
with now against two years ago and explain what changed" is not one query;
it is several, plus judgement about which to run next, plus reasoning over
what came back. The number of such shapes is combinatorial and cannot be
enumerated — which is the same reason there is no route taxonomy and no
vocabulary table.

So SQL stops being the answer generator and becomes the DATA ACCESS LAYER
for an agent that can look, query, look again, and decide when it has
enough.

THE LINE THAT DOES NOT MOVE

The agent decides WHAT to ask. The database produces every NUMBER.

An agent may not read rows and report a total — that is the counting-by-
reading failure this rebuild exists to remove, and it is how the legacy
engine answered 208 for a field that does not exist. Aggregates come back
from `query()` as rows, already computed by Postgres, and the validator
recomputes them from the same result set.

SAFETY, ENFORCED RATHER THAN REQUESTED

An agent writes the SQL, so the SQL is untrusted:

  * a single statement only, SELECT or WITH, never DDL or DML
  * executed in a READ ONLY transaction, so even a missed keyword cannot
    write
  * statement timeout, so a cartesian join cannot hang the request
  * row cap, so a runaway result cannot exhaust memory
  * evidence tables only — chat history and audit logs are not evidence,
    and a question must never be answered from the system's own records

Rejection is explicit and names the reason, because an agent that is told
why can correct itself, while one that gets an empty result learns the
wrong lesson.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

import asyncpg


MAX_ROWS = 500
STATEMENT_TIMEOUT_MS = 15_000


@dataclass
class ToolResult:
    ok: bool
    tool: str
    data: object = None
    error: str | None = None
    note: str | None = None

    def as_dict(self) -> dict:
        out: dict = {"tool": self.tool, "ok": self.ok}
        if self.data is not None:
            out["data"] = self.data
        if self.error:
            out["error"] = self.error
        if self.note:
            out["note"] = self.note
        return out


@dataclass
class QueryLog:
    """Every query an agent ran, so an answer can be audited afterwards."""

    entries: list[dict] = field(default_factory=list)
    # The rows themselves, kept so the validator can re-derive every figure
    # independently. Checking an answer against a summary of what ran is
    # not an independent check.
    results: list[list[dict]] = field(default_factory=list)
    # Numbers seen in ANY tool result, not only query results. describe()
    # reports row counts and values() reports frequencies — both come from
    # the database, and an answer citing them is sourced. Counting only
    # query() rejected the correct answer 73 as unsourced because the agent
    # had read it from a describe.
    observed: list[float] = field(default_factory=list)

    def observe(self, payload) -> None:
        import re as _re
        from decimal import Decimal as _D
        def walk(v):
            if isinstance(v, bool):
                return
            if isinstance(v, (int, float, _D)):
                self.observed.append(float(v))
            elif isinstance(v, str):
                for m in _re.finditer(r"\d[\d,]*(?:\.\d+)?", v):
                    try:
                        self.observed.append(float(m.group(0).replace(",", "")))
                    except ValueError:
                        pass
            elif isinstance(v, dict):
                for x in v.values():
                    walk(x)
            elif isinstance(v, (list, tuple)):
                for x in v:
                    walk(x)
        walk(payload)

    def record(self, sql: str, rows: int, ms: float, error: str | None,
               data: list[dict] | None = None) -> None:
        self.entries.append({"sql": " ".join(sql.split())[:400],
                             "rows": rows, "ms": round(ms, 1),
                             "error": error})
        if data:
            self.results.append(data[:200])

    def evidence_entries(self) -> list[dict]:
        return list(self.entries)

    def numbers_are_sourced(self) -> bool:
        return any(e["error"] is None and e["rows"] > 0 for e in self.entries)


# ── SQL admission ───────────────────────────────────────────────────────

_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"copy|vacuum|reindex|refresh|call|do|set\s+role|security\s+definer)\b",
    re.I)
_STARTS_OK = re.compile(r"^\s*(select|with)\b", re.I)


def admit(sql: str) -> tuple[bool, str | None]:
    """Decide whether agent-written SQL may run at all.

    Checked before execution AND executed read-only, because a keyword
    blacklist alone is not a security boundary — it is a fast rejection
    with a clear message, and the transaction is what actually enforces.
    """
    if not sql or not sql.strip():
        return False, "empty query"
    if ";" in sql.strip().rstrip(";"):
        return False, ("multiple statements in one query; send one statement "
                       "at a time so each result can be attributed")
    if not _STARTS_OK.match(sql):
        return False, "only SELECT or WITH queries are allowed"
    m = _FORBIDDEN.search(sql)
    if m:
        return False, f"{m.group(0)!r} is not permitted; this connection reads only"
    return True, None


async def _allowed_tables(conn: asyncpg.Connection) -> set[str]:
    rows = await conn.fetch(
        "SELECT table_name FROM table_roles WHERE is_evidence")
    return {r["table_name"] for r in rows}


_IDENT = re.compile(r"\b(?:from|join)\s+([a-zA-Z_][a-zA-Z0-9_]*)", re.I)


async def check_tables(conn: asyncpg.Connection,
                       sql: str) -> tuple[bool, str | None]:
    """Refuse a query that reads outside the evidence surface.

    `sessions` and `messages` hold this system's own chat history. A
    question answered from those would be the system citing itself —
    measured as a real risk when a term lookup surfaced session titles as
    a place to find facts about theft.
    """
    allowed = await _allowed_tables(conn)
    referenced = {m.group(1).lower() for m in _IDENT.finditer(sql)}
    # Anything not a known table is a CTE name or an alias; the read-only
    # transaction bounds the damage either way.
    known = await conn.fetch("SELECT DISTINCT table_name FROM column_catalog")
    known_names = {r["table_name"] for r in known}
    offending = sorted((referenced & known_names) - allowed)
    if offending:
        return False, (f"{', '.join(offending)} is not evidence: it cannot "
                       f"support a claim about a case. Evidence tables are "
                       f"those that trace to doc_spans.")
    return True, None


# ── The tools ───────────────────────────────────────────────────────────


async def schema_overview(conn: asyncpg.Connection) -> ToolResult:
    """What exists, and what each table is for. The agent's starting point."""
    rows = await conn.fetch(
        """
        SELECT tr.table_name, tr.role, tr.in_deg,
               obj_description(c.oid, 'pg_class') AS purpose
        FROM table_roles tr
        JOIN pg_class c ON c.relname = tr.table_name
        JOIN pg_namespace n ON n.oid = c.relnamespace AND n.nspname = 'public'
        WHERE tr.is_evidence AND tr.role IN ('fact','dimension','entity')
        ORDER BY tr.role, tr.table_name
        """)
    return ToolResult(True, "schema_overview",
                      [dict(r) for r in rows],
                      note="facts are counted; dimensions group; provenance "
                           "is cited but never joined through")


async def describe(conn: asyncpg.Connection, table: str) -> ToolResult:
    desc = await conn.fetchval("SELECT describe_table($1)", table)
    if desc is None:
        return ToolResult(False, "describe", error=f"no such table: {table}")
    return ToolResult(True, "describe",
                      json.loads(desc) if isinstance(desc, str) else desc)


async def values(conn: asyncpg.Connection, table: str,
                 column: str) -> ToolResult:
    """What values actually occur, with a verdict on whether the list is
    complete. An empty list is never ambiguous here: the verdict says
    whether the column is free text, the table is empty, or every row is
    null — three situations a bare empty result cannot distinguish."""
    rows = await conn.fetch(
        "SELECT value_raw, frequency, script, verdict, exhaustive"
        " FROM discover_values($1, $2)", table, column)
    return ToolResult(True, "values", [dict(r) for r in rows])


async def join_paths(conn: asyncpg.Connection, a: str, b: str,
                     limit: int = 3) -> ToolResult:
    rows = await conn.fetch(
        "SELECT rank, path_tables, join_steps, hops, path_cost, hub_table"
        " FROM find_join_paths($1, $2, 6, $3)", a, b, limit)
    if not rows:
        return ToolResult(False, "join_paths",
                          error=f"no foreign-key path between {a} and {b}")
    return ToolResult(True, "join_paths", [dict(r) for r in rows],
                      note="ranked by hub cost, not hop count: a short route "
                           "through a provenance table connects everything "
                           "to everything and means nothing")


async def coverage(conn: asyncpg.Connection, table: str,
                   column: str) -> ToolResult:
    row = await conn.fetchrow(
        "SELECT must_disclose, null_rate, populated, total, message"
        " FROM coverage_warning($1, $2)", table, column)
    return ToolResult(True, "coverage", dict(row) if row else None)


async def find_term(conn: asyncpg.Connection, term: str) -> ToolResult:
    """Where a word lives — as a table, a column, or a stored value, across
    scripts. This is what stops an agent filtering an English guess against
    an Urdu value and getting a confident zero."""
    rows = await conn.fetch(
        "SELECT match_kind, table_name, column_name, value_raw, score,"
        " table_role, note FROM search_schema($1)", term)
    return ToolResult(True, "find_term", [dict(r) for r in rows],
                      note="a cross_script match is a CANDIDATE: romanisation "
                           "is lossy and must be confirmed before filtering")


async def query(conn: asyncpg.Connection, sql: str,
                log: QueryLog | None = None) -> ToolResult:
    """Run agent-written SQL, read-only and bounded."""
    import time

    ok, why = admit(sql)
    if not ok:
        if log:
            log.record(sql, 0, 0.0, why)
        return ToolResult(False, "query", error=why)

    ok, why = await check_tables(conn, sql)
    if not ok:
        if log:
            log.record(sql, 0, 0.0, why)
        return ToolResult(False, "query", error=why)

    started = time.perf_counter()
    try:
        # READ ONLY is the actual boundary; the keyword check above only
        # gives a faster, clearer refusal.
        async with conn.transaction(readonly=True):
            await conn.execute(
                f"SET LOCAL statement_timeout = {STATEMENT_TIMEOUT_MS}")
            rows = await conn.fetch(f"SELECT * FROM ({sql}) _q LIMIT {MAX_ROWS}")
    except Exception as exc:                            # noqa: BLE001
        err = str(exc)[:300]
        if log:
            log.record(sql, 0, (time.perf_counter() - started) * 1000, err)
        # The agent is told what went wrong so it can correct the query.
        # An empty result would teach it the wrong lesson.
        return ToolResult(False, "query", error=err)

    ms = (time.perf_counter() - started) * 1000
    data = [dict(r) for r in rows]
    if log:
        log.record(sql, len(data), ms, None, data)
    note = None
    if len(data) == MAX_ROWS:
        note = (f"result capped at {MAX_ROWS} rows — aggregate in SQL rather "
                f"than counting these rows yourself")
    return ToolResult(True, "query", data, note=note)


TOOLS = {
    "schema_overview": schema_overview,
    "describe": describe,
    "values": values,
    "join_paths": join_paths,
    "coverage": coverage,
    "find_term": find_term,
    "query": query,
}
