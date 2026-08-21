-- ============================================================
-- Migration 021: Identity index (Graph Scale & Schema Expansion,
-- Milestone A1 — docs/decisions/0002-graph-schema-expansion-and-scale.md)
--
-- Backs entity_resolution._find_by_primary_id() / _generate_candidates()'s
-- hard-block filtering with a real Postgres primary-key lookup, instead of
-- the AGE `MATCH (n:Label {id_key: $value})` those functions used to run
-- alone — that Cypher looks targeted but AGE has no property index behind
-- it, so it is a full label scan under the hood (confirmed by inspection:
-- migrations/005_age_graph.sql/020_age_date_and_cites_labels.sql only ever
-- pre-create labels, never a property index). §2 of the plan names this as
-- the thing that "breaks down once Person/Vehicle counts hit the tens of
-- thousands".
--
-- This is a plain side table, not an index on AGE's internal storage —
-- deliberately: AGE's per-label vertex tables are an internal
-- implementation detail (undocumented table names/shapes across AGE
-- versions), and this repo already has a working precedent for a
-- Postgres-side table shadowing derived/lookup state alongside the graph
-- (migration 016's community_membership). Apache AGE stays the graph store
-- (§5 of the plan, confirmed decision) — this is additive, not a
-- replacement.
--
-- One row per (label, id_key, id_value) -> the entity_id that currently
-- owns it. Maintained by src/graph/versioning.py's write_node() for every
-- identity-bearing label (IDENTITY_KEYS in src/graph/identity_index.py:
-- currently Person/cnic, Vehicle/plate, PhoneNumber/phone — belt_no
-- (Officer) is B2's concern, not yet a graph label as of this migration).
--
-- Read path: src/graph/identity_index.py's lookup()/entity_ids_excluding()
-- are consulted FIRST by entity_resolution.py; the existing AGE scan is
-- kept as a fallback for an index miss (defends against index/graph drift
-- per the plan's explicit instruction), never removed.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, safe to re-run.
--
-- HOW TO APPLY
--   python scripts/apply_migration.py migrations/021_identity_index.sql

CREATE TABLE IF NOT EXISTS identity_index (
    label       TEXT NOT NULL,
    id_key      TEXT NOT NULL,
    id_value    TEXT NOT NULL,
    entity_id   TEXT NOT NULL,
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (label, id_key, id_value)
);

-- Supports entity_ids_excluding()'s "every entity_id this label/id_key has
-- an indexed value for" scan (used to shrink entity_resolution.py's
-- name-fallback candidate pool to just the entities the index does NOT
-- already know about) without touching the id_value column at all.
CREATE INDEX IF NOT EXISTS ix_identity_index_label_key_entity
    ON identity_index (label, id_key, entity_id);
