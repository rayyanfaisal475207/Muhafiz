"""
Milestone A3 verification (§7-A of GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md) —
embedding-pipeline throughput, before vs. after bounded-concurrency
embedding, measured against the REAL local model server (EMBEDDINGS_URL),
not a fake.

Unlike A1/A2 (which populate a throwaway database with tens of thousands
of synthetic rows), a real 10x/100x corpus of live HTTP requests against a
single shared model-server tunnel would be a disproportionate load to put
on that infrastructure just to run a load test. Instead: BEFORE and AFTER
are both measured directly, live, over a modest real sample (
SAMPLE_SIZE texts, actually sent to EMBEDDINGS_URL) — enough to get a
real, not fabricated, per-request latency and a real concurrent-vs-
sequential wall-clock comparison. 10x/100x corpus-size throughput is then
PROJECTED from that measured per-request latency and
config.EMBEDDING_MAX_CONCURRENCY (clearly labeled as a projection, not
re-measured at that volume against shared live infrastructure).

BEFORE reproduces the exact old code path (sequential, one request at a
time, 0.3s sleep between requests) against the real endpoint. AFTER calls
the actual current src.retrieval.embedder.embed_texts() — the real
production code path, not a re-implementation of it.

Usage:
    python scripts/loadtest_embedding_pipeline.py
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

import httpx

from src import config
from src.retrieval.embedder import embed_texts

SAMPLE_SIZE = 24
OLD_INTER_REQUEST_DELAY = 0.3  # the exact pacing this milestone removes

# 1x = the real corpus's measured chunk count (docs/decisions/0001-
# muhafiz-api-migration.md: ~350 chunks from the real 73-FIR corpus).
BASELINE_CHUNK_COUNT = 350
PROJECTION_SCALES = {"1x": BASELINE_CHUNK_COUNT, "10x": BASELINE_CHUNK_COUNT * 10, "100x": BASELINE_CHUNK_COUNT * 100}


def _sample_texts(n: int) -> list[str]:
    # Varied-length real-ish sentences — long enough that the model server
    # does genuine work per request, short enough that SAMPLE_SIZE real
    # requests complete in a reasonable time for a load test.
    base = (
        "The complainant reported that a motorcycle was stolen from outside "
        "the residence during the night. An FIR was registered under the "
        "relevant section of the Pakistan Penal Code, and the investigating "
        "officer recorded statements from two witnesses at the scene."
    )
    return [f"{base} (sample chunk {i})" for i in range(n)]


async def _embed_before_sequential_paced(texts: list[str]) -> list[float]:
    """Exact reproduction of the pre-A3 code path — sequential, one
    request at a time, fixed 0.3s pacing between requests."""
    samples = []
    async with httpx.AsyncClient(timeout=90.0) as client:
        for i, text in enumerate(texts):
            start = time.perf_counter()
            response = await client.post(config.EMBEDDINGS_URL, json={"text": text, "is_query": False})
            response.raise_for_status()
            response.json()["embedding"]
            samples.append(time.perf_counter() - start)
            if i + 1 < len(texts):
                await asyncio.sleep(OLD_INTER_REQUEST_DELAY)
    return samples


async def main() -> None:
    if not config.EMBEDDINGS_URL:
        print("EMBEDDINGS_URL is not configured — cannot run a live embedding load test.")
        sys.exit(1)

    print(f"Model server: {config.EMBEDDINGS_URL}")
    print(f"Sample size: {SAMPLE_SIZE} real requests per leg (live, not simulated)\n")

    texts = _sample_texts(SAMPLE_SIZE)

    print("--- BEFORE (sequential, one request at a time, 0.3s pacing — the exact pre-A3 code path) ---")
    start = time.perf_counter()
    per_request = await _embed_before_sequential_paced(texts)
    before_wall_clock = time.perf_counter() - start
    before_throughput = len(texts) / before_wall_clock
    print(f"wall clock: {before_wall_clock:.2f}s  throughput: {before_throughput:.2f} texts/sec")
    print(f"mean per-request latency (network+model, excl. the 0.3s pacing): {statistics.mean(per_request):.2f}s")

    print(f"\n--- AFTER (bounded concurrency, max {config.EMBEDDING_MAX_CONCURRENCY} in flight — the real embed_texts()) ---")
    start = time.perf_counter()
    await embed_texts(texts, task_type="RETRIEVAL_DOCUMENT")
    after_wall_clock = time.perf_counter() - start
    after_throughput = len(texts) / after_wall_clock
    print(f"wall clock: {after_wall_clock:.2f}s  throughput: {after_throughput:.2f} texts/sec")

    speedup = before_wall_clock / after_wall_clock
    print(f"\nSpeedup (wall clock, {SAMPLE_SIZE} texts): {speedup:.1f}x")

    mean_request_latency = statistics.mean(per_request)
    print(f"\n{'='*70}\nProjected ingest throughput at corpus scale")
    print(f"(projected from the measured {mean_request_latency:.2f}s mean real per-request")
    print(f"latency above — NOT re-measured live at these volumes against the")
    print(f"shared model server)\n{'='*70}")
    for label, n in PROJECTION_SCALES.items():
        old_projected_seconds = n * (mean_request_latency + OLD_INTER_REQUEST_DELAY)
        new_projected_seconds = (n / config.EMBEDDING_MAX_CONCURRENCY) * mean_request_latency
        old_throughput = n / old_projected_seconds
        new_throughput = n / new_projected_seconds
        print(
            f"{label} ({n} chunks): BEFORE ~{old_projected_seconds/60:.1f} min "
            f"({old_throughput:.2f} texts/sec) -> AFTER ~{new_projected_seconds/60:.1f} min "
            f"({new_throughput:.2f} texts/sec) — {old_projected_seconds/new_projected_seconds:.1f}x"
        )


if __name__ == "__main__":
    asyncio.run(main())
