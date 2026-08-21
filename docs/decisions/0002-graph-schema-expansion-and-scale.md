# 0002 — Graph scale prerequisites and schema expansion (Milestones A–C)

**Status:** in progress (Milestones A, B, and C complete; see checklists
at the bottom of each section) **Date:** 2026-08-21

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

This ADR covers Milestones A (the three scale-prerequisite modules, A1–A3),
B (schema depth: jurisdiction/officer nodes), and C (the remaining
structured-field gaps: person-relationship edges, chalaan name
resolution, zimni officer/position timeline, cross-version edge, typed
recovered property, witness home jurisdiction, and the ethnicity/religion
governance record) — Milestones D–F (queue-scale resolution, query-time
scoping, documentation) are out of scope here and were not started.

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

## B1 — Jurisdiction graph nodes

**Decision:** `PoliceStation`/`District` as real vertex labels
(`migrations/023_jurisdiction_graph_labels.sql`), populated from
`FirRecord.police_station`'s nested `{id, name, code, district}` object
(the Muhafiz Data API's stand-in for `psrms.police_station`/
`psrms.district`) — `Case-[FILED_AT]->PoliceStation-[PART_OF]->District`,
written by a new `_write_jurisdiction()` step in
`src/graph/structured_projection.py`'s `project_fir()`, alongside every
other structural write that function already makes for every FIR. `PART_OF`
is a second, semantically-consistent reuse of the existing edge label
(first written for `Incident->Case`) — no new edge label needed for it.

Because `project_fir()` runs for every FIR on every `--full` re-sync (M9),
this backfills the relationship for every pre-existing Case, not only newly
ingested ones, exactly as the plan requires — no separate backfill script.
Idempotency is two mechanisms working together, not one: `write_node()`'s
MERGE means re-running the same FIR's station/district writes refreshes the
same node rather than duplicating it; `write_edge()` is a bare append-only
CREATE, so `FILED_AT` was added to `scripts/sync_muhafiz_data.py`'s
`EDGE_LABELS` purge list (the same purge-by-source-doc-id-prefix step that
already keeps every other structured edge type from duplicating on
re-sync) — without that addition, a second `--full` run would have
duplicated every `FILED_AT` edge.

Station identity key: `FirRecord.police_station_id` (the FIR's own FK, the
canonical identifier) first, falling back to the nested object's own `id`,
falling back to `name` as a last resort — deterministic across
re-projection, and two FIRs at the same real station MERGE onto one shared
node rather than minting one each. District tolerates both shapes measured
live: a nested `{id, name, province}` object, or (per
`muhafiz_records.py`'s own `_station_district()`) a bare string — keyed the
same way (id-or-name fallback). A FIR with no station data on file writes
no jurisdiction nodes at all, rather than fabricating one.

### Access control — addressed explicitly, not left implicit

Station/district-scoped traversal ("every case filed at this station",
`retrieve_jurisdiction_cases()` in `src/retrieval/graph_retriever.py`) is a
**broader** enumeration capability than a single cross-case entity-link
hop, even though it only reads jurisdiction metadata — so it reuses the
exact same cross-case role gate `retrieve_graph(cross_case=True, ...)`
already enforces (`supervisor`/`station-admin`/`platform-admin` only),
rather than getting a looser tier of its own or a second, parallel check
that could drift out of sync with it (`SUBAGENT_INTERFACES.md`'s existing
warning against a third gate, already heeded once for
`xgraph_tool`/`xnetwork_tool`).

Concretely: the role-check-and-audit-log logic that used to live inline in
`retrieve_graph()` was extracted, unchanged, into
`_enforce_cross_case_role_gate()` — a single function both
`retrieve_graph()`'s cross-case branch and `retrieve_jurisdiction_cases()`
call. This is not "the same behavior reimplemented twice, kept in sync by
convention" (the drift risk the warning is about) — it is the literal same
function, traceable at both call sites, so there is exactly one place this
check could ever be wrong. A denied jurisdiction-scoped query writes the
identical `authorization_violation` audit record the existing gate already
writes (same `event_type` string, same `gateway.log_audit_event()` call);
`tests/test_graph_retriever.py`'s
`TestJurisdictionScopedTraversalReusesTheGate` asserts this with one fake
gateway capturing both call sites' denials, not by asserting on either
site in isolation.

`retrieve_jurisdiction_cases()` itself is deliberately narrow: given
`station_id` and/or `district_id`, it returns the matching `case_ids` —
metadata enumeration, not an entity/evidence traversal, so it carries no
chunk/hop/confidence provenance the way `retrieve_graph()`'s return does.
Wiring it into a harness tool/sub-agent so a real query can reach it
(query-scope preclassification) is Milestone E1's job, explicitly out of
scope here — this module only makes the capability exist, correctly
gated, for E1 to compose later.

### §7-B verification — measured, not assumed

Cypher assertion run against the real Postgres/AGE instance
(`muhafiz-postgres`) after a `--full` re-sync of the complete 73-FIR
corpus (`scripts/sync_muhafiz_data.py --full`, against the recorded
snapshot — the same fixture the unit tests use, this time exercised live):

```
MATCH (c:Case) WHERE NOT exists((c)-[:FILED_AT]->()) RETURN c.case_id AS case_id
```

(AGE's Cypher parser rejects an anonymous-labeled node directly inside a
`NOT (...)` pattern — `WHERE NOT (c)-[:FILED_AT]->(:PoliceStation)` raises
a Postgres syntax error at the label colon; `exists()` wrapping the
pattern is the form AGE actually accepts. Confirmed empirically, not
assumed, while writing this verification query — the same
"reconciled-against-the-code, not the docs" discipline the rest of this
milestone follows.)

**Result: 0 cases without a `FILED_AT` edge, out of 73 total `Case`
nodes — full coverage**, including every pre-existing Case from before B1
landed (the corpus predates this module entirely; the backfill claim is
what this number confirms, not a coincidence of only testing newly-added
cases).

No-duplication check — ran the exact same `--full` re-sync a SECOND time
against the same live instance (the plan's explicit "verify it doesn't
duplicate or orphan anything on a second run" requirement), then re-ran:

```
MATCH (c:Case)-[r:FILED_AT]->(:PoliceStation) RETURN c.case_id AS case_id, count(r) AS n
```

**Result: identical after both runs — 73 total `FILED_AT` edges, 0 cases
with more than one.** `PoliceStation`/`District` node counts (19/9) were
also unchanged between the two runs, confirming `write_node()`'s MERGE
is doing its job — re-projecting the same station/district data twice
never mints a second node. (`scripts/sync_muhafiz_data.py`'s purge-by-
source-doc-id-prefix step, with `FILED_AT` added to its `EDGE_LABELS`
list, is what makes the edge side of this idempotent — see the purge
step's own module docstring.)

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

## B2 — Officer identity resolution

**Decision:** `Officer` as a real vertex label
(`migrations/024_officer_graph_labels.sql`), resolved by `belt_no` — added
to `entity_resolution.py`'s `TYPE_TO_LABEL`/`TYPE_PRIMARY_ID_KEY` as a
fourth entity type alongside `person`/`vehicle`/`phone`, so an officer with
a known `belt_no` gets the identical `cnic_auto`-equivalent exact-match
tier CNIC-based Person resolution already gets (never scored by name
similarity against a candidate with a DIFFERENT non-empty `belt_no` —
the same hard-block invariant, same mechanism, not a parallel one).
`belt_no` was also added to `src/graph/identity_index.py`'s
`IDENTITY_KEYS` in this same module (not deferred to a follow-up, per the
plan's explicit instruction and that file's own comment anticipating this
exact addition) — so Officer resolution gets A1's O(1) identity-index
lookup from day one, not a temporary full-label-scan gap.

`ASSIGNED_TO(role, assigned_from, assigned_to)` edges (Officer->Case)
replace the Postgres `Case.investigation_officer` column's collapsed
"current officer" string as the graph's own record of who has held a
case — that Postgres column itself is untouched (out of scope; this is a
graph-side addition, not a schema migration on `cases`). Two sources, one
write path (`src/graph/structured_projection.py`'s new `_write_officers()`
step, wired into `project_fir()`):

- **Investigating officers** (`fir_investigating_officer` child rows) — a
  real reassignment case exists in the live data
  (`fir-205-26`: belt `1854L` from 2026-02-15, superseded by belt
  `GEN-0105` from 2026-06-22). Rows are sorted by `assigned_from` and
  written as a SUPERSESSION CHAIN: each later officer's `ASSIGNED_TO` edge
  passes the previous officer's edge id as `versioning.write_edge()`'s
  `supersedes_edge_id` — the prior edge is never deleted, only marked
  `superseded_by`, so "who is investigating this case NOW" (the edge with
  no `superseded_by`) and "who ever has" (the full chain) are both still
  answerable, matching `OCCURRED_ON`'s own append-only precedent rather
  than inventing a second versioning idiom.
- **Recording officer** (`FirRecord.recording_officer_*` fields — a single
  officer per FIR, no history rows in the source data) — one `ASSIGNED_TO`
  edge, `role="recording"`, `assigned_from` from the FIR's own
  `report_datetime`. Nothing to supersede against, so no chain.

Idempotency: `ASSIGNED_TO` was added to `scripts/sync_muhafiz_data.py`'s
`EDGE_LABELS` purge list, same mechanism as B1's `FILED_AT` — a `--full`
re-sync purges and rebuilds the whole chain for a FIR from its own
source-doc-id prefix, rather than needing the supersede mechanism to
additionally guard against a second sync run duplicating the chain.

### §7-B verification — measured, not assumed

Run against the real Postgres/AGE instance (`muhafiz-postgres`), after the
same two `--full` re-syncs of the complete 73-FIR corpus B1 verified
against above (both runs write B1 and B2 output together — one sync pass
projects the whole FIR):

```
MATCH (o:Officer)-[r:ASSIGNED_TO]->(c:Case {case_id: 'fir-205-26'})
RETURN o.belt_no AS belt_no, r.role AS role, r.assigned_from AS assigned_from, r.superseded_by AS superseded_by
```

**Result — identical after both runs, confirming the real reassignment
case (`fir-205-26`) round-trips correctly through a full purge-and-rebuild
without gaining or losing a row:**

| belt_no | role | assigned_from | superseded_by |
|---|---|---|---|
| 1854L | recording | 2026-02-15T14:20:00Z | (none) |
| 1854L | investigating | 2026-02-15 | *(set — points at the GEN-0105 edge below)* |
| GEN-0105 | investigating | 2026-06-22 | (none) |

Three rows, not two: the recording-officer edge (belt `1854L`, from the
FIR's own `recording_officer_belt_no`) is a separate fact from the
investigating-officer chain — this FIR's recording officer and its FIRST
investigating officer happen to be the same real person, which is exactly
why `Officer` resolution (not a fresh node per role) is what keeps both
edges pointing at one shared `Officer` node rather than two. The
investigating-officer chain itself is exactly the required ">1 row"
shape: the `1854L` edge has a non-null `superseded_by` (it is superseded,
never deleted), and exactly one edge (`GEN-0105`'s) has none — the
unambiguous "current investigating officer."

Corpus-wide counts, unchanged between the two `--full` runs (same
no-duplication property B1 verified for `FILED_AT`, confirmed here for
`ASSIGNED_TO`): 76 `Officer` nodes, 144 `ASSIGNED_TO` edges total.

## Status: Milestone A complete

All three scale-prerequisite modules (A1 identity index, A2 persistent
full-text index, A3 batched/concurrent embedding pipeline) landed as
their own branch, merged `--no-ff` into local `main`, full test suite
green at every step, live-verified against real Postgres/AGE and the real
model server. Nothing pushed to `origin`. Milestones B–F (schema depth,
queue-scale resolution, query-time scoping, documentation) were not
started — out of scope for this pass.

## Status: Milestone B complete

- [x] **B1** — jurisdiction graph nodes. `migrations/023_jurisdiction_graph_labels.sql`,
      `src/graph/structured_projection.py`'s `_write_jurisdiction()`
      (Case-[FILED_AT]->PoliceStation-[PART_OF]->District), and
      `src/retrieval/graph_retriever.py`'s `retrieve_jurisdiction_cases()`
      + the extracted `_enforce_cross_case_role_gate()` shared with
      `retrieve_graph()`'s cross-case branch. New/updated tests in
      `tests/test_structured_projection.py` and `tests/test_graph_retriever.py`.
      Full suite green. Live-verified against real Postgres/AGE per §7-B
      above (73/73 Case coverage, no duplication across two `--full`
      re-syncs).
- [x] **B2** — officer identity resolution. `migrations/024_officer_graph_labels.sql`,
      `Officer`/`belt_no` added to `entity_resolution.py`'s
      `TYPE_TO_LABEL`/`TYPE_PRIMARY_ID_KEY` and `identity_index.py`'s
      `IDENTITY_KEYS`, `src/graph/structured_projection.py`'s new
      `_write_officers()` (investigating-officer supersession chain +
      single recording-officer edge). New/updated tests in
      `tests/test_entity_resolution.py`, `tests/test_identity_index.py`,
      `tests/test_structured_projection.py`. Full suite green. Live-verified
      against real Postgres/AGE per §7-B above — the real `fir-205-26`
      reassignment case round-trips correctly through two full re-syncs,
      144 `ASSIGNED_TO` edges total, unchanged between runs.

Both B1 and B2 landed as their own branch (`feature/jurisdiction-graph-
nodes`, `feature/officer-identity-resolution`), merged `--no-ff` into
local `main`, full test suite green at every step, live-verified against
the real Postgres/AGE instance. Nothing pushed to `origin`.

## Milestone C — closing the remaining structured-field gaps

Before starting: `muhafiz-postgres` was confirmed actually reachable, not
assumed still up from Milestone B — a real query against `evidence_graph`
(`MATCH (c:Case) RETURN count(c)`) returned 73, matching B1's own recorded
count, before any C-module work began.

## C1 — Person-relationship edges

**Decision:** `RELATED_TO{role}` elabel (`migrations/025_related_to_label.sql`),
written directly (confidence=1.0, no SAME_AS/pending step) from two sources:

- **`fir_accused.relationship_to_victim`/`relationship_to_complainant`**
  (`src/graph/structured_projection.py`'s `_write_accused()`/
  `_write_related_to()`) — direction is always accused -> victim/
  complainant, `role` carrying the raw relationship text as recorded (e.g.
  "اجنبی", "بھائی"). The victim side needed a new write path that didn't
  exist before this module: `fir.victim_name` is a bare name with no CNIC
  and no separate victim table on the schema (flagged the same way as
  `cross_version` — "NOT OBSERVED... added on direct instruction"), so
  `_write_victim()` resolves it through the exact same
  `resolve_structured_person()` corroboration-gate path every other
  no-CNIC structured mention in this module already uses, rather than a
  new, looser path just because this is the one caller with nothing but a
  name. `_write_victim()`/`_write_complainant()` now return their
  resolved `entity_id` (previously `None`) so `_write_accused()` has
  something to point `RELATED_TO` at.
- **PKM `tenant_registration`(owner/tenant)/`employee_registration`
  (employer/employee)** (`src/graph/cross_silo_projection.py`'s new
  `_write_pkm_relationship()`) — `role="landlord_of"`/`"employer_of"`.
  Never case-scoped (same as `_write_pkm_vehicle()`'s `REGISTERED_TO`):
  neither person is minted here, an edge is written only when BOTH sides
  already resolve to an EXISTING Person by CNIC via
  `entity_resolution._find_by_primary_id()` — the identical soft-reference
  discipline `REGISTERED_TO`/criminal-record linking already use. Minting
  a fresh caseless Person here would bypass the corroboration gate
  entirely (there is no case to corroborate against), so an unresolved
  owner/tenant/employer/employee is simply not linked, not fabricated.

  **Shape not observed live** — 0 of 14 real PKM applications in the
  snapshot are either service type. `PkmApplication.owner`/`.tenant`/
  `.employer`/`.employee` (new properties, `src/data_gateway/muhafiz_api/models.py`)
  read top-level `owner`/`tenant`/`employer`/`employee` keys, inferred by
  direct analogy to `applicant` (the one nested-person enrichment
  confirmed live on every application) and API_CONSUMER_GUIDE.md's own
  text ("It also includes available applicant, police-station, employee,
  tenant... references" — listed as its own top-level references, the
  same tier as `applicant`/`police_station`). Flagged here the same way
  the schema itself flags `victim_name`/`cross_version` as inferred, not
  confirmed — tested against a constructed fixture
  (`tests/test_cross_silo_projection.py`'s `TestProjectPkmRelationships`),
  same disclosure as C4's `cross_version` test below.

Idempotency: `RELATED_TO` added to `scripts/sync_muhafiz_data.py`'s
`EDGE_LABELS` purge list, same mechanism as B1's `FILED_AT`/B2's
`ASSIGNED_TO`.

### §7-B verification — measured, not assumed

Cypher assertion run against the real Postgres/AGE instance
(`muhafiz-postgres`) after a `--full` re-sync of the complete 73-FIR
corpus:

```
MATCH (a:Person)-[r:RELATED_TO]->(b:Person)
RETURN a.canonical_name, r.role, b.canonical_name, r.source_doc_id
```

**Result: 24 `RELATED_TO` edges**, every one from `fir_accused`'s
relationship fields (no live tenant/employee_registration data to
exercise the PKM path, per the "shape not observed live" note above).

**Hand-checked sample** (`fir-214-26`, cross-checked directly against
`tests/fixtures/muhafiz_api_snapshot.json`): complainant `ارشد حسین`,
victim `صابر حسین`; both accused (`شہزیب عرف شابی`, `بلال عرف بلو`) carry
`relationship_to_victim="اجنبی"`, `relationship_to_complainant="اجنبی"`
on the source record — and the graph shows exactly the 4 edges this
predicts: each accused -[RELATED_TO{role:"اجنبی"}]-> both the victim and
the complainant, source_doc_id `psrms/fir/fir-214-26#structured`. Not a
count-only check — the edge endpoints, direction, and role text were
read back against the real source record they came from.

No-duplication check — ran the exact same `--full` re-sync a SECOND time
against the same live instance, then re-ran the count query: **24
`RELATED_TO` edges, identical after both runs** (73/73 `Case` nodes also
unchanged), confirming the purge-list addition is doing its job.

## C2 — Chalaan name resolution

**Decision:** `chalaan_dispatch.accused_names`/`witness_names` resolved
back to this FIR's own already-written `Person` nodes via
`APPEARS_IN{role: "chalaan_accused"|"chalaan_witness", surface_text}`
edges (`src/graph/structured_projection.py`'s new
`_write_chalaan_name_links()`, called from `_write_structured_records()`
only for the `chalaan_dispatch` table).

Reuses the EXACT SAME in-FIR-only name-matching pattern
`weapon_register.recovered_from` already uses — `_write_weapons()`'s
`accused_by_name` dict, built during `_write_accused()` — plus a new
parallel `witness_by_name` dict built the same way during
`_write_witnesses()` (that function now takes the dict as a parameter and
populates it, mirroring `_write_accused()`'s existing
`accused_by_name` parameter exactly). Both dicts are local to one
`project_fir()` call — never a global/cross-FIR name lookup. A name that
doesn't match any entry in THIS FIR's own dict is simply left unresolved,
never looked up elsewhere in the graph — the same discipline that guards
`weapon_register.recovered_from` against the corpus's name collisions.
`APPEARS_IN` (not a new edge label) is reused for "this record names this
person," the same label already used for "this entity appears in this
document" elsewhere in the module — edge-label reuse, same precedent as
B1's `PART_OF`.

Name-list splitting (`_split_name_list()`) uses the same Urdu-or-ASCII
comma class `structured_fields.py` already splits section-reference lists
on (not imported from there — that module's split regex is scoped to a
different field — but the same two-character class), needed because the
real data mixes both: `"محمد عدنان, عمران قریشی"` (ASCII) alongside
`"طارق، عدنان"` (Urdu) in the same corpus.

No new elabel/migration needed — `APPEARS_IN` already exists and is
already in `scripts/sync_muhafiz_data.py`'s `EDGE_LABELS` purge list.

### §7-B verification — measured, not assumed

Cypher assertion run against the real Postgres/AGE instance after a
`--full` re-sync of the complete 73-FIR corpus:

```
MATCH (p:Person)-[r:APPEARS_IN]->(sr:StructuredRecord)
WHERE r.role IN ['chalaan_accused','chalaan_witness']
RETURN p.canonical_name, r.role, r.surface_text, sr.record_id
```

**Result: 57 name-resolution edges.**

**Hand-checked sample** (`fir-214-26`'s `CD-C2-1`, cross-checked against
`tests/fixtures/muhafiz_api_snapshot.json`): `accused_names="شہزیب عرف
شابی، بلال عرف بلو"`, `witness_names="بلال احمد، عبدالستار"` — the graph
shows exactly these 4 names resolved, each `surface_text` matching the
source record's own name string, each `role` matching which field it
came from.

**A genuine corpus artifact surfaced by this verification, not a defect
in this module:** one edge's `surface_text` ("بلال", `fir-202-26`'s
witness) points at a `Person` node whose `canonical_name` now reads
"فیصل" — traced directly: `fir-202-26`'s witness "بلال" and
`fir-401-26`'s accused "فیصل" share the identical CNIC
(`00000-9000007-1`) in the synthetic corpus, so `entity_resolution.py`'s
pre-existing `cnic_auto` tier (unrelated to this module, unchanged by it)
correctly merges them into one canonical node — the same hard-block/
auto-merge behavior every other CNIC-keyed write in this codebase already
relies on. `surface_text` on the edge preserves the real chalaan-record
name regardless of which write to the shared node happened to land last
on `canonical_name` — this is exactly why `surface_text` is tracked
separately from the node's own property, same reasoning
`_write_weapons()`'s `APPEARS_IN{surface_text}` already applies.

No-duplication check — ran the exact same `--full` re-sync a SECOND time
against the same live instance: **57 edges, identical after both runs**
(73/73 `Case` nodes also unchanged).

## C3 — Zimni officer and position timeline

**Decision:** two independent changes in `src/graph/structured_projection.py`,
same module because both touch `_write_occurred_on()`/`_write_officers()`:

- **`fir_zimni.officer_name` -> Officer identity**
  (`_write_zimni_officers()`, called from `_write_officers()`) — reuses
  B2's `entity_resolution.resolve_and_write("officer", ...)` path
  DIRECTLY, the same call `_write_investigating_officers()`/
  `_write_recording_officer()` already make, not a parallel matching
  mechanism. `fir_zimni` rows carry no `belt_no` (only
  `fir_investigating_officer`/`recording_officer_*` do), so every one of
  these resolves through entity_resolution's ordinary name-fallback
  tiering — ordinary behavior for a belt_no-less officer mention, not a
  gap. An `Officer-[OCCURRED_ON{event_type:"zimni_entry", detail}]->Date`
  edge is written alongside the existing `Incident`'s own zimni
  `OCCURRED_ON` edge (same date, same event_type, different `from_label`)
  — the same idiom `OCCURRED_ON` already uses for Person's `arrest_date`,
  not a new edge label.
- **`fir_position` -> full dated timeline** — `_write_occurred_on()` now
  writes `Incident-[OCCURRED_ON{event_type:"position", detail:position}]->Date`
  for every row with a `status_date`, consistent with how `OCCURRED_ON`
  already handles multiple dated events per Incident (zimni entries,
  chalaan dispatch) rather than collapsing to one. `fir_position` was also
  added to `_STRUCTURED_RECORD_TABLES` so its non-dated fields
  (`prosecutor_name`, `cross_certificate_ref`,
  `pending_challan_objections`, `remarks`) get the same full-field
  `StructuredRecord` capture every other typed row with no identity of
  its own already gets — a complementary addition beyond the plan's literal
  ask, needed so a dateless `fir_position` row's other fields aren't
  silently dropped just because it can't join the dated timeline.
  `muhafiz_cases.py`'s `_current_status()` (feeding the separate Postgres
  `Case.investigation_status` column) is untouched — same "graph-side
  addition, source column stays as-is" precedent B2 set for
  `investigation_officer`.

No new elabel/migration — `Officer`/`OCCURRED_ON` already exist.

### §7-B verification — measured, not assumed

Cypher assertions run against the real Postgres/AGE instance after a
`--full` re-sync of the complete 73-FIR corpus:

```
MATCH (o:Officer)-[r:OCCURRED_ON {event_type:'zimni_entry'}]->(d:Date)
RETURN o.canonical_name, d.date, r.detail
```

**Result: 62 Officer-dated zimni edges.** Several officers carry more
than one (e.g. "طارق جمالی": entries 1/2/3 on 2026-02-11/12/13), the
required ">1 row wherever the source data shows it" shape.

```
MATCH (i:Incident)-[r:OCCURRED_ON {event_type:'position'}]->(d:Date)
RETURN d.date, r.detail
```

**Result: 94 dated position edges** (matching all 94 `fir_position` rows
in the corpus — every row in this dataset happens to carry a
`status_date`, even the 65/94 with a null `position` text, which land
with `detail=""` rather than being silently skipped).

**Hand-checked sample** (`fir-205-26`, cross-checked against
`tests/fixtures/muhafiz_api_snapshot.json`): zimni entries 1/2 dated
2026-02-15/2026-03-05 with `officer_name="(نامزد ASI)"`, `fir_position`
rows dated 2026-03-05/2026-06-27 — the graph shows exactly these edges,
same dates, same officer, same detail text.

No-duplication check — ran the exact same `--full` re-sync a SECOND time:
**62 Officer-zimni edges, 94 Incident-position edges, 94 `fir_position`
StructuredRecord nodes — all identical after both runs** (73/73 `Case`
nodes also unchanged).

## C4 — Cross-version edge

**Decision:** `CROSS_VERSION_OF{filed_by}` elabel
(`migrations/026_cross_version_of_label.sql`), written DIRECTLY
(confidence=1.0, no `status: "pending"`) from `psrms.cross_version`'s
`related_fir_display_code` (a soft reference to the other, independently
registered FIR) — same "structured field, no heuristic" tier as the
CNIC-based criminal-record join, distinct from `CITES` (a regex hit
against free prose, always `pending`, no confidence scoring here because
there is nothing to score).

`src/graph/cross_silo_projection.py`'s new `project_fir_cross_versions()`
resolves `related_fir_display_code` through the exact same
`display_code_index` (`muhafiz_cases.build_display_code_index()`) PKM's
`forwarded_fir_number` and CITES' cited-code resolution already use — not
a third lookup mechanism. Same ordering dependency as `CITES`: run as its
own pass in `scripts/sync_muhafiz_data.py` (`sync_cross_versions()`,
mirroring `sync_citations()`) after every FIR's `Case` node already
exists; `CROSS_VERSION_OF` added to the `EDGE_LABELS` purge list, same
idempotency mechanism as every other Milestone B/C edge type.

**0 populated `cross_version` rows in the live dataset**
(`muhafiz_schema.dbml.txt`: "NOT OBSERVED as a populated tab in the
source material") — tested against a constructed fixture
(`tests/test_cross_silo_projection.py`'s `TestProjectFirCrossVersions`),
per the plan's own instruction, rather than a real-snapshot count lock
like CITES's 9-example M6b test.

### §7-B verification — measured, not assumed

No real rows exist to run a live Cypher count against (confirmed:
`TestProjectFirCrossVersions.test_measured_count_against_real_snapshot`
— 0 of 73 real FIRs carry a `cross_version` row). Instead, the fixture
test was additionally run LIVE, not just against fakes, matching this
milestone's "run these against the real live Postgres/AGE instance" bar:
a temporary snapshot was built by injecting ONE constructed
`cross_version` row onto a real FIR (`fir-1001-26`, `related_fir_display_code`
pointing at the real `fir-117-26`, `filed_by="accused side"`), then
`scripts/sync_muhafiz_data.py --full` was run against that temporary
snapshot on the real `muhafiz-postgres` instance:

```
MATCH (a:Case)-[r:CROSS_VERSION_OF]->(b:Case)
RETURN a.case_id, b.case_id, r.filed_by
```

**Result: exactly the 1 constructed edge**, `fir-1001-26 -> fir-117-26`,
`filed_by="accused side"` — matching the injected fixture row exactly.
Re-ran the same `--full` sync against the same temporary snapshot a
SECOND time: **still 1 edge, no duplication** (73/73 `Case` nodes
unchanged both times). The live instance was then restored to the real,
unmodified 73-FIR snapshot with a final `--full` re-sync — the purge-by-
source-doc-id-prefix step correctly removed the fixture-only edge (the
real data has no `cross_version` row for `fir-1001-26`), confirmed back
to 0 `CROSS_VERSION_OF` edges, with every other Milestone B/C edge count
(e.g. `RELATED_TO` at 24) unchanged by the round-trip.

## C5 — Typed recovered property

**Decision:** `malkhana_register.item_detail` classified at write time
(`src/graph/structured_projection.py`'s new
`_classify_and_write_malkhana_item()`, called from
`_write_structured_records()` for the `malkhana_register` table only) —
a value shaped like a vehicle plate or phone number resolves into the
existing `Vehicle`/`PhoneNumber` node type via
`entity_resolution.resolve_and_write()`, INSTEAD OF a generic
`StructuredRecord` (matching the plan's literal wording — the row's
generic write is skipped, not duplicated alongside the typed one).
Everything else (cash, generic exhibits — everything actually observed
live) stays a `StructuredRecord`, unchanged.

Reuses `structured_fields.py`'s existing `extract_plates()`/
`extract_phones()` (built on that module's own `_PLATE_RE`/`_PHONE_RE`)
— no new pattern matchers, per the plan's explicit instruction. Plate is
checked before phone, deterministically (not a coin-flip) for a detail
string that happens to contain both shapes.

`entity_resolution.resolve_and_write("vehicle"/"phone", ...)` is the
EXACT SAME call `src/ingestion/service.py` already makes for free-text
plate/phone extraction — not a second node-minting scheme. This matters
concretely: `TYPE_PRIMARY_ID_KEY`'s `cnic_auto`-equivalent exact-match
tier (keyed on `"plate"`/`"phone"`) means a malkhana-recovered plate
correctly MERGEs onto an EXISTING `Vehicle` node carrying that plate
regardless of which path created it first — a PKM `vehicle_verification`
application's deterministic `VEHICLE-{plate}` node
(`cross_silo_projection.py`'s `_vehicle_entity_id()`) included, since the
lookup matches on the `plate` PROPERTY, not the `entity_id` naming
convention. One additional explicit
`APPEARS_IN{role: "recovered", surface_text}` edge is written on top of
`resolve_and_write()`'s own generic (role-less) `APPEARS_IN`, to carry
the "recovered in this malkhana entry" fact the generic edge doesn't.

No new elabel/migration — `Vehicle`/`PhoneNumber`/`APPEARS_IN` already
exist.

### §7-B verification — measured, not assumed

**0 real `malkhana_register.item_detail` values are plate/phone-shaped**
in the live dataset — every one of the 73 FIRs' entries is descriptive
text ("ایک عدد موبائل فون", "نقدی رقم", "چرس", ...), confirmed by running
the real classifier against every real `item_detail` string in
`tests/fixtures/muhafiz_api_snapshot.json`. Live-verified the same way as
C4: a temporary snapshot was built by injecting two constructed
`malkhana_register` rows onto a real FIR (`fir-1001-26`) — one
plate-shaped (`"ICT-LE-309 برآمد"`), one phone-shaped
(`"0300-1234567 نمبر برآمد"`) — then `scripts/sync_muhafiz_data.py --full`
was run against it on the real `muhafiz-postgres` instance:

```
MATCH (v:Vehicle {plate:'ICT-LE-309'})-[r:APPEARS_IN {role:'recovered'}]->(:Document)
RETURN v.entity_id, r.surface_text
MATCH (p:PhoneNumber {phone:'0300-1234567'})-[r:APPEARS_IN {role:'recovered'}]->(:Document)
RETURN p.entity_id, r.surface_text
```

**Result: exactly 1 edge each**, `surface_text` matching the injected
detail string exactly (`"ICT-LE-309 برآمد"`/`"0300-1234567 نمبر برآمد"`).
Re-ran the same `--full` sync against the same temporary snapshot a
SECOND time: **still 2 `recovered`-role edges total, 1 `Vehicle` node for
that plate — no duplication** (73/73 `Case` nodes unchanged both times).
The live instance was then restored to the real, unmodified 73-FIR
snapshot — the purge-by-source-doc-id-prefix step correctly removed both
fixture-only edges (confirmed back to 0 `recovered`-role `APPEARS_IN`
edges), with every other Milestone B/C edge count (`RELATED_TO` at 24)
unchanged by the round-trip.

## C6 — Witness home jurisdiction

**Decision:** `fir_witness.police_station_of_residence_id`/
`other_district` -> a `LOCATED_AT`-style edge (`Person -> PoliceStation`
or `Person -> District`) to the witness's HOME jurisdiction — distinct
from the case's own filing jurisdiction (`Case-[FILED_AT]->PoliceStation`,
B1). `src/graph/structured_projection.py`'s new
`_write_witness_home_jurisdiction()`, called from `_write_witnesses()`
per witness after resolution.

**Real API shape verified before assuming it, per the plan's explicit
instruction** — `police_station_of_residence_id` is a BARE id string
(e.g. `"PS-FSD-CIVILLINES"`), confirmed against
`tests/fixtures/muhafiz_api_snapshot.json` directly, never a nested
object the way `FirRecord.police_station` is. It matches B1's own
`PoliceStation.station_id` values exactly (confirmed against the live
graph before writing any code: `PS-FSD-CIVILLINES`/`PS-LHR-MODELTOWN`/
`PS-LHR-BARKI` already exist as B1-written nodes).

This MUST MERGE onto the SAME `PoliceStation`/`District` nodes B1 already
writes, not a second, parallel node set — so this reuses B1's exact
identity keys (`{"station_id": ...}`/`{"district_id": ...}`), never a
locally-invented one. The `PoliceStation` write uses EMPTY properties
(`write_node()`'s SET clause is then a no-op on the property side) so a
witness-only reference to a station never overwrites a real name/code B1
already populated for that station from its own filing-side data — a
dedicated test (`test_home_station_written_with_empty_properties_never_
clobbers_filing_station`) asserts this directly, and the live run below
confirms the real station's name survived the round-trip.

`other_district` (never observed populated live — 0/37 in the recorded
snapshot) is the fallback when no `police_station_of_residence_id` is on
file: a direct `Person -> District` edge, keyed the same id-or-name way
B1's own `_district_identity()` already tolerates for a bare-string
district. Station takes priority when both are present — mutually
exclusive in this design.

No new elabel/migration — `LOCATED_AT`/`PoliceStation`/`District` already
exist; this is a second, semantically-consistent reuse of `LOCATED_AT`
(first written for Person->Address), same edge-label-reuse precedent as
B1's own `PART_OF` reuse.

### §7-B verification — measured, not assumed

Cypher assertions run against the real Postgres/AGE instance after a
`--full` re-sync of the complete 73-FIR corpus:

```
MATCH (p:Person)-[r:LOCATED_AT]->(s:PoliceStation) RETURN p.canonical_name, s.station_id
MATCH (p:Person)-[r:LOCATED_AT]->(d:District) RETURN p.canonical_name, d.district_id
```

**Result: 8 `Person->PoliceStation` edges, 0 `Person->District` edges** —
matching the measured real data exactly (8/37 real witnesses carry
`police_station_of_residence_id`, 0/37 carry `other_district`).

**No-clobber check** (the specific risk this module's empty-properties
design guards against): `PS-FSD-CIVILLINES` (one of the 8 witness-linked
stations, also a real filing station on other FIRs) still carries its
real name (`"تھانہ سول لائنز، فیصل آباد"`) after the sync — the witness
write did not blank it. `FILED_AT` edges also unchanged at 73/73.

No-duplication check — ran the exact same `--full` re-sync a SECOND time:
**8/0 edges, identical after both runs** (73/73 `Case` nodes also
unchanged).

## C7 — Sensitive-field governance: ethnicity/religion (docs only, no code)

**Decision:** `fir_witness.ethnicity`/`fir_witness.religion` are **never
ingested** — this is the default, and it stays the default until/unless
there is a specific, deliberate policy sign-off to do otherwise.

This is not a gap C1–C6 left open by accident: `muhafiz_schema.dbml.txt`
flags both fields itself, in its own words, as "SENSITIVE — real field on
the Witness tab, confirm with the team whether to actually populate it."
No module in this milestone (or any earlier one) reads either field.
Concretely: `structured_projection.py`'s `_person_mention()` — the one
function every `fir_witness` row's mention dict is built through — copies
an explicit, fixed whitelist of keys (`cnic`, `father_name`,
`address_text`, `phone`); `ethnicity`/`religion` are not on that list, so
they are silently dropped at the mention-building step, never reaching
`resolve_structured_person()`, `versioning.write_node()`, or any
`StructuredRecord` property dict. A grep of the entire `src/` tree for
either field name, run while writing this record, returns zero matches —
the exclusion is total, not partial.

**Why recorded now, not deferred to Milestone F's ADR:** the plan's own
text names "the new ADR (Milestone F)" as this decision's eventual
home, but Milestone F may not land for a while yet, and this repository's
own discipline — established by Milestones A and B, both landing their
docs with the code rather than batching — is to record a decision when
it's made, not when a future milestone happens to get around to writing
it up. The M1–M12 migration's name-fallback accepted-risk decision got
this same treatment (recorded in `docs/decisions/0001-...` as its own
change landed); this is the identical discipline applied to a governance
decision that has no code change to accompany it, only the schema's own
explicit flag and this milestone's confirmation that the default was
actually honored end-to-end.

**Scope of this decision:** it covers exactly these two fields on
`fir_witness`, per the schema's own flag. It does not extend a blanket
policy to other fields not similarly flagged, and it does not preclude a
future, deliberate decision to populate them — that would require its
own explicit sign-off and its own ADR entry recording the reasoning,
exactly as this entry records the reasoning for the current default. Not
a branch, not a code change — this section IS the governance record.

## Status: Milestone C complete

- [x] **C1** — person-relationship edges. `migrations/025_related_to_label.sql`,
      `structured_projection.py`'s `_write_victim()`/`_write_related_to()`,
      `cross_silo_projection.py`'s `_write_pkm_relationship()`,
      `PkmApplication.owner`/`.tenant`/`.employer`/`.employee`. New/updated
      tests in `tests/test_structured_projection.py`
      (`TestPersonRelationshipEdges`) and `tests/test_cross_silo_projection.py`
      (`TestProjectPkmRelationships`). Full suite green. Live-verified
      against real Postgres/AGE per §7-B above — 24 edges, hand-checked
      sample matches source data exactly, no duplication across two
      `--full` re-syncs.
- [x] **C2** — chalaan name resolution. `structured_projection.py`'s
      `_write_chalaan_name_links()`/`_split_name_list()`, `_write_witnesses()`
      extended to build `witness_by_name` alongside the existing
      `accused_by_name`. New tests in `tests/test_structured_projection.py`
      (`TestChalaanNameResolution`). Full suite green. Live-verified
      against real Postgres/AGE per §7-B above — 57 edges, hand-checked
      sample matches source data exactly, no duplication across two
      `--full` re-syncs.
- [x] **C3** — zimni officer and position timeline.
      `structured_projection.py`'s `_write_zimni_officers()`,
      `_write_occurred_on()` extended for `fir_position`, `fir_position`
      added to `_STRUCTURED_RECORD_TABLES`. New tests in
      `tests/test_structured_projection.py`
      (`TestZimniOfficerAndPositionTimeline`). Full suite green.
      Live-verified against real Postgres/AGE per §7-B above — 62
      Officer-zimni edges, 94 Incident-position edges, hand-checked sample
      matches source data exactly, no duplication across two `--full`
      re-syncs.
- [x] **C4** — cross-version edge. `migrations/026_cross_version_of_label.sql`,
      `cross_silo_projection.py`'s `project_fir_cross_versions()`,
      `sync_muhafiz_data.py`'s `sync_cross_versions()`. New tests in
      `tests/test_cross_silo_projection.py` (`TestProjectFirCrossVersions`).
      Full suite green. 0 real rows (locked in by test); live-verified
      against real Postgres/AGE per §7-B above via a constructed-fixture
      injection — 1 edge written and matched exactly, no duplication
      across two `--full` re-syncs, graph correctly restored to 0 edges
      once the real (unmodified) snapshot was re-synced.
- [x] **C5** — typed recovered property.
      `structured_projection.py`'s `_classify_and_write_malkhana_item()`.
      New tests in `tests/test_structured_projection.py`
      (`TestTypedRecoveredProperty`). Full suite green. 0 real
      plate/phone-shaped `item_detail` values in the live dataset;
      live-verified against real Postgres/AGE per §7-B above via a
      constructed-fixture injection (one plate-shaped, one phone-shaped
      row) — both resolved and edge-matched exactly, no duplication
      across two `--full` re-syncs, graph correctly restored once the
      real (unmodified) snapshot was re-synced.
- [x] **C6** — witness home jurisdiction.
      `structured_projection.py`'s `_write_witness_home_jurisdiction()`.
      New tests in `tests/test_structured_projection.py`
      (`TestWitnessHomeJurisdiction`). Full suite green. Live-verified
      against real Postgres/AGE per §7-B above — 8 `Person->PoliceStation`
      edges (matching the measured 8/37 real witnesses), 0
      `Person->District` edges (matching 0/37), no-clobber check
      confirmed a real station's name survived the round-trip, no
      duplication across two `--full` re-syncs.
- [x] **C7** — sensitive-field governance (ethnicity/religion), docs
      only, no code. See its own section above — default is never
      ingest, confirmed still total across every C1–C6 write path (a
      full-tree grep for either field name in `src/` returns zero
      matches).

All six code modules (C1–C6) landed as their own branch, merged
`--no-ff` into local `main`, full test suite green at every step,
live-verified against the real Postgres/AGE instance (`muhafiz-postgres`,
confirmed reachable before this milestone started) — real-data counts
where real data exists (C1: 24, C2: 57, C3: 62/94, C6: 8/0), a
constructed-fixture injection run live where it doesn't (C4, C5), with
every fixture-only edge confirmed to self-heal back out once the real
snapshot was re-synced. C7 landed as a docs-only record, no branch.
Nothing pushed to `origin`. Milestones D–F were not started — out of
scope for this pass.
