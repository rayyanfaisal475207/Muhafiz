# Blind Aggregate QA Corpus — Summary

**Companion to:** `BLIND_AGGREGATE_QA_CORPUS.md`
**Date:** 2026-09-17
**Target:** `evidence_graph` (Apache AGE 1.5.0 / PostgreSQL 16), live container `muhafiz-postgres`

---

## Number of Queries

**25** (target range was 20–25).

| Difficulty | Count |
|---|---:|
| Easy | 5 |
| Medium | 8 |
| Hard | 7 |
| Adversarial | 5 |

---

## Category Coverage

| Category | IDs | Count |
|---|---|---:|
| 1 — Simple Aggregates | QA-001 … QA-003 | 3 |
| 2 — Filtered Aggregates | QA-004 … QA-006 | 3 |
| 3 — Distinct Entity Queries | QA-007 … QA-009 | 3 |
| 4 — Relationship-Based | QA-010 … QA-012 | 3 |
| 5 — Role / Semantic | QA-013 … QA-015 | 3 |
| 6 — Temporal | QA-016 … QA-018 | 3 |
| 7 — Multi-Constraint | QA-019, QA-020 | 2 |
| 8 — Adversarial | QA-021 … QA-024 | 4 |
| 9 — Complex Investigation | QA-025 | 1 |

All nine required categories are covered. No two queries test the same
reasoning pattern.

---

## Entities Tested

| Entity | Instances | Queries |
|---|---:|---|
| Case | 73 | QA-001, 009, 010, 011, 012, 020, 025 |
| Person | 430 (208 unmerged) | QA-006, 007, 008, 012, 013, 014, 015, 019, 020, 022, 023, 025 |
| Weapon | 32 | QA-002, 004, 010, 019, 021, 024, 025 |
| Incident | 73 | QA-013, 014, 016, 017, 018, 025 |
| Document | 126 | QA-005 |
| Officer | 1155 | QA-011, 023 |
| PoliceStation | 19 | QA-003, 009, 020, 025 |
| District | 9 | QA-009, 020, 025 |

Deliberately excluded as too sparse for meaningful aggregates: `Vehicle` (2),
`PhoneNumber` (2), `Organization` (0), `StructuredRecord` (provenance-internal),
`Address` (2171 nodes but only 71 with usable text).

---

## Relationships Tested

| Relationship | Shape | Queries |
|---|---|---|
| `INVOLVED_IN` (with `role`) | Person → Incident | QA-013, 014, 015, 019, 025 |
| `BELONGS_TO_CASE` | Person/Weapon/Officer → Case | QA-010, 012, 020, 023 |
| `FILED_AT` | Case → PoliceStation | QA-009, 020, 025 |
| `PART_OF` | PoliceStation → District; Incident → Case | QA-009, 020, 025 |
| `ASSIGNED_TO` | Officer → Case | QA-011 |
| `OWNS` | Person → Weapon | QA-019 |
| `CITES` | Case → Case | QA-012 |
| `SAME_AS` / `merged_into` | Person → Person | QA-007, 023 |

Three-hop traversals appear in QA-020 and QA-025.

---

## Hardest Queries

Ranked by likelihood of exposing a real defect:

1. **QA-022** — nonexistent attribute (`criminal_risk_score`). The trap is
   `confidence`: numeric, present on all 430 persons, and a plausible-looking
   substitute that would fabricate a criminality statistic from a data-quality
   field. Highest-severity failure available.
2. **QA-004** — Urdu literal `بغیر لائسنس` for "unlicensed". Fails silently and
   confidently as 0. Gateway test for the entire cross-lingual dimension.
3. **QA-025** — full investigative chain; roles attach to Incident, not Case.
   Two measures, three-hop geography, Urdu caliber literal.
4. **QA-007** — 222 of 430 persons are merged duplicates. Answer is 208 or 430
   depending on reading; an undisclosed 430 is a failure.
5. **QA-023** — ambiguous on three axes simultaneously. Any bare number fails.
6. **QA-015** — two filters on one edge, Urdu free-text status, 57% null.
7. **QA-020** — "Faisalabad" → `فیصل آباد` transliteration plus three hops.
8. **QA-017** — the 2025 bucket is genuinely empty; zero is the answer, not an
   error.
9. **QA-019** — intersection vs union of two relationship constraints.
10. **QA-024** — correct answer is 0 for a legitimate reason; only meaningful
    when graded against QA-004.

---

## Recommended Execution Order

Run in phases and stop early if a phase fails badly — later phases assume
earlier capabilities work.

### Phase 1 — Baseline (must pass before anything else)
`QA-001` → `QA-002` → `QA-003` → `QA-005`

Establishes that basic counting and English-literal filtering work. If QA-005
fails, filtering is broken outright and cross-lingual results in Phase 3 will
be uninterpretable.

### Phase 2 — Cardinality discipline
`QA-008` → `QA-010` → `QA-011`

Tests COUNT vs COUNT DISTINCT and edge-vs-node conflation. Cheap to run, and
failures here explain inflated numbers throughout the rest of the suite.

### Phase 3 — Cross-lingual literals (the decisive phase)
`QA-004` → `QA-024` → `QA-006`

**Grade QA-004 and QA-024 together.** If QA-004 returns 0, cross-lingual
matching is broken and QA-015, QA-020 and QA-025 will fail for the same root
cause — treat those as blocked rather than as independent defects.

### Phase 4 — Roles and semantics
`QA-013` → `QA-014` → `QA-015`

### Phase 5 — Traversal
`QA-009` → `QA-012` → `QA-019`

### Phase 6 — Temporal
`QA-016` → `QA-018` → `QA-017`

QA-017 last in this phase — the empty-bucket probe is only interpretable once
basic date filtering is confirmed by QA-016.

### Phase 7 — Safe failure
`QA-021` → `QA-022` → `QA-023`

Run these **before** the final phase. A system that fabricates attributes
should not be evaluated on complex queries until that is fixed.

### Phase 8 — Full investigative chain
`QA-007` → `QA-020` → `QA-025`

The three composite queries. Meaningful only if Phases 3–5 passed.

---

## Two Cautions for Whoever Grades This

**Ten of 25 queries can return the correct number through incorrect
reasoning** on this snapshot — QA-001 (Case and Incident both 73), QA-009
(District count equals traversed count), QA-010 (weapons and their cases both
32), QA-019 (all weapon owners happen to be accused), QA-024 (0 is right for
the wrong reason), QA-025 (the only caliber present makes that filter a no-op),
among others. **Inspect the emitted query plan, not just the output value.**
Value-only grading will overstate pass rate.

**These counts are true as of 2026-09-17.** If the graph is rebuilt or
re-ingested, re-derive them with the Cypher shown in the corpus. The reasoning
requirements and failure modes are snapshot-independent; only the numbers move.
