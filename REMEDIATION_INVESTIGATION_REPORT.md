# Muhafiz — Dataset-Drift Remediation: Investigation & Verification Report

**Date:** 2026-08-25
**Branch:** `agent-harness` @ `c96dead`
**Status:** INVESTIGATION + VERIFICATION CHECKPOINT ONLY — nothing purged, no code/prompts/tests/schema modified, no data mutated.

**Awaiting two decisions:**
1. Approve/reject the 231-chunk ChromaDB purge (Part A).
2. Review the Phase 3 scope and ordering (Part B §8–9).

---

## Table of Contents

- [1. Current repository state](#1-current-repository-state)
- [PART A — Phase 2 purge verification](#part-a--phase-2-purge-verification)
  - [2.1 Current ChromaDB inventory](#21-current-chromadb-inventory)
  - [2.2 Target and preservation counts](#22-target-and-preservation-counts)
  - [2.3 Three independent discriminators](#23-three-independent-discriminators-agree-exactly)
  - [2.4 Deletion target list](#24-deletion-target-list)
  - [2.5 Preservation set](#25-preservation-set--all-33-real-)
  - [2.6 Proof of exclusion / disjointness](#26-proof-of-exclusion--disjointness)
  - [2.7 Orphan verification](#27-orphan-verification-new-check-not-in-the-earlier-analysis)
  - [2.8 Discrepancies](#28-discrepancies-from-the-earlier-23133-classification)
  - [2.9 Exact purge code path](#29-exact-purge-code-path--not-executed)
  - [2.10 Recommendation](#210-recommendation)
- [PART B — Four unaudited sub-agents](#part-b--investigation-report-four-unaudited-sub-agents)
  - [Corrections to figures](#corrections-to-figures-used-in-the-brief)
  - [3. Timeline Building audit](#3-timeline-building-audit)
  - [4. Investigative Analysis audit](#4-investigative-analysis-audit)
  - [5. Report Drafting audit](#5-report-drafting-audit)
  - [6. Cross-Case Linkage audit](#6-cross-case-linkage-audit)
  - [7. Reassessment of existing Phase 3 findings](#7-reassessment-of-existing-phase-3-findings)
  - [8. Consolidated remediation scope](#8-consolidated-remediation-scope)
  - [9. Recommended Phase 3 ordering](#9-recommended-phase-3-ordering)
  - [10. Test/fixture implications](#10-testfixture-implications-phase-4--mandatory)
- [Appendix A — Prior phase context](#appendix-a--prior-phase-context)
- [Appendix B — Verification commands used](#appendix-b--verification-commands-used)

---

## 1. Current repository state

| | |
|---|---|
| Branch | `agent-harness` |
| HEAD | `c96dead` (Merge main into agent-harness) |
| vs `origin/main` | **0 behind** — fully current |
| Ahead of `main` | 2 commits (Phase 1 XAGG fix `1a81bff` + merge) |
| Uncommitted | `.gitignore`, `README.md`, `PROJECT_OVERVIEW.md` (untracked), `frontend/` markdown-rendering fix |

Nothing uncommitted touches the purge path. The Postgres dump restore since the last
checkpoint did **not** alter ChromaDB (separate store), so the earlier target analysis
remains applicable — but it was re-derived from scratch rather than assumed.

---

# PART A — PHASE 2 PURGE VERIFICATION

*(read-only, nothing modified)*

## 2.1 Current ChromaDB inventory

**`muhafiz_kb`: 1054 chunks**

| doc_type | Chunks | Origin |
|---|---|---|
| `fir_narrative` | 704 | new corpus |
| `pdf` | **264** | old corpus |
| `roznamcha_entry` | 74 | new corpus |
| `pkm_application` | 8 | new corpus |
| `cms_complaint` | 4 | new corpus |

- `is_global`: True=264 (old), False=790 (new) — a clean structural split.
- `case_id` shape: `fir-*`=712, `CASE-*`=208, none=134.
- Second collection `muhafiz_community_reports`: **0 documents** (see §2.8 discrepancy note).

## 2.2 Target and preservation counts

- **Deletion target: 231**
- **Preservation (`REAL-*`): 33**
- **Untouched (new corpus): 790**
- 231 + 33 + 790 = **1054** ✅

## 2.3 Three independent discriminators agree exactly

A single metadata field was deliberately not trusted:

| Filter | Count |
|---|---|
| A: `doc_type=='pdf'` AND not `REAL-*` | 231 |
| B: `is_global is True` AND not `REAL-*` | 231 |
| C: not-new-doc_type AND not `REAL-*` | 231 |

**A == B == C (identical sets, not merely equal counts).** `A∩B∩C` = 231, `A∪B∪C` = 231.

## 2.4 Deletion target list

Full itemized list (chunk ID, source, case_id, classification) written to
`scratchpad/phase2_targets_full.txt`. Summary by classification:

| Classification | Chunks | Files |
|---|---|---|
| Old synthetic case evidence (`FIR-2026-*`, `CHARGESHEET-*`, `CASEDIARY-*`, `DARKHAST-*`, `RECOVERY-*`, `WITNESS-*`) | **208** | 68 |
| Synthetic MP/TAR reports (`MP-2026-*`, `TAR-2026-*`) | **23** | 7 |
| **UNEXPECTED** | **0** | — |
| **Total** | **231** | **84** |

Sample rows:

```
CASEDIARY-FIR-2026-BUR-009-01_pdf_fdd4db53_c0   CASEDIARY-FIR-2026-BUR-009-01.pdf   CASE-009            old synthetic case evidence
CASEDIARY-FIR-2026-CYBER-001-01_pdf_5f306d21_c0 CASEDIARY-FIR-2026-CYBER-001-01.pdf CASE-B0-CYBER-001   old synthetic case evidence
CASEDIARY-FIR-2026-BUR-007-01_pdf_28e349b4_c0   CASEDIARY-FIR-2026-BUR-007-01.pdf   CASE-007            old synthetic case evidence
CASEDIARY-FIR-2026-BUR-008-01_pdf_decab992_c0   CASEDIARY-FIR-2026-BUR-008-01.pdf   CASE-008            old synthetic case evidence
CASEDIARY-FIR-2026-HAR-001-01_pdf_4bf25203_c0   CASEDIARY-FIR-2026-HAR-001-01.pdf   CASE-B0-HAR-001     old synthetic case evidence
CASEDIARY-FIR-2026-RTA-001-01_pdf_c015b4a9_c0   CASEDIARY-FIR-2026-RTA-001-01.pdf   CASE-B0-RTA-001     old synthetic case evidence
```

### Target files by chunk count (84 files, 231 chunks)

```
  1  CASEDIARY-FIR-2026-BUR-007-01.pdf        1  CASEDIARY-FIR-2026-BUR-008-01.pdf
  1  CASEDIARY-FIR-2026-BUR-009-01.pdf        5  CASEDIARY-FIR-2026-CYBER-001-01.pdf
  1  CASEDIARY-FIR-2026-CYBER-005-01.pdf      1  CASEDIARY-FIR-2026-CYBER-006-01.pdf
  1  CASEDIARY-FIR-2026-FRAUD-016-01.pdf      4  CASEDIARY-FIR-2026-HAR-001-01.pdf
  3  CASEDIARY-FIR-2026-RTA-001-01.pdf        6  CASEDIARY-FIR-2026-THEFT-001-01.pdf
  1  CASEDIARY-FIR-2026-THEFT-011-01.pdf      1  CASEDIARY-FIR-2026-THEFT-012-01.pdf
  7  CHARGESHEET-FIR-2026-CYBER-001.pdf       1  CHARGESHEET-FIR-2026-FRAUD-016.pdf
  7  CHARGESHEET-FIR-2026-HAR-001.pdf         7  CHARGESHEET-FIR-2026-THEFT-001.pdf
  1  CHARGESHEET-FIR-2026-THEFT-012.pdf       1  DARKHAST-FIR-2026-BUR-009.pdf
  4  DARKHAST-FIR-2026-CYBER-001.pdf          1  DARKHAST-FIR-2026-CYBER-006.pdf
  4  DARKHAST-FIR-2026-HAR-001.pdf            3  DARKHAST-FIR-2026-RTA-001.pdf
  4  DARKHAST-FIR-2026-THEFT-001.pdf          2  FIR-2026-ARMS-002.pdf
  8  FIR-2026-BUR-001.pdf                     8  FIR-2026-BUR-002.pdf
  2  FIR-2026-BUR-007.pdf                     1  FIR-2026-BUR-008.pdf
  8  FIR-2026-CYBER-001.pdf                   1  FIR-2026-CYBER-005.pdf
  7  FIR-2026-DOM-001.pdf                     7  FIR-2026-DOM-002.pdf
  1  FIR-2026-DOM-014.pdf                     7  FIR-2026-FRAUD-001.pdf
  7  FIR-2026-FRAUD-002.pdf                   1  FIR-2026-FRAUD-015.pdf
  1  FIR-2026-FRAUD-016.pdf                   6  FIR-2026-HAR-001.pdf
  8  FIR-2026-HAR-002.pdf                     1  FIR-2026-HAR-018.pdf
  6  FIR-2026-RTA-001.pdf                     7  FIR-2026-RTA-002.pdf
  1  FIR-2026-RTA-019.pdf                     9  FIR-2026-THEFT-001.pdf
  7  FIR-2026-THEFT-002.pdf                   1  FIR-2026-THEFT-011.pdf
  3  MP-2026-001.pdf                          3  MP-2026-002.pdf
  4  MP-2026-003.pdf                          1  MP-2026-020.pdf
  1  RECOVERY-FIR-2026-ARMS-003.pdf           1  RECOVERY-FIR-2026-BUR-007.pdf
  1  RECOVERY-FIR-2026-BUR-008.pdf            1  RECOVERY-FIR-2026-BUR-009.pdf
  1  RECOVERY-FIR-2026-THEFT-012.pdf          4  TAR-2026-001.pdf
  4  TAR-2026-002.pdf                         4  TAR-2026-003.pdf
  1  WITNESS-FIR-2026-ARMS-003-02.pdf         1  WITNESS-FIR-2026-BUR-007-01.pdf
  1  WITNESS-FIR-2026-BUR-007-02.pdf          1  WITNESS-FIR-2026-BUR-008-01.pdf
  1  WITNESS-FIR-2026-BUR-008-02.pdf          1  WITNESS-FIR-2026-BUR-009-01.pdf
  1  WITNESS-FIR-2026-BUR-009-02.pdf          2  WITNESS-FIR-2026-BUR-009-03.pdf
  2  WITNESS-FIR-2026-CYBER-004-01.pdf        1  WITNESS-FIR-2026-CYBER-005-01.pdf
  2  WITNESS-FIR-2026-CYBER-006-01.pdf        1  WITNESS-FIR-2026-CYBER-006-03.pdf
  1  WITNESS-FIR-2026-DOM-013-01.pdf          1  WITNESS-FIR-2026-DOM-014-01.pdf
  1  WITNESS-FIR-2026-FRAUD-015-01.pdf        1  WITNESS-FIR-2026-FRAUD-015-02.pdf
  1  WITNESS-FIR-2026-FRAUD-016-01.pdf        1  WITNESS-FIR-2026-FRAUD-016-02.pdf
  2  WITNESS-FIR-2026-FRAUD-016-03.pdf        1  WITNESS-FIR-2026-HAR-018-01.pdf
  1  WITNESS-FIR-2026-RTA-019-01.pdf          1  WITNESS-FIR-2026-THEFT-010-01.pdf
  1  WITNESS-FIR-2026-THEFT-011-01.pdf        1  WITNESS-FIR-2026-THEFT-011-02.pdf
  1  WITNESS-FIR-2026-THEFT-012-01.pdf        1  WITNESS-FIR-2026-THEFT-012-02.pdf
  (+ remaining WITNESS-*/RECOVERY-* single-chunk files)
```

## 2.5 Preservation set — all 33 `REAL-*`

| File | Chunks |
|---|---|
| REAL-001-essential-safety-tips-for-citizens.pdf | 9 |
| REAL-002-general-police-verification-procedure.pdf | 3 |
| REAL-003-facilitation-on-wheels.pdf | 5 |
| REAL-004-copy-of-fir-procedure.pdf | 2 |
| REAL-005-lost-report-procedure.pdf | 3 |
| REAL-006-foreigner-registration-procedure.pdf | 2 |
| REAL-007-character-certificate-procedure.pdf | 5 |
| REAL-008-tenant-registration-procedure.pdf | 2 |
| REAL-009-servant-registration-procedure.pdf | 2 |
| **Total** | **33** |

### Full preservation chunk IDs

```
REAL-001-essential-safety-tips-for-citizens_pdf_6b2be766_c0
REAL-001-essential-safety-tips-for-citizens_pdf_6b2be766_c1
REAL-001-essential-safety-tips-for-citizens_pdf_6b2be766_c2
REAL-001-essential-safety-tips-for-citizens_pdf_c3f5bfce_c0
REAL-001-essential-safety-tips-for-citizens_pdf_c3f5bfce_c1
REAL-001-essential-safety-tips-for-citizens_pdf_c3f5bfce_c2
REAL-001-essential-safety-tips-for-citizens_pdf_c3f5bfce_c3
REAL-001-essential-safety-tips-for-citizens_pdf_c3f5bfce_c4
REAL-001-essential-safety-tips-for-citizens_pdf_c3f5bfce_c5
REAL-002-general-police-verification-procedure_pdf_c9c4326c_c0
REAL-002-general-police-verification-procedure_pdf_c9c4326c_c1
REAL-002-general-police-verification-procedure_pdf_c9c4326c_c2
REAL-003-facilitation-on-wheels_pdf_04c98697_c0
REAL-003-facilitation-on-wheels_pdf_04c98697_c1
REAL-003-facilitation-on-wheels_pdf_04c98697_c2
REAL-003-facilitation-on-wheels_pdf_04c98697_c3
REAL-003-facilitation-on-wheels_pdf_04c98697_c4
REAL-004-copy-of-fir-procedure_pdf_cc005692_c0
REAL-004-copy-of-fir-procedure_pdf_cc005692_c1
REAL-005-lost-report-procedure_pdf_99496781_c0
REAL-005-lost-report-procedure_pdf_99496781_c1
REAL-005-lost-report-procedure_pdf_99496781_c2
REAL-006-foreigner-registration-procedure_pdf_4ded382b_c0
REAL-006-foreigner-registration-procedure_pdf_4ded382b_c1
REAL-007-character-certificate-procedure_pdf_910ab3f2_c0
REAL-007-character-certificate-procedure_pdf_e89c2e96_c0
REAL-007-character-certificate-procedure_pdf_e89c2e96_c1
REAL-007-character-certificate-procedure_pdf_e89c2e96_c2
REAL-007-character-certificate-procedure_pdf_e89c2e96_c3
REAL-008-tenant-registration-procedure_pdf_643565dd_c0
REAL-008-tenant-registration-procedure_pdf_643565dd_c1
REAL-009-servant-registration-procedure_pdf_536b6de2_c0
REAL-009-servant-registration-procedure_pdf_536b6de2_c1
```

## 2.6 Proof of exclusion / disjointness

```
targets ∩ REAL-*    = 0     ✅
targets ∩ new data  = 0     ✅
targets ∪ REAL-* ∪ new = ALL 1054   ✅
REAL-* doc_type distribution = {'pdf': 33}   ← all 33 sit inside filter A's
                                                candidate pool and are removed
                                                ONLY by the REAL- exclusion
targets carrying a fir-* case_id = 0   ✅
```

The last check matters most: the `REAL-*` chunks are `doc_type='pdf'`, i.e. they *would*
be caught by the broad filter — the exclusion is doing real work, not passively
coinciding.

## 2.7 Orphan verification (new check, not in the earlier analysis)

| Check | Result |
|---|---|
| Distinct `case_id`s among targets | 30 |
| …of those present in Postgres `cases` | **0** |
| Distinct `doc_id`s among targets | 84 |
| …of those present in Postgres `documents` | **0** |

**All 231 targets are fully orphaned** — they reference a corpus that no longer exists in
either Postgres table. Nothing in the live system can cite them.

## 2.8 Discrepancies from the earlier 231/33 classification

**None in the purge target itself** — 208 + 23 = 231, 33 preserved, exactly as established.

Two observations that do **not** affect the purge but should be recorded:

1. **Duplicate `REAL-*` ingests.** REAL-001 exists under two content hashes
   (`_6b2be766_` 3 chunks + `_c3f5bfce_` 6 chunks) and REAL-007 likewise
   (`_910ab3f2_` 1 + `_e89c2e96_` 4). The same source was ingested twice with different
   chunkings. All 33 are preserved either way, so this is not a purge risk — but it
   means the reference corpus contains near-duplicate chunks that could both surface in
   retrieval. Out of scope here; worth its own decision later.

2. **`muhafiz_community_reports` Chroma collection is EMPTY (0 documents)** despite
   Postgres now holding 19 `community_reports` rows from `RUN-20260825074016`. This
   contradicts the assumption that the dump restore fixed XNETWORK.
   `query_similar_communities()` (`src/retrieval/community_vector_store.py:110-120`)
   reads the **Chroma** collection and returns `[]` when `count()==0` — so **XNETWORK
   still has zero evidence**. Feeds directly into Part B's Cross-Case Linkage audit.

## 2.9 Exact purge code path — NOT EXECUTED

```python
# WOULD delete 231 chunks. NOT RUN.
import chromadb
coll = chromadb.PersistentClient(path='data/chroma_db').get_collection('muhafiz_kb')
r = coll.get(include=['metadatas'])
target_ids = [
    i for i, x in zip(r['ids'], r['metadatas'])
    if x.get('doc_type') == 'pdf'
    and not (x.get('source') or '').startswith('REAL-')
]
assert len(target_ids) == 231          # guard
coll.delete(ids=target_ids)            # ← the only destructive call
```

Recommended hardening if approved: delete by the **frozen ID list** already written to
`phase2_targets_full.txt` rather than re-running the filter at execution time, so the
deletion set is exactly what was reviewed.

Expected result: **1054 → 823** (790 new + 33 `REAL-*`).

## 2.10 Recommendation

### ✅ SAFE TO APPROVE

Reasoning:

- Three independent discriminators produce byte-identical target sets — not a
  single-field artifact
- Sets are provably disjoint and partition the collection exactly
- All 33 `REAL-*` chunks are inside the broad filter's pool and excluded by an explicit
  rule that is demonstrably load-bearing
- Zero unexpected records among 231 targets across 84 files
- **All targets are fully orphaned** from both Postgres `cases` and `documents` —
  nothing live references them
- No new-corpus chunk can be selected (`targets ∩ new = 0`, and no target carries a
  `fir-*` case_id)

Residual risk is low but non-zero: ChromaDB deletion is not transactional and there is no
snapshot. If a rollback point is wanted, the 231 chunks' text+metadata could be exported
to a file first — that is an addition to the plan, not a blocker, and requires explicit
approval.

**Nothing has been deleted. Awaiting explicit approval.**

---

# PART B — INVESTIGATION REPORT: FOUR UNAUDITED SUB-AGENTS

**Nothing was modified.** All figures below are independently verified against the live
database, not taken from agent reports at face value.

## Corrections to figures used in the brief

| Figure | Brief/earlier said | **Verified actual** |
|---|---|---|
| SAME_AS edges | 340 | **1211** (pending 1187 / confirmed 19 / rejected 5) |
| `"entry None"` OCCURRED_ON edges | 74 | **188** |
| Chroma `muhafiz_community_reports` | assumed fixed by restore | **0 embeddings** |
| `community_reports` spanning >1 case | — | **2 of 19** |
| `conflicts_checked_at` | may now be set | **still 0 / 73** |
| `CONFLICTS_WITH` / `CROSS_VERSION_OF` | — | **0 / 0** |
| `Incident.description` populated | — | **0 of 73** |

### Verified current data model facts

- 73 cases; `case_id` like `fir-117-26`; `fir_number` like `117/26`
- `cases.crime_category` = **statute lists** (`PPC`, `PPC, Arms Ordinance 1965`,
  `CNSA 1997`, `PECA 2016, PPC`) — 7 distinct values, NOT crime types
- `cases.investigation_status` = free-text Urdu narrative, **EMPTY STRING in 52/73**
- `cases.police_station` = Urdu free text, city embedded, nationwide
  (Karachi/Lahore/Faisalabad/Hyderabad)
- Graph nodes: Officer 832, StructuredRecord 713, Person 478, Date 137, Document 125,
  Case 73, Incident 73, Address 71, Weapon 32, PoliceStation 19, District 9,
  Vehicle 2, PhoneNumber 1, **Organization 0**
- Graph edges: BELONGS_TO_CASE 1527, APPEARS_IN 1512, OCCURRED_ON 568,
  ASSOCIATED_WITH 252 (was 0), INVOLVED_IN 221, PART_OF 146, ASSIGNED_TO 144,
  LOCATED_AT 101, FILED_AT 73, OWNS 30, RELATED_TO 24, CITES 9, REGISTERED_TO 5,
  SAME_AS 1211, **CONFLICTS_WITH 0**, **CROSS_VERSION_OF 0**
- SAME_AS tiers: `flagged_unverified` 1178, `human_review` 33; `basis` and `confidence`
  non-null on all 1211
- Per-case sample (`fir-117-26`): Person 4, StructuredRecord 10, Officer 1, Document 1,
  Incident 1, Weapon 1 — **zero Vehicle/PhoneNumber/Organization**
- OCCURRED_ON `event_type` distribution: `zimni_entry` 259, `position` 94, `incident` 64,
  `chalaan_dispatch` 15
- `police_reference_data`: 21 rows, all `source_type='synthetic'`, all
  `category='penal_code'`

---

## 3. Timeline Building audit

**Files:** `agents/timeline_building.py`; `graph/case_scope.py` (`scoped_cypher`);
`data_gateway.get_case()`

### Findings

| # | Finding | Evidence | Severity |
|---|---|---|---|
| T1 | `Incident.description` is NULL on **all 73** nodes → every event description falls to placeholder `"Incident {id} (no description recorded)"` | `:221, :386`; verified `73 total, 0 with_desc` | correctness/precision |
| T2 | **188** OCCURRED_ON edges carry literal `detail = "entry None"` — an f-string leaked a Python `None` at projection time, rendered verbatim to investigators | `:223, :394`; verified count 188 | correctness/precision |
| T3 | `occ.locked` NULL everywhere → `bool(None)` reports `False` (a definite "not locked") rather than unknown | `:397` | misleading-output (low) |
| T4 | `answer_text` says detection *"could not be completed"* when it was in fact **never run** — reads as a transient error an investigator might retry forever | `:446-451` | misleading-output (low) |
| T5 | **Conflict gating works correctly** — `conflicts_checked_at` NULL 73/73 + `CONFLICTS_WITH`=0 → all events UNKNOWN, caveat fires, status forced PARTIAL, never OK | `:406, :548-551` | **no-issue — this is the anti-XAGG pattern** |
| T6 | ASSOCIATED_WITH-vs-OCCURRED_ON docstring "deviation" | `:18-52` | **no-issue** — ASSOCIATED_WITH is Person↔Person co-mention carrying no dates; 0→252 changes nothing. Reasoning was correct |
| T7 | One Incident carries several typed OCCURRED_ON edges; `id(occ)` dedup key is correct (old `entity_id` dedup would have collapsed 432 events to 73) | `:196-214, :417` | no-issue |

### Output semantics

Fully deterministic — **no LLM, no prompt**. No Verifier runs (documented XAGG-precedent
exemption at `:108-119`), which is defensible for computed strings *but* means T1/T2 have
**zero downstream check**.

Because `conflicts_checked_at` is NULL everywhere and `CONFLICTS_WITH` is 0, **every real
case takes the `detection_confirmed=False` branch** (`:409-411`): every event gets
`ConflictState.UNKNOWN`, the caveat at `:548-551` fires, and status is forced to
`PARTIAL` (`:553`) — never `OK`. The conflict-checked branch (`:453-458`) is unreachable
on live data. **This is the correct, fail-closed outcome and the direct opposite of the
XAGG failure mode.**

**Crucially, this agent never touches `crime_category`, `investigation_status`,
`police_station`, or `case_id` format** — it is graph-only, so the four biggest real-data
traps never reach it. That is why it survived the migration far better than XAGG.

### Shared modules

- `src.graph.case_scope.scoped_cypher` — **harness-only for this path** (orchestrator
  does not import it). A fix to the two Cypher templates has **zero live-traffic impact**.
- `src.data_gateway.get_case()` (`:321-324`) — **shared**, but read-only single-row
  fetch; no fix pressure.
- `orchestrator.py` imports **no harness module at all** (verified by grep). The entire
  harness is dark to live traffic today.

### Prompts

**None.** This agent makes zero LLM calls — no prompt file, no inline template. It is the
only fully deterministic sub-agent audited. A genuine strength.

### Tests (`tests/test_harness_agent_timeline_building.py`)

Fixtures use synthetic shapes throughout: `active_case_id="CASE-001"` (`:45`),
`entity_id="INCIDENT-002"`, `description="Second incident"` (`:122-126`). Real ids are
`fir-117-26` and real descriptions are **NULL**.

`_stub_scoped_cypher` (`:75-95`) returns hand-built dicts, so **no test ever exercises a
NULL description, a NULL `locked`, or an `"entry None"` detail.**
`test_successful_timeline_mixed_conflict_states` (`:121`) asserts `status == OK` with
`_FakeGateway(checked=True)` — **a state that exists in no real case**.

Credit where due: `test_unconfirmed_detection_yields_unknown_not_none` (`:169`) and
`test_one_incident_multiple_occurred_on_edges_become_multiple_events` (`:196`) do encode
real post-migration semantics correctly.

**Minimum realistic fixture:** one
`_dated_row("fir-117-26", None, "2026-03-14", locked=None, occ_id=7, event_type="zimni_entry", detail="entry None")`
with `_FakeGateway(checked=False)`, asserting the description is not a bare placeholder
and does not contain `"entry None"`. That single row surfaces three of the four findings.

---

## 4. Investigative Analysis audit

**Files:** `agents/investigative_analysis.py`; `tools/{rag,graph,sql}.py`;
`prompts/sql_param_extractor.txt`

### Findings

| # | Finding | Evidence | Severity |
|---|---|---|---|
| I1 | **Synthetic reference rows presented as authoritative law.** `_to_evidence_chunk` sets `source_file="police_reference_data row N"` and **drops `source_type`**; all 21 rows are `source_type='synthetic'`. Model told at `:226-227` it's "penal-code reference data", cites as `[Document N]`. Neither Verifier nor user can tell | `tools/sql.py:73-78` | **misleading-output (medium)** |
| I2 | `police_reference_data` has **no rows for CNSA 1997 or Arms Ordinance 1965** — the new corpus's actual statutes. SQL slot silently contributes nothing for narcotics/arms FIRs; `sql_tool` returns EMPTY → `fallback_to_rag=True` | `tools/sql.py:99-105`; 21 rows verified | unsupported-data (**disclosed** via `degraded_from`) |
| I3 | `prompts/sql_param_extractor.txt` hardcodes old-corpus subject vocabulary in schema + all 3 worked examples (`Mobile/Vehicle Theft`, `Cyber Fraud/Online Scam`, `Harassment/Cyber Harassment`, `overspeeding`); instructs defaulting `category` to `'penal_code'` | prompt file | unsupported-data |
| I4 | 264 stale `pdf` chunks retrievable and citable as current evidence | corpus-level | correctness/precision — **resolved by the pending Phase 2 purge** |
| I5 | Verifier/Validation **cannot** catch synthetic-or-stale-but-internally-consistent citations — the chunk genuinely says it | `:466-470, :542-546` | misleading-output (medium) |
| I6 | ABSTAINED thresholds, `degraded_from` caveats, verifier-rejection reset | `:497-504, :520-537, :549-556` | **no-issue — honest degradation** |
| I7 | GRAPH leg genuinely **improved** — ASSOCIATED_WITH 0→252 means one-hop expansion actually traverses now | `graph_retriever.py:597` | no-issue |
| I8 | `target_entity` threading correct (Unit-8 fix), mirrors `orchestrator.py:858` | `:359` | no-issue |

### Output semantics

Claims: one synthesized analytical answer with citations across three sources. Actually
gets: whichever of RAG/GRAPH/SQL returned `status == OK` (`:490, :510-512`).

Silent-zero behavior is **well handled**. The `not tools_used` → `ABSTAINED` path
(`:497-504`) does not degrade to a confident empty answer. `degraded_from` is always
disclosed as a caveat (`:549-556`).

**Would the Verifier catch a wrong answer?** Partially. `verify_grounding()` checks
claim-by-claim traceability and case-id leakage. It **would** catch a fabricated fact. It
**would not** catch: (a) a synthetic reference row cited as real law — internally
consistent, the chunk genuinely says it; (b) a stale old-corpus `pdf` chunk cited as
current case evidence — same reason. This is the classic "internally consistent but
semantically wrong" gap. `validate_answer(tier="full")` (`:542-546`) has the same blind
spot: it is an entailment check against the same chunks.

### Prompts

- **Inline `_SYSTEM_PROMPT_TEMPLATE` (`:224-236`)** — clean. Names three source kinds
  neutrally, mandates `[Document N]` citation, and explicitly instructs *"If the material
  does not contain enough information to answer, say so plainly rather than guessing"*.
  Does **not** assert any status taxonomy. **no-issue.** One gap: does not tell the model
  reference rows may be synthetic.
- **`prompts/sql_param_extractor.txt`** — the drifted one (see I3).
- **`prompts/verifier.txt`** — rule 4 ("Never penalize an answer for correctly stating
  that evidence is absent") and rule 3 (hedging for `graph_confidence < 0.85`) are both
  well-suited to sparse real data. No drift.

### Shared modules — high blast radius

| Module | Status | Impact |
|---|---|---|
| `src.retrieval.graph_retriever.retrieve_graph` | **SHARED** (`orchestrator.py:32`) | Fixes hit live GRAPH/GRAPH_HYBRID routes |
| `src.pipeline.verifier.verify_grounding` | **SHARED** (`orchestrator.py:25`) | Affects every live route |
| `src.pipeline.sql_extractor.extract_sql_params` + prompt | **SHARED** (`orchestrator.py:22`) | Changes live SQL route |
| `src.pipeline.validation`, `harness/tools/*`, `harness/supervisor.py` | **HARNESS-ONLY** | Free to fix |
| `src.llm.client.call_llm` | shared infrastructure | No semantics |

**Prefer fixing at the tool-wrapper layer (`tools/sql.py`, harness-only) over
`graph_retriever.py`/`sql_extractor.py`.**

### Tests (`tests/test_harness_agent_investigative_analysis.py`)

Synthetic throughout: `case_id="CASE-001"` (`:58, :64, :72`),
`_rag_chunk(text="the suspect fled the scene", source="doc.pdf")` (`:58-61`),
`_graph_chunk(text="Person P-1 is linked to Vehicle V-1")` (`:64`). That graph fixture is
**doubly wrong** — it asserts a Person→Vehicle link when `Vehicle`=2 nodes total, and
uses `P-1`/`V-1` id shapes that no longer exist.

All 15 tests stub the three tools and the verifier (`:124-130`), so **every test passes
regardless of any data drift**.

**Minimum realistic fixture:** a `_sql_chunk` carrying `source_type="synthetic"` in
metadata plus an assertion that the resulting caveats or citations disclose it; and
`case_id="fir-117-26"` with `source="fir-117-26_narrative.txt"` to catch id-shape
coupling.

---

## 5. Report Drafting audit

**Files:** `agents/report_drafting.py`; `pipeline/file_structurer.py`;
`generation/{pdf,xlsx,docx}_builder.py`; `prompts/file_structurer.txt`

### Findings

| # | Finding | Evidence | Severity |
|---|---|---|---|
| R1 | **Single-synthetic-chunk regime makes the Verifier a paraphrase check, not a fact check.** All summary text wrapped as one `[Document 1]`; `check_citation_consistency(..., valid_citation_count=1)` can only reject `[Document N≠1]`. Any upstream factual error is by construction perfectly grounded and passes both gates | `:517, :533-542`; documented honestly at `:58-96` | correctness/precision |
| R2 | Inherits Case Summarization's degradation wholesale into a **formal, downloadable PDF/DOCX** carrying the same apparent authority | `:475, :618-639` | misleading-output |
| R3 | Does **NOT** depend on `crime_category`/`investigation_status` — verified by grep across drafting, summarization, `file_structurer.py`, and `prompts/file_structurer.txt`. Drafting prompt (`:232-244`) is taxonomy-free | — | **no-issue** |
| R4 | `session_id` validated upfront before any LLM/tool work, matching the prior fix's stated intent | `:457-470` | **no-issue — prior fix confirmed correct** |
| R5 | `file_structurer`/builders schema-generic (title/description/sections/table); `_normalize_payload` (`:12-56`) defensively coerces types and pads ragged rows | `file_structurer.py:59-80` | **no-issue** |
| R6 | 3000-token structuring cap vs. Urdu narratives (which tokenize poorly) may truncate locally and fall through to cloud | `:76-80` | availability/robustness |

Disclosure machinery (`_disclosure_line_for` `:291-309`, `_inject_disclosure_into_payload`
`:312-350`) is genuinely well-built and does fire on PARTIAL, including the xlsx
table-row path at `:343-350`. `_persist_generated_file` (`:369-413`) retains its fallback
for missing `user_id`, which is the correct residual posture.

GRAPH-side degradation from Cross-Case Linkage findings does **not** reach here — Case
Summarization uses RAG+GRAPH, not XGRAPH — so the inheritance is bounded.

### Shared modules

- `src/pipeline/file_structurer` + `src/generation/{pdf,xlsx,docx}_builder` —
  **SHARED** (`orchestrator.py:42-45`, via `_generate_file()`). Any builder change hits
  live file downloads.

### Tests (`tests/test_harness_agent_report_drafting.py`, 549 lines)

Synthetic `CASE-001` (`:53`); `case_summarization`, `call_llm`, `structure_for_file`, and
all builders stubbed. Disclosure/xlsx/session_id coverage is genuinely good, but **no test
asserts anything about field content**.

**Minimum realistic fixtures for this agent:**
- A `case_summarization` stub returning `status=PARTIAL` with `degraded_from=["GRAPH"]`
  and a factually wrong claim; assert the generated file carries the disclosure line AND
  that the test acknowledges the Verifier cannot catch the upstream error (documents R1
  as intended behavior rather than silently relying on it).
- `active_case_id="fir-117-26"` to catch any id-shape coupling in filename/metadata
  construction.
- A long Urdu narrative payload exceeding the 3000-token structuring cap; assert
  truncation is disclosed rather than silent (R6).

---

## 6. Cross-Case Linkage audit

**Files:** `agents/cross_case_linkage.py`; `tools/{xgraph,xnetwork}.py`;
`pipeline/xnetwork.py`; `retrieval/community_vector_store.py`;
`retrieval/graph_retriever.py`

### Findings

| # | Finding | Evidence | Severity |
|---|---|---|---|
| C1 | **XNETWORK has zero evidence.** Postgres has 19 `community_reports`, but `query_similar_communities()` reads **only Chroma**, which has **0 embeddings**. `xnetwork_tool` always returns EMPTY → `xnetwork_contributes` always False (`:618`) → permanently in `degraded_from` (`:694`). Every XNETWORK path — `_generate_xnetwork_text` (`:350-434`), the cloud-retry, `_xnetwork_links` (`:505`), citations (`:685`) — is **dead code in production**. The "MANDATORY full semantic tier" validation gate documented at `:179-190` **provides no protection today** (always SKIPPED, `:716` passes `[]`) | verified 0 embeddings; `xnetwork.py:99`; `community_summarization.py:222-223, 316-317` | **correctness/precision** |
| C2 | **XGRAPH description misattributes case span.** With `target_entity=None` (the dominant real path via `_find_recurring_entities_for_query()`), renders *"A recurring entity appears across N case(s)"* using `case_ids_touched` — the **aggregate footprint of all seeds**, not one entity's recurrence. Same defect in `_xgraph_summary_line` (`:524-539`). Deterministic text the Verifier **deliberately never checks** (`:648-650`) | `:437-460` — read and confirmed directly | **correctness/precision — highest consequence** |
| C3 | Even if embeddings were rebuilt, **17 of 19 clusters are single-case** → a "cross-case pattern" would be a single-case cluster presented as cross-case. `_xnetwork_links` (`:512-521`) stamps `is_unconfirmed=False`. Prompt (`:276-286`) asserts a multi-case premise the data does not satisfy | verified 2/19 multi-case | misleading-output |
| C4 | **1187 pending SAME_AS**, 1178 at `flagged_unverified`, emitted **uncapped, one caveat each**, rendering a raw machine token `"(tier flagged_unverified)"` to investigators. The 33 genuinely reviewable `human_review` links are buried | `graph_retriever.py:552-592`; `:463-502, :480-487` | misleading-output |
| C5 | `_ENUMERATION_KEYWORDS` = `("list","every","all","تمام","فہرست")` substring match — "wall"/"allegation"/"call" spuriously trigger `min_cases=1, limit=50`, silently flipping recurrence→enumeration without disclosure | `graph_retriever.py:237` | correctness/precision |
| C6 | `_recover_target_entity` uses `extract_statistical()` + `max(confidence)`; Roman-transliterated gazetteers underperform on Urdu-script FIR names. On failure returns `None` and silently falls to the broader recurring-entity seed set with no caveat | `:226-265` (the `None` path is documented as valid at `:247-250`) | availability/robustness |
| C7 | Status mapping (`:587-639`) sound; "both definite empty" branch (`:608-615`) correctly presents *no connections* as a finding rather than an error; **role gate correctly non-duplicated** (`:28-36`) and independently enforced in `retrieve_graph()`/`run_network_query()` (`xnetwork.py:67-78`) | — | **no-issue** |

**`basis`/`tier`/`confidence` propagation itself works** — all 1211 edges carry non-null
`basis` and `confidence`, and `:480-487` renders them. The defect is volume and
presentation, not plumbing.

### Shared modules

| Module | Status | Live impact of a fix |
|---|---|---|
| `src/retrieval/graph_retriever.retrieve_graph` | **SHARED** (`orchestrator.py:32`) | C4/C5 fixes change live GRAPH/XGRAPH answers. Highest blast radius |
| `src/pipeline/xnetwork.run_network_query` | **SHARED** (`orchestrator.py:40`) | Live XNETWORK route equally broken by C1; re-embedding fixes both at once |
| `src/pipeline/verifier.verify_grounding` | **SHARED** (`orchestrator.py:25`) | Do not alter; R1 is a composition issue, not a verifier bug |
| `src/retrieval/community_vector_store` | **SHARED** (via `run_network_query`) | Re-embedding is data-repair, not a code change — safe |
| `harness/tools/{xgraph,xnetwork}.py`, `citation_consistency`, `validation`, all `harness/agents/*` | **HARNESS-ONLY** | Free to fix |

### Tests (`tests/test_harness_agent_cross_case_linkage.py`, 708 lines)

100% monkeypatched tool results. Synthetic ids `CASE-002`/`CASE-005` (`:57, 224, 235`),
synthetic community text (`:65`), and tiers `"high"/"medium"/"low"` (`:435-436`) that
**do not exist in real data** (`flagged_unverified`/`human_review`).
`test_both_contribute_returns_ok` (`:217`) fabricates a non-empty `XNetworkToolResult`,
which **cannot occur in production** (C1). Nothing exercises
`query_similar_communities`, so a 0-count collection is invisible.

**Minimum realistic fixtures for this agent:**
1. **C1** — integration test asserting `query_similar_communities("...")` returns ≥1
   result, or that the Chroma community count equals
   `SELECT count(*) FROM community_reports`. Single highest-value addition in the whole
   remediation.
2. **C2/C3** — `XGraphToolResult(case_ids_touched=["fir-117-26","fir-118-26"], chunks=[2
   chunks from 2 *different* entities])` with `target_entity=None`; assert the
   description does not claim one entity spans both cases. Plus an `XNetworkToolResult`
   whose `case_ids_touched` has length 1; assert it is not rendered as cross-case.
3. **C4** — `unconfirmed_links` with 40 entries at `tier="flagged_unverified"`; assert
   output caps/ranks and does not emit a raw tier token to the investigator.
4. **C5** — a query containing the substring "wall" or "allegation"; assert it does NOT
   trigger enumeration mode (`min_cases=1, limit=50`).
5. Replace the synthetic tier vocabulary `"high"/"medium"/"low"` (`:435-436`) with the
   real `flagged_unverified`/`human_review` values.

### Live-data verification specific to this audit

Several figures carried into this investigation were stale and were re-measured:

| Fact | Brief said | **Actual (verified)** |
|---|---|---|
| Chroma `muhafiz_community_reports` | "verify (was 0)" | **0 embeddings** (`muhafiz_kb`=1054) |
| `community_reports` (Postgres) | 19 | 19 confirmed; **only 2 of 19 span >1 case** |
| SAME_AS edges | 340 | **1211** directed: `pending` **1187**, `confirmed` 19, `rejected` 5 |
| SAME_AS tier split | — | `flagged_unverified` 1178, `human_review` 33; **`basis` and `confidence` non-null on all 1211** |
| CROSS_VERSION_OF / CONFLICTS_WITH | verify | **0 / 0** |
| AGE graph name | — | `evidence_graph` (not `muhafiz_graph`) |

---

## 7. Reassessment of existing Phase 3 findings

### `case_summarization.py:160-163` — GENUINE, but narrower than first flagged

The prompt says status is *"e.g. open, closed, under investigation"* and asks for
*"people, vehicles, phone numbers, organizations"*. Against real data: status is empty in
52/73 and Urdu narrative otherwise; a typical case has Person 4, StructuredRecord 10,
Weapon 1, and **zero Vehicle/PhoneNumber/Organization**.

**Mitigating factor initially understated:** the same prompt already instructs *"If a
section has nothing to report from the material given, say so plainly rather than
guessing."* That is a real abstention guard.

**Severity: misleading-output (medium)**, not correctness — it *steers* toward a
vocabulary the data lacks rather than forcing a fabrication.

### `data_quality.py:231-239` — RE-SCOPED; the original "masking" framing was wrong

Tracing the actual mechanism: `_run_metric` (`:443-465`) uses a **primary key** per group
(`total_entities` for `entity_extraction`), and readiness is `READY if primary > 0`. So
zero-count labels are **not** "masked" — they are *reported* in `counts` as
`by_label:Vehicle=0`, and readiness legitimately reflects that the case *does* have
entities.

The genuine issue is narrower: `_ENTITY_LABELS` includes **`Organization`, which has 0
nodes corpus-wide**, so that dimension can never be anything but 0 — a permanently dead
dimension presented alongside live ones.

**Severity: misleading-output (low).**

Also verified: `_fetch_conflict_coverage` (`:396-404`) counts live `CONFLICTS_WITH` edges
(0 exist) but `incidents_checkable` is the primary key, so it correctly reports READY —
**correct by design**, the one graph metric that survives cleanly.

### Jurisdiction alias map — unchanged, still Phase 3

Root cause confirmed as **no English↔Urdu↔ASCII-ID mapping**, not a `toLower` no-op
(`station_id` is ASCII, so `toLower` works there). Measured: station `Karachi` resolves
via `station_id`, `Lahore` does not (`LHR` ≠ `lahore`); district `لاہور` resolves,
`Lahore` does not. Needs an alias map for 19 stations + 9 districts.

**Severity: P1 precision.** The role gate is untouched and was never bypassed — this is a
scope-narrowing failure inside already-authorized cross-case access, **not** an
access-control violation.

---

## 8. Consolidated remediation scope

### Genuine repairs required

| Finding | Agent/module | Root cause | Severity | Current behavior | Fix? | Shared w/ orchestrator? | Repair or new capability? |
|---|---|---|---|---|---|---|---|
| C1 | XNETWORK / community_vector_store | 19 Postgres reports never embedded to Chroma | correctness | Silent permanent EMPTY; validation gate dead | Yes | **Shared** (`orchestrator.py:40`) — fixes live route too | **Data repair** (re-embed), no code change |
| C2 | cross_case_linkage `_xgraph_confirmed_link` | `case_ids_touched` = multi-seed aggregate, described as one entity | correctness | Wrong attribution, unverified | Yes | Harness-only | Repair |
| T2 | structured_projection → `OCCURRED_ON.detail` | f-string leaked `None` at projection | correctness | 188 edges render `"entry None"` | Yes | Projection shared | Repair |
| T1 | `Incident.description` NULL 73/73 | Projection never populates it | correctness | Every event a placeholder | Yes | Projection shared | Repair |
| I1 | `tools/sql.py:73-78` | `source_type` dropped from chunk | misleading (med) | Synthetic data cited as law | Yes | **Harness-only** ✅ | Repair |
| C4 | graph_retriever `_unconfirmed_same_as_links` | No tier floor / cap | misleading | 1187 links, raw tokens | Yes | **Shared** (`:32`) | Repair |
| Jurisdiction | graph_retriever `_resolve_*_id` | No EN↔UR↔ID alias map | precision | Silent platform-wide | Yes | **Shared** | Repair |
| case_summ prompt | `:160-163` | Old taxonomy vocabulary | misleading (med) | Steers to absent fields | Yes | Harness-only | Repair |
| I3 | `prompts/sql_param_extractor.txt` | Old subject vocabulary | unsupported | Wrong extraction, empty result | Yes | **Shared** (`:22`) | Repair |
| T3/T4 | timeline_building | NULL `locked` → False; misleading caveat wording | misleading (low) | Definite "not locked"; "could not be completed" | Optional | Harness-only | Repair |
| C5 | graph_retriever `_ENUMERATION_KEYWORDS` | Substring collision | precision | Silent semantics flip | Yes | **Shared** | Repair |
| data_quality | `_ENTITY_LABELS` | `Organization` has 0 nodes | misleading (low) | Permanently dead dimension | Optional | Harness-only | Repair |
| R6 | file_structurer token cap | Urdu tokenizes poorly | robustness | Possible truncation | Optional | **Shared** | Repair |
| C6 | `_recover_target_entity` | Gazetteer vs Urdu names | robustness | Silent broader answer | Optional | Harness-only | Repair |

### Already safe — do not change

- **T5** — conflict gating (the anti-XAGG pattern)
- **I6** — honest degradation (`ABSTAINED`, `degraded_from`, verifier-rejection reset)
- **C7** — status mapping + role gate correctly non-duplicated
- **R3/R4/R5** — taxonomy independence, `session_id` validation, builder schema
- **T6** — ASSOCIATED_WITH reasoning correct; the "deviation" was the right call
- **T7** — `id(occ)` dedup key correct
- **I7/I8** — GRAPH leg improved by ASSOCIATED_WITH; `target_entity` threading correct
- `basis`/`tier`/`confidence` propagation plumbing
- `prompts/verifier.txt` — no drift
- Investigative Analysis inline prompt — clean, has abstention instruction

### False positives / re-scoped

- **`data_quality` "masking"** — mechanism was misdescribed; real issue is only the dead
  `Organization` label
- **T6 (ASSOCIATED_WITH deviation)** — flagged as a deviation; it is correct design
- **Jurisdiction "P0 security"** — corrected to P1 precision; role gate intact

### Out of scope — must NOT enter this remediation

- Query decomposition sub-agent
- `section_code` crime classification capability
- **Rebuilding community detection to produce genuinely multi-case clusters** (C3 — that
  is tuning a capability, not repairing drift)
- Duplicate `REAL-*` ingest dedup
- Any new capability smuggled into a repair

### No security/access-control findings

Across all four agents. Both cross-case gates are correctly enforced once, inside
`retrieve_graph()` and `run_network_query()`, and correctly not duplicated at the
sub-agent layer. Timeline Building is entirely within-case via `scoped_cypher()`, which
structurally refuses a template lacking `$case_id`. SQL is unscoped reference data by
design. Nothing crosses an authorization or confidentiality boundary.

### Headline per agent

- **Timeline Building — the *good* case.** Its Unit-6 `conflicts_checked_at` guard is
  exactly the fail-closed pattern XAGG lacked: it correctly abstains on all 73 real cases
  rather than reporting a false all-clear. Its real defects are user-visible description
  *quality* (NULL descriptions, `"entry None"`), not wrong reasoning. It is also the only
  fully deterministic sub-agent — no LLM, no prompt — and never touches the four biggest
  real-data traps.

- **Investigative Analysis — degrades honestly, but has one genuine hole.** The
  `ABSTAINED`/`degraded_from` machinery is sound and discloses missing SQL data properly.
  The hole: **synthetic reference rows lose their `source_type` marker and are presented
  to the model and the user as real penal-code law**, and neither the Verifier nor the
  Validation tier can detect it because the citation is internally consistent.

- **Report Drafting — structurally sound, but inherits upstream error into a durable
  artifact.** It is taxonomy-free (no `crime_category`/`investigation_status` coupling),
  `session_id` validation is correct, and the disclosure machinery genuinely fires. But
  the single-synthetic-chunk regime means the Verifier can only catch drafting-stage
  hallucination, not an upstream wrong summary — which then becomes a formal,
  downloadable document.

- **Cross-Case Linkage — the most affected of the four.** XNETWORK is entirely dead in
  production (zero embeddings), taking its "mandatory" validation gate down with it; and
  the XGRAPH confirmed-link description misattributes a multi-seed case footprint to a
  single entity, in deterministic text the Verifier deliberately never checks.

---

## 9. Recommended Phase 3 ordering

### 3a — Data repairs (no code; unblocks measurement)

Re-embed the 19 community reports into Chroma (**C1**); investigate/repair the projection
defects feeding **T1**/**T2**. These are *data* problems; fixing code first would be
measuring against broken inputs. C1 also fixes the **live** XNETWORK route.

### 3b — Harness-only, zero live risk

**I1** (`tools/sql.py` provenance), **C2** (`_xgraph_confirmed_link`),
**case_summarization prompt**, **data_quality `Organization` label**, optionally
**T3/T4**. All isolated from live traffic.

### 3c — Shared modules, one at a time with orchestrator verification

**C4** (tier floor/cap), **jurisdiction alias map**, **I3**
(`sql_param_extractor.txt`), **C5** (enumeration keywords). Each changes live behavior;
each needs explicit before/after on both paths.

### Dependency notes

- **C3** (single-case clusters) is only *observable* after C1 — do not act on it until
  then, and it may prove out of scope entirely.
- **I4** (stale chunks) is resolved by the Phase 2 purge, not by a code fix.
- **R1/R2** are composition properties, not bugs to patch — they inform how much trust to
  place in generated documents, and argue for fixing upstream (case_summarization) rather
  than the drafting layer.

---

## 10. Test/fixture implications (Phase 4 — mandatory)

Every one of the ~1600 currently-green tests would pass through all findings above,
because fixtures are synthetic (`CASE-001`, `P-1`/`V-1`) and tools are stubbed above the
data layer. **Green today means "internal logic unchanged," not "works on real data."**

### Minimum mandatory regression fixtures

1. **Corpus-consistency test** — assert Chroma community count ==
   `SELECT count(*) FROM community_reports`. Highest value; catches **C1** directly.
2. **Real-shape timeline row** —
   `("fir-117-26", description=None, locked=None, detail="entry None", checked=False)`;
   assert output contains neither a bare placeholder nor `"entry None"`. Catches
   **T1/T2/T3** in one fixture.
3. **Multi-seed XGRAPH** — `case_ids_touched=["fir-117-26","fir-118-26"]` from *two
   different* entities with `target_entity=None`; assert no claim that one entity spans
   both. Catches **C2**.
4. **Synthetic-provenance SQL chunk** — `source_type="synthetic"`; assert it is
   disclosed. Catches **I1**.
5. **Unconfirmed-link flood** — 40 links at `tier="flagged_unverified"`; assert
   capped/ranked and no raw tier token emitted. Catches **C4**.
6. **Real-format id smoke fixture** — `fir-117-26`, statute `crime_category`, empty
   `investigation_status` — as a *shared* fixture so **any** agent coupling to old shapes
   fails loudly.
7. **Single-case XNETWORK cluster** — `case_ids_touched` of length 1; assert it is not
   rendered as a cross-case finding. Catches **C3** (after C1).

---

## Appendix A — Prior phase context

### Phase 1 (COMPLETE, committed as `1a81bff`)

Fixed XAGG reporting unevaluatable filters as zero. Measured against the live 73-case
corpus **before** the fix:

| Query | Before | After |
|---|---|---|
| "how many closed cases" | **0** (stated as fact) | All matching cases + *"status could not be filtered…"* |
| "how many open cases" | **73** (stated as fact) | All matching cases + same disclosure |
| "how many theft cases" | **0** (stated as fact) | All matching cases + *"these records classify by statute…"* |
| "cases per category" | `PPC: 41` labelled as category | Same figures + *"grouped by statute, not crime type"* |
| District query that won't resolve | Platform-wide, presented as scoped | Platform-wide + *"could not be matched to a district…"* |

Root causes (both upstream-intentional, **not** sync bugs):
- `investigation_status` = `_current_status()`'s projection of `psrms.fir_position`'s
  latest row → free-text Urdu, empty in 52/73
- `crime_category` = `_crime_category()` joining distinct `act` values off
  `psrms.fir_section` → statute lists. The real FIR schema carries only `section_code`
  and `act`; **no offence-category field exists upstream**

Design decisions:
- `_status_filter_supported()` / `_crime_type_filter_supported()` are **data-driven**, so
  the filter re-enables itself if a future corpus regains parseable values (verified both
  ways: old-style fixtures still filter correctly with **zero** caveats)
- Jurisdiction disclosure carried on a **ContextVar**, not a changed return type — a
  sentinel would be consumed as a case-id allow-list and silently zero the query
- Caveats prepended into the aggregate **text**, not just metadata, because that text is
  also `raw_summary_text` — what the Verifier sees on rejection
- `xagg.py` is **shared**: orchestrator and harness were broken identically and are fixed
  identically, rendering the same constants from one source of truth

### Phase 2 (sync COMPLETE; purge PENDING approval)

Sync ran `--full`, exit 0, zero errors:

| | Pre-sync | Post-sync |
|---|---|---|
| Chroma chunks | 264 | **1054** |
| New-format (`fir-*`) chunks | 0 | **712** |
| Cases with vectors | 0 / 73 | **73 / 73** ✅ |
| Graph edges | 4,722 | 4,952 → 7,732 (after dump restore) |

Idempotency verified three ways before running: the module docstring's stated guarantee,
`purge_edges_by_source_prefix` (edge-only, never nodes — so a CNIC-auto-merged Person
survives), two passing regression tests (`twice`, `three times`), and
`upsert_documents(ids=…)` keyed on deterministic chunk IDs for the Chroma half.

**Key discovery:** the new corpus has **no source PDFs** — records are API-derived
(`psrms/fir/...#narrative`, `#zimni`, `#position`). The original "re-ingest PDFs from
disk" plan would have been a no-op.

### Database restore (COMPLETE)

42.5MB dump restored with zero errors. `community_reports` 0 → 19 in **Postgres**
(but **not** Chroma — see C1). Required re-applying
`migrations/015_app_least_privilege_role.sql` because the dump carries no role
definitions.

---

## Appendix B — Verification commands used

All read-only. No data modified.

```bash
# Chroma inventory + three-discriminator agreement
python -c "import chromadb; coll = chromadb.PersistentClient(path='data/chroma_db')\
  .get_collection('muhafiz_kb'); r = coll.get(include=['metadatas']); ..."

# Graph node census
SELECT * FROM cypher('evidence_graph',
  $$ MATCH (n) RETURN labels(n)[0] AS label, count(*) AS c $$) AS (label agtype, c agtype);

# Graph edge census
SELECT * FROM cypher('evidence_graph',
  $$ MATCH ()-[r]->() RETURN type(r) AS t, count(*) AS c $$) AS (t agtype, c agtype);

# SAME_AS status split  →  pending 1187 / confirmed 19 / rejected 5
SELECT * FROM cypher('evidence_graph',
  $$ MATCH ()-[r:SAME_AS]->() RETURN r.status AS s, count(*) AS c $$) AS (s agtype, c agtype);

# Incident.description NULL check  →  73 total, 0 with description
SELECT * FROM cypher('evidence_graph',
  $$ MATCH (i:Incident) RETURN count(i) AS total, count(i.description) AS with_desc $$)
  AS (total agtype, with_desc agtype);

# "entry None" literal count  →  188
SELECT * FROM cypher('evidence_graph',
  $$ MATCH ()-[r:OCCURRED_ON]->() WHERE r.detail = 'entry None' RETURN count(r) $$)
  AS (c agtype);

# Community reports spanning >1 case  →  2 of 19
SELECT count(*) total,
       count(*) FILTER (WHERE array_length(
         string_to_array(trim(both '{}' from case_ids::text), ','), 1) > 1) multi
FROM community_reports;

# Conflict-detection coverage  →  0 of 73
SELECT count(*) FILTER (WHERE conflicts_checked_at IS NOT NULL) checked,
       count(*) total FROM cases;

# Chroma community collection  →  0
python -c "import chromadb; print(chromadb.PersistentClient(path='data/chroma_db')\
  .get_collection('muhafiz_community_reports').count())"

# Orphan verification: target case_ids/doc_ids vs Postgres  →  0 / 0
```

---

## Final stop condition

**This is an investigation + verification checkpoint only.**

Not done, and not authorized:
- ❌ Purging the 231 ChromaDB chunks
- ❌ Deleting/resetting any database data
- ❌ Modifying production code, prompts, tests, or schema
- ❌ Implementing the jurisdiction alias map
- ❌ Fixing `case_summarization.py` or `data_quality.py`
- ❌ Implementing fixes for any of the four investigated agents
- ❌ Introducing query decomposition or `section_code` crime classification

Awaiting explicit approval on (1) the 231-chunk purge and (2) the Phase 3 scope/order.
