# Muhafiz — Module 28: CR4's weapon-evidence chain is routed to cross-case linkage

**Status:** Task brief, ready to implement. **Previously had to serialize
after Module 26 — that merged (PR #13), so this is now unblocked.**
**Branch to create:** `fix/router-weapon-evidence-chain-to-xagg`
**Split out of:** Module 21 (PR #12) — read its investigation writeup for the
full live evidence: the CR2 control probe, the `_recover_target_entity()`
read, and the captured community distances.
**Read first:** `WAVE2_ORCHESTRATION_PROMPT.md`, then
`GOLD_QA_REMAINING_FIXES_PLAN.md`. **Update both when you finish** (§7).

**Parallel-safe:** yes. Your file is `src/pipeline/router.py`. Track A
(Modules 23/24, `xagg.py`) and Track D (Module 30, retrieval) are disjoint
and can run concurrently. **Module 26 owned this same file and has merged** —
branch off current `main` so you have its changes.

---

## 0. The question

**CR4:**

> If we've got a weapon logged as evidence, can we tell who it was taken off
> and what happened to them?

**Gold answer:** Yes — e.g. the 30-bore pistol logged as evidence in **FIR
891/24** was recovered from **شہزیب عرف شابی**, whose recorded status on that
case is *گرفتار، بعد ازاں سزا یافتہ* (arrested, later convicted). Gold is
careful to add that the trail runs weapon → FIR number → accused, and relies
on **matching the FIR number rather than an enforced database key**.

CR4 currently scores **0.0**.

## 1. Why it fails — and why the fix is not in `xnetwork.py`

Module 21 investigated this as a relevance-gate over-refusal and concluded
the gate is **correctly calibrated**: CR4's nearest community distance is
0.184 against a 0.145 cutoff, so refusing was right. The defect is upstream,
in **routing**.

CR4 names no entity. It therefore routes to XGRAPH's cross-case
recurring-entity traversal via Cross-Case Linkage — a tool for finding
patterns of recurrence across cases. CR4 is not that. It asks for **one
example chain**: weapon → FIR → accused → status.

**The data and a working query path both already exist.** Module 21's control
probe of **CR2** (a genuine cross-case recurrence question) showed XAGG's
Large-Scale Aggregate sub-agent resolving person-across-cases data correctly,
including the exact person CR4's gold names (شہزیب عرف شابی, fir-891-24). So
this is a routing miss, not a missing capability.

## 2. The work

Add a router classification that sends "weapon logged as evidence → who was
it taken from and what happened to them" questions to **XAGG** rather than
XGRAPH, backed by an aggregate that surfaces a weapon → FIR → accused →
status chain (given a weapon-type filter, or simply "give one example").

Before writing the router change, **check whether the aggregate already
exists**. `xagg.py` has a weapon family (`_normalize_weapon_type()`,
`_weapon_compliance_scan()`, a weapon recurrence path). If an existing
aggregate already produces the chain, this module is router-only. If it does
not, adding it here is in scope — but say so explicitly in the PR, and keep
it a separate commit from the routing change.

**Follow Module 26's own M1 fix as the pattern** for how a deterministic
routing override is written and negative-controlled in this codebase. Read
its diff (PR #13) before starting; it is the closest precedent and it also
touched `supervisor.py`, so check whether your change needs to as well.

### Keyword discipline — the part most likely to go wrong

Mine the pattern from **CR4's literal gold text**, then negative-control it
against **all 32 gold questions**. The specific hazard Module 21 flagged: a
keyword that also matches **CR2** would misroute a question that currently
works. CR2 is a genuinely different shape — cross-case recurrence, not a
single example chain — so the discriminator has to be the *"traced back to an
individual"* framing, not the bare presence of weapon vocabulary.

`xagg.py`'s existing weapon entries show the house technique for this: require
a **conjunction** of term families rather than a single keyword, and comment
the entry with the question and module it serves.

## 3. Objective

Live CR4 returns a real weapon → FIR → accused → status chain from actual
data, and **CR2 is unaffected**.

Gold's own hedge — that the link depends on FIR-number matching rather than an
enforced key — is part of a good answer, not a defect to hide. If the chain is
only reconstructible by string-matching FIR numbers, the answer should say so.

## 4. Verification — both halves required

**Unit:** `tests/test_router.py` full pass, plus:
- a **regression test pinned to CR4's literal gold text** routing to XAGG;
- a **negative-control test over all 32 gold questions** asserting that no
  other question's route changes. This is not optional for a routing module —
  it is the check that would have caught the M4/G3 collision (PR #8) before
  it shipped;
- if you add an aggregate, its own tests in `tests/test_xagg.py`.

Use the shared interpreter with `PYTHONPATH=.` (orchestration doc §2.1) or
you will be testing the main checkout's `src/`.

**Live:**
1. `docker compose up -d postgres`, wait for `(healthy)`.
2. Confirm the model-server tunnel is alive (orchestration doc §3.3).
3. CR4's exact gold text through `/api/chat` as `admin@example.com` /
   `MuhafizAdmin2026!`, All Cases.
4. From the **raw SSE stream**, confirm the `route=` event reaches XAGG.
   **Beware the known parsing trap:** Meta-Analysis emits a *second* route
   event for its decomposed sub-query, so a naive "last route event wins"
   parse is misleading. Read the whole stream.
5. Capture the returned chain and compare against gold's FIR 891/24 example.

**Non-gold paraphrase** (required): *"For weapons we've seized as evidence,
can we trace them back to whoever they were taken from?"* — or another
phrasing sharing no distinctive keywords with CR4's literal text.

**Regression guard:** re-run **CR2** live and confirm it is unchanged — this
is the specific collision Module 21 warned about. Also confirm **G5**
(weapon-register compliance, currently 1.0) still routes as before, since it
lives in the same weapon keyword space. Report both.

## 5. Watch for

- **Do not raise `RELEVANCE_DISTANCE_THRESHOLD`.** Module 12 chose 0.145 from
  measured distances, and Module 21 re-confirmed it. Raising it re-admits the
  RC-1 cluster dumps this project already fixed once. Nothing in this module
  should touch `xnetwork.py`.
- If your investigation shows CR4 *should* stay on XGRAPH and the real defect
  is in the traversal, say so and correct the plan — shipping an investigation
  plus a regression test is a legitimate outcome here, exactly as Module 21
  did.
- A full `pytest -q` run empties the `muhafiz_entity_descriptions` Chroma
  collection; re-run `refresh_entity_embeddings()` before live checks if you
  ran the whole suite.

## 6. Git discipline

- Branch off current `main` (which now contains Module 26):
  `git checkout main && git pull && git checkout -b fix/router-weapon-evidence-chain-to-xagg main`
- Author every commit as
  `rayyanfaisal475207 <rayyanfaisal475207@users.noreply.github.com>`.
- Keep the `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` trailer.
- End the PR description with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- `main` is protected — the **"Backend tests"** check must pass.
- Every number in your writeup traces to a captured live output.

## 7. Required: update the tracker when you finish

In the same PR, update `GOLD_QA_REMAINING_FIXES_PLAN.md`:

1. Module 28's **status row** → `✅ PR #<n> open` / `✅ Merged`.
2. Module 28's **own section** — the routing rule you added, the exact
   discriminator that separates CR4 from CR2, whether an aggregate already
   existed or you added one, and the live chain you captured.
3. Paste the **all-32 negative-control result** and the **CR2 and G5**
   regression results explicitly.
4. If the root cause turns out not to be routing, correct the plan's framing
   and add a new module section for what you actually found rather than
   widening this one.
