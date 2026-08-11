-- Migration 019: per-message degradation trace on `messages`
--
-- Background: every harness sub-agent computes `tools_used`, `degraded_from`
-- and `caveats` on its SubAgentResult, and the supervisor's completion hook
-- already writes that (via build_degradation_trace()) into
-- `pipeline_steps.output_summary` for the admin Run History page. That surface
-- is platform-admin only.
--
-- Per direct product guidance, investigators need the same "what worked, what
-- failed" visibility for their OWN query, in their own chat, durably — not
-- live-only. Today the chat UI renders rich pipeline detail while the answer
-- streams and then loses all of it on reload, because `messages` stores only
-- role/content and get_session_history() returns only those fields. This
-- column is what makes that transparency survive a page refresh.
--
-- WHY A COLUMN AND NOT A NEW TABLE. The trace is strictly 1:1 with an
-- assistant message, has no independent lifetime, and is always read together
-- with the message it belongs to. A separate table would buy a join on every
-- history read plus a second RLS policy to write and keep correct, for no
-- gain. `pipeline_steps.output_summary` and `audit_logs.details` set the
-- precedent for JSONB-on-the-owning-row in this schema.
--
-- RLS: `messages` is already ENABLE + FORCE ROW LEVEL SECURITY with
-- `messages_isolation_policy` joining through `sessions.case_id` (migration
-- 010). A new column is covered by that policy automatically — no policy
-- change is needed, and no grant change either, since migration 015's
-- least-privilege role holds table-level DML on `messages`. This is a further
-- argument against the separate-table option, which would have needed both.
--
-- NULLABLE ON PURPOSE. Every message written before this migration — and
-- every message produced by the legacy orchestrator path, which does not call
-- build_degradation_trace() — reads as NULL. NULL means "no trace recorded",
-- which readers must NOT render as "clean run": those are different facts, the
-- same distinction ConflictState.UNKNOWN vs NONE draws elsewhere in the
-- harness. The frontend renders nothing at all for NULL.
--
-- Payload shape is build_degradation_trace()'s output (see
-- src/pipeline/harness/events.py), versioned via its own `v` key so a reader
-- meeting an older payload knows which contract it is looking at.

ALTER TABLE messages
    ADD COLUMN IF NOT EXISTS degradation_trace JSONB NULL;

COMMENT ON COLUMN messages.degradation_trace IS
    'Per-query degradation trace from the agent harness (build_degradation_trace() '
    'output: tools_used/degraded_from/contributed_only/degraded_and_contributed/'
    'degraded_only/caveats, plus a version key). NULL means no trace was recorded '
    '- a pre-harness or legacy-path message - which is NOT the same as a clean run.';
