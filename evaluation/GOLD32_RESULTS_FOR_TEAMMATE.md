# Gold-32 Final Results — Ownership Breakdown & What Still Needs Fixing

**Run date:** 2026-09-07
**Build:** `main` @ `c435207` (your KB1 router/verifier fix included) + one
additional fix from this session (M4 keyword collision — see §3).
**Data:** your regenerated 2026-09-07 dump + Chroma, **plus a data repair
performed during this run** — read §2 before re-running anything, or you will
reproduce failures that aren't code bugs.
**Judge:** `gemini-flash-lite-latest`, "close numbers OK" prompt (Module 20).
All numbers below come from `evaluation/gold32_results.json` captured this run.

---

## 1. Where we landed

| Slice | FactualCorrectness | Pass (≥0.5) | AnswerRelevancy |
|---|---|---|---|
| **All 32** | **0.425** | 14/32 | 0.687 |
| **Excluding KB bucket (24 Q)** | **0.546** | 13/24 | 0.780 |
| Najiah's 6 questions | **0.817** | 5/6 | **1.000** |
| Everything else, excl. KB (18 Q) | 0.456 | 8/18 | — |

Baseline for comparison was **0.11** mean / 1-of-24 pass
(`GOLD32_EVALUATION_REPORT.md`). So: real, large improvement overall, with the
remaining weakness concentrated in two specific buckets.

**By question type:**

| Type | Score | Pass | Status |
|---|---|---|---|
| Fact Retrieval | **0.98** | 6/6 | ✅ solved |
| Complex Reasoning | **0.64** | 5/8 | mostly good |
| Creative Generation | 0.40 | 2/5 | mixed |
| Contextual Summarization | **0.02** | 0/5 | ❌ needs work — §4 |
| Knowledge Base Reasoning | **0.06** | 1/8 | ❌ needs work — §5 |

---

## 2. ⚠️ Data-integrity issue in the shared dump — please regenerate it

**The 2026-09-07 `muhafiz_dump.sql` has corrupted Urdu text.** Every Urdu
value is stored as CP437 mojibake — `╪¿╪║█î╪▒` instead of `بغیر لائسنس`. This
happened when the dump was created through a PowerShell pipe that re-encoded
UTF-8. English data is unaffected.

**Why it matters:** any aggregate matching on Urdu values silently returns
wrong answers. Live-confirmed: G5 (weapon compliance) reported **"0 of 32
unlicensed"** instead of the correct **30 of 32**, purely from this.

**What I did about it in this run** (so the scores above are valid):
1. Re-projected the graph from `tests/fixtures/muhafiz_api_snapshot.json`
   (which has clean UTF-8 Urdu), one FIR per fresh process — the bulk
   `sync --full` kept OOMing on this machine.
2. That re-projection wrote **duplicate edges** on top of the dump's
   differently-keyed nodes (every relationship type roughly doubled — 189
   accused edges for 94 real pairs). Deduped by keeping the newest edge per
   distinct endpoint pair. Verified afterward against gold: **A1 = 94 total /
   67M / 24F / 3 unclear, G5 = 30/32, CR7 = 33 records / 1 settled** — all
   exact matches.
3. The dedupe missed 18 edges on `cms_complaint` / `pkm_application`
   (StructuredRecords carrying only `record_id`, no `entity_id`/`case_id`),
   which broke CR6/CR8 down to 0.0. Fixed by writing those
   `BELONGS_TO_CASE` edges directly; both re-scored to **1.0**.

**Action for you:** regenerate the dump with proper UTF-8 handling (avoid
piping `pg_dump` output through PowerShell; write directly to file or use
`docker exec ... > file` from a UTF-8-safe shell). Also worth knowing:
`sync_muhafiz_data.py --endpoint cms` **cannot work in isolation** — it only
fetches the CMS endpoint, so `build_e_tag_index(firs)` is empty and
`resolve_cms_case_id()` returns `None` for every complaint, silently writing
no case links. It needs the full fetch (or at minimum FIR + CMS together).

---

## 3. One bug found in Najiah's modules — already fixed this session

**M4 was being hijacked by Module 16's routing.** `_COURT_READINESS_KEYWORDS`
contained the bare Urdu word **`'عدالت'`** ("court"), which matched M4's
"...وہ مقدمے عدالت میں کہاں تک پہنچے" (how far cases progressed *in court*)
and routed it to the court-file-readiness scan — an unrelated question about
missing case-file fields. Same false-positive class as the CR8 pattern that
was over-matching KB1 (which you already fixed).

**Fix applied:** removed the bare `'عدالت'` entry; the specific handover
phrases (`عدالت کو حوالگی`, `کیس فائل تیار`, `حوالگی کے لیے`,
`قبول کرنے سے پہلے`, `پراسیکیوٹر`) are sufficient. Verified: M4 no longer
matches, **G3 still routes correctly and still scores 1.0**, and all 171
`test_xagg.py` + `test_router.py` tests pass.

**Note:** this fix was necessary but M4 only moved 0.2 → 0.1 — the routing was
only part of its problem. See §4.

---

## 4. Contextual Summarization (0.02, 0/5) — the biggest remaining gap, yours

All five failures are **derived-aggregate / synthesis questions**. I traced
each to the actual dispatching function so you're not guessing:

| Q | Route | Score | Root cause |
|---|---|---|---|
| **M7** | XAGG | 0.0 | `_reporting_delay_rate_by_year` computes the **wrong metric**. Its own `note` admits it: "rate of FIRs *recording a delay reason*", returning 0% (2024) → 14.9% (2026). But M7 asks for **mean minutes from incident to report** — gold is **15.0 min (2024) → 1401.3 min (2026)**. The primitive cannot answer the question as asked; it needs an actual incident→report time-delta computation. |
| **M5** | XAGG | 0.0 | Routes to `statute_by_year`, which returns valid data (2026: PPC 39, Arms Ordinance 16, CNSA 12…). But M5 asks **which case types weapons appear in, and whether that changed since 2024** — that needs a weapon-type × statute **co-occurrence** join across years, which no primitive does. Gold expects: Arms Ordinance §13 paired only with robbery statutes in 2024, but with narcotics (CNSA §9(c), 8 cases) and murder (PPC 302, 8 cases) in 2026. |
| **M4** | XAGG | 0.1 | After the §3 routing fix, M4 gets the statute half right (PPC 61, Arms Ordinance 29) but **claims no court-stage data exists** — it does, in the criminal-record table (33 records, 1 conviction, 30 under trial; the same source `_criminal_record_court_crosscheck` reads). Needs an aggregate joining **statute counts with court/conviction stage**. |
| **M2** | XAGG | 0.0 | Fails at *"The synthesized answer could not be verified as grounded in the sub-answers."* — Meta-Analysis composed sub-answers and the **verifier rejected the synthesis**. This is the Module 11 (Meta-Analysis) + Module 17 (verifier) interaction, not the aggregate. Gold is a simple, computable fact: 9 of 73 FIRs (~12%) from 2 of 19 stations. |
| **M1** | XGRAPH | 0.0 | Refuses to provide the requested year-over-year case-type comparison. Routed to XGRAPH rather than an aggregate — likely a routing miss, since this is a countable comparison. |

**Pattern:** M7 and M5 are *wrong-metric / missing-join* problems in Module
13's derived primitives. M2 is a Meta-Analysis→verifier rejection. M1 is a
routing miss. M4 needs a new statute×court-stage join.

---

## 5. Knowledge Base Reasoning (0.06, 1/8) — §154 fix landed, but 6/8 still abstain

Your Module 8d chunker fix **did work** — §154's body is now in Chroma and
retrievable, and **KB1 improved to 0.5** (correctly cites Section 154 CrPC,
docked only for incompleteness on the compound question's second half).

But the other 7 are still failing, and **not because of judge harshness** — I
checked every judge reason:

- **KB2, KB3** — `status=error`, `route=RAG`. Hard failures, not abstentions.
  KB3 returned no answer at all.
- **KB4, KB5, KB6, KB8, KB9** — `route=None`, all **abstaining** with
  variations of *"could not find sufficient information"* / *"insufficient
  sources"*. The judge's reasons consistently call these "unwarranted
  refusal" / "incorrectly abstained" — the content **exists** in the corpus
  (7,733 documents in the store, all 7 legal PDFs ingested), the app just
  isn't surfacing or accepting it.

**Where to look:** retrieval ranking or the RAG relevance-gate threshold for
the other 6 legal PDFs (Qanun-e-Shahadat, Police Order 2002, Punjab Police
Rules III, Forensics guidelines, PTA Act, Anti-Rape Act). It's no longer a
missing-content problem — it's a retrieval/gating one. `route=None` on five of
them is itself a signal worth checking: these may not be reaching a route
cleanly at all.

---

## 6. Complex Reasoning / Creative Generation failures (XNETWORK / XGRAPH)

| Q | Route | Score | Symptom |
|---|---|---|---|
| CR3 | XNETWORK | 0.2 | "no cross-case connections or patterns were found" |
| CR4 | XGRAPH | 0.0 | same — claims no connections, gold expects a real chain |
| CS4 | XGRAPH | 0.0 | `status=error` — timeout, unverified state |
| G1 | XNETWORK | 0.0 | "no patterns or connections found" vs. specific gold findings |
| G6 | XNETWORK | 0.0 | "unwarranted refusal / abstention" per judge |

This is the **RC-1 broad-query cluster-dump guard** behaving too
conservatively after Module 12: it correctly stopped narrating irrelevant
clusters, but now refuses on questions where a real synthesized answer was
expected. Worth revisiting the relevance-distance threshold (0.145) — five
questions now fail *closed* where they previously failed *open*.

---

## 7. Suggested priority order

1. **Regenerate the dump** with correct UTF-8 (§2) — otherwise anyone
   restoring it hits the Urdu corruption and misattributes it to code.
2. **KB retrieval/gating** (§5) — 8 questions, currently 0.06. Biggest single
   bucket, and the content is already there.
3. **XNETWORK/XGRAPH over-refusal** (§6) — 5 questions failing closed.
4. **Module 13 derived primitives** (§4) — M7's wrong metric and M5's missing
   co-occurrence join are concrete, well-specified fixes.
5. **M2's verifier rejection** — worth checking why Meta-Analysis synthesis
   gets rejected on a straightforwardly-computable fact.

## 8. What's confirmed working (don't regress these)

- **Fact Retrieval: 0.98, 6/6** — D1, S2, S3, A1, A7, CP6 all at 0.9–1.0.
- **Najiah's 6: 0.817, 5/6 pass, AnswerRelevancy 1.000** — CR6, CR8, G3 at
  1.0; CR7 at 0.9; G5 0.6 and G2 0.4 (both partial for documented data-model
  reasons: G2's zimni entries aren't queryable — only a per-FIR index exists —
  and G5's deterministic answer omits some Creative-Generation framing).
- **CR2, CP1** at 1.0.

## 9. Files

- `evaluation/gold32_results.json` — per-question scores + judge reasons
- `evaluation/gold32_pipeline_outputs.json` — captured answers + routes
- `evaluation/MODULE_18_FINAL_REPORT.md` — fuller write-up incl. judge-fairness review
- `evaluation/MODULE_8D_SPEC.md` — the chunker root-cause writeup
