# Muhafiz — Gold-QA Fix Implementation Plan (Modules 1–9)

## Where this comes from

`goldtest-eval3` (origin, 5 commits ahead of `main`) added an `evaluation/` folder
and 5 commits of app-code changes meant to fix the low Gold-QA scores documented in
`evaluation/GOLD32_EVALUATION_REPORT.md` (0.11 mean FactualCorrectness, 1/24 pass).
Reviewing that branch in full (code diffs, running the test suite, directly
exercising the router/xagg logic against the exact gold-dataset question text, and
cross-checking every claim in `HANDOFF.md` against the branch's own bundled
evidence) found:

- **The app-code fixes are not on `main` at all** — they exist only on the
  unmerged branch.
- **One of them is a real regression**: removing `"reporting delay"` from the
  trend-keyword list and adding it to a new count-keyword list, checked earlier,
  makes a genuine trend question ("reporting delay trend over time") get
  misrouted into the new count aggregate instead of the honest "not supported"
  message. Confirmed by running the existing test suite:
  `tests/test_xagg.py::test_trend_question_returns_unsupported_aggregate` fails on
  `goldtest-eval3`, passes on `main`.
- **`HANDOFF.md`'s "verified end-to-end" claims are contradicted by its own
  bundled evidence.** `evaluation/gold32_targeted_outputs.json`, committed in the
  same final commit as those claims, shows D1/A1/A7/CP6/CP1's answers
  byte-for-byte identical to the pre-fix baseline, and CR2/S3 still routing to
  XGRAPH, not XAGG as claimed. The underlying code fixes check out correctly when
  tested directly against the exact gold question text — but nothing in the
  branch actually proves they work through the live pipeline.
- **Two ground-truth data problems**: live DB has 79 FIRs vs. the gold answer's
  73; the branch's own draft answer key (`gold32_answer_key.json`) contradicts the
  official one (`Gold_QA_Dataset_Final32.json`) for CP6 — 0 vs. 11.
- **Several root causes acknowledged as not started**: CrPC/legal-PDF chunking
  (blocks ~8 of 32 questions, the single biggest lever), the CP6
  officer-placeholder aggregate, and a missing `BELONGS_TO_CASE` graph edge.

Goal: fix all of this properly, one module at a time, each one **verified against
a live running stack** (not just unit tests) before merging — so the next full
Gold-QA run improves for real, ahead of the demo.

Because `main` doesn't have any of the goldtest-eval3 code yet, the modules below
are **implemented fresh against `main`**, not literally re-applied from that
branch — where a module fixes a root cause that branch also touched, it's built
correctly from the start (e.g. Module 2 never introduces the trend/count keyword
collision in the first place, so there's no separate "regression fix" module).

---

## Working discipline (same for every module)

1. `git checkout main && git pull` → `git checkout -b <module-branch> main`.
2. Implement the module's change only — nothing from another module.
3. Verify — both parts, every module:
   - **Unit/regression tests** relevant to the change, full related test file(s), no new failures.
   - **Live, against the real running stack** (backend on `http://127.0.0.1:8001`, real Postgres/AGE, real model server): send the exact gold-dataset question(s) that module targets through `/api/chat`, read the actual answer, grade it **contextually** against `Gold_QA_Dataset_Final32.json`'s answer for that question — same facts/points covered, not exact wording, per the testing team's own rule.
4. `git checkout main && git merge --no-ff <module-branch>` → `git push origin main` → delete the branch.
5. Report the before/after answer for that module's question(s) before moving to the next module.
6. **Author every commit as `rayyanfaisal475207 <rayyanfaisal475207@users.noreply.github.com>`. No `Co-Authored-By: Claude` trailer on any commit.**

One module at a time, in order — do not start the next module until the current one is merged and pushed.

---

## Module 1 — Ground-truth & data alignment

**Type:** investigation + data, no app-code risk. Done first because it decides the
correct target numbers Modules 2/4/7 verify against.

**Branch:** `chore/gold-qa-ground-truth-alignment`

**What:**
- Determine why the live DB has **79** FIRs while the gold answer / Muhafiz Data
  API snapshot says **73** — a 6-record gap, not the 1-record edge issue below.
  Query the running DB and the live Muhafiz API directly; document which is
  authoritative for grading going forward (resync one to the other, or record
  "accept either" with a stated reason).
- Resolve the CP6 conflict: the branch's own draft `gold32_answer_key.json` says
  **0** FIRs have no real investigating officer; the official
  `Gold_QA_Dataset_Final32.json` says **11**. Count placeholder-officer
  (`"(Naamzad ASI/SI)"`) FIRs directly in the live graph to confirm which is
  right, and correct the stale draft key.
- Investigate the pre-existing 72-vs-73 Incident/Case gap (one Incident missing a
  `BELONGS_TO_CASE` edge) and backfill it if a script for that already exists
  (check `scripts/` for a `backfill_missing_belongs_to_case`-shaped script).

**Verify:** the corrected numbers are written into a short `evaluation/GROUND_TRUTH_NOTES.md` (or an update to the answer key) that Modules 2, 4, and 7 will cite when grading D1, A7, and CP6.

---

## Module 2 — XAGG reporting-delay count aggregate (RC4, question A7)

**Branch:** `feature/xagg-reporting-delay-count`

**Question targeted:** A7 — "Kitne cases mein mudai ne police ke paas waqe ke kuch
arse baad aane ki koi wajah batai...?" (gold: 8 of 73/79 FIRs recorded a
reporting-delay reason).

**Files:**
- `src/graph/structured_projection.py` — project `reporting_delay_reason` onto the
  Incident node in `project_fir()` (mirrors how gender/age were added).
- `src/pipeline/xagg.py` — new `_reporting_delay_count()` aggregate, wired into
  `run_aggregate()`'s keyword routing. **Built trend-safe from the start**: a
  reporting-delay *count* phrasing routes here; a reporting-delay *trend*
  phrasing ("... trend over time") must still fall through to the existing
  honest "trend not supported" path. Add both keyword sets with that
  precedence/exclusion designed in up front — this is the exact bug
  `goldtest-eval3` shipped; do not reproduce it.
- `src/pipeline/harness/tools/xagg.py` + `src/pipeline/orchestrator.py` (both
  rendering sites) — render the new `reporting_delay_count` kind.
- Re-run the offline backfill for the FIR endpoint (idempotent,
  `127.0.0.1`-only) so the property actually exists on the graph before verifying.

**Verify:**
- New unit tests: A7's count phrasing → `_reporting_delay_count`; a trend
  phrasing containing "reporting delay" → still `unsupported_aggregate`
  (this is the regression-proofing test).
- `tests/test_xagg.py` full pass.
- Live: A7's exact text via `/api/chat` → states a real count (not "does not
  specify"); grade against Module 1's resolved ground truth.

---

## Module 3 — Router: person-recurrence questions → XAGG (RC3, questions CR2/S3)

**Branch:** `fix/router-xagg-person-recurrence`

**Question targeted:** CR2 (English, "resurfaced as a suspect in a newer case"),
S3 (Urdu, "کیا کسی شخص کو ایک سے زیادہ بار گرفتار کیا گیا ہے؟").

**Files:** `src/pipeline/router.py` — add narrative + Urdu/Roman-Urdu
person-recurrence patterns to `_XAGG_OVERRIDE_PATTERNS`, checked ahead of the LLM
classifier, so these deterministically hit XAGG's already-correct
`graph_recurrence` path (which names people with their specific cases) instead of
XGRAPH (which, for a broad no-named-seed query, can only report a flat case-ID
union by design).

**Verify:**
- `tests/test_router.py` full pass (no existing route regresses).
- Direct call to `_deterministic_route_override()` with CR2's and S3's exact
  gold-dataset text → must return `XAGG`.
- Live: send CR2 and S3's exact text to the running `/api/chat`, confirm the
  actual route is `XAGG` and the real answer names the recurring person(s) with
  their FIR numbers — grade contextually against gold.

---

## Module 4 — XAGG grand-total for "how many FIRs" (RC2, question D1)

**Branch:** `fix/xagg-fir-grand-total`

**Question targeted:** D1 — "How many FIRs are currently registered?"

**Files:**
- `src/pipeline/router.py` — "how many FIRs" patterns in
  `_XAGG_OVERRIDE_PATTERNS`.
- `src/pipeline/xagg.py` — FIR phrasing added to `_TOTAL_KEYWORDS`, so this
  resolves to `_total_count()` (a plain number) instead of the per-statute
  breakdown default.
- `src/pipeline/verifier.py` — `verify_structured_aggregate_paraphrase()`: allow a
  stated grand total that equals the sum of the source's own per-category
  counts, **narrowed** to a single recognized breakdown group (not a blind sum of
  every "N cases/FIRs" occurrence in the whole source text) — this closes the
  fabrication-guard hardening gap identified during review, built in from the
  start rather than shipped loose.

**Verify:**
- `tests/test_verifier.py`, `tests/test_xagg.py`, `tests/test_router.py` all pass.
- New verifier test: a source with two unrelated breakdown groups does not let a
  coincidental cross-group sum pass.
- Live: D1's exact text via `/api/chat` → states a clean total (not a statute
  breakdown, not a "raw computed aggregate" fallback), matching Module 1's
  resolved FIR count — grade contextually.

---

## Module 5 — Evaluator: compound-question relaxation (RC1)

**Branch:** `fix/evaluator-compound-question-relaxation`

**Question targeted:** KB1 and similar compound legal+data questions.

**Files:** `prompts/evaluator.txt` — the relevance evaluator returns TRUE when
retrieved documents answer at least the primary/answerable part of a compound
question, instead of hard-rejecting because a secondary part (e.g. "does our
recordkeeping follow it") isn't covered by the legal corpus.

**Verify:**
- Live: KB1's exact text via `/api/chat` — confirm the evaluator stops
  hard-rejecting and a substantive primary-part answer comes back (note: full KB1
  correctness on the legal content itself is gated on Module 8's chunking fix —
  this module only confirms the evaluator no longer over-rejects).

---

## Module 6 — XGRAPH: answer-first summary phrasing

**Branch:** `fix/xgraph-summary-answer-first-phrasing`

**Files:** `src/pipeline/harness/agents/cross_case_linkage.py` —
`_xgraph_summary_line()` leads with "Yes — ..." / "Yes (with some uncertainty) —
..." instead of a debug-log-style "Entity-graph search found connections...".

**Verify:**
- `tests/test_harness_agent_cross_case_linkage.py` full pass.
- Live: since Module 3 already moved the person-recurrence question shape to
  XAGG, exercise this path with a genuinely XGRAPH-shaped query (a named
  CNIC/entity traversal) and confirm the phrasing reads as a direct answer.

---

## Module 7 — CP6 placeholder-officer count (RC4 part 2)

**Branch:** `feature/xagg-placeholder-officer-count`

**Question targeted:** CP6 — "Kitne cases abhi tak bina kisi tafteeshi afsar ke
asal tor par muqarrar kiye pade hue hain?" (gold, per Module 1's resolution: 11).

**Files:** `src/pipeline/xagg.py` — new `_placeholder_officer_count()`, same shape
as Module 2's aggregate: counts Cases whose current (non-superseded)
investigating Officer name matches the `"(Naamzad ASI/SI)"` placeholder pattern;
wired into routing keywords and all 3 rendering sites (`xagg.py`,
`harness/tools/xagg.py`, `orchestrator.py`).

**Verify:**
- New unit test for the aggregate function.
- Live: CP6's exact text via `/api/chat` → states the Module-1-confirmed number —
  grade contextually.

---

## Module 8 — CrPC / legal-PDF structure-aware chunking (RC5 — highest ceiling)

**Branch:** `feature/crpc-structure-aware-chunking`

**Problem:** the CrPC 1898 PDF was ingested with broken fixed-size chunking —
2,360 chunks, only 5 mention "154" — so the actual §154 statutory text ranks
poorly for its own topic and KB legal questions retrieve the wrong law. This
blocks all ~8 currently-excluded KB questions, the single biggest score lever
in the dataset.

**Scope note:** larger and more open-ended than Modules 1–7 (ingestion-pipeline
work, not a small app-code patch) — gets its own focused exploration/sub-plan
once Modules 1–7 are merged, rather than being fully speced here. At minimum:
re-ingest the CrPC (and other legal PDFs) with section-boundary-aware chunking
instead of fixed-size windows, drop/down-weight table-of-contents/index pages,
optionally tag chunks with a `section` metadata field.

**Verify:**
- A direct vector query for "Section 154" surfaces real statutory text, not
  table-of-contents fragments.
- Live: run all 8 previously-excluded KB questions through `/api/chat`, grade
  contextually against gold.

---

## Module 9 — Full Gold-32 re-run and final honest report

**No app-code branch** (a `docs/` branch for the report only).

Run `evaluation/gold32_run.py` then `evaluation/gold32_score.py` for real against
the fully-fixed, fully-verified build (Gemini flash-lite judge, "close numbers /
contextual coverage OK" rule). Compare against `GOLD32_EVALUATION_REPORT.md`'s
baseline (0.11 mean FactualCorrectness, 1/24 pass) and write an evidence-backed
after-report — every claim in it backed by an actual captured output, not
asserted the way `HANDOFF.md`'s claims turned out not to be.

---

## Environment (already confirmed working this session)

- Docker Desktop + `docker compose up -d postgres` — attaches to the existing
  `rag-chatbot_pgdata` volume (real, already-migrated data, not empty).
- Remote model server (`MODEL_SERVER_BASE_URL`, ngrok tunnel) — already up and
  healthy (`/health` → `{"status":"ok"}`).
- Real Groq/Gemini/Muhafiz-API keys already in `.env`.
- `scripts/create_admin.py` creates/promotes `admin@example.com` /
  `MuhafizAdmin2026!` — matches `evaluation/gold32_run.py`'s own default
  fallback login.
- Backend: `uvicorn src.main:app --reload --port 8001`.
