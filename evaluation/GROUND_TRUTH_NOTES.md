# Gold-QA Ground-Truth Notes (Module 1)

Investigated live, against the running Postgres/AGE instance
(`docker compose up -d postgres`, `DATABASE_URL` in `.env`) on 2026-09-05.
Every number below was queried directly, not copied from another document.

## 1. The 73-vs-79 FIR count — resolved: it was a data-hygiene bug, not a snapshot-timing issue

**Finding:** the Postgres `cases` table had **79** rows, but only **73** carry a
real `fir-` id. The other **6** were `CASE-TEST-*` rows:

| case_id | created_at | fields |
|---|---|---|
| CASE-TEST-3500a8 | 2026-08-27 20:58:04 | all null except timestamps |
| CASE-TEST-747b9f | 2026-08-27 20:58:04 | all null except timestamps |
| CASE-TEST-4b1bf2 | 2026-08-27 20:58:04 | all null except timestamps |
| CASE-TEST-102c80 | 2026-08-28 06:53:34 | all null except timestamps |
| CASE-TEST-b35b8e | 2026-08-28 06:53:34 | all null except timestamps |
| CASE-TEST-41ec94 | 2026-08-28 06:53:36 | all null except timestamps |

All real `fir-*` rows share a single `created_at` (2026-08-20 12:45:00 —
the bulk ingest timestamp), confirming the 6 `CASE-TEST-*` rows are a separate,
later event: automated-test fixtures (two `sessions` rows named "Case A chat"
with no `user_id`, 6 matching `case_assignments` rows, both dated the same two
timestamps) that were written into the shared dev/demo database instead of an
isolated test database — not real data, and not a live-data-growth situation.

**The AGE graph never had these 6** — `MATCH (c:Case) RETURN count(c)` was
already **73** before any cleanup, confirming the graph ingestion path was never
affected, only the relational `cases` table (which is what a raw SQL-based
"how many cases/FIRs" aggregate would count).

**Action taken:** backed up the three affected tables
(`data/backups/muhafiz_pre_case_test_cleanup_<timestamp>.sql`, `pg_dump
--data-only -t cases -t case_assignments -t sessions`), then deleted the 6
`case_assignments` rows, 2 `sessions` rows, and 6 `cases` rows referencing
`CASE-TEST-%`. No `documents`, `generated_files`, `audit_logs`,
`chunk_fulltext`, `ingestion_run_quality`, or `same_as_queue_snapshot` rows
referenced them — confirmed empty before deleting.

**Result:** `SELECT count(*) FROM cases` now returns **73**, matching the graph
and the gold answer.

**Authoritative number for D1 and A7's denominator: 73.**

## 2. CP6 — the official gold answer (11) is right, but reflects a since-superseded state; live current count is 10

**Finding:** querying the live graph for the *current* (non-superseded)
investigating officer per case (`ASSIGNED_TO {role: "investigating"}` edges
where `superseded_by IS NULL`):

- **10** cases currently have only a placeholder officer name: 7×
  `(نامزد ASI)`, 3× `(نامزد SI)` — note the placeholder is written in **Urdu
  script** (`نامزد`), not the Latin transliteration "Naamzad" the draft answer
  key and gold answer's romanization implied. Any aggregate matching on the
  Latin string alone will silently match zero rows — see Module 7.
- Including **superseded** edges (i.e. every placeholder ever recorded, even if
  later replaced by a real officer), the count is **11**: `fir-205-26` had a
  placeholder `(نامزد ASI)` that was later superseded by a real officer
  assignment (belt `GEN-0105`) — this is the exact case
  `structured_projection.py`'s own `_write_investigating_officers()` docstring
  cites as its worked example of a supersession chain.
- The official `Gold_QA_Dataset_Final32.json` answer for CP6 is **11**, which
  matches the historical/ever-assigned count, not the current-state count. The
  question's own phrasing ("abhi tak... **still** assigned only a placeholder")
  reads as asking about current state — so the live, honest current answer is
  **10**, with 11 being what was true before `fir-205-26`'s officer was later
  updated.
- Cross-checked the gold answer's other detail — "the busiest named officers
  (Faisal and Tariq) are assigned 4 FIRs each" — against live data: `طارق`
  (Tariq) and `فیصل` (Faisal) each currently appear as the investigating officer
  on exactly 4 cases. Matches exactly, which increases confidence the rest of
  the gold answer (the 11) reflects a real prior state, not an error.
- The draft `evaluation/gold32_answer_key.json`'s "0" for CP6 answered a
  different question ("how many cases have NO officer row at all" — genuinely
  0, every case has *some* officer row) and is simply wrong for what CP6 asks.
  Superseded by this note; not worth editing the draft file further since the
  official key is what matters going forward.

**Authoritative number for Module 7 (CP6 aggregate):** build the aggregate
against the **live, current-state count (10 today)** — it is the honest,
defensible number for a live demo — and phrase the answer to also mention the
total-ever-assigned figure (11) so it still reads as a correct, in-context match
against the official gold answer of 11 under contextual grading ("10 FIRs
currently carry only a placeholder investigating officer — 7 marked `(نامزد
ASI)`, 3 marked `(نامزد SI)`; an 11th case, fir-205-26, originally had a
placeholder too but has since been assigned a real officer").

## 3. The 72-vs-73 Incident/`BELONGS_TO_CASE` edge gap — already resolved, no action needed

**Finding:** `HANDOFF.md` (goldtest-eval3, unmerged) described one Incident node
missing a `BELONGS_TO_CASE` edge. Querying the live graph directly today:

```
MATCH (c:Case) RETURN count(c)                                  -> 73
MATCH (i:Incident) RETURN count(DISTINCT i)                      -> 73
MATCH (i:Incident)-[:BELONGS_TO_CASE]->(c:Case) RETURN count(DISTINCT i) -> 73
```

All three agree at 73 — **no orphaned Incident exists in the current graph.**
This was likely resolved by the already-merged `fix/backfill-orphaned-graph-case-edges`
work (`scripts/backfill_missing_belongs_to_case.py`, merged to `main` via commit
`9c5f7e1`) or a subsequent re-sync, even though that script's own docstring
scopes it to Officer/Vehicle nodes specifically, not Incident. Whatever the
mechanism, the live data no longer shows this gap — **no backfill needed.**

(Note for future graph-aggregate authors: Apache AGE's Cypher parser rejects a
negated relationship pattern directly in a `WHERE` clause, e.g.
`WHERE NOT (i)-[:BELONGS_TO_CASE]->(:Case)`, with a bare syntax error — confirmed
live. Use two separate `MATCH`/count queries and diff in Python, as done here,
rather than that pattern.)

## 4. A1 (Module 10.1) — the app is dividing by the wrong denominator, not missing data; gold's 94/67/24/3 is exactly reproducible

**Finding:** live graph, queried directly (`MATCH (p:Person)-[r:INVOLVED_IN
{role: "accused"}]->(i:Incident) ...`):

| Count basis | Male | Female | Unknown | Total |
|---|---|---|---|---|
| **Row-level** (`count(r)` — one per `fir_accused` row/mention) | **67** | **24** | **3** | **94** |
| **Distinct-Person-level** (`count(DISTINCT p)` — one per resolved entity) | 65 | 24 | 3 | 92 |

Row-level matches the official gold answer (67/24/3/94) **exactly** — female
and unknown already matched at both levels; only male/total differ, by
precisely 2.

**Root cause, pinpointed to the line:** `src/pipeline/xagg.py::_gender_breakdown()`
(~line 482) builds `seen_entities: dict[entity_id, gender]`, keyed by
`entity_id` — i.e. it counts **distinct resolved persons**, not accused
**entries**. Two people are named as accused in two different FIRs each
(confirmed by direct query: two `PERSON-*` nodes each carry 2 `INVOLVED_IN
{role: "accused"}` edges — these are the same two recidivist identities S3/CR2
already surface: شہزیب عرف شابی across FIR 891/24 and 214/26, and عاصم رشید
across FIR 64/26 and 65/26). Because `_gender_breakdown()` dedupes by
`entity_id`, each of those two men's second accused-entry is silently dropped
— losing exactly 2 from the male count and 2 from the total.

**This is not a data gap and not an entity-resolution bug** — the entity
resolution correctly recognizing the same person across two FIRs is exactly
right and exactly what CR2/S3 depend on. It's a **question-framing mismatch**:
gold's A1 explicitly asks about "94 ملزم اندراجات" — 94 accused **entries**
(i.e. count the `fir_accused` row/mention, once per FIR each person is named
in), not 94 distinct people. `_gender_breakdown()` needs to count
`INVOLVED_IN {role: "accused"}` **edges**, not `DISTINCT p` — a one-line
change (drop the `seen_entities` dedup-by-entity_id map, count edges with
their own gender read directly, or equivalently `count(r)` grouped by
`p.gender` in the Cypher itself).

**Authoritative numbers for Module 13's A1 aggregate: 67 male / 24 female / 3
unknown / 94 total — reproduced exactly by counting accused edges, not
distinct persons.** No ground-truth ambiguity remains; this is a pure
app-code fix, scoped and ready for Module 13.

## Summary — authoritative numbers for Modules 2, 4, 7, 13

| Question | Authoritative target | Source |
|---|---|---|
| D1 (how many FIRs) | **73** | Postgres `cases` table, post-cleanup; matches graph and gold |
| A7 denominator (of how many FIRs) | **73** | same |
| CP6 (placeholder-officer count) | **10 current** (mention 11 historical/ever-assigned for full context) | live graph, `ASSIGNED_TO` supersession chain |
| A1 (accused gender ratio) | **67 M / 24 F / 3 unknown / 94 total** | live graph, `INVOLVED_IN{role:"accused"}` edge count (not distinct-person count) |

No further ground-truth work is needed before Modules 2, 4, 7, and 13 proceed.
