# Muhafiz — Module 29: Meta-Analysis doesn't decompose broad synthesis questions

**Status:** Task brief. **Blocked until PR #16 (Module 25) merges** — it owns
the same file, and this module depends on understanding the decomposer *after*
25's changes.
**Branch to create:** `fix/meta-analysis-decompose-broad-synthesis`
**Split out of:** Module 21 (PR #12) — read its investigation writeup first.
**Read first:** `WAVE2_ORCHESTRATION_PROMPT.md`, then
`GOLD_QA_REMAINING_FIXES_PLAN.md`. **Update both when you finish** (§7).

**Parallel-safe:** no, against Module 25. Your file is
`src/pipeline/harness/agents/meta_analysis.py`. Do 25 then 29 sequentially in
one chat/worktree (Track C). Tracks A (`xagg.py`), B (`router.py`) and D
(retrieval) are disjoint.

---

## 0. The questions

Three questions, all scoring 0.0, all failing the same way.

**CR3:** *"In the online banking fraud matter involving two separate victims,
was each victim's case processed and recorded the same way?"*

**G1:** *"Acting as a crime analyst, review our current caseload and flag
anything that looks unusual or worth monitoring."*

**G6:** *"Yahan naye tainaat hone wale afsar ke liye ek mukhtasar orientation
note likhein — unhein mojooda case load se kya tawaqqo rakhni chahiye?"*
(Write a short orientation note for a newly posted officer.)

Read all three gold answers in full in
`evaluation/Gold_QA_Dataset_Final32_With_Answers.json` before starting. The
essential observation: **each gold answer is assembled from several
independently computable facts**, not retrieved from any single place.

## 1. Why they fail

All three route to XNETWORK. Meta-Analysis's `_decompose()` evidently returns
`decompose: false` for each — only **one** sub-agent dispatch appears per
query in the live SSE trace, not several. Meta-Analysis therefore falls back
to re-dispatching the **original, un-split** query, which lands straight back
on the XNETWORK / Cross-Case-Linkage path and correctly finds no relevant
community cluster.

Module 21 confirmed the relevance gate is right to refuse here (measured
distances: CR3 0.156, G1 0.202, G6 0.181, against a 0.145 cutoff). **The gate
is not the bug.** The bug is that these questions should never have arrived at
the community layer as a single monolithic query.

### What each one actually decomposes into

- **CR3** — two per-FIR field-consistency checks: does 64/26 have a matching
  walk-in complaint, and does 65/26? That is Module 15's cross-check territory
  (`_cms_fir_linkage()`).
- **G1** — offender age range and average; accused–complainant relationship
  breakdown; seized-property and forensic-lab counts; incident time-of-day
  distribution.
- **G6** — case-mix-by-year trend; arrest rate; reporting-delay trend; weapon
  licensing rate.

Each of those sub-facts should route to **XAGG** once decomposed and
re-dispatched through `Supervisor().handle()`.

## 2. The work

Extend the decomposer so a broad, open-ended analytical or "orientation note"
question triggers `decompose: true` with sub-queries shaped like countable
facts, each independently routable.

### Do the gap analysis first, and write it down

Before touching the prompt, determine **which of the needed XAGG primitives
already exist** versus which are missing. Several likely exist already from
Modules 13–15 — the age, relationship, seized-property, time-of-day,
case-mix-by-year, arrest-rate and weapon-licensing aggregates are all
plausible existing paths. Probe each one live with a direct sub-question
before assuming.

**That gap analysis, not this module, determines whether new `xagg.py`
aggregates are also needed.** If some are missing, **track them as new
Module 22/23/24-style additions in the plan rather than silently expanding
this module's scope**. This project's practice is that a newly discovered gap
becomes its own module — that is how 19b, 28, 29 and 30 were all found.

### The decomposition trigger

The risk is symmetrical, so state how you handled both sides:

- Trigger too narrowly and CR3/G1/G6 keep failing.
- Trigger too broadly and simple questions get decomposed into sub-queries,
  which is slower, costs more model calls, and can turn a currently-correct
  single-fact answer into a muddled synthesis.

Negative-control against the gold questions that Meta-Analysis and the
single-fact paths currently answer **correctly**, and report which ones you
checked.

## 3. Objective

Live CR3, G1 and G6 return real, gold-comparable synthesized answers built
from computed facts — while questions that currently answer correctly without
decomposition still do.

## 4. Verification — both halves required

**Unit:** `tests/test_harness_agent_meta_analysis.py` full pass, plus:
- tests pinned to the **literal decomposition** of each of CR3, G1 and G6 —
  assert `decompose: true` and assert the sub-query shapes;
- a **negative control**: a simple single-fact question still returns
  `decompose: false`;
- Module 25's own tests must still pass — you are editing its file right after
  it landed.

Use the shared interpreter with `PYTHONPATH=.` (orchestration doc §2.1).

**Live:**
1. `docker compose up -d postgres`, wait for `(healthy)`.
2. Confirm the model-server tunnel is alive (orchestration doc §3.3). This
   module makes many model calls per question — decomposition multiplies them.
3. Each of CR3, G1 and G6's exact gold text through `/api/chat` as
   `admin@example.com` / `MuhafizAdmin2026!`, All Cases.
4. From the **raw SSE stream**, confirm you now see **several** sub-agent
   dispatches per query rather than one, and capture where each sub-query
   routed. **Meta-Analysis emits a second route event for its decomposed
   sub-query** — that trap is exactly what this module's evidence depends on
   reading correctly, so parse the whole stream, not the last event.
5. Compare each synthesized answer against gold.

**Non-gold paraphrase** (required, at least one): G1's own *"look over
everything currently open"* paraphrase is already captured in Module 21's
writeup — use it or another that shares no distinctive keywords.

**Regression guard:** re-run **M2** (Module 25's question, in this same file)
and confirm 25's fix still holds. Re-run at least one question Meta-Analysis
already answers acceptably. Report both.

## 5. Watch for

- **Do not touch `xnetwork.py` or its threshold.** Module 12 set it from
  measured distances and Module 21 re-confirmed it. These three questions
  should stop *reaching* the community layer as monolithic queries; the gate
  itself stays.
- Decomposition multiplies model calls. Watch the Groq/Gemini key rotation
  and quota behaviour during live runs, and note in the PR if you hit limits —
  a quota failure mid-run looks like a decomposition bug.
- Free RAM on this machine is ~5 GB. Run this track's live verification alone
  (orchestration doc §2.4).
- A full `pytest -q` run empties the `muhafiz_entity_descriptions` Chroma
  collection; re-run `refresh_entity_embeddings()` before live checks.

## 6. Git discipline

- Branch off current `main` **after PR #16 merges**:
  `git checkout main && git pull && git checkout -b fix/meta-analysis-decompose-broad-synthesis main`
- Author every commit as
  `rayyanfaisal475207 <rayyanfaisal475207@users.noreply.github.com>`.
- Keep the `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` trailer.
- End the PR description with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- `main` is protected — the **"Backend tests"** check must pass.
- Every number in your writeup traces to a captured live output.

## 7. Required: update the tracker when you finish

In the same PR, update `GOLD_QA_REMAINING_FIXES_PLAN.md`:

1. Module 29's **status row** → `✅ PR #<n> open` / `✅ Merged`.
2. Module 29's **own section** — the decomposition trigger you added, the
   sub-queries each of CR3/G1/G6 now produces, and where each routed.
3. **Publish the gap analysis**: which XAGG primitives already existed and
   which were missing. Add a new module section for each missing one rather
   than building it here.
4. State the **M2 regression result** and your decomposition negative control.
5. If `_decompose()` turns out to be returning `decompose: true` after all and
   the failure is elsewhere, correct the plan's framing — this brief's claim
   rests on an SSE trace read before Module 25 changed the file.
