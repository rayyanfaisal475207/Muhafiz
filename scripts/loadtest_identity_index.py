"""
Milestone A1 verification (§7-A of GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md) —
identity-lookup latency, before vs. after the Postgres identity index, at
10x and 100x the real corpus's Person-node count.

Runs entirely against an ISOLATED throwaway Postgres database
(`muhafiz_loadtest`, on the same running Postgres/AGE instance as the real
`muhafiz` database, but a completely separate `CREATE DATABASE` — no
tables, no rows, nothing shared) — the plan's explicit "bulk synthetic
FIRs generated for load-testing only, never mixed into real data"
requirement. Every synthetic Person node and identity_index row this
script writes lives only in that throwaway database, and the whole
database is dropped at the end (or left in place with --keep for manual
inspection, never merged into `muhafiz`).

"Before" and "after" are measured directly against the two real code
paths in src/graph/entity_resolution.py:
  - before: the plain `MATCH (n:Person {cnic: $id_value})` Cypher this
    codebase always ran — the exact query _find_by_primary_id() falls
    back to on an index miss.
  - after:  identity_index.lookup() — the new Postgres primary-key read.

Usage:
    python scripts/loadtest_identity_index.py [--keep]

Requires a reachable Postgres/AGE instance (same DATABASE_URL host/port/
credentials as .env) — local/self-hosted only, same constraint as
scripts/apply_migration.py.
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import json

import asyncpg

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from src import config

LOADTEST_DB = "muhafiz_loadtest"
GRAPH_NAME = "loadtest_graph"

# 1x = the real evidence_graph's current Person-node count (measured
# directly: `MATCH (n:Person) RETURN count(n)` against production returned
# 198 at the time this script was written, from 73 real FIRs) — 10x/100x
# below are relative to that measured baseline, not an arbitrary round
# number, per §7-A's "at a synthetic 10x and 100x corpus size".
BASELINE_PERSON_COUNT = 198
SCALES = {"10x": BASELINE_PERSON_COUNT * 10, "100x": BASELINE_PERSON_COUNT * 100}

LOOKUPS_PER_SCALE = 200  # sampled lookups per before/after measurement


def _dsn_for(dbname: str) -> str:
    # asyncpg wants a plain postgresql:// DSN, not SQLAlchemy's
    # "postgresql+asyncpg://" driver-qualified form config.DATABASE_URL uses.
    url = config.DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
    base, _, _ = url.rpartition("/")
    return f"{base}/{dbname}"


async def _ensure_loadtest_database() -> None:
    conn = await asyncpg.connect(_dsn_for("postgres"))
    try:
        exists = await conn.fetchval("SELECT 1 FROM pg_database WHERE datname = $1", LOADTEST_DB)
        if not exists:
            await conn.execute(f'CREATE DATABASE "{LOADTEST_DB}"')
            print(f"Created throwaway database {LOADTEST_DB!r}.")
        else:
            print(f"Throwaway database {LOADTEST_DB!r} already exists — reusing.")
    finally:
        await conn.close()


async def _setup_schema(conn: asyncpg.Connection) -> None:
    await conn.execute("CREATE EXTENSION IF NOT EXISTS age")
    await conn.execute("LOAD 'age'")
    await conn.execute('SET search_path = ag_catalog, "$user", public')
    exists = await conn.fetchval(
        "SELECT 1 FROM ag_catalog.ag_graph WHERE name = $1", GRAPH_NAME
    )
    if not exists:
        await conn.execute("SELECT create_graph($1)", GRAPH_NAME)
    label_exists = await conn.fetchval(
        """
        SELECT 1 FROM ag_catalog.ag_label l
        JOIN ag_catalog.ag_graph g ON g.graphid = l.graph
        WHERE g.name = $1 AND l.name = 'Person'
        """,
        GRAPH_NAME,
    )
    if not label_exists:
        await conn.execute("SELECT create_vlabel($1, 'Person')", GRAPH_NAME)

    # Same shape as migrations/021_identity_index.sql.
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS identity_index (
            label       TEXT NOT NULL,
            id_key      TEXT NOT NULL,
            id_value    TEXT NOT NULL,
            entity_id   TEXT NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
            PRIMARY KEY (label, id_key, id_value)
        )
        """
    )
    await conn.execute(
        "CREATE INDEX IF NOT EXISTS ix_identity_index_label_key_entity "
        "ON identity_index (label, id_key, entity_id)"
    )


async def _cypher(conn: asyncpg.Connection, query: str, params: dict, columns: str = "n agtype"):
    """Mirrors src/graph/age_client.py's execute_cypher() wrapping exactly
    — $paramname references bound via one JSON-encoded agtype argument,
    never string-interpolated into the query text (same injection-safety
    reasoning as the real client)."""
    sql = f"SELECT * FROM cypher('{GRAPH_NAME}', $cypher${query}$cypher$, $1::agtype) AS ({columns})"
    return await conn.fetch(sql, json.dumps(params))


async def _current_person_count(conn: asyncpg.Connection) -> int:
    rows = await _cypher(conn, "MATCH (n:Person) RETURN count(n)", {}, columns="c agtype")
    return int(str(rows[0]["c"]))


async def _populate(conn: asyncpg.Connection, target_count: int) -> list[str]:
    """
    Insert synthetic Person nodes (fake CNICs, fake names) up to
    target_count, and their matching identity_index rows. Returns the
    list of CNICs inserted, to sample lookups against.

    Batched via UNWIND (one round trip per BATCH_SIZE nodes) — this is
    test-corpus setup, not the thing being measured, but a naive
    one-MERGE-per-node loop at 19800 nodes would make the 100x scale take
    unreasonably long for no reason relevant to this test.
    """
    BATCH_SIZE = 500
    existing = await _current_person_count(conn)
    to_add = max(target_count - existing, 0)

    cnics: list[str] = []
    for batch_start in range(0, to_add, BATCH_SIZE):
        batch_rows = []
        for i in range(batch_start, min(batch_start + BATCH_SIZE, to_add)):
            entity_id = f"LOADTEST-P-{uuid.uuid4().hex[:12]}"
            cnic = f"9{i:010d}"  # synthetic, never a real CNIC shape reused elsewhere
            cnics.append(cnic)
            batch_rows.append({"entity_id": entity_id, "cnic": cnic})

        # CREATE, not MERGE — these entity_ids/CNICs are freshly minted
        # (uuid4-based) and guaranteed unique within this run, so there is
        # no existing-node case to match against. MERGE's per-row
        # existence check against a label with no property index is
        # exactly the O(nodes) scan Milestone A1 exists to get away from
        # — using it here would make populating the 100x fixture itself
        # take the same super-linear time this test is trying to measure
        # a workaround for, for no benefit (this is corpus setup, not the
        # thing under measurement).
        await _cypher(
            conn,
            "UNWIND $rows AS row CREATE (n:Person {entity_id: row.entity_id, cnic: row.cnic}) RETURN n",
            {"rows": batch_rows},
        )
        await conn.executemany(
            """
            INSERT INTO identity_index (label, id_key, id_value, entity_id, updated_at)
            VALUES ('Person', 'cnic', $1, $2, now())
            ON CONFLICT (label, id_key, id_value) DO NOTHING
            """,
            [(r["cnic"], r["entity_id"]) for r in batch_rows],
        )

    # Also sample from whatever already existed in an earlier run (--keep).
    if not cnics:
        rows = await conn.fetch(
            "SELECT id_value FROM identity_index WHERE label='Person' AND id_key='cnic' LIMIT $1",
            LOOKUPS_PER_SCALE,
        )
        cnics = [r["id_value"] for r in rows]
    return cnics


async def _measure_before(conn: asyncpg.Connection, cnics: list[str]) -> list[float]:
    """The plain AGE MATCH-on-property scan — no index, exactly the query
    this codebase always ran before Milestone A1."""
    samples = []
    for cnic in cnics:
        start = time.perf_counter()
        await _cypher(conn, "MATCH (n:Person {cnic: $cnic}) RETURN n", {"cnic": cnic})
        samples.append((time.perf_counter() - start) * 1000)
    return samples


async def _measure_after(conn: asyncpg.Connection, cnics: list[str]) -> list[float]:
    """identity_index.lookup()'s actual query — the new O(1) path."""
    samples = []
    for cnic in cnics:
        start = time.perf_counter()
        await conn.fetchrow(
            "SELECT entity_id FROM identity_index WHERE label = 'Person' AND id_key = 'cnic' AND id_value = $1",
            cnic,
        )
        samples.append((time.perf_counter() - start) * 1000)
    return samples


def _summarize(label: str, samples: list[float]) -> str:
    samples_sorted = sorted(samples)
    p50 = statistics.median(samples_sorted)
    p95 = samples_sorted[int(len(samples_sorted) * 0.95) - 1]
    return f"{label}: n={len(samples)} mean={statistics.mean(samples_sorted):.3f}ms p50={p50:.3f}ms p95={p95:.3f}ms max={max(samples_sorted):.3f}ms"


async def main() -> None:
    keep = "--keep" in sys.argv

    await _ensure_loadtest_database()
    conn = await asyncpg.connect(_dsn_for(LOADTEST_DB))
    try:
        await _setup_schema(conn)

        print(f"\n{'='*70}\nMilestone A1 — identity-lookup latency, before vs after\n{'='*70}")
        for scale_label, target_count in SCALES.items():
            print(f"\n--- Scale {scale_label} ({target_count} Person nodes) ---")
            cnics = await _populate(conn, target_count)
            sample_cnics = cnics[:LOOKUPS_PER_SCALE] or cnics

            before = await _measure_before(conn, sample_cnics)
            after = await _measure_after(conn, sample_cnics)

            print(_summarize("BEFORE (AGE property scan, no index)", before))
            print(_summarize("AFTER  (identity_index Postgres lookup)", after))
            speedup = statistics.mean(before) / statistics.mean(after) if statistics.mean(after) else float("inf")
            print(f"Speedup (mean): {speedup:.1f}x")

        # §7-A: "confirm no full-label-scan query plan remains on the
        # identity-lookup hot path" — EXPLAIN identity_index's own query
        # plan (the AFTER path) and assert it is an index/PK lookup, not
        # a Seq Scan.
        sample_cnic = (await conn.fetchrow(
            "SELECT id_value FROM identity_index WHERE label='Person' AND id_key='cnic' LIMIT 1"
        ))["id_value"]
        plan_rows = await conn.fetch(
            "EXPLAIN SELECT entity_id FROM identity_index WHERE label = 'Person' AND id_key = 'cnic' AND id_value = $1",
            sample_cnic,
        )
        plan_text = "\n".join(r[0] for r in plan_rows)
        print(f"\n--- Query plan for identity_index.lookup() ---\n{plan_text}")
        assert "Seq Scan" not in plan_text, (
            "identity_index lookup used a sequential scan — the identity-lookup "
            "hot path still has a full-scan plan behind it."
        )
        print("\nCONFIRMED: no full-scan query plan on the identity-lookup hot path.")
    finally:
        await conn.close()
        if not keep:
            admin_conn = await asyncpg.connect(_dsn_for("postgres"))
            try:
                await admin_conn.execute(
                    f'DROP DATABASE IF EXISTS "{LOADTEST_DB}" WITH (FORCE)'
                )
                print(f"\nDropped throwaway database {LOADTEST_DB!r}.")
            finally:
                await admin_conn.close()
        else:
            print(f"\n--keep passed: {LOADTEST_DB!r} left in place for inspection.")


if __name__ == "__main__":
    asyncio.run(main())
