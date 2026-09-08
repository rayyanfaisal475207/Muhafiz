# Muhafiz — Module 23: M5 weapon × statute co-occurrence join

**Status:** Task brief, ready to implement.
**Branch to create:** `feature/xagg-weapon-statute-cooccurrence`
**Read first:** `WAVE2_ORCHESTRATION_PROMPT.md` (infra, machine constraints,
verification standard), then `GOLD_QA_REMAINING_FIXES_PLAN.md`.
**Update both when you finish** — see §7, a required part of the module.

**Parallel-safe:** partly. Your file is `src/pipeline/xagg.py`, which
**Module 24 also owns**. Run 23 and 24 sequentially in one chat/worktree
(Track A), 23 first. Module 28 (`router.py`) and Module 30 (retrieval) are
disjoint and may run concurrently.

---

## 0. The question

**M5**, asked in Urdu:

> ہتھیار عام طور پر کس نوعیت کے مقدمات میں سامنے آتے ہیں، اور کیا 2024 کے مقابلے میں اب یہ نوعیت بدل گئی ہے؟

*(Roughly: in what kinds of cases do weapons typically show up, and has that
changed compared with 2024?)*

**Gold answer** — a two-year co-occurrence contrast:

- **2024:** Arms Ordinance §13 appears **only** alongside robbery sections
  (PPC 34 and PPC 392, 13 cases each).
- **2026:** it also appears with **narcotics** (CNSA §9(c), 8 cases) and
  **murder** (PPC 302, 8 cases), alongside the same robbery sections (8 each).
- The point of the answer: weapons are no longer confined to robbery.

Note the shape. Gold is not a statute ranking — it is *which statutes a
weapon charge co-occurs with*, per year, and how that set changed.

## 1. Why it fails today

M5 currently reaches XAGG (confirmed 2026-09-08 — it needs **no router
change**, which is why it cannot collide with Module 28) and dispatches to
`_statute_mix_by_year()` via the `_TIME_COMPARISON_KEYWORDS` check in
`xagg.py`'s dispatch chain.

That aggregate returns a valid but **unrelated** answer: a flat per-year
breakdown of statutes across all cases. It has no weapon dimension at all, so
it can never express "§13 pairs with robbery in 2024 and with narcotics and
murder in 2026". The failure is a **missing primitive**, not a bad one.

No existing aggregate joins weapons to their case's statute set. Verify that
claim yourself before writing code — read the dispatch chain end to end.

## 2. The work

Add one aggregate to `src/pipeline/xagg.py`: for each case that has a Weapon,
collect that case's statute set, group by incident year, and count the
weapon-type × statute pairings.

**Follow `_criminal_record_court_crosscheck()` (Module 14) for structure** —
same shape family (a join across two per-case attributes with a renderer),
and it is the house style for this kind of aggregate.

Concrete pieces already in the file that you should reuse rather than
reinvent:

- `_statute_mix_by_year()` — how a year is resolved per case, via each
  Incident's own `OCCURRED_ON -> Date` edge, then `split_crime_category()`
  over the gateway's case rows. Your year bucketing should match it exactly,
  or the two aggregates will disagree about what year a case is in.
- `_normalize_weapon_type()` — already exists; use it so "30 bore pistol" and
  "30-bore pistol" do not split into two rows.
- `_extract_year()` / `_count_breakdown_by_year()` — the existing year
  primitives.
- A `render_*()` function alongside the others, following
  `render_criminal_record_crosscheck()`'s conventions.

### Dispatch placement — the part most likely to go wrong

`xagg.py`'s dispatch is an **ordered** chain of `_matches_any(query_lower,
_X_KEYWORDS)` checks; first match wins. Your new check must be inserted so
that:

- It comes **before** `_TIME_COMPARISON_KEYWORDS` (which currently swallows
  M5 into `_statute_mix_by_year()`).
- It does **not** capture G5, which is `_WEAPON_TERMS` **and**
  `_COMPLIANCE_TERMS` (weapon-register licence compliance). G5 currently
  scores 1.0 — do not break it.
- It does **not** capture the bare-weapon recurrence aggregate, or the
  Module 1c district+weapon path (`_DISTRICT_KEYWORDS`), both of which the
  chain comments call out as prior collision sites.
- It does **not** capture M7's reporting-speed shape, checked above it.

The distinguishing signal for M5 is **weapon terms + a case-type/statute term
+ a change-over-time term**. A bare weapon term must keep falling through.
Write the keyword tuple with its own comment in the file's established style,
naming the question and module — every other entry in that chain does.

## 3. Objective

Live M5 names the real per-year statute pairings, in the shape gold uses (the
2024 robbery-only set versus the 2026 narcotics/murder expansion), computed
from real data — not a statute ranking, and not a hardcoded narrative.

If the real data does **not** reproduce gold's numbers, say so with the
captured query output rather than bending the aggregate to hit them. A
wrong-but-matching number is worse than an honest mismatch, and this plan has
already had one module mis-scoped by inheriting a figure instead of
re-deriving it.

## 4. Verification — both halves required

**Unit:** `tests/test_xagg.py` full pass, plus new tests:
- the aggregate returns the expected pairings from a fixture with a known
  weapon/statute/year mix;
- a **regression test pinned to M5's literal Urdu gold text** routing to your
  new aggregate rather than `_statute_mix_by_year()`;
- **negative controls**: G5's literal text still reaches
  `_weapon_compliance_scan()`, and M1's text still reaches
  `_statute_mix_by_year()`.

Run with the shared interpreter and `PYTHONPATH=.` (see the orchestration
doc §2.1) or you will be testing the main checkout's `src/`.

**Live:**
1. `docker compose up -d postgres`, wait for `(healthy)`. A dead Postgres
   reads as a *hang*, not an error.
2. Confirm the model-server tunnel is alive — `/health` on the backend does
   **not** cover it (orchestration doc §3.3).
3. M5's exact Urdu gold text through `/api/chat` as `admin@example.com` /
   `MuhafizAdmin2026!`, All Cases.
4. From the raw SSE stream, confirm `route=` reaches XAGG and your new
   aggregate — not `_statute_mix_by_year()`.
5. Capture the actual per-year pairings and compare against gold.

**Non-gold paraphrase** (required): e.g. *"Are guns turning up in different
types of cases than they used to?"* — plain English, no Urdu statute
vocabulary, no shared keywords with M5's literal phrasing. This is what
proves a capability fix rather than curve-fitting.

**Regression guard:** re-run **G5** (weapon-register compliance, currently
1.0) and **M1** (year-over-year statute mix, fixed by Module 26 and merged).
Both share your keyword space. State both results in the PR.

## 5. Watch for

- **Do not add a `router.py` override.** M5 already reaches XAGG. If your
  live `route=` capture says otherwise, stop and coordinate with Module 28
  rather than editing `router.py` — that is the one thing that would break
  Track A/B independence.
- Urdu text through a PowerShell pipe mojibakes. Never take a database dump
  that way (see `SHARE/database/create_dump.ps1`'s header); the same caution
  applies to any capture you paste into the PR.
- A full `pytest -q` run empties the `muhafiz_entity_descriptions` Chroma
  collection. If you run the whole suite, re-run `refresh_entity_embeddings()`
  before live verification.

## 6. Git discipline

- Branch off current `main`:
  `git checkout main && git pull && git checkout -b feature/xagg-weapon-statute-cooccurrence main`
- Author every commit as
  `rayyanfaisal475207 <rayyanfaisal475207@users.noreply.github.com>`.
- Keep the `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` trailer.
- End the PR description with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- `main` is protected — the **"Backend tests"** check must pass.
- Every number in your writeup traces to a captured live output.

## 7. Required: update the tracker when you finish

In the same PR, update `GOLD_QA_REMAINING_FIXES_PLAN.md`:

1. Module 23's **status row** → `✅ PR #<n> open` / `✅ Merged`.
2. Module 23's **own section** — replace the hypothesis with the finding: the
   aggregate you added, where it sits in the dispatch chain and why there,
   the real per-year pairings you measured, and whether they match gold.
3. State the **negative-control result for G5 and M1** explicitly.
4. If M5 turned out **not** to reach XAGG, or the co-occurrence data is not
   derivable from the graph as this brief assumes, correct the plan's framing
   of this module rather than working around it — and add a new module
   section for whatever you found instead of widening this one.
