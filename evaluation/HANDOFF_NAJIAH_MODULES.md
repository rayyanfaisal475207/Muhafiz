# Handoff — Najiah's Gold-QA Modules (8c, 14, 15, 16, 8d, 9) → next owner

**Branch:** `najiah/gold-qa-modules-8c-14-15-16` (pushed to origin)
**Base:** off `main` @ `fd09fa5`
**Author:** najiahkhalid (commits are under her identity, no Claude co-author
trailer — her call as the contributor)
**One-line status:** All assigned fix modules are **done, tested, committed,
and pushed**; the app scores substantially better; one operational step (a KB
re-ingest that this machine's RAM can't run) is left before the KB bucket can
score.

---

## TL;DR for the reviewer

- **8 commits** on the branch (listed below). Open a PR from
  `najiah/gold-qa-modules-8c-14-15-16` → `main` (main is protected and needs
  the "Backend tests" CI check — that's why these are on a branch, not pushed
  to main directly).
- **Gold-QA metrics jumped**: FactualCorrectness **0.11 → 0.39** (all 32), **→
  0.53 excluding the KB bucket**; pass-rate **1/24 → 13/24**. Fact Retrieval
  0.23→0.98, Complex Reasoning 0.10→0.59, Creative Generation 0.04→0.38.
  (Evidence: `MODULE_9_RERUN_REPORT.md`, every number from a captured output.)
- **The one open item** is fully documented in `MODULE_8D_SPEC.md`: the actual
  root-cause bug is **fixed and committed** — only the live KB re-ingest
  remains, and it needs a machine with more RAM than this one.

---

## What shipped, module by module

### Module 8c — KB legal-question scoping + robust large-PDF extraction
Commits: `11f31c9`, `a0f8275` (+ `8397eb3` docs)
- **Retrieval scoping** (`rag.py`): legal-KB-intent questions search the KB
  corpus first instead of being out-ranked by case narratives in All-Cases
  mode. English/Urdu/Roman-Urdu, safe fallback to the mixed pool.
- **OCR-aware ingestion** (`pdf_loader.py`): text-layer PDFs extract with OCR
  off (correct + ~100× faster); PyMuPDF text-layer fallback; blank-page drop;
  graceful vision-failure so one bad page can't abort a whole file.
- **Batch size 80→32** (Docling drops content on large page ranges).
- Tests: rag + pdf-loader + chunker suites pass.

### Module 14 — CR7 criminal-record × court-outcome cross-check
Commit: `fd75923`
- New `_criminal_record_court_crosscheck()` in `xagg.py`: status breakdown +
  a consistency cross-check joining criminal records to court outcomes on the
  FIR number (normalized from each system's own format).
- **Verified live**: 33 records, 30 "Under trial" + 1 "Convicted, on bail
  pending appeal"; FIR 891/24 cross-checks CONSISTENT. Scored **0.9** in the
  Module 9 rerun.

### Module 15 — field-consistency cross-record aggregates (CR6, CR8, G2, G5)
Commit: `606c19e`
- **CR6** `_cms_fir_linkage`: walk-in CMS complaints ↔ FIR (all 4 link). **1.0**
- **CR8** `_dv_report_fir_match`: DV reports ↔ FIR (4 of 8 confirmed). **1.0**
- **G2** `_case_completeness_scan`: cases with weak records (73 cases, 9
  missing incident date — excludes leftover CASE-TEST-* rows so it's correct
  regardless of DB hygiene). **0.4** (see "honest scope note" below).
- **G5** `_weapon_compliance_scan`: 30 of 32 weapons (~94%) unlicensed. **0.5**
- Router: a field-consistency override family so these route to XAGG instead
  of falling to RAG ("Retrieval failed").
- **RC-6 note**: no longer relevant for these — they don't route through RAG
  at all anymore.

### Module 16 — G3 court-readiness scan + RC-4 confirmation
Commit: `0128a42`
- **G3** `_court_readiness_scan`: combines 3 court-readiness signals —
  accused-relationship blank (81/93), weapon licence missing (2/32), incident
  date missing (9). Reproduces all gold figures. Scored **1.0**.
- **RC-4 finding**: the plan's "LLM cross-case-scope fallback" is **already in
  main** via Root Cause 1's upstream fix (route_query prepends `ACTIVE_CASE:
  none`, tested at `test_router.py:409`). Module 16 added G3's aggregate + route.

### Module 9 — first full Gold-32 rerun (docs/eval)
Commit: `d313a60` — `MODULE_9_RERUN_REPORT.md` + captured
`gold32_pipeline_outputs.json` / `gold32_results.json`.
- All 32 questions run through the **live** harness; scored with the Gemini
  judge. All 6 of Najiah's questions work end-to-end via live HTTP (not just
  in-process).

### Module 8d — the KB root-cause bug: FIXED (code), live re-ingest deferred
Commit: `21c53ca`
- **The real bug**: the text splitter silently **dropped ~448-char spans** of
  dense text — §154's body fell in one, which is why KB1 never worked despite
  8b/8c. Fixed by capping the advance so no text is skipped. General
  correctness fix (affected the whole corpus, not just legal PDFs). 11 chunker
  tests pass.
- **What's left**: re-ingest the KB PDFs so the fixed chunker's output reaches
  Chroma, then verify KB1 live. **Blocked on this machine by RAM** (the
  319-page Docling pass OOMs) and a Chroma store now in a bad state from
  processes killed mid-write. **Full recovery + re-ingest steps are in
  `MODULE_8D_SPEC.md` §"To finish 8d on a stronger machine".**

---

## Honest caveats (so nothing surprises you)

1. **KB bucket still 0.00** — entirely the 8d live-re-ingest item above. The
   *fix* is shipped; only the re-ingest is pending. On adequate hardware it's
   mechanical (MODULE_8D_SPEC.md steps 1–5).
2. **Contextual Summarization ≈ 0.02** (M1/M2/M4/M5/M7) and a few Creative
   Generation zeros (CR3/CR4/CS4/G1/G6) — these are the XNETWORK/Meta-Analysis
   broad-synthesis questions (RC-1/RC-5, **Modules 11–13 territory**, not
   Najiah's assigned modules). They show "no connections found" / "could not
   be verified". This is the largest remaining headroom after 8d.
3. **G2/G5 at 0.4–0.5** — documented honest-scope calls, both with high
   AnswerRelevancy. G2: the graph only projects a per-FIR zimni *index*, not
   entries, so the "which FIRs lack zimni" signal isn't reliably queryable and
   was deliberately not fabricated (the judge itself credited the incident-date
   part and docked only the omitted zimni point). G5: the deterministic scan
   reports the core 30/32 fact; the Creative gold lists extra angles.
4. **Chroma store health** — `data/chroma_db` currently hangs on queries (~9
   orphaned collection-segment dirs from killed re-ingest processes). Rebuild
   it before the re-ingest (MODULE_8D_SPEC.md §1).
5. **Test-account password** — the eval admin (`admin@example.com`) password was
   reset to the documented `MuhafizAdmin2026!` for the eval run (synthetic
   account; `scripts/create_admin.py` only promotes role, doesn't reset an
   existing user's password — flagged here so you know why).

---

## Environment / how to run the eval (from `MODULE_9_RERUN_REPORT.md`)

- Postgres: `docker compose up -d postgres` (container `muhafiz-postgres`,
  volume `pgdata` — data persists).
- Backend: `PYTHONPATH=. .venv/Scripts/python.exe -m uvicorn src.main:app
  --host 127.0.0.1 --port 8001`.
- Model server: `MODEL_SERVER_BASE_URL` ngrok tunnel (check `/embed` responds).
- Eval: `EVAL_ADMIN_EMAIL=admin@example.com EVAL_ADMIN_PASSWORD=MuhafizAdmin2026!
  python evaluation/gold32_run.py` (resumes from existing output), then
  `python evaluation/gold32_score.py` (Gemini judge, `gemini-flash-lite-latest`,
  key in `.env` GEMINI_API_KEY). `gold32_run.py` reads
  `Gold_QA_Dataset_Final32_With_Answers.json`.

---

## Suggested next steps for you

1. **Open the PR** for this branch → `main` (CI needs "Backend tests").
2. **Finish 8d** on a machine with more RAM: rebuild Chroma → re-ingest the 7
   KB PDFs (fixed chunker is already in the code) → verify KB1 live
   (MODULE_8D_SPEC.md).
3. **Module 18** (final eval rerun): once 8d lands, re-run the 32-question eval
   — the KB bucket should move off 0.00, and the headline should climb further.
4. **Optional, biggest remaining headroom**: the Contextual-Summarization /
   broad-synthesis bucket (Modules 11–13 / RC-1 follow-ups) — teammate
   territory, not blocked by anything Najiah shipped.

## Reference docs on the branch
- `MODULE_9_RERUN_REPORT.md` — the eval numbers, per-type, honest caveats.
- `MODULE_8D_SPEC.md` — the root-cause writeup + exact re-ingest recovery steps.
- `NAJIAH_MODULES_IMPL_PLAN.md` — the original per-module implementation plan.
- `gold32_pipeline_outputs.json` / `gold32_results.json` — captured run + scores.
