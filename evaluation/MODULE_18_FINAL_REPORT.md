# Module 18 — Final Gold-32 Rerun (Post-8d, Post-Data-Repair)

**Date:** 2026-09-07
**Setup:** teammate's regenerated dump + Chroma (2026-09-07, includes the
Module 8d chunker fix — §154's previously-dropped body now retrievable),
restored fresh, **plus a data-integrity repair performed during this run**
(see §3 — the shared dump's Urdu text was corrupted, and the graph
re-projection to fix it introduced duplicate edges, which were then deduped).
Judge: `gemini-flash-lite-latest`, "close numbers OK" prompt (Module 20).
Every number below traces to `evaluation/gold32_results.json` /
`gold32_pipeline_outputs.json`, captured from this run.

---

## 1. Headline numbers

| Metric | Baseline (`GOLD32_EVALUATION_REPORT.md`) | Module 9 (mid-fix) | **Module 18 (final)** |
|---|---|---|---|
| FactualCorrectness mean (all 32) | 0.11 | 0.39 | **0.428** |
| FactualCorrectness mean (excl. KB) | — | 0.53 | **0.550** |
| Pass rate (≥0.5), excl. KB | 1/24 | 13/24 | **13/24** |
| AnswerRelevancy mean (all 32) | 0.76 | 0.65 | **0.677** |
| AnswerRelevancy mean (excl. KB) | — | 0.79 | **0.767** |
| Precision / Recall / F1 (pass=TP, binary) | — | — | **0.438 / 0.438 / 0.438** |

*(Precision/recall/F1 collapse to one number here because "pass" is a single
binary correct/incorrect judgment per question against ground truth — there
is no multi-class confusion matrix to separate them in this eval design; see
§5 for the framing.)*

**By question type:**

| Type | Baseline | Module 9 | **Module 18** |
|---|---|---|---|
| Fact Retrieval | 0.23 | 0.98 | **0.98** (6/6 pass) |
| Complex Reasoning | 0.10 | 0.59 | **0.64** (5/8 pass) |
| Creative Generation | 0.04 | 0.38 | **0.40** (2/5 pass) |
| Contextual Summarization | 0.06 | 0.02 | **0.04** (0/5 pass) |
| Knowledge Base Reasoning | 0.00 | 0.00 | **0.06** (1/8 pass) |

---

## 2. Najiah's 6 questions — final scores

| Q | Module | FactualCorrectness | AnswerRelevancy |
|---|---|---|---|
| CR7 | 14 (criminal-record × court cross-check) | **0.9** | 1.0 |
| CR6 | 15 (CMS ↔ FIR linkage) | **1.0** | 1.0 |
| CR8 | 15 (DV-report ↔ FIR match) | **1.0** | 1.0 |
| G3 | 16 (court-readiness scan) | **1.0** | — |
| G5 | 15 (weapon compliance) | 0.6 | — |
| G2 | 15 (case completeness) | 0.4 | — |

All 6 confirmed working end-to-end through the live HTTP harness against the
final, corrected data. CR6/CR8 moved from a mid-run 0.0 to 1.0 once the data
bug described in §3 was fixed (verified: the judge's own new reasoning says
"the actual output accurately captures all key facts" — it was correctly
scoring the earlier answer 0 when the data really was wrong, and correctly
scores 1.0 now that it's fixed. **The judge was fair in both cases** — see §5).

---

## 3. Data-integrity issue found and fixed mid-run (important — read before trusting KB/A1/G5 numbers)

The teammate's regenerated Postgres dump, while correctly carrying the 8d
chunker's fixed KB embeddings, had **Urdu text corrupted to mojibake**
(`╪¿╪║█î╪▒` instead of `بغیر لائسنس`) — introduced when the dump was created
through a PowerShell pipe that re-encoded UTF-8 as CP437. This affected every
Urdu-valued graph property (weapon license status, person names, incident
descriptions) and would have silently produced wrong answers for any
Urdu-dependent aggregate (confirmed live: G5 read "0 of 32 unlicensed"
instead of the correct 30).

**Fix applied, in order:**
1. Re-projected the graph from the clean-Urdu API snapshot
   (`tests/fixtures/muhafiz_api_snapshot.json`), one FIR per fresh process
   (avoids the RAM exhaustion that killed earlier bulk attempts) — restored
   correct Urdu across Weapon/Person/Incident nodes.
2. This re-projection, run on top of the dump's differently-keyed existing
   nodes, created **duplicate edges** (every relationship type roughly
   doubled — e.g. 189 accused edges for 94 real pairs). A targeted dedupe
   (keep the newest edge per distinct endpoint pair, verified against a
   dry-run first) restored exact counts: **A1 94/67M/24F/3-unclear, G5
   30/32 unlicensed, CR7 33 records — all confirmed matching gold exactly.**
3. The dedupe's identity-matching missed 18 edges on two record types
   (`cms_complaint`, `pkm_application` — StructuredRecords with no
   `entity_id`/`case_id`, only `record_id`), which broke CR6/CR8. Found via
   the judge-fairness review this module explicitly asked for (CR6/CR8 both
   scored 0.0 with high AnswerRelevancy — a red flag pattern worth
   investigating, not accepting at face value). Root cause: `--endpoint cms`
   in isolation never loads FIR data, so `resolve_cms_case_id()`'s
   `e_tag_index` was empty. Fixed by writing the 18 missing
   `BELONGS_TO_CASE` edges directly from the snapshot's own e-tag/
   forwarded-FIR-number resolution — verified CR6 4/4 linked, CR8 4/8
   confirmed, both re-scored 1.0.

**Net effect:** the numbers in §1 reflect the graph AFTER this repair — i.e.
they are the intended, correct comparison. Flagging this prominently because
if anyone re-runs this eval against a stale copy of this same dump without
the repair, they will reproduce the corrupted-data failures, not a code
regression.

---

## 4. Judge-fairness review (as requested)

Every question scoring below 0.7 was checked against its judge `reasons`
field and the actual answer text, per the testing team's rule: cover the
main points in your own way + extra detail + right facts = pass; only
opposite or incomplete counts against it.

**Verdict: the judge is scoring fairly, not harshly**, with one caveat noted below.

- **CR6/CR8 (mid-run 0.0 → final 1.0):** genuinely a data bug, not judge
  harshness — see §3. The judge's own reasoning in both directions matches
  what was actually true of the data at each point.
- **G2 (0.4):** judge's own words — *"covers the missing incident dates well
  and matches the expected figure of 9 FIRs, however it omits [the zimni
  point]."* This is the same honest scope gap documented in Module 15: the
  graph only stores a zimni **index**, not entries, so that one gold sub-point
  genuinely isn't answerable from current data. Fair score, not harsh —
  the answer really is incomplete on the metric the gold cites.
- **G5 (0.6):** the deterministic scan correctly states 30/32 (~94%)
  unlicensed — matches gold's core fact. The gold (Creative Generation)
  also expects additional framing/angles; the judge docks partial credit for
  the narrower deterministic answer. Borderline but not unfair — the core
  number is right and the judge credits that partially.
- **CR3, M4 (0.2):** both compound/synthesis questions where the model's
  narrative introduces a claim that contradicts a specific fact in the gold
  (per the judge's reasons) — not a phrasing-vs-substance issue, a genuine
  factual miss.
- **KB2–KB9 (0.0, mostly):** **not judge harshness.** The app is *abstaining*
  ("insufficient sources," "could not find sufficient information") on
  questions the gold treats as directly answerable — this is a real,
  unresolved retrieval/coverage gap for 6 of the 8 KB documents (the 8d fix
  only directly addressed the CrPC's §154; the other 6 legal PDFs and/or the
  RAG relevance gate still need work). The judge correctly penalizes an
  abstention when a real answer exists in the corpus; that is exactly the
  "opposite or incomplete" case the testing team's own rule says should fail.
- **CR4, CS4, M1, M2, M5, M7, G1, G6 (0.0):** the pre-existing
  XNETWORK/Meta-Analysis broad-synthesis bucket (RC-1/RC-5) — same "no
  connections found" / "could not be verified" pattern documented in Module
  9, unrelated to Najiah's modules or this run's data repair. Not reviewed
  further here; teammate territory (Modules 11–13).

**One fairness caveat worth flagging to the team:** the AnswerRelevancy
metric is frequently high (0.8–1.0) even on FactualCorrectness=0.0 answers —
this is expected (relevancy measures topical relevance, not correctness) but
worth remembering when skimming scores quickly: a high relevancy number next
to a 0.0 factual score is NOT evidence the judge is being inconsistent; it's
two different things being measured.

---

## 5. Honest framing on precision/recall/F1

Gold-32 is a single-answer-per-question eval (32 independent Q&A pairs
scored against one ground truth each), not a multi-label classification
task — there's no natural notion of false positives vs. false negatives
distinct from "correct" vs. "incorrect." The precision/recall/F1 requested
are reported as the pass-rate treated as both precision and recall (a
question is a "positive" prediction of correctness; it's a true positive iff
FactualCorrectness ≥ 0.5), which is why all three collapse to the same
number (0.438 all-32). If a different breakdown is wanted (e.g. per-type
precision/recall treating each question type as a class), that can be
computed from the same `gold32_results.json` — this report used the
single-aggregate framing since that's what a 32-question fixed-answer eval
actually supports without inventing additional structure.

---

## 6. What's still weak, and whose scope it is

- **Knowledge Base Reasoning (0.06, 1/8 pass):** §154/CrPC retrieval now
  works (KB1 partial-credit at 0.5), but 6 of 8 KB questions still abstain.
  Needs investigation into why retrieval/relevance-gating fails for the
  other 6 legal PDFs even though they're in Chroma (confirmed 7,733 total
  documents in store) — likely a ranking or evaluator-threshold issue, not
  a missing-content one anymore.
- **Contextual Summarization (0.04, 0/5 pass) and several Complex
  Reasoning/Creative Generation zeros:** the XNETWORK/Meta-Analysis
  broad-synthesis bucket (RC-1/RC-5), unchanged from Module 9 — Modules
  11–13 territory.
- **No regressions** in any question that was previously strong.

## 7. Files

- `gold32_pipeline_outputs.json` / `gold32_results.json` — final captured
  run + scores (post-repair).
- `evaluation/MODULE_9_RERUN_REPORT.md` — the mid-fix baseline this compares
  against.
- `evaluation/MODULE_8D_SPEC.md` — the KB root-cause writeup this run
  confirms landed (partially — see §6).
