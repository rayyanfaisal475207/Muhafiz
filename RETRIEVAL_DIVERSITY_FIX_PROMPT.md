# Muhafiz — Cross-Lingual / Cross-Case Retrieval Diversity Fix

You are fixing a retrieval bug in the Muhafiz Evidence Intelligence Platform
(FastAPI + Postgres/Apache AGE + ChromaDB backend, two React frontends).

## The bug

Asking the same question in Urdu vs. English (e.g. "list of people mentioned in
cases") returns answers grounded in **completely different, non-overlapping cases**
(one language surfaces `FIR-2026-DOM-014`, the other `FIR-2026-CYBER-001`), with no
indication to the user that other cases exist and were excluded. This is not a
translation bug — the system correctly answers in the query's language — it's a
retrieval coverage bug.

## Root cause (already diagnosed — do not re-derive from scratch, verify against current code first)

Retrieval is hybrid: semantic search (ChromaDB + `multilingual-e5-large-instruct`)
fused with BM25 keyword search via Reciprocal Rank Fusion, then cross-encoder
reranked. Pipeline: `src/pipeline/orchestrator.py` (RAG route ~lines 1206-1424) →
`src/retrieval/vector_store.py`, `src/retrieval/bm25_retriever.py`,
`src/retrieval/reranker.py`, `src/retrieval/cross_reranker.py`,
`src/retrieval/embedder.py`.

Two compounding issues:

1. **BM25 never sees the whole corpus.** `orchestrator.py` (~line 1311-1313) runs
   BM25 only over `semantic_results` — the chunks vector search already returned —
   not the full document store. BM25 can only re-rank what vector search already
   surfaced; it can never rescue chunks vector search missed.
2. **Vector search has no cross-case diversity guarantee.** `TOP_K_RETRIEVAL = 10`
   (`src/config.py`) per expanded query, fused via RRF back down to 10, then
   cross-reranked to `TOP_K_RERANK = 5`. `_build_where`
   (`src/retrieval/vector_store.py`, ~lines 237-302) only filters by
   `project_id`/`is_global`/`case_id` — there is no logic to spread the top-k across
   multiple matching cases when the query isn't scoped to one case. It's pure
   nearest-neighbor search: whichever single case's chunks sit closest in embedding
   space for that exact phrasing wins the entire top-k window. Because even a
   multilingual embedding model doesn't rank Urdu-script and English phrasings of
   the same question identically (cross-script semantic drift), the two languages
   pull in nearest neighbors from two different cases — and issue 1 means BM25 can't
   broaden that set back out.

## Read before touching anything

- `src/pipeline/orchestrator.py` — the RAG route, both call sites for
  `semantic_results`/`bm25_results`/`where_clause`.
- `src/retrieval/vector_store.py` — `_build_where`, `query_similar`.
- `src/retrieval/bm25_retriever.py` — `retrieve_bm25`, what it's currently given as
  its candidate pool.
- `src/retrieval/reranker.py` — the RRF fusion, to understand what changes if BM25's
  input pool grows.
- `src/config.py` — `TOP_K_RETRIEVAL`, `TOP_K_RERANK`, and any case-scoping config.

Confirm line numbers against current code — this diagnosis was written 2026-07-29
from a live read, but don't trust stale line numbers blindly.

## What to implement

Two independent fixes. Implement and verify them **one at a time**, in this order,
and stop after each for review before starting the next:

### Fix 1 — BM25 over the full corpus

Change `orchestrator.py`'s RAG route so BM25 runs against the full candidate
document/chunk pool for the relevant scope (case/project/global, respecting
whatever access-control filtering already applies elsewhere in this pipeline — do
not bypass RLS/case-scoping to do this), not just `semantic_results`. Check whether
`bm25_retriever.py` already supports being handed a larger/different candidate set,
or needs a new code path to pull from the full indexed corpus (e.g. via
`vector_store` metadata or a separate BM25 index/store if one exists).

Watch for: performance (BM25 over the full corpus on every query vs. only the
current small pool), and whether "full corpus" needs the same
case/project/is_global scoping the vector query already applies via
`_build_where` — it must not leak cross-case results where access control
currently prevents that.

### Fix 2 — Cross-case diversity in vector retrieval

When a query is not scoped to a single specific case (i.e. more than one case could
legitimately match), ensure the top-k retrieval window isn't dominated by a single
case. Reasonable approaches, pick the one that fits the existing code shape best
(propose your choice before implementing if it's a nontrivial design call):

- Bucket candidates by `case_id`, take a capped number of top results per case
  (e.g. top-N per case) before the final rerank, rather than pure global top-k.
- Or: over-fetch a larger candidate pool (e.g. top-30 instead of top-10), then apply
  case-diversity capping before cross-reranking down to `TOP_K_RERANK`.

This should only change behavior when the query is genuinely cross-case (no
explicit `case_id` filter already scoping it to one case) — a query already scoped
to a single case via `_build_where` should behave exactly as it does today.

## Non-negotiable working rules

- **One fix at a time.** Implement Fix 1, report, wait for review. Then Fix 2.
- **Re-read the actual current code first** — the diagnosis above may have drifted
  from the code by the time you implement it.
- If either fix turns out to be based on a misreading of the current pipeline, stop
  and say so rather than implementing a fix believed to be wrong.
- Don't widen scope beyond these two fixes (no unrelated refactors, no touching the
  cross-encoder reranker's logic itself, no changing embedding models).
- Report outcomes honestly: show real test output; if something can't be verified in
  this environment (e.g. no live Postgres/Chroma), say so plainly.

## Verification

- Run the full backend test suite:
  `python -m pytest tests/ --continue-on-collection-errors`
- Add/extend tests that directly demonstrate the fix: e.g. a test proving BM25's
  candidate pool is no longer limited to `semantic_results`, and a test proving a
  cross-case query's top-k now includes chunks from more than one case when more
  than one case has relevant content.
- If live Postgres/ChromaDB access is available in this session, use it: run an
  actual Urdu-language and English-language version of the same cross-case query
  and confirm both now surface overlapping/consistent case coverage. If not
  available, say so explicitly rather than implying it was validated.

## Git discipline (this task, explicitly authorized — differs from this repo's usual default)

- Create one branch for this work, e.g. `fix/retrieval-cross-case-diversity`.
- Commit each fix separately, with a message describing what changed and which
  issue (1 or 2, per this doc) it addresses.
- After both fixes are implemented, verified, and reviewed: merge to `main` locally,
  then **push to `origin main`**. The user has explicitly asked for this to be
  pushed this time — unlike this repo's usual "never push without being asked"
  default, that authorization is granted in advance for this specific task. Still
  confirm with the user before the push itself if anything about the diff looks
  larger or riskier than expected.
- Do not force-push, do not rewrite history, do not touch other branches.

## Report format

After each fix: what changed (files/lines), why, test output, what wasn't verified,
and the rollback command (`git revert <sha>` or branch deletion pre-merge).
