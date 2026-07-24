# Synthetic Dataset Plan — Muhafiz Evidence Intelligence Platform

A standalone, actionable plan for the dev/eval dataset that stands in for real police data until customer handoff. Companion to [`EVIDENCE_INTELLIGENCE_PLATFORM_ARCHITECTURE.md`](EVIDENCE_INTELLIGENCE_PLATFORM_ARCHITECTURE.md) — that report made the graph/no-graph, Case-centric, and CNIC-first-resolution calls; this plan produces the data those calls get benchmarked against, restructured around the same model.

**Locked decisions this plan is built on** (confirmed before writing, this revision):

| Decision | Answer |
|---|---|
| Build strategy | **Extend** the existing 40-document corpus in `data/memory/` — don't discard it. It becomes 14 **retrofitted Cases** (see §1.1) rather than a case-less legacy slice. |
| Language mix | **Urdu-dominant.** New documents are Urdu-first; English appears only incidentally (legal section numbers, loanwords, a few institutional terms) except for a small, deliberate Roman-Urdu stress slice. |
| Urdu quality review | You can read/review Urdu directly — human spot-check is a real option, not a blocker requiring LLM-as-judge as the primary gate. |
| **Case volume** *(this revision)* | **20 new Cases** for Batch 1 + **14 retrofitted Cases** from the existing Batch 0 FIRs = **34 total Cases**. Keeps the corpus size roughly where it was (~95-100 documents) while making the whole thing case-testable, not just the new slice. |
| **Case-shape distribution** *(this revision)* | ~40% minimal (FIR + 1 statement), ~40% developing (FIR + 2-3 statements + case diary, sometimes a recovery memo), ~20% complex/full-lifecycle (FIR + complaint + multiple statements + case diary + charge sheet) |
| **Investigation Status values** *(this revision)* | Under Investigation / Charge-Sheeted (sent to court) / Closed – Convicted / Closed – Untraced / Pending Trial, weighted so status roughly tracks case-shape maturity |
| **CNIC coverage target** *(this revision)* | ~60% of Person entities have a CNIC on at least one document they appear in (formal documents); ~40% never have a CNIC anywhere (informal-only). All 3 confusable pairs get a CNIC-present variant; at least 1 pair is *also* tested in a CNIC-absent context |

**Design principle that shapes every section below:** the corpus and its ground-truth artifacts (Case index, entity roster, eval query set) are **architecture-agnostic** on model choice — they don't assume a specific embedding model or LLM, those stay a real A/B — but they now *do* assume the Case-centric, CNIC-first structure the architecture report locked in. That's a deliberate change from the previous version of this plan, which was written before that structure existed. See §9 for the explicit reconciliation.

---

## 1. Case and document corpus design

### 1.1 Cases are the top-level unit — including the existing corpus

Per the architecture's §3.1, every document belongs to exactly one **Case**, generated first, with this field set:

| Field | Required? | Notes |
|---|---|---|
| FIR Number | Required (except Missing Person cases pending registration) | Human-facing identifier; not globally unique across stations, so not the primary key |
| Case ID | Required | System-generated primary key — what every document's `case_id` FK actually points to |
| Crime Category | Required | Drives which `offense_sections.csv` row(s) apply |
| Investigation Officer | Required | Also the ABAC case-assignment anchor (architecture §10) |
| Police Station | Required | |
| Incident Date | Required | Distinct from document dates — every attached document's date must be consistent with it (§1.2's new Tier-1 rule) |
| Investigation Status | Required | One of the 5 values in the locked-decisions table above |
| Location | Optional | Free text |
| Initial Case Description | Optional | Short free text |
| Victim Information | Optional | Structured sub-fields where known; becomes a `Person` entity in the roster, not a separate schema |
| Suspect Information | Optional | Same treatment |

**The existing 40-document corpus gets retrofitted, not left behind.** Its 14 existing FIRs (`FIR-2026-THEFT-001`, `FIR-2026-CYBER-001`, etc.) already have most of this data in `manifest.json` (`police_station`, `date_registered`, `category`, `sections`) — a retrofit pass generates the missing fields (Case ID, Investigation Officer, Investigation Status) deterministically and writes 14 `Case` rows into `case_index.csv`, so the older third of the corpus is fully usable for case-scoped retrieval testing, not excluded from it. This was one of the two options put to you before finalizing volumes; retrofitting was the confirmed choice specifically so the corpus doesn't end up with a case-aware two-thirds and a case-blind one-third.

**20 new Cases for Batch 1**, distributed across categories (including the dry-run's `CASE-DRY-001`, already built in Step 1):

| Category | New Cases | Notes |
|---|---|---|
| Illegal Weapon Possession | 3 (incl. `CASE-DRY-001`) | |
| Cyber Fraud/Online Scam | 3 | Carries the cyber-fraud-ring cross-case entity storyline (§2) |
| Burglary/House Theft | 3 | Carries the burglary-ring cross-case entity storyline (§2) |
| Mobile/Vehicle Theft | 3 | Carries the independent repeat-offender storyline (§2) |
| Domestic Dispute | 2 | Carries a repeat-offender storyline |
| Cheating/Financial Fraud | 2 | Carries a repeat-offender storyline |
| Harassment/Cyber Harassment | 2 | Carries a recurring-phone-number storyline |
| Road Traffic Accident | 1 | Carries a near-miss vehicle-plate pair |
| **Missing Person** *(new category)* | 1 | Gets its own Case record like any other investigation — the old plan treated Missing Person Reports as a standalone, case-less document type; this revision folds them in, since a missing-person investigation is a real Case by any reasonable reading of the SOW's definition |
| **Total** | **20** | |

### 1.2 Every document is internally consistent with its Case — a new Tier-1 rule

Adding a document to a Case isn't just setting a `case_id` field. Every document attached to a Case must agree with that Case's metadata: the FIR number referenced in a witness statement's structured fields must match the Case's FIR Number exactly; the station named must match; any date on the document must be on or after the Case's Incident Date (a witness statement can't be dated before the incident it's a statement about). **This consistency check is now a Tier-1 structural validation rule** (§4.3), not a nice-to-have — a document that references the wrong FIR number for its own case is exactly the kind of generation bug the dry run was built to catch, now formalized as an automated gate rather than something caught by eye.

### 1.3 Case shapes — realistic document accumulation, not a flat count

A case isn't "one FIR" — it accumulates evidence unevenly over its lifecycle. Three shapes, confirmed against your read of realistic case-resolution rates:

| Shape | Share of new Cases | Document composition | Typical Investigation Status |
|---|---|---|---|
| **Minimal** | ~40% (8 cases) | FIR + 1 witness statement | Under Investigation |
| **Developing** | ~40% (8 cases) | FIR + 2-3 witness statements + 1 case diary entry set, sometimes + 1 recovery memo (weapon/burglary/theft categories) | Under Investigation, or Charge-Sheeted if furthest along |
| **Complex / full lifecycle** | ~20% (4 cases) | FIR + complaint application + 2-3 witness statements + 1-2 case diary entry sets + 1 charge sheet | Charge-Sheeted, Closed – Convicted, Closed – Untraced, or Pending Trial |

Recomputing document counts from this (replacing the old plan's flat per-type table, which was written before the Case-shape design existed):

| Shape | Cases | Approx. docs/case | Subtotal |
|---|---|---|---|
| Minimal | 8 | 2 | 16 |
| Developing | 8 | ~5 (varies: some get a recovery memo, some don't) | ~40 |
| Complex | 4 | ~7 | ~28 |
| **New case-narrative documents** | **20** | | **≈84** |

Plus two document types that are **deliberately not case-scoped** (§1.5) and a new structured-records evidence type (§1.6):

| Type | Count | Case-scoped? |
|---|---|---|
| General Daily Diary (Roznamcha) pages | 10 pages (~60 entries) | No — station-level log, see §1.5 |
| Internal Circular | 4 | No — station/department-level, see §1.5 |
| Structured record (case-management export) | ~15 rows | Yes, but not narrative-generated — see §1.6 |

**New total: ≈84 case-narrative documents + 10 diary pages + 4 circulars + 40 existing = ≈138 documents**, plus the structured-records CSV as a separate, small artifact. This is close to the previous plan's ~136-document total — the case-shape recomputation redistributes *how* documents accumulate rather than inflating the overall scope. It stays realistic for a solo builder to generate and validate: the case-shape design actually *reduces* the number of independent narrative-generation calls needed for FIR-adjacent documents versus generating 22 flat, disconnected FIRs, since developing/complex cases reuse the same case context across their 2-7 documents rather than each document starting from scratch.

### 1.4 Investigation Status distribution

| Status | Approx. share | Tied to shape |
|---|---|---|
| Under Investigation | ~45% | All minimal cases, most developing cases |
| Charge-Sheeted (sent to court) | ~20% | Furthest-along developing cases, some complex cases |
| Pending Trial | ~10% | Complex cases post-charge-sheet |
| Closed – Convicted | ~10% | Complex cases |
| Closed – Untraced | ~15% | A mix across shapes — including at least one deliberately stalled minimal case and one stalled complex case, matching the existing Batch-0 corpus's own pattern (`FIR-2026-THEFT-001` is already deliberately left untraced) |

This gives the eval set (§3) a real, non-trivial basis for status-aware queries — "open cases involving X" needs some X to appear in both open and closed cases, or the query is trivially easy.

### 1.5 Documents that are deliberately *not* case-scoped

Daily Diary pages and Internal Circulars are station/department-level records, not evidence *for* a specific case — a diary page logs a duty officer's whole shift, most of which has nothing to do with any investigation. Forcing a `case_id` onto them would be architecturally dishonest. Instead:

- They carry **no `case_id`** in the manifest and are excluded from case-scoped retrieval by construction.
- A minority of diary entries (1-2 per page, ~15-20 entries total across 10 pages) *reference* an active Case in passing (e.g., "patrol confirmed address in FIR-2026-BUR-005 vicinity, nothing unusual noted") — these are deliberately **not** linked via `case_id`, testing whether the retrieval system can still surface a loosely-relevant, non-case-scoped document through general/cross-case search when a case-scoped query alone wouldn't find it. This is an intentional, realistic edge case for the case-scoped-by-default design, not an oversight.

### 1.6 Structured records — the new evidence type

The architecture's §3.2 splits evidence into documents and structured records; this plan previously only had the former. A **structured record** is data arriving as rows/fields, not narrative text — modeling a case-management-system export or a reference-table entry:

- **What it is**: `data/memory/structured_records/case_management_export.csv` — one row per record, columns matching a plausible external case-tracking system export (`case_id, fir_number, record_type, field_1, field_2, ..., source_system, export_date`). Record types: property-tag entries (item, tag number, storage location — a lighter-weight cousin of the narrative Recovery Memo), and court-listing entries (case_id, court, hearing_date, status) for the complex-shape cases that reach Pending Trial.
- **Scope, deliberately kept small**: ~15 rows total, each tied to a `case_id`. This is a CSV/table-shaped artifact, not a new narrative-generation pipeline — no LLM narrative text, no noise-rendering tier, no OCR test surface. It exercises the SQL-route ingestion pattern (architecture §3.2) with a second, distinct source table, not a second document-generation effort.
- **Validation**: Tier-1 structural validation (§4.3) extends to check every structured-record row's `case_id` resolves to a real Case and its `fir_number` matches that Case's FIR Number — the same consistency rule as §1.2, applied to a non-narrative evidence type.

### 1.7 Document types and internal structure (unchanged from the prior version, now case-attached)

The document-type list itself doesn't change — FIRs, witness statements, daily diary entries, complaint applications, case diaries, charge sheets, recovery memos, missing-person reports, traffic accident reports, internal circulars remain the full document-evidence side, per your confirmation this list stays as-is. What changes is that each narrative document type (except the two in §1.5) now carries a `case_id` and must satisfy §1.2's consistency rule, and Missing Person / Traffic Accident reports are now generated as part of a Case's document set (§1.1) rather than a disconnected standalone bucket.

| Type | Fields |
|---|---|
| Witness Statement | Case ID, FIR reference, witness name, father's name, CNIC (present or absent per §2.3's roster design), address, phone, date/time recorded, recording officer, first-person narrative, thumb-impression/signature note |
| Daily Diary page | Station, date, duty officer; 5-8 short numbered entries per page — DD entry no., time, terse entry text. No `case_id` (§1.5) |
| Recovery Memo | Case ID, FIR reference, item(s) recovered, recovery location, recovered from (person), 2 witnesses to recovery (*mashir*), date/time, officer |
| Internal Circular | Circular number, subject, issuing authority/station, date, body. No `case_id` (§1.5) |

### 1.8 Noise injection plan — unchanged in method, explicitly document-evidence only

The single biggest realism gap in the existing corpus: every document is clean-typed. Real documents are typed *and* scanned *and* occasionally handwritten, and the noise is exactly what the OCR pipeline in the architecture report has to survive.

> **Scope clarity, per this revision:** everything in this section is about *document images* — scanned or photographed paper — not "media evidence" in the SOW's broader sense (audio, video, CCTV). The architecture report explicitly restricts evidence types to documents and structured records for now (§3.2) and defers media entirely. A scanned witness statement rendered as a noisy image is still a **document**, evaluated on OCR text-extraction accuracy; it is not the media-evidence type this plan and the architecture both leave out of scope. This distinction was already true in practice in the prior version of this plan — it's stated explicitly here so the two documents don't drift out of sync on it.

| Rendering | Share | Method |
|---|---|---|
| Clean typed (born-digital PDF, selectable text) | ~65% | Reportlab + Naskh-family Urdu font + `arabic_reshaper`/`python-bidi` for correct shaping — validated working in Step 1; Nastaliq specifically does **not** render correctly through this path (a real, tested finding — reportlab has no OpenType GSUB shaping engine) |
| Scanned (image-only PDF, no text layer) | ~28% | Naskh-family font rasterized via HarfBuzz + FreeType shaping, then a curated Augraphy pipeline (paper texture, brightness/gamma shift, ink bleed, JPEG compression) — **not** Augraphy's `default_augraphy_pipeline()`, which was tested in Step 1 and rejected: it's tuned for a messier document genre (ruled notebook backgrounds, highlighter marks, `BleedThrough`-simulated ghost text from an unrelated source image) that corrupts ground-truth pairing outright rather than just degrading legibility |
| Handwritten-style | ~7% | Nastaliq font, properly shaped via HarfBuzz + FreeType rasterization (validated in Step 1 — this is real, correct Nastaliq shaping, not reportlab's broken attempt), then a stronger variant of the same curated Augraphy pipeline. Drawn only from witness statements and daily diary entries — the two types where real handwriting actually shows up |

> **OCR stays in scope — cross-reference to the architecture's flag.** The architecture report keeps OCR/Nastaliq handling in scope now despite the client SOW framing it as a later-stage (Phase 3, tentative) capability, and flags that explicitly as a deviation worth a client conversation (architecture §4.1, §15.3). This noise-rendering design is built in anticipation of that staying true — if the client conversation resolves the other way and OCR gets pushed to a later phase, this section (and the OCR-diff tooling built in Step 1) is what would get deprioritized, not redesigned; nothing here needs to change, just wait.

### 1.9 Volume rationale

≈138 documents + ~15 structured-record rows is a modest increase in document-type richness (Missing Person/Traffic Accident now case-attached, structured records added) at essentially the same total scope as the previous plan — not scope creep. It's still sized for a solo builder to generate and validate: 34 total Cases is enough to get real cross-case entity recurrence (§2) and realistic status/shape distributions without hand-authoring anywhere near real-department case volumes. If benchmarks later show the numbers too noisy to trust, scaling up is the same "parameter change, not a redesign" promise as before — the Case-first generation order (§8) means adding more Cases doesn't touch the schema.

---

## 2. Entity and relationship design (for cross-case graph testing)

This is the part that most needed rework for this revision. The prior version's design already had the right entity *types* (recurring people, vehicles, phones, addresses, confusable pairs, name variants) — what it didn't have was Cases to place them across. A recurring entity confined to multiple documents of the *same* case never exercises cross-case pattern detection (SOW Module 7) or the case-scoped-vs-cross-case retrieval split (architecture §8) — it only ever needed within-case context to resolve, which is the easier problem.

### 2.1 CNIC as ground truth, on every Person entity

Every Person entity in the roster now carries a CNIC as ground-truth data — in a **clearly synthetic format**, not the real Pakistani CNIC digit-range structure, to avoid any confusion with real identifiers. The dry run's entities used real-looking province/district-code prefixes (e.g., `61101-...`, `37405-...`, which are real Islamabad-area codes) — **this revision changes the format going forward** to an unambiguously fake block: `00000-XXXXXXX-X`, using a reserved prefix no real CNIC uses, while keeping the same `5-7-1` digit grouping so regex-extraction testing (architecture §4.3) still exercises the real structural pattern. The dry run's four existing entities get their CNIC values regenerated in this format as part of this revision's roster update, not left inconsistent with the new rule.

**Whether a document actually shows that CNIC is a separate, deliberately-varied decision** (§2.3) — the roster records the ground truth for every person regardless; individual documents include or omit it realistically.

### 2.2 The designed cast — now placed across Cases, not just documents

| Cast member type | Count | Design |
|---|---|---|
| Repeat-offender persons | 6 | Each appears as accused/suspect across **2-4 distinct Cases** (different FIR, different Case ID, different station, different Incident Date) — the classic repeat-offender pattern, and now genuinely cross-case rather than cross-document-within-one-case |
| Confusable name pairs (should **not** merge) | 3 pairs (6 people) | Same or near-identical name, different person, **different CNIC**. 1 pair (the Step-1 dry run's, already built) stays **within one Case** — the easier version, where shared case context helps disambiguate; the other 2 pairs are placed in **different, unrelated Cases** — the harder version this revision specifically adds. CNIC is the explicit disambiguating fact per the architecture's CNIC-first design: two mentions, same name, different CNIC → must never auto-merge, full stop, regardless of whether they share a Case |
| Name-variation-but-same-person | 4 people | Each has 3 controlled surface variants, used consistently across their 2-3 appearances. 2 of the 4 appear across documents *within one Case's lifecycle chain* (the Step-1 dry run already validated this pattern); the other 2 appear across **different Cases**, the harder version where there's no shared case context to help disambiguate |
| Recurring vehicles | 4 | Each appears across 2-3 distinct Cases |
| Near-miss vehicle plates (should **not** merge) | 2 pairs | One digit/letter apart, different vehicles, different Cases |
| Recurring phone numbers | 5 | Reused across distinct Cases, weighted toward Cyber Fraud (a real pattern — same scam number, multiple victims, different FIRs, different stations) |
| Recurring addresses | 4 | Mix of "same person, different Case" (expected link) and "different unrelated people, same address" (e.g. a boarding house — shared address that should **not** imply a relationship) |
| Organization / informal group | 2 | Cyber-fraud ring linked via shared phone numbers across 3 Cases at different stations; property-theft ring linked via a shared vehicle + 2 repeat-offender persons across 3 Cases |

Everything else — the long tail of one-off people, most vehicles, most addresses across the 20 new Cases — appears in exactly one Case, as a realistic baseline. The deliberate cast above is what's woven through that long tail; a graph where every entity recurs is as unrealistic as one where nothing does.

### 2.3 CNIC presence/absence — designed variation, not incidental

Per the confirmed target: **~60% of Person entities have a CNIC on at least one document they appear in** (formal documents — FIR, charge sheet, recovery memo, structured records — plausibly capture it); **~40% never have a CNIC anywhere** (informal-only appearances — witness statements, daily diary mentions). This is what actually exercises both resolution paths in the architecture's confidence-tier design (§7.3 there): if every document always carried CNIC, the name-fallback path would never get tested; if none did, the CNIC-tier auto-merge path wouldn't either.

Applied specifically to the hard cases:

| Cast member | CNIC treatment |
|---|---|
| All 3 confusable pairs | **Both members get a CNIC-present variant** — at least one document per person shows their (different) CNIC, so the core "same name, different CNIC, must not merge" test case is directly exercisable via the CNIC-tier rule, not just inferred |
| 1 of the 3 confusable pairs | **Also** gets a CNIC-absent version — re-tested in a context where neither mention shows a CNIC, forcing the harder name-fallback path ("strong name+context match without CNIC → flagged-unverified at most, never auto-merged," per architecture §7.3). Both variants of this one pair exist in the roster, not a replacement of the CNIC-present version |
| Repeat offenders, recurring vehicles/phones/addresses | Follow the general 60/40 split — some always show identifying info, some don't, matching the realistic long-tail pattern rather than being specially engineered either way |

### 2.4 Relationship types (unchanged, now explicitly case-linked)

| Relationship | Real query pattern it supports |
|---|---|
| `BELONGS_TO_CASE` (entity/document → Case) *(new, per architecture §7.1)* | The first-class scoping edge — makes within-case retrieval a single filtered traversal rather than an indirect join |
| `APPEARS_IN` (entity → document) | Base provenance edge for every extraction |
| `ASSOCIATED_WITH` / `CO_ACCUSED_WITH` (person → person) | "Map this person's known associates" |
| `REGISTERED_TO` / `OWNS` (vehicle, phone → person) | "Has this phone number / vehicle appeared in other cases" |
| `RESIDES_AT` (person → address) | "Who else is linked to this address" |
| `MEMBER_OF` (person → organization) | "Timeline of incidents involving this group" |
| `INVOLVED_IN` (person → incident), `PART_OF` (incident → case) | Case-level rollup, repeat-offender-across-stations queries |
| `OCCURRED_ON` (incident → date) | Timeline/temporal reasoning |
| `CONFLICTS_WITH` (statement/incident → statement/incident) *(new, per architecture §7.5)* | Within-case inconsistency flagging — see §3.4 |

### 2.5 Target graph shape (recomputed)

| Metric | Approx. target | Why this number |
|---|---|---|
| Document nodes | ~138 (+ ~15 structured records) | Full corpus, §1.9 |
| Case nodes | **34** (14 retrofitted + 20 new) | Every document/record has a direct `BELONGS_TO_CASE` edge |
| Person nodes | ~150-200 total, **~20 deliberately recurring/confusable, now placed cross-case** | Long-tail distribution; the deliberate cast is what makes cross-case entity-resolution precision/recall measurable, not just within-case |
| Vehicle / phone / address nodes | ~15-20 / ~20-25 / ~30-40, recurring cast from §2.2 embedded in each, cross-case | |
| CNIC coverage | ~60% of Person entities carry CNIC on ≥1 document | §2.3 |
| Typical query hop count | 1-3 hops within-case; cross-case queries typically 2 hops (entity → Case A, entity → Case B) | Matches the architecture's traversal cap |

### 2.6 A deliberate control group: don't let every query need the graph

Some eval queries (§3) name a recurring entity but are answerable by exact-string keyword search alone (e.g., "which documents mention phone number 0300-XXXXXXX" — BM25 finds this without any graph). Only a subset genuinely requires traversal or aggregation. Both kinds are in the eval set on purpose — the whole point of the architecture's §6 was proving the graph is *earning its cost*, not assuming it.

### 2.7 What this sample size can actually tell you

The arithmetic, updated for the cross-case placement: 3 confusable pairs give **3 independent must-not-merge test cases** at the CNIC-tier (all pairs CNIC-present) **plus 1 additional must-not-merge test case at the harder name-fallback tier** (the one pair also tested CNIC-absent) — **4 must-not-merge cases total across two different confidence tiers**, not one blended count. 4 name-variant people, each appearing under all 3 of their scripted variants, give **C(3,2) × 4 = 12 pairwise must-merge test cases**, split 2 people's worth within-case (easier — case context helps) and 2 people's worth cross-case (harder — no shared case context). That's roughly 16 hard-case data points, now meaningfully split across CNIC-tier vs. name-fallback-tier and within-case vs. cross-case, rather than one undifferentiated pool.

| What this scale can support | What it can't |
|---|---|
| Catching a resolution pipeline that's obviously broken at either tier — merges everything, merges nothing, or fails the CNIC-mismatch rule outright | Distinguishing precise precision percentages at either tier, or reliably comparing within-case vs. cross-case difficulty from this sample alone — with ~4-8 cases per split, a couple of results flipping swings the number by a lot |
| A first-pass sanity check, per tier, before investing further build time | Trusting a single borderline result at any one split as a stable measurement |

**Threshold for scaling up:** unchanged in principle from the prior version — if a tier's first-pass result is clearly good or clearly broken, proceed; if it's near a pass/fail line, generate more cases in that specific tier/split before trusting the number. This remains a **scale-later, not redesign-later** situation: both `entity_roster.csv` and `case_index.csv` support adding more rows with no structural change.

---

## 3. Evaluation query set

### 3.1 Format (unchanged structurally, new fields)

`scripts/run_eval.py`'s `TEST_CASES` shape stays the base; the eval set is stored as structured data (`data/eval/eic_eval_set.json`). Each entry now also carries `case_id` (or `null` for genuinely cross-case/non-case-scoped queries) and a `scope` field:

```json
{
  "id": "eic-0142",
  "question_ur": "...",
  "question_en": "...",
  "question_roman_ur": null,
  "category": "graph_multihop",
  "scope": "case-scoped",
  "case_id": "CASE-011",
  "expected_route": "GRAPH",
  "expected_answer_entities": ["person:P014", "person:P031"],
  "expected_source_docs": ["FIR-2026-BUR-011.pdf", "CASEDIARY-FIR-2026-BUR-011-01.pdf"],
  "difficulty": "3-hop",
  "notes": "Tests co-accused traversal through a shared incident, not a direct mention"
}
```

### 3.2 Category counts — case-scoped as the majority, cross-case explicitly sized

Per Change 6: case-scoped queries are the majority category, matching the architecture's case-scoped-by-default retrieval design; cross-case is a distinct, explicitly-sized category, not an afterthought; status-aware filtering gets real coverage.

| Category | Scope | Concepts | × languages | Instances |
|---|---|---|---|---|
| Content/RAG — single-document factual lookup | Case-scoped | 18 | Ur + En | 36 |
| Structured/SQL lookup (offense sections, FIR/case metadata, date ranges) | Case-scoped | 12 | Ur + En | 24 |
| **Status-aware filtering** *(new)* — e.g. "open cases involving X," "which of this person's cases are closed" | Mixed (some case-scoped, some cross-case) | 10 | Ur + En | 20 |
| Within-case entity relationship | Case-scoped | 12 | Ur + En | 24 |
| Within-case multi-hop / timeline | Case-scoped | 8 | Ur + En | 16 |
| **Cross-case entity/pattern queries** *(resized, was blended with the row above)* — repeat offenders, shared entities across Cases, SOW Module 7's pattern-analysis ask | Explicit cross-case | 14 | Ur + En | 28 |
| **Within-case conflict/inconsistency detection** *(new — architecture §7.5)* | Case-scoped | 6 | Ur + En | 12 |
| Ambiguous/routing-control (mentions an entity but doesn't need the graph, or doesn't need cross-case) | Mixed | 8 | Ur + En | 16 |
| No-answer-in-corpus (must say "not enough information," not fabricate) | Mixed | 8 | Ur + En | 16 |
| Roman-Urdu standalone slice | Case-scoped | — | — | 12 |
| **Total** | | **~96 concepts** | | **≈204 instances** |

Case-scoped and mixed-but-mostly-case-scoped categories account for roughly 70% of instances; the explicit cross-case category (28 instances) is sized to be a real test of the cross-case retrieval mode on its own, not a handful of examples riding along inside a bigger bucket — the change Change 6 specifically asked for. Total instance count grew from ~162 to ~204 because status-aware and conflict-detection are genuinely new categories, not a re-slicing of the old ones; if that's more than you want to hand-verify at Tier-3 (§4.3), the status-aware and conflict-detection categories are the ones to trim first, since they're additive rather than replacing existing rigor.

### 3.3 The "does not guess" slice, specifically

Unchanged in intent: 8 concepts (16 instances) with no correct answer anywhere in the corpus — now including at least one case-scoped variant ("what is [entity]'s CNIC" for an entity deliberately never given one in this case's documents — testing that the system reports the CNIC as unknown rather than fabricating a plausible-looking one) alongside the existing fabricated-case-number and nonexistent-phone-number style questions.

### 3.4 Conflict/inconsistency eval queries, specifically

The 6 new concepts here (12 instances) are built directly against deliberately-seeded contradictions: at least 2 of the complex-shape Cases (§1.3) get two witness statements that disagree on a concrete detail (a time, a location, a description) for the same incident, on purpose — the eval set then tests whether the system surfaces this as a flagged inconsistency (per architecture §7.5) rather than silently picking one version or, worse, blending both into a confident-sounding merged answer.

---

## 4. Generation methodology

### 4.1 Method: hybrid, not pure-LLM and not pure-template (unchanged)

| Layer | Method | Why |
|---|---|---|
| Structured fields (Case metadata, FIR number, station, date, PPC/PECA sections, CNIC) | Rule-based templating from `case_index.csv` and the entity roster | Deterministic and correct by construction — including the new §1.2 consistency rule, which is only enforceable if these fields are generated from the Case record, not improvised per document |
| Narrative/free text (Tehrir, witness statement prose, IO remarks, diary entries) | LLM generation, prompted per-document with the relevant Case context and entity profile(s) injected | Templating alone produces repetitive, unnatural prose; pure LLM generation with no scaffolding drifts on fields that need to be exact — validated directly in Step 1, including a real prompt-design failure (an over-explicit disambiguation instruction produced unnatural meta-commentary) that was caught and fixed before scaling |
| Noise (OCR/scan/handwriting) | Separate rendering + degradation pass (§1.8), applied *after* clean generation | Keeps a clean ground-truth version of every document for scoring OCR accuracy |
| Structured records (§1.6) | Pure templating from `case_index.csv`, no LLM narrative step | It's row/field data by design, not prose — there's nothing for an LLM to generate here |

### 4.2 Case-first, entity-second generation order

The entity roster (§2) can't be placed across Cases until the Cases exist — this revision makes the generation order explicit: **`case_index.csv` is generated first** (all 34 Case records, including the retrofit pass over Batch 0), **then `entity_roster.csv`** is built with each designed entity's cross-case placements referencing real `case_id` values, **then** document generation proceeds per §8's build order. This is a change from the prior version, which built the entity roster and case index as parallel, loosely-coordinated artifacts — now there's a hard dependency direction, which is also what makes §1.2's consistency rule checkable at all.

### 4.3 Entity consistency — the mechanism (unchanged principle, extended schema)

The failure mode to avoid: prompting an LLM separately for each document and hoping it "remembers" entity details from a document generated earlier. It won't. `entity_roster.csv`'s schema, extended this revision:

`entity_id, type, canonical_name, canonical_attributes (father's name / CNIC / address / plate / phone as applicable), surface_variants (list), designed_as (recurring / confusable-pair / name-variant / single-mention), pair_or_group_id, case_ids (list — every Case this entity appears in, new), cnic_shown_in (list of doc_ids where the CNIC is actually displayed — new, distinct from canonical_attributes' ground-truth CNIC, since §2.3 requires some documents to omit it even though the roster always records it)`

Every document-generation prompt is given the exact canonical profile, the specific surface variant to use, **and whether this specific document shows the CNIC or not** — all chosen deterministically by the generation script, never left to the model.

### 4.4 Review tiers (unchanged process, same effort budget)

| Tier | Coverage | Method |
|---|---|---|
| 1. Structural validation | 100%, automated | Every document parses against schema; every required field present; every referenced `entity_id` resolves; **every document's Case-consistency check passes (§1.2)**; **every structured record's `case_id`/`fir_number` resolves (§1.6)**; encoding is valid Urdu Unicode |
| 2. Fluency first-pass | 100%, automated (LLM-as-judge), calibrated first | Unchanged — see the one-time calibration gate below |
| 3. Human spot-check | Stratified ~15-20% of documents, **100%** of hard cases (confusable pairs, name-variant sets — now including the CNIC-present/absent variants specifically), **100%** of eval-set answer keys | Your Urdu review time is the scarcest resource — the CNIC-absent confusable-pair variant is exactly the kind of subtle case an LLM judge can't reliably self-assess |

**Tier-2 calibration — unchanged, still a one-time gate.** Before trusting the judge at 100% coverage, run it once against a 15-20 document sample that also gets a full human read; calibration fails if judge/human disagree on pass/fail for more than ~20% of that sample, or the judge consistently misses one error type. This runs once, early — it doesn't add per-document cost to the now-larger corpus.

---

## 5. Data formats and storage

### 5.1 File formats and proportions (updated for structured records)

| Format | Use | Approx. share |
|---|---|---|
| PDF (text layer, born-digital) | Clean-typed documents | ~65% of narrative documents |
| PDF (image-only, no text layer) | Scanned + handwritten renderings | ~35% of narrative documents |
| CSV | `offense_sections.csv`, `entity_roster.csv`, `case_index.csv`, **new `structured_records/case_management_export.csv`** | Structured reference/evidence data, feeds the SQL route |
| JSON | Ground-truth content per document (§5.2), eval query set | Not ingested by the pipeline — evaluation/scoring only |

### 5.2 Ground-truth pairing (unchanged)

Every document has one canonical structured content record and one or more *rendered* artifacts sharing the same `doc_id`:

```
data/memory/<doc_type>/<doc_id>.pdf              # what the pipeline ingests
data/memory/_ground_truth/<doc_id>.json          # canonical field values + narrative text, never touched by noise
```

### 5.3 Folder layout (updated)

```
data/memory/
  official_documents/        (existing, unchanged — 9 real scraped SOPs)
  firs/                      (existing 14 + new ~20)
  complaint_applications/    (existing 4 + new, complex-shape cases only)
  case_diaries/              (existing 4 + new, developing/complex-shape cases)
  charge_sheets/             (existing 3 + new, complex-shape cases only)
  missing_persons/           (existing 3 + new — now case-attached, §1.1)
  traffic_accident_reports/  (existing 3 + new — now case-attached, §1.1)
  witness_statements/        (new — per case shape, §1.3)
  daily_diary/               (new — 10 pages, NOT case-attached, §1.5)
  recovery_memos/            (new — developing/complex cases in relevant categories)
  internal_circulars/        (new — 4, NOT case-attached, §1.5)
  structured_records/        (new — case_management_export.csv, §1.6)
  _ground_truth/             (one JSON per new doc_id, §5.2)
  manifest.json              (extended — see §5.4)
  offense_sections.csv       (extended — + Illegal Weapon Possession, unchanged from prior revision)
  entity_roster.csv          (extended schema — §4.3)
  case_index.csv             (the source of truth for all 34 Cases — §1.1, §4.2)

data/eval/
  eic_eval_set.json          (new — the ~204-instance query set, §3)
```

### 5.4 `manifest.json` extensions (updated)

Each entry adds these fields beyond the existing schema (`doc_id, doc_type, source, category, police_station, date, sections, file_path, related_fir`):

| Field | Purpose |
|---|---|
| `language` | `ur` \| `en` \| `mixed` \| `roman-ur` |
| `rendering` | `clean` \| `scanned` \| `handwritten` |
| `entities` | list of `entity_id`s referenced |
| `case_id` | cross-references `case_index.csv`; **`null` for Daily Diary and Internal Circular entries (§1.5) — a document legitimately having no case is now a valid, expected state, not a missing field** |
| `cnic_shown` *(new)* | boolean — whether this specific document displays a CNIC for any person entity it references, independent of whether the roster has one on file |

### 5.5 `case_index.csv` schema (new, formalized)

`case_id, fir_number, crime_category, investigation_officer, police_station, incident_date, investigation_status, location, initial_description, shape (minimal/developing/complex), source (retrofitted/new), linked_doc_ids, structured_record_ids`

---

## 6. Validation of the synthetic data itself

### 6.1 What to check, and against what reference (unchanged)

No real Urdu police-document dataset exists to check against — that's the entire premise. Two legitimate reference points: the 9 real scraped Islamabad Police documents (register/phrasing consistency check), and published Urdu OCR benchmark datasets (noise-plausibility calibration, not content).

### 6.2 Statistical shape checks (extended)

| Check | What "healthy" looks like |
|---|---|
| Document length distribution | Varies by type — diary entries short, charge sheets long |
| Entity frequency distribution | Long-tailed — most entities appear in one Case, a small deliberate minority recur across Cases (§2.2) |
| **CNIC coverage rate** *(new)* | Close to the 60/40 target (§2.3) — if it's badly off in either direction, one of the two resolution-confidence tiers isn't getting real test coverage |
| **Case-shape distribution** *(new)* | Close to the 40/40/20 target (§1.3) — a distribution skewed toward one shape means the eval set's status-aware and multi-hop categories are testing a narrower slice than intended |
| OCR noise rate on the scanned/handwritten slice | Comparable order of magnitude to published Nastaliq OCR benchmarks, not near-zero |
| Language tag accuracy | Spot-check against the `language` field |

### 6.3 "Good enough to start testing" checklist (extended)

| Criterion | Gate |
|---|---|
| 100% of documents pass Tier-1 structural validation, **including the Case-consistency check (§1.2)** | Hard gate |
| 100% of `entity_roster.csv` entries referenced in at least one document they claim to appear in | Hard gate |
| 100% of confusable-pair and name-variant hard cases manually verified as designed correctly, **including that the CNIC-present and CNIC-absent variants are each genuinely distinguishable as intended** | Hard gate |
| **100% of structured-record rows resolve to a real Case and matching FIR number** *(new)* | Hard gate — same reasoning as the document-level consistency check |
| ≥90% of Tier-2 fluency scores pass threshold, Tier-3 sample confirms no systematic issue | Soft gate |
| OCR noise rate check lands in a plausible band | Soft gate |
| **CNIC coverage rate lands within a reasonable band of the 60/40 target** *(new)* | Soft gate — tune the roster's CNIC-assignment script and regenerate the affected documents' structured fields if it's badly off |
| Eval set answer keys 100% human-verified | Hard gate |

---

## 7. Transition plan to real data (unchanged, one addition)

Carries over unchanged: folder layout, `manifest.json`/`case_index.csv`/`entity_roster.csv` schemas (not content), the eval set's JSON schema, the tiered QA process.

Gets rebuilt/revalidated: noise profile, entity frequency distribution, document-structure fidelity — all as before, plus:

- **Real Case-shape and Investigation-Status distributions.** The 40/40/20 shape split and the 5-way status distribution in this plan are design choices calibrated against your judgment, not a measurement of how a real department's caseload actually looks. Once real data exists, check the actual distribution and treat any retrieval/eval-set category sized against the synthetic assumption as needing a second look if reality is meaningfully different (e.g., if real cases are minimal far more often than 40%, the eval set is currently over-testing complex-case scenarios relative to what officers will actually query most).
- **Real CNIC-presence rate**, already flagged as an open risk in the architecture report (§12.4 there) — this plan's 60/40 target is a design choice for test coverage, not a prediction.

---

## 8. Build order

Case-first this revision (§4.2) — each step is a hard dependency for the next:

1. **`case_index.csv`** — all 34 Case records: retrofit the 14 existing Batch-0 FIRs first (deterministic, derived from `manifest.json`), then generate the 20 new Cases with their shape/status/category assignments (§1.1, §1.3, §1.4)
2. **`entity_roster.csv`** — built against the now-real `case_id` values from step 1, placing the designed cross-case cast (§2.2) and CNIC coverage (§2.3) — get the hard cases (confusable pairs, name variants) designed and verified first, since they're the highest-value/lowest-volume artifact
3. **Extended `offense_sections.csv`** — unchanged from the prior revision (Illegal Weapon Possession category)
4. **`structured_records/case_management_export.csv`** — small, templated directly from `case_index.csv`, no dependencies beyond step 1
5. **Clean-typed narrative documents**, in Case order (all of one Case's documents generated together, so Case context is fresh in the generation prompts) — unblocks ingestion, Urdu NLP, and embedding dev
6. **Tier-1/Tier-2 validation pass**, including the new Case-consistency and structured-record checks — fix before proceeding
7. **Noise rendering pass** (scanned + handwritten variants) — only once clean content is validated
8. **Eval query set** — written last, against the final corpus, referencing real `case_id` values for the `scope`/`case_id` fields (§3.1)
9. **Tier-3 human review** of the eval set and hard cases — the last gate before this is trustworthy as a benchmark baseline

---

## 9. Reconciliation against the architecture report

This section makes explicit what changed here specifically because of the architecture's Change 1 (case-centric model) and Change 4 (CNIC-first resolution) — so it's traceable that this revision tests those decisions deliberately, not incidentally.

| Architecture decision | What this plan does to actually test it |
|---|---|
| Case is a first-class entity; every document FK's to exactly one Case (§3.1) | §1.1 generates `case_index.csv` first and case-first in the build order (§4.2, §8); §1.2 adds a Tier-1 consistency check that a document's fields must actually agree with its owning Case, not just carry a `case_id` label |
| Retrieval is case-scoped by default, cross-case is explicit (§8) | §3.2's eval set makes case-scoped queries the majority category and sizes cross-case as its own explicit, non-trivial category (28 instances) rather than blending the two |
| Documents that aren't case-evidence still need to exist in a case-aware corpus (implicit in §3.2's evidence-type framing) | §1.5 deliberately keeps Daily Diary and Internal Circular documents un-cased, testing that the system doesn't force everything into the case model incorrectly |
| Structured records as a distinct evidence type, separate from the document pipeline (§3.2) | §1.6 adds a small, genuinely non-narrative structured-records artifact, kept deliberately small in scope rather than turned into a second document-generation effort |
| CNIC-first, name-fallback entity resolution with a 3-tier confidence gate (§7.3) | §2.1 gives every Person entity a ground-truth CNIC in a clearly-synthetic format; §2.3 deliberately varies CNIC presence at ~60/40 so both resolution paths get real coverage; the confusable-pair design explicitly tests the CNIC-tier "must not merge" rule *and* — for one pair — the harder CNIC-absent name-fallback version of the same test |
| Within-case conflict/inconsistency detection (§7.5) | §3.4 seeds deliberate contradictions in at least 2 complex-shape Cases and sizes a dedicated eval category (12 instances) to test whether they're flagged, not silently resolved either way |
| Cross-case pattern analysis (SOW Module 7, architecture §6/§8) | §2.2's repeat-offender/organization cast is explicitly placed across distinct Cases (not documents-within-a-case) specifically so this is testable at the harder, intended difficulty |

---

## Changelog (this revision)

- **§1 rewritten around Cases as the top-level unit.** Case schema pulled directly from the architecture's §3.1 field list; the existing 40-doc corpus's 14 FIRs get retrofitted Case records (confirmed choice) rather than being left case-blind; 20 new Cases replace the old flat 22-FIR count, distributed across categories and three realistic case shapes (40% minimal / 40% developing / 20% complex, confirmed); Investigation Status gets a 5-value distribution tied to case-shape maturity (confirmed); Missing Person and Traffic Accident reports are now case-attached rather than a standalone bucket; a new Tier-1 rule requires every document's fields to actually agree with its owning Case, not just carry a `case_id` label.
- **§1.5 (new).** Daily Diary and Internal Circular documents explicitly excluded from case-scoping, on the reasoning that forcing every document into the Case model would be architecturally dishonest for station-level logs — flagged as a deliberate design choice, not an oversight.
- **§1.6 (new).** A small structured-records evidence type added (CSV/table-shaped, ~15 rows, no narrative generation), directly implementing the architecture's document-vs-structured-record evidence-type split.
- **§1.8 noise-injection section** gets one added paragraph making explicit that this is document-evidence noise, not "media evidence" in the SOW's broader sense, and a cross-reference to the architecture's flagged OCR-timing-vs-SOW deviation so the two documents don't silently drift apart on that point.
- **§2 substantially reworked.** Every designed recurring/confusable/name-variant entity is now explicitly placed across distinct Cases rather than documents-within-one-case (Change 2) — the harder, intended-difficulty version of the cross-case test. Ground-truth CNIC added to every Person entity in a newly-defined clearly-synthetic format (the dry run's real-looking province-code CNICs get regenerated to match). CNIC presence/absence deliberately varied at ~60/40 (confirmed target), with the confusable-pair design extended so one pair is tested in both a CNIC-present and a CNIC-absent context — exercising both tiers of the architecture's confidence gate, not just the easier one.
- **§2.7 sample-size arithmetic redone** to reflect the new CNIC-tier vs. name-fallback-tier split, rather than one blended hard-case count.
- **§3 eval set restructured** around case-scoped (majority) vs. explicit cross-case (resized to a real, dedicated 28-instance category) per Change 6; new status-aware-filtering category (20 instances) and new within-case conflict-detection category (12 instances, tied to deliberately-seeded contradictions in §3.4); total grew from ~162 to ~204 instances, with an explicit note on which new categories to trim first if that's too much Tier-3 review burden.
- **§4 generation methodology**: build order is now Case-first, entity-roster-second (§4.2, a real change from the previous parallel-artifact approach); `entity_roster.csv` schema extended with `case_ids` and `cnic_shown_in`.
- **§5 storage**: added `structured_records/`, formalized `case_index.csv`'s schema explicitly (§5.5, previously implied but not spelled out), `manifest.json` gets a `cnic_shown` field and an explicit note that `case_id: null` is valid for non-case-scoped documents.
- **§6 validation checklist**: added CNIC-coverage and case-shape-distribution statistical checks, and a hard gate on structured-record Case/FIR consistency.
- **§9 (new).** Explicit reconciliation table tracing each architecture Change 1/Change 4 decision to the specific plan mechanism that tests it, per your request.
- Nothing from the prior version's rigor was dropped: noise realism, ground-truth/rendered-artifact pairing, the tiered QA process (including the Tier-2 calibration gate added in the previous revision), and all hard/soft gates carry forward, extended rather than replaced.
