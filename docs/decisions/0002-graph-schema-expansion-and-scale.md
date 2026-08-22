# 0002 — Graph scale prerequisites and schema expansion (Milestones A–F)

**Status:** complete (Milestones A, B, C, D, E, and F all landed; see the
checklist at the bottom of each section) **Date:** 2026-08-22

## Contents

**Milestone F's own decision on this ADR's final shape** (one of F1's
resolved open points): this document stayed an append-only running log
as each milestone landed (a new `## Milestone X` block plus its own
`## Status: Milestone X complete` block, in landing order) for the whole
A→E build — that shape matches how the work actually happened and is
cheap to keep extending mid-flight. Now that Milestone F is the last
entry and the document has stopped growing, a plain table of contents is
added here rather than restructuring the 1,500+ lines above it into a
different shape — restructuring would risk silently dropping or
misplacing a decision that took real investigation to reach, for a
navigability improvement a TOC gets just as well. Decided, not
defaulted, same as every module below.

- [Context](#context) — the three hot paths this plan diagnosed
- [Milestone A](#a1--identity-index-tables) — scale prerequisites: A1 identity index, A2 full-text index, A3 batched embeddings
- [Milestone B](#b1--jurisdiction-graph-nodes) — schema depth: B1 jurisdiction nodes, B2 officer identity
- [Milestone C](#milestone-c--closing-the-remaining-structured-field-gaps) — C1–C6 structured-field gaps, C7 governance record
- [Milestone D](#milestone-d--queue-scale-resolution-hard-human-confirmation-rule-kept) — D1 pending-candidate reprioritization, D2 confidence-hedged retrieval
- [Milestone E](#milestone-e--query-time-scale) — E1 query-scope preclassification, E2 default case-scoped traversal, E3 incremental community refresh
- [Milestone F](#milestone-f--documentation) — this milestone: `docs/graph_schema.md`/`docs/DATABASE_DESIGN.md` brought current, the two §5 "watch, don't build" items recorded, this ADR's own final shape decided
- [Out of scope, watched not built](#out-of-scope-watched-not-built-milestone-f) — AGE-as-graph-store (confirmed), Chroma-as-vector-store (a real watch item)

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
B (schema depth: jurisdiction/officer nodes), C (the remaining
structured-field gaps: person-relationship edges, chalaan name
resolution, zimni officer/position timeline, cross-version edge, typed
recovered property, witness home jurisdiction, and the ethnicity/religion
governance record), D (queue-scale resolution: pending-candidate
reprioritization and confidence-hedged retrieval, both keeping the hard
human-confirmation rule absolute), and E (query-time scale: station/
district preclassification wired into the router's own single
classification step, closing the one within-case traversal path that
could leak across cases, and moving community-summary rebuilding from
admin/script-invoked-only to an automatic staleness-gated trigger), and
F (documentation: `docs/graph_schema.md` and `docs/DATABASE_DESIGN.md`
brought current with everything A–E added, the two §5 "watch, don't
build" items recorded here, and this document's own final shape
decided).

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
Nothing pushed to `origin`. Milestone D followed immediately (see below);
Milestones E–F were not started — out of scope for this pass.

---

## Milestone D — queue-scale resolution, hard human-confirmation rule kept

**Four open points resolved before implementation started** (each found
by checking the plan against the actual code, not assumed from the plan
text alone — see `GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md`'s own "Open
points to resolve first" for the original framing):

1. **D1's "why" is a deterministic template, never an LLM call.** Built
   from the exact `entity_resolution.Candidate` scoring fields
   (`name_similarity`, `shared_case`, `shared_structured_id`) —
   `src/graph/candidate_reprioritization.py`'s `_why()`. To make this
   possible without re-parsing free text, `entity_resolution.
   ResolutionDecision` gained three new fields carrying those exact
   values, persisted onto the SAME_AS edge itself by `resolve_and_write`
   alongside `tier`/`basis`/`status` (additive edge properties, no
   existing reader affected).
2. **D1's grouping algorithm: connected components**, concretely —
   `candidate_reprioritization._UnionFind` unions two pending SAME_AS
   candidates when they share the same target/candidate entity, the same
   mention entity, a shared structured-id value between their mention
   nodes, or a shared case between their mention nodes (the plan's own
   "shared CNIC-adjacent value / shared address / shared case" example,
   made literal).
3. **D1's execution model: no new worker infrastructure.** This codebase
   has no standalone scheduled worker/cron — every existing "background"
   task is a per-request `asyncio.create_task()` (confirmed by reading
   `src/ingestion/conflict_bg.py`, `src/pipeline/orchestrator.py` before
   deciding). D1 uses two paths that both fit that existing shape rather
   than inventing a new one: (1) an incremental fire-and-forget task
   scheduled at the same point conflict detection already is, right
   after a document's graph extraction, scoped to that case's own
   pending candidates (`src/ingestion/reprioritization_bg.py`); (2) a
   supervisor-triggered manual full-sweep endpoint
   (`POST /api/admin/graph-review/queue/reprioritize`) for staleness
   deprioritization, since there is no cron to run that on a schedule —
   named honestly as a gap rather than papered over.
4. **D1's own hot query gets the same treatment A1 gave identity
   lookups.** `migrations/027_pending_candidate_priority.sql` — a plain
   Postgres side table, `pending_candidate_priority`, mirroring the
   design discipline of migration 021 (`identity_index`)/016
   (`community_membership`): one row per currently-pending SAME_AS/CITES
   edge, maintained by `src/graph/versioning.py`'s `write_edge()` (the
   same single choke point A1 already uses for `identity_index`) —
   inserted the moment a pending edge is written, deleted the moment a
   human confirms/rejects it. Backs `WHERE r.status = 'pending'` reads
   (previously an unindexed AGE label scan, same confirmed diagnosis as
   migration 021's header) with a real Postgres index scan.

### D1 — Pending-candidate reprioritization

**Decision:** `src/graph/candidate_reprioritization.py` re-scores
(never auto-decides) pending SAME_AS candidates against the graph's
CURRENT state, diffed against the original scoring snapshot on the edge.
Two non-destructive outputs, both owned exclusively by this module's one
Postgres write (`pending_candidate_priority.update_priority()` — the
module has zero calls to `versioning.write_edge()`, so it is
structurally incapable of confirming/rejecting a match):

- **Reorders**: `priority_score` recomputed with entity_resolution's own
  scoring weights (`name_sim*0.7 + 0.15 shared_structured + 0.15
  shared_case`) plus a `+0.10` reinforcement bonus when the fresh signal
  improved on the original — so a candidate that gained real new
  evidence visibly outranks an equally-scored-but-stale one.
- **Groups**: connected components (point 2 above) into a `group_id`,
  surfaced via the new `GET /api/admin/graph-review/queue/groups`
  endpoint, actioned via `POST /api/admin/graph-review/queue/batches/
  {group_id}/{confirm,reject}` — internally still one
  `confirm_match()`/`reject_match()` call per member edge (the existing,
  already-tested, already-audited single-edge path), never a new
  graph-write primitive. One member already independently reviewed (409)
  is reported per-edge without aborting the rest of the batch.
- **Deprioritizes** (sinks, never deletes/rejects) a candidate with no
  new corroboration after `STALE_AFTER` (14 days) since its last score —
  asserts nothing false, only reduces attention, unlike confirm/reject.

New endpoints, all `require_role("supervisor")` (reusing the existing
dependency, not a new gate): `GET /queue` (reordered list), `GET
/queue/groups`, `POST /queue/reprioritize` (manual full sweep — point
3's path #2), `POST /queue/batches/{group_id}/confirm|reject`. The
existing `/pending`/`/citations/*` endpoints are unchanged — `/queue/*`
is additive.

### D2 — Confidence-hedged retrieval

**Decision:** extends the existing mechanism on both ends named in the
plan, rather than building parallel machinery.

- `src/retrieval/graph_retriever.py`'s `retrieve_graph()` opens the
  pending-SAME_AS traversal exclusion — behind
  `config.FEATURE_HEDGED_PENDING_TRAVERSAL` (default **off** — XGRAPH is
  live production traffic), and **only for `cross_case=True`** (the
  XGRAPH path the plan names explicitly; the within-case path is
  untouched regardless of the flag). With the flag on, a pending SAME_AS
  link is followed like a confirmed one, except its confidence is
  compounded (not carried through unchanged) and hard-capped at
  `_PENDING_HEDGE_CAP = 0.80` — strictly below verifier.py's 0.85 hedge
  threshold, by construction, not by hoping the numbers land there —
  and every chunk reached only through it is tagged
  `metadata.same_as_status = "pending"` / `same_as_basis`. The tag
  propagates forward through any further hop, since a downstream entity
  is still evidentially downstream of that same unconfirmed identity.
  With the flag off, behavior is byte-for-byte the prior exclusion
  (regression-tested).
- `src/pipeline/verifier.py`'s `_check_hedging()` — the SAME function,
  extended with one more condition, not a parallel check — now also
  requires a disclosed hedge when `metadata.same_as_status == "pending"`,
  independent of the numeric confidence value (defense-in-depth: a
  future change to how confidence compounds can never silently stop
  requiring this disclosure). `cross_case_response.txt`'s existing rules
  4 ("carry forward any UNCONFIRMED IDENTITY LINK... never present it as
  fact") and 5b ("hedge every LOW-confidence citation") already covered
  the generation-side instruction — confirmed by reading the prompt
  before writing any code — so no prompt change was needed.

**Why silent downweighting was rejected, restated from the plan:** it
would let unconfirmed evidence shape an answer's ranking without ever
disclosing that it did — an integrity problem in an evidentiary tool.
Disclosed hedging preserves recall (real leads surface) without ever
asserting an unconfirmed identity as settled fact.

### §7-D verification — measured against the real live instance

`muhafiz-postgres` confirmed reachable (Docker Desktop was not running at
the start of this milestone — started, health-checked, confirmed before
any other work) before this milestone started, same discipline as A/B/C.

`scripts/verify_milestone_d.py` — a synthetic corroborating-evidence
sequence run against the REAL running Postgres/AGE instance (D1 has no
eval-graph override, unlike entity_resolution's own tiering, so this
script is destructive-but-cleaned-up: every synthetic node/edge/row is
tagged `D1VERIFY-<run-id>` and deleted again in a `finally` block
regardless of outcome, plus an orphan-row sweep at the start of every
run so a prior crashed run can never leave stale clutter behind).
Confirmed live, in order: (1) a near-identical second mention lands in a
pending SAME_AS tier, never CNIC-auto; (2) new corroborating evidence
(a shared phone, a shared case) increases `priority_score` and produces
a deterministic reinforcement `why`, and assigns a `group_id`; (3) the
edge's own `status` stays `'pending'` throughout reprioritization —
changes only after a simulated human action through
`graph_review.confirm_match()`, at which point the queue row is cleared;
(4) with `FEATURE_HEDGED_PENDING_TRAVERSAL` off, a second synthetic
pending pair is excluded from cross-case traversal exactly as before;
with it on, the pair is surfaced via `unconfirmed_links` (a full
chunk-level hedge-tag assertion, requiring a Chroma-backed document, is
covered at the unit level in `tests/test_verifier.py` instead — see below
for why standing up Chroma content wasn't in scope for this graph-focused
script). Three real bugs were found and fixed by this live run, not
caught by the mocked-fixture unit suite: an `asyncpg` parameter-type
collision in the `INSERT` (a literal `0` inside `COALESCE` alongside a
`double precision` column), the identical class of bug in `UPDATE`'s
`last_scored_at` (a plain ISO string handed to a `timestamptz` column —
asyncpg's prepared-statement binding requires a native `datetime`, not a
`CAST` around a string), and two connection-pool-across-event-loops
mistakes in the verification script's own two-`asyncio.run()` structure.

Unit-level (no network, matches the `no_network` guard): `tests/
test_candidate_reprioritization.py` (why-template/scoring/grouping, pure
functions, including a structural assertion that `_why()` is not even
a coroutine — it cannot call an LLM), new cases in `tests/
test_graph_review.py` (`/queue`, `/queue/groups`, `/queue/reprioritize`
never writing a graph edge, batch confirm/reject applying the same
single-edge path per member, one already-reviewed member not aborting
the batch), and new cases in `tests/test_graph_retriever.py` /
`tests/test_verifier.py` (flag-off byte-for-byte regression, flag-on
traversal+hedge-tag+confidence-cap, flag inert within-case, hedge
required by the tag alone even at high numeric confidence, no
false-positive hedge on an untagged high-confidence chunk). Full suite
green (all pre-existing tests unchanged in behavior) before both merges.

- [x] **D1** — pending-candidate reprioritization.
      `migrations/027_pending_candidate_priority.sql`,
      `src/graph/pending_candidate_priority.py`,
      `src/graph/candidate_reprioritization.py`,
      `src/ingestion/reprioritization_bg.py`, new `/queue/*` endpoints in
      `src/api/graph_review.py`. `entity_resolution.ResolutionDecision`
      gained `name_similarity`/`shared_case`/`shared_structured_id`.
      `tests/test_candidate_reprioritization.py` (new), new cases in
      `tests/test_graph_review.py`. Full suite green. Live-verified
      against real Postgres/AGE per §7-D above.
- [x] **D2** — confidence-hedged retrieval.
      `src/retrieval/graph_retriever.py` (pending-identity traversal
      behind `config.FEATURE_HEDGED_PENDING_TRAVERSAL`, default off),
      `src/pipeline/verifier.py`'s `_check_hedging()` extended (not
      duplicated). New cases in `tests/test_graph_retriever.py` and
      `tests/test_verifier.py`. Full suite green. Live-verified against
      real Postgres/AGE per §7-D above (flag on/off traversal behavior;
      full chunk-level hedge-tag assertion at the unit level).

## Status: Milestone D complete

Both modules (D1, D2) landed as their own branch, merged `--no-ff` into
local `main`, full test suite green at every step, live-verified against
the real Postgres/AGE instance. The hard human-confirmation rule stayed
absolute throughout: no code path in either module can set a
SAME_AS/CITES/CROSS_VERSION_OF edge's status to confirmed/rejected
without a human call to `graph_review.py`'s existing confirm/reject
endpoints. Nothing pushed to `origin`.

---

## Milestone E — query-time scale

Before starting: `muhafiz-postgres` was confirmed actually reachable
(`MATCH (c:Case) RETURN count(c)` returned 73, matching every prior
milestone's recorded count) before any E-module work began.

**Five open points resolved before implementation started** (each found
by checking this plan against the actual code, not assumed from the plan
text alone — full reasoning in `GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md`'s
own "Open points to resolve first" for the original framing):

1. **E2's premise ("case_scope.py is currently eval-only") was factually
   wrong and rewritten before implementation, not implemented as
   worded.** Reading `case_scope.py`'s own module docstring and every
   call site confirmed it was already the production enforcement
   chokepoint for `graph_retriever.py`'s within-case seed lookup/per-hop
   filter/conflict lookup, `entity_resolution.py`'s case-membership
   check, and the harness's `timeline_building.py`/`data_quality.py`
   agents. The real remaining gap was found by tracing every path that
   grows `retrieve_graph()`'s `visited` set, not by assuming the plan's
   framing: `_expand_confirmed_identity()`'s confirmed-SAME_AS identity
   fold ran unconditionally — no `cross_case` gate, and never routed
   through the per-hop `_filter_to_case` guard the way ordinary
   `ASSOCIATED_WITH` hops already were. A CONFIRMED SAME_AS edge is
   frequently itself a cross-case link (the same real person recognized
   in two FIRs), so a within-case query (`cross_case=False`, no role
   check) could silently fold another case's node straight into
   `visited` and, from there, surface that case's evidence chunks —
   without ever passing through `_enforce_cross_case_role_gate()`.
2. **E1 extends router.py's SAME single classification call**, not a
   second gate. `route_query()` already classifies every query
   (`route`/`case_scope`/`target_entity`/`target_year`/`confidence`) in
   one place; E1 adds `station`/`district` fields to that identical JSON
   schema/parsing pass rather than introducing a separate LLM call or
   parsing step — avoiding the "a third gate drifting out of sync"
   problem `SUBAGENT_INTERFACES.md` already warns about, and B1's own
   access-control section already resolved once (reusing
   `_enforce_cross_case_role_gate` rather than adding a second check).
3. **E1 wires into B1's existing `retrieve_jurisdiction_cases()`**, not
   a second jurisdiction-lookup path — confirmed by reading that
   function's own docstring, which named this exact wiring as "Milestone
   E1's job, out of scope here." `graph_retriever.resolve_jurisdiction_case_ids()`
   is the new entry point: it resolves the router's free-text
   station/district to a real `PoliceStation`/`District` id via a plain
   (ungated) metadata lookup, then calls `retrieve_jurisdiction_cases()`
   completely unchanged — same `_enforce_cross_case_role_gate()`, not a
   second gate, per B1's own "second caller of it, not a second gate"
   precedent. `crime_category` needed no new schema work — confirmed
   already a flat `Case` property (read at `orchestrator.py`'s
   case-summary formatting) and already filtered by `xagg.py`'s own
   keyword matching — so it was deliberately NOT added as a second
   router field, which would itself have been a second place deciding
   the same filter.
4. **E3's execution model reuses D1's resolution, not a deviation** —
   this codebase still has no standalone scheduled worker/cron
   (re-confirmed while implementing this, same check D1 already made).
   `community_detection.detect_communities()`/
   `community_vector_store.clear_all_reports()` (that module actually
   lives at `src/retrieval/community_vector_store.py`, not `src/graph/`
   as an earlier draft of the plan had it — corrected there and here)
   were ADMIN/SCRIPT-INVOKED ONLY (`scripts/check_community_staleness.py`
   was a manual pre-flight check with no automatic caller anywhere,
   confirmed by reading it). E3 reuses D1's exact shape: an incremental
   fire-and-forget task at the same point ingestion already schedules
   conflict detection/D1 reprioritization (case-scoped triggers), gated
   by the SAME 10%-drift staleness heuristic
   `scripts/check_community_staleness.py` already used manually (moved
   into `community_detection.get_staleness()`, not duplicated — the
   script is now a thin CLI wrapper around it), plus a
   supervisor-triggered manual full-sweep endpoint for the case there's
   no cron to run a schedule on. Genuinely incremental community
   *detection* (re-clustering only the subgraph that actually changed,
   rather than the full Louvain pass every run) is a real algorithmic
   undertaking left explicitly out of scope, named honestly rather than
   half-built — E3 changes WHEN the existing full-recompute runs, not
   what it computes.
5. **E3 ships unflagged, reasoning stated explicitly rather than
   silently picked.** Unlike D2's XGRAPH dual-path (which changed what a
   query is allowed to see — the actual reason it needed a flag),
   `query_similar_communities()`'s semantics are byte-for-byte unchanged
   for both `src/pipeline/xnetwork.py` and the harness tool wrapper —
   confirm/reject-equivalent visibility rules never change, only
   recompute cadence does. The transient clear-then-upsert rebuild
   window (`community_vector_store.clear_all_reports()` followed by a
   fresh `upsert_community_reports()`) already existed on every prior
   MANUAL admin invocation of this same pipeline — automating the
   trigger doesn't introduce a new risk class, only changes how often an
   already-existing window occurs.

### E2 — Default case-scoped traversal

**Decision:** filter the confirmed-SAME_AS identity fold's `new_identity`
set through the same `_filter_to_case()` call `retrieve_graph()`'s
ordinary hop-expansion step (`next_frontier`) already used, when
`cross_case=False` and `case_id` is set — closing the one path that grew
`visited` without going through a per-hop case filter. Cross-case
behavior (`cross_case=True`, role-gated) is completely unaffected: the
fold still runs unconditionally there, exactly as before.

`case_scope.py`'s own module docstring was updated to record this
finding directly, so a future reader tracing "which templates does this
chokepoint cover" sees the corrected picture, not the plan's original
"eval-only" framing repeated a second place.

#### §7-E verification — measured against the real live instance

Unit-level (`tests/test_graph_retriever.py`): a new fixture,
identical in shape to the existing cross-case confirmed-identity test,
run with `cross_case=False` — a CONFIRMED SAME_AS pair spanning two
different cases must not leak the other case's chunk into a within-case
`retrieve_graph()` call, while the seed entity's own case chunk is still
returned. Full suite green (5,350+ tests) before merge.

Live-verified against `muhafiz-postgres`: a synthetic confirmed SAME_AS
pair was injected spanning two REAL existing cases (tagged
`E2VERIFY-<run-id>`, cleaned up in a `finally` block), proving
`_filter_to_case()`'s real Cypher correctly excludes the other case's
identity node from a within-case traversal's `seed_entities`/`visited`
set against the genuine AGE instance, not just the fake-graph unit test.

- [x] **E2** — default case-scoped traversal.
      `src/retrieval/graph_retriever.py`'s `retrieve_graph()` hop loop
      (identity-fold case filter), `src/graph/case_scope.py` docstring
      updated. New test in `tests/test_graph_retriever.py`
      (`test_confirmed_same_as_never_leaks_another_case_within_case_traversal`).
      Full suite green. Live-verified against real Postgres/AGE per
      §7-E above.

### E1 — Query-scope preclassification

**Decision:** `src/pipeline/router.py`'s `route_query()` gains two new
free-text fields, `station`/`district` (schema/prompt in
`prompts/router.txt`), forced to `None` for every route except
XGRAPH/XAGG/XNETWORK (same "only these three routes can ever be
cross-case" discipline the existing `case_scope` field already
enforces). `src/retrieval/graph_retriever.py`'s new
`resolve_jurisdiction_case_ids()` is the orchestrator-facing entry point:
resolves the free text to a real `PoliceStation`/`District` id (a plain,
ungated metadata lookup — case-insensitive `CONTAINS` match on
id/name/code), then calls B1's `retrieve_jurisdiction_cases()`
unchanged. Returns `None` (never `[]`) when nothing resolved — narrowing
to an empty set would silently zero out a query whose station/district
text just didn't match a real node, which is a materially different,
worse failure than "don't narrow."

`src/pipeline/orchestrator.py` resolves this ONCE, right after routing,
before dispatching to any of the three cross-case routes; a resolution
failure (including an unauthorized-role `PermissionError`) is logged and
degrades to unscoped (`jurisdiction_case_ids=None`) rather than raising —
the SAME role check inside `retrieve_graph()`/`run_aggregate()`/
`run_network_query()` a few lines later independently denies an
unauthorized caller anyway, so nothing is silently bypassed by
swallowing the exception here.

Threading, per route:
- **XGRAPH** (`graph_retriever.retrieve_graph(cross_case=True, jurisdiction_case_ids=...)`) —
  a real pre-filter: `_find_seed_nodes()`'s cross-case branch and
  `_find_recurring_entities_for_query()` both add
  `AND c.case_id IN $case_ids` to their own Cypher, cutting the
  candidate set before any hop expansion runs.
- **XAGG** (`xagg.run_aggregate(jurisdiction_case_ids=...)`) — both
  aggregate families: `_top_recurring_nodes()`'s Cypher match (graph
  family) and `_filtered_cases()`'s case-list filter, applied BEFORE the
  status/category filtering that already existed (relational family).
- **XNETWORK** (`xnetwork.run_network_query(jurisdiction_case_ids=...)`) —
  a POST-filter on `query_similar_communities()`'s already-computed
  top-k, stated honestly as a narrower guarantee than XGRAPH/XAGG's true
  pre-filter: `community_vector_store`'s Chroma collection stores
  `case_ids` as a comma-joined metadata string, not a natively
  filterable list field, so pushing this down into the Chroma `where`
  clause itself would need a metadata-schema change out of scope here.

#### §7-E verification — measured against the real live instance

Unit-level: new tests in `tests/test_router.py` (station/district
pass-through, forced-`None` for non-cross-case routes),
`tests/test_graph_retriever.py` (`TestResolveJurisdictionCaseIds`,
`TestJurisdictionNarrowsCrossCaseSeedLookup`), `tests/test_xagg.py`
(jurisdiction narrowing for both aggregate families), the new
`tests/test_xnetwork.py` (no prior test file existed for
`src/pipeline/xnetwork.py`), and new cases in `tests/test_orchestrator.py`
(station classified -> reaches `run_aggregate()` as the resolved
case_ids; no station/district -> resolver never called; resolver
failure degrades to unscoped without failing the query). Full suite
green before merge.

Live-verified against `muhafiz-postgres`: `resolve_jurisdiction_case_ids()`,
given the REAL name of the station with the most filed cases in the live
corpus (`تھانہ ماڈل ٹاؤن، لاہور` / `PS-LHR-MODELTOWN`, 7 cases), resolved
it to the identical 7-case_id set B1's own `retrieve_jurisdiction_cases()`
returns for that station's real id — confirming the free-text-to-id
resolution step works against genuine `PoliceStation` data, not just a
fake. Candidate-set narrowing measured directly on
`xagg._top_recurring_nodes()` (jurisdiction-narrowed count never exceeds
the unscoped count — 0 in both cases on this real corpus, since no
vehicle currently recurs across cases in the live data). An unauthorized
(`investigator`) caller was confirmed denied by the identical role gate.

- [x] **E1** — query-scope preclassification. `src/pipeline/router.py`/
      `prompts/router.txt` (station/district fields),
      `src/retrieval/graph_retriever.py`'s `resolve_jurisdiction_case_ids()`
      plus `jurisdiction_case_ids` threaded through `retrieve_graph()`/
      `_find_seed_nodes()`/`_find_recurring_entities_for_query()`,
      `src/pipeline/xagg.py`'s `run_aggregate()` (both families),
      `src/pipeline/xnetwork.py`'s `run_network_query()` (post-filter),
      `src/pipeline/orchestrator.py` (resolves once, before dispatch).
      New/updated tests across `tests/test_router.py`,
      `tests/test_graph_retriever.py`, `tests/test_xagg.py`,
      `tests/test_xnetwork.py` (new file), `tests/test_orchestrator.py`.
      Full suite green. Live-verified against real Postgres/AGE per
      §7-E above.

### E3 — Incremental community refresh

**Decision:** `src/graph/community_detection.py` gains `get_staleness()`
— the exact 10%-node/10%-edge-drift heuristic
`scripts/check_community_staleness.py` already computed manually,
moved into the module itself (`NODE_DRIFT_WARN_PCT`/`EDGE_DRIFT_WARN_PCT`,
`_current_raw_node_count()`/`_current_raw_edge_count()`) so it has one
home instead of two copies; the script is now a thin CLI wrapper that
calls it and prints the result.

`src/ingestion/community_refresh_bg.py`'s `_run_community_refresh_bg()`
is the incremental half of D1's reused execution model: a
`asyncio.create_task()` fire-and-forget call at the same point
`src/ingestion/service.py` already schedules conflict detection and D1
reprioritization, right after a document's graph extraction. Unlike
those two (case-scoped), this is NOT case-scoped — community detection
clusters the whole Person graph, so there is no case-specific slice to
pass in; every ingestion event just asks `get_staleness()` whether the
whole-graph partition is stale enough to be worth a full recompute, and
only actually calls `detect_communities()` + `community_summarization.summarize_communities()`
when it is. This bounds how often the real cost here (an LLM call per
community, inside `summarize_communities()`) can fire from ingestion —
every ingest checks staleness cheaply; only a drift-crossing ingest
re-summarizes. Best-effort, same as `reprioritization_bg.py`: a failure
here is logged and swallowed, never propagated to fail the ingestion job
it rides alongside.

`src/api/community_admin.py` — `GET /api/admin/community/staleness`
(read-only) and `POST /api/admin/community/refresh` (supervisor role;
always runs both steps unconditionally, unlike the automatic trigger's
staleness gate — a supervisor explicitly asking for a refresh isn't
gated behind the drift heuristic) — the manual full-sweep path
(D1's execution-model point 3, path #2) for the case there's no cron to
run a schedule on, same shape as `graph_review.py`'s own
`POST /queue/reprioritize`. Registered in `src/main.py` alongside the
other admin routers.

#### §7-E verification — measured against the real live instance

Unit-level: `tests/test_community_staleness.py` (new —
`get_staleness()`'s four branches: no prior run, prior run missing raw
counts, within threshold, past threshold on either dimension;
`_run_community_refresh_bg()`'s three branches: skips when not stale,
runs detect+summarize when stale, best-effort swallows a failure) and
`tests/test_community_admin.py` (new — both endpoints, confirming the
manual endpoint runs unconditionally regardless of staleness). Full
suite green before merge.

Live-verified against `muhafiz-postgres`: `get_staleness()` correctly
reported "no run found yet" (stale) against the real graph's actual
counts at the time (444 raw Person nodes, 221 raw edges); a REAL
`detect_communities()` run (`RUN-20260822103913`, 60 nodes after the
implausible-name filter, 18 communities) wrote a fresh `community_runs`
row, confirmed by `get_latest_run()`; `get_staleness()` immediately after
that run correctly reported "within threshold" (not stale, 0% drift
against itself); `_run_community_refresh_bg()` run immediately
afterward correctly SKIPPED calling `detect_communities()` again (a spy
confirmed zero calls); a second run with staleness forced artificially
confirmed the positive path — a fresh `detect_communities()` run
actually fires and writes a new `run_id`.
`community_summarization.summarize_communities()`'s LLM step could not
complete in this session due to a pre-existing, unrelated environment
issue (the local model tunnel returned 404, and the configured Groq
fallback model `llama-3.3-70b-versatile` does not exist/is not
accessible under this deployment's Groq account) — flagged here plainly
as an infrastructure gap to fix separately, not an E3 defect: the
graph-side half of what E3 actually changes (the staleness check, the
trigger, `detect_communities()` itself) is fully live-verified above;
`summarize_communities()`'s own prompt/logic is unmodified by this
milestone.

- [x] **E3** — incremental community refresh.
      `src/graph/community_detection.py`'s `get_staleness()` (+
      `_current_raw_node_count()`/`_current_raw_edge_count()` moved in
      from the script), `scripts/check_community_staleness.py`
      (refactored to a thin wrapper), `src/ingestion/community_refresh_bg.py`
      (new), `src/ingestion/service.py` (wired in alongside
      conflict/reprioritization triggers), `src/api/community_admin.py`
      (new), `src/main.py` (router registered). New tests
      `tests/test_community_staleness.py`, `tests/test_community_admin.py`.
      Full suite green. Live-verified against real Postgres/AGE per
      §7-E above (graph-side); `summarize_communities()`'s LLM step
      blocked by an unrelated, pre-existing environment issue, flagged
      separately.

## Status: Milestone E complete

All three modules (E1, E2, E3) landed as their own branch, merged
`--no-ff` into local `main`, full test suite green at every step,
live-verified against the real Postgres/AGE instance. Nothing pushed to
`origin`.

---

## Milestone F — Documentation

No code changes, no new branch/merge cycle — F1 is the one module, and
its own output (the docs below) is the thing being checked, so it was
verified by re-reading the actual current code/schema against what got
written, not by assuming `GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md`'s own
text already described reality (the same discipline every A–E open point
already applied to code, applied here to docs).

**Four open points resolved before writing anything** (each found by
diffing `docs/graph_schema.md`/`docs/DATABASE_DESIGN.md` against the
actual current code, not assumed from the plan text):

1. **`docs/graph_schema.md` was stale as far back as pre-B1.** Its
   `INVOLVED_IN` edge entry read "an investigating officer is written as
   an unresolved Person-labelled node with no cross-document identity —
   no reliable identifier like `belt_no` is treated as one yet" — false
   since B2. A grep of the file turned up zero mentions of
   `PoliceStation`, `District`, `Officer`, `FILED_AT`, `ASSIGNED_TO`,
   `RELATED_TO`, `CROSS_VERSION_OF`, or `LOCATED_AT`'s C6 reuse — the
   Node types/Edge types tables, the "Entity resolution &
   canonicalization" section, and the "Case isolation for the graph"
   section (which still described `graph_retriever.py`'s identity-fold
   helper as safe "by construction," the exact claim E2 found false)
   were all rewritten.
2. **`docs/DATABASE_DESIGN.md` documented D1's `pending_candidate_priority`
   already, but was missing A1's `identity_index` and A2's
   `chunk_fulltext`** — both confirmed absent by grep, despite
   `pending_candidate_priority`'s own prose explicitly cross-referencing
   `identity_index` by name (a section that only ever pointed at
   something that didn't exist yet in the doc). Both added at the same
   tier of detail (`migrations/023`–`026` cross-checked too — confirmed
   AGE-label-only, correctly out of this relational-schema doc's scope,
   already covered by `docs/graph_schema.md` instead).
3. **C7 was already a complete, standalone record** (confirmed by
   re-reading it against this point's own bar — it quotes the schema's
   own sensitive-field flag, names the exact code check that enforces
   the exclusion, and states its scope, not just a pointer back to the
   plan file). **The plan's two §5 "watch, don't build" items (AGE as
   graph store, Chroma as vector store) were NOT yet in this ADR** —
   confirmed by grepping for "AGE"/"Chroma" outside the A1/A2 decision
   sections and finding only a one-line passing mention of AGE in the
   Context section above, no dedicated reasoning, and no Chroma entry at
   all. Both get their own section below, carrying over the plan's own
   §5 reasoning rather than re-deriving it.
4. **This ADR's own final shape** — decided explicitly in the new
   [Contents](#contents) section at the top: stays the append-only
   running log it already was through E, with a table of contents added
   now that it's finished growing, rather than restructured.

### F1 — `docs/graph_schema.md` / `docs/DATABASE_DESIGN.md` brought current

**`docs/graph_schema.md`:**
- Node types table: added `PoliceStation`/`District` (B1) and `Officer`
  (B2), each naming the exact write path and identity key.
- Edge types table: added `FILED_AT` (B1), `ASSIGNED_TO` (B2),
  `RELATED_TO` (C1), `CROSS_VERSION_OF` (C4); documented `PART_OF`'s B1
  reuse (`PoliceStation`→`District`), `LOCATED_AT`'s C6 reuse (witness
  home jurisdiction, distinct from the case's own `FILED_AT` and from a
  street `Address`), `OCCURRED_ON`'s two new C3 `event_type` values
  (`zimni_entry`, `position`), and `APPEARS_IN`'s C2/C5 reuses (chalaan
  name resolution, typed recovered property) in a new paragraph below
  the table rather than overloading the table's own row further.
  Corrected `INVOLVED_IN`'s stale officer-identity note per open point 1.
- "Entity resolution & canonicalization": added B2's `Officer`/`belt_no`
  tier, and a paragraph recording E2's finding/fix directly (this
  section's own "follow confirmed SAME_AS, not one node = one person"
  rule was the exact rule the identity-fold gap violated).
- "Case isolation for the graph": corrected the paragraph that used to
  call `graph_retriever.py`'s identity/hop helpers safe "by construction"
  (true only for ordinary hops, not the identity fold — see E2), pointed
  at `case_scope.py`'s own module docstring as the authoritative call-site
  list rather than restating it a second place, and added E1's
  jurisdiction-scoped-enumeration paragraph (same role gate, a second
  caller of it, not a second gate).

**`docs/DATABASE_DESIGN.md`:** added the "Identity index" (A1) and
"Persistent full-text index" (A2) sections (table shape, maintenance
choke point, read path, why a side table and not an AGE-internal index)
that were missing despite being cross-referenced by name from D1's own
already-written section.

### Out of scope, watched not built (Milestone F)

Carried over from `GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md` §5, recorded
here in this ADR now that the plan file itself (untracked) stops being
the durable home for this reasoning once Milestone F closes it out:

**Apache AGE/Cypher-over-Postgres as the graph store — a confirmed
decision, not a watch-item.** Real-world case volume (thousands, even
low millions, of FIRs across a station/province) stays within what AGE
over a properly-indexed Postgres instance can carry, especially with A1
(identity indexes) and E2 (default case/jurisdiction-scoped traversal)
in place — traversal cost stays bounded to a scoped subgraph rather than
the whole graph. No migration off AGE anywhere in this plan. Revisit only
if a measured trigger is actually hit post-deployment (e.g. p95
traversal latency past a set threshold on real production volume) — not
spec'd or built blind now. A future switch (a dedicated graph store, or
partitioning by district/station) stays possible later without this plan
having precluded it.

**Chroma as the vector store — a real watch item, not a confirmed
decision the way AGE is.** Scales reasonably for now with ANN indexing;
a dedicated vector DB or `pgvector` is a later call if/when a measured
ceiling is actually hit, not a preemptive rewrite. Unlike AGE (where A1/
E2 give a specific, named reason the current choice keeps scaling),
nothing in A–E specifically hardened Chroma's own scaling story — this
entry is a plain "watch, don't build" flag, not a decision backed by a
completed mitigation the way AGE's entry is.

### §7-F verification — docs reviewed against actual code/schema

Every node/edge label added to `docs/graph_schema.md`'s tables
(`PoliceStation`, `District`, `Officer`, `FILED_AT`, `ASSIGNED_TO`,
`RELATED_TO`, `CROSS_VERSION_OF`, `PART_OF`'s B1 reuse, `LOCATED_AT`'s C6
reuse, `OCCURRED_ON`'s C3 event types, `APPEARS_IN`'s C2/C5 reuses)
confirmed present, by name, in `src/graph/structured_projection.py` or
`src/graph/cross_silo_projection.py` before being written into the doc —
not copied from this ADR's own B/C sections without re-checking the
current source. Both new `docs/DATABASE_DESIGN.md` table sections
(`identity_index`, `chunk_fulltext`) cross-checked against their real
migration files (`021`, `022`) column-by-column. `git log --oneline`
confirms one merge commit per A–E module (`--no-ff`, as required).
`git status` confirms `origin` still untouched.
`GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md` itself remains untracked (`git
ls-files` does not list it) — its reasoning now lives here, in this ADR,
per its own stated convention (the same one `MUHAFIZ_API_MIGRATION_PLAN.md`
followed into `docs/decisions/0001-...md`).

- [x] **F1** — documentation. `docs/graph_schema.md` (Node types/Edge
      types tables, entity-resolution section, case-isolation section),
      `docs/DATABASE_DESIGN.md` (identity_index, chunk_fulltext
      sections), this ADR (Contents section, Milestone F section, the
      two §5 watch items). No code changes. Verified per §7-F above.

## Status: Milestone F complete — plan finished

All six milestones (A–F) landed. Every code module (A1–A3, B1–B2,
C1–C6, D1–D2, E1–E3) as its own branch, merged `--no-ff` into local
`main`, full test suite green at every step, live-verified against the
real Postgres/AGE instance where the module's own scope called for it.
C7 and F1 are docs-only records, no branch. Nothing pushed to `origin`
at any point across the whole plan.
