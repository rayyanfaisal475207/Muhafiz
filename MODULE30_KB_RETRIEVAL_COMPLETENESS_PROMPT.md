# Muhafiz — Module 30: KB3/KB8/KB9 — the right statutory chunk never reaches the pool

**Status:** Task brief, ready to implement. **Highest-value module remaining**
— it is worth 3 of the 8 KB questions.
**Branch to create:** `fix/kb-retrieval-completeness-statutory-chunks`
**Split out of:** Module 19b (PR #15), deliberately, per that module's brief —
its scope was `prompts/evaluator.txt` only.
**Read first:** `WAVE2_ORCHESTRATION_PROMPT.md`, then
`GOLD_QA_REMAINING_FIXES_PLAN.md`. **Update both when you finish** (§7).

**Parallel-safe:** with one hard condition. **This module owns Chroma while
it runs.** It may re-embed or re-index the KB corpus, and every other track
shares `data/chroma_db`. Either run against your own copy (0.13 GB) or be the
only track doing live verification at that time. Say which in the PR.

---

## 0. What Module 19b established

Module 19b fixed the evaluator's compound-question relaxation and took the KB
bucket from **1/8 to 5/8**. The remaining three — KB3, KB8, KB9 — were
diagnosed there and explicitly left out of scope.

**The finding, already confirmed:** for these three, the specific statutory
provision the gold answer cites **never enters the retrieval candidate pool**.
Not in the top-5 after cross-rerank, and not in the wider ~15–30 item
semantic + BM25 pool before that cut — for the original question, **or** either
of the 2 expanded queries, **or** the cross-script variant, **or** the
evaluator-feedback retry rewrite.

**The chunk exists in the corpus and is findable.** Module 19b confirmed this
by querying the live vector store directly with better-targeted text (e.g.
*"Article 18 Police Order investigation staff head of investigation"* for
KB3). It simply is not reached by any query text the pipeline generates from
these questions' phrasing.

**This is a retrieval defect, not an evaluator defect.** Module 19b proved it
by feeding the **actual** retrieved chunks — not idealized ones — through both
the pre-fix and post-fix evaluator prompts. The post-fix prompt correctly
returns false when a compound question's norm clause is not addressed by
what is on the page. **No prompt-only change should make these return true**
without risking false-positive relevance elsewhere. Do not attempt one.

## 1. Exactly which provision each question needs

Taken from the gold answers — more specific than the plan's own description,
so use these:

- **KB3** — **Police Order 2002, Article 18**: the distinct Investigation
  Wing under a dedicated head of investigation, the "shall be investigated by
  the investigation staff" language, and the bar on the District Police
  Officer interfering.
  *Retrieved instead:* CrPC sections on statements/bonds/imprisonment, and
  Police Order administrative forms.

- **KB8** — **CrPC 1898, section 173**: investigation to be completed without
  unnecessary delay, and where it is not complete within **14 days** of FIR
  registration, the officer in charge must send the magistrate an **interim
  report** through the public prosecutor within 3 days of that period ending.
  *Retrieved instead:* CrPC ss.170–171 ("case sent to magistrate when
  evidence is sufficient") — related but distinct.

- **KB9** — **CrPC 1898, section 174**: the inquest / cause-of-death
  investigation duty when a person dies by suicide, homicide, or in
  suspicious circumstances.
  *Retrieved:* inconsistently. It appeared in some attempts, not reliably.
  **Note the trap Module 19b recorded:** in one early probe the evaluator
  cited "section 174" when the text was **not actually in the chunks it was
  judging** — that citation was a model hallucination. Verify chunk contents
  directly; do not trust a citation in an answer as evidence of retrieval.

## 2. Re-probe before choosing a fix

The plan lists candidate directions but explicitly says none has been
investigated. **Re-probe the corpus first**, as Module 19b did, and confirm
which approach actually surfaces the missing chunk before committing:

1. **Query expansion** — widen the expander's prompting so a legal-KB-intent
   question tries **naming candidate governing statutes** ("Police Order
   2002", "CrPC section 173"). This is the most targeted option: the gap is
   that the generated query text never contains the statute's own vocabulary.
   Also consider the cross-script variant path, since KB8 and KB9 are asked in
   roman-Urdu.
2. **A keyword/BM25 boost path** for statute-name-shaped tokens.
   `src/retrieval/bm25_retriever.py` and `fulltext_index.py` already exist —
   check what they do with "Article 18" / "section 173" before adding
   anything.
3. **Raising `TOP_K_RETRIEVAL` (default 10) or
   `CROSS_CASE_RETRIEVAL_MULTIPLIER` (default 3) for KB-scope only** — cheap
   but blunt. `src/config.py`'s own comments explain what each knob widens.
   Treat this as a fallback, and if you use it, scope it to KB intent rather
   than raising it globally.

Record what you probed and what each attempt returned. The probe evidence is
as valuable as the fix — it is what stops the next person re-deriving it.

## 3. Objective

KB3, KB8 and KB9 retrieve the correct statutory chunk, and the evaluator —
**unchanged from Module 19b's fix** — then returns true on its own.

Taking the KB bucket from 5/8 to 8/8 is the target. If one of the three turns
out to need something structurally different (a missing document in the
corpus, say, rather than a reachability problem), report that as its own
finding rather than forcing all three through one mechanism.

## 4. Verification — both halves required

**Unit:** the touched retrieval modules' tests pass with no new failures, plus
a test pinned to whichever mechanism you changed — e.g. that a legal-KB-intent
question's expanded queries now contain statute-name vocabulary.

Use the shared interpreter with `PYTHONPATH=.` (orchestration doc §2.1).

**Live:**
1. `docker compose up -d postgres`, wait for `(healthy)`.
2. **Confirm `EMBEDDINGS_URL` is alive before anything else.** This module
   makes real embedding calls, and `EMBEDDING_PROVIDER=e5` has **no cloud
   fallback** — `embed_texts()` hard-fails on a dead tunnel. The backend's
   `/health` will be green anyway; it only reports vector-store and database
   status. If the tunnel is down, that is the user's own model server to
   restart — report it, do not work around it. **Never switch
   `EMBEDDING_PROVIDER` to get unblocked**: it would write dimension-mismatched
   vectors into the 1024-dim collections and corrupt the corpus.
3. Run KB3, KB8 and KB9's exact gold text through `/api/chat` as
   `admin@example.com` / `MuhafizAdmin2026!`.
4. **Read `backend.log`, not just the answer.** The evaluator's own
   `relevant=…` reasons are the single most useful diagnostic for this bucket.
5. For each, capture the **actual retrieved chunk texts** and confirm the
   right provision is present. Do not infer retrieval from a citation in the
   answer — see the KB9 hallucination trap in §1.

**Non-gold paraphrase** (required): at least one per question if you can, one
overall at minimum — a differently-phrased legal question whose answer depends
on a statute the pipeline would previously not have reached.

**Regression guard — mandatory and specific:** re-run **KB1, KB2, KB4, KB5 and
KB6**, the five Module 19b just fixed. A retrieval change is exactly the kind
of change that regresses them. Report all five results in the PR.

## 5. Watch for

- **Do not change `prompts/evaluator.txt`.** Module 19b's fix is correct and
  merged. If you believe the evaluator still needs work, that is a new module.
- **Chroma is shared and this module may write to it.** Orchestration doc
  §2.2. Also: a full `pytest -q` run leaves `muhafiz_entity_descriptions` at
  count 0 — if you run the whole suite, re-run `refresh_entity_embeddings()`
  before believing any live retrieval result.
- Raising retrieval width globally will slow every question and can push
  genuinely relevant chunks out of the reranker's window. Scope it.
- KB8 and KB9 are roman-Urdu. Whatever you change must work on that script,
  not only on English phrasing — that is partly why the cross-script variant
  path is worth probing.

## 6. Git discipline

- Branch off current `main`:
  `git checkout main && git pull && git checkout -b fix/kb-retrieval-completeness-statutory-chunks main`
- Author every commit as
  `rayyanfaisal475207 <rayyanfaisal475207@users.noreply.github.com>`.
- Keep the `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` trailer.
- End the PR description with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- `main` is protected — the **"Backend tests"** check must pass.
- Every number in your writeup traces to a captured live output.

## 7. Required: update the tracker when you finish

In the same PR, update `GOLD_QA_REMAINING_FIXES_PLAN.md`:

1. Module 30's **status row** → `✅ PR #<n> open` / `✅ Merged`.
2. Module 30's **own section** — replace the "likely fix directions" list with
   what you actually probed, what each attempt returned, and which mechanism
   worked. Name the KB bucket's new score.
3. State explicitly that **the evaluator was not changed**, and paste the
   **KB1/2/4/5/6 regression results**.
4. Say whether you ran against a **copy** of Chroma or owned the shared one.
5. If any of the three needs a corpus change rather than a retrieval change,
   add that as its own module section rather than widening this one.
