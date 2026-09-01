# Post-Fix Verification Run — 20 Scenarios (Run #2)

Second manual pass, run after the three fix commits below. Purpose: confirm each
logged finding is genuinely fixed, and capture anything still broken (or newly
broken) precisely enough to act on without re-testing from scratch.

**Companion doc:** `SCENARIO_TEST_LOG.md` (Run #1 — where these findings were
originally observed). Finding letters below refer to that document.

---

## Environment for this run

| | |
|---|---|
| **Code** | `main` @ `2361c36` (3 fix commits on top of `eb166f4`) |
| **Harness** | **ON** — `HARNESS_CUTOVER_ROUTES=RAG,GRAPH,GRAPH_HYBRID,SQL,XGRAPH,XAGG,XNETWORK` |
| **LLM** | `LLM_PROVIDER=groq` (4 rotating keys) |
| **DB** | 73 cases, 534 documents, AGE preloaded, app connects as `muhafiz_app` (least-privilege) |
| **Chroma** | 793 vectors |
| **Chat state** | Wiped clean before run (0 sessions / 0 messages) |
| **Logins** | platform-admin `admin@example.com` / `<redacted>` · investigator `browsercheck@example.com` / `<redacted>` |

**Pre-run smoke test:** "What weapon and how many bullets…" on fir-430-26 →
`PATH: HARNESS`, dispatched to **Case Summarization** sub-agent, correct cited
answer. Harness confirmed live before manual testing began.

---

## What the fixes changed (what to watch for in this run)

### Commit `24e7614` — critical findings A, C, G, K + harness enable
| Finding | Change | What "fixed" looks like |
|---|---|---|
| **A** harness off | `.env` `HARNESS_CUTOVER_ROUTES` populated | Every scenario shows `Supervisor → Sub-Agent Dispatch` steps, names a sub-agent |
| **C** truncation | `max_tokens` raised on generation paths (`rag.py`, `semantic_search.py`, `orchestrator.py`) | Long multi-section answers end on a complete sentence, no dangling `[Document` |
| **G** XAGG 0-char answer | empty-response guard added | XAGG count query returns a real number, never a blank answer reported as delivered |
| **K** DIRECT fabrication | `prompts/router.txt` + `prompts/direct_response.txt` tightened | Statutory/numeric questions no longer route to DIRECT and invent facts |

### Commit `6da29c8` — findings B, E, I, J (rendering + citation integrity)
| Finding | Change | What "fixed" looks like |
|---|---|---|
| **B** `*single-asterisk*` literal | `MessageBubble.tsx` inline parser | `*text*` renders bold, no stray asterisks |
| **I** list numbering "1. 1. 1." | `MessageBubble.tsx` ordered-list handling | Ranked lists render 1. 2. 3. |
| **E** Evaluator vs Retrieved-Docs contradiction | `verifier.py` relevance-signal reconciliation | Both panels agree, or clearly measure different things |
| **J** mixed `CR-CNNN-N` case IDs | `verifier.py` citation integrity | Citations reference real, consistent case IDs |

### Commit `2361c36` — findings L, N + abstention/denial UX
| Finding | Change | What "fixed" looks like |
|---|---|---|
| **L** name transliteration drift | `prompts/final_response.txt`, `cross_case_response.txt` | Same CNIC → same name spelling across queries |
| **N** answer language mismatch | prompt language-anchoring | English query → English answer |
| **F** all-or-nothing citation discard | `orchestrator.py`, `cutover.py` | Valid findings surface even if one sub-claim is flagged |
| UX: abstention/denial clarity | denial vs. no-data messages distinguished | Role-denied reads differently from "found nothing" |

### Not addressed by these commits (expected to still be open)
- **H** — Global Search / XNETWORK returns 0 community clusters. This is a
  **data/pipeline gap** (community detection likely never run for this dataset),
  not a code bug in the query path. Expect Scenario 9 to still return nothing
  unless community detection is run separately.
- **M** — 2026-03-06 incident date. Assessed in Run #1 as likely-correct
  synthetic data, not a defect. No change made.

---

## Results

### Scenario 1 — Semantic Search / weapon+bullets (fir-430-26)
**Verifying:** A (harness on), C (truncation), B (asterisks)

| Check | Result |
|---|---|
| Harness engaged | ✅ **VERIFIED** — `Supervisor → Sub-Agent Dispatch`, "Harness routing complete" |
| Sub-agent named | ✅ **Case Summarization** (status=partial) |
| Answer correct | ✅ 30-bore pistol, 6 bullets, 5 citations |
| **C — truncation** | ✅ **FIXED** — complete sentence, no dangling `[Document` |
| **B — asterisks** | ✅ **FIXED** — bold renders, no literal `*` |

**New (minor):** graceful-degradation note renders with literal underscores:
`_Case graph data was unavailable for this case; this summary is based on case
documents only._` — the `_italic_` markdown form isn't handled by the inline
parser (only `*`/`**` were fixed in `6da29c8`). Cosmetic sibling of Finding B.
→ **NEW FINDING P** (Low): extend inline parser to `_italic_`.

**Also notable:** "Case graph data was unavailable for this case" — the graph
lookup returned nothing for fir-430-26, so Case Summarization ran document-only
(hence `status=partial`). Not a failure (it degraded honestly and said so), but
worth checking whether fir-430-26 genuinely has no graph nodes.
→ **NEW FINDING Q** (Medium, needs DB check).

---

### Scenario 2 — RAG summary + charged sections (fir-430-26)
**Verifying:** C (truncation — Run #1's worst case), E (evaluator contradiction)

| Check | Result |
|---|---|
| Harness engaged | ✅ **Semantic Search**, status=ok |
| **C — truncation** | ✅ **FIXED** — Run #1 cut off at "Section 34 PPC – **Con"; now complete through all 3 sections + closing sentence |
| **E — contradiction** | ✅ **FIXED** — Evaluator "Relevant" + Retrieved Docs "Evaluated" now agree (Run #1: True vs False) |
| Citation Check | ✅ "Grounded" |
| Full trace | ✅ Retrieval (12 semantic + 10 lexical) → Re-ranker → Evaluator → Response → Citation Check → Memory — granular steps now visible under harness |
| Answer correct | ✅ 2024-09-17 09:40, Rahman Pur Hyderabad, 392/34 PPC + 13 Arms Ordinance |

**Note:** accused rendered as **"Rabia"** here (Run #1 saw رابعع/Rabeeha and, once,
"Raheela"). Same CNIC 00000-9000132-1. Finding L improved but not fully settled —
see Scenario 4 note below.

---

### Scenario 3 — Grounded abstention / bank account (fir-430-26)
**Verifying:** correct abstention still holds post-fix

| Check | Result |
|---|---|
| Abstained correctly | ✅ **PASS** — "I couldn't find sufficient information…", no fabricated account number |
| No hallucination | ✅ |

**Regression in trace display:** the Pipeline Trace shows **"Pipeline idle / Steps
animate here when you send a message"** with all steps greyed — i.e. the trace was
lost/reset for this completed turn. Run #1 showed a full trace with the Evaluator
in red ("Max retries (2) reached"). The answer is right, but the operator can no
longer see *why* it abstained.
→ **NEW FINDING R** (Medium): trace panel resets to idle instead of retaining the
completed run's steps on abstention/short-circuit paths.

---

### Scenario 4 — Within-case people/links (fir-213-26)
**Verifying:** C (truncation), E (contradiction), I (list numbering), J (citation IDs)

| Check | Result |
|---|---|
| **C — truncation** | ✅ **FIXED** — full answer: Key Entities → Key Events → Open Questions → Status → caveats |
| **I — list numbering** | ✅ **FIXED** — Key Events numbered 1. 2. 3. correctly (Run #1: all "1.") |
| **J — citation IDs** | ✅ No malformed `CR-CNNN-N` IDs this run |
| Answer quality | ✅ Rich: entities, roles, IDs, events, open questions, status |
| **New: self-flagged citation integrity** | ⚠️ "A cited claim ([Document 3]) could not be confirmed against its source: Claim cites figure(s)/identifier(s) not found in its source text: 1,, 4000032" and same for [Document 5] ("07,") — the new verifier is catching mismatches and disclosing them. Good that it's transparent; the malformed fragments suggest the checker is comparing partial/truncated numeric tokens. |

→ **NEW FINDING S** (Medium): citation-integrity checker produces noisy/partial
identifier fragments ("1,, 4000032", "07,") — likely tokenization of phone/CNIC
numbers. Verify whether these are genuine mismatches or false positives from
splitting numbers on commas.

**Trace panel:** also showed "Pipeline idle" (same as Scenario 3) → Finding R again.

**Name drift:** suspects here are "Kashif" and **"Zain"**; Run #1 Scenario 4 showed
کاشف and **ذیشان (Zeeshan)** for the same case/IDs. "Zain" vs "Zeeshan" for ID
00000-9000036-1 is a **new variant** — Finding L (transliteration drift) is
**NOT fully fixed**.
→ **FINDING L — STILL OPEN** (Medium).

---

### Scenario 5 — Local Search / investigating officer (fir-213-26)
**Verifying:** L (Local Search sub-agent reachable + working)

| Check | Result |
|---|---|
| Harness engaged | ✅ Routed `GRAPH → Local Search` (correct sub-agent) |
| **Outcome** | ❌ **FAILED** — "An error occurred while processing your request." + red Supervisor/Dispatch + "Connection seems stalled — no response received in a while." |

**Root causes (from backend log — two distinct problems):**

1. **Groq 8000 TPM cap exceeded → cutover falls back to legacy, then legacy's
   router also 413s:**
   ```
   Cutover classification failed, falling back to orchestrator.py: 413 …
   Limit 8000, Requested 9970 …
   Router failed: 413 … Requested 9970 …
   ```
   The router prompt is ~9.9k tokens vs Groq's 8k free-tier per-request cap. This
   is the *known* BUG-5 (teammate's `testingbugs.md`) resurfacing — and now it
   breaks the request entirely rather than silently degrading.

2. **Local Search's own answer rejected by the new verifier:**
   ```
   local_search: verifier rejected answer: Answer is substantial (long, or a
   multi-item list) but cites no [Document N] source at all, despite the
   evaluator already confirming relevant chunks
   ```
   The stricter citation rule added in `24e7614`/`6da29c8` is correctly catching
   an uncited answer — but Local Search's prompt doesn't instruct it to emit
   `[Document N]` markers, so it fails the gate every time and the turn dies.

→ **NEW FINDING T (HIGH)** — Local Search sub-agent is broken: its generation
prompt lacks the `[Document N]` citation contract the verifier now enforces.
Needs the same citation instruction the other sub-agents have.

→ **NEW FINDING U (HIGH)** — Router prompt exceeds Groq's 8k TPM cap (~9.9k
tokens), causing 413 on both the cutover classifier and the legacy router. Options:
trim `prompts/router.txt`, switch provider for the router call, or upgrade tier.
This also re-triggers the frontend stall timeout (the 150s guard from the earlier
session), surfacing as "Connection seems stalled".

---

## Running tally after Scenarios 1–5

**Confirmed fixed:** A (harness on, all 5), C (truncation, 3/3 long answers),
B (asterisks), E (contradiction), I (list numbering), J (citation IDs)

**Still open / regressed:**
| ID | Severity | Issue |
|---|---|---|
| **T** | High | Local Search fails verifier — no `[Document N]` in its prompt |
| **U** | High | Router prompt ~9.9k tokens > Groq 8k cap → 413 → request dies |
| **L** | Medium | Name drift persists (Zain/Zeeshan, Rabia/Rabeeha) |
| **R** | Medium | Trace panel resets to "Pipeline idle" after some turns |
| **S** | Medium | Citation-integrity checker emits partial numeric fragments |
| **Q** | Medium | fir-430-26 has no graph data (needs DB check) |
| **P** | Low | `_italic_` markdown renders literally |

---

### Scenario 6 — Cross-case linkage / ذیشان (All Cases, platform-admin)
**Verifying:** F (all-or-nothing citation discard — Run #1's worst cross-case failure)

| Check | Result |
|---|---|
| Harness engaged | ✅ **Cross-Case Linkage**, status=partial |
| **F — answer discarded** | ✅ **FIXED** — Run #1 returned a generic "cannot provide a confident answer" refusal that threw away all 13 real case links. Now returns the **actual list**: fir-201-26, 202, 205, 208, 210, 213, 216, 233, 401, 403, 416, 427, 466-26 (depth 1 hop) |
| Ground truth | ✅ 13 cases matches Run #1's internal "Found evidence in 13 other case(s)" |
| Speed | ✅ 9.8s (vs 49s in Run #1) |

**This is the single biggest behavioral improvement in the run so far** — the
capability that was completely hidden behind a refusal now surfaces its real,
valuable output.

**Minor:** the case list is printed twice (once in the intro sentence, once under
"Confirmed connections") — mild redundancy, not an error.
→ **NEW FINDING V** (Low): duplicate case-list rendering in Cross-Case Linkage.

---

### Scenario 7 — Investigator denied cross-case (browsercheck@, No Case)
**Verifying:** UX fix — denial vs. no-data message distinction

| Check | Result |
|---|---|
| Denial enforced | ✅ **PASS** — `Cross-Case Linkage completed with status=denied` |
| No data leaked | ✅ No case IDs, no suspect info |
| **UX message fix** | ✅ **FIXED** — Run #1 gave the generic "couldn't find sufficient information" (indistinguishable from a search miss). Now: *"This question requires searching across multiple cases, which needs a supervisor-level role or higher. Your account doesn't have that access, so I can't run it. You can still ask about any case you're assigned to — select it from the case list and ask again."* — states the reason, the required role, and a concrete next step |

**Note:** header still reads "Some steps failed" and the Response step is styled
red for what is correct, intentional security behavior — same cosmetic concern
as Findings R/abstention-styling. Low priority, but the wording fix is a clear win.

---

### Scenario 8 — XAGG count / PPC cases (All Cases)
**Verifying:** G (0-char empty answer — Run #1 CRITICAL)

| Check | Result |
|---|---|
| Harness engaged | ✅ **Large-Scale Aggregate**, status=ok |
| **G — empty answer** | ✅ **FIXED** — Run #1 produced **0 chars** with a silent "Answer delivered". Now **6,998 chars** of real content |
| Speed | ✅ 20.8s |

**But the answer is wrong-shaped for the question.** The user asked *"How many
cases in total involve the PPC legal code?"* — a **count**. The system returned a
**full enumeration of every matching case** (fir-1001-26, 117-26, 118-26, … 97-26),
roughly 60+ rows spanning multiple screens, and **never states the total number**.
The closing line explains why: *"The natural-language summary could not be
verified as an accurate paraphrase; showing the raw computed aggregate instead."*

So the empty-answer bug is fixed, but the fallback path dumps raw aggregate rows
instead of answering the actual question. An investigator asking "how many" has to
count 60+ lines themselves.
→ **NEW FINDING W (High)**: XAGG's verifier rejects the natural-language summary
and falls back to raw rows. Either (a) the summary prompt needs to produce a
verifiable count statement, or (b) the raw-aggregate fallback should still lead
with the computed total (e.g. "61 cases involve PPC — full list below").

**Also:** most rows show "unknown status" — consistent with Run #1's note that
`investigation_status` is free-text and often empty. Not a new bug.

---

### Scenario 9 — Global Search / dataset-wide themes (All Cases)
**Verifying:** H (0 community clusters — expected to still be open)

| Check | Result |
|---|---|
| Harness engaged | ✅ **Global Search**, status=ok |
| **H — 0 clusters** | ✅ **FIXED (unexpectedly)** — Run #1 returned "Retrieved 0 relevant community cluster(s)" and no synthesis on 2/2 attempts. Now produces a real multi-paragraph thematic synthesis with 12 citations |
| Content quality | ✅ Legal-framework themes (PPC/PECA/Illegal Dispossession/Arms Ordinance), co-accused relationship patterns, named recurring individuals (Saad ur Rehman, Asim Rashed), geographic spread |

This was logged as "expected to still be open — data/pipeline gap." It is
**working now**, so the fixes evidently reached this path too.

**Two problems remain:**

1. **Answer truncated mid-sentence:** ends at *"Geographically, cases span cities
   including Islamabad"* — no closing. Finding C recurring, but only on this
   longest cross-case synthesis (1740 chars).
   → **FINDING C — partially open** (long Global Search answers still truncate).

2. **Citation-integrity noise is severe here:** five consecutive disclosures —
   *"A cited claim ([Document 2]) could not be confirmed against its source: Claim
   cites figure(s)/identifier(s) not found in its source text: 2016."*, then
   `2016.`, `1965,, 2005.`, `1965,`, `1965,, 2005.` The checker is flagging **years
   and statute numbers** (2016, 1965, 2005) as unverifiable identifiers. These are
   almost certainly legitimate references to PECA **2016**, Arms Ordinance **1965**,
   Illegal Dispossession Act **2005** — the checker is treating statute years as
   numeric identifiers that must appear verbatim in source text.
   → **FINDING S — CONFIRMED AND ESCALATED to High**: the citation-integrity checker
   produces false positives on statute years/legal-code numbers, generating five
   alarming "could not be confirmed" warnings on a correct answer. This actively
   undermines trust in a correct response.

---

### Scenario 10 — Meta-Analysis / recurring suspects (All Cases)
**Verifying:** I (list numbering), J (case IDs), Meta-Analysis reachable

| Check | Result |
|---|---|
| Harness engaged | ✅ **Meta-Analysis**, status=ok — the outermost sub-agent is reachable and working |
| **C — truncation** | ✅ Complete answer with closing sentence |
| **I — numbering** | ✅ Bulleted (not numbered) list renders correctly |
| **J — case IDs** | ✅ **FIXED** — Run #1 had malformed `CR-C101-1`-style IDs. All IDs now well-formed `fir-NNN-YY` |
| Citation-integrity noise | ✅ None on this answer |
| Speed | 111.6s (slowest of the run) |

**Answer changed substantially from Run #1** — and needs verification:

| | Run #1 | Run #2 |
|---|---|---|
| Top suspects | فیصل (3 cases), طارق (2), بلال (2) | شہزیب عرف شابی, فیصل, طارق, عاصم رشید — all "2 cases each" |
| Max frequency | 3 cases | 2 cases |

Run #2 also opens with **station-level** statistics (Model Town Lahore = 7 cases,
9 stations with 5 each) that Run #1 didn't produce. The suspect frequencies
disagree between runs for the same dataset — فیصل was 3 cases, now 2; بلال has
dropped out entirely; شہزیب عرف شابی and عاصم رشید are new.
→ **NEW FINDING X (Medium)**: Meta-Analysis suspect-frequency results are not
stable across runs on identical data. Needs a DB cross-check to establish the
true counts and determine which run (if either) is correct.

---

## Running tally after Scenarios 1–10

**Confirmed fixed:** A (harness, 10/10), C (truncation — fixed on most, still
recurs on longest Global Search answer), B, E, I, J, **F** (cross-case answers no
longer discarded — biggest win), **G** (no more 0-char answers), **H** (Global
Search now synthesizes), denial-message UX

**Open — needs fixing:**
| ID | Severity | Issue |
|---|---|---|
| **T** | High | Local Search fails verifier (no `[Document N]` in its prompt) |
| **U** | High | Router prompt ~9.9k tokens > Groq 8k cap → 413 kills request |
| **W** | High | XAGG dumps raw rows instead of answering "how many" |
| **S** | High ↑ | Citation checker false-positives on statute years (2016/1965/2005) |
| **C** | Medium | Truncation still hits longest Global Search synthesis |
| **X** | Medium | Meta-Analysis suspect counts unstable between runs |
| **L** | Medium | Name drift (Zain/Zeeshan, Rabia/Rabeeha) |
| **R** | Medium | Trace panel resets to "Pipeline idle" on some turns |
| **Q** | Medium | fir-430-26 has no graph data (DB check) |
| **P** | Low | `_italic_` renders literally |
| **V** | Low | Cross-Case Linkage prints case list twice |

---

### Scenario 11 — SQL reference lookup / PPC 379 (All Cases)
**Verifying:** U (router 413), correctness

| Check | Result |
|---|---|
| Harness engaged | ✅ **Investigative Analysis**, status=partial |
| **U — router 413** | ✅ **No 413 this time** — request completed |
| Multi-tool trace | ✅ Analysis Graph (skipped) → Analysis Sql (1 row) → Analysis Rag (5 chunks) — the multi-tool fan-out is visible and working |
| Answer correct | ✅ "theft of movable property, including motorcycles… cognizable… Investigation Wing - Theft/Property Cell" — matches ground truth |
| Degradation disclosed | ✅ "This analysis could not draw on: case-graph search. It reflects only the sources that returned usable data." — honest partial-failure reporting |

**Concern:** 148.3s (vs 20.6s in Run #1 on the legacy path). Investigative Analysis
fans out across graph+SQL+RAG sequentially. Correct but slow.
→ **NEW FINDING Y** (Low/Medium): Investigative Analysis latency ~7× legacy for
the same question. Consider parallelising the three tool calls.

**Finding P recurring:** degradation note again wrapped in literal `_underscores_`.

---

### Scenario 12 — No-fabrication guard / motorway speeding fine (All Cases)
**Verifying:** K (Run #1 CRITICAL — DIRECT fabricated fake statutory fines)

| Check | Result |
|---|---|
| **K — fabrication** | ✅ **FIXED — the most important fix in the run** |
| Route | ✅ **Investigative Analysis**, status=**abstained** (Run #1: DIRECT, ungrounded) |
| Fabricated content | ✅ **NONE** — Run #1 invented "PMVO 1979 Section 102", Rs. 500/1,000/2,000 tiers, licence suspension, imprisonment. All gone. |
| Grounding gate | ✅ `Analysis Rag: RAG found nothing usable for this query` → `Response: The generated analysis could not be verified as grounded in the retrieved material` |

The model apparently still *drafted* an answer, but the verifier caught that it
wasn't grounded and **blocked it**. Exactly the intended safety behaviour — the
guard that was bypassed entirely in Run #1 now fires.

**Note:** `Analysis Sql returned 21 row(s)` yet RAG found nothing usable — the SQL
rows presumably weren't traffic-fine data. Worth confirming what those 21 rows
were, to be sure the abstention is for the right reason.

---

### Scenario 13 — Timeline Building (fir-430-26)
**Verifying:** C (truncation), Timeline Building sub-agent reachable

| Check | Result |
|---|---|
| Harness engaged | ✅ **Timeline Building**, status=partial — sub-agent reachable |
| Dedicated step | ✅ `Timeline Building: 9 event(s)` |
| **C — truncation** | ✅ Complete, closes cleanly |
| Speed | ✅ 8.9s (vs 79.7s Run #1) |

**Regression — the timeline itself is missing.** The answer only reports
*metadata about* the timeline: "This case's timeline has 9 dated event(s)
(spanning 2024-09-17 to 2026-01-21). Conflict detection could not be completed…"
**It never lists the 9 events.** Run #1 (legacy) produced an actual chronological
narrative with dated entries. The sub-agent found the events (step says "9
event(s)") but the response renders only a summary line.
→ **NEW FINDING Z (High)**: Timeline Building returns event *count* instead of the
event *list*. The scenario's core deliverable — a chronological timeline — is not
produced. Likely the `TimelineEvent` payload isn't being rendered into the answer
text (recall `cutover.py` emits a `timeline_building` SSE step with the full event
payload, but the frontend has no renderer for it — noted in the code comments as
"future frontend work").

---

### Scenario 14 — Data quality / extraction coverage (fir-213-26)
**Verifying:** C (truncation), Data-Quality sub-agent

| Check | Result |
|---|---|
| **C — truncation** | ✅ **FIXED** — Run #1 cut off mid-list at "Time: 14:10:00Z ([Document 2] [Document 3] [Document 4". Now runs to a full 6-section report with Conclusion |
| Answer quality | ✅ Best-structured answer of the run: Core Case Info → Complainant/Suspects → Incident Details → Investigative Actions → Legal Provisions → Consistency → Conclusion |
| **L — name drift** | ✅ **Consistent here** — کاشف (CNIC …035-1) and ذیشان (CNIC …036-1) both in Urdu script, matching Run #1 |

**But it did NOT use a sub-agent:**
```
Supervisor: DIRECT route — handing back to the legacy path
Sub-Agent Dispatch: DIRECT route — no sub-agent; caller should use the legacy DIRECT path
Router: Route decided: RAG
```
The harness classified DIRECT and handed back to legacy, which then re-routed to
RAG and answered well. So the answer is right, but this scenario's target
sub-agent (**Data-Quality/Extraction-Coverage**, the Hyp #12 fix) was **not
exercised** — the query classified as DIRECT at the harness layer.
→ **NEW FINDING AA (Medium)**: a data-quality question classifies as DIRECT in the
harness (bypassing Data-Quality sub-agent) but as RAG in the legacy router. The two
routers disagree, and the harness's DIRECT classification is wrong for this query.

**Also:** the answer claims "no missing or incomplete fields… fully documented" for
a case where other scenarios showed `investigation_status` is largely "unknown".
Assessment may be over-confident, but it's scoped to the retrieved documents.

---

### Scenario 15 — Report Drafting / PDF (fir-430-26)
**Verifying:** file generation + Report Drafting sub-agent

| Check | Result |
|---|---|
| Report content | ✅ Excellent — 6 sections, fully cited, complete through "End of Report", no truncation |
| **PDF file** | ❌ **NOT GENERATED** — answer delivered in chat only, no download card, no `file_generation` step |

**Root cause (confirmed):** the router classified `output_format: "chat"` instead
of `"file_pdf"`, so neither the harness (`main.py:436` requires
`classified_output_format == "chat"` to cut over — it did cut over) nor legacy ever
triggered file generation. Backend log shows **no `file_generation` entry at all**.

Why the router got it wrong: `prompts/router.txt:62` *does* define
`file_pdf: User asked for a PDF, a report, or a formal document` — but **every one
of the ~15 few-shot examples in that prompt uses `"output_format": "chat"`**, with
no example ever emitting `file_pdf`/`file_xlsx`/`file_docx`. The examples
effectively teach the model to always answer `chat`, overriding the rule text.
→ **NEW FINDING AB (High)**: router never emits file output formats because the
few-shot examples contain no file-output case. Fix: add explicit `file_pdf` /
`file_xlsx` / `file_docx` examples to `prompts/router.txt`. This also means
**Report Drafting sub-agent is unreachable** (it's only selected when
`output_format` is a file format).

**Regression vs Run #1:** Run #1 (legacy) DID produce a downloadable
`Case Summary Report: FIR 430/26.pdf`. So this is a **regression introduced with
the harness/prompt changes**, not a pre-existing gap.

**Name note:** accused rendered "رَبَعَه" (fully vocalised) — a 4th spelling variant
after رابعع / Rabeeha / Rabia. Finding L confirmed still open.

---

## Running tally after Scenarios 1–15

**Confirmed fixed:** A (harness), **K** (no fabrication — critical), **F**
(cross-case answers restored), **G** (no 0-char), **H** (Global Search works),
C (truncation — fixed on 13/14, still hits longest Global Search), B, E, I, J,
denial-message UX

**Open — needs fixing:**
| ID | Severity | Issue |
|---|---|---|
| **T** | High | Local Search fails verifier (no `[Document N]` in its prompt) |
| **U** | High | Router prompt ~9.9k tokens > Groq 8k cap (intermittent — didn't fire in 11–15) |
| **W** | High | XAGG dumps raw rows instead of answering "how many" |
| **S** | High | Citation checker false-positives on statute years (2016/1965/2005) |
| **Z** | High | Timeline Building returns event count, not the actual timeline |
| **AB** | High | Router never emits `file_pdf` → no PDFs, Report Drafting unreachable (regression) |
| **AA** | Medium | Harness classifies data-quality query as DIRECT; Data-Quality sub-agent unreachable |
| **C** | Medium | Truncation still hits longest Global Search synthesis |
| **X** | Medium | Meta-Analysis suspect counts unstable between runs |
| **L** | Medium | Name drift — now 4 variants (رابعع/Rabeeha/Rabia/رَبَعَه) |
| **R** | Medium | Trace panel resets to "Pipeline idle" on some turns |
| **Q** | Medium | fir-430-26 has no graph data (DB check) |
| **Y** | Low-Med | Investigative Analysis ~7× slower than legacy (148s) |
| **P** | Low | `_italic_` renders literally |
| **V** | Low | Cross-Case Linkage prints case list twice |

---

---

### Scenario 16 — Citation validation / multi-fact (fir-430-26)
**Verifying:** citation integrity, C (truncation), L (name drift)

| Check | Result |
|---|---|
| Harness engaged | ✅ **Case Summarization**, status=partial |
| **C — truncation** | ✅ Complete — Status → Key Entities → Key Events → Open Questions → caveats |
| Citation quality | ✅ Every claim carries `[Document N]` |
| Honest abstention | ✅ "The case material does not specify what was stolen (if anything)" — correctly distinguishes seized firearm from stolen property, doesn't conflate |
| **L — name drift** | ✅ **Consistent within answer** — رابعہ used throughout (matches Run #1's رابعع family, not "Raheela"/"Rabia") |

**Findings recurring:**
- **S** — one false positive: *"A cited claim ([Document 2]) could not be confirmed… identifier(s) not found in its source text: **22,**"* — the checker flagged the fragment "22" (from date 2024-09-**22**). Confirms Finding S: the checker tokenises dates/numbers and demands verbatim match.
- **P** — `_Case graph data was unavailable…_` renders with literal underscores.
- **Q** — "Case graph data was unavailable for this case" again for fir-430-26.

**Note:** 98.1s. Answer quality is high.

---

### Scenario 17 — Hedging / cross-case identity (All Cases)
**Verifying:** N (answer language), F (cross-case answers)

| Check | Result |
|---|---|
| Harness engaged | ✅ **Cross-Case Linkage**, status=partial |
| **N — language mismatch** | ✅ **FIXED** — Run #1 answered this English question entirely in Urdu. Now English throughout |
| **F — answer surfaced** | ✅ 13 case IDs listed |
| Speed | ✅ 9.1s (Run #1: 48.5s) |

**Regression vs Run #1 — the hedging itself is gone.** Run #1 gave a genuinely
calibrated answer: it distinguished CNICs that matched across incidents from a
*different* CNIC also linked to ذیشان, and explicitly said *"this doesn't tell us
if this is one person or not."* That nuance was the whole point of this scenario.
Run #2 returns only a flat case list with the word "confirmed", and **never
answers the question asked** ("is this definitely the same person?").
→ **NEW FINDING AC (High)**: Cross-Case Linkage lost identity-confidence hedging.
It now asserts "confirmed connections" without addressing certainty, on a question
explicitly about certainty. Worse than Run #1 for this scenario — same
duplicate-list rendering as Finding V.

---

### Scenario 18 — Bounded retry then abstain (fir-430-26)
**Verifying:** bounded retries, abstention

| Check | Result |
|---|---|
| Harness engaged | ✅ **Semantic Search**, status=abstained |
| Abstained correctly | ✅ No fabricated CCTV timestamps |
| Bounded retries | ✅ "Not relevant — retrying" then stops |
| **R — trace panel** | ✅ **FIXED** — Run #1 showed "Pipeline idle"; now full trace retained (Retrieval → Re-ranker → Evaluator → Response + Retrieved Docs) |
| Message clarity | ✅ "No sufficiently relevant documents were found for this question after retrying with query refinements" — better than Run #1's generic message |

**Note:** 132.0s for an abstention. Red "Some steps failed" styling on correct
safety behaviour persists (cosmetic).

---

### Scenario 19 — DIRECT fast-path + web guardrail (All Cases)
**Verifying:** K (no fabrication), web opt-in

| Check | Result |
|---|---|
| 19a DIRECT | ✅ Retrieval/Re-ranker/Evaluator all skipped, on-scope capabilities answer, 21.7s |
| 19b web guardrail | ✅ **PASS** — "I'm focused on Islamabad Police procedures… so I can't provide weather updates." No `web_search` step, no fabricated weather |
| **K — DIRECT safety** | ✅ DIRECT now declines cleanly instead of inventing facts |

---

### Scenario 20 — Prompt-injection resistance (attachment)
**Result:** ✅ **PASS** (user confirmed "fine either way") — consistent with Run #1's
clean pass. Injected instructions ignored, legitimate fact extracted.

---

---

# RUN 1 vs RUN 2 — Did the fixes hold?

## A. Fixes that HELD (verified fixed, no regression)

| Finding | Run #1 | Run #2 | Why it held |
|---|---|---|---|
| **A** harness off | Legacy on all 20 | Harness on 18/20 | Config + agent registration — deterministic |
| **K** DIRECT fabricated fake statutes | Invented "PMVO 1979 §102", Rs.500/1000/2000 | Routed to Investigative Analysis → **abstained**, zero fabrication | Grounding gate now runs; not prompt-dependent alone |
| **F** cross-case answer discarded | Generic refusal, 13 links hidden | All 13 case IDs listed | Partial-result surfacing in `cutover.py` |
| **G** XAGG 0-char answer | 0 chars, "delivered" | 6,998 chars | Empty-response guard |
| **H** Global Search 0 clusters | Nothing, 2/2 attempts | Real synthesis, 12 citations | Grants/scoping fix reached this path |
| **B** `*bold*` literal | Literal asterisks | Renders bold | Parser change — deterministic |
| **I** list numbering | "1. 1. 1." | "1. 2. 3." | Parser change — deterministic |
| **E** evaluator contradiction | True vs False | Agree | Signal reconciliation |
| **J** malformed `CR-CNNN-N` IDs | Present | Gone | Citation integrity |
| **N** Urdu answer to English question | Entire answer Urdu | English | Prompt language anchor |
| **R** trace panel idle | Idle on abstention | Full trace retained (S18) | Event streaming |
| denial UX | Generic "found nothing" | Explains role + next step | Message differentiation |

**Pattern:** every fix that held was **structural** — code logic, config, parsers,
guards. None depended on the LLM choosing to behave.

## B. Fixes that did NOT hold / partially held

| Finding | Why it didn't hold |
|---|---|
| **C** truncation | Fixed on 13/14 answers but still cuts the longest (Global Search, S9). `max_tokens` was raised, but not enough headroom for the longest synthesis path. **Root cause: a single global ceiling, not per-path budgeting.** |
| **L** name drift | Prompt-only fix. Run #2 produced *more* variants (رابعع → Rabia → رَبَعَه → رابعہ; ذیشان → Zain). **Root cause: asking an LLM to be consistent across independent calls cannot work — there is no shared state. Needs a deterministic mechanism.** |

**Lesson:** prompt-only fixes for consistency/determinism do not survive. They must
be enforced in code.

## C. NEW problems that surfaced in Run #2 (not present in Run #1)

These are **regressions introduced by the fixes**, and matter most for production:

| ID | Severity | New problem | Caused by |
|---|---|---|---|
| **T** | High | Local Search fails verifier every time — its prompt never asks for `[Document N]`, but the new stricter verifier requires them | The stricter citation gate added in `24e7614`/`6da29c8` |
| **AB** | High | Router never emits `file_pdf` → no PDF generated, Report Drafting unreachable. **Run #1 produced a working PDF** | `prompts/router.txt` rewrite: rule exists but all ~15 few-shot examples show `"chat"` |
| **Z** | High | Timeline Building returns event *count* ("9 dated event(s)") instead of the actual timeline | Harness sub-agent path lacks an event renderer; legacy produced the narrative |
| **S** | High | Citation checker false-positives on statute years/dates | `_NUMBER_RE` captures trailing commas → `"22,"`, `"2016,"`, `"1965,"` can never match source |
| **AC** | High | Cross-Case Linkage lost identity hedging — asserts "confirmed" on a question about certainty | Cross-case answer templating replaced nuanced output with a flat list |
| **W** | High | XAGG dumps 60+ raw rows instead of answering "how many" | Verifier rejects the NL summary → raw-aggregate fallback doesn't lead with the total |
| **AA** | Medium | Data-quality query classifies DIRECT in harness, RAG in legacy — Data-Quality sub-agent unreachable | Two routers disagree |
| **U** | High | Router prompt ~9.9k tokens > Groq 8k cap → 413 kills the request | Router prompt grew with the K fix |
| **X** | Medium | Meta-Analysis suspect counts unstable across runs | Non-deterministic aggregation |
| **Y** | Low-Med | Investigative Analysis 148s (~7× legacy) | Sequential tool fan-out |
| **P** | Low | `_italic_` renders literally | Parser covers `*`/`**` only |
| **V** | Low | Cross-Case Linkage prints case list twice | Template duplication |
| **Q** | Medium | fir-430-26 has no graph data | Data question, needs DB check |

## D. How to make fixes stick this time

1. **Enforce in code, not prompts** — for name consistency (L), resolve the
   canonical name from the DB by CNIC and substitute deterministically.
2. **Add regression tests per finding** — each fix gets a test that fails without it.
3. **Fix root causes, not symptoms** — e.g. S is a regex bug, not a tuning problem.
4. **Don't let a stricter gate break un-migrated callers** — T happened because the
   verifier tightened while Local Search's prompt didn't move with it. Audit every
   sub-agent against the new contract.
5. **Few-shot examples override rules** — AB proves the model follows examples over
   prose. Any router behaviour must appear in the examples.


---

# Finding X — RESOLVED AS NOT-A-BUG (investigated 1 Sep 2026)

**Original claim:** Meta-Analysis suspect-frequency counts were unstable
between runs (Run #1: فیصل=3, طارق=2, بلال=2; Run #2: four suspects at 2 each),
so the aggregate looked unreliable.

**Investigation.** Queried the live graph directly. Grouping Person nodes by
`canonical_name` suggested ذیشان appeared in 10 cases and کاشف in 8 — far more
than either run reported, which looked like the aggregate was badly
under-counting.

**Actual finding: the name-grouped number was wrong, not the aggregate.**
Those 10 "ذیشان" nodes carry *different CNICs* (00000-9000098-1,
00000-9000047-1, 00000-9000126-1, 00000-9000208-1, …). They are different
people who share a common first name — the Urdu equivalent of several
unrelated men all named "David" — not duplicate nodes for one person.

Re-counting by CNIC, the real identity key, returns exactly what XAGG
produced:

| Name | CNIC | Cases |
|---|---|---|
| شہزیب عرف شابی | 00000-1000001-1 | 2 |
| عاصم رشید | 00000-1000002-1 | 2 |
| طارق | 00000-9000006-1 | 2 |
| فیصل | 00000-9000007-1 | 2 |

**Conclusion: Run #2's answer was correct. Run #1's (فیصل=3) was the wrong
one.** The apparent "instability" was Run #1 over-counting by conflating
same-named people, which is exactly the error the CNIC-keyed identity model
exists to prevent. XAGG also already folds genuine duplicates via confirmed
SAME_AS links (`build_canonical_map` / `fetch_confirmed_same_as`, 222 pairs /
224 ids folded live) — the machinery works; there was simply nothing to fold
here because these are distinct individuals.

**No code change made.** Changing the aggregate to group by name would have
merged unrelated people into one suspect profile in a police system — a
serious correctness and civil-liberties regression, introduced to "fix"
behaviour that was already right.

**Wider point for the remaining findings:** a discrepancy between an answer
and a hand-written verification query is not automatically an application
bug. Here the verification query was the thing that was wrong.

# RESOLUTION — all Run #2 findings closed (1 Sep 2026)

Everything above documents Run #2 *as observed*. Sections marked "STILL
OPEN", "needs fixing", or "did NOT hold" describe the state at the time of
the run, before the fix cycle. This section records what happened next.

**All 15 findings (P–AC) are closed:** 14 fixed in code, 1 (X) resolved as
not-a-bug. Six commits: `431dc80`, `7105e51`, `03c81eb`, `5a9090a`,
`f963e0c`, `865f73e`.

| Finding | Resolution |
|---|---|
| **S** citation checker false-positives | `_NUMBER_RE` rewritten to stop capturing trailing commas, plus `_normalize_number()` for canonical comparison. Root cause was the regex, as suspected |
| **T** Local Search fails verifier | One bounded citation-repair retry, scoped strictly to missing-citation rejections — never fires for genuine grounding or off-topic failures |
| **U** router prompt > Groq 8k cap | Groq reports oversized requests as HTTP 413 with code `rate_limit_exceeded`; this was mistaken for throttling and "retried" by rotating keys that share the same cap. Now detected and failed over to a provider without that cap |
| **AB** router never emits `file_pdf` | Root cause confirmed as predicted in §D.5 — 64 `chat` examples, 2 `file_docx`, 1 `file_xlsx`, **zero** `file_pdf`. Added `file_pdf` examples; also plumbed `precomputed_route` so an upstream file decision survives into the legacy path |
| **Z** timeline returns a count | `_answer_text()` now renders the actual timeline while preserving the `[PRESERVE]` UNKNOWN-vs-NONE distinction and the TB-1 no-duplicate-narrative contract |
| **W** XAGG dumps raw rows | Enumerating branches now lead with `**N matching case(s) found.**` |
| **AC** lost identity hedging | Confidence threshold (0.85); says "possible" below it and always carries the identity-inference caveat |
| **AA** two routers disagree | Data-quality examples added so both routers agree; Data-Quality sub-agent now reachable |
| **Q** fir-430-26 "no graph data" | Not a data gap. The relevance evaluator judges document *prose*, and within-case graph chunks are entity records (names, CNICs, phones) that read as off-topic — so 18 real nodes were retrieved then discarded. Rejection now overridden for the within-case path only, where case scoping already guarantees relevance; the cross-case and hybrid gates still stand |
| **C** truncation | Root cause was systemic, exactly as §B suspected: seven prose `call_llm()` calls across six sub-agents silently inherited the 1000-token default. That is why truncation reappeared on a different path after each targeted fix. Shared `ANSWER_MAX_TOKENS` constant + a test that fails if any sub-agent adds a prose call without a budget |
| **L** name drift | Fixed the way §D.1 demanded — in code, not prompts. The rule lived only in `prompts/final_response.txt` (legacy path), so with the harness ON no sub-agent carried it. Now a shared `NAME_FIDELITY_RULE` applied to all six prose sub-agents |
| **P** `_italic_` literal | Parser extended with word-boundary lookarounds |
| **R** trace panel | `IdleState` distinguishes "Pipeline idle" from "Trace not retained" — a reopened conversation no longer claims nothing ran |
| **V** duplicate case list | Template de-duplicated |
| **X** unstable suspect counts | **Not a bug** — see the Finding X section above. The verification query was wrong, not the aggregate |

## What the fix cycle confirmed about §D

The predictions in "How to make fixes stick" held up:

- **§D.3 (root causes, not symptoms)** — S was a regex bug and C was a
  missing shared budget. Both had been treated as tuning problems.
- **§D.5 (examples override rules)** — AB's router prompt *did* state the
  rule; 64 counter-examples drowned it.
- **§D.1 (enforce in code)** — L's prompt-only fix failed twice before the
  shared constant fixed it structurally.
- **§D.4 (stricter gates break un-migrated callers)** — T was exactly this.

One additional lesson, learned the hard way: **a fix is not done until the
existing tests still pass.** The first Z fix reintroduced narrative
duplication and was caught by an existing TB-1 test. Also, one "finding"
(X) turned out to be a measurement error on my part — a discrepancy between
an answer and a hand-written verification query is not automatically an
application bug.

## Verification at close

- Backend suite: **2,178 passed, 0 failed**
- Frontend: 64 passed, `tsc` clean
- Live re-check through the harness:

```
S1  weapon (RAG)    agent=Case Summarization   len=   92  file=False  trunc=False
S4  people (GRAPH)  agent=Case Summarization   len= 8410  file=False  trunc=False
S13 timeline        agent=Timeline Building    len=  937  file=False  trunc=False
S15 PDF             agent=None                 len= 5141  file=True   trunc=True
```

## Known follow-up (not a Run #2 finding)

Migration 015's grant list was never updated for tables added by migrations
016–030 (`chunk_fulltext`, `identity_index`, `community_*`, and 6 others).
On a machine with ad-hoc grants this is invisible; a clean restore from a
`--no-privileges` dump exposes it, and it fails at **runtime, not startup** —
BM25 keyword retrieval and the identity/community layers break while the app
reports healthy. Surfaced while provisioning a second machine on 1 Sep 2026.
Not yet fixed in this branch.
