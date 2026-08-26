# Muhafiz — Agent Harness Data-Consistency Remediation: Session Context **v3**

> **This supersedes v2 (`MUHAFIZ_REMEDIATION_CONTEXT_V2.md`) and v1.** v2 was written at
> the end of Phase 3B; Phase 3C-a and 3C-b.1 have since completed, and the teammate has
> pushed 18 commits that materially change the picture. **Draft prompts from v3 only.**
> If v2 or v1 conflict with this document, v3 wins.

**Purpose:** hand to a new ChatGPT conversation (with `PROJECT_OVERVIEW.md`, `README.md`,
and `PHASE_3B_SUBAGENT_READINESS_AUDIT.md`) so it can help draft the next Claude Code
prompts with current context.

**As of:** 2026-08-26. Branch `agent-harness` @ `c96dead`, **18 commits behind
`origin/main`**.

---

## 0. What changed since v2

| Work | v2 said | Now |
|---|---|---|
| TB-1 (repeated narrative) | HIGH, unfixed | ✅ **DONE** — narrative rendered once |
| TB-2 (blank `position`) | HIGH, unfixed | ✅ **DONE at presentation layer**; projection hygiene deferred |
| TB-3 (role labels) | MEDIUM | ⛔ **Confirmed out of scope** — upstream semantics |
| CCL-C3 (single-case shown as cross-case) | HIGH, unfixed | ✅ **DONE** — per-community attribution |
| Everything else in §4 | open | still open |

**Two corrections v2 got wrong**, established by tracing code:

1. **v2 called `xnetwork.py` "harness-only". It is not** — `orchestrator.py:40` imports
   it, used at `:1574`. The CCL-C3 fix stayed harness-only *only because* the orchestrator
   reads just `net_result["results"]` and never the flattened `case_ids`. **A future fix
   touching `xnetwork.py:105` would enter shared/live code.**
2. **TB-2's fix produces no user-visible change on its own.** `""` and an absent key
   rendered identically, so the projection fix + re-projection would have cost the full
   3A blast radius for zero output change. That is why it was deferred and solved at the
   presentation layer instead.

---

## 1. The problem (unchanged, still accurate)

Muhafiz is a case-centric evidence-intelligence platform for Islamabad Police, with two
pipelines: the **legacy orchestrator** (serves all live traffic) and the **agent
harness** (sub-agents, built and tested, **not live** — `HARNESS_CUTOVER_ROUTES` empty,
and the router has no classification signal for several sub-agents).

The dataset was replaced: old synthetic corpus → real Pakistani FIR records from a live
Muhafiz Data API. The remediation makes sub-agents **trustworthy against the new
dataset** — not to add capabilities.

**Core principle:** *"Muhafiz does not guess."* Every bug found was a case of returning a
plausible wrong answer instead of admitting it couldn't answer.

---

## 2. Working method (keep this — it has repeatedly paid off)

- **Investigate → report → explicit approval → implement ONE phase → checkpoint → stop.**
- **No destructive action** without an exact target set and proof of what's excluded.
- **Shared-module changes always state orchestrator-vs-harness impact.**
- **Claude Code must verify claims against live data, not relay sub-agent output.** This
  has caught wrong numbers repeatedly, including from its own earlier reports.
- **Phase 4 (real-data fixtures) is mandatory**, not optional cleanup.

Two refinements worth adding to future prompts:

- **Require a mutation-check on new tests** — confirm they actually fail against the old
  behavior. Done for TB-1, TB-2 and CCL-C3; it caught nothing wrong but proves the tests
  aren't vacuous.
- **Require an explicit "did anything change that I didn't cause?" check.** This surfaced
  unexplained graph drift (see §6).

---

## 3. Current verified state

### Repo
- Branch `agent-harness` @ `c96dead`, **18 commits behind `origin/main`** — needs a pull
- **Uncommitted (all approved work):** 7 source/test files + 6 untracked `.md` reports
- Tests: **1,682 collected, all passing** (`tests/test_pdf_loader.py` excluded — known
  Docling/MSVC environment issue)

### Data
| Metric | Value |
|---|---|
| Cases / Incidents | 73 / 73 |
| Incident.description populated | **73/73** |
| OCCURRED_ON | 568 · `"entry None"` = **0** |
| `position` edges | 94 (65 blank `detail`, 29 with content) |
| Chroma `muhafiz_kb` / community | 823 / **19** |
| Communities spanning >1 case | **2 of 19** |
| Total graph edges | **8,182** (was 7,853 — see §6) |

---

## 4. Completed phases

**Phase 1** — XAGG stopped reporting unevaluatable filters as zero (shared `xagg.py`,
both paths fixed identically). Committed as `1a81bff`.

**Phase 2** — Sync + selective purge: Chroma 1,054 → 823, 33 `REAL-*` reference chunks
preserved, all 73 cases aligned.

**Phase 3A** — T1 (Incident narratives, 0→73/73), T2 (`"entry None"` 188→0), C1
(community embeddings 0→19), CITES lost to the purge then deterministically restored
(9). Code + existing-data both corrected via a targeted re-projection.

**Phase 3B** — Full read-only audit of all remaining sub-agents →
`PHASE_3B_SUBAGENT_READINESS_AUDIT.md`.

**Phase 3C-a** — TB-1 (narrative once in `answer_text`) and TB-2 (blank `position`
renders `position: no position recorded`). Harness-only.

**Phase 3C-b.1** — CCL-C3. Added `XNetworkToolResult.community_case_ids` (index-aligned
per-community case IDs); `_xnetwork_links` now uses each community's own set.
`case_ids_touched` deliberately remains the union (Verifier `[PRESERVE]` contract).
Verified on live data: 3 communities previously all reported as spanning 4 cases now
report 1, 2 and 1 correctly.

---

## 5. Open findings (from the 3B audit)

**Harness-only, zero live risk — the natural next batch:**
- **CCL-C2** — XGRAPH renders "A recurring entity appears across N case(s)" from the
  aggregate footprint when `target_entity` is None
- **IA-I1** — synthetic reference rows presented as authoritative law (`tools/sql.py`
  drops `source_type`)
- **CS-2 / CS-3** — Case Summarization prompt asks for a status enum and
  vehicles/phones/organizations the data lacks (Vehicle 2, PhoneNumber 2, Organization 0)

**Shared modules — one at a time, orchestrator verification each:**
- **CCL-C4** — 1,224 pending SAME_AS emitted uncapped (must preserve the 20
  `human_review`)
- **Jurisdiction alias map** — no English district name resolves
- **IA-I3** — `sql_param_extractor.txt` schema hardcodes old vocabulary
- **CS-1** — `Incident` unreachable by graph traversal (highest blast radius)

**Deferred by explicit decision:**
- TB-2 projection hygiene (`structured_projection.py:1244`) + existing-data re-projection
- `fir-97-26`'s missing victim edge (n=1)

---

## 6. Two things needing attention before the next phase

**a) 18 commits behind — and they change audit assumptions.** The teammate added
**two new sub-agents** (Local Search, Global Search / map-reduce), a real
community-detection hierarchy, adaptive multi-method retrieval, a cross-case graph-leak
fix, and SAME_AS duplicate dedup + bulk-confirm. **None touch our modified files** (verified —
no conflict), but the 3B audit predates them, so its sub-agent inventory is now
incomplete.

**b) Unexplained graph drift.** Total edges moved 7,853 → **8,182** (APPEARS_IN +123,
BELONGS_TO_CASE +126, SAME_AS +114) between Phase 3B and 3C-b.1. Investigated: newest
edges timestamped 18:00, ~5.5h before that phase began, carrying the **hashed**
`source_doc_id` form written by the legacy LLM extraction path — **not** our
re-projection's clean prefix. Our phases modified no graph writer. The teammate's Module
11 SAME_AS bulk-confirm work is the likely cause. **Not acted on; worth confirming.**

---

## 7. What NOT to do

- **`fir-97-26`** — n=1, leave it
- **Data Quality** — zero actionable findings; changing it would introduce defects
- **CCL-C3 by regenerating communities** — capability tuning, not drift repair
- **CCL-C4 with a blunt threshold** — would suppress the 20 real `human_review` links
- **TB-3** — upstream semantic overloading; do not classify Urdu strings
- **`xnetwork.py:105`** — shared with the live orchestrator
- **Out of scope, unchanged:** `cross_silo_projection.py:602`, `muhafiz_records.py:171`
- **Parked, do not start:** query-decomposition sub-agent, `section_code` classification

---

## 8. Corrections Claude Code has made to its own work

Preserved as evidence the discipline works — keep demanding this:

1. Jurisdiction bug: "P0-security-adjacent" → **P1 precision** (role gate independent
   and intact)
2. Jurisdiction root cause: not a `toLower` no-op → **no EN↔UR↔ASCII alias map**
3. "0 LLM adjudications expected, structurally" → **disproven live** (fired once on
   `fir-97-26`; hard guard blocked it). Static measurement of a graph the operation
   itself mutates is not a guarantee
4. `data_quality` "dead Organization label" → **withdrawn**, mechanism misdescribed
5. Multiple wrong SAME_AS tier counts, including from sub-agents → measured directly:
   **1,204 `flagged_unverified` / 20 `human_review`** among pending
6. **CITES loss** — the approved purge deleted 9 edges as an unpredicted side effect;
   reported rather than quietly restored, then reconstructed deterministically under
   separate approval
7. **v2's "xnetwork.py is harness-only"** → **corrected** (see §0)

---

## 9. What I need from the new ChatGPT conversation

Help draft the next Claude Code prompt(s), following §2. The immediate decisions:

1. **Pull the 18 commits first?** They add two sub-agents the 3B audit never covered.
   Options: (a) pull + re-audit scope before continuing, (b) pull + continue 3C-b as
   planned, (c) continue first, pull at a natural break.
2. **Next finding** — CCL-C2, IA-I1, or CS-2/CS-3 (all harness-only, zero live risk).
3. **When to commit** — 7 source/test files of approved work are still uncommitted.

Constraints for any prompt:
- read-only investigation first, stop for approval before implementing
- no destructive action without an approved, explicit target list
- shared-module touches must state live-orchestrator impact
- require verification against live data, not sub-agent relay
- don't drift toward §7's parked items
- Phase 4 fixtures remain mandatory before this effort closes
