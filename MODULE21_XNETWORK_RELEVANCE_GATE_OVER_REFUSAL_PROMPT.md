# Muhafiz — Module 21: the XNETWORK relevance gate now fails closed on five questions

**Status:** Task brief, ready to implement.
**Branch to create:** `fix/xnetwork-relevance-gate-over-refusal`
**Authoritative tracker:** `GOLD_QA_REMAINING_FIXES_PLAN.md` — read its
"Which modules can run in parallel" section first, then **update that file's
status table and this module's section when you finish** (see §6 below;
required part of the module).

**Parallel-safe:** yes. This module's primary file is
`src/pipeline/xnetwork.py`, plus possibly
`src/pipeline/harness/agents/cross_case_linkage.py`. Modules 19b, 25 and 26
are running concurrently against `prompts/evaluator.txt`,
`meta_analysis.py`/`verifier.py`, and `router.py`. **One caution:** Module
25 touches `src/pipeline/verifier.py` — if your fix starts reaching into the
verifier, stop and coordinate rather than both editing it.

---

## 0. The situation

Module 12 fixed a real, serious defect (RC-1): broad/evaluative questions
were being answered with a wall of irrelevant community-cluster narration —
*"This cluster centers on FIR X…"* — that had nothing to do with what was
asked. It added a relevance gate in `src/pipeline/xnetwork.py`:

```python
RELEVANCE_DISTANCE_THRESHOLD = 0.145
```

chosen from **real measured cosine distances** against the live
`muhafiz_community_reports` collection: the 6 known-bad questions ranged
0.1558–0.2057, four realistic should-match controls ranged 0.1256–0.1395.
0.145 sits in that gap. When nothing clears the gate, `run_network_query()`
returns empty plus an honest `no_relevant_reason` naming the question and
the nearest distance found.

**That fix worked — and it now fails the other way.** Per
`MODULE_18_FINAL_REPORT.md` §6 and `GOLD32_RESULTS_FOR_TEAMMATE.md` §6, five
questions now refuse where a real answer was expected:

| Q | Route | Score | Symptom |
|---|---|---|---|
| CR3 | XNETWORK | 0.2 | "no cross-case connections or patterns were found" |
| CR4 | XGRAPH | 0.0 | same — claims no connections; gold expects a real chain |
| CS4 | XGRAPH | 0.0 | `status=error` — timeout, unverified state |
| G1 | XNETWORK | 0.0 | "no patterns or connections found" |
| G6 | XNETWORK | 0.0 | judge: "unwarranted refusal / abstention" |

These previously failed **open** (confidently wrong) and now fail **closed**
(no answer). Honest, but no score gain.

## 1. ⚠️ Do NOT just raise the threshold

This is the obvious move and it is wrong. 0.145 was derived from measured
distances with only a **~0.016** gap between the bad questions and the good
controls. Raising it re-admits precisely the cluster dumps Module 12
existed to stop — trading five closed failures for six open ones.

If after investigation you conclude the threshold genuinely needs to move,
you must **re-measure** distances against the current live collection (the
corpus has changed since Module 12 — the KB re-ingest added 6,926 chunks)
and show the new bad-vs-good separation, exactly as Module 12 documented its
own evidence in the constant's comment. Assertion is not evidence.

## 2. The real gap to investigate

"No relevant **community cluster**" is being treated as "no answer at all".
For several of these, the answer doesn't live in community summaries:

- **CR4** — *"If we've got a weapon logged as evidence, can we tell who it
  was taken off and what happened to them?"* Gold: the 30-bore pistol in FIR
  891/24 → recovered from شہزیب عرف شابی → status "گرفتار، بعد ازاں سزا
  یافتہ". That is a **concrete graph traversal chain** (weapon → FIR →
  accused → status), not a community-summary question. Note this exact chain
  was confirmed working live during Module 17 when asked with the FIR number
  named — so the data and the traversal both exist.
- **CR3** — *"…was each victim's case processed and recorded the same way?"*
  Gold is a per-record comparison (64/26 has a matching walk-in complaint,
  65/26 has none) — a field-consistency join, closer to Module 15's
  territory than a cluster narration.
- **G1 / G6** — genuinely broad synthesis. Gold for G1 wants specific
  profiled findings (offender ages 24–49, relationship-field skew, etc.);
  G6 wants an orientation note. These need aggregate/profile data, not
  nearest-cluster text.

**Investigate in this order:**

1. For each of the 5, capture **which path actually runs** (XNETWORK
   community lookup vs. XGRAPH traversal vs. Meta-Analysis dispatch) and
   what it returns *before* the gate applies. Read the raw SSE stream, not
   just the final answer — and beware the known parsing trap: Meta-Analysis
   emits a *second* route event for its decomposed sub-query, so a naive
   "keep the last route event" parse is misleading (documented in the master
   plan's Module 11 section).
2. For **CR4** specifically, determine whether it should be reaching
   XGRAPH's traversal at all rather than the community layer. If it's a
   routing problem, the fix may not belong in `xnetwork.py` — say so rather
   than forcing a fix into the wrong file.
3. **Prefer a fallback over a threshold change.** When the community gate
   finds nothing relevant, consider falling through to the graph/aggregate
   path instead of immediately returning the honest-refusal message. The
   refusal message stays as the genuine last resort — Module 12's fix
   preserved, its blind spot covered.

## 3. Objective

The five questions produce real, grounded answers where the underlying data
supports one — **without** reintroducing RC-1's cluster-dump behavior for
questions where no relevant cluster genuinely exists.

An honest refusal remains the correct output when there really is nothing;
the goal is that a refusal is the *last* resort after other available paths
have been tried, not the first response to "no nearby cluster".

## 4. Verification — both halves required

**Unit:** `tests/test_xnetwork.py`, `tests/test_harness_tool_xnetwork.py`,
`tests/test_harness_agent_cross_case_linkage.py` full pass (these cover the
gate's relevant/no-relevant/boundary cases and `no_relevant_reason`
propagation through all three EMPTY branches). Add cases for whatever new
fallback path you introduce.

**Live:** all 5 gold questions (CR3, CR4, CS4, G1, G6) through real
`/api/chat` as `admin@example.com` / `MuhafizAdmin2026!`, All Cases, with
Postgres healthy and the model server up. Capture the actual answers.

**The RC-1 regression check is mandatory.** Module 12 documented its
before/after for G1 and CR3 word-for-word in the master plan (the
"cross-regional cybercrime coordination" cluster dump for G1; the
"C-20260903-L0-0011 … cybercrime under PECA 2016" dump for CR3). Re-run
those two and confirm **the old cluster-dump answer does not return**. A fix
that restores answers by restoring dumps is a regression.

**Non-gold paraphrase** (required): e.g. *"As the on-duty analyst, look over
everything currently open and tell me what's worth a second look"* — the
same paraphrase Module 12 used, so the two modules' evidence is comparable.

**CS4 note:** it fails with `status=error` (timeout), not a refusal. Confirm
whether that's the same root cause or a separate performance problem — if
separate, it may deserve its own module rather than being folded in here.

## 5. Git discipline

- Branch off current `main`: `git checkout main && git pull && git checkout
  -b fix/xnetwork-relevance-gate-over-refusal main`
- **Author every commit as `rayyanfaisal475207
  <rayyanfaisal475207@users.noreply.github.com>`.**
- Keep the `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` trailer.
- End PR descriptions with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- Push and open a PR against `main` (protected; "Backend tests" must pass).
- Every number in your writeup traces to a captured live output.

## 6. Required: update the plan file when you finish

Update `GOLD_QA_REMAINING_FIXES_PLAN.md` **in the same PR**:

1. **Status snapshot table** — Module 21's row → `✅ PR #<n> open` / `✅
   Merged`.
2. **Module 21's own section** — replace the investigation steps with what
   you found: which path each of the 5 actually took, whether the fix was a
   fallback / a routing change / a re-measured threshold, and the
   before/after answer for at least CR4 and G1.
3. **Record the RC-1 regression evidence** — explicitly state that G1's and
   CR3's cluster dumps did not return, with the actual answer text.
4. **If CR4 turns out to be a routing problem** (not an xnetwork one), or
   **CS4 turns out to be a separate timeout defect**, add them as their own
   module sections with a status-table row rather than absorbing them
   silently — and update the parallelization table if the new module touches
   `router.py` (Module 26's file).
