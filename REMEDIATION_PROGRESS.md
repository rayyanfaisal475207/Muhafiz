# Muhafiz Agent-Harness Remediation — Progress Report

**Branch:** `agent-harness` @ `5c32ea0`
**Date:** 2026-08-26
**Status:** ~85% complete · all HIGH-priority findings closed · all approved fixes checkpointed

---

## 1. What this work is

Muhafiz's underlying dataset was replaced: the old synthetic corpus was swapped for real
Pakistani FIR records from the live Muhafiz Data API. The agent harness had been built
and tested against the old data.

**Goal:** make the harness sub-agents trustworthy against the new dataset. Not new
capabilities — correctness only.

Every defect found is the same class of failure: the system returning a *plausible wrong
answer* instead of admitting it could not answer. That violates the project's core
principle — **"Muhafiz does not guess."**

**Scope note:** the harness is not live — `HARNESS_CUTOVER_ROUTES` is empty. The one
exception is the jurisdiction fix (§5), which is shared/live code reached by the
orchestrator, and was verified on the live path before checkpointing.

---

## 2. Working method

Every change followed the same gate:

**investigate → report → explicit approval → implement ONE finding → verify →
mutation-check → checkpoint → stop**

Three disciplines that repeatedly caught real problems:

- **Verify against live data, never relay a sub-agent's numbers.** This caught wrong
  SAME_AS counts three times, and a sub-agent audit that misreported entity counts by 5x.
- **Mutation-check every new test** — restore the old behaviour and prove the new tests
  fail against it. This caught a vacuous test during GS-1 and forced a correction to a
  badly-designed mutation during the jurisdiction phase.
- **Explicit "did anything change that I did not cause?" check** before every commit.
  This surfaced both the legacy re-ingestion writer (§8) and the Chroma isolation failure
  (§4).

Three findings were **investigated and then closed or deferred without code changes**
(§7). Two of the three turned out to be materially different from how the original audit
recorded them.

---

## 3. Completed and checkpointed

| Phase | Scope | Commit |
|---|---|---|
| **Phase 1** | XAGG stopped reporting unevaluatable filters as zero (shared; both paths fixed identically) | `1a81bff` |
| **Phase 2** | Corpus sync + selective purge: Chroma 1,054 → 823, all 33 `REAL-*` reference chunks preserved | — |
| **Phase 3A** | T1 Incident narratives 0 → **73/73** · T2 `"entry None"` 188 → **0** · C1 community embeddings 0 → **19** · CITES restored (9) | — |
| **Phase 3B** | Full read-only audit of every sub-agent → severity-rated finding list | — |
| **Phase 3C-a** | TB-1 narrative repetition · TB-2 blank `position` events | `611561e` |
| **Phase 3C-b** | CCL-C3 per-community attribution | `611561e` |
| **Merge** | Reconciled 19 upstream commits — 37 files, +6,127 lines, **0 conflicts** | `67a5ac8` |
| **Phase 3C-c** | CCL-C2 singular-entity attribution | `74ef93b` |
| **Phase 3C-d** | GS-1 Global Search community scope | `3ba6ef1` |
| **Test safety** | Global pytest Chroma isolation + fail-closed guard | `44acd8c` |
| **Jurisdiction** | Bilingual district/station alias resolution (**shared/live**) | `5c32ea0` |
| **LS-1** | Entity embeddings populated — operational, Chroma-only, no commit | — |

### The five harness code fixes

**TB-1 — timeline events repeated the entire FIR narrative.** With 3–9 events per case
the same ~1,347-char narrative repeated ~5.9x per answer, across **73/73 cases**.
Measured reduction: 577,488 → ~98,400 characters.

**TB-2 — `position` edges rendered as contentless dated events** (65 edges). Now renders
`position: no position recorded`.

**CCL-C3 — single-case clusters presented as cross-case links.** The top-k union of case
IDs was stamped onto every link. **17 of 19 communities are single-case.**

**CCL-C2 — aggregate traversal footprint attributed to one entity.** On open-ended
queries with no seed entity, output asserted *"A recurring entity appears across N
case(s)"* — inventing an entity the evidence never established.

**GS-1 — Global Search dropped per-community case attribution.** 17 single-case clusters
became indistinguishable from the 2 genuinely cross-case ones. Per-community footprints
now reach both map and reduce as deterministic labels built from structured data, never
from prose.

---

## 4. Test-safety fix — a finding discovered mid-remediation (`44acd8c`)

`CHROMA_PERSIST_DIR` is read from the environment at import time, so **a pytest process
with no override resolved to the live `data/chroma_db`**, and isolation was per-file and
opt-in. Any test reaching a real Chroma call could mutate production persistence.

Two protections now live in test infrastructure only:

- **Global isolation** — every pytest session gets a disposable temp persist root, set at
  the top of `conftest.py` before any application module can read the variable.
- **Fail-closed guard** — `chromadb.PersistentClient` is wrapped so that opening the
  production root during a test *raises* rather than proceeding.

Both are mutation-verified. Production Chroma semantics are unchanged.

---

## 5. Jurisdiction fix (`5c32ea0`) — the first shared/live change

English district names did not resolve: `Lahore` → nothing, while `لاہور` → `DIST-04`.
District `name` values are Urdu-only and `district_id` is opaque, so no English query
could match. When resolution failed the system did not narrow at all, and a supervisor
asking about one district received **platform-wide figures presented as that district's**.

**What the fix does:**

- English aliases now resolve deterministically — no LLM, no transliteration, no data
  migration — scoped strictly to the 9 real districts and 19 real stations.
- Normalization: **NFKC + trim + internal-whitespace collapse + casefold**.
- **`Lahore` and `لاہور` resolve to the same 18-case scope** (previously English fell
  through to all 73).
- Generic **`Karachi` fails safely as ambiguous** — two real Karachi districts exist, so
  picking either would silently narrow to half the city; it discloses instead.
- **`Karachi Central` / `Karachi East`** resolve specifically.
- Deterministic **station aliases** added where provable from the ASCII `station_id`
  (e.g. `Model Town` → `PS-LHR-MODELTOWN`); `Saddar` and `Cyber` are treated as ambiguous
  because each names two real stations.
- **Accidental substring matches removed** — `Karachi` used to resolve to
  `PS-KHI-NEWKARACHI` purely because `khi` sits inside that id.
- **RBAC unchanged** — investigator still denied, supervisor+ unaffected except in result
  scope.

**Verification:** 1,811 passed / 5 skipped at implementation time; the later guarded full
suite passed 1,819 / 5 skipped.

---

## 6. LS-1 — restored and test-safe, currently stale by 7

**Original population:** 822 entities. **Those rows were unexpectedly lost.** The
historical deleter **remains unproven** — the loss coincided with a full-suite run, but
after the isolation fix a complete suite ran with the guard armed and nothing tripped,
which weakens rather than confirms the in-suite hypothesis.

Pytest Chroma isolation was then hardened globally and fail-closed (§4), and LS-1 was
restored against a **newly-approved exact target of 836 entities**.

| | Value |
|---|---|
| Approved target | **836** (Person 698 / Officer 137 / PhoneNumber 1) |
| Target checksum | `9f341cdd4a24283f40372dd06e568f67d307ff65f6af2da07c0b21798bca884e` |
| First refresh | `scanned=1163 · upserted=836 · deleted=0` |
| Second refresh | `upserted=0 · deleted=0` (idempotence proven) |
| Set equality | `target − chroma = 0`, `chroma − target = 0` |
| Local Search | semantic retrieval ranked the target #1; case scoping enforced; empty `case_id` fails closed |
| Guarded full suite | **1,824 collected · 1,819 passed · 5 skipped · 0 failed** |
| Live Chroma after suite | **823 / 19 / 836**, unchanged, checksum identical |

**LS-1 survived a complete guarded suite intact** — the outcome that failed before the
isolation fix.

### Status — not fully synchronized

**Operationally restored and test-safe at the approved 836-entity checkpoint; currently
stale by 7 entities because ongoing legacy re-ingestion changed the graph target to 843.**

During the guarded suite the legacy writer caused:

- graph edges **9,154 → 9,262**
- SAME_AS **1,650 → 1,682**
- confirmed SAME_AS **19** (unchanged)
- eligible LS-1 target **836 → 843** — 7 additional Person entities, 0 removed

All 7 belong to **`fir-1001-26`, a legitimate corpus case**. A top-up to 843 was
deliberately **not** performed: it needs its own approval, and the recurring writer means
the number may move again.

---

## 7. Findings closed or deferred without code changes

| Finding | Outcome | Why |
|---|---|---|
| **CS-3** | **Closed — no defect** | The prompt already treats vehicles/phones/organizations conditionally and forbids guessing; retrieval supports all five labels. Vehicle/Organization absence is corpus sparsity, not a defect. |
| **CS-2** | **Reclassified + deferred** | The "hardcoded status enum" was **disproven** — the prompt lists *examples*, not an enum, and says "if the material indicates one". The real issue is that `investigation_status` never reaches the agent: a **shared data-plumbing gap**. |
| **IA-I1** | **Deferred, narrowed** | `source_type` is missing from `ChunkMetadata`, but `text=str(row)` already puts it in the chunk text, **so the LLM and Verifier do see it**. The gap is structured metadata/citations only. All 21 rows are `synthetic`, and the live orchestrator has the identical pattern. |

---

## 8. Unresolved: legacy re-ingestion / synchronization issue

Tracked separately from this remediation, **not investigated or fixed in these phases**.

Recurring bursts of **+108 edges / +32 SAME_AS** have now been observed six times
(7,853 → 8,182 → 8,830 → 8,938 → 9,046 → 9,154 → 9,262). Characteristics:

- Every added edge carries the hashed `psrms_fir_` provenance form written by the legacy
  LLM extraction path; the clean `psrms/fir` form written by our re-projection has held at
  **exactly 62** throughout.
- **Confirmed SAME_AS has stayed pinned at 19** across every measurement, ruling out the
  earlier hypothesis that Module 11's bulk-confirm was responsible.
- Activity centres on **`fir-1001-26`, a legitimate corpus case**.
- It changes the entity/provenance footprint, which is what moves the LS-1 target.
- One burst occurred with **zero Python processes running** and the backend stopped.

**Classification: a legacy re-ingestion / synchronization / idempotency issue — not
foreign-data contamination.** The corpus remains exactly **73 relational cases and 73
graph Case nodes** throughout.

> **Correction to earlier reporting:** an earlier version of this document and several
> phase reports described `fir-1001-26` as "out-of-corpus". That was wrong. It is present
> in the `cases` table and is a legitimate case; the issue is repeated re-ingestion of it,
> not insertion of foreign data.

---

## 9. Remaining work

### MEDIUM — 7 open

| Finding | What it is | Layer |
|---|---|---|
| **LS-2** | Degradation caveat misstates its cause ("no match found" vs. empty store) — lower priority now that the store is populated | Harness-only |
| **GS-2** | Leakage check inert on this route — consequence of GS-1, not independent | Harness-only |
| **IA-I1** | `source_type` absent from structured metadata/citations (deferred, §7) | Harness-only |
| **CS-2** | `investigation_status` never reaches Case Summarization (reclassified, §7) | **SHARED / live** |
| **CS-1** | `Incident` unreachable by graph traversal — not in `_SEED_LABELS`; **highest blast radius** | **SHARED / live** |
| **CCL-C4** | 1,224+ unconfirmed SAME_AS links emitted uncapped (must preserve the 20 `human_review`) | **SHARED / live** |
| **IA-I3** | `sql_param_extractor.txt` hardcodes old-corpus vocabulary | **SHARED / live** |

### Operational — 1 open

**LS-1 top-up 836 → 843** (7 Person entities from `fir-1001-26`). Purely additive, 0
removals. Needs its own approval.

### LOW — 4 open, all deliberately deferred

CCL-C5 (slow undirected query), CS-4 (bounded by three stacked guards), RD-6 (token cap
vs Urdu), LS-4 (partly-inapplicable label guard). None actionable.

### Parked by explicit decision

TB-3 (upstream semantic overloading), IA-I2 (data coverage gap, already disclosed),
IA-I4 / RD-1 (inherent verifier limits), `fir-97-26` victim edge (n=1), Data Quality
(zero actionable findings), CS-3 (closed, §7).

### Phase 4 — mandatory, pending

Test fixtures are still largely synthetic. **GS-1 is the proof this matters:** its
fixtures were 50% multi-case against a corpus that is 10.5% multi-case, and no test
asserted per-chunk attribution — exactly why the defect shipped green. The jurisdiction
tests are the first to use real corpus values (the 9 Urdu district names, real `PS-LHR-*`
station ids) and should be the model.

---

## 10. Progress estimate

**Overall ~85%.**

| Area | Done | Note |
|---|---|---|
| Structural / data repair (Phases 1–3A) | **100%** | The heavy half — corpus split, re-projection, embeddings |
| Harness-only code fixes | **~90%** | 5 shipped; LS-2 and the deferred IA-I1 remain |
| Shared-module fixes | **~40%** | Jurisdiction checkpointed; CS-1, CCL-C4, IA-I3, CS-2 remain |
| Test-infrastructure safety | **100%** | New finding, found and fixed this cycle |
| Phase 4 real-data fixtures | **~10%** | Jurisdiction tests use real values; the rest do not |

All figures are effort estimates, not measurements. By raw finding count the picture is
more conservative — the breakdown above is the honest way to read it.

---

## 11. Test and data state

| | Value |
|---|---|
| Full backend suite | **1,824 collected · 1,819 passed · 5 skipped · 0 failed** |
| Harness compliance | **58 / 58** |
| Exclusion | `tests/test_pdf_loader.py` — known Docling/MSVC environment issue |
| Graph edges | 9,262 |
| SAME_AS | 1,682 (19 confirmed) |
| Chroma | `muhafiz_kb` 823 · `muhafiz_community_reports` 19 · `muhafiz_entity_descriptions` **836** |
| Corpus | 73 relational cases · 73 graph Case nodes |

All test counts read from JUnit XML, not console output.

---

## 12. Next steps, in order

1. **LS-1 top-up to 843** (optional, needs approval) — purely additive.
2. Remaining harness-only work: **LS-2**, and **IA-I1** if its narrowed scope is worth it.
3. Shared-module findings one at a time with live-orchestrator verification on each:
   **CS-1** (highest blast radius), **CCL-C4**, **IA-I3**, **CS-2**.
4. **Phase 4 real-data fixtures** before this effort closes.
5. Separately, for whoever owns ingestion: the **legacy re-ingestion / synchronization
   issue** in §8.
