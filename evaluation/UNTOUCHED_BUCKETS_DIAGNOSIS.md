# Module 10 — Live Reconfirmation Sweep (Untouched Gold-32 Buckets)

Run live against the current `main`-based branch (`feature/crpc-structure-aware-chunking`,
Module 8 still mid-flight but touches only `src/ingestion/chunker.py` — no
overlap with anything below), 2026-09-05, as platform-admin with All Cases,
via `evaluation/module10_run.py`. Raw captured answers in
`evaluation/module10_untouched_buckets_outputs.json`. This supersedes the
stale `goldtest-eval3` trace the original plan's diagnosis was built from —
every root cause below is reconfirmed against **current** behavior, not the
pre-Module-1–7 baseline.

**Headline: RC-0 through RC-5 all reconfirmed as still-live on current `main`.
Zero regressions from Modules 1–7 introduced. One new failure mode found
(RC-6, below) that the original plan didn't anticipate.**

---

## Per-question classification

| ID | Route (live now) | RC(s) confirmed | Note |
|---|---|---|---|
| A1 | XAGG / raw-dump fallback | RC-5 | Also: live count (65 M / 92 total) still drifts from gold (67 M / 94 total) — same shape as the D1/CP6 dedup gap Module 1 resolved, not yet looked at for accused counting. |
| CR3 | XNETWORK cluster dump | **RC-1** | Answer is 5 unrelated community clusters; never addresses the walk-in-complaint/CMS-tag question at all. |
| CR4 | XGRAPH cluster dump + citation rejection | **RC-1 + RC-5** | Verifier rejects its own correctly-sourced claim about FIR 301/26 as "unsupported" then buries the real weapon→accused→status chain under disclaimers. |
| CR6 | **null** (new) | **RC-6** (new) | See below — regressed from a hedged RAG answer (baseline) to a bare "couldn't find sufficient information" with no route at all. |
| CR7 | XAGG / explicit gap statement | **RC-2** | Says outright: no case-status field, no court-record cross-check exists in this aggregate. Matches diagnosis exactly. |
| CR8 | RAG, **status=error** | RC-3 + **RC-6** | "Document search failed; no answer could be produced." — worse than baseline's hedged prose; same new failure mode as CR6. |
| CS4 | XGRAPH cluster dump | **RC-1** | Same generic-traversal narration; never performs the actual ask (find a criminal-record entry with NO matching local FIR-accused — a set-difference, not a traversal). |
| CP1 | XAGG raw per-district counts | **RC-2** | Unchanged from baseline — flat counts, no rate-per-district-caseload. |
| M1 | XAGG statute breakdown | **RC-2** | Explicitly states no year-partitioned data exists. Unchanged. |
| M2 | XAGG, explicit gap statement | **RC-2** | Now states plainly "time-series data would be required" instead of baseline's raw-dump — a wording improvement, same underlying gap. |
| M4 | XNETWORK/graph traversal dump | **RC-1** | Unchanged — never engages the charge-severity-vs-court-outcome comparison. |
| M5 | **null** (new) | **RC-6** (new) | Same bare "couldn't find sufficient information" pattern as CR6 — identical boilerplate text, both ~90–120s elapsed. |
| M7 | XAGG raw breakdown | **RC-2 + RC-5** | Unchanged — flat statute counts, verifier rejects the NL summary. |
| G1 | XNETWORK cluster dump | **RC-1** | Unchanged — "cross-regional cybercrime coordination" instead of the actual demographic/property/timing anomalies asked for. |
| G2 | RAG, **status=error** | RC-3 + **RC-6** | Same "Document search failed" pattern as CR8/G5. |
| G3 | GRAPH_HYBRID, bounced | **RC-4** | Unchanged — router still demands a specific case or an exact cross-case cue phrase. |
| G5 | RAG, **status=error** | RC-3 + **RC-6** | Same pattern. |
| G6 | XNETWORK cluster dump | **RC-1** | Unchanged — orientation-note ask answered with unrelated co-accusal networks. |

---

## RC-0 reconfirmed: Meta-Analysis still never fires

**0 of 18** routed to `META_ANALYSIS`. Routes actually seen: XAGG (6), XNETWORK
(6), XGRAPH (2), RAG (4, 3 of them now hard errors), GRAPH_HYBRID (1). This is
the single most consequential confirmed finding — it's exactly the gap Module
11 targets, and it explains why RC-1 (XNETWORK/XGRAPH dumping irrelevant
clusters) shows up on 6 of 18 questions: with no decomposer claiming these
broad/evaluative queries first, they fall through to whichever cross-case
tool's regex happens to match, which is usually the network/graph tools
rather than anything that reasons about the specific ask.

## RC-6 (new) — a silent "give up" path now returns a bare, generic failure instead of degrading gracefully

Not anticipated in the original plan. Four questions (CR6, CR8, M5, G2, G5 —
5 of 18, all RAG-routed) show a failure mode absent from the stale baseline
trace: instead of RAG's old hedged-but-substantive prose, three now return
`status: "error"` with the literal string *"Document search failed; no answer
could be produced."*, and two return `route: null` with *"I couldn't find
sufficient information in the knowledge base... You may want to try
rephrasing... or ensure the relevant documents have been ingested."* — a
message shaped for a genuinely-missing-document case, produced for questions
where the underlying data (walk-in-complaint tags, forwarded-FIR numbers,
weapon license status) plainly exists in the ingested corpus.

**Before folding this into Module 15's routing fix, it needs a narrow root-cause
check of its own**, because two different explanations are consistent with
what's visible here and they call for different fixes:
1. A real regression/exception in the current RAG retry-with-refinement path
   (an unhandled error being swallowed and replaced with this generic
   message) — a bug to fix directly, independent of routing.
2. A transient infra issue at capture time (Groq rate-limit/timeout — 4 of the
   5 affected queries ran 44–117s, well above this bucket's usual RAG latency)
   — in which case it's not a new code defect, just re-run and confirm it
   clears.

**Module 10.2 result (2026-09-05): confirmed real, isolated to 3 of the 5 —
not transient infra, not the retrieval logic itself.**

Re-ran all 5 in isolation. **M5 and G5 now succeed** — both routed to XAGG
this time (not RAG) and returned real, coherent answers. This is itself a
separate, real finding: the router's route classification for these two
questions is **non-deterministic run-to-run** (RAG one run, XAGG the next,
same exact query text, no code changed in between) — worth folding into
Module 11/16's work on cross-case routing stability, since a route that
depends on which run you catch it on is its own reliability problem
independent of RC-6.

**CR6, CR8, G2 failed identically both times** — same route (RAG), same
"Retrieval failed" error, ruling out plain flakiness. Traced the swallow
point: `src/pipeline/harness/tools/rag.py:362-364` catches whatever
`_retrieve_candidates()` raises, logs it (to server console output this
session had no access to), and replaces it with the generic "Retrieval
failed" / "Document search failed; no answer could be produced." — the real
exception message never reaches the API response.

**Then called `_retrieve_candidates()` directly, in isolation, for the exact
same 3 query texts** (bypassing the live server entirely) — **all 3 succeeded
immediately**, 8 semantic + 8 BM25 results each, no exception. So the
retrieval logic itself is not broken.

**Conclusion:** the failure is specific to the *live request's runtime
context*, not the code path in isolation. The 3 failing live calls each took
71–99s before erroring (vs. near-instant in the isolated call) — consistent
with the RAG tool's retry-with-refinement loop making several rounds of
embedding calls (each round: original + 2 expanded + 1 cross-script query =
up to 4 embedding calls) before finally failing. Most likely a connection-
pool exhaustion or a stricter effective timeout under that repeated load in
the long-running server process, not a defect in `_retrieve_candidates()`
itself. **Could not pin down the exact exception** — the currently-running
dev backend's stdout isn't captured to a file, and restarting it to capture
logs wasn't done in this module (would interrupt whatever else is using that
process; needs a deliberate go-ahead, not a diagnostic-only decision).

**Action for Module 15 (or whoever picks this up):**
1. Stop swallowing the exception into a generic message — `_emit("retrieval",
   "error", "Retrieval failed")` should carry `str(retr_exc)` (already
   captured in `ToolError.message`) through to wherever this gets logged
   /surfaced, not just the generic string.
2. Restart the backend with stdout captured to a file
   (`uvicorn ... 2>&1 | tee backend_$(date).log`) and re-run CR6/CR8/G2 once
   more to get the actual exception/traceback before writing a fix — this
   module deliberately stopped short of that (it requires restarting a
   process this session doesn't own outright).
3. Separately: investigate why M5/G5's route classification isn't stable
   run-to-run (see above) — likely relevant to Module 11's Meta-Analysis
   routing work and Module 16's cross-case-scope fallback, since both touch
   the same classification path.

## A1's own number drift — separate from the 6 RCs, needs its own mini ground-truth check

Live: 65 male / 24 female / 3 unknown / 92 total. Gold: 67 / 24 / 3 / 94. The
female and unknown counts match exactly; male and total are each off by 2 —
the same shape of discrepancy Module 1 resolved for D1 (a small number of
non-canonical/duplicate accused rows). Not yet investigated. Recommend a
short Module-1-style check (count accused rows directly in the live DB/graph,
diff against the gold 94) before building A1's XAGG aggregate fix, so the
aggregate is built against a confirmed-correct denominator, not a stale one.

---

## Net effect on the Module 11–17 plan

No regrouping needed — RC-0 through RC-5 hold exactly as scoped. Two
additions to carry forward:
- **RC-6** gets its own short investigation (above) before Module 15, and
  becomes either part of Module 15 or a small standalone module depending on
  what that investigation finds.
- **A1's ground-truth drift** gets a short Module-1-style check before
  Module 13 builds the gender-ratio aggregate on top of it (A1 wasn't
  explicitly assigned a fix module yet in the plan — it naturally belongs
  with Module 13's aggregate work, same shape as D1/CP6).
