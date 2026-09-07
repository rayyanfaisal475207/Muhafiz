# Muhafiz — Module 27: the final Gold-32 rerun

**Status:** Task brief. **Blocked on Modules 23, 24, 28, 29, 30 and PRs #12
and #16 all merging.** Runs last, alone, on a quiet machine.
**Branch to create:** `docs/gold32-final-rerun`
**Read first:** `WAVE2_ORCHESTRATION_PROMPT.md`, then
`GOLD_QA_REMAINING_FIXES_PLAN.md`. **Update the plan when you finish** (§6).

This is the Module 18 redo. It is docs-and-measurement only — **no source
changes belong in this PR.** If the rerun exposes a defect, it becomes its own
module.

---

## 0. The baseline you are measuring against

Module 18's run, recorded in `MODULE_18_FINAL_REPORT.md` and
`GOLD32_RESULTS_FOR_TEAMMATE.md`:

| Metric | Module 18 |
|---|---|
| FactualCorrectness, all 32 | **0.428** |
| FactualCorrectness, excluding KB | **0.550** |
| Passing | **14 / 32** |

Every module in this round was scoped against that run. The purpose of this
one is to state, per bucket, what actually moved.

## 1. Preconditions — check every one before running

**All of these must be true, and you should state in the PR that you checked
them.** A rerun on a half-merged or mojibaked build is worse than no rerun,
because its numbers will be quoted later.

1. **Every module merged.** Modules 23, 24, 28, 29, 30, plus PRs #12 and #16.
   Confirm with `gh pr list --state open` — nothing Gold-QA-related should
   remain open.
2. **A freshly restored, UTF-8-clean dump.** Take it with the **fixed**
   `SHARE/database/create_dump.ps1`. **Never pipe the dump through
   PowerShell** — see that script's own header comment. Piping silently
   mojibakes every Urdu value, which reads as a code bug: it is why G5 once
   reported "0 of 32 unlicensed" instead of 30, and it cost a full eval run
   to diagnose.
3. **Postgres healthy** — `docker compose up -d postgres`, wait for
   `(healthy)`, not merely `Up`. A dead Postgres presents as a hang.
   **Start only `postgres`** — the `vllm` service wants a CUDA device and this
   machine has Intel UHD 620 graphics.
4. **The model-server tunnel is alive.** `EMBEDDINGS_URL`, `LOCAL_LLM_URL`,
   `RERANKER_URL`, `QALB_URDU_URL` all sit behind a free-tier ngrok tunnel on
   the user's own machine and rotate on its restart. The backend's `/health`
   does **not** cover them. A tunnel that dies mid-run will look like a
   scattering of unrelated question failures.
5. **Chroma is intact.** If a full `pytest -q` has been run on this machine
   since the last refresh, `muhafiz_entity_descriptions` is at count 0 — re-run
   `refresh_entity_embeddings()` first, or Local Search will find nothing.
6. **The machine is quiet.** ~5 GB free RAM. No other track's backend running,
   no other worktree doing live verification.

## 2. Run it

```bash
EVAL_ADMIN_EMAIL=admin@example.com EVAL_ADMIN_PASSWORD=MuhafizAdmin2026! python evaluation/gold32_run.py
```

Then score:

```bash
python evaluation/gold32_score.py
```

Keep `backend.log` for the whole run — the evaluator's `relevant=…` reasons
are the only place the KB bucket's behaviour is visible.

## 3. What to report

Per bucket, movement against Module 18's baseline:

| Bucket | Module 18 | Now | Δ |
|---|---|---|---|
| KB (8 questions) | 1/8 at Module 18; 5/8 after Module 19b | | |
| M (aggregate) | | | |
| CR (cross-case) | | | |
| CS / G (general) | | | |
| **All 32** | 0.428 | | |
| **Excluding KB** | 0.550 | | |

Then, **module by module**, state whether its **non-gold paraphrase** confirmed
a real capability gain — not merely that its gold question passes. That
distinction is the whole point of this project's verification standard, and
this is the run where it gets audited end to end.

Call out explicitly:

- Any question that **regressed** from Module 18. Investigate each one and
  name the module that most plausibly caused it.
- Any question that passes its gold text but **fails its paraphrase** — that
  is curve-fitting and should be recorded as such, not counted as a win.
- Any question whose judge reason suggests the **gold answer itself** is
  wrong or ambiguous.

## 4. Afterwards

- **Re-share the SHARE package.** `SHARE_muhafiz_20260907.zip` was rebuilt
  2026-09-07 with the corrected dump and the fixed create/restore scripts —
  both previously corrupted Urdu text, the create side on export and the
  restore side on import. Anyone holding an older copy should be re-sent it.
- **Then re-assess, from this run's own data.** Do **not** plan Modules 31+
  from the older reports. Read the fresh `gold32_results.json` judge reasons
  first. Every module in this plan that turned out to be mis-scoped was
  mis-scoped because it inherited a claim from an earlier report instead of
  re-deriving it — that is the single most expensive recurring mistake in this
  project's history, and this is the moment it is most tempting to repeat.

## 5. Git discipline

- Branch off current `main`:
  `git checkout main && git pull && git checkout -b docs/gold32-final-rerun main`
- Commit the results artefacts (`gold32_results.json`,
  `gold32_pipeline_outputs.json`) alongside the written report, so the numbers
  are reproducible rather than quoted.
- Author every commit as
  `rayyanfaisal475207 <rayyanfaisal475207@users.noreply.github.com>`.
- Keep the `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` trailer.
- End the PR description with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- `main` is protected — the **"Backend tests"** check must pass.

## 6. Required: close out the plan

In the same PR, update `GOLD_QA_REMAINING_FIXES_PLAN.md`:

1. Module 27's **status row** → `✅ Merged`, and mark the plan itself complete.
2. Add the **final per-bucket table** and the paraphrase audit to Module 27's
   section.
3. **Correct any module section whose claimed result this rerun contradicts.**
   A module that reported a win its paraphrase does not support should be
   recorded honestly, not left standing.
4. List what remains as candidate Modules 31+, **derived from this run's judge
   reasons only** — with a note saying so.
