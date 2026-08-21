"""
Milestone A2 verification (§7-A of GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md) —
BM25 candidate-pool query latency, before vs. after the persistent
Postgres full-text index, at 10x and 100x the real corpus's chunk count.

Runs against an ISOLATED throwaway Postgres database (`muhafiz_loadtest`,
its own `CREATE DATABASE` on the same running instance, dropped at the end
of the run) — never mixed into real data, same convention as
scripts/loadtest_identity_index.py (A1).

"Before" and "after" are measured directly against the two real
candidate-generation strategies:
  - before: fetch every chunk in scope (what get_all_chunks() always did)
    and tokenize the WHOLE pool with BM25Okapi — the actual per-query cost
    orchestrator.py's own comment flagged ("this rebuilds an in-memory
    BM25 index over the full scoped corpus on every retrieval... the
    dominant cost per query").
  - after: src/retrieval/fulltext_index.py's candidate_pool() — a GIN
    lookup that narrows the pool to chunks sharing >=1 token with the
    query, THEN the same BM25Okapi tokenize+score pass over that smaller
    pool.

Both legs include the BM25Okapi construction cost, not just the fetch —
that construction, not the fetch itself, is the per-query cost this
milestone targets.

Usage:
    python scripts/loadtest_fulltext_index.py [--keep]
"""
from __future__ import annotations

import asyncio
import random
import statistics
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import asyncpg
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from src import config
from src.ingestion.tokenizer import tokenize

LOADTEST_DB = "muhafiz_loadtest"

# 1x = the real corpus's measured chunk count. The M1-M12 migration's own
# decision record measured ~350 chunks from the real 73-FIR/198-record
# corpus (docs/decisions/0001-muhafiz-api-migration.md: "narrative 59K +
# zimni 66K + roznamcha 4K ~ 130K chars -> ~350 chunks").
BASELINE_CHUNK_COUNT = 350
SCALES = {"10x": BASELINE_CHUNK_COUNT * 10, "100x": BASELINE_CHUNK_COUNT * 100}

QUERIES_PER_SCALE = 50

# The query vocabulary — every synthetic query is 3 of these terms.
_VOCAB = [
    "theft", "vehicle", "recovered", "arrested", "accused", "complainant",
    "witness", "narrative", "station", "district", "firearm", "weapon",
    "investigation", "challan", "court", "property", "movable", "cnic",
    "phone", "address", "incident", "occurred", "reported", "case",
]

# A large NOISE vocabulary (distinct from _VOCAB) that most of each
# chunk's tokens are drawn from — real FIR narrative text has thousands
# of distinct words, so any given 2-3 term query only matches a small,
# selective fraction of the corpus (the whole reason a GIN index helps at
# all). Using only the ~24 realistic-shaped terms above for EVERY token
# in EVERY chunk (as an earlier version of this script did) makes the
# corpus artificially dense — nearly every chunk then contains nearly
# every query term, so Postgres correctly picks a sequential scan over
# the GIN index (it would have to visit almost every row either way) —
# a genuine planner decision, but one driven by this fixture's unrealistic
# term density, not by anything about the index itself. 3000 distinct
# noise tokens keeps term-density realistic without needing real text.
_NOISE_VOCAB = [f"noiseterm{i:04d}" for i in range(3000)]


def _synthetic_chunk_text(rng: random.Random) -> str:
    # Mostly noise (realistic vocabulary breadth), with a couple of real
    # _VOCAB terms mixed in about a third of the time — some chunks
    # genuinely match a given query, most don't, same shape as a real
    # corpus against a real keyword query.
    tokens = rng.choices(_NOISE_VOCAB, k=27)
    if rng.random() < 0.35:
        tokens += rng.sample(_VOCAB, k=2)
    rng.shuffle(tokens)
    return " ".join(tokens)


def _dsn_for(dbname: str) -> str:
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
    await conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chunk_fulltext (
            chunk_id    TEXT PRIMARY KEY,
            doc_id      TEXT NOT NULL,
            source      TEXT,
            project_id  TEXT,
            case_id     TEXT,
            is_global   BOOLEAN NOT NULL DEFAULT false,
            text        TEXT NOT NULL,
            metadata    JSONB NOT NULL DEFAULT '{}'::jsonb,
            tsv         TSVECTOR NOT NULL,
            updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    await conn.execute("CREATE INDEX IF NOT EXISTS ix_chunk_fulltext_tsv ON chunk_fulltext USING GIN (tsv)")


async def _populate(conn: asyncpg.Connection, target_count: int) -> list[str]:
    existing = await conn.fetchval("SELECT count(*) FROM chunk_fulltext")
    to_add = max(target_count - existing, 0)
    rng = random.Random(42)

    BATCH_SIZE = 1000
    for batch_start in range(0, to_add, BATCH_SIZE):
        rows = []
        for i in range(batch_start, min(batch_start + BATCH_SIZE, to_add)):
            chunk_id = f"LOADTEST-C-{uuid.uuid4().hex[:12]}"
            raw_text = _synthetic_chunk_text(rng)
            tokenized = " ".join(tokenize(raw_text))
            rows.append((chunk_id, "LOADTEST-DOC", "loadtest.pdf", None, None, True, raw_text, "{}", tokenized))
        await conn.executemany(
            """
            INSERT INTO chunk_fulltext (chunk_id, doc_id, source, project_id, case_id, is_global, text, metadata, tsv)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb, to_tsvector('simple', $9))
            ON CONFLICT (chunk_id) DO NOTHING
            """,
            rows,
        )

    return []


async def _fetch_all_in_scope(conn: asyncpg.Connection) -> list[dict]:
    """The BEFORE path — what get_all_chunks() always returned: every
    chunk in scope, unfiltered by the query at all."""
    rows = await conn.fetch("SELECT chunk_id, text FROM chunk_fulltext")
    return [{"id": r["chunk_id"], "text": r["text"]} for r in rows]


async def _candidate_pool(conn: asyncpg.Connection, query_text: str) -> list[dict]:
    """The AFTER path — src/retrieval/fulltext_index.py's actual query."""
    tokens = tokenize(query_text)
    tsquery = " | ".join(tokens)
    rows = await conn.fetch(
        "SELECT chunk_id, text FROM chunk_fulltext WHERE tsv @@ to_tsquery('simple', $1) LIMIT 2000",
        tsquery,
    )
    return [{"id": r["chunk_id"], "text": r["text"]} for r in rows]


def _bm25_score(query_text: str, pool: list[dict]) -> int:
    """Mirrors retrieve_bm25()'s actual cost: tokenize the corpus, build
    BM25Okapi, score. Returns len(pool) scored, just to prove the work ran."""
    from rank_bm25 import BM25Okapi

    if not pool:
        return 0
    tokenized_corpus = [tokenize(doc["text"]) for doc in pool]
    bm25 = BM25Okapi(tokenized_corpus)
    bm25.get_scores(tokenize(query_text))
    return len(pool)


def _summarize(label: str, samples: list[float]) -> str:
    s = sorted(samples)
    p50 = statistics.median(s)
    p95 = s[int(len(s) * 0.95) - 1]
    return f"{label}: n={len(s)} mean={statistics.mean(s):.2f}ms p50={p50:.2f}ms p95={p95:.2f}ms max={max(s):.2f}ms"


async def main() -> None:
    keep = "--keep" in sys.argv
    rng = random.Random(7)

    await _ensure_loadtest_database()
    conn = await asyncpg.connect(_dsn_for(LOADTEST_DB))
    try:
        await _setup_schema(conn)

        print(f"\n{'='*70}\nMilestone A2 — BM25 candidate-pool query latency, before vs after\n{'='*70}")
        for scale_label, target_count in SCALES.items():
            print(f"\n--- Scale {scale_label} ({target_count} chunks) ---")
            await _populate(conn, target_count)

            before_samples, after_samples = [], []
            before_pool_sizes, after_pool_sizes = [], []
            for _ in range(QUERIES_PER_SCALE):
                query_text = " ".join(rng.choices(_VOCAB, k=3))

                start = time.perf_counter()
                pool = await _fetch_all_in_scope(conn)
                _bm25_score(query_text, pool)
                before_samples.append((time.perf_counter() - start) * 1000)
                before_pool_sizes.append(len(pool))

                start = time.perf_counter()
                pool = await _candidate_pool(conn, query_text)
                _bm25_score(query_text, pool)
                after_samples.append((time.perf_counter() - start) * 1000)
                after_pool_sizes.append(len(pool))

            print(_summarize("BEFORE (full scoped pool, rebuilt BM25 index every query)", before_samples))
            print(f"  mean pool size: {statistics.mean(before_pool_sizes):.0f} chunks")
            print(_summarize("AFTER  (persistent GIN candidate pool -> BM25 over just that)", after_samples))
            print(f"  mean pool size: {statistics.mean(after_pool_sizes):.0f} chunks")
            speedup = statistics.mean(before_samples) / statistics.mean(after_samples)
            print(f"Speedup (mean): {speedup:.1f}x")

        # §7-A: confirm the candidate-generation query itself is index-
        # backed, not a sequential scan, at the largest scale populated.
        plan_rows = await conn.fetch(
            "EXPLAIN SELECT chunk_id, text FROM chunk_fulltext WHERE tsv @@ to_tsquery('simple', 'theft | vehicle') LIMIT 2000"
        )
        plan_text = "\n".join(r[0] for r in plan_rows)
        print(f"\n--- Query plan for candidate_pool() ---\n{plan_text}")
        assert "Seq Scan" not in plan_text, (
            "candidate_pool()'s query used a sequential scan instead of the GIN index."
        )
        print("\nCONFIRMED: no full sequential scan on the candidate-generation query.")
    finally:
        await conn.close()
        if not keep:
            admin_conn = await asyncpg.connect(_dsn_for("postgres"))
            try:
                await admin_conn.execute(f'DROP DATABASE IF EXISTS "{LOADTEST_DB}" WITH (FORCE)')
                print(f"\nDropped throwaway database {LOADTEST_DB!r}.")
            finally:
                await admin_conn.close()
        else:
            print(f"\n--keep passed: {LOADTEST_DB!r} left in place for inspection.")


if __name__ == "__main__":
    asyncio.run(main())
