# Muhafiz — Module 24: M4 statute × court-stage join

**Status:** Task brief, ready to implement. **Previously blocked on PR #8 —
that has merged, so this is now unblocked.**
**Branch to create:** `feature/xagg-statute-court-stage-join`
**Read first:** `WAVE2_ORCHESTRATION_PROMPT.md`, then
`GOLD_QA_REMAINING_FIXES_PLAN.md`. **Update both when you finish** (§7).

**Parallel-safe:** partly. Your file is `src/pipeline/xagg.py`, shared with
**Module 23**. Run **23 first, then 24**, in the same chat/worktree (Track A).

---

## 0. The question

**M4**, asked in Urdu:

> ایک طرف یہ دیکھیں کہ لوگوں پر کن دفعات میں مقدمے بن رہے ہیں، اور دوسری طرف یہ کہ وہ مقدمے عدالت میں کہاں تک پہنچے — کیا دونوں سے کیس لوڈ کی سنگینی کا ایک ہی اندازہ ہوتا ہے؟

*(Look at which sections people are being charged under, and separately at how
far those cases have got in court — do the two give the same impression of how
serious the caseload is?)*

**Gold answer:** No — the two disagree. FIR sections already show serious
crime is common, but of **33 criminal records only 1 is a final conviction;
30 are still under trial**. The conclusion gold draws: judge current caseload
severity by FIR/section counts, not by conviction counts, which lag.

This is explicitly a **two-halves-and-compare** question. An answer that
reports only one half is wrong even if that half is accurate.

## 1. Why it fails today

Two separate defects, one already fixed:

1. **Routing (fixed).** M4 used to be misrouted by a bare Urdu "court"
   keyword colliding with G3's court-readiness space. **PR #8 fixed that and
   is merged.** Re-confirm the current routing live before assuming anything
   about it — this brief's remaining claims were written before that merge.

2. **Missing join (yours).** After PR #8, M4 gets the statute half right
   (PPC 61, Arms Ordinance 29) but then claims **no court-stage data
   exists**. It does exist, in the criminal-record table — 33 records, 1
   conviction, 30 under trial — and it is the same source
   `_criminal_record_court_crosscheck()` (Module 14) already reads
   successfully for CR7.

So the data and a working reader both already exist. What is missing is an
aggregate that puts the statute counts and the court/conviction stage side by
side and lets the answer say whether they agree.

## 2. The work

Add an aggregate to `src/pipeline/xagg.py` that joins statute counts with
court/conviction stage, **reusing Module 14's own record reader rather than
opening a second query path to the same table**. Two readers over the same
data will drift apart, and this file already carries comments about exactly
that class of mistake.

Reuse, do not reinvent:

- `_criminal_record_court_crosscheck()` — the existing reader and its
  `_conviction_is_settled()` helper, which already encodes what counts as a
  settled conviction versus still-under-trial. Do not re-derive that rule.
- `_fir_key()` — the existing FIR-number normalizer for matching records to
  cases.
- `split_crime_category()` and the year/statute handling used by
  `_statute_mix_by_year()`, so statute naming matches the rest of the file.
- A `render_*()` function following `render_criminal_record_crosscheck()`.

The renderer matters more than usual here: gold's value is the **comparison**,
so the rendered evidence must present both halves and state whether they
agree, not just emit two number lists and leave the synthesis to the model.

### Dispatch placement

Insert your check into the ordered dispatch chain so that:

- It wins for M4's shape (statute/section terms **and** court-stage terms).
- It does **not** capture **G3** (`_COURT_READINESS_KEYWORDS`) — G3 and M4
  share the court-readiness keyword space, and untangling them is precisely
  what PR #8 had to do. Read PR #8's diff before writing your keywords.
- It does **not** capture **CR7** (`_CRIMINAL_RECORD_KEYWORDS`), which must
  keep reaching `_criminal_record_court_crosscheck()` directly.
- It does not fall into `_TIME_COMPARISON_KEYWORDS` or the `_TREND_KEYWORDS`
  refusal.

Comment the entry in the file's established style, naming the question and
module, as every other entry in that chain does.

## 3. Objective

Live M4 reports **both** halves from real data and says whether they agree —
the statute picture and the court-stage picture — rather than asserting that
court-stage data is unavailable.

If the live numbers differ from gold's 33 / 1 / 30, report what you measured
and investigate the discrepancy. Do not tune the aggregate to reproduce
gold's figures.

## 4. Verification — both halves required

**Unit:** `tests/test_xagg.py` full pass, plus:
- a test that the aggregate returns both halves and the agreement verdict
  from a fixture with a known statute/court-stage mix;
- a **regression test pinned to M4's literal Urdu gold text** reaching your
  new aggregate;
- **negative controls**: G3's literal text still reaches
  `_court_readiness_scan()`, and CR7's still reaches
  `_criminal_record_court_crosscheck()`.

Use the shared interpreter with `PYTHONPATH=.` (orchestration doc §2.1).

**Live:**
1. `docker compose up -d postgres`, wait for `(healthy)`.
2. Confirm the model-server tunnel is alive (orchestration doc §3.3).
3. M4's exact Urdu gold text through `/api/chat` as `admin@example.com` /
   `MuhafizAdmin2026!`, All Cases.
4. From the raw SSE stream, confirm `route=` reaches XAGG and your aggregate.
5. Capture both halves and the agreement verdict.

**Non-gold paraphrase** (required): a plain-English compound comparative that
must reach the same aggregate — e.g. *"Do the charges we file and the court
outcomes tell the same story about how serious our caseload is?"*

**Regression guard — G3 is mandatory here.** Re-run **G3** live and confirm
it still scores 1.0. M4 and G3 share the court-readiness keyword space; this
is the single most likely thing your change breaks. Also re-run **CR7**
(Module 14's own question). Report both.

## 5. Watch for

- **Re-confirm M4's routing first.** This brief inherits "PPC 61, Arms
  Ordinance 29, claims no court-stage data" from a report written before
  PR #8 merged. Capture the current live behaviour before trusting it, and
  correct the plan if it has changed.
- **Do not open a second criminal-record query path.** If Module 14's reader
  does not give you what you need, extend it in place and re-run its tests.
- Module 23 lands in this same file first. Rebase on it rather than resolving
  a merge conflict later — the two additions are independent functions plus
  separate dispatch entries, so a rebase should be mechanical.
- A full `pytest -q` run empties the `muhafiz_entity_descriptions` Chroma
  collection; re-run `refresh_entity_embeddings()` before live checks if you
  ran the whole suite.

## 6. Git discipline

- Branch off current `main` **after Module 23 merges**:
  `git checkout main && git pull && git checkout -b feature/xagg-statute-court-stage-join main`
- Author every commit as
  `rayyanfaisal475207 <rayyanfaisal475207@users.noreply.github.com>`.
- Keep the `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` trailer.
- End the PR description with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- `main` is protected — the **"Backend tests"** check must pass.
- Every number in your writeup traces to a captured live output.

## 7. Required: update the tracker when you finish

In the same PR, update `GOLD_QA_REMAINING_FIXES_PLAN.md`:

1. Module 24's **status row** → `✅ PR #<n> open` / `✅ Merged`, and drop the
   now-stale "Blocked on PR #8" note.
2. Module 24's **own section** — the aggregate you added, its dispatch
   placement and why, the real statute and court-stage numbers you measured,
   and whether they matched gold's 33 / 1 / 30.
3. State the **G3 and CR7 regression results** explicitly.
4. If M4's post-PR-#8 live behaviour differs from what this brief describes,
   correct the plan's framing rather than coding around it.
