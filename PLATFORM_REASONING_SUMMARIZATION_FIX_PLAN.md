# Muhafiz — Platform Reasoning/Summarization/Creative-Synthesis Fix Plan (Modules 10+)

## Framing — this is not a "pass 18 more gold questions" plan

`GOLD_QA_FIXES_IMPLEMENTATION_PLAN.md` (Modules 1–9) fixed narrow, mostly
one-question-shaped bugs: a missing aggregate, a router pattern, a verifier
edge case. That covered ~13–14 of the 32 gold questions. The other 18 — 1 Fact
Retrieval (A1), 7 of 8 Complex Reasoning, all 5 Contextual Summarization, all 5
Creative Generation — were never in scope, and the baseline report
(`GOLD32_EVALUATION_REPORT.md`, on `goldtest-eval3`, not merged) already
flagged these three buckets as the app's weakest (0.10 / 0.06 / 0.04 mean,
0/8, 0/5, 0/5 pass) without diagnosing why.

This plan diagnoses why, using the actual captured answers from that baseline
run (`gold32_pipeline_outputs_FULL.json`), and the goal is stated deliberately
in capability terms, not question terms: **fix the handful of real platform
gaps these 18 questions happen to expose**, because those same gaps degrade
*any* compound, comparative, or open-ended question a real investigator asks —
gold32 is the test harness we verify against, not the target. A module here
is done when the underlying capability works, checked against the gold
question that exposed it **and** at least one paraphrase/variant that isn't
in the dataset, so we're not just curve-fitting to 32 fixed strings.

---

## What the evidence actually shows (read before assuming these are 18 separate bugs)

I read every one of the 18 questions' actual captured answers, not just the
scores. They collapse into **six root causes**, most shared across multiple
questions and multiple buckets — this is good news: it's ~6 fixes, not 18.

### RC-0 — The one sub-agent built for exactly this job never fires

`src/pipeline/harness/agents/meta_analysis.py` exists specifically to
decompose a compound cross-case question into standalone sub-questions,
re-run each through the Supervisor, and synthesize across the answers —
which is precisely the shape of every Complex Reasoning, Contextual
Summarization, and Creative Generation question in this set ("compare X vs
Y", "review the caseload and flag...", "does A match B"). **Not one of the
18 questions routed to `META_ANALYSIS`** in the captured baseline — they all
landed on `XAGG`, `XGRAPH`, `XNETWORK`, `RAG`, or `GRAPH_HYBRID`, none of
which can compose multiple sub-answers into one synthesized response. Either
`_META_ANALYSIS_TRIGGER_PATTERNS` / the `case_scope="cross_case"` gate in
`supervisor.py::classify_to_subagent()` is too narrow to catch this phrasing,
or something upstream (the router) is claiming these queries before the
supervisor ever gets to consider Meta-Analysis. This is the highest-leverage
single fix candidate — confirm live in Module 10 before assuming it.

### RC-1 — Broad cross-case questions silently fall back to XNETWORK's raw community-cluster dump

CR3, CS4 (partially — via XGRAPH's own cluster narration), M4, G1, G6 all come
back as a wall of "This cluster centers on FIR X... co-mentioned with..."
text lifted straight from `global_search`'s community summaries, with no
relation to what was actually asked. Example — G1 asked to flag *unusual
patterns* (offender age spread, stranger-vs-known relationships, property/
fatality mismatches, time-of-day skew); the actual answer instead named
"cross-regional cybercrime coordination" and "high co-accused count" purely
because those were the community clusters nearest the query embedding.
**XNETWORK is answering the question "what communities exist near this text"
instead of the question actually asked.** This is a routing/fallback defect,
not a missing-capability one: a broad, comparative, or evaluative question
with no named entity is getting caught by XNETWORK's/XGRAPH's own trigger
patterns ahead of anything that would actually reason over the specific ask.

### RC-2 — XAGG's aggregate library is flat-count-only; it has no rate, cross-tab, or time-bucketed primitive

CP1 (weapon-recovery **rate** per district, not raw count), M1 (statute mix
**this year vs 2024**), M2 (station-**type**-normalized caseload, not a
station list), M7 (mean reporting-delay **by year**), CR7 (criminal-record
**status breakdown + cross-check against court outcomes** — no such aggregate
exists at all) all hit the same wall: `xagg.py` can group-count one field and
total one field, but has no "group by X, divide by group's own case count",
no "same aggregate for period A and period B, diffed", and no "join this
aggregate against a second record type". Modules 2/4/7 each added one new
*flat* aggregate function one at a time — that pattern doesn't scale to this
bucket; it needs one **general derived-aggregate primitive** (a rate/ratio
helper and a time-bucket helper that any aggregate can be composed with),
not five more bespoke functions.

### RC-3 — Narrative RAG is asked structured-consistency questions it structurally cannot answer

CR6, CR8, G2, M5, G5 all ask some version of "does field A on record-type 1
match/confirm field B on record-type 2" (walk-in-complaint tag ↔ FIR number;
forwarded-FIR field ↔ real FIR table; incident date presence; dispatch-time
ordering; license-status rate). RAG retrieves narrative chunks and can only
speak to what one chunk happens to say — it has no way to iterate every row
and check a field-level match. Two of these (M5, G5) come back as an outright
retrieval failure ("No sufficiently relevant documents found") because the
question has no narrative chunk that "matches" it at all — the honest data
lives in structured columns, not prose. **These are XAGG/graph-join
questions misrouted to RAG**, the same class of problem Module 3 already
fixed for person-recurrence questions (CR2/S3) — this is that same fix,
generalized to a wider trigger set (field-consistency / cross-reference
phrasing), not a new mechanism.

### RC-4 — The router's cross-case trigger set is a fixed phrase list, so it silently rejects unseen phrasings

G3 ("what's most likely to be flagged as incomplete") gets bounced outright:
*"This question needs either a specific case selected... or rephrasing as a
cross-case question (e.g. '...across cases', '...in total', 'which
cases...')"* — the question **is** a valid cross-case question, it just
doesn't contain the exact cue words the router's regex list expects. Any
question that's obviously about the whole caseload but phrased another way
hits the same wall. This is a coverage gap in a hand-maintained pattern list,
the same shape of gap Modules 2/3/4 each patched with one more regex — worth
fixing generally this time (an LLM-based cross-case-scope fallback classifier
behind the regex fast-path, not another growing list) since regex-only
coverage will keep failing on real investigator phrasing forever.

### RC-5 — The claim-verifier over-rejects true claims phrased differently from the retrieved text, then substitutes a raw dump

CR4 (XGRAPH rejects its own correct citation because the cluster-summary
text doesn't literally restate the fact) and M2/M7 (XAGG's own
"could not be verified as an accurate paraphrase; showing the raw computed
aggregate instead" fallback) are the same failure mode already flagged for
D1 in the original baseline report and only partially closed. This is a
verifier-strictness problem, not a data or routing problem — it produces a
correct underlying answer that a downstream check throws away.

### RC-6 — (found by Module 10's live sweep, not in the original diagnosis) RAG's retry-and-refine path fails silently instead of degrading gracefully

CR6, CR8, M5, G2, G5 — all 5 RAG-routed questions in this set — now come back
with either `status: "error"` / *"Document search failed; no answer could be
produced."* or `route: null` / *"I couldn't find sufficient information in
the knowledge base... ensure the relevant documents have been ingested"* — a
message shaped for a genuinely-missing-document case, on questions whose
underlying data plainly exists in the ingested corpus. This is worse than the
hedged-but-substantive prose the stale baseline trace showed for the same
questions, so it needed its own investigation (Module 10.2) before assuming
it's just "RAG can't do joins" (RC-3) — see that module for why.

---

## Working discipline (same as Modules 1–9 — see that document for the full text)

One module at a time: branch from `main`, implement, run the relevant unit
tests, then verify **live** against the running stack (`/api/chat`,
platform-admin, All Cases) — for every module here, verify against **both**
the gold question(s) that exposed the root cause **and** at least one
free-form paraphrase that is *not* in `Gold_QA_Dataset_Final32.json`, since
the point is the underlying capability, not the fixed string. Merge, push,
report before/after, move on. Author commits as `rayyanfaisal475207`, no
Claude co-author trailer (per the existing plan's stated discipline for this
work).

---

## Module 10 — Live reconfirmation sweep (no code changes) — ✅ DONE (2026-09-05)

**Why first:** the diagnosis above is built from `gold32_pipeline_outputs_FULL.json`,
captured on the unmerged `goldtest-eval3` branch, *before* Modules 1–7 (router/
XAGG/verifier changes) landed on `main`. Some symptoms may have already
shifted. Don't design Module 11+ against a stale trace.

**What:** ran all 18 questions (A1, CR3/CR4/CR6/CR7/CR8/CS4/CP1, M1/M2/M4/M5/
M7, G1/G2/G3/G5/G6) through the current `/api/chat` on this branch
(`feature/crpc-structure-aware-chunking`, Module 8 in flight but file-disjoint
from everything here), via `evaluation/module10_run.py`. Raw output in
`evaluation/module10_untouched_buckets_outputs.json`; full per-question
writeup in `evaluation/UNTOUCHED_BUCKETS_DIAGNOSIS.md`.

**Result:** RC-0 through RC-5 all reconfirmed exactly as scoped above, zero
regressions from Modules 1–7. One new failure mode found, not in the original
diagnosis — **RC-6** (added above) — plus a standalone data-quality finding
on A1. Both get their own short module below before the fix modules that
depend on them.

---

## Module 10.1 — A1 ground-truth check — ✅ DONE (2026-09-05)

**Branch:** `chore/gold-qa-a1-accused-count-alignment`

**Result:** not a data gap after all — a code bug, and a precisely
pinpointed one. Queried the live graph directly:
`INVOLVED_IN{role:"accused"}` **edges** (row-level, one per `fir_accused`
mention) total **94**, gender-split **67 M / 24 F / 3 unknown** — matches gold
**exactly**. `xagg.py::_gender_breakdown()` instead counts **distinct Person
nodes** (`seen_entities` keyed by `entity_id`), which silently collapses the
2 accused entries belonging to the two recidivist identities already known
from CR2/S3 (شہزیب عرف شابی, عاصم رشید — each accused in two separate FIRs)
down to 1 each, losing exactly 2 from the male count and total. Fix is a
one-line change: count edges (or group by `p.gender` over `r`), not
`DISTINCT p`. Full writeup + the exact query results in
`evaluation/GROUND_TRUTH_NOTES.md` §4 — ready for Module 13 to consume
directly, no further ground-truth work needed.

---

## Module 10.2 — RC-6 root-cause isolation — ✅ DONE (2026-09-05), fix not yet applied

**Result:** confirmed real for 3 of 5, not transient, not a broken retrieval
function. Re-ran all 5 in isolation:
- **M5, G5 now succeed** — both routed to **XAGG** this time (not RAG),
  returning real answers. Same query text, no code change — the route
  classification for these two is **non-deterministic run-to-run**. Folded
  into Module 11/16's scope below (both touch this same classification path).
- **CR6, CR8, G2 fail identically both times** — RAG route, "Retrieval
  failed" both runs. Traced the swallow point:
  `src/pipeline/harness/tools/rag.py:362-364` catches whatever
  `_retrieve_candidates()` raises and replaces it with a generic message —
  the real exception never reaches the API response. **Called
  `_retrieve_candidates()` directly for the same 3 queries, bypassing the
  live server — all 3 succeeded instantly**, so the retrieval logic itself
  isn't broken. The failing live calls took 71–99s before erroring (vs.
  instant direct), consistent with RAG's retry-with-refinement loop's
  repeated embedding calls hitting a connection-pool/timeout issue under
  load in the long-running server process — not pinned to the exact
  exception, since that requires restarting the dev backend with output
  captured to a file (deliberately not done in this module — that's a
  go/no-go call, not a diagnostic one). Full writeup in
  `evaluation/UNTOUCHED_BUCKETS_DIAGNOSIS.md`.

**Carried into Module 15:** (1) stop swallowing the exception —
surface `ToolError.message` instead of the generic string; (2) restart the
backend with `2>&1 | tee` before re-attempting CR6/CR8/G2 to get the actual
exception/traceback before writing the fix.

---

## Module 10.3 — Merge the Gold-32 eval harness itself onto `main` (script-only, no app code)

**Branch:** `chore/merge-gold32-eval-harness`

**Why:** neither the original Modules 1–9 plan's Module 9 nor this plan's
Module 18 can actually run today — `evaluation/gold32_run.py`,
`gold32_score.py`, and `Gold_QA_Dataset_Final32_With_Answers.json` only exist
on the unmerged `goldtest-eval3` branch; `main` only has the older 17-query
DeepEval harness. Cherry-pick just those 3 files (plus
`GOLD32_EVALUATION_REPORT.md` for the historical baseline record) onto
`main` — no app-code changes ride along.

**Verify:** `gold32_run.py` runs end-to-end against the live stack and
produces output for all 32 questions without modification.

---

## Module 11 — Route compound/comparative/creative questions to Meta-Analysis (RC-0)

**Branch:** `fix/router-meta-analysis-trigger-coverage`

**Files:** `src/pipeline/router.py` (or wherever `case_scope`/route
classification happens ahead of the supervisor — confirm exact site in
Module 10), `src/pipeline/harness/supervisor.py::classify_to_subagent()`,
`src/pipeline/harness/agents/meta_analysis.py`'s own trigger patterns/prompt.
**Also investigate:** Module 10.2 found M5/G5 classify to a different route
(RAG vs. XAGG) on identical repeated runs with no code change in between —
if this route classifier has a non-deterministic (LLM-based, no cache) step
in its decision path, that instability should be fixed here too, since it
affects whether Meta-Analysis ever gets a stable shot at these queries
either.

**What:** determine (live tracing, Module 10) exactly why these 18 never
reach `META_ANALYSIS` — narrow trigger patterns, an earlier route claiming
the query first, or `case_scope` not resolving to `cross_case` for these
phrasings — and fix that gate. This is the highest-leverage module: if
Meta-Analysis correctly decomposes "review the caseload and flag anything
unusual" into sub-questions like "what's the age range of accused persons",
"what fraction of accused-complainant relationships are 'stranger'", etc.,
and synthesizes across their (already-correct, tool-verified) sub-answers,
it should materially move most of G1/G2/G3/G6, M1/M2/M4/M5/M7, and several of
the CR questions at once — they were never actually missing an *answer*, they
were missing a *dispatcher* that would ask the right narrow questions on
their behalf.

**Verify:**
- `tests/test_harness_supervisor.py`, meta-analysis's own test file, full
  pass.
- Live: G1's exact text (and a paraphrase, e.g. "Is there anything about this
  caseload a supervisor should be worried about?") → routes to
  `META_ANALYSIS`, decomposes, and the synthesized answer names specific,
  correct sub-facts (age range, stranger-relationship skew, etc.), not a
  community-cluster dump.

---

## Module 12 — Stop XNETWORK/XGRAPH from substituting a raw cluster dump for broad reasoning questions (RC-1)

**Branch:** `fix/xnetwork-xgraph-broad-query-guard`

**Files:** `src/pipeline/router.py` (`_XGRAPH_OVERRIDE_PATTERNS` and
whatever currently lets a no-named-entity, evaluative/comparative query fall
through to `XNETWORK`/`XGRAPH`), `src/pipeline/xnetwork.py`,
`src/pipeline/harness/agents/cross_case_linkage.py`.

**What:** this is a backstop for whatever Module 11 doesn't already catch —
some cross-case query will still land on XNETWORK/XGRAPH legitimately (a real
named-entity network-mapping ask), and for those, the community-cluster
narration must stay scoped to clusters that actually relate to the query
(embedding proximity to the *question*, not just "nearby clusters exist"),
and must say so plainly when nothing relevant is found rather than reciting
unrelated clusters as if they were the answer. Add a relevance gate before
the cluster narration is allowed to stand in as the final answer.

**Verify:**
- `tests/test_harness_agent_cross_case_linkage.py`, `tests/test_xnetwork.py`
  (or equivalent) full pass.
- Live: send a query designed to have no genuinely relevant cluster and
  confirm the app says so, instead of narrating unrelated clusters as fact.

---

## Module 13 — General derived-aggregate primitives: rate/ratio and time-bucket (RC-2), + A1's gender-ratio aggregate

**Branch:** `feature/xagg-derived-aggregate-primitives`

**Files:** `src/pipeline/xagg.py` — add two composable primitives instead of
more one-off functions:
1. A **rate/ratio helper**: given any existing group-by-count aggregate,
   compute `count(subset) / count(group total)` per group (powers CP1 —
   weapons recovered ÷ total cases, per district).
2. A **time-bucket helper**: given any existing aggregate, partition by a
   date field (year, from `incident_date`/`report_date`) and return the same
   breakdown per bucket, diffable (powers M1's 2024-vs-now statute mix, M7's
   year-over-year mean reporting-delay, M5's year-over-year weapon/statute
   co-occurrence).
Wire both into routing keywords and all rendering sites, same discipline as
Modules 2/7. Also add A1's accused-gender-ratio aggregate here — same flat-
count shape as D1/CP6, no new primitive needed, just built against Module
10.1's resolved denominator instead of the stale one.

**Verify:**
- New unit tests for both primitives directly, plus one for each of CP1/M1/
  M7/M5 built on top of them, plus one for A1's gender-ratio aggregate.
- `tests/test_xagg.py` full pass.
- Live: CP1, M1, M7, M5, **A1**'s exact text via `/api/chat` → each states the
  real derived number/comparison (not a flat count, not "additional data
  would be required") — grade contextually against gold (A1 against Module
  10.1's resolved count).
- One non-gold paraphrase per primitive (e.g. "what fraction of Faisalabad's
  cases involve a firearm?") to confirm it generalizes.

---

## Module 14 — CR7's criminal-record-status × court-outcome cross-check aggregate

**Branch:** `feature/xagg-criminal-record-court-crosscheck`

Built on Module 13's primitives (a group-by-status count, cross-referenced
against the separate court-outcome record by case ID). Scoped separately
from Module 13 because it also needs a genuine cross-record-type join (not
just a bucketed single-table aggregate), which is closer to Module 3's
person-recurrence join pattern than to a plain XAGG breakdown — confirm the
right home for this (XAGG vs. a small new harness tool) during Module 10.

**Verify:** live CR7 exact text → states the real status split and the
match/mismatch finding for the one case with both records — grade
contextually.

---

## Module 15 — Field-consistency / cross-reference questions: route to XAGG/graph joins, not RAG (RC-3, + RC-6 if Module 10.2 points here)

**Branch:** `fix/router-field-consistency-to-xagg`

**Depends on:** Module 10.2's finding. If RC-6 turned out to be a real
exception in RAG's retry path (not transient infra), fix that directly here
too (or note it's fixed as a side effect of these questions no longer routing
through RAG at all) — don't merge this module without re-checking Module
10.2's written finding first.

**Files:** `src/pipeline/router.py` — generalize Module 3's person-recurrence
override into a broader "does record-type A's field match record-type B" /
"is X confirmed by the case record" trigger family (English, Urdu, Roman
Urdu), covering CR6/CR8/G2/G5's shape; `src/pipeline/xagg.py` or a new small
harness tool for the actual per-row field-match check (walk-in-complaint tag
↔ FIR; forwarded-FIR field ↔ real FIR; incident-date presence rate;
dispatch-time-before-report-time anomaly count; license-status rate).

**Verify:**
- `tests/test_router.py` full pass.
- Live: CR6, CR8, G2, G5's exact text → each states the real per-record
  match/mismatch count (e.g. "4 of 8 forwarded-FIR fields match a real,
  active FIR"), not an abstention, a generic narrative answer, or RC-6's
  "document search failed" — grade contextually.

---

## Module 16 — Cross-case scope: LLM fallback behind the regex fast-path (RC-4)

**Branch:** `fix/router-cross-case-scope-llm-fallback`

**Files:** `src/pipeline/router.py` — when no `_XAGG_OVERRIDE_PATTERNS` /
`_XGRAPH_OVERRIDE_PATTERNS` / cross-case regex matches, instead of defaulting
to case-scoped and rejecting outright when no active case is set, ask the
existing LLM classifier explicitly "is this question about one specific case
or the whole caseload" before giving up — regex coverage will always miss
real phrasing; this closes the gap structurally instead of one more pattern
at a time.

**Verify:**
- `tests/test_router.py` full pass (existing routes unchanged).
- Live: G3's exact text → no longer bounced with the "needs a specific case"
  message; a paraphrase with no cue words at all (e.g. "what's usually
  missing from these files?") also resolves to cross-case scope.

---

## Module 17 — Verifier: stop discarding correct claims phrased differently from retrieved text (RC-5)

**Branch:** `fix/verifier-paraphrase-strictness-relaxation`

**Files:** `src/pipeline/verifier.py` — the same claim-confirmation logic
already flagged as too literal for D1 in the original baseline; extend
whatever relaxation Module 4 already applied to XAGG's grand-total case to
XGRAPH's per-claim citation check and to the general "could not be verified
as an accurate paraphrase" XAGG fallback, so a semantically-equivalent
restatement of retrieved data isn't treated as unconfirmed.

**Verify:**
- `tests/test_verifier.py` full pass, plus a new test: a claim that
  paraphrases (not contradicts) its cited source is confirmed.
- Live: CR4's exact text → the weapon→FIR→accused chain is stated directly,
  without the "could not be confirmed" disclaimer burying it; M2/M7 (already
  covered by Module 13, but confirm the verifier no longer independently
  forces the raw-dump fallback on a correct NL summary).

---

## Module 18 — Second full Gold-32 re-run + updated honest report

**No app-code branch.** Depends on Module 10.3 (harness merged onto `main`).
Re-run `evaluation/gold32_run.py` / `evaluation/gold32_score.py` against the
fully-merged build from Modules 10–17 (on top of Modules 1–9 and the Module 8
chunking work). Report, per question-type bucket, the new mean/pass-rate
against the original baseline (Complex Reasoning 0.10→?, Contextual
Summarization 0.06→?, Creative Generation 0.04→?) — and separately note, for
each of the 6 root causes, whether it was confirmed fixed by a **non-gold
paraphrase** as required by each module's own verify step, since that's the
actual evidence the fix is a platform capability improvement and not an
overfit to 32 fixed strings.

---

## Module 19 — Fix the 17-query DeepEval harness's incomplete-context understatement bug (eval infrastructure, not app code)

**Branch:** `fix/deepeval-harness-context-capture`

**Why:** `evaluation/EVALUATION_REPORT.md` (the older, still-live-on-`main`
17-query harness) documents its own known defect: 3 of 5 metrics (Answer
Relevancy 0.47, Hallucination 0.44, NameFactFidelity 0.16) score against an
**incomplete captured retrieval context** — correct answers sourced from
structured API fields (e.g. "30-bore pistol") get penalized as "hallucinated"
because the harness never captured that structured context, only narrative
chunks. This makes those 3 scores measure the harness, not the app — anyone
reading them without the caveat in §4 of that report draws the wrong
conclusion about app quality.

**Files:** `evaluation/run_pipeline.py`'s `_parse_sse()` — capture the full
retrieval context the pipeline actually used (including structured-aggregate
and graph-node data, not just narrative chunk text) for every query, not a
truncated/partial slice.

**Verify:** re-run the 17-query harness; the 3 previously-understated metrics
move up in a way attributable to better-captured context specifically for
the queries §3 flagged as unfairly penalized (e.g. the "30-bore pistol"
case) — not a blanket score inflation across all 17.

---

## Module 20 — Judge-prompt tightening: apply the "close numbers are OK" rule consistently

**Branch:** `fix/gold32-judge-close-number-tolerance`

**Why:** the baseline report itself flags A1 as "arguably a partial pass,
understated by the judge" — it got the female count (24) and unknown count
(3) exactly right and the total off by a small, non-dedup-level margin, per
the testing team's own explicit rule ("close numbers are OK, don't match
word-by-word"), yet scored 0.30. If the grading rubric doesn't reliably apply
the team's own stated tolerance, every module's Module-18 rerun risks being
scored more harshly than the intended standard.

**Files:** `evaluation/gold32_score.py`'s judge prompt — make the "close
numbers / contextual coverage OK, opposite-or-incomplete is the real failure"
rule an explicit, worked-example instruction to the judge model, not an
implicit expectation.

**Verify:** re-score A1's already-captured baseline answer (24/92 close to
24/94) under the updated judge prompt and confirm it now scores as a
pass/partial-pass rather than 0.30, without changing how a genuinely wrong
or opposite answer scores on a spot-check of 2–3 known-bad answers from the
same baseline run.

---

## Sequencing note

Modules 10.1–10.3 are independent of each other and of the in-flight Module 8
(CrPC chunking) work on this branch (`feature/crpc-structure-aware-chunking`)
— finish and merge Module 8 first (it's mid-flight per current working-tree
changes), then run the original plan's Module 9 rerun, then work through
10.1/10.2/10.3 and 11–21 against the resulting `main`. Modules 19 and 20
touch only `evaluation/`, never app code — they're parallel-safe against
everything else in this plan and can run whenever convenient, including
before Module 18's rerun (so Module 18 benefits from both fixes) or after (as
a documented correction to Module 18's own numbers).
