# -*- coding: utf-8 -*-
"""
Module 178 — bound 2 (read-only, least privilege) and bound 4 (timeout)
probed against the LIVE Postgres/AGE, safely.

Every write-shaped statement below runs inside a transaction that is
ROLLED BACK unconditionally, on the `muhafiz_mcp_readonly` role
(`MCP_DATABASE_URL`). What is recorded is whether the ROLE or a READ ONLY
transaction refused the statement — not whether it persisted (it cannot:
rollback). The finding this produced (tracker row 183): on AGE 1.5.0 a
SELECT-only role and a READ ONLY transaction both let SET / REMOVE /
DETACH DELETE through; only CREATE / MERGE are refused. The unconditional
rollback in `llm_query_fallback.execute_readonly()` is therefore the bound
that holds, and this probe also proves that path leaves no trace.

DO NOT run the write statements here outside a rollback-only transaction.
The first time this was tried without one (2026-09-15, this module) the
`DETACH DELETE` removed all 73 Case vertices and every edge on them from
the live graph; they were restored from SHARE/database/muhafiz_dump.sql
(MODULE178_RESULT.md §3.2).

    PYTHONPATH=. python -X utf8 evaluation/module178_bound_probe.py
"""
from __future__ import annotations

import asyncio
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT = ROOT / "docs" / "gold-qa-wave2-results" / "module178_live" / "module178_bound_probe.json"

WRITES = {
    "CREATE": "CREATE (x:Weapon {canonical_name: 'm178-probe'}) RETURN x.canonical_name AS n",
    "MERGE": "MERGE (x:Weapon {canonical_name: 'm178-probe'}) RETURN x.canonical_name AS n",
    "SET": "MATCH (c:Case) WHERE c.case_id = 'fir-142-26' SET c.m178_probe = 'x' RETURN c.case_id AS n",
    "REMOVE": "MATCH (c:Case) WHERE c.case_id = 'fir-142-26' REMOVE c.confidence RETURN c.case_id AS n",
    "DETACH DELETE": "MATCH (c:Case) WHERE c.case_id = 'fir-142-26' DETACH DELETE c RETURN 1 AS n",
}


async def _probe_role(conn, cypher: str, *, readonly_txn: bool) -> dict:
    """Run `cypher` on `conn` inside a transaction that is always rolled back."""
    from src.graph.age_client import _load_age
    await _load_age(conn)
    tr = conn.transaction(readonly=readonly_txn)
    await tr.start()
    try:
        rows = await conn.fetch(
            f"SELECT * FROM cypher('evidence_graph', $cypher${cypher}$cypher$, $1::agtype) AS (n agtype)",
            "{}",
        )
        return {"refused": False, "rows": len(rows)}
    except Exception as exc:  # noqa: BLE001
        return {"refused": True, "error": f"{type(exc).__name__}: {str(exc).splitlines()[0][:160]}"}
    finally:
        await tr.rollback()


async def main() -> None:
    import asyncpg
    from src import config
    from src.pipeline import llm_query_fallback as f

    out: dict = {"role": "muhafiz_mcp_readonly", "graph": "evidence_graph", "ran_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    dsn = f._readonly_dsn()
    assert "muhafiz_mcp_readonly" in dsn, "MCP_DATABASE_URL must point at the read-only role"
    conn = await asyncpg.connect(dsn, statement_cache_size=0)
    try:
        who = await conn.fetchrow("SELECT current_user, rolsuper, rolbypassrls FROM pg_roles WHERE rolname = current_user")
        out["connection"] = dict(who)
        out["grants"] = [dict(r) for r in await conn.fetch(
            "SELECT nspname AS schema, has_schema_privilege(current_user, nspname, 'USAGE') AS usage "
            "FROM pg_namespace WHERE nspname IN ('public','ag_catalog','evidence_graph','evidence_graph_eval') ORDER BY 1")]
        out["table_privs_on_Case"] = dict(await conn.fetchrow(
            "SELECT has_table_privilege(current_user, 'evidence_graph.\"Case\"', 'SELECT') AS select, "
            "has_table_privilege(current_user, 'evidence_graph.\"Case\"', 'INSERT') AS insert, "
            "has_table_privilege(current_user, 'evidence_graph.\"Case\"', 'UPDATE') AS update, "
            "has_table_privilege(current_user, 'evidence_graph.\"Case\"', 'DELETE') AS delete"))
        out["read_works"] = await _probe_role(conn, "MATCH (c:Case) RETURN count(c) AS n", readonly_txn=True)
        out["writes_role_only"] = {k: await _probe_role(conn, q, readonly_txn=False) for k, q in WRITES.items()}
        out["writes_role_plus_read_only_txn"] = {k: await _probe_role(conn, q, readonly_txn=True) for k, q in WRITES.items()}
    finally:
        await conn.close()

    # The executor path itself: a SET forced past validation (called directly,
    # validation deliberately bypassed) leaves no trace after the call.
    rows = await f.execute_readonly(WRITES["SET"], {}, ["n"])
    after = await f.execute_readonly(
        "MATCH (c:Case) WHERE c.case_id = 'fir-142-26' RETURN c.m178_probe AS v, c.confidence AS conf", {}, ["v", "conf"])
    out["executor_rollback_proof"] = {"forced_set_ran": rows, "after_call": after,
                                      "no_trace": after[0]["v"] is None and after[0]["conf"] is not None}
    # The text filter, on the same five statements.
    out["text_filter"] = {}
    for k, q in WRITES.items():
        try:
            f.validate_query(q)
            out["text_filter"][k] = "ACCEPTED"
        except f.QueryRejected as exc:
            out["text_filter"][k] = f"rejected: {exc.reason}"
    # Bound 4: the statement timeout, measured.
    t = time.monotonic()
    try:
        await f.execute_readonly("MATCH (a:Person)-[:ASSOCIATED_WITH*1..8]-(b:Person) RETURN count(*) AS n", {}, ["n"], timeout_s=1)
        out["timeout"] = {"fired": False}
    except Exception as exc:  # noqa: BLE001
        out["timeout"] = {"fired": True, "error": type(exc).__name__, "after_s": round(time.monotonic() - t, 1)}
    await f.close_readonly_pool()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    io.open(OUT, "w", encoding="utf-8").write(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    print(json.dumps(out, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    asyncio.run(main())
