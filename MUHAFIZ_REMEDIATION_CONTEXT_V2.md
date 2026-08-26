# Muhafiz — Agent Harness Data-Consistency Remediation: Session Context **v2**

> **This supersedes `MUHAFIZ_REMEDIATION_CONTEXT.md` (v1).** v1 was written before the
> Phase 2 purge was executed and is now materially out of date — it describes as
> "pending" several things that are complete, and does not contain Phase 3A or 3B at
> all. **Do not draft prompts from v1.** If both are in context, v2 wins on every
> factual conflict.

**Purpose:** hand this to a new ChatGPT conversation (alongside `PROJECT_OVERVIEW.md`,
`README.md`, and `PHASE_3B_SUBAGENT_READINESS_AUDIT.md`) so it can help draft the next
Claude Code prompts with full, current context.

**As of:** 2026-08-25, end of Phase 3B. Branch `agent-harness` @ `c96dead`.

---

## 0. What changed since v1 — read this first

v1 ended with "Phase 2 purge not yet executed." Since then, **all of the following
happened and were verified**:

| Work | v1 said | Actual now |
|---|---|---|
| Phase 2 purge (231 chunks) | pending approval | ✅ **DONE** — 1054 → 823, 33 REAL-* preserved |
| C1 community embeddings | blocker noted | ✅ **DONE** — Chroma 0 → 19, IDs match Postgres |
| T1 (Incident.description) | not identified | ✅ **CODE + DATA FIXED** — 73/73 populated |
| T2 (`"entry None"`) | not identified | ✅ **CODE + DATA FIXED** — 188 → 0 |
| CITES | not identified | Lost to purge (9→0), then ✅ **restored** (0→9) |
| 4 un-audited sub-agents | "open question, next step" | ✅ **AUDITED** — Phase 3B report exists |
| Jurisdiction alias map | Phase 3 | ⏳ **still not fixed** (unchanged) |
| `case_summarization` / `data_quality` | Phase 3 | Re-scoped by 3B — see §4 |

**One finding in v1 is now formally withdrawn:** v1 §6 says `data_quality.py:231-239`
has "zero-count labels masked by Person/StructuredRecord counts." **That was wrong.**
Phase 3B traced the actual mechanism: readiness keys on `total_entities` (a sum), and a
per-label zero is reported as data, never gating readiness. **Data Quality has zero
actionable findings.** Do not draft a prompt to "fix" it.

---

## 1. The problem (unchanged from v1, still accurate)

Muhafiz is a case-centric evidence-intelligence platform for Islamabad Police, with two
parallel pipelines: the **legacy orchestrator** (serves all live traffic) and the
**agent harness** (8 sub-agents, built and tested, **not yet live** —
`HARNESS_CUTOVER_ROUTES` is empty).

The underlying dataset was replaced: old synthetic corpus → real Pakistani FIR records
synced from a live Muhafiz Data API. Field values, taxonomies, and available capabilities
all differ. The remediation's goal is to make the eight sub-agents **trustworthy against
the new dataset** — not to add capabilities.

**Core principle being enforced:** *"Muhafiz does not guess."* The bugs found are cases
where a sub-agent returned a plausible wrong answer instead of admitting it couldn't
answer.

---

## 2. Working method (carry this forward — it has worked well)

- **Investigate → report → explicit approval → implement ONE phase → checkpoint → stop.**
- **No destructive action** without showing the exact target set and proving what's
  excluded. This has been honoured throughout (the 231-chunk purge required a frozen ID
  list; the graph re-projection required a rollback artifact and pre-flight assertions).
- **Shared-module changes always state orchestrator-vs-harness impact.** Fix once, both
  paths consistent, never diverging.
- **Claude Code is expected to push back and self-correct.** It has done so repeatedly
  and correctly — see §6, which is worth preserving because it's a sign the discipline
  is working.
- **Phase 4 (real-data test fixtures) is mandatory, not optional cleanup.**

A refinement worth adding for the next conversation: **Claude Code has been asked to
verify sub-agent claims rather than relay them**, and this has caught several wrong
numbers (including from its own earlier reports). Keep that instruction in future
prompts.

---

## 3. Current verified state (measured, not assumed)

### Data / graph
| Metric | Value |
|---|---|
| Cases / Incidents | 73 / 73 |
| **Incident.description populated** | **73/73** verbatim `narrative_text` (avg 1,347 chars, max 2,070) |
| OCCURRED_ON | 568 · `"entry None"` = **0** · valid `entry N` = 133 |
| OCCURRED_ON per Incident | min 3, **avg 5.9**, max 9 |
| SAME_AS | 1,248 → pending **1,224** / confirmed 19 / rejected 5 |
| Pending tier split | **`flagged_unverified` 1,204 / `human_review` 20** |
| **Confirmed SAME_AS crossing a case boundary** | **0** |
| Persons in >1 case | **4** · cross-case ASSOCIATED_WITH: **19 of 248** |
| CITES / CROSS_VERSION_OF / CONFLICTS_WITH | 9 / 0 / 0 |
| `conflicts_checked_at` | **0/73** (conflict detection has never run) |
| Chroma `muhafiz_kb` | **823** (post-purge) |
| Chroma community reports | **19** (restored; IDs == Postgres) |
| Communities spanning >1 case | **2 of 19** |
| `police_reference_data` | 21 rows, **all `synthetic`**, **0** covering CNSA/Arms |
| Cases with no reference coverage | **39/73 (53%)** |

### Repo
- Branch `agent-harness` @ `c96dead`, 0 behind `origin/main` (verify with `git fetch` —
  the teammate pushes often)
- **Uncommitted:** `src/graph/structured_projection.py` + `tests/test_structured_projection.py`
  (the T1/T2 fix, 10 new tests), plus README/`.gitignore`/frontend markdown fix and 4
  untracked `.md` reports
- Test suite: ~1,611 collected, **all green** (`tests/test_pdf_loader.py` excluded — known
  Docling/MSVC environment issue)

### Known isolated gap — deliberately NOT fixed
`fir-97-26` is missing one victim `INVOLVED_IN` edge. Victim name is
`نازیہ کوثر (بیک وقت مدعیہ)` ("Nazia Kausar, simultaneously the complainant"); the
parenthetical drags name similarity to **0.5556**, into the LLM-adjudication band
(0.55–0.90). Investigation proved this is **n=1 in the whole corpus** (1 annotated name
across all 73 FIRs, all four name fields). **Decision: leave it.** Do not build
normalization for one record; do not manually create the edge.

---

## 4. Phase 3B audit results — the current findings list

Full detail in `PHASE_3B_SUBAGENT_READINESS_AUDIT.md` (attach it). Summary:

### HIGH (3)
| ID | Finding | Records | Fix layer |
|---|---|---|---|
| **TB-1** | Every timeline event repeats the **entire ~1,347-char FIR narrative**; 3–9 events per case differ only by a ~20-char suffix. T1 fixed "no description"; it did not make descriptions *per-event*. | **73/73** | projection or agent-logic |
| **TB-2** | 65 `position` OCCURRED_ON edges have `detail = ""` → render as **dated events with zero content** | 65 edges | projection |
| **CCL-C3** | `xnetwork.py:105` flattens `case_ids` into a **union across all top-k**, so single-case clusters are presented as cross-case findings | **17/19 (89%)** | retrieval (**harness-only**) |

### MEDIUM (10)
TB-3 (`position` detail is sometimes a person's *role*, mis-dated), CCL-C2 (XGRAPH
"A recurring entity appears across N cases" uses aggregate footprint), CCL-C4 (1,224
unconfirmed links emitted uncapped, 1 caveat each), CS-1 (`Incident.description` can't
reach Case Summarization — Incident isn't a graph seed or hop target), CS-2/CS-3 (prompt
asks for status enum + vehicles/phones that don't exist), IA-I1 (synthetic reference rows
presented as real law), IA-I2 (53% of cases have no reference coverage), IA-I3
(`sql_param_extractor.txt` schema hardcodes old vocabulary), RD-1/RD-2 (Verifier reduced
to a paraphrase check; degradation inherited into durable PDFs).

### LOW (3) · NO ISSUE / already protected (17)
Including: the `conflicts_checked_at` guard correctly refuses a false all-clear on all
73 cases; **no cross-case leakage** (role gate enforced before retrieval — verified, not
assumed); CITES/CROSS_VERSION_OF correctly not traversed; **all five Data Quality checks
clean**.

---

## 5. Recommended Phase 3C order (from the 3B audit)

**3C-a — projection (highest value, harness-only blast radius)**
1. TB-2 / TB-3 — stop writing `detail = ""`; same file and `or ""` idiom as T2
2. TB-1 — decide whether the narrative belongs on every event (likely agent-logic:
   render base once, suffix per event)

**3C-b — harness-only (zero live risk)**
3. CCL-C3 — `xnetwork.py` return per-result `case_ids`
4. CCL-C2, IA-I1 (`tools/sql.py` provenance), CS-2/CS-3 (prompt vocabulary)

**3C-c — shared modules (one at a time, orchestrator verification each)**
5. CCL-C4 — tier floor + cap (**must preserve the 20 `human_review` links**)
6. Jurisdiction alias map · IA-I3 · CS-1 (highest blast radius)

**Then Phase 4 — mandatory fixtures.**

---

## 6. What NOT to do (hard boundaries)

- **`fir-97-26`** — n=1, leave it
- **Data Quality** — zero actionable findings; changing `_ENTITY_LABELS` or the conflict
  primary key would *introduce* defects
- **CCL-C3 by regenerating communities** — that's capability tuning, not drift repair
- **CCL-C4 with a blunt threshold** — would suppress the 20 genuine `human_review` links
- **RD-1** — a composition property, not a patchable bug
- **Out of scope, unchanged:** `cross_silo_projection.py:602`, `muhafiz_records.py:171`
  (`zimni_entry_None` / `"Zimni entry None"` — related-looking, different fields)
- **Parked, do not start:** query-decomposition sub-agent; `section_code` crime
  classification

---

## 7. Corrections Claude Code has made to its own work (evidence the discipline works)

Worth preserving — these are the kind of self-corrections to keep demanding:

1. **Jurisdiction bug severity** — initially called "P0-security-adjacent," corrected to
   **P1 precision**. The role gate is independent and unaffected; only supervisor+ reach
   these routes. It's scope-narrowing *within already-authorized access*, not an
   authorization bypass.
2. **Jurisdiction root cause** — first described as a `toLower` no-op. **Wrong:**
   `station_id` is ASCII and works. Real gap is **no alias map** between English city
   names and Urdu-script / abbreviated-ASCII IDs. Fails asymmetrically in both language
   directions.
3. **"0 LLM adjudications expected, structurally"** — measured pre-mutation, then
   **disproven during the actual re-projection** (fired once, on `fir-97-26`). A hard
   guard blocked the call. Lesson: static measurement of a graph that the operation
   itself mutates is not a guarantee.
4. **`data_quality` "dead Organization label"** — **withdrawn**, mechanism was
   misdescribed.
5. **Multiple wrong tier counts** — including from sub-agents. Direct measurement:
   pending SAME_AS is **1,204 `flagged_unverified` / 20 `human_review`**.
6. **CITES loss** — the approved purge deleted 9 CITES edges as a side effect Claude Code
   had *not* predicted in its blast-radius analysis. It reported this rather than
   quietly restoring them, then reconstructed all 9 deterministically from source and
   restored them under separate approval.

---

## 8. What I need from the new ChatGPT conversation

Using this v2 doc + `PROJECT_OVERVIEW.md` + `README.md` +
`PHASE_3B_SUBAGENT_READINESS_AUDIT.md`:

Help me draft the next Claude Code prompt(s), following the discipline in §2. The
immediate decision is **how to scope Phase 3C** — specifically whether to:

- **(a)** take 3C-a alone (TB-2/TB-3 first as the smallest well-understood projection
  fix, then TB-1 as a separate decision), or
- **(b)** batch all of 3C-a + 3C-b since both are harness-only/zero-live-risk, or
- **(c)** do something else entirely given the findings

Constraints to bake into any prompt:
- read-only investigation first, then stop for approval before implementing
- no destructive action without an explicit approved target list
- any shared-module touch must state live-orchestrator impact explicitly
- do not let scope drift toward the parked items in §6
- require Claude Code to **verify claims against live data**, not relay sub-agent output
- Phase 4 fixtures remain mandatory before this effort is considered closed
