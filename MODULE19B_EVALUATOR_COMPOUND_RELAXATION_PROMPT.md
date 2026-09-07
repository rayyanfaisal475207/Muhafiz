# Muhafiz — Module 19b: the evaluator rejects on-topic legal documents for compound KB questions

**Status:** Task brief, ready to implement.
**Branch to create:** `fix/evaluator-compound-relaxation-not-firing`
**Authoritative tracker:** `GOLD_QA_REMAINING_FIXES_PLAN.md` — read its
"Which modules can run in parallel" section first, then **update that file's
status table and this module's section when you finish** (see §6 below;
this is a required part of the module, not optional bookkeeping).

**Parallel-safe:** yes. This module touches `prompts/evaluator.txt` (and
possibly `src/pipeline/evaluator.py`) and nothing else. Modules 21, 25 and
26 are running concurrently in other chats against `xnetwork.py`,
`meta_analysis.py`/`verifier.py`, and `router.py` respectively — you will
not collide with them. Do **not** touch `src/pipeline/harness/tools/rag.py`
(PR #9's territory) or any `xagg.py` aggregate.

**This is the highest-value module in the remaining plan.** The Knowledge
Base Reasoning bucket is 8 of 32 gold questions and currently scores
**0.06 (1/8 pass)** — the single largest available gain.

---

## 0. The situation

Two separate defects were blocking the KB bucket. **The first is already
fixed** (PR #9, Module 19a): `_LEGAL_KB_INTENT_PATTERNS` in `rag.py` had
been mined from KB1's phrasing alone and matched **0 of the other 7** KB
questions, so those questions searched the mixed case-document pool instead
of the legal corpus. That is now 8/8 matching, with 0/24 non-KB
over-matching, and the backend log confirms KB-only scoping fires.

**This module is the second defect, and it is what still keeps the bucket at
0.06.** With scoping fixed, retrieval now surfaces the *correct* legal
corpus — and the evaluator rejects it anyway.

**Captured live, 2026-09-07, with PR #9's branch active** (backend log,
`Evaluator: relevant=… — …` lines):

| Q | Evaluator's own stated reason | Verdict |
|---|---|---|
| **KB2** | *"The retrieved documents discuss procedures for recording witness/accused statements"* | `relevant=False` |
| **KB2** | *"The retrieved documents discuss legal procedures for recording statements in cou[rt]…"* | `relevant=False` |
| **KB3** | *"The retrieved documents discuss legal procedures for FIR registration and investigation"* | `relevant=False` |
| **KB3** | *"The retrieved documents do not explicitly address whether the same officer must …"* | `relevant=False` |

Read those reasons carefully: for KB2 the evaluator is describing
*precisely what KB2 asks about* — how witness/accused statements are
recorded — and then rejecting it. Each question retries 3× and ends
`status=abstained`, `"No sufficiently relevant documents were found for
this question after retrying with query refinements."`

This is Module 5's compound-question relaxation failing to fire.

## 1. What's already in place (do not re-add it)

`prompts/evaluator.txt` **already contains** a substantial
compound-question section (added by Module 5), including:

> *"For these, return TRUE if the retrieved documents sufficiently answer AT
> LEAST the primary/answerable part of the question (typically the
> law/procedure/reference part). Do NOT return false merely because the
> documents don't also cover the SECONDARY part…"*

…plus a worked bad-`false`-reason example using KB1's exact shape. So the
rule exists and is well-written. **Your job is to find out why the model
isn't applying it to KB2–KB9 and fix that** — not to write the rule again.

## 2. Two hypotheses to test first

Do not jump straight to editing prose. Test these, in order, and record
which one (or both) is real:

### Hypothesis A — primary/secondary inversion

The prompt's rule assumes the **legal** half is "primary" and the data half
"secondary". KB1 fits that (*"What legal requirement governs… and does our
recordkeeping follow it?"*). But several of these invert it:

- **KB2**: *"**Why doesn't our system keep a record** of what a witness or
  an accused person actually said in a police interview — is that a data
  gap?"* — leads with the data half. A model asked to check "the
  primary/answerable part" can reasonably conclude the *primary* part is the
  system question, which the legal documents genuinely don't answer.

**Fix direction:** make the rule *order-independent* — if the documents
answer the legal/procedural half of a compound question, that is sufficient
**regardless of which half the sentence leads with**, and regardless of
which half the model considers "primary".

### Hypothesis B — the prompt's own closing line overrides the rule

The file's **last paragraph** says:

> *"Evaluate strictly for a SINGLE-topic question: if documents are on topic
> but missing the specific data point asked, return false."*

Every one of these KB questions is *exactly* "on topic but missing the
specific data point". For any question the model doesn't confidently
classify as compound, this closing instruction — last, and phrased as a
strict default — is the one that wins.

**Fix direction:** make compound-detection the default whenever a question
contains **both** a norm/legal clause and an our-data clause, and soften the
single-topic strictness so it cannot override the compound rule. Note that
`rag.py` already has a working two-signal detector for exactly this shape
(`_NORM_SIGNAL_RE` + `_OUR_DATA_SIGNAL_RE`, added in PR #9) — read it for
the shape definition; whether to reuse it programmatically or mirror it in
prose is your call.

## 3. Objective

All 8 KB gold questions return `relevant=True` and produce a substantive,
cited answer grounded in the legal corpus, instead of abstaining — **while
genuinely-insufficient retrievals still return `false`.**

The answer does not have to fully cover the "does our data comply" half.
Per the prompt's own rule, generation should answer the legal part it has
evidence for and honestly note what it cannot determine — that is a pass,
not a failure. KB1 already demonstrates the target shape (scored 0.5,
correctly citing CrPC §154, docked only for the compound question's second
half).

## 4. Verification — both halves required

**Unit:** the evaluator's existing tests must pass. Add cases pinned to
KB2/KB3/KB4's literal gold text from `Gold_QA_Dataset_Final32.json`. Mock
the LLM — assert the prompt/parse contract, not model behavior.

**Live is the real test here** — this is primarily a prompt fix and unit
tests cannot prove it works:

1. Start Postgres (`docker compose up -d postgres`, wait for `healthy` —
   a dead Postgres reads as a *hang*, not an error), the model server, and
   the backend with output redirected to a log file.
2. Send all 8 KB questions through real `/api/chat` as
   `admin@example.com` / `MuhafizAdmin2026!`, All Cases.
3. For each, capture the backend log's `Evaluator: relevant=… — …` line.
   **Report the before/after reason text for KB2 and KB3 specifically** —
   those are the two documented above.
4. Expect: `relevant=True`, a cited answer naming real statutory
   provisions, `status` not `abstained`.

**Non-gold paraphrase** (required — proves capability, not curve-fitting):
e.g. *"Is there a legal standard for how long we keep case property, and do
our records follow it?"*

**Regression guard (do not skip):** confirm a genuinely-insufficient
retrieval still returns `false`. The prompt's own "Examples of correct
'false' decisions" list is your test set — e.g. *"Retrieved chunks are about
the foreigner registration procedure, not tenant registration as the user
asked"* must still be a `false`. A fix that makes the evaluator return
`true` for everything is a regression, not a fix.

## 5. Git discipline

- Branch off current `main`: `git checkout main && git pull && git checkout
  -b fix/evaluator-compound-relaxation-not-firing main`
- **Author every commit as `rayyanfaisal475207
  <rayyanfaisal475207@users.noreply.github.com>`.**
- Keep the `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` trailer —
  platform-level attribution requirement, not switchable off per-project.
- End PR descriptions with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- Push the branch and open a PR against `main` (main is protected; the
  "Backend tests" check must pass).
- **Never put an unverified claim in the writeup.** Every number traces to a
  captured live output. Both Module 18 reports described the M4 keyword fix
  as applied and test-verified when it had never actually been committed —
  don't add to that.

## 6. Required: update the plan file when you finish

`GOLD_QA_REMAINING_FIXES_PLAN.md` is the authoritative tracker. Before
opening your PR, update it **in the same PR**:

1. **Status snapshot table** — change Module 19b's row from
   `⬜ Not started — **highest value**` to `✅ PR #<n> open` (or `✅ Merged`
   once merged).
2. **Module 19b's own section** — replace the two hypotheses with what you
   actually found: which hypothesis was real (A, B, both, or neither and
   something else), the fix applied, and the before/after evaluator reason
   text for KB2 and KB3.
3. **If the KB bucket is now passing**, say so with the measured numbers and
   note that Module 27 (final rerun) is one step closer to unblocked.
4. **If you discover a third KB blocker** — entirely possible; that's how
   this module itself was found — add it as a new module section with the
   same structure (root cause, evidence, work, verification) and add a row
   to the status table, rather than silently expanding this module's scope.
5. Keep the "Which modules can run in parallel" section accurate if your
   work turned out to touch a file it doesn't currently list.

