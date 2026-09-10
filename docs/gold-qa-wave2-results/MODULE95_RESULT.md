# Module 95 — G2 and G5 name findings their gold answers do not

**Branch:** `fix/g2-g5-finding-coverage` · **Merge-base:** `origin/main` @ `730b4dc`, then merged with `origin/main` @ `d9bf286` (Module 89, which also touches `xagg.py`) and the all-32 control in §7 **re-run against that newer base with the same result**.

Both questions are open-ended *"what would you flag?"* briefings whose gold
answer is a method line plus three numbered, specifically-evidenced
findings. The system produced a fluent, plausible briefing naming
*different, also-true* findings. Not hallucinating, not refusing —
**selecting the wrong evidence.** Module 87's re-judge of Module 27's
**frozen** answers (system held constant, only the judge changed):

| Q | old judge | new judge |
|---|---|---|
| **G2** | 0.5 / 0.5 / 0.5 | **0.3 / 0.3 / 0.3** |
| **G5** | 0.5 / 0.6 / 0.6 | **0.4 / 0.4 / 0.4** |

**Headline:** four of the six gold findings were **not computable at all**
before this module — one of them rested on an FIR column
(`station_departure_datetime`) that **no line of this codebase read**.
Every gold figure reproduces exactly, so this is a code fix and **not** a
sixth gold correction. Finding coverage went **1/3 → 3/3 on both
questions**, three gold-wording runs each. Two of six pre-registered
paraphrases still fail, for a reason that is Module 92's, not this
module's, and that is reported rather than papered over.

---

## 1. Root cause — the six-finding computability table

Phase 1 was done entirely offline, no backend. Sources: the recorded API
snapshot (`tests/fixtures/muhafiz_api_snapshot.json`, 73 FIRs), the live
`evidence_graph` AGE graph, `muhafiz_schema.dbml.txt`, and a repo-wide
grep for every field name.

| # | Gold finding | Verdict | Evidence |
|---|---|---|---|
| G2 (1a) | 9 FIRs record no incident date | **(a) computable now** | `_case_completeness_scan()` already reports it, and did in all three Module 27 passes |
| G2 (1b) | most zimni entries record no type | **(b) new aggregate** | `psrms.fir_zimni.entry_type` null on **188 of 259** rows. But `fir_zimni` is the one child table `structured_projection.py` does **not** write as StructuredRecords — the graph holds 259 `fir_zimni_index` (tracking) rows and no per-entry node. Nothing could count it |
| G2 (2) | 13 of 73 record departure EARLIER than the report | **(b) new aggregate** | `psrms.fir.station_departure_datetime` — FIR form field 6, "تھانہ سے روانگی کی تاریخ و بوقت", CONFIRMED in the DBML, **populated on 44 of 73** live FIRs, returned by the API. `grep -rn station_departure src/` returned **nothing**. The `Incident` node carried only `incident_datetime` and `report_datetime` |
| G2 (3) | complaints and FIRs joined by an OPTIONAL tag | **(b), schema-shaped** | `cms.complaint.case_tag_number` ↔ `fir.e_tag_number`, both nullable, no FK. **5 of 73** FIRs carry a tag at all. `_cms_fir_linkage()` (CR6) already computes the join — it had simply never been read by this answer |
| G5 (1) | 30 of 32 (94%) unlicensed | **(a) computable now** | Already reported, all three passes. Confirmed |
| G5 (2) | no field for packaging / photographs / chain of custody | **(b), schema-shaped** | The register's real columns are `id, fir_display_code, sr_no, item_detail, caliber_or_bore, quantity, license_status, recovered_from, date_entered, condition, updated_at`. Gold is exactly right: *what* and *condition* are there, custody is not |
| G5 (3) | soft FIR-code join, no enforced key | **(b), schema-shaped** | The DBML says so in the column note: *"SOFT REFERENCE to psrms.fir.fir_display_code, matching the real register which lists the FIR number as a plain value, not a foreign key column."* Measurable as weapons resolving to no `Case` |

**Two corrections to this module's own filed diagnosis.** It predicted that gold's *13 of 73* "would have to come from FIR narrative text, and nothing computes it today" — the second half was right, the first was wrong: `station_departure_datetime` is a typed, CONFIRMED **structured column**, returned populated on 44 of 73 FIRs, that simply nothing had ever read. And it filed CP1 alongside G2/G5; Module 87's re-judge scored CP1 **1.0 / 1.0 / 1.0**, so CP1 closed itself and this module scoped to the two that had not.

**Nothing was (c), and no gold figure was wrong.** After five gold
corrections on this programme (G1, G6, KB9, CP6, KB1) that was the live
possibility. Every figure was re-measured:

```
FIRs: 73
no incident_datetime: 9                          <- gold: 9            ✓
zimni rows: 259, no entry_type: 188 (73%)        <- gold: "most"       ✓
departure present: 44 ; departure < report: 13   <- gold: 13 of 73     ✓
weapon rows: 32 ; بغیر لائسنس: 30 (94%)          <- gold: 30 of 32 94% ✓
cms complaints: 4 ; FIRs carrying an e_tag: 5/73 <- gold: "optional"   ✓
```

Pinned by `tests/test_module95_finding_coverage.py::
test_gold_figures_reproduce_from_the_api_snapshot`, which passes on the
merge-base too — deliberately: it is gold verification, not a change
detector.

### Why the schema-shaped findings needed a different mechanism

Three of the six are statements about a schema's *shape* ("joined by an
optional tag", "no field for chain of custody", "a soft match with no
enforced key"). A count cannot express one. Two are computed as **absences
measured against the real structure**: weapons with no `Case` edge, and the
set difference between the register's column inventory and a declared list
of custody controls. The second deliberately is **not** a canned sentence —
`_missing_custody_controls()` retires a finding automatically if the column
ever appears upstream, and a unit test proves it.

**Why a declared inventory and not `keys(w)` off the graph:** the `Weapon`
node is a *projection* of the register and drops columns the register does
carry (`condition`, `date_entered`). Deriving the finding from the projected
node's keys would report *"no condition field"* — true of our graph, **false
of the register**, i.e. the opposite of what gold asserts and of what an SHO
would act on. `tests/…::test_weapon_register_inventory_matches_the_live_api_shape`
fails the build if the API's real shape ever stops matching the constant.

---

## 2. The change

| File | Change | Held by another track? |
|---|---|---|
| `src/graph/structured_projection.py` | `Incident.station_departure_datetime` joins Module 22's timestamp tuple under the same optional convention; `zimni_entry_count` / `zimni_typed_count` added | No |
| `scripts/backfill_incident_completeness_fields.py` | **New.** MATCH-only backfill of those properties onto existing Incident nodes | No |
| `src/pipeline/xagg.py` | `_case_completeness_scan()` and `_weapon_compliance_scan()` grew the findings they were missing, plus three new pure helpers and two renderer extensions | **Yes — m89 (Module 89) holds this file.** Changes are additive: new helper functions, new keys on two result dicts, appended renderer lines. No existing line's behaviour altered, no import moved except `typing.Sequence` |
| `tests/test_xagg.py` | Two existing tests now stub `age_client` (see below) | Yes, same file family |
| `tests/test_structured_projection.py` | New `TestModule95IncidentCompletenessFields` | No |
| `tests/test_module95_finding_coverage.py` | **New** | No |
| `scripts/module95_aggregate_baseline.py`, `scripts/module95_live_runs.py`, `docs/gold-qa-wave2-results/module95_paraphrases.json` | **New**, measurement only | No |

**One behaviour change to flag at merge:** `_case_completeness_scan()` was
the one aggregate that read only the gateway and never the graph. It now
issues three graph reads, so two existing tests in `tests/test_xagg.py`
that drove it with a fake gateway alone began hitting the real database.
They now stub `age_client` with a `_NoGraphRows` helper that returns no
rows — which is exactly the "graph projected before Module 95" case, and
those tests then assert the *unchanged* pre-Module-95 output.

### What I deliberately did NOT change, and why

- **No new aggregate kind, and not one line of `resolve_aggregate_kind()`.**
  G2 and G5 already reach the right families; what was missing was the
  evidence, not the route. This makes the all-32 dispatch equality control
  hold **by construction** rather than by luck, and it is the reason §7 has
  no route diffs to explain.
- **No widening of `_COMPLETENESS_KEYWORDS`,** even though §6 measures two
  paraphrases falling through it. That list is a bag of gold-derived
  phrases and widening it is the fourth round of gold-specific regexes
  Module 92 exists to stop. Filed as **Defect 106** with the measurement,
  not fixed here.
- **No prompt change and no canned finding list for G2/G5.** The anti-goal.
  Everything added is a measurement over real data or real structure.
- **`fir_zimni` was not projected as a new StructuredRecord family.** That
  is the *right* fix and it would add 259 rows to a graph four other tracks
  are querying live, changing every existing StructuredRecord count
  underneath them. Two per-Incident counts stand in; the trade is written
  into the projection's own comment and filed as **Defect 105**.
- **Neither verifier was touched** (§8, Defect 104) — that layer is m83's.

### Why a backfill and not a re-projection

Verbatim the reasoning in `scripts/backfill_incident_report_timestamps.py`
(Module 22), which this script is modelled on line for line: a previous
re-projection duplicated every edge type in this graph
(`MODULE_18_FINAL_REPORT.md` §3), and the graph is shared with four live
tracks. The script writes **no nodes and no edges**; it only SETs
properties on Incident nodes that already exist, matched by `entity_id`,
and is idempotent. Run live: `73 Incident node(s) updated, 0 not found.`

It also repairs a Module 22 hole found on the way: that backfill required
`incident_datetime` **and** `report_datetime` together, so the 9 FIRs with
no incident date carry no `report_datetime` either (measured: 64 of 73 had
it). G2's chronology check compares departure against the *report* time and
does not need the incident date, so those nine would have silently dropped
out. `report_datetime` is now written independently. It cannot affect
Module 22's own aggregate, which filters on both being present.

---

## 3. Unit tests — checked in both directions

`tests/test_module95_finding_coverage.py` (12 tests) and
`tests/test_structured_projection.py::TestModule95IncidentCompletenessFields`
(4 tests).

**Before the change** (`git stash` of `src/pipeline/xagg.py` and
`src/graph/structured_projection.py`, tests unchanged):

```
FAILED test_weapon_register_inventory_matches_the_live_api_shape
FAILED test_missing_custody_controls_names_all_three_gold_gaps
FAILED test_missing_custody_controls_retires_a_finding_when_a_column_appears
FAILED test_departure_chronology_flags_only_a_true_contradiction
FAILED test_zimni_typing_coverage_sums_the_projected_counts
FAILED test_case_completeness_scan_reports_all_three_g2_findings
FAILED test_weapon_compliance_scan_reports_all_three_g5_findings
FAILED test_weapon_compliance_scan_reports_a_real_orphan_when_one_exists
FAILED TestModule95IncidentCompletenessFields::test_departure_timestamp_becomes_an_incident_property
FAILED TestModule95IncidentCompletenessFields::test_zimni_typing_counts_are_projected
```

Four tests pass on the merge-base **by design** and are labelled as such in
the file: the gold-figure verification, the backfill's arithmetic, the
"degrades to pre-Module-95 output" test (it asserts an *absence*), and the
all-32 dispatch pin.

**After the change:** 16/16 pass, and `tests/test_xagg.py` (445 tests) and
`tests/test_structured_projection.py` both pass in full.

---

## 4. Live verification

Backend on this branch's code, port 8095, shared Postgres/AGE + shared
Chroma, `route` read from the SSE stream. Runner:
`scripts/module95_live_runs.py`. Raw:
`docs/gold-qa-wave2-results/module95_runs.json`.

**Coverage is scored on the EVIDENCE each gold finding rests on** — its
figure, or the mechanism it names — never on gold's phrasing, because that
is the standard this programme judges by. The markers are in the runner and
were written before the runs.

### Before

The before arm is **Module 27's own frozen answers**, the exact three
passes Module 87 re-judged, scored with this module's coverage checker.
No new quota spent, and it is a like-for-like measurement rather than a
re-run on changed code:

| Q | pass 1 | pass 2 | pass 3 |
|---|---|---|---|
| **G2** | 1/3 | 1/3 | 1/3 |
| **G5** | 1/3 | 1/3 | 1/3 |

In all six the *only* finding present is gold's (1) — for G2 the missing
incident dates, for G5 the unlicensed rate. That is exactly Module 87's
judge's complaint, arrived at independently: G2 *"focuses on different
metrics (missing investigation status)"*, G5 *"correctly identifies the
primary compliance issue … however, it is materially incomplete."*

### After — gold wording, three runs each

| Row | route | findings | s |
|---|---|---|---|
| G2 run 1 | XAGG | **3/3** | 9.0 |
| G2 run 2 | XAGG | **3/3** | 8.3 |
| G2 run 3 | XAGG | **3/3** | 9.9 |
| G5 run 1 | XAGG | **3/3** | 6.7 |
| G5 run 2 | XAGG | **3/3** | 7.3 |
| G5 run 3 | XAGG | **3/3** | 4.8 |

**6 of 6 runs carry all three gold findings**, against 0 of 6 before.

### Two side effects, both measured, both from the same defect

- **G2, 4 of 4 XAGG runs** now append *"A cited claim ([Document 1]) could
  not be confirmed against its source: … not found in its source text: 1."*
- **G5, 3 of 3 gold-wording runs** now serve the raw aggregate instead of
  the natural-language summary: *"Paraphrase states number(s) not present in
  the computed result: 4."*

Neither is a content error and neither loses a finding — the G5 fallback
serves the machine-computed text, so coverage stays 3/3. Both were
reproduced deterministically offline and traced to **one** root cause: a
Markdown **list ordinal** is read as a claimed figure. G2's citing sentence
runs straight into `\n\n1.` and `_validate_structural()` looks for a bare
"1" in the source; G5's answer enumerates four points and
`verifier.py::_numbers_in()` looks for a bare "4". The platform therefore
penalises an answer *for having more findings in it*. Filed as
**Defect 104** and not fixed here — that layer is m83's.

---

## 5. Gold comparison — judged on substance, not wording

### G2 — gold's three findings against a representative run

| Gold | Answer (run 1) |
|---|---|
| (1) 9 FIRs no incident date, most zimni entries no type — events cannot be ordered | *"Nine FIRs … do not record an incident date. This lack of information makes it impossible to establish a reliable timeline"* + *"Out of 259 roznamcha/zimni diary entries, 188 (approximately 73%) do not record the entry type … difficult to determine the sequence of events"* ✓ |
| (2) 13 of 73 record departure EARLIER than the report — a chronology contradiction | *"Thirteen FIRs … record the officer's departure from the station as earlier than the report time. This contradiction could be problematic in court"* ✓ |
| (3) complaints and FIRs in separate systems, joined only by an optional tag, so a mistagged complaint looks unactioned | *"Walk-in complaints and FIRs are recorded in separate systems and are only linked by an optional shared tag … can lead to complaints appearing unactioned even when a corresponding FIR exists, especially if the tag is mistyped or missing"* ✓ |

The answer also keeps the *"52 of 73 carry no investigation status"* line —
the finding the old judge called *"different metrics"*. It is true, it is
G1's as much as G2's, and gold's own closing line (*"none of these on its
own loses a case, but each is a gap that obstructs review"*) makes an extra
true gap additive rather than wrong, so it was kept rather than removed to
game the coverage count.

### G5 — gold's three findings

| Gold | Answer (run 1, raw-aggregate fallback) |
|---|---|
| (1) 30 of 32 (94%) unlicensed, needs its own compliance note | *"30 of 32 recovered weapons (~94%) are recorded WITHOUT a licence — a pattern too broad to treat case-by-case; it warrants a standing compliance check"* ✓ |
| (2) records what and its condition, but no packaging / photographs / chain of custody, so procedure cannot be verified | *"The register records WHAT was recovered and its condition, but has no field for packaging or sealing, photographs, chain of custody / handover — so for any entry, whether the item was handled to procedure cannot be verified"* ✓ |
| (3) soft FIR-code match, no enforced key, so a mistyped code silently orphans an item | *"Weapons attach to a case only by a SOFT match on the FIR code … no enforced key. All 32 resolve to a real case today, but a single mistyped code would silently orphan an item with nothing raising an error"* ✓ |

Gold's *"the unlicensed rate is the most prominent point"* is preserved: it
is still the first line.

**One thing gold's (3) required care on.** Gold asserts a *risk*, and the
measured reality is **0 orphans today**. Stating "0 of 32 are orphaned"
would have been true and useless; asserting "items are being orphaned"
would have been false. The renderer branches on the measurement: at 0 it
states the join is unenforced and what a mistyped code *would* do; above 0
it reports the real count. Both branches are unit-tested.

### An honest note on language

G2 is asked in Urdu and G5 in Roman-Urdu; both are answered in English.
That is **unchanged** — Module 27's frozen answers are English too — and it
is outside this module. Flagged because a coverage measurement should not
be read as an all-clear on the answer as an SHO would receive it.

---

## 6. Non-gold paraphrase — written before the runs, not adjusted after

Six rewordings, pre-registered in
`docs/gold-qa-wave2-results/module95_paraphrases.json` and committed
unchanged: two in each question's own language plus one English.

| Row | Lang | route | findings |
|---|---|---|---|
| G2-P1 — *"ہمارے ریکارڈ میں ایسی کون سی خامیاں ہیں جن کی وجہ سے کوئی مقدمہ خاموشی سے رُک سکتا ہے…"* | ur | XAGG | **3/3** |
| G2-P2 — *"اگر تفتیشی ریکارڈ کا جائزہ لیا جائے تو کن اندراجات پر بھروسہ نہیں کیا جا سکتا…"* | ur | **RAG** | 0/3 |
| G2-P3 — *"If you were warning a station commander about cases that could quietly slip through the cracks…"* | en | XAGG | 0/3 |
| G5-P1 — *"Weapon register ka jaiza lein — regulatory compliance ke aitbaar se…"* | roman_ur | XAGG | **3/3** |
| G5-P2 — *"Hathiyaron ki baramdagi ka record kitna mukammal hai…"* | roman_ur | **RAG** | 0/3 |
| G5-P3 — *"Looking at how recovered weapons are recorded, what compliance concerns would you raise?"* | en | XAGG | **3/3** |

**4 of 6 survive.** The capability is real and reaches paraphrases in all
three languages — G5's English paraphrase is byte-identical in coverage to
its Roman-Urdu gold, and G2's first Urdu paraphrase carries all three
findings.

**The two failures are not this module's defect, and are worth more than
the four successes.** Both fall out of the *dispatch vocabulary*, not the
new capability:

- **G2-P2 and G5-P2 lost the XAGG route entirely** and were answered by RAG
  with *"No sufficiently relevant documents were found."* — Module 92's
  finding, fifth sighting.
- **G2-P3 kept XAGG and still failed**, which is the more interesting one:
  `resolve_aggregate_kind()` returns `station_or_category_counts` — the
  generic trailing catch-all — so the answer is a list of stations with few
  cases. `_COMPLETENESS_KEYWORDS` is a bag of gold-derived phrases
  (`"fall through the cracks"` is literally in it; *"slip through the
  cracks"* is not). **Module 92's disease, one layer below the router.**
  Filed as **Defect 106**. Widening the list is exactly the fourth round of
  gold-specific regexes the brief forbids, so it was measured and left.

---

## 7. Regression guard — all 32

`scripts/module95_aggregate_baseline.py` runs every gold question through
`resolve_aggregate_kind()` **and** `run_aggregate()` **and** the shared
renderer — the exact text the orchestrator puts in front of the model —
and dumps JSON. Run on the merge-base, then on the branch, then diffed.

Run twice: against `730b4dc` (this branch's original base) and again after merging Module 89, against `d9bf286`. **Both times: all 32 kinds identical, and exactly G1 / G2 / G5 differ in rendered text.** Module 89 added an aggregate to the same file and moved none of the 32.

**Dispatch equality control: all 32 kinds identical.** Module 95 adds no
aggregate kind and touches no line of the chain, so this holds by
construction; it is pinned in
`tests/…::test_all_32_dispatch_unchanged` against the recorded merge-base
values.

**Rendered-answer equality: exactly three of 32 differ, and all three are
expected.**

| Q | differs | why |
|---|---|---|
| **G2** | yes | the module's subject |
| **G5** | yes | the module's subject |
| **G1** | yes | **shares `case_completeness_scan` with G2** |
| G3 | **no — byte-identical** | `_court_readiness_scan()` reuses both scans but consumes only named keys and renders through its own renderer |
| other 28 | **no — byte-identical** | |

### The G1 cost, measured rather than assumed

G1 dispatches to the same aggregate, so its completeness block gained the
same three findings. This is the one thing in Module 95 that could make
something worse, so it was run live rather than reasoned about.

**Measured: 2 live runs on this branch, and G1 does not use this aggregate
at all.** G1 is `multi_step`, so live it goes through Meta-Analysis, which
decomposes it into five sub-queries answered by five different aggregates
(offender age profile, accused relationships, seized property, incident
time of day, person recurrence — `[Document 1]`…`[Document 5]`). The
completeness scan never appears in the answer. Both runs `route=XAGG`,
41.9s and 48.9s, and both carry **all four** of G1's gold findings:

| G1 gold finding | run 1 | run 2 |
|---|---|---|
| (1) offender profile uniform, 24–49, average ~31 | ✓ *"ranging from 24 to 49 … average age of 31.5"* | ✓ |
| (2) 'stranger' dominates the recorded relationships | ✓ *"اجنبی (stranger) … 15 of the 24"* | ✓ |
| (3) 13 items to a forensic lab, 7 held for a deceased's heirs | ✓ verbatim | ✓ |
| (4) times fairly flat with a mild evening lean | ✓ *"busiest … evening (18:00–23:59), with 19 incidents"* + the full four-bucket split | ✓ |

Neither run carries a verifier caveat. **So the offline diff overstates the
blast radius**: `run_aggregate()` called with G1's *whole* question does
resolve to `case_completeness_scan` and does render differently, but no live
path takes that route. Recorded as measured rather than as a risk retired
by argument — and it means the only aggregate whose rendered text reaches a
user differently is G2's and G5's own.

`tests/test_xagg.py` (445 tests) and `tests/test_structured_projection.py`
pass in full on the branch, including every pre-existing G1/G3/CR6 test.

---

## 8. New defects found, not fixed

Highest number in use in `GOLD_QA_REMAINING_FIXES_PLAN.md` when this was
written: **97**. Taking the next free numbers above 99.

### Defect 104 — a Markdown list ordinal is read as a claimed figure, by BOTH verifiers

**The platform penalises an answer for having more findings in it.**
Measured on this branch, deterministic, reproduced offline:

- `src/pipeline/validation.py::_validate_structural` — the sentence carrying
  `[Document 1]` runs into the following `\n\n1.` list marker, so `1`
  becomes a "cited figure" and the source is searched for a bare `1`.
  **G2: 4 of 4 XAGG runs** append *"Claim cites figure(s)/identifier(s) not
  found in its source text: 1."*
- `src/pipeline/verifier.py::verify_structured_aggregate_paraphrase`
  (`_numbers_in`) — a four-item enumeration puts `4` in the answer's number
  set; the computed result holds `{30, 32, 94, 2}`. **G5: 3 of 3
  gold-wording runs** are rejected with *"Paraphrase states number(s) not
  present in the computed result: 4."* and fall back to the raw aggregate.

Offline reproduction (no backend):

```python
pairs = validation._extract_claim_chunk_pairs(answer, [{"chunk_text": rendered}])
validation._validate_structural(pairs)
# CLAIM  >> '…based on the cross-case aggregate data [Document 1]:\n\n1.'
# REASON >> Claim cites figure(s)/identifier(s) not found in its source text: 1.
```

Neither costs a finding today, but both are noise the judge reads, and the
G5 path silently discards the natural-language answer. The fix is in the
number extraction (ignore an ordinal that is a list marker), in the m83
layer. **Not fixed here** — this module holds neither file.

### Defect 105 — `fir_zimni` is never projected, so investigation-step content is not queryable

`structured_projection.py` writes 11 of the 12 FIR child tables as
StructuredRecords. `fir_zimni` — the one that holds the actual content of
each investigation step (`entry_text`, `entry_type`, `officer_name`,
`entry_date`) — is not among them; only the tracking `fir_zimni_index` is
(259 rows). The content reaches the free-text RAG index and nothing else,
so no aggregate can ask *"which cases have no arrest entry"*, *"which
officer wrote which step"*, or order events within a case. Module 95 needed
one ratio out of it (188 of 259 untyped) and stood two per-Incident counts
in rather than add 259 nodes to a graph four tracks are querying live. The
real fix is the projection, plus a Module-22-style backfill.

### Defect 106 — `resolve_aggregate_kind()`'s vocabulary is Module 92's disease one layer down

Module 92 measured the *router* losing a capability outside a narrow
neighbourhood of the gold wording, and Defect 97 found the same in the
*retrieval window*. This is the third layer: **XAGG's own dispatch chain.**
Measured here on two pre-registered paraphrases, one of which kept
`route=XAGG` and still got the generic `station_or_category_counts`
catch-all — a list of stations with few cases in answer to a data-quality
question, confidently, with no caveat. `_COMPLETENESS_KEYWORDS` contains the
literal `"fall through the cracks"`; the paraphrase said *"slip through the
cracks"*. Widening the list is the fourth round of gold-specific regexes;
the fix is a predicate, and it belongs with Module 92's.

### Defect 107 — the Weapon projection drops `condition` and `date_entered`

`psrms.weapon_register` carries `condition` (ضبط شدہ / forensic status) and
`date_entered`; `structured_projection._write_weapons` projects only
`canonical_name`, `caliber_or_bore` and `license_status`. So no aggregate
can answer what was done with a recovered weapon or when it was booked in —
and any schema claim derived from the *projected* node (rather than the
register, as Module 95 deliberately does) would wrongly report that the
register has no condition field.
