# Blind Aggregate QA Corpus — Muhafiz Evidence Intelligence Platform

**Author:** Independent QA analyst
**Date:** 2026-09-17
**Method:** Corpus derived exclusively from direct inspection of the live
`evidence_graph` graph in the running `muhafiz-postgres` container (Apache AGE
1.5.0 / PostgreSQL 16). No existing QA corpus, evaluation report, or
implementation source was consulted. Every query below was checked to be
answerable from real data, and every expected answer was computed by running
Cypher against the graph.

---

## 1. Dataset Summary

### 1.1 How the data was inspected

```
docker exec muhafiz-postgres psql -U postgres -d muhafiz \
  -c "LOAD 'age'; SET search_path=ag_catalog,public;
      SELECT * FROM cypher('evidence_graph', $$ ... $$) as (...);"
```

Two graphs exist: `evidence_graph` (production) and `evidence_graph_eval` (an
evaluator copy). All figures below are from **`evidence_graph`**.

### 1.2 Node labels and counts

| Label | Count | Notes |
|---|---:|---|
| Address | 2171 | Largest label; only 71 carry `text`/`normalized_text` |
| Officer | 1155 | Only 77 have `designation`/`belt_no` |
| StructuredRecord | 713 | Record-level provenance nodes |
| Person | 430 | **222 are merged duplicates** (`merged_into` set) |
| Date | 137 | Standalone date nodes, `date` = `YYYY-MM-DD` |
| Document | 126 | |
| Case | 73 | **Only property: `case_id`** |
| Incident | 73 | One per case |
| Weapon | 32 | |
| PoliceStation | 19 | |
| District | 9 | |
| Vehicle | 2 | Very sparse |
| PhoneNumber | 2 | Very sparse |
| Organization | **0** | Label exists, zero instances |

### 1.3 Edge types and counts

| Edge | Count | Observed shape(s) |
|---|---:|---|
| SAME_AS | 4702 | Address→Address (4196), Person→Person (444), Officer→Officer (62) |
| BELONGS_TO_CASE | 3605 | Address/StructuredRecord/Person/Officer/Incident/Weapon/Document → Case |
| APPEARS_IN | 3591 | Address/StructuredRecord/Person/Officer/Weapon/Vehicle/PhoneNumber → Document |
| OCCURRED_ON | 568 | Incident→Date (432), Person→Date (74), Officer→Date (62) |
| ASSOCIATED_WITH | 248 | Person→Person |
| INVOLVED_IN | 221 | **Person→Incident only** — carries `role` |
| PART_OF | 146 | PoliceStation→District (73), Incident→Case (73) |
| ASSIGNED_TO | 144 | Officer→Case |
| LOCATED_AT | 101 | Person→Address (93), Person→PoliceStation (8) |
| FILED_AT | 73 | Case→PoliceStation |
| OWNS | 30 | Person→Weapon |
| RELATED_TO | 24 | Person→Person |
| CITES | 9 | Case→Case |
| REGISTERED_TO | 5 | Vehicle→Person |
| CONFLICTS_WITH | **0** | Type exists, zero instances |
| CROSS_VERSION_OF | **0** | Type exists, zero instances |

### 1.4 Properties that actually exist

**Person** (430): `entity_id`, `canonical_name`, `confidence`,
`extraction_confidence`, `as_of` (all 430); `name_skeleton` (416);
`merged_into`, `merged_at` (222); `source_doc_id` (206); `cnic` (196);
`gender` (119); `father_name` (93); `address_text` (92); `phone` (90);
**`age` (only 19)**.

**Case** (73): `case_id`, `as_of`, `confidence`, `source_doc_id`. **There is
no `status`, `type`, `category`, or `classification` property on Case.**

**Incident** (73): `entity_id`, `canonical_name`, `description`,
`report_datetime`, `zimni_entry_count`, `zimni_typed_count` (all 73);
`incident_datetime` (64); `station_departure_datetime` (44);
`reporting_delay_reason` (8).

**Weapon** (32): `canonical_name`, `entity_id` (32); `caliber_or_bore` (30);
`license_status` (30); `condition` (5).

**Document** (126): `doc_id`, `source_doc_id` (126); `filename` (125);
`doc_type` (124).

**Officer** (1155): `canonical_name`, `name_skeleton`, `entity_id` (1155);
`designation`, `belt_no` (77); `phone` (70).

**Vehicle** (2): `plate`, `canonical_name` (2); `make`, `model`, `chassis_no`,
`engine_no`, `verification_result` (1 each).

**INVOLVED_IN edge**: `role` (221), `arrest_status` (94).

### 1.5 Value distributions — the decisive finding

**The corpus is predominantly Urdu.** Literal values are Urdu strings, not
English. Any system that matches English literals against this data will
silently return zero.

| Field | Values |
|---|---|
| `Weapon.license_status` | `بغیر لائسنس` ("unlicensed") ×30; null ×2. **No licensed weapon exists.** |
| `Weapon.caliber_or_bore` | `30 بور` ×30; null ×2. Single-valued. |
| `Weapon.condition` | 5 distinct free-text Urdu values, 27 null |
| `Person.gender` | `مرد` ×85, `عورت` ×34, **null ×311** |
| `Person.age` | 19 values only, range 24–49 |
| `Document.doc_type` | `fir_structured` ×73, `criminal_record_structured` ×33, `pkm_structured` ×14, `cms_structured` ×4, null ×2 — **these are English** |
| `INVOLVED_IN.role` | `accused` ×94, `complainant` ×73, `witness` ×37, `victim` ×9, `applicant_pkm` ×4, `complainant_cms` ×4 — **English** |
| `INVOLVED_IN.arrest_status` | Urdu free text, 12 distinct values, 127 null. `زیر تفتیش` ×73, `گرفتار` ×11, rest ×1 |
| `Officer.designation` | `ASI` ×66, `SI` ×8, `سب انسپکٹر` ×2, `انسپکٹر` ×1, null ×1078 — **mixed English/Urdu for the same roles** |
| `District.name` | 9 Urdu names (فیصل آباد, لاہور, راولپنڈی, کراچی ایسٹ, کراچی وسطی, اسلام آباد, چنیوٹ, حیدر آباد, ملتان) |
| `PoliceStation.name` | 19 Urdu names |

### 1.6 Temporal coverage

- `Incident.incident_datetime`: `2024-09-14T22:00:00Z` → `2026-08-01T06:50:00Z`.
  **51 in 2026, 0 in 2025, 13 before 2025, 9 null.** The 2025 gap is real.
- `Date.date`: ISO `YYYY-MM-DD`, 137 nodes.
- `Incident.report_datetime`: present on all 73.

### 1.7 Two structural traps every query must respect

1. **Entity resolution / merged duplicates.** 222 of 430 Person nodes have
   `merged_into` set, and 444 Person→Person `SAME_AS` edges exist. "How many
   persons" has two defensible answers: 430 (raw nodes) or 208 (live
   entities). The same applies to Address (2171 raw, 4196 SAME_AS edges) and
   Officer (1155 raw, 62 SAME_AS edges). A correct system should either pick
   the resolved reading and say so, or ask.

2. **Roles live on Person→Incident, not Person→Case.** There is no role-bearing
   edge to Case. Any role question about cases must traverse
   `Person-[:INVOLVED_IN {role}]->Incident-[:PART_OF|BELONGS_TO_CASE]->Case`.

---

## 2. Query Distribution by Category

| # | Category | Query IDs | Count |
|---|---|---|---:|
| 1 | Simple Aggregates | QA-001, QA-002, QA-003 | 3 |
| 2 | Filtered Aggregates | QA-004, QA-005, QA-006 | 3 |
| 3 | Distinct Entity Queries | QA-007, QA-008, QA-009 | 3 |
| 4 | Relationship-Based | QA-010, QA-011, QA-012 | 3 |
| 5 | Role / Semantic | QA-013, QA-014, QA-015 | 3 |
| 6 | Temporal | QA-016, QA-017, QA-018 | 3 |
| 7 | Multi-Constraint | QA-019, QA-020 | 2 |
| 8 | Adversarial | QA-021, QA-022, QA-023, QA-024 | 4 |
| 9 | Complex Investigation | QA-025 | 1 |
| | **Total** | | **25** |

Difficulty: Easy 5 · Medium 8 · Hard 7 · Adversarial 5.

---

## 3. The Corpus

> **On expected answers.** Each entry records the value obtained by running
> Cypher against the live graph on 2026-09-17. This is ground truth for *this
> snapshot*, given as a grading aid — not as a constraint on how the system
> should phrase its response. Where two readings are defensible, both are
> listed and the grading rule says which to accept.

---

### QA-001

**User Question**
> How many cases are recorded in the system?

**Query Category**
Simple Aggregate

**Expected Reasoning**
Count all `Case` nodes. No filter, no traversal. The simplest possible
aggregate — it establishes a baseline.

**Expected Entities**
Entity: `Case` · Measure: `COUNT`

**Required Relationships**
None

**Required Filters**
None

**Expected Answer**
`73`

**Difficulty**
Easy

**Expected Failure Risk**
- Counting `Incident` instead of `Case` (also 73 — a coincidence that would
  mask the error; check the emitted plan, not just the number).
- Counting `BELONGS_TO_CASE` edges (3605) rather than distinct Case nodes.

---

### QA-002

**User Question**
> How many weapons are in the evidence records?

**Query Category**
Simple Aggregate

**Expected Reasoning**
Count all `Weapon` nodes.

**Expected Entities**
Entity: `Weapon` · Measure: `COUNT`

**Required Relationships**
None

**Required Filters**
None

**Expected Answer**
`32`

**Difficulty**
Easy

**Expected Failure Risk**
- Counting `OWNS` edges (30) — two weapons have no owner, so the edge count
  silently undercounts.

---

### QA-003

**User Question**
> How many police stations are there?

**Query Category**
Simple Aggregate

**Expected Reasoning**
Count all `PoliceStation` nodes. Tests whether a low-frequency label is
reachable at all.

**Expected Entities**
Entity: `PoliceStation` · Measure: `COUNT`

**Required Relationships**
None

**Required Filters**
None

**Expected Answer**
`19`

**Difficulty**
Easy

**Expected Failure Risk**
- Counting `FILED_AT` edges (73) instead of stations.
- Confusing `PoliceStation` with `District` (9).

---

### QA-004

**User Question**
> How many unlicensed weapons are recorded?

**Query Category**
Filtered Aggregate + Value Literal

**Expected Reasoning**
Count `Weapon` nodes whose license status indicates unlicensed. **The stored
value is the Urdu string `بغیر لائسنس`, not "unlicensed".** The system must
bridge from the English question to the Urdu literal — via value-vocabulary
lookup, translation, or fuzzy match. A literal `= 'unlicensed'` comparison
returns 0, which is wrong.

**Expected Entities**
Entity: `Weapon` · Measure: `COUNT`

**Required Relationships**
None

**Required Filters**
`license_status = 'بغیر لائسنس'`

**Expected Answer**
`30` (2 weapons have no `license_status` and must not be counted)

**Difficulty**
Hard

**Expected Failure Risk**
- **Cross-lingual literal failure** — matching English "unlicensed", returning 0,
  and reporting it as a confident answer. This is the highest-value failure in
  the corpus.
- Counting the 2 null-status weapons as unlicensed (answer 32).
- Dropping the filter entirely (answer 32).

---

### QA-005

**User Question**
> How many documents are FIR records?

**Query Category**
Filtered Aggregate

**Expected Reasoning**
Count `Document` nodes with `doc_type = 'fir_structured'`. Here the stored
value *is* English, so this is the control case for QA-004: it isolates
"can it filter at all?" from "can it filter across languages?".

**Expected Entities**
Entity: `Document` · Measure: `COUNT`

**Required Relationships**
None

**Required Filters**
`doc_type = 'fir_structured'`

**Expected Answer**
`73`

**Difficulty**
Easy

**Expected Failure Risk**
- Matching `doc_type = 'fir'` exactly and returning 0 instead of handling the
  `_structured` suffix.
- Substring-matching so loosely that other `*_structured` types are swept in
  (would give 124).

---

### QA-006

**User Question**
> How many female persons are in the records?

**Query Category**
Filtered Aggregate + Sparse Attribute

**Expected Reasoning**
Count `Person` where `gender` indicates female — stored as `عورت`. Critically,
**`gender` is null for 311 of 430 persons**, so the answer covers only the 119
persons where gender is known. A trustworthy answer states that coverage
limit rather than implying 34 is the total number of women.

**Expected Entities**
Entity: `Person` · Measure: `COUNT`

**Required Relationships**
None

**Required Filters**
`gender = 'عورت'`

**Expected Answer**
`34` — with the caveat that 311 persons have no recorded gender.

**Difficulty**
Hard

**Expected Failure Risk**
- Cross-lingual literal failure (as QA-004) → 0.
- **Silent sparsity** — reporting 34 with no mention that 72% of persons have
  no gender recorded. An investigator could read 34 as "there are 34 women".
- Treating null as "not female" without saying so.

---

### QA-007

**User Question**
> How many distinct persons appear across all cases?

**Query Category**
Distinct Entity Query + Entity Resolution

**Expected Reasoning**
This is the deduplication probe. 430 Person nodes exist, but 222 carry
`merged_into`, meaning they were resolved into other records; 208 are live
entities. "Distinct persons" most defensibly means resolved entities → 208.
The system must choose a reading and disclose it.

**Expected Entities**
Entity: `Person` · Measure: `COUNT DISTINCT`

**Required Relationships**
Optionally `Person -[:BELONGS_TO_CASE]-> Case`

**Required Filters**
`merged_into IS NULL` for the resolved reading

**Expected Answer**
`208` (resolved) — **accept `430` only if the response explicitly says it is
counting raw records including merged duplicates.** An undisclosed 430 is a
failure.

**Difficulty**
Hard

**Expected Failure Risk**
- Returning 430 as if it were a distinct-person count — an inflated headcount
  in an investigative context.
- Applying `COUNT DISTINCT` on node identity (still 430) and believing the
  `DISTINCT` keyword solved deduplication, when the duplication is semantic.

---

### QA-008

**User Question**
> How many distinct CNIC numbers are on record, and how many persons have one?

**Query Category**
Distinct Entity Query — COUNT vs COUNT DISTINCT

**Expected Reasoning**
Two measures over the same filtered set: `COUNT(person)` where `cnic` is not
null versus `COUNT(DISTINCT cnic)`. They differ (196 vs 192), and the gap is
the point — the same national ID appears on more than one person record.

**Expected Entities**
Entity: `Person` · Measures: `COUNT`, `COUNT DISTINCT cnic`

**Required Relationships**
None

**Required Filters**
`cnic IS NOT NULL`

**Expected Answer**
196 persons hold a CNIC; 192 distinct CNIC values.

**Difficulty**
Medium

**Expected Failure Risk**
- Collapsing both measures into one number, losing the discrepancy.
- Counting all 430 persons in the first measure by ignoring the null filter.
- Not surfacing that 234 persons have no CNIC at all.

---

### QA-009

**User Question**
> How many different districts have cases filed in them?

**Query Category**
Distinct Entity Query + Traversal

**Expected Reasoning**
Districts are not attached to cases directly. Traverse
`Case -[:FILED_AT]-> PoliceStation -[:PART_OF]-> District` and count distinct
districts reached. Tests distinct-counting at the *far* end of a two-hop path.

**Expected Entities**
Entity: `District` · Measure: `COUNT DISTINCT`

**Required Relationships**
`Case → PoliceStation → District`

**Required Filters**
None

**Expected Answer**
`9` (all districts have at least one case)

**Difficulty**
Medium

**Expected Failure Risk**
- Counting `District` nodes directly (also 9) without traversing — right answer,
  wrong reasoning. Inspect the plan.
- Counting path instances (73) rather than distinct districts.
- Stopping at PoliceStation (19).

---

### QA-010

**User Question**
> How many cases have a weapon linked to them?

**Query Category**
Relationship-Based (single hop)

**Expected Reasoning**
Traverse `Weapon -[:BELONGS_TO_CASE]-> Case` and count **distinct cases**. 32
weapons map to 32 cases here, but the distinct-ness must be explicit in the
plan, since nothing guarantees one weapon per case.

**Expected Entities**
Entity: `Case` · Measure: `COUNT DISTINCT`

**Required Relationships**
`Weapon → Case` (`BELONGS_TO_CASE`)

**Required Filters**
None

**Expected Answer**
`32` distinct cases (of 73)

**Difficulty**
Medium

**Expected Failure Risk**
- Returning the weapon count instead of the case count — indistinguishable by
  value here (both 32), so the plan must be inspected.
- Using `APPEARS_IN` (Weapon→Document) and reporting documents.

---

### QA-011

**User Question**
> How many officers are assigned to cases, and how many cases have an officer assigned?

**Query Category**
Relationship-Based + Dual Measure

**Expected Reasoning**
One edge type, two distinct counts from opposite ends:
`Officer -[:ASSIGNED_TO]-> Case`. 144 edges, 76 distinct officers, 73 distinct
cases. Tests whether the system distinguishes edge cardinality from endpoint
cardinality.

**Expected Entities**
Entities: `Officer`, `Case` · Measures: `COUNT DISTINCT` on each side

**Required Relationships**
`Officer → Case` (`ASSIGNED_TO`)

**Required Filters**
None

**Expected Answer**
76 distinct officers; 73 distinct cases; 144 assignment edges.

**Difficulty**
Medium

**Expected Failure Risk**
- Reporting 144 for both — the classic edge-vs-node conflation.
- Counting all 1155 Officer nodes, ignoring the assignment requirement.

---

### QA-012

**User Question**
> How many persons are connected to cases that cite another case?

**Query Category**
Relationship-Based (multi-hop)

**Expected Reasoning**
Three hops: find cases with an outgoing `CITES`, then the persons connected to
them via `BELONGS_TO_CASE`. Only 9 `CITES` edges exist, so the result is
small — which makes a wrong traversal easy to spot. Direction matters: "cases
that cite" is the source side of `CITES`.

**Expected Entities**
Entity: `Person` · Measure: `COUNT DISTINCT`

**Required Relationships**
`Case -[:CITES]-> Case`; `Person -[:BELONGS_TO_CASE]-> Case`

**Required Filters**
None

**Expected Answer**
Nonzero and small (9 citing cases, each with a handful of linked persons).
Grade on traversal correctness — the citing side, not the cited side — rather
than an exact figure.

**Difficulty**
Hard

**Expected Failure Risk**
- Reversing `CITES` direction (citing vs cited cases) — a silently different
  answer.
- Losing the `DISTINCT` and multiplying persons by citation paths.
- Failing on the 2-relationship join and falling back to counting all persons.

---

### QA-013

**User Question**
> How many people have been accused in incidents?

**Query Category**
Role / Semantic

**Expected Reasoning**
"Accused" is a specific role, stored as `role='accused'` on the
`Person -[:INVOLVED_IN]-> Incident` edge. The system must use the role filter
and not fall back to generic involvement. Count distinct persons, not edges.

**Expected Entities**
Entity: `Person` · Measure: `COUNT DISTINCT`

**Required Relationships**
`Person -[:INVOLVED_IN {role:'accused'}]-> Incident`

**Required Filters**
`role = 'accused'`

**Expected Answer**
`92` distinct persons (94 edges — two persons are accused in more than one
incident)

**Difficulty**
Medium

**Expected Failure Risk**
- **Dropping the role filter** and counting all involvement (221 edges / ~all
  involved persons). In an investigative product, reporting witnesses and
  complainants as accused is a serious error.
- Returning 94 (edges) instead of 92 (persons).

---

### QA-014

**User Question**
> How many witnesses are there, and how many victims?

**Query Category**
Role / Semantic — Role Discrimination

**Expected Reasoning**
Two different role values in one question, over the same edge. Requires
separating `witness` from `victim` rather than merging them into a single
"non-accused" bucket.

**Expected Entities**
Entity: `Person` · Measure: `COUNT DISTINCT`, grouped by role

**Required Relationships**
`Person -[:INVOLVED_IN]-> Incident`

**Required Filters**
`role IN ('witness','victim')`, reported separately

**Expected Answer**
37 witnesses; 9 victims.

**Difficulty**
Medium

**Expected Failure Risk**
- Returning a single combined 46.
- Answering only the first clause and dropping "and how many victims".
- Conflating `victim` with `complainant` (73) — distinct roles in this schema.

---

### QA-015

**User Question**
> How many accused persons are still under investigation rather than arrested?

**Query Category**
Role / Semantic + Edge-Property Filter + Sparsity

**Expected Reasoning**
Filter the `INVOLVED_IN` edge on both `role='accused'` and `arrest_status`.
The under-investigation value is `زیر تفتیش` (73 edges); arrested is `گرفتار`
(11). `arrest_status` is **null on 127 of 221 edges**, and its 12 values are
Urdu free text, not a clean enum — several are compound phrases like
`گرفتار، بعد ازاں سزا یافتہ` ("arrested, later convicted"). A good answer
gives the count and flags that the status vocabulary is unnormalized.

**Expected Entities**
Entity: `Person` · Measure: `COUNT DISTINCT`

**Required Relationships**
`Person -[:INVOLVED_IN]-> Incident`

**Required Filters**
`role = 'accused'` AND `arrest_status = 'زیر تفتیش'`

**Expected Answer**
`73` edges carry `زیر تفتیش`. Accept a distinct-person count in that
neighbourhood; require that both filters were applied.

**Difficulty**
Hard

**Expected Failure Risk**
- Cross-lingual literal failure → 0.
- Treating compound statuses as plain "arrested" and misclassifying.
- Counting the 127 null-status edges as "not arrested" — that would report
  people as under investigation with no evidence for it.
- Applying `role` but dropping `arrest_status`, or the reverse.

---

### QA-016

**User Question**
> How many incidents occurred in 2026?

**Query Category**
Temporal — Year Filter

**Expected Reasoning**
Filter `Incident.incident_datetime` to the 2026 calendar year. The field is an
ISO-8601 string with a `Z` suffix, so range comparison must handle string or
timestamp semantics consistently. **`incident_datetime` is null on 9 of 73
incidents**, which must not be silently bucketed into 2026.

**Expected Entities**
Entity: `Incident` · Measure: `COUNT`

**Required Relationships**
None

**Required Filters**
`incident_datetime >= '2026-01-01' AND < '2027-01-01'`

**Expected Answer**
`51`

**Difficulty**
Medium

**Expected Failure Risk**
- Using `report_datetime` (present on all 73) instead of `incident_datetime` —
  when the incident happened is not when it was reported.
- Counting nulls into the year.
- Off-by-one boundary handling at 2026-01-01 / 2026-12-31.

---

### QA-017

**User Question**
> Were there more incidents in 2025 or in 2024?

**Query Category**
Temporal — Comparison + Empty Bucket

**Expected Reasoning**
A genuine empty-result probe dressed as a comparison. **2025 has zero
incidents**; pre-2025 has 13. The system must report 2025 = 0 as a real
finding rather than erroring, hallucinating, or quietly widening the range
until it finds rows.

**Expected Entities**
Entity: `Incident` · Measure: `COUNT` grouped by year

**Required Relationships**
None

**Required Filters**
`incident_datetime` within 2025 vs within 2024

**Expected Answer**
2025: `0`. 2024: `13`. 2024 is higher.

**Difficulty**
Hard

**Expected Failure Risk**
- **Treating the empty 2025 bucket as an error** or as "no data available"
  instead of the substantive answer zero.
- Silently relaxing the filter to produce a nonzero comparison.
- Omitting the 2025 row entirely, so the reader cannot see the gap.

---

### QA-018

**User Question**
> What is the date of the earliest recorded incident?

**Query Category**
Temporal — MIN Aggregate

**Expected Reasoning**
`MIN(incident_datetime)` over `Incident`. Tests a non-COUNT aggregate — a
different code path from every other query in this corpus.

**Expected Entities**
Entity: `Incident` · Measure: `MIN(incident_datetime)`

**Required Relationships**
None

**Required Filters**
`incident_datetime IS NOT NULL`

**Expected Answer**
`2024-09-14T22:00:00Z`

**Difficulty**
Medium

**Expected Failure Risk**
- Only supporting COUNT and refusing or misrouting MIN/MAX.
- Returning a null as the minimum because nulls sort first under string
  comparison.
- Returning the earliest `Date` node or `report_datetime` instead.

---

### QA-019

**User Question**
> How many accused persons own a weapon?

**Query Category**
Multi-Constraint (role + ownership)

**Expected Reasoning**
Intersect two relationship constraints on the same person:
`Person -[:OWNS]-> Weapon` **and**
`Person -[:INVOLVED_IN {role:'accused'}]-> Incident`. Both must hold for the
same node — a join, not a union.

**Expected Entities**
Entity: `Person` · Measure: `COUNT DISTINCT`

**Required Relationships**
`Person → Weapon` (`OWNS`); `Person → Incident` (`INVOLVED_IN`)

**Required Filters**
`role = 'accused'`

**Expected Answer**
`29`

**Difficulty**
Hard

**Expected Failure Risk**
- Unioning the two sets instead of intersecting (would give ~92).
- Dropping the role constraint and returning all 29–30 weapon owners without
  checking they are accused — right number here, wrong logic, and wrong the
  moment the data changes.
- Double-counting the one person who owns two weapons.

---

### QA-020

**User Question**
> How many unique persons are linked to cases filed in Faisalabad?

**Query Category**
Multi-Constraint (geography + traversal + distinct)

**Expected Reasoning**
Four-part chain: resolve "Faisalabad" to the Urdu district name `فیصل آباد`,
traverse `District <-[:PART_OF]- PoliceStation <-[:FILED_AT]- Case`, then
collect persons via `BELONGS_TO_CASE`, then deduplicate. Faisalabad is the
largest district by case count (19 of 73).

**Expected Entities**
Entity: `Person` · Measure: `COUNT DISTINCT`

**Required Relationships**
`Person → Case → PoliceStation → District`

**Required Filters**
`District.name = 'فیصل آباد'`

**Expected Answer**
Nonzero, derived from the 19 Faisalabad cases. Grade on: correct district
resolution, full three-hop traversal, and deduplication.

**Difficulty**
Hard

**Expected Failure Risk**
- **Transliteration failure** — "Faisalabad" never matched to `فیصل آباد`,
  yielding 0. Note four Faisalabad police stations exist, so partial
  station-name matching is a tempting wrong path.
- Truncating the traversal at Case or PoliceStation.
- Losing `DISTINCT` and inflating by path count.

---

### QA-021

**User Question**
> How many unicorn weapons were seized?

**Query Category**
Adversarial — Unknown Entity / Nonexistent Value

**Expected Reasoning**
No weapon category resembling "unicorn" exists; every weapon is `30 بور`. The
correct behaviour is to say no such category is present in the data — not to
invent one, and not to silently answer 0 as though "unicorn weapons" were a
valid recognized category that happens to be empty. The distinction matters:
0 implies the category exists.

**Expected Entities**
Entity: `Weapon` · Measure: none — should not execute a filter it cannot ground

**Required Relationships**
None

**Required Filters**
None — the requested filter is ungroundable

**Expected Answer**
A refusal or a clarification: no such weapon type exists in the data; the
recorded weapons are 30-bore firearms.

**Difficulty**
Adversarial

**Expected Failure Risk**
- **Answering a bare "0"** with no indication the category is unrecognized.
- Hallucinating a nonzero count.
- Fuzzy-matching "unicorn" onto an unrelated real value and answering 30.

---

### QA-022

**How many persons have a criminal risk score above 80?**

**Query Category**
Adversarial — Nonexistent Attribute

**Expected Reasoning**
No `criminal_risk_score` (or any scoring) property exists on `Person`. The
available numeric properties are `age`, `confidence`, and
`extraction_confidence`. The system must refuse on the grounds that the
attribute does not exist. The trap: `confidence` is numeric and present on all
430 persons, making it an inviting but completely wrong substitute — it is
extraction confidence, not criminality.

**Expected Entities**
Entity: `Person` · Measure: none — should refuse

**Required Relationships**
None

**Required Filters**
None — `criminal_risk_score` does not exist

**Expected Answer**
Refusal: no such attribute is recorded. Optionally list what *is* available.

**Difficulty**
Adversarial

**Expected Failure Risk**
- **Substituting `confidence` or `extraction_confidence`** and returning a
  confident number. This is the worst possible outcome: a plausible-looking
  criminal risk statistic fabricated from a data-quality field.
- Returning 0 as if the attribute existed but no one qualified.
- Silently dropping the filter and returning 430.

---

### QA-023

**User Question**
> How many people are connected to cases?

**Query Category**
Adversarial — Ambiguous Wording

**Expected Reasoning**
Deliberately underspecified on three axes: (a) "people" — `Person` only, or
`Officer` too? (b) "connected" — `BELONGS_TO_CASE`, `INVOLVED_IN` via
Incident, `APPEARS_IN` a case document, or any path? (c) deduplicated or raw,
given 222 merged Person records. Defensible answers span roughly 208 to well
over 1000. The right behaviour is to state the interpretation chosen, or ask.

**Expected Entities**
Entity: `Person` (and possibly `Officer`) · Measure: `COUNT DISTINCT`

**Required Relationships**
Ambiguous by design

**Required Filters**
None

**Expected Answer**
Any well-formed count **accompanied by an explicit statement of the reading**,
or a clarifying question. An unqualified bare number is the failure, whatever
its value.

**Difficulty**
Adversarial

**Expected Failure Risk**
- Picking one reading silently and presenting it as *the* answer.
- Including Officer nodes under "people" with no mention — Officer outnumbers
  Person 1155 to 430, so this swings the result enormously.
- Ignoring the merged-duplicate problem.

---

### QA-024

**User Question**
> How many licensed weapons are in evidence?

**Query Category**
Adversarial — Legitimate Filter, Genuinely Empty Result

**Expected Reasoning**
The inverse of QA-004, and the subtlest case in the corpus. The filter is
entirely valid — `license_status` exists and is a real field — but **every
recorded weapon is unlicensed**, so the true answer is 0. This distinguishes
"0 because the category is empty" (correct here) from "0 because I failed to
match the literal" (the QA-004 failure mode). A system that fails QA-004 will
produce the *right* answer here for the *wrong* reason.

**Expected Entities**
Entity: `Weapon` · Measure: `COUNT`

**Required Relationships**
None

**Required Filters**
`license_status` indicating licensed — no such value present

**Expected Answer**
`0`, ideally noting that all 30 weapons with a recorded status are unlicensed
and 2 have no status recorded.

**Difficulty**
Adversarial

**Expected Failure Risk**
- Right answer, wrong reasoning — grade this **only together with QA-004**.
  Passing QA-024 while failing QA-004 indicates the literal matching is broken
  and this 0 is a coincidence.
- Reporting "no data" instead of a substantive zero.
- Counting the 2 null-status weapons as licensed.

---

### QA-025

**User Question**
> For cases involving a 30-bore weapon, how many distinct accused persons are
> named, and which districts are those cases filed in?

**Query Category**
Complex Investigation — multi-hop, two measures, cross-entity

**Expected Reasoning**
A realistic investigator question requiring a full chain:
`Weapon {caliber_or_bore:'30 بور'} -[:BELONGS_TO_CASE]-> Case`, then from
those cases reach accused persons via
`Case <-[:PART_OF]- Incident <-[:INVOLVED_IN {role:'accused'}]- Person` (roles
attach to Incident, **not** to Case — the single most common structural
mistake available here), and separately
`Case -[:FILED_AT]-> PoliceStation -[:PART_OF]-> District` for the geography.
Two different measures over one filtered case set. Note `30 بور` is again an
Urdu literal, and it is the only caliber present — so the caliber filter is
effectively a no-op on the data, and a system that drops it will still look
correct on the numbers.

**Expected Entities**
Entities: `Weapon`, `Case`, `Incident`, `Person`, `District` ·
Measures: `COUNT DISTINCT Person`, `COUNT DISTINCT District`

**Required Relationships**
`Weapon → Case`; `Incident → Case`; `Person → Incident`; `Case → PoliceStation → District`

**Required Filters**
`caliber_or_bore = '30 بور'`; `role = 'accused'`

**Expected Answer**
Scoped to the 32 weapon-linked cases. Grade on structure: correct Urdu
caliber literal, role applied at the Incident edge, deduplicated person count,
and districts resolved two hops out from Case.

**Difficulty**
Hard

**Expected Failure Risk**
- **Attaching `role` to a Person→Case edge**, which does not exist — either an
  error or a silent fallback to unfiltered involvement.
- Answering only one of the two sub-questions.
- Dropping the caliber filter (numerically invisible here — inspect the plan).
- Cross-lingual failure on `30 بور` → 0.
- Losing `DISTINCT` on persons across multiple weapon-case paths.

---

## 4. Consolidated Failure Modes

Ranked by investigative severity:

1. **Fabricated attributes** (QA-022) — substituting `confidence` for a
   nonexistent risk score produces an authoritative-sounding statistic with no
   basis in the data. Worst possible outcome in a policing context.
2. **Cross-lingual literal failure** (QA-004, QA-006, QA-015, QA-020, QA-025) —
   the data is Urdu, the questions are English. This fails *silently and
   confidently* as a zero or an empty set. Affects 5 of 25 queries.
3. **Dropped role filters** (QA-013, QA-015, QA-019, QA-025) — reporting
   witnesses or complainants as accused.
4. **Unresolved duplicates** (QA-007, QA-023) — 222 of 430 persons are merged
   records; ignoring this inflates headcounts by up to 2×.
5. **Edge/node conflation** (QA-002, QA-010, QA-011, QA-013) — counting
   relationships where entities were asked for.
6. **Silent sparsity** (QA-006, QA-015, QA-016) — `gender` null on 72% of
   persons, `arrest_status` null on 57% of involvements, `designation` null on
   93% of officers. Answers that omit coverage are misleading even when
   arithmetically correct.
7. **Empty results treated as errors** (QA-017, QA-024) — zero is often the
   correct, substantive answer.
8. **Unqualified answers to ambiguous questions** (QA-023).
9. **Wrong temporal field** (QA-016, QA-018) — `report_datetime` vs
   `incident_datetime`.
10. **Right answer, wrong reasoning** (QA-001, QA-009, QA-010, QA-019, QA-024,
    QA-025) — six queries where a plausible wrong plan yields the correct
    number on this snapshot. These must be graded on the emitted plan, not the
    output value.

---

## 5. Grading Notes

- **Grade the plan, not just the number.** Ten of 25 queries can return the
  correct value via incorrect reasoning on this particular snapshot.
- **QA-004 and QA-024 must be graded as a pair.** Passing QA-024 alone proves
  nothing.
- **Non-refusal is a failure on QA-021 and QA-022.** A bare `0` is not a pass.
- **Bare numbers fail QA-023** regardless of value.
- Counts are true as of 2026-09-17 against `evidence_graph`. Re-derive them if
  the graph is rebuilt; the reasoning requirements are snapshot-independent.
