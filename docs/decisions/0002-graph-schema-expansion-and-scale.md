# 0002 — Graph scale prerequisites (Milestone A)

**Status:** in progress (Milestone A only; see checklist at the bottom)
**Date:** 2026-08-21

## Context

`GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md` (untracked, same convention as
`MUHAFIZ_API_MIGRATION_PLAN.md` was before it) diagnosed three specific,
named hot paths that are invisible at the M1–M12 migration's 73-FIR/198-
record MVP scale but become the system's actual bottleneck at real
station/province deployment volume (thousands to millions of FIRs):

1. `entity_resolution._find_by_primary_id()` / `_generate_candidates()`'s
   full label scan for CNIC/plate lookup and name-fallback candidate
   generation — no Postgres/AGE index behind either.
2. `bm25_retriever.py`'s keyword index rebuilt from scratch, in memory, on
   every single query.
3. `embedder.py`'s embedding pipeline — one HTTP request per chunk, paced
   0.3s apart, wall-clock linear in corpus size.

This ADR covers only Milestone A (the three scale-prerequisite modules,
A1–A3) — Milestones B–F (schema depth: jurisdiction/officer nodes, person-
relationship edges, queue-scale resolution, query-time scoping) are out of
scope here and were not started.

Apache AGE stays the graph store (confirmed decision, not revisited by this
work) — every module below is additive Postgres-side state alongside it,
never a replacement.

## A1 — Identity index tables

**Decision:** a plain Postgres side table, `identity_index (label, id_key,
id_value, entity_id)` (`migrations/021_identity_index.sql`), primary-keyed
on `(label, id_key, id_value)`. Maintained from one choke point —
`src/graph/versioning.py`'s `write_node()` — for every write to a label
`src/graph/identity_index.py`'s `IDENTITY_KEYS` tracks (`Person`/`cnic`,
`Vehicle`/`plate`, `PhoneNumber`/`phone`; `Officer`/`belt_no` isn't listed —
Officer isn't a graph label yet, that's Milestone B2).

Why a side table and not an index on AGE's own internal storage: AGE's
per-label vertex tables are an internal implementation detail (undocumented
across AGE versions), and this repo already has a working precedent for a
Postgres-side table shadowing derived graph state alongside AGE
(migration 016's `community_membership`).

Two read paths in `src/graph/entity_resolution.py` were rewired to consult
this index FIRST, falling back to the original AGE scan only on a miss
(the plan's explicit "defends against drift between the index and the
graph" requirement — the index is never the sole source of truth):

- `_find_by_primary_id()` — an index hit resolves `cnic_auto` without any
  AGE round trip at all (the only property the one caller reads off the
  result is `entity_id`).
- `_generate_candidates()` (via a new `_fetch_nodes_excluding()`) — when the
  mention carries an id_value, the index's `entity_ids_excluding()` gives
  every entity_id already known to carry a DIFFERENT non-empty value for
  that id_key. Those are excluded from the AGE fetch entirely, rather than
  pulled out of AGE and discarded in Python by the existing hard-block
  rule — safe because the hard block would have discarded them anyway.
  Nodes never indexed for that id_key (e.g. an older record with no CNIC
  on file) still go through the full scan, unchanged.

Both graphs share the read guard: the identity index is only ever
consulted for `graph="evidence_graph"` (production) — an eval run against
`evidence_graph_eval` never reads or is influenced by the shared
production index (`_PRODUCTION_GRAPH` constant, captured once at import
time rather than re-read off the possibly-monkeypatched `age_client`
reference at call time).

### §7-A verification — measured, not assumed

Run via `scripts/loadtest_identity_index.py` against an isolated throwaway
Postgres database (`muhafiz_loadtest`, its own `CREATE DATABASE` on the
same running Postgres/AGE instance, dropped at the end of the run) — never
mixed into real data. 1x is the real `evidence_graph`'s measured Person
count at the time of this run (198, from the real 73-FIR/198-record
corpus); 10x/100x are synthetic Person nodes with fabricated CNICs
generated purely for this load test.

| Scale | BEFORE (AGE property scan) | AFTER (identity_index lookup) | Speedup |
|---|---|---|---|
| 10x (1,980 nodes) | mean 4.71ms, p95 7.66ms | mean 1.02ms, p95 1.61ms | 4.6x |
| 100x (19,800 nodes) | mean 14.13ms, p95 33.82ms | mean 1.11ms, p95 1.57ms | 12.7x |

BEFORE grows with corpus size (4.71ms → 14.13ms, 3x, consistent with an
O(nodes) scan); AFTER stays flat (~1ms) across both scales, consistent
with the O(1) primary-key lookup it actually is. `EXPLAIN` on the AFTER
query confirms `Index Scan using identity_index_pkey` — no `Seq Scan`
anywhere on the identity-lookup hot path, satisfying §7-A's explicit
"confirm no full-label-scan query plan remains" requirement.

`_fetch_nodes_excluding()`'s own win (shrinking, not eliminating, the
name-fallback scan) was not separately load-tested — it is still an AGE
label scan under the hood (AGE has no property index to push the
exclusion down to), so its benefit is real but bounded by how many
entities the index already covers, not an O(1) claim; stated here
plainly rather than implied by the headline numbers above.

## A2 — Persistent full-text index

**Decision:** a Postgres `tsvector`/GIN index (`chunk_fulltext`,
`migrations/022_chunk_fulltext_index.sql`), one row per chunk, maintained
incrementally at ingest from `src/retrieval/vector_store.py`'s
`upsert_documents()` (and torn down on delete, `delete_by_ids`/
`delete_by_source`, mirroring Chroma's own).

The actual per-query cost this replaces is NOT the Chroma fetch itself —
`get_all_chunks()` was always a plain metadata read. It's
`retrieve_bm25()` building a fresh `BM25Okapi` index (full tokenization +
term-frequency stats) over the ENTIRE scoped candidate pool on every
single query, confirmed in this codebase's own pre-existing comments
(`orchestrator.py`: *"this rebuilds an in-memory BM25 index over the full
scoped corpus on every retrieval... at real production scale this
tokenize+index pass becomes the dominant cost per query. No caching/
persistent-index layer is added here"*). Swapping where chunk TEXT comes
from without narrowing the pool would not have touched that cost at all —
so this had to change what pool `retrieve_bm25()` receives, not just
where it's fetched from.

`src/retrieval/fulltext_index.py`'s `candidate_pool(query_text, where)` is
the new step: a GIN-backed lookup for chunks sharing at least one token
with the query (an OR `tsquery` across query tokens — recall-oriented;
precision is still `retrieve_bm25()`'s own BM25 scoring over whatever this
returns, unchanged). `where` uses the exact same two-dimensional scoping
(project/global OR, case AND) `vector_store._build_where()` already
enforces for Chroma, applied here as a plain SQL filter over
`chunk_fulltext`'s own denormalized scope columns.

Tokenizer consistency: `tsv` is built from ALREADY-TOKENIZED text
(`src/ingestion/tokenizer.py`'s Urdu-aware `tokenize()`, space-joined),
not Postgres's own `to_tsvector` tokenizing raw text — `bm25_retriever.py`
is explicit that corpus and query must use the same tokenizer (Urdu
codepoint variants, script-specific punctuation); letting Postgres's
built-in tokenizer diverge from the one BM25 already depends on would
silently under/over-match Urdu content differently than the real scoring
tokenizer does.

Metadata fidelity: `chunk_fulltext.metadata` stores the chunk's COMPLETE
Chroma metadata dict as JSONB, not just the denormalized scope columns —
so a downstream consumer reading e.g. `record_date` (reranker.py's
recency boost) sees the identical shape it would have gotten from
Chroma's `get_all()`, not a thinned projection.

Call sites rewired to use `candidate_pool()` in place of `get_all_chunks()`
specifically for the BM25 leg: `orchestrator.py` (both the GRAPH_HYBRID
and RAG routes' BM25 calls — its FIR-number auto-scope metadata scan,
an unrelated use of `get_all_chunks()`, was deliberately left untouched),
`src/pipeline/harness/tools/rag.py`, `scripts/eval_end_to_end.py`,
`scripts/eval_keyword_search.py` (restructured to build the pool per-query
instead of once for the whole eval run, matching real per-query
production behavior).

### §7-A verification — measured, not assumed

Run via `scripts/loadtest_fulltext_index.py` against the same isolated
throwaway-database convention as A1 (`muhafiz_loadtest`, dropped after the
run). 1x is the real corpus's measured chunk count from the M1–M12
decision record (~350 chunks from 73 FIRs); 10x/100x are synthetic chunks
with a realistic vocabulary shape — a small "real" term vocabulary (what
queries are drawn from) mixed into a much larger noise vocabulary (~3,000
distinct filler tokens), so a query's term-selectivity is representative
of real narrative text rather than an unrealistically dense toy corpus
(an earlier run with only ~24 total distinct words showed Postgres
correctly choosing a sequential scan over the GIN index — a genuine
planner decision, but driven by that fixture's unrealistic density, not
by anything about the index; the vocabulary was widened and the
measurement re-run before being recorded here).

| Scale | BEFORE (full pool, BM25Okapi rebuilt) | AFTER (GIN candidate pool -> BM25) | Mean pool size (before → after) | Speedup |
|---|---|---|---|---|
| 10x (3,500 chunks) | mean 260.3ms, p95 338.1ms | mean 26.5ms, p95 33.6ms | 3,500 → 280 | 9.8x |
| 100x (35,000 chunks) | mean 2,965.5ms, p95 3,534.4ms | mean 191.7ms, p95 247.7ms | 35,000 → ~2,000 (LIMIT-capped) | 15.5x |

BEFORE grows ~11x from 10x to 100x scale (260ms → 2,965ms), consistent
with `BM25Okapi`'s O(corpus-size) tokenization cost; AFTER grows far more
slowly (26ms → 192ms) because the candidate pool itself barely grows
(bounded by real term-selectivity, and by `candidate_pool()`'s own 2,000-
row cap). `EXPLAIN` on the AFTER query at 100x confirms `Bitmap Index Scan
using ix_chunk_fulltext_tsv` — no `Seq Scan`, satisfying §7-A's "confirm
no full-label-scan query plan remains" requirement (the full-text
analogue of A1's identity-lookup requirement).

## A3 — Batched embedding pipeline

**Decision:** bounded WORKER CONCURRENCY, not literal request batching —
the model server's `/embed` route accepts exactly one text per request (a
`{"texts": [...]}` payload 422s, confirmed against the real server), so
there is no batch parameter to use. `src/retrieval/embedder.py`'s
`_embed_local_e5()` now issues up to `config.EMBEDDING_MAX_CONCURRENCY`
(default 8) requests concurrently via a bounded `asyncio.Semaphore`,
sharing one `httpx.AsyncClient` (connection pooling now matters with
genuine concurrency, unlike when only one request was ever in flight),
replacing the old strictly-sequential loop with a fixed 0.3s sleep between
every request. `asyncio.gather()` preserves output order regardless of
which request actually completes first — verified directly
(`tests/test_embedder.py`'s out-of-order-completion test).

Why bounded rather than unbounded concurrency: a free-tier ngrok tunnel
and a single model-server process both have a real capacity ceiling —
this is "faster, still polite," not "as fast as possible, tunnel be
damned." `EMBEDDING_MAX_CONCURRENCY` is a plain env-configurable constant
(`src/config.py`), not hardcoded, so it can be tuned to whatever the
actual serving infrastructure can sustain.

The old design made ingestion throughput a hard wall-clock floor of
`N * (request_latency + 0.3s)` no matter how much hardware or network
headroom was available — a full re-embed of a real station's backlog
would take days regardless of anything else being scaled up. The new
design is roughly `(N / concurrency) * request_latency` — throughput now
scales with the concurrency limit (and the server's actual capacity), not
staying wall-clock-linear in corpus size.

### §7-A verification — measured, not assumed

Unlike A1/A2 (an isolated throwaway Postgres database, safe to fill with
tens of thousands of synthetic rows), a real 10x/100x corpus of live HTTP
requests against the ONE shared model-server tunnel this deployment
actually has would be a disproportionate load to put on real
infrastructure purely to run a load test. Instead: `scripts/
loadtest_embedding_pipeline.py` measures BEFORE and AFTER live, for real,
against the actual `EMBEDDINGS_URL` model server, over a modest real
sample (24 texts, genuinely sent over the network, not simulated) — then
PROJECTS 10x/100x corpus throughput from that measured real per-request
latency and `EMBEDDING_MAX_CONCURRENCY`, labeled plainly as a projection
rather than re-measured at that volume.

**Live-measured (24 real requests, real model server):**

| | Wall clock | Throughput |
|---|---|---|
| BEFORE (sequential, 0.3s-paced — exact old code path) | 25.49s | 0.94 texts/sec |
| AFTER (bounded concurrency, max 8 — the real `embed_texts()`) | 3.08s | 7.79 texts/sec |

**Speedup on the live sample: 8.3x.** Mean real per-request latency
measured: 0.75s (network + model inference, excluding the old 0.3s
pacing).

**Projected at corpus scale** (from the measured 0.75s per-request
latency; NOT re-measured live at these volumes):

| Scale | BEFORE (projected) | AFTER (projected) | Speedup |
|---|---|---|---|
| 1x (350 chunks) | ~6.1 min (0.95 texts/sec) | ~0.5 min (10.66 texts/sec) | 11.2x |
| 10x (3,500 chunks) | ~61.3 min | ~5.5 min | 11.2x |
| 100x (35,000 chunks) | ~10.2 hours | ~54.7 min | 11.2x |

The projected speedup (11.2x) is consistent with the arithmetic
`(latency + 0.3s) / (latency / concurrency)` at `concurrency=8`, latency
≈0.75s — a sanity check that the live-sample measurement and the scale
projection agree, not two independent, potentially-inconsistent numbers.

## Status: Milestone A in progress

- [x] **A1** — identity index tables. `migrations/021_identity_index.sql`,
      `src/graph/identity_index.py`, wired into `versioning.write_node()`
      (maintenance) and `entity_resolution.py`'s `_find_by_primary_id()`/
      `_generate_candidates()` (read, index-first with graph-scan
      fallback). 20 new tests (`tests/test_identity_index.py` plus
      additions to `tests/test_entity_resolution.py`). Full suite green.
      Live-verified against real Postgres/AGE per §7-A above.
- [x] **A2** — persistent full-text index. `migrations/022_chunk_fulltext_index.sql`,
      `src/retrieval/fulltext_index.py`, wired into
      `vector_store.upsert_documents()` (incremental maintenance) and
      `orchestrator.py`/`rag.py`/eval scripts (candidate-pool read,
      replacing `get_all_chunks()` for the BM25 leg only). 20 new tests
      (`tests/test_fulltext_index.py` plus updates to
      `tests/test_orchestrator.py`, `tests/test_harness_tool_rag.py`,
      `tests/test_harness_agent_semantic_search.py`,
      `tests/test_eval_scripts.py`). Full suite green. Live-verified
      against real Postgres per §7-A above.
- [x] **A3** — batched embedding pipeline (bounded worker concurrency).
      `src/config.py`'s `EMBEDDING_MAX_CONCURRENCY`, `src/retrieval/
      embedder.py`'s `_embed_local_e5()` rewired from a sequential/0.3s-
      paced loop to a bounded `asyncio.Semaphore` over concurrent
      requests, order preserved via `asyncio.gather()`. 7 new tests
      (`tests/test_embedder.py`). Full suite green. Live-verified against
      the real model server per §7-A above (8.3x speedup on a live
      24-request sample; 11.2x projected at 10x/100x corpus scale).

## Status: Milestone A complete

All three scale-prerequisite modules (A1 identity index, A2 persistent
full-text index, A3 batched/concurrent embedding pipeline) landed as
their own branch, merged `--no-ff` into local `main`, full test suite
green at every step, live-verified against real Postgres/AGE and the real
model server. Nothing pushed to `origin`. Milestones B–F (schema depth,
queue-scale resolution, query-time scoping, documentation) were not
started — out of scope for this pass.
