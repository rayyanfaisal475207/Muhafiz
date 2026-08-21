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

*(filled in once A2 lands)*

## A3 — Batched embedding pipeline

*(filled in once A3 lands)*

## Status: Milestone A in progress

- [x] **A1** — identity index tables. `migrations/021_identity_index.sql`,
      `src/graph/identity_index.py`, wired into `versioning.write_node()`
      (maintenance) and `entity_resolution.py`'s `_find_by_primary_id()`/
      `_generate_candidates()` (read, index-first with graph-scan
      fallback). 20 new tests (`tests/test_identity_index.py` plus
      additions to `tests/test_entity_resolution.py`). Full suite green.
      Live-verified against real Postgres/AGE per §7-A above.
- [ ] **A2** — persistent full-text index.
- [ ] **A3** — batched embedding pipeline.
