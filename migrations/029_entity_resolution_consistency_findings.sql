-- ============================================================
-- Migration 029: Entity-resolution consistency findings (Ingestion
-- Quality Control at Scale, Module G3 -- see
-- INGESTION_QUALITY_AT_SCALE_PLAN.md).
--
-- Extends scripts/eval_entity_resolution.py's ground-truth-driven
-- precision/recall idea into a CONTINUOUS background check: periodically
-- samples a random subset of recently resolved SAME_AS matches (pending
-- flagged_unverified/human_review candidates, and already-confirmed
-- ones) and re-diffs each one's ORIGINAL scoring signal
-- (name_similarity/shared_case/shared_structured_id, snapshotted at
-- write time -- see entity_resolution.ResolutionDecision) against a
-- FRESHLY recomputed signal off the graph's current state -- the same
-- diff idiom src/graph/candidate_reprioritization.py already performs
-- for the pending queue (Milestone D1), generalized here to also cover
-- matches a human has already confirmed, and looking for DEGRADATION
-- (a fresh signal weaker than the original) rather than D1's own
-- REINFORCEMENT (a fresh signal stronger than the original).
--
-- SCOPE, STATED HONESTLY: this table's population is entity_resolution.py's
-- name-fallback tiers (flagged_unverified/human_review) -- the only
-- resolution outcomes with a comparable, persisted original-vs-current
-- scoring snapshot anywhere in this codebase (a SAME_AS edge's own write-
-- time properties, per resolve_and_write()). TIER_CNIC_AUTO merges never
-- create a SAME_AS edge at all (see resolve_and_write()'s own tier
-- branch) and versioning.write_node() overwrites node properties on every
-- MERGE with no per-write history log -- there is genuinely nothing to
-- diff a cnic_auto merge's current CNIC against without a new node-
-- history sidelog, which is a real, separate capability this module was
-- not sized to build (confirmed against the actual code before writing
-- this, not assumed from the plan's prose alone).
--
-- WHAT THIS TABLE IS NOT: a SAME_AS/CITES edge, or anything that can
-- confirm/reject a match. src/graph/entity_resolution_sampling.py (the
-- module that writes to this table) never imports
-- src.graph.versioning/age_client's write helpers -- structurally
-- incapable of writing to any graph edge/node, same "cannot do the risky
-- thing even by mistake" discipline candidate_reprioritization.py already
-- established for D1. A finding here is a HUMAN-FACING SUGGESTION,
-- surfaced through src/api/graph_review.py (reusing that existing review
-- surface, not a second queue) -- an investigator decides what, if
-- anything, to do about it through the existing confirm/reject
-- machinery, exactly as today.
--
-- Idempotent: CREATE TABLE IF NOT EXISTS, safe to re-run.
--
-- HOW TO APPLY
--   python scripts/apply_migration.py migrations/029_entity_resolution_consistency_findings.sql

CREATE TABLE IF NOT EXISTS entity_resolution_consistency_findings (
    finding_id              SERIAL PRIMARY KEY,

    -- The SAME_AS edge this finding is about. For a still-pending
    -- candidate, this IS the pending edge's own id. For an already-
    -- confirmed match, this is the CONFIRMED edge's id (not the
    -- superseded pending one it replaced) -- the id an investigator
    -- would actually recognize from graph_review.py's own responses.
    edge_id                 BIGINT NOT NULL,
    tier                    TEXT NOT NULL,
    status_at_detection     TEXT NOT NULL,  -- 'pending' | 'confirmed' -- what the edge's status was when this finding was recorded
    mention_entity_id       TEXT NOT NULL,
    candidate_entity_id     TEXT NOT NULL,

    original_basis          TEXT,
    original_name_similarity        DOUBLE PRECISION,
    original_shared_case            BOOLEAN,
    original_shared_structured_id   BOOLEAN,

    fresh_name_similarity           DOUBLE PRECISION,
    fresh_shared_case               BOOLEAN,
    fresh_shared_structured_id      BOOLEAN,

    -- Deterministic one-line explanation -- a template over the diffed
    -- fields above, same as candidate_reprioritization._why(); never LLM
    -- narration.
    finding_reason           TEXT NOT NULL,

    detected_at               TIMESTAMPTZ NOT NULL DEFAULT now(),

    -- Same "flag, then a human acknowledges" shape as G2's
    -- flagged_for_review/flagged_reason -- an acknowledgment records that
    -- an investigator looked at this finding, it asserts nothing about
    -- whether the underlying match is actually still good or bad (that
    -- judgment stays with confirm/reject on the edge itself, unchanged).
    acknowledged              BOOLEAN NOT NULL DEFAULT false,
    acknowledged_by           TEXT,
    acknowledged_at           TIMESTAMPTZ,

    -- One open finding per edge at a time -- a re-sample landing on the
    -- same still-open edge is a re-confirmation of an existing finding,
    -- not a second row (see entity_resolution_sampling.py's insert path).
    UNIQUE (edge_id, acknowledged)
);

CREATE INDEX IF NOT EXISTS ix_entity_resolution_consistency_findings_open
    ON entity_resolution_consistency_findings (acknowledged, detected_at DESC);
