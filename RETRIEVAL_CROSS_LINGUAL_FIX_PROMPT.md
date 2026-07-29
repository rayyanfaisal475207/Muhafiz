# Muhafiz — Cross-Lingual Retrieval Consistency Fix (Fix 3)

You are fixing a retrieval bug in the Muhafiz Evidence Intelligence Platform
(FastAPI + Postgres/Apache AGE + ChromaDB backend, two React frontends).

## The bug

Asking the same question in Urdu vs. English still surfaces different
documents/chunks, even after two prior fixes
(`RETRIEVAL_DIVERSITY_FIX_PROMPT.md` — BM25 full-corpus scope, and cross-case
diversity capping). Those two fixes addressed retrieval **diversity/coverage**
(making sure more than one relevant case can appear in the candidate pool) —
they explicitly did NOT address cross-lingual **consistency** (whether the
*same* case/chunks surface regardless of which language the question is
asked in). This doc is that follow-up, scoped narrowly to just that gap.

## Root cause (already diagnosed — do not re-derive from scratch, verify against current code first)

Two compounding issues, neither touched by the prior two fixes:

1. **BM25 is script-blind.** `src/retrieval/bm25_retriever.py` tokenizes with
   a plain word-level tokenizer (`src/ingestion/tokenizer.py::tokenize`) and
   scores exact token overlap. The corpus is a genuine mix of Urdu-script and
   English documents. An Urdu-script query gets real lexical hits against
   Urdu-script chunks (shared words, FIR numbers, names); the same question
   asked in English gets essentially **zero** BM25 signal against those same
   Urdu-script chunks — completely different alphabet, no token overlap at
   all. This asymmetry feeds directly into the RRF fusion score
   (`src/retrieval/reranker.py`), so the same question in two languages gets
   different fused rankings **by construction**, not by bug — Fix 1 changed
   *what pool* BM25 searches over, not this asymmetry.
2. **`multilingual-e5-large-instruct` doesn't guarantee identical
   nearest-neighbors across languages.** Real, well-known cross-lingual
   embedding drift — semantically-equivalent phrasings in two languages land
   *close* in vector space, not identically ranked. This is the mechanism
   named directly in both prior fixes' commit messages as the underlying
   cause, and explicitly left untouched (`RETRIEVAL_DIVERSITY_FIX_PROMPT.md`:
   "no changing embedding models" was out of scope for Fix 1/Fix 2).

**Why case-scoped queries are affected most:** Fix 2's diversity capping only
activates when there's no `case_id` in `where_clause` (a genuinely cross-case
query). A question already scoped to one case skips Fix 2 entirely — you're
seeing raw, unmitigated embedding+BM25 fusion behavior, and issue 1 above (the
BM25 asymmetry) is the dominant, easily-demonstrable cause there.

**Compounding constraint, not itself a bug:** `prompts/query_rewriter.txt`
rule 5 explicitly forbids translating the user-facing rewritten query ("Keep
the rewrite in EXACTLY the language/script the user wrote in") — for good
reason: translating it would break BM25's lexical match against the
untranslated source text even further for whichever language wasn't chosen.
This means nothing in the pipeline currently gives retrieval *both* language
variants of a query to search with — each request only ever searches in one
script. This constraint must be preserved (do not touch query_rewriter.txt or
change what language the final answer is generated in).

## Read before touching anything

- `src/pipeline/orchestrator.py` — the RAG route, ~lines 1248-1400: where
  `all_queries` is built (`current_query` + `expand_query()`'s output),
  embedded, vector-searched, and joined into `combined_query` for BM25. This
  is the single integration point — everything downstream (RRF fusion,
  cross-case diversity capping from Fix 2, cross-encoder reranking) already
  operates generically over `all_queries`/`semantic_results`/`combined_query`
  with no hardcoded assumption about how many query variants there are.
- `src/pipeline/query_expander.py` — the existing pattern to mirror: a small,
  focused LLM call with graceful `[]`/`None`-on-failure degradation, driven
  by a prompt file under `prompts/`.
- `prompts/query_expander.txt` / `prompts/query_rewriter.txt` — style and
  tone to match; note query_rewriter.txt's explicit "never translate" rule
  applies to the user-facing rewrite, not to this new internal-only variant.
- `src/ingestion/tokenizer.py` — the shared tokenizer BM25 uses, to confirm
  how it handles Urdu-script vs. Latin-script text (relevant to picking a
  reliable script-detection method).

Confirm line numbers against current code — this diagnosis was written
2026-07-30 from a live read, but don't trust stale line numbers blindly.

## What to implement

**One fix, one branch.** Add a single additional query variant — generated
via one small LLM call, translated/transliterated into "the other" script
relative to the input query's detected script (Urdu-script → English,
English/Roman-Urdu → Urdu-script) — used ONLY to widen the retrieval
candidate pool. It is never shown to the user and never affects the final
answer's language (`preferred_language` handling is untouched).

Concretely:

1. New prompt file `prompts/cross_script_query.txt` — a focused translation
   prompt (not a paraphrase/expansion prompt like query_expander.txt): given
   a query and a target script/language, produce ONE faithful
   translation/transliteration, preserving case-specific identifiers (FIR
   numbers, CNICs, phone numbers, PPC/PECA section numbers, proper nouns)
   verbatim rather than translating them. Output should be exactly one line,
   same minimal-ceremony contract as `query_rewriter.txt` ("no preamble, no
   quotes, no markdown").
2. New module `src/pipeline/cross_script_variant.py`, mirroring
   `query_expander.py`'s shape:
   - A script detector (regex on the Arabic-script Unicode block, e.g.
     `؀-ۿ`, is sufficient — Urdu-script text will contain
     characters in that range, Latin-script English/Roman-Urdu will not).
   - `async def generate_cross_script_variant(query: str) -> str | None` —
     detects the query's script, calls the LLM with the new prompt asking
     for the *other* script's equivalent, returns the one-line result or
     `None` on any failure/empty response (same graceful-degradation
     contract as `expand_query`'s `[]`-on-failure).
   - Same max_tokens-vs-reasoning-trace consideration as every other
     Qwen3-14B call site in this codebase (see `query_expander.py`'s and
     `query_rewriter.py`'s comments on this) — size `max_tokens` accordingly,
     don't reintroduce the "silently truncates to empty" failure mode.
3. In `orchestrator.py`'s RAG route, alongside the existing
   `expanded_queries = await expand_query(current_query, n=2)` call, add the
   cross-script variant and fold it into `all_queries`:
   ```python
   cross_script_query = await generate_cross_script_variant(current_query)
   all_queries = [current_query] + expanded_queries + ([cross_script_query] if cross_script_query else [])
   ```
   Nothing else needs to change — `embed_tasks`, `search_tasks`, and
   `combined_query = " ".join(all_queries)` (feeding BM25) already iterate
   over `all_queries` generically, so the new variant automatically gets
   embedded + vector-searched AND contributes real same-script tokens to the
   BM25 pass, closing gap 1 above and reducing the practical impact of gap 2.

## Non-negotiable working rules

- **One fix.** Don't touch Fix 1/Fix 2's code, don't touch the embedding
  model, don't touch `query_rewriter.txt`'s "never translate" rule or the
  final answer's language handling.
- Re-read the actual current code first — line numbers above may have
  drifted.
- If this turns out to be based on a misreading of the current pipeline,
  stop and say so rather than implementing a fix believed to be wrong.
- Report outcomes honestly: show real test output; if something can't be
  verified in this environment (e.g. no live model server), say so plainly.

## Verification

- Run the full backend test suite:
  `python -m pytest tests/ --continue-on-collection-errors`
- Add tests: a unit test for `generate_cross_script_variant`'s script
  detection and graceful-failure behavior (mocking the LLM call, no network),
  and an orchestrator-level test proving the cross-script variant's text
  ends up in `all_queries`/the BM25 `combined_query` (mirroring Fix 2's
  existing test style in `tests/test_orchestrator.py`, e.g. a chunk that's
  only BM25-matchable via the cross-script variant's vocabulary).
- If live model-server access is available in this session, use it: run an
  actual Urdu-language and English-language version of the same
  single-case-scoped query and confirm both now surface more overlapping
  chunk sets than before. If not available, say so explicitly rather than
  implying it was validated.

## Git discipline

- Create one branch for this work (e.g. `fix/cross-lingual-retrieval-variant`).
- Commit the change with a message describing what changed and why.
- After implemented and verified: merge to `main` locally. Do NOT push to
  `origin` — the user will push manually later, unlike
  `RETRIEVAL_DIVERSITY_FIX_PROMPT.md`'s explicit push authorization, this one
  stays local-only until told otherwise.
- Do not force-push, do not rewrite history, do not touch other branches.

## Report format

What changed (files/lines), why, test output, what wasn't verified, and the
rollback command (`git revert <sha>` or branch deletion pre-merge).
