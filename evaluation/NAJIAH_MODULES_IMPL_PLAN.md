# Implementation Plan — Najiah's Gold-QA Modules (8c, 14, 15, 16, 9, 18)

**Owner:** najiahkhalid
**Branch:** `najiah/gold-qa-modules-8c-14-15-16` (off fresh `main` @ `fd09fa5`)
**Commit identity:** `najiahkhalid <najiahkhalid03@gmail.com>`, **no Claude
co-author trailer** (owner's explicit call, overrides the master plan's
attribution note for this contributor's commits).

This is my own execution plan for the modules assigned to me, grounded in the
actual code in `main` (not the master plan's prose). It follows the master
plan's §0 working discipline: one module per branch off `main`, unit +
**live** verification (gold question **and** a non-gold paraphrase) before
merge, no unverified claims in any writeup.

**State confirmed before starting:**
- All teammate module work (1–8, 8b, 10–13, 17, 19, 20) is merged to
  `origin/main`. Nothing unmerged waiting.
- Module 15's half-run left **nothing** orphaned in `origin/main` or on the
  remote (verified: no branch, no partial commit, no half-applied `router.py`
  patterns). Clean slate.
- System load at plan time: CPU 11%, ~4.5 GB free RAM, no concurrent
  python/docling sessions — quiet enough for live Docling verification, but
  RAM is the constraint, so Docling runs stay isolated (small-then-large) to
  avoid the `std::bad_alloc`/segfault 8b originally hit.

---

## Sequencing (why this order)

`8b re-verify → 8c → 14 → 15 → 16 → 9 → 18`

- **8b re-verify FIRST** — it's already merged (`06f3030`) but never
  live-confirmed on the real CrPC PDF (segfaulted under load). Everything
  downstream (8c retrieval fix, KB re-ingestion, KB1/KB3 eval) assumes 8b
  correctly extracts §154. Confirm the foundation before building on it.
- **8c SECOND** — new code (retrieval scoping). Only meaningful once 8b's
  extraction is confirmed good.
- **14 → 15 → 16** — 14 is independent (built on Module 13's merged
  primitives). 15 and 16 both edit `router.py`, so they are **serialized**:
  15 fully merged before 16 starts, to avoid the conflict the master plan
  flags.
- **9 then 18** — docs/eval only. 9 is the first full Gold-32 rerun (can run
  after 8c). 18 is the final rerun (the DeepEval one), gated on 14/15/16
  landing — this is the "at the end" DeepEval work.

Each module below lists: the exact problem, the fix location in real code, and
the verification (unit + live gold + live paraphrase).

---

## Module 8b (re-verify only — already merged) — ✅ RE-VERIFIED

**No code change.** Confirmed the merged batching fix works on the real corpus.

**Verified (2026-09-05):**
- No crash. The full 319-page `load_pdf()` ran ~4 hours of CPU across all 4
  page-batches (1-80/81-160/161-240/241-319) with **zero segfault/bad_alloc**
  and bounded RAM (~470 MB → ~2 GB) — the batching fix holds where the old
  whole-document Docling call died. (Full OCR run stopped once the crash-free
  claim was proven; §154 text presence confirmed separately, below.)
- §154 survives the batched `page_range` path: extracted batch-1 (pp 1-80,
  contains §154 on p77) via the exact `page_range=(1,80)` call `load_pdf()`
  makes — all 80 pages returned, no crash.

**Important finding — OCR corrupts these text-layer PDFs (relevant to 8c):**
Module 8's honest-status flagged "§154 body missing from Docling extraction."
Root cause found: it's an **OCR artifact**, not a batching or source problem.
- The CrPC PDF has a clean embedded **text layer** — PyMuPDF reads §154's body
  on p77 instantly: *"Every information relating to the commission of a
  cognizable offence…"* (the correct statutory text).
- With Docling's RapidOCR **enabled** (the default), that body is garbled/
  dropped from the markdown.
- With Docling **`do_ocr=False`**, the body extracts **correctly** from p77.
- Conclusion: for legal PDFs that already carry a text layer, OCR is not just
  slow (~7s/page) — it actively degrades extraction. This is a candidate
  improvement for the KB re-ingestion in 8c (extract text-layer PDFs with OCR
  off, reserve OCR for genuinely scanned pages), which would ALSO make §154's
  body retrievable — the missing half of what KB1 needs.

- CrPC is **319 pages** → 4 Docling batches at `_DOCLING_BATCH_SIZE=80`
  (1-80, 81-160, 161-240, 241-319). §154 ("First Information") is an early,
  low section number → expected in batch 1, clear of any batch boundary.
- **Verify (isolated, low-RAM):**
  1. `_cheap_page_count()` returns 319 (PyMuPDF) — already confirmed.
  2. Run `load_pdf()` on the real CrPC PDF in a standalone process; confirm no
     segfault, all 319 pages return text, and §154's heading **and body**
     appear in the extracted markdown (not just the heading — Module 8's own
     honest-status flagged §154 body as a possible Docling extraction gap;
     re-confirm now on the batched path).
  3. If §154 body is genuinely missing from Docling markdown (vs. 153/155/200
     which extract fine), that's an **extraction-quality** issue separate from
     8b/8c — record it honestly, don't paper over it. It would become its own
     follow-on, not silently absorbed.
- **Outcome gate:** if 8b extracts §154 cleanly → proceed to 8c. If §154 body
  is missing → flag it, still proceed to 8c (retrieval scoping is independent
  of that one paragraph), and note KB1/KB3 may stay weak until extraction is
  addressed.

---

## Module 8c — retrieval scoping: KB legal corpus vs. case documents

**Problem (confirmed in code).** In "All Cases" mode, `_build_where()`
(`src/pipeline/harness/tools/rag.py:208-223`) sets `where["all_cases"] = True`,
which pools **case-narrative chunks and global/KB legal chunks together** in
one ranked search. For a *legal* question ("which section governs FIR
registration?"), the far more numerous FIR case narratives — which share
vocabulary ("FIR", "registration", "154") — out-rank the actual CrPC statutory
chunks, so the legal answer never surfaces (observed live on KB1 in Module 8's
honest status: "surfaces real FIR case documents instead of the legal KB
corpus").

**Fix approach (stable, minimal, no new subsystem).** Detect a
**legal/knowledge-base-intent** query and, for it, scope retrieval to the
**global/KB corpus only** (`where = {"is_global": True}`) even when the caller
is in All-Cases mode — instead of the mixed `all_cases` pool. This mirrors the
existing `is_global_only_scope` path already recognized at rag.py:339 (Module
5), so the plumbing exists; 8c decides *when* to use it.

- **Where:** `src/pipeline/harness/tools/rag.py` — add a legal-intent check
  that, when the query is a which-law/section/procedure question, overrides
  the `all_cases` branch to KB-only scope. Keep it a **narrow, additive**
  signal (a legal-vocabulary check: "section", "CrPC", "law governs", "under
  which act", Urdu "دفعہ/قانون", etc.), not a broad reroute — a case-specific
  question must still search case docs.
- **Precision guard:** a question that names BOTH a legal concept AND a
  specific case/FIR stays mixed-scope (it genuinely needs both). KB-only
  scoping fires only for a *pure* legal-reference question with no case anchor.
- **Fallback:** if KB-only scope returns nothing (e.g. §154 body extraction
  gap from 8b), fall back to the existing mixed search rather than a hard
  abstention — so 8c never makes an answer strictly worse than today.

**Verify:**
- Unit: new cases in `tests/test_harness_tool_rag.py` (or the nearest existing
  rag-tool test) — legal-intent query → `is_global` scope; case-anchored legal
  query → mixed scope; non-legal query → unchanged.
- Live gold: KB1 / KB3 exact text → retrieval surfaces a CrPC §154/§155 chunk
  in the top results and the answer cites the governing section (gated on 8b
  extraction being clean).
- Live paraphrase (non-gold): "under what statute does a police report become
  an FIR?" → same KB-scoped legal answer.
- **Then re-ingest** the 7 KB PDFs with 8 + 8b + 8c all active (scoped
  re-ingestion, not a full wipe — the pattern Module 8 used), and re-test
  KB1/KB3 live end-to-end.

---

## Module 14 — CR7 criminal-record × court-outcome cross-check

**Problem.** CR7 asks to cross-reference each case's **investigation/court
status** against the **separate criminal-record / court-outcome record** by
case ID, and report the match/mismatch. Today there's no cross-record-type
join for this shape.

**Fix approach.** Built on Module 13's merged derived-aggregate primitives
(`src/pipeline/xagg.py`). CR7 is a group-by-status count cross-referenced
against the court-outcome record — closer to Module 3's person-recurrence join
than a plain breakdown. **Decide the home first** (confirm in code before
writing): XAGG aggregate vs. a small new harness tool. Likely XAGG, reusing
Module 13's join/time-bucket helpers.

- **Where:** `src/pipeline/xagg.py` — new `_criminal_record_court_crosscheck()`
  (or similar), joining case status ↔ court-outcome by case ID; route it via
  the keyword override; render in all 3 sites (harness tool + 2 orchestrator
  paths), same discipline as Modules 2/7.

**Verify:**
- Unit: new test for the cross-check aggregate.
- Live gold: CR7 exact text → states the real status split and the
  match/mismatch finding for the case(s) with both records.
- Live paraphrase: a reworded "do our court outcomes line up with case status?"

---

## Module 15 — field-consistency questions → XAGG/graph joins, not RAG (RC-3 + RC-6)

**Pre-work (teammate's half-run cleanup — already verified clean in `main`).**
His stuck run left nothing in shared state. In MY environment there's no
`fix/router-field-consistency-to-xagg` branch. So I start fresh; nothing to
un-orphan on my side.

**RC-6 first step (from Module 10.2's carryover).** Before writing the fix,
restart the backend with `2>&1 | tee backend_<date>.log` and re-run
CR6/CR8/G2 to capture the **actual exception** behind rag.py:362-364's generic
"Retrieval failed" — the swallow point Module 10.2 traced. Two outcomes:
(a) it's a real RAG-retry-path exception → surface `ToolError.message` and fix
it here too; or (b) it's moot once these questions stop routing through RAG at
all (the RC-3 half below). Determine which from the captured log, don't assume.

**RC-3 fix.** Generalize Module 3's person-recurrence override into a broader
"does record-type A's field match record-type B" / "is X confirmed by the case
record" trigger family (English/Urdu/Roman-Urdu) in
`src/pipeline/router.py`, covering CR6/CR8/G2/G5's shape; add the per-row
field-match check in `src/pipeline/xagg.py` or a small harness tool
(walk-in-complaint tag ↔ FIR; forwarded-FIR field ↔ real FIR; incident-date
presence rate; dispatch-time-before-report-time anomaly count; license-status
rate).

**Verify:**
- Unit: `tests/test_router.py` full pass (new triggers, no regressions).
- Live gold: CR6, CR8, G2, G5 exact text → each states the real per-record
  match/mismatch count, not an abstention or RC-6's "document search failed".
- Live paraphrase: one non-gold field-consistency question.

**Serialization note:** 15 touches `router.py`; **fully merge 15 before
starting 16**.

---

## Module 16 — cross-case scope LLM fallback (RC-4)

**Problem.** When no regex override matches, `router.py` defaults to
case-scoped and can reject a valid cross-case question outright (G3) for
lacking the exact cue words.

**Fix approach.** In `_deterministic_route_override`'s miss path / the router's
scope decision: when no override matches, ask the **existing LLM classifier**
explicitly "one case or the whole caseload?" before defaulting/rejecting —
closing the gap structurally instead of adding one more regex. Keep it a
single additional classifier question, temperature 0, with a safe default.

**Verify:**
- Unit: `tests/test_router.py` full pass (existing routes unchanged).
- Live gold: G3 exact text → no longer bounced; resolves to cross-case scope.
- Live paraphrase: a cue-word-free cross-case question also resolves correctly.

**Depends on:** 15 merged first (shared `router.py`).

---

## Module 9 — first full Gold-32 rerun + report (docs/eval only)

After 8c lands (and ideally 14/15/16), run `evaluation/gold32_run.py` then
`evaluation/gold32_score.py` against the current build. Compare to
`GOLD32_EVALUATION_REPORT.md`'s baseline (0.11 mean FactualCorrectness, 1/24
pass). **Every number traces to a captured output.** Expect KB1/KB3 to move
only if 8b/8c fully landed; report honestly if extraction gaps remain.

---

## Module 18 — second full Gold-32 rerun + updated report (the DeepEval end-step)

Gated on 14/15/16 merged. Re-run the full 32-question live eval, plus the
DeepEval metrics harness (uses Module 19's citation-context fix + Module 20's
judge-tolerance fix, both merged). Report per-bucket mean/pass-rate vs.
baseline (Complex Reasoning 0.10→?, Contextual Summarization 0.06→?, Creative
Generation 0.04→?), and for each root cause note whether a **non-gold
paraphrase** confirmed the fix — capability, not curve-fit. This is the final
"did the metrics turn out good" pass.

---

## Discipline carried through every module

1. Branch off fresh `main`; one module per branch.
2. Unit/regression tests green on touched files — no new failures.
3. Live verify: gold question **and** a non-gold paraphrase, graded
   contextually (same facts, not exact wording).
4. Merge `--no-ff` to `main`, push, delete branch. One module at a time.
5. Report before/after for each module before starting the next.
6. No unverified claim in any writeup — every number from a captured output.
7. Commit as `najiahkhalid`, no Claude co-author trailer.
