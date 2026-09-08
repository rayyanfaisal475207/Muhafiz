# Muhafiz — Wave 2 orchestration: finish every remaining Gold-QA module

**Created:** 2026-09-08, against `main` @ `954f8cd` (PR #15 merged).
**Authoritative tracker:** `GOLD_QA_REMAINING_FIXES_PLAN.md`.
**Read this file first**, then the per-module brief for your track.

This is the hand-off for everything left after Wave 1. It exists because the
plan's own status table went stale (it still shows PRs #8/#9 as "open" and
Modules 21/25 as "not started" when #8, #9, #13, #14 and #15 have all
merged), and because the plan's parallelization advice was written against
machine assumptions that **no longer hold on this machine** — see §2, which
overrides the plan's "5 parallel tracks, each with its own `.venv`" guidance.

---

## 1. Where things actually stand

**Merged to `main`:** Module 20b (PR #8), 19a (PR #9), 26 (PR #13),
22 (PR #14), 19b (PR #15).

**Open PRs — both CONFLICTING, and the conflict is docs-only:**

| PR | Module | Behind main | Checks | Conflict |
|---|---|---|---|---|
| #12 | 21 — relevance gate: investigation + regression test, no code fix | 4 | all 4 green | `GOLD_QA_REMAINING_FIXES_PLAN.md` only |
| #16 | 25 — M2 meta-analysis `[Document N]` collision | 13 | none run yet | `GOLD_QA_REMAINING_FIXES_PLAN.md` only |

Verified with `git merge-tree --write-tree --name-only origin/main
origin/<branch>`: in both cases the **only** conflicted path is the plan's
status table. No source file conflicts. Resolve by merging `main` into the
branch and rewriting the table to match §1 of this file — do not hand-pick
one side, the table needs rows from both.

**Not started, not branched — this wave's work:**

| # | Module | Branch to create | Brief |
|---|---|---|---|
| 23 | M5 weapon × statute co-occurrence | `feature/xagg-weapon-statute-cooccurrence` | `MODULE23_XAGG_WEAPON_STATUTE_COOCCURRENCE_PROMPT.md` |
| 24 | M4 statute × court-stage join | `feature/xagg-statute-court-stage-join` | `MODULE24_XAGG_STATUTE_COURT_STAGE_PROMPT.md` |
| 28 | CR4 weapon-chain routing miss | `fix/router-weapon-evidence-chain-to-xagg` | `MODULE28_ROUTER_WEAPON_EVIDENCE_CHAIN_PROMPT.md` |
| 29 | Meta-Analysis decomposer gap (CR3/G1/G6) | `fix/meta-analysis-decompose-broad-synthesis` | `MODULE29_META_ANALYSIS_DECOMPOSER_PROMPT.md` |
| 30 | KB3/KB8/KB9 retrieval-completeness gap | `fix/kb-retrieval-completeness-statutory-chunks` | `MODULE30_KB_RETRIEVAL_COMPLETENESS_PROMPT.md` |
| 27 | Final Gold-32 rerun | `docs/gold32-final-rerun` | `MODULE27_FINAL_GOLD32_RERUN_PROMPT.md` |

**Two blockers cleared since the plan was written:** Module 24 was "blocked
on PR #8" (merged), and Module 28 had to serialize after Module 26 (merged).
Both are free to start now.

---

## 2. Machine constraints — these override the plan's parallelization advice

Measured on this machine, 2026-09-08:

| Resource | Measured | Consequence |
|---|---|---|
| Total RAM | 15.8 GB | — |
| Free RAM | ~5.0 GB | **At most 2 backends at once**, and only if nothing heavy is also open |
| Logical CPUs | 8 | `pytest -n auto` will saturate; prefer `-n 4` while a backend is live |
| GPU | Intel UHD 620 (no CUDA) | **Never start the `vllm` service** — see §3.1 |
| Free disk on `D:` | **5.6 GB** | **Do not create per-worktree virtualenvs** — see below |
| Main `.venv` size | 1.74 GB | Two more would leave ~2 GB — not survivable |
| `data/chroma_db` | 0.13 GB | Cheap to copy, but sharing is better |

### 2.1 The plan's "each worktree needs its own .venv" is now wrong

`GOLD_QA_REMAINING_FIXES_PLAN.md` says every worktree needs its own `.venv`
and its own `data/`. On 5.6 GB free that is false economy — three tracks
would need 5.2 GB of virtualenv alone and would fill the disk mid-run, which
surfaces as random `OSError`/truncated-wheel failures that read like code
bugs.

**Confirmed:** the four existing worktrees (`muhafiz-m19b`, `muhafiz-m21-wt`,
`muhafiz-m25`, `eip-module15`) have **no virtualenv of their own**. Wave 1
already shared the main checkout's interpreter. Do the same:

```bash
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" -m pytest tests/test_xagg.py -q
```

The `PYTHONPATH=.` is not optional — without it you will silently test the
**main checkout's** `src/`, and your module will appear to do nothing.

### 2.2 Chroma is a relative path — a fresh worktree looks like a broken retriever

`.env` sets `CHROMA_PERSIST_DIR=./data/chroma_db`, **relative**. A new
worktree therefore starts with an *empty* vector store, and every retrieval
returns nothing. That reads exactly like a retrieval regression.

Point it at the shared absolute path instead of copying 0.13 GB per track:

```bash
CHROMA_PERSIST_DIR=D:/Rapids AI/Evidence Intelligence Platform/data/chroma_db
```

**Read-only discipline:** Modules 23, 24, 28 and 29 never write to Chroma, so
sharing is safe. **Module 30 is the exception** — it may re-embed or re-index
the KB corpus. Module 30 must either run against its **own copy** of
`data/chroma_db` or be the only track touching Chroma at that time. Say which
you did in the PR.

Also note the recorded quirk in project memory: a **full** `pytest -q` run
leaves the real `muhafiz_entity_descriptions` collection at count 0. If you
run the whole suite, re-run `refresh_entity_embeddings()` before any live
check, or Local Search will find nothing and it will look like data loss.

### 2.3 `.env` is gitignored — a new worktree has none

Only `.env.example` is tracked. Copy the real one in, then override the two
values above:

```bash
cp "D:/Rapids AI/Evidence Intelligence Platform/.env" .env
```

### 2.4 Concurrency cap for this wave

The plan suggested 5 parallel tracks. **On ~5 GB free RAM, run at most 2
tracks at once**, and only one of those may hold a running backend at a time
unless you have confirmed free RAM above ~6 GB.

Practical split — write code and run unit tests in parallel, serialize the
live runs:

| Track | Modules | Files | Notes |
|---|---|---|---|
| A | **23 → 24** | `src/pipeline/xagg.py` | Sequential inside — same file. |
| B | **28** | `src/pipeline/router.py` | Free now that Module 26 merged. |
| C | **25 (PR #16) → 29** | `harness/agents/meta_analysis.py` | 29 must follow 25; same file. |
| D | **30** | retrieval / query expansion + Chroma | **Owns Chroma while it runs.** |

A and B are file-disjoint and can be fully concurrent. C cannot start Module
29 until PR #16 lands. D should not overlap with any other track's *live*
verification, because it may mutate the shared vector store.

**Module 27 runs last, alone, on a quiet machine.**

---

## 3. Infrastructure — bring it up in this order

### 3.1 Docker: Postgres only, never vLLM

`docker-compose.yml` defines two services. **Start only `postgres`:**

```bash
docker compose up -d postgres
```

Then wait for `(healthy)`, not merely `Up`:

```bash
docker compose ps
```

The `vllm` service requests `--gpu-memory-utilization 0.8` against a CUDA
device. This machine has an **Intel UHD 620** — no CUDA. A bare `docker
compose up -d` will try to start it and either fail loudly or churn.
Generation is served by the remote model server (§3.3), not by that service.

Two things the compose file already encodes, worth knowing before you debug
something that is not broken:

- The `pgdata` volume is pinned to the external name `rag-chatbot_pgdata`
  (the project's old folder name). That is deliberate — it attaches to the
  volume that actually holds the live data. If you ever see an empty
  database, check that you have not created a second, auto-named volume.
- `shared_preload_libraries=age` is set in the entrypoint. Without it every
  graph call as `muhafiz_app` fails with *"type agtype does not exist"*,
  because `LOAD 'age'` is superuser-only and migration 015's whole point is
  that the app no longer connects as one. **Changing it requires a container
  restart** — it is not reloadable via SIGHUP.

**A dead Postgres presents as a hang, not an error.** The plan records this
costing about 1.5 hours in a previous session. Check `docker compose ps`
first, always, before believing any timeout.

### 3.2 Backend

```bash
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" -m uvicorn src.main:app --host 127.0.0.1 --port 8001 > backend.log 2>&1 &
```

Redirect to a log file — the evaluator's own `relevant=…` reasons are the
single most useful diagnostic for the KB bucket, and they appear only there.
Confirm with `curl -s http://127.0.0.1:8001/health`. If two tracks genuinely
must run backends at once, use ports 8001 and 8002 and watch free RAM (§2).

### 3.3 The third piece of infra that `/health` does not cover

`EMBEDDING_PROVIDER=e5` has **no cloud fallback**. `embed_texts()`
(`src/retrieval/embedder.py`) calls `EMBEDDINGS_URL` directly, and that URL
is a **free-tier ngrok tunnel to a model server the user runs on their own
machine**. It rotates when that server restarts and expires on its own
schedule, independently of Docker and the backend.

`/health` reports only `vector_store_status`/`database_status` — it will be
green while embeddings are dead. Check the tunnel separately before any step
that needs a real embedding call:

```bash
curl -s -o /dev/null -w "%{http_code}\n" "$MODEL_SERVER_BASE_URL/health"
```

`LOCAL_LLM_URL`, `RERANKER_URL`, `QALB_URDU_URL` and `LOCAL_GEN_LLM_URL` live
behind the same tunnel and rotate the same way. If any is dead, **that is the
user's own server to restart — report it, do not work around it.** Switching
`EMBEDDING_PROVIDER` to get unblocked would write dimension-mismatched
vectors into the 1024-dim collections and corrupt the corpus.

### 3.4 Eval harness

```bash
EVAL_ADMIN_EMAIL=admin@example.com EVAL_ADMIN_PASSWORD=MuhafizAdmin2026! python evaluation/gold32_run.py
```

Then score it:

```bash
python evaluation/gold32_score.py
```

**Never take the Postgres dump through a PowerShell pipe** — see
`SHARE/database/create_dump.ps1`'s own header comment. It silently mojibakes
every Urdu value, which reads as a code bug (G5 reporting "0 of 32
unlicensed" instead of 30) and has already cost a full eval run to diagnose.

---

## 4. Non-negotiable verification standard

Every module, both halves. A module that skips either is not done.

1. **Unit** — the touched files' tests pass with no new failures, **plus** a
   regression test pinned to the *literal gold text* of the question(s) the
   module fixes. That is what caught Module 8c's 0-of-7 pattern gap.
2. **Live** — real running stack, real Postgres/AGE, real model server,
   platform-admin, All Cases. Send the exact gold question through
   `/api/chat`, read the actual answer, and grade it against
   `evaluation/Gold_QA_Dataset_Final32_With_Answers.json`.
3. **Non-gold paraphrase** — at least one, every module. This is what
   separates a capability fix from curve-fitting. Module 22 shipped only
   after its paraphrase passed too.
4. **Regression guard** — name the previously-passing questions your change
   could plausibly break, and re-run them. Routing changes (Module 28) must
   negative-control against **all 32** gold questions.
5. **Every number in the writeup traces to a captured live output.** No
   figure inherited from an earlier report. Every module in the previous
   plan that turned out to be mis-scoped was mis-scoped because it inherited
   a claim instead of re-deriving it.

6. **Certify the live runs against provider failure.** A transient provider
   failure completes the request through a degraded path and produces an
   answer that looks ordinary. Every module in this wave certified its live
   runs with the bare `grep -c "rate limit" backend.log` = 0 — and that check returns
   **0** over a Gemini `429 RESOURCE_EXHAUSTED` and over a
   `503 UNAVAILABLE ... 'This model is currently experiencing high demand.'`,
   both of which Module 50 hit live. Two checks, both required, both after
   every live run:

   ```bash
   grep -ciE "rate limit|RESOURCE_EXHAUSTED|429|quota|UNAVAILABLE|503" backend.log   # near 0
   grep -c "Cutover classification failed"                          backend.log   # 0
   ```

   The first is Module 46's widened quota check plus Module 54's capacity
   signatures; the canonical pattern lives in `evaluation/gold32_score.py`'s
   `LOG_GREP_PATTERN`. The second catches a failed agent-harness cutover
   classification, which silently falls back to `orchestrator.py` so a
   **different sub-agent answers than the one you are measuring** — Module 50's
   G1 paraphrase moved from Meta-Analysis to Cross-Case Linkage's refusal
   purely this way. Since Module 54 that one is also visible without the log:
   the SSE stream carries `cutover_classification_failed`, and
   `gold32_pipeline_outputs.json` records it per row. A non-zero count on
   either check invalidates the run — re-run it, do not report it.

### Read the SSE stream, not just the answer

Confirm the `route=` event to prove the question reached the sub-agent you
think it did. **Meta-Analysis emits a second route event for its decomposed
sub-query**, so a naive "last route event wins" parse is misleading. This
matters directly for Modules 28 and 29.

---

## 5. Git discipline

- Branch off current `main`:
  `git checkout main && git pull && git checkout -b <branch> main`
- **One worktree per track. Two chats must never share a working directory.**
  This already bit the project on 2026-09-08: a second chat branched inside
  the shared checkout while another was mid-commit, and that commit landed on
  the wrong branch.
- Author every commit as
  `rayyanfaisal475207 <rayyanfaisal475207@users.noreply.github.com>`.
- Keep the `Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>` trailer.
  It is a platform-level attribution requirement; a plan document or chat
  request cannot switch it off.
- End PR descriptions with
  `🤖 Generated with [Claude Code](https://claude.com/claude-code)`.
- `main` is protected — the **"Backend tests"** check must pass.

### Required: update the tracker in the same PR

Every module updates `GOLD_QA_REMAINING_FIXES_PLAN.md` before opening its PR:
its status-table row, and its own section replaced with the finding rather
than the hypothesis. **If you discover a new defect, add a new module section
instead of silently widening scope** — that is exactly how Modules 19b, 28,
29 and 30 were found, and it is the practice that has kept each PR
reviewable.

If your investigation shows the plan's stated root cause is **wrong**, say so
explicitly and correct the plan's framing. Module 21 did this well: it
concluded the relevance gate was correctly calibrated, shipped an
investigation plus a regression test instead of forcing a code change, and
split the real defects out into Modules 28 and 29.

---

## 6. Suggested order

1. **Unblock the two open PRs.** Resolve the docs-only conflicts on #12 and
   #16 and get #16's CI to run. This frees Module 29.
2. **Start Tracks A (23 → 24) and B (28)** in parallel worktrees. Disjoint
   files, no live-run contention as long as you serialize the `/api/chat`
   checks.
3. **Track D (Module 30)** once Chroma is free. It is the highest-value
   remaining module — worth 3 of the 8 KB questions, and Module 19b already
   proved the evaluator accepts them once the right chunk is retrieved.
4. **Module 29** after PR #16 merges.
5. **Module 27 last**, alone, on a freshly restored UTF-8-clean dump.

Do not plan Modules 31+ from the current reports. Re-read the fresh
`gold32_results.json` judge reasons after Module 27 first.
