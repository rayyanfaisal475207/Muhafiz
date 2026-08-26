# Muhafiz Agent-Harness Remediation — Progress Report

**Branch:** `agent-harness` @ `d5fa333`
**Date:** 2026-08-26
**Status:** ~90% complete (effort estimate, not a measured completion percentage) · all HIGH-priority findings closed · future graph drift fixed · live test-fixture contamination removed

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

**Scope note:** the harness is not live — `HARNESS_CUTOVER_ROUTES` is empty. Two
exceptions touch shared/live code: the jurisdiction fix (§5) and the ingestion
idempotency fix (§7), both verified on the live path before checkpointing.

---

## 2. Working method

Every change followed the same gate:

**investigate → report → explicit approval → implement ONE finding → verify →
mutation-check → checkpoint → stop**

Three disciplines that repeatedly caught real problems:

- **Verify against live data, never relay a sub-agent's numbers.** This caught wrong
  SAME_AS counts three times, and a sub-agent audit that misreported entity counts by 5x.
- **Mutation-check every new test** — restore the old behaviour and prove the new tests
  fail against it. This caught a vacuous test during GS-1, a badly-designed mutation
  during the jurisdiction phase, and an unsafe "mark complete too early" implementation
  during the ingestion idempotency phase.
- **Explicit "did anything change that I did not cause?" check** before every commit.
  This surfaced the legacy re-ingestion writer (§6), the Chroma isolation failure (§4),
  the Postgres/AGE isolation failure (§4), and ultimately the test-fixture contamination
  of the SAME_AS pool (§8).

Several findings were **investigated and then closed, deferred, or corrected without code
changes** (§10). Two of them turned out to be materially different from how the original
audit recorded them. One destructive phase (§8) **halted itself** at a safety gate because
a measured endpoint set was 25, not the assumed 24 — that halt prevented deletion of a
real corpus record.

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
| **Test safety (Chroma)** | Global pytest Chroma isolation + fail-closed guard | `44acd8c` |
| **Jurisdiction** | Bilingual district/station alias resolution (**shared/live**) | `5c32ea0` |
| **Test safety (Postgres/AGE)** | Global pytest DB isolation + fail-closed guard | `9f7ca35` |
| **Future-drift fix** | Graph extraction made idempotent by chunk (**shared/live**) | `d5fa333` |
| **LS-1** | Entity embeddings populated — operational, Chroma-only, no commit | — |
| **SET B cleanup** | Live test-fixture graph remnants removed — operational, no commit | — |

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

## 4. Test-infrastructure safety — two findings, both fixed

Both were discovered by the "did anything change that I did not cause?" discipline, and
both are **test-infrastructure only** — no production code was changed by either.

### Chroma isolation (`44acd8c`)

`CHROMA_PERSIST_DIR` is read from the environment at import time, so a pytest process with
no override resolved to the live `data/chroma_db`, and isolation was per-file and opt-in.
Fixed with a disposable per-session persist root set before any application import, plus a
fail-closed guard that raises if a test opens the production root.

### Postgres / AGE isolation (`9f7ca35`)

**pytest previously resolved the live Postgres/AGE database** —
`postgresql+asyncpg://postgres:dev@localhost:5432/muhafiz`. `tests/conftest.py` had no DB
isolation, and the general `no_network` guard **explicitly allows loopback**, which is
exactly where Postgres listens.

Fixed with global test DB isolation (a disposable `muhafiz_pytest_<uuid>` identity set
before any application import) plus a fail-closed guard on `asyncpg.create_pool` — the
single boundary every AGE write crosses. Production DB semantics unchanged.

Verification: guarded full suite **1,833 collected · 1,828 passed · 5 skipped · 0 failed**,
with **live Postgres/AGE unchanged** throughout and zero guard trips.

**Important wording:** the pytest live-DB exposure was a genuine safety defect, but **the
historical re-ingestion trigger remained unproven**. This fix closed a real pathway; it
did not establish that pytest caused the historical bursts.

---

## 5. Jurisdiction fix (`5c32ea0`) — first shared/live change

English district names did not resolve: `Lahore` → nothing, while `لاہور` → `DIST-04`.
District `name` values are Urdu-only and `district_id` is opaque, so no English query
could match. When resolution failed the system did not narrow at all, and a supervisor
asking about one district received **platform-wide figures presented as that district's**.

- Deterministic aliases — no LLM, no transliteration, no data migration — scoped to the
  9 real districts and 19 real stations.
- Normalization: **NFKC + trim + internal-whitespace collapse + casefold**.
- **`Lahore` and `لاہور` resolve to the same 18-case scope.**
- Generic **`Karachi` fails safely as ambiguous** (two real Karachi districts exist);
  **`Karachi Central` / `Karachi East`** resolve specifically.
- Station aliases added only where provable from the ASCII `station_id`; `Saddar` and
  `Cyber` treated as ambiguous because each names two real stations.
- **Accidental substring matches removed** — `Karachi` used to resolve to
  `PS-KHI-NEWKARACHI` purely because `khi` sits inside that id.
- **RBAC unchanged.**

Verification: 1,811 passed / 5 skipped at implementation time; later guarded suites passed
1,819 and 1,840.

---

## 6. Legacy re-ingestion — root cause proven

The recurring graph drift (~+108 edges per burst, observed repeatedly) is now understood.

**The mechanism, proven from code and live data:**

- Affected case: **`fir-1001-26` — a legitimate corpus case.** It is present in the
  `cases` table; the corpus remains exactly **73 relational cases and 73 graph Case nodes**.
- Repeated, stable narrative chunk: `psrms_fir_fir-1001-26#narrative_c8bf2613`.
- Two provenance namespaces exist: clean `psrms/fir/...` (structured projection) and
  sanitized `psrms_fir_...`, produced because `src/ingestion/document.py` replaces `/`
  with `_`.
- `scripts/sync_muhafiz_data.py` passes **`run_graph_extraction=False`** at every call
  site, and purges by the clean `psrms/fir/{fir_id}#` prefix — correct by design.
- Generic ingestion (e.g. `ingest_file`) **defaults to `run_graph_extraction=True`**, so
  it runs the LLM/NER pass and writes the sanitized namespace, which sync's purge never
  cleans.
- `entity_resolution._new_entity_id()` mints a **random `uuid4`** for Person identities.
  Only exact CNIC-auto resolution (`TIER_CNIC_AUTO`) reuses an existing entity.

**Therefore** an unchanged, CNIC-less Person extraction could historically create *new*
Person nodes on every replay, each carrying its own APPEARS_IN / BELONGS_TO_CASE edges and
generating fresh pending SAME_AS candidates.

Observed historical burst shape: **+41 APPEARS_IN, +35 BELONGS_TO_CASE, +32 SAME_AS
≈ +108 edges.**

> **Correction to earlier reporting:** earlier phase reports described `fir-1001-26` as
> "out-of-corpus" or foreign. **That was wrong.** It is a legitimate corpus case; the
> issue was repeated re-ingestion of it, not insertion of foreign data.

**The external runtime trigger remains unproven.** No persistent process, scheduled task,
or DB-side job was identified.

---

## 7. Future-drift fix (`d5fa333`) — graph extraction idempotent by chunk

**What it does:** the chunk's existing `Document` node — keyed on the full
content-derived `doc_id` — now carries a **`projection_complete`** property. Graph
extraction is skipped only when that exact chunk is marked complete.

**Why the marker was necessary rather than reusing existing state:** the `Document` node
is written at the *start* of extraction, so its existence proves only that projection
*began*. An earlier attempt at this fix deliberately halted rather than gate on it, because
doing so would have made a half-projected document permanently unrecoverable.

Safety properties, each covered by a test:

- marker written **only after a clean extraction run** (`stats["errors"]` empty)
- **partial/failed runs stay retryable** — marker absent
- **marker-write failures stay retryable** — the failure is recorded, not swallowed
- **changed content remains eligible** (different chunk hash → different `doc_id`)
- `run_graph_extraction=False` behaviour unchanged (sync unaffected)
- **Person UUID semantics unchanged**
- **SAME_AS semantics unchanged**
- **historical Documents were not backfilled**

Verification: projection-specific tests passed; full suite **1,845 collected · 1,840
passed · 5 skipped · 0 failed**, with both the DB and Chroma guards active.

**Known limitation, stated plainly:** historical unmarked chunks are **not** automatically
treated as completed. No historical completion-marker backfill has been performed, so a
previously-projected chunk could replay once more before its marker is established.

---

## 8. SET B — live test-fixture contamination removed

### The discovery (corrects an earlier interpretation)

Earlier documentation recorded **"confirmed SAME_AS = 19"** and treated it as a corpus
review result. **That interpretation is now disproven.**

Investigation found test-fixture provenance in the live graph:

| Provenance | SAME_AS edges |
|---|---|
| `D1VERIFY-*` | **46** |
| `D1DEBUG-*` | **2** |
| **Total fixture** | **48** (19 confirmed / 5 rejected / 24 pending) |

**All 19 historical confirmed SAME_AS and all 5 historical rejected SAME_AS were
test-fixture remnants.** At that checkpoint the corpus had **0 genuine confirmed** and
**0 genuine rejected** SAME_AS relationships.

> The number "19 confirmed" *was* historically measured and is preserved here as a
> historical observation. The corrected interpretation is: **19 test-fixture
> confirmations, 0 genuine corpus confirmations.**

### The cleanup — COMPLETE

Operational graph cleanup; **no Git commit** (no repo files changed).

- **48 D1 fixture SAME_AS relationships deleted** by exact relationship id, in a
  transaction.
- **24 proven-orphan D1 Person nodes deleted** by exact `entity_id`, after re-verifying
  post-edge-deletion that each had **zero** remaining relationships.

**Critical safety result:** the 48 fixture edges had **25 distinct endpoints — 24 fixture
Persons and 1 genuine corpus Person.** The first attempt at this cleanup **halted itself**
at the LS-1 safety gate because the endpoint set was 25, not the assumed 24.

The genuine endpoint was explicitly protected and **survived unchanged**:

```
PERSON-0075e0c602 · فہد میمن · psrms/fir/fir-142-26#structured · case fir-142-26
```

**Post-cleanup live state:**

| Metric | Value |
|---|---|
| Postgres cases / graph Case nodes | 73 / 73 |
| Graph nodes / edges | **4,129 / 9,214** |
| SAME_AS | **1,634** — **0 confirmed · 0 rejected · 1,634 pending** |
| APPEARS_IN / BELONGS_TO_CASE | 3,017 / 2,999 (unchanged) |
| Chroma | 823 / 19 / 836 (unchanged) |

**No `D1VERIFY` / `D1DEBUG` SAME_AS remain.**

**Accurate statement:** the 19 confirmed and 5 rejected relationships were test-fixture
remnants. After SET B cleanup, the live corpus contains **zero genuine confirmed and zero
genuine rejected SAME_AS relationships.**

LS-1 eligibility was proven unaffected: exact target set **843 → 843**, identical checksum
`b5c5dee7cc51c0d5034dee85874c5b14eee3d2148ccfcb55bf9f7f863e86669f`.

---

## 9. SET A — historical replay duplication — BLOCKED

`fir-1001-26` currently contains heavy historical replay duplication.

Last measured: **577 Person nodes across only 8 distinct `canonical_name` values.**

Large duplicate families: `کاشف`, `محمد رمضان`, `بجے فیصل`, `فیصل`, `مدعی فیصل`,
`محمد رمضان ساکنہ محلہ`, `قبضے`, `تحت فیصل`.

**These are not all safe duplicates.** SET A cleanup is **BLOCKED** because no
deterministic survivor policy has been approved. For **7 of the 8** observed Person
families, the sanitized provenance contains the **only** representation — so broad
deletion of sanitized nodes could erase unique graph information.

Some entries (`قبضے` = "possession", `تحت فیصل` = "under Faisal") appear to be extraction
artefacts rather than people, which further complicates any survivor rule.

**No SET A deletion has occurred.**

---

## 10. Findings closed, deferred, or corrected without code changes

| Finding | Outcome | Why |
|---|---|---|
| **CS-3** | **Closed — no defect** | The prompt already treats vehicles/phones/organizations conditionally and forbids guessing; retrieval supports all five labels. Vehicle/Organization absence is corpus sparsity. |
| **CS-2** | **Reclassified + deferred** | The "hardcoded status enum" was **disproven** — the prompt lists *examples*, not an enum. The real issue is that `investigation_status` never reaches the agent: a **shared data-plumbing gap**. |
| **IA-I1** | **Deferred, narrowed** | `source_type` is missing from `ChunkMetadata`, but `text=str(row)` already puts it in the chunk text, **so the LLM and Verifier do see it**. The gap is structured metadata/citations only. |

---

## 11. New findings — recorded, not investigated

### NEW FINDING — historical/entity-resolution CNIC deduplication anomaly

**77 Person nodes** associated with `fir-1001-26` share CNIC **`00000-9000058-1`**.

This means the assumption that an exact CNIC necessarily resulted in `TIER_CNIC_AUTO`
reuse **did not hold** for those historical graph writes.

Status: **recorded · not investigated · not repaired.** No root cause is asserted here.

### NEW FINDING — test-fixture script hygiene

`scripts/verify_milestone_d.py` fixture cleanup is incomplete. Observed:

- fixture **nodes** were removed
- **D1-provenance SAME_AS relationships remained**
- at least one fixture SAME_AS linked to a **real corpus Person**

The script's cleanup is **node-identity scoped**, while the contamination is
**provenance-scoped**.

Status: **identified · not fixed.** The live cleanup (§8) and the script fix are
deliberately separable.

---

## 12. Remaining work

### Operational / data-state

| Item | Status |
|---|---|
| **SET A** survivor policy + historical duplicate cleanup | **BLOCKED** — needs a product decision |
| **LS-1 synchronization** (836 stored vs 843 eligible) | deferred until graph state is stable |
| **CNIC deduplication anomaly** | recorded, may be needed for SET A correctness |
| **Fixture-script hygiene** (`verify_milestone_d.py`) | identified, separate test-safety task |

### MEDIUM — code findings, 7 open

| Finding | What it is | Layer |
|---|---|---|
| **LS-2** | Degradation caveat misstates its cause ("no match found" vs. empty store) | Harness-only |
| **GS-2** | Leakage check inert on this route — consequence of GS-1, not independent | Harness-only |
| **IA-I1** | `source_type` absent from structured metadata/citations (deferred, §10) | Harness-only |
| **CS-2** | `investigation_status` never reaches Case Summarization (reclassified, §10) | **SHARED / live** |
| **CS-1** | `Incident` unreachable by graph traversal — not in `_SEED_LABELS`; **highest blast radius** | **SHARED / live** |
| **CCL-C4** | Unconfirmed SAME_AS links emitted uncapped — **context corrected, see below** | **SHARED / live** |
| **IA-I3** | `sql_param_extractor.txt` hardcodes old-corpus vocabulary | **SHARED / live** |

### CCL-C4 — context corrected

CCL-C4 remains **OPEN**, but its earlier framing is stale.

The old description assumed a pool containing **19 genuine confirmed links** and 20
`human_review` entries to preserve. **That is no longer accurate.** Post-SET-B state:

```
SAME_AS   = 1,634
confirmed =     0
rejected  =     0
pending   = 1,634
```

The corpus currently has **no genuine reviewed/confirmed SAME_AS examples** to preserve.
Separately, a very large portion of the pending pool has historically been associated with
`fir-1001-26` replay duplication.

**Therefore CCL-C4 should not be implemented until SET A / historical duplicate-state
decisions are resolved** — capping or thresholding a pool that is mostly replay artefacts
would tune against the wrong data.

### LOW — 4 open, all deliberately deferred

CCL-C5 (slow undirected query), CS-4 (bounded by three stacked guards), RD-6 (token cap
vs Urdu), LS-4 (partly-inapplicable label guard). None actionable.

### Parked by explicit decision

TB-3 (upstream semantic overloading), IA-I2 (data coverage gap, already disclosed),
IA-I4 / RD-1 (inherent verifier limits), `fir-97-26` victim edge (n=1), Data Quality
(zero actionable findings), CS-3 (closed, §10).

### Phase 4 — mandatory, pending

Test fixtures are still largely synthetic. **GS-1 is the proof this matters:** its
fixtures were 50% multi-case against a corpus that is 10.5% multi-case, and no test
asserted per-chunk attribution — exactly why the defect shipped green. The jurisdiction
and projection-idempotence tests are the first to use real corpus values and should be
the model. **`fir-1001-26` is a mandatory real-data fixture:** replaying the same
unchanged narrative twice must produce zero new graph state.

---

## 13. LS-1 status — NOT synchronized

Historical sequence: **822** → approved restoration to **836** → graph target later moved
to **843** because of legacy replay activity.

| | Value |
|---|---|
| Stored Chroma entity embeddings | **836** |
| Current graph eligible target | **843** |

SET B proved exact LS-1 target equality across the cleanup — **843 → 843**, checksum
`b5c5dee7cc51c0d5034dee85874c5b14eee3d2148ccfcb55bf9f7f863e86669f` — so **SET B did not
affect LS-1 eligibility.**

**Accurate status:** LS-1 remains operational and test-safe at the **836** stored
checkpoint, but is **not synchronized** with the current **843** eligible graph target.

Further synchronization should happen only after SET A / historical graph-state decisions,
so LS-1 is refreshed once against a stable graph rather than chasing a moving target.

---

## 14. Progress estimate

**Overall ~90% — an effort estimate, not a measured completion percentage.**

| Area | Done | Note |
|---|---|---|
| Structural / data repair (Phases 1–3A) | **100%** | Corpus split, re-projection, embeddings |
| Harness-only code fixes | **~90%** | 5 shipped; LS-2 and the narrowed IA-I1 remain |
| Shared-module fixes | **~50%** | Jurisdiction + ingestion idempotency shipped; CS-1, CCL-C4, IA-I3, CS-2 remain |
| Test-infrastructure safety | **100%** | Chroma + Postgres/AGE isolation, both mutation-verified |
| Legacy re-ingestion | **~70%** | Root cause proven, future drift fixed; historical cleanup (SET A) blocked, trigger unproven |
| Historical graph cleanup | **~40%** | SET B complete; SET A blocked |
| Phase 4 real-data fixtures | **~15%** | Jurisdiction + projection tests use real values; the rest do not |

The move from ~85% reflects the Postgres/AGE isolation fix, the proven legacy root cause,
the future-drift fix, and the SET B cleanup.

**This is not near-complete.** SET A, LS-1 synchronization, four shared findings, the
CCL-C4 reassessment, and Phase 4 all remain.

---

## 15. Current test and data state

| | Value |
|---|---|
| Full backend suite (after `d5fa333`) | **1,845 collected · 1,840 passed · 5 skipped · 0 failed** |
| Known exclusion | `tests/test_pdf_loader.py` — Docling/MSVC environment issue |
| Corpus | **73 relational cases · 73 graph Case nodes** |
| Graph nodes / edges | **4,129 / 9,214** |
| SAME_AS | **1,634** — 0 confirmed · 0 rejected · **1,634 pending** |
| APPEARS_IN / BELONGS_TO_CASE | **3,017 / 2,999** |
| Chroma | `muhafiz_kb` **823** · `muhafiz_community_reports` **19** · `muhafiz_entity_descriptions` **836** |
| **Stored Chroma entities** | **836** |
| **Current graph eligible target** | **843** |

All test counts read from JUnit XML, not console output.

---

## 16. Next steps, in order

1. **SET A survivor-policy / historical replay cleanup decision** (product decision).
2. **Investigate the CNIC deduplication anomaly** if needed for SET A correctness.
3. **Resolve historical `fir-1001-26` graph duplication safely.**
4. **Establish / synchronize completion state** as appropriate.
5. **Recompute the final LS-1 eligible target.**
6. **Refresh LS-1 once** against stable graph state.
7. **Reassess CCL-C4** using cleaned SAME_AS data.
8. **Remaining shared/harness findings:** CS-1, IA-I3, CS-2, LS-2, and the narrowed IA-I1
   if still worthwhile.
9. **Phase 4 real-data fixtures** before remediation closes.

Separately: **fixture-script hygiene** (`verify_milestone_d.py`) as a test-safety task.
