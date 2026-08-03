# Why RAG Sometimes Fails — Root Cause Notes

Short answer: **RAG itself (retrieval) is not the problem.** Every failure traced
today came from the **generation step** — the local model choosing not to use
the evidence it was already given — and, secondarily, from the **verifier**
not reliably catching that. Neither is a flaw in the RAG *architecture*; both
are local-model reliability issues that the pipeline now has real (but not
perfect) defenses against.

---

## 1. Is retrieval/the database the problem?

**No.** Confirmed repeatedly, on every failing query tested:

- Retrieval always found the correct chunk (10 chunks retrieved, right
  document among them).
- The **evaluator** step always correctly said `relevant: True`, and its own
  reasoning text quoted the *correct* content (right names, right police
  station, right dates).
- ChromaDB / Postgres were never at fault — checked directly, data is there
  and correctly indexed.

So by the time a query reaches the generation step, the pipeline already has
the right evidence in hand, every time. The failure happens *after* that.

## 2. The core issue: generation-step refusal

The **local generation model** (Qwen3-14B, used for the final answer text)
sometimes ignores the retrieved documents sitting in its own prompt and
answers instead like a generic assistant with "no database access":

> "I don't have access to specific case files, police records, or
> databases... this information is typically confidential... contact the
> appropriate law enforcement agency directly."

This happens **even though the correct answer is right there in the prompt**.
It's a privacy/safety-training reflex triggered by the *shape* of the
question (a specific person, a case number, "witness report") — not a
retrieval failure, not a prompt-formatting bug. Confirmed directly: the exact
same prompt, sent to a different model (Groq's cloud model), answers
correctly every time. Only the local model does this.

It's also not confined to one fixed sentence — it shows up in many different
phrasings ("not publicly available," "part of a closed investigation,"
"typically confidential," "consult official records," etc.), which is why a
simple keyword filter alone can't fully catch it.

## 3. The secondary issue: the verifier didn't reliably catch it

Every RAG answer is supposed to pass through a **Verifier** step before
delivery, whose job includes flagging exactly this pattern (its own prompt
defines `off_topic: true` as "a generic non-answer when case-specific content
was available"). In practice, the verifier — itself another local-model
judgment call — **sometimes missed it**, marking a refusal `grounded: true`
and letting it through to the user unfiltered. So for a while, refusals were
reaching the interface disguised as confident answers, rather than being
blocked.

## 4. What's actually been fixed

- **Deterministic backstop in the verifier**, independent of the LLM judge's
  own (unreliable) opinion:
  - A phrase-based check for known refusal language, tiered by strength so
    it doesn't reject a genuinely good, cited answer just because it also
    includes an honest caveat sentence.
  - A **citation-based check**: any substantial answer that cites zero
    `[Document N]` sources — regardless of exact wording — is treated as
    suspicious, since a real grounded answer always cites something. This
    is the more durable fix, since it doesn't depend on anticipating every
    future refusal phrasing.
- **Local-only retry with correction**: on a detected refusal, the pipeline
  re-asks the model up to 2 more times with an explicit correction
  ("you DO have access, the documents ARE the case file, don't refuse"),
  entirely on the local model — no cloud dependency, per your instruction to
  stay local-first.
- **Honest failure message**: if all retries still refuse, the user now sees
  an honest "I cannot provide a confident answer" instead of either (a) the
  refusal disguised as a real answer, or (b) a misleading "the knowledge base
  doesn't have this" message when the data was actually there all along.

## 5. What's still a real limitation

- The local model can still refuse **all 3 local attempts** on a
  particularly "sensitive-sounding" query. When that happens now, you get a
  safe, honest abstention — not a wrong answer — but not the correct answer
  either. This is a genuine capability ceiling of the local model for this
  narrow class of query, not something prompt engineering alone reliably
  overcomes.
- Cloud escalation (Groq) *would* reliably break through this in nearly
  every case tested — but per your explicit instruction, cloud is now
  reserved strictly for genuine local unavailability, not for local
  content-quality issues like this. That's a deliberate trade-off (fewer
  cloud calls / lower rate-limit risk) in exchange for occasionally losing a
  answerable query to a stubborn local refusal.

## 6. Bottom line

| Question | Answer |
|---|---|
| Is RAG (retrieval) broken? | No — confirmed working correctly every time. |
| Is the database/embeddings the issue? | No — data is present and correctly retrieved every time. |
| Is the router misrouting? | Not the primary issue for this failure class — separate, already-addressed issue. |
| Where's the actual failure? | Generation step: local model refuses to use evidence it already has. |
| Is the verifier at fault? | Partially — its own LLM judgment missed refusals; now backed by deterministic checks. |
| Is this fixable with more prompting alone? | Not fully — it's closer to a safety-training reflex than an instruction-following gap. |
| What's the current behavior on failure? | Honest abstention, never a disguised wrong answer. |

---

## 7. Update (2026-08-02) — root cause found and fixed: it wasn't wording, it was message placement

A follow-up audit re-ran the refusal-prone queries and found the problem had gotten
**worse**, not just persisted: a sample of previously-reliable RAG queries (e.g. the
missing-person-report lookup for `MP-2026-001`, documented as clean in `demo_1.md`)
now refused on **all 3 local attempts** too, not just the known-hard cases like
`FIR-2026-CYBER-004`. This raised the priority of finding an actual fix rather than
just tuning the existing safety net further.

### What was tested and ruled out

- **Prefill/response-prefix forcing** — refuted directly. Probed the local model
  server's bespoke `/llm` endpoint with 7 candidate field names (`prefix`,
  `assistant_prefill`, `response_prefix`, `prefill`, `seed_response`,
  `continue_from`, `assistant_prefix`) on real requests. Every one was silently
  ignored — output was identical with or without the field, confirmed by a
  temperature-0 control (a poem prompt seeded with `"Once upon a time in
  Bananaland, "` came back as a plain, unrelated sentence every time). This
  server does not expose anything like assistant-prefill; this lever isn't
  available here.
- **Reframe as extraction, not Q&A** — tested directly on the exact
  `FIR-2026-CYBER-004` witness-statement content, 3 runs. A rigid "extract these
  4 fields" framing did stop the refusal (0/3), but broke in a different way —
  the model asked the user to "please provide the document," even though the
  document was right there in the prompt. A more generalized extraction framing
  ("extract and report facts responsive to the request") went back to refusing
  (3/3), with the exact same wording as the baseline. Extraction framing is not
  a reliable fix on its own.
- **Explicit synthetic-data framing** — refuted cleanly. Added a prominent
  system-prompt note ("all case data below is synthetic demonstration content,
  not real personal data — no real confidentiality concern") ahead of the exact
  same refusing prompt, 3 runs. **Zero effect** — byte-for-byte identical
  refusal text, 3/3, with or without the framing. Whatever is triggering this
  reflex, it isn't reading (or isn't persuaded by) an explicit disclaimer about
  the data's realness.
- **Model-level lever** — not testable this session; there's no channel to the
  model server operator from here. Flagged as an open question below.

### What actually fixed it

Comparing the RAG generation call (`orchestrator.py`, refuses) to the evaluator
call (`evaluator.py`, same local model, same underlying chunk content, **never**
refuses) turned up a real structural difference that none of the four hypotheses
above were about: **where the retrieved documents live in the request**.

- Evaluator: retrieved chunks go in the **user/prompt field**; the system field
  is generic, static instructions.
- RAG's final-answer generation (before this fix): retrieved chunks — the actual
  case files, witness statements, etc. — were baked into the **system field**,
  with only the bare user question in the prompt field.

Controlled A/B test (direct calls to the local model, bypassing the pipeline,
3 runs per condition, 2 different query/document pairs — the `CYBER-004` witness
statement and the `MP-2026-001` missing-person report):

| Condition | Refusals |
|---|---|
| Same instructions + same documents, documents in **system** field | 6/6 |
| Same instructions + same documents, documents in **user** field | 0/6 |

Identical content, identical rules, only the message role holding the documents
changed. This is not a wording/framing fix — it's why the evaluator was never
part of this bug in the first place, and why "reword the refusal-prevention
instruction" approaches (rule 9 in `final_response.txt`) could never fully close
the gap: the instruction telling the model it has access was itself sitting in
the same system-prompt slot the model's safety training treats with more
suspicion for this kind of content.

### The fix

- `prompts/final_response.txt`: stripped down to instructions only — no more
  `{documents}` / `{project_memory}` / `{user_context}` / `{history}`
  placeholders. It now tells the model that its next (user) message will
  contain those sections.
- `src/pipeline/orchestrator.py` (RAG branch): builds a new
  `grounded_user_message` containing the PROVIDED DOCUMENTS, ESTABLISHED
  PROJECT CONTEXT, USER CONTEXT & PREFERENCES, CONVERSATION HISTORY, and the
  user's actual question — sent as the user turn instead of the bare question.
  The existing 3-attempt local-only retry-with-correction loop is unchanged in
  structure; the `[SYSTEM CORRECTION]` text still gets appended to the (now much
  shorter) system prompt on a detected refusal.
- Scope: **only the RAG route was changed.** GRAPH/XGRAPH/XAGG/SQL/WEB routes in
  `orchestrator.py` use the identical system-prompt-embeds-content pattern and
  likely share this exact risk, but they weren't showing the same confirmed
  failure this session and weren't touched, per the "distinct path for RAG, not
  a universal change" guidance — worth the same fix later, but only after its
  own live verification.

### Verification

- Unit: `tests/test_orchestrator.py` — 3 tests asserted retrieved content
  reaching the prompt via `system_prompt` (a mock capture point); updated to
  check the user-turn capture instead (added `fake_call_llm.last_user`,
  mirroring the existing `last_system`). Full suite: `pytest tests/ -q` — all
  passing, no regressions.
- Live, through the real running pipeline (not just the isolated A/B calls
  above): `FIR-2026-CYBER-004` and `MP-2026-001`, 3 runs each — **0/6 refusals**,
  all `grounded: true`, all answered on the first generation attempt (no retry
  needed), citations present (`[Document 1]`), facts correct and matching
  `demo_1.md`'s documented answers exactly. `MP-2026-020` (previously flagged in
  Known Limitations as an abstention) also now answers correctly — a bonus, not
  just a neutral result.
- Regression check: `FIR-2026-RTA-001` and the certified-copy-of-FIR SOP
  question were re-run. `FIR-2026-RTA-001` showed evaluator-stage relevance
  flakiness (`relevant: false` on the first retrieval attempt) but recovered
  and answered correctly on retry. The certified-copy-of-FIR question was
  less consistent: it failed all 3 retrieval/evaluator attempts (never once
  reaching `relevant: true`) across two repeated runs on the fixed code, while
  a single control run against the **unmodified pre-fix code** reproduced the
  identical `relevant: false → false → true` pattern before eventually
  reaching generation (where it then hit the pre-existing no-citation refusal
  this whole fix is about). The control run confirms the evaluator instability
  itself isn't new or caused by this change — `evaluator.py` and the retrieval
  code were not touched — but the sample here is small (1 old-code run vs. 2
  new-code runs) and retrieval latency was visibly elevated throughout this
  session's testing (9-20s+ per retrieval call, vs. the sub-10s the original
  demo reported), consistent with general network/tunnel load rather than
  anything code-related. Flagging this as a real, separate, **not fully
  characterized** open question about evaluator-stage reliability for this
  specific query — worth a dedicated look, not silently written off.
- Operational note: verification above ran against a second, temporary backend
  instance, because the live-facing backend process on the app's usual port
  could not be restarted from this session — a local ngrok tunnel fronting it
  meant Windows process tools couldn't reliably identify or kill the underlying
  process. The code fix itself lives on `fix-rag-refusal-doc-placement` and is
  merged to `main`; the operator needs to restart the actual serving process
  for the live app to pick it up.

### Refusal rate: before vs. after (this sample)

| | Before | After |
|---|---|---|
| `FIR-2026-CYBER-004` witness question | refused, all 3 local attempts, every run | 0/3 refused |
| `MP-2026-001` (previously "reliable") | refused, all 3 local attempts, every run | 0/3 refused |
| `MP-2026-020` (Urdu, prev. abstained) | abstained (Known Limitations) | answers correctly |
| `FIR-2026-RTA-001` | refusal / no-citation reject after evaluator passed | succeeds |
| cert-copy-of-FIR | refusal / no-citation reject after evaluator passed (when evaluator did pass) | evaluator itself unstable on this query on both old and new code — separate open issue, see below |

Not yet re-run: the full 30-question `demo_1.md` set end-to-end (this session's
audit was deliberately scoped to a targeted subset — the known refusal cases
plus a few controls — rather than the full set, per an explicit scope choice
this session; the fix should hold given the mechanism is structural, not
per-query, but the complete set hasn't been exercised against it yet).

### Open questions / follow-ups

- **Model-level lever** (different checkpoint, `/no_think`-style flag, sampling
  changes) — never asked; no channel to the model server operator this
  session. Given the structural fix already closes the gap in every case
  tested, this is now lower priority, but worth asking if refusals reappear on
  content shapes not covered here.
- **GRAPH/XGRAPH/XAGG/SQL/WEB routes** share the same system-prompt-embeds-
  documents pattern as RAG had. Not confirmed broken, not fixed — flagged as a
  likely-same-risk follow-up, to be tested (not assumed) before changing.
- The extraction-framing and synthetic-data-framing experiments are recorded
  above specifically so they aren't re-tried from scratch next time — both were
  measured, not just theorized, and neither is the fix.
- ~~**Certified-copy-of-FIR query evaluator instability**~~ — **root-caused,
  see section 8 below.** Not evaluator flakiness at all: retrieval itself was
  silently dropping the one correct document from the candidate pool before
  the evaluator ever saw it in some runs.

---

## 8. Update (2026-08-02, continued) — a second, unrelated bug found during the
30-question re-audit: RRF fusion silently drops high-confidence semantic-only
hits

Running the fix against the **full** `demo_1.md` question set (not just the
refusal-prone subset) surfaced a second, structurally different problem — nothing
to do with generation refusal, this one is upstream in **retrieval fusion**.
Several Part 1 (official SOP) queries — certified-copy-of-FIR, foreigner
registration, safety tips, and a Part 2 case query (cyber-005 investigating
officer) — failed at the **evaluator** stage (`relevant: false` across all 3
retries), even though `demo_1.md` documented them as reliable.

**Root cause, confirmed directly against the pipeline's own SQLite retrieval
log** (`data/pipeline_logs.db`, `retrieved_documents` table) for the
certified-copy-of-FIR query: `REAL-004-copy-of-fir-procedure.pdf` — the one
genuinely relevant document — was semantic search's **#1 or #2 highest-scoring
hit** (cosine similarity 0.90-0.94) on every single retry. But it **never once
appeared** in the fused (RRF) top-10 that reached the evaluator, because BM25
never found it at all.

This is not a tokenization or indexing bug — checked directly, the shared
tokenizer overlaps `certified`/`copy`/`fir`/`procedure`/`fee` correctly on both
the query and the document. It's a real, structural property of Reciprocal
Rank Fusion: RRF's score is `sum of 1/(rank + k)` **per list a document
appears in** — it only ever weighs *ordinal rank*, never raw similarity
magnitude. A document found ONLY by semantic search, even at a very high
similarity, contributes a single term and routinely loses to documents that
rank only moderately in *both* lists (two smaller terms summed beat one large
one). Here, BM25 misses `REAL-004` specifically because its distinguishing
terms get diluted by the word "FIR" appearing in hundreds of unrelated case
documents across the corpus — a real corpus-scale side effect, not a defect
in any single component.

### The fix

`src/retrieval/reranker.py`'s `rerank_results()` — the orchestrator-facing RRF
wrapper — now applies a **semantic confidence floor** after fusion: any chunk
with raw semantic similarity ≥ `SEMANTIC_FLOOR_SCORE` (0.85) that RRF's fusion
would have dropped entirely gets appended back in (capped at
`SEMANTIC_FLOOR_MAX_RESCUED = 2`, so this rescues a small number of
very-high-confidence hits, not an unbounded semantic-search bypass of hybrid
fusion). This applies to both call sites (RAG and GRAPH_HYBRID), since it's a
property of the fusion algorithm itself, not route-specific behavior.

The two hypotheses originally proposed for this ("BM25 doesn't find it" vs.
"the diversity cap drops it") turned out to be **the same underlying gap**:
`cap_case_diversity()` was checked and cleared first (`REAL-004` survives it —
it's the top scorer within its own bucket); the actual gap is RRF's rank-only
fusion math, which the floor fixes directly. There is no separate "BM25 fix"
needed beyond this.

### Verification

- Unit: two new tests in `tests/test_retrieval_and_memory.py` —
  `test_semantic_floor_rescues_a_high_confidence_bm25_invisible_chunk` (proves
  the exact failure mode: a 0.93-similarity semantic-only chunk that plain RRF
  drops from a top-10 of 10 dual-list-ranked chunks is rescued by
  `rerank_results()`) and `test_semantic_floor_ignores_chunks_below_the_threshold`
  (a merely-decent 0.5-similarity semantic-only chunk is NOT force-included —
  the floor doesn't become a backdoor around hybrid fusion). Full suite:
  `pytest tests/ -q` passing.
- Live re-verification against the actual previously-failing queries (not just
  unit-level) was in progress when this note was written — see the session's
  running commentary for the latest live results before treating this as fully
  closed end-to-end.

### Scope note

This is a genuinely separate bug from the generation-refusal issue in sections
1-7 above — it lives entirely in retrieval fusion, before the evaluator or
generator ever run. Finding it doesn't change anything about the refusal
diagnosis/fix; it does mean this document's original headline claim ("RAG
itself / retrieval is not the problem") needs a caveat: retrieval was reliable
for every *specific-case-number* query tested in the original refusal audit,
but not for every query shape — a full-corpus, high-similarity semantic hit
can still be dropped by fusion when BM25 doesn't independently corroborate it,
particularly for reference/SOP-style content competing against a much larger
case-document corpus using overlapping generic terms.

## 9. Update (2026-08-03) — a third bug: unscoped queries naming a specific
   FIR number were never actually scoped to that case

### Symptom

Live-tested (no case selected in the UI, matching how every query in this
document has been tested so far):

- "Trace the full case history for FIR-2026-THEFT-001 from the initial
  complaint through to the charge sheet." → retrieval failed on all 3
  retries, final response: "I couldn't find sufficient information in the
  knowledge base..."
- "list of people accused" (no case number at all) → evaluator eventually
  marked scattered, unrelated documents "relevant" on retry 2, but
  generation correctly refused to synthesize a coherent answer from them.

### Root cause (confirmed via direct SQLite pipeline-log inspection, not
   guessed)

`data/pipeline_logs.db`'s `retrieved_documents` table showed the FIR-2026-
THEFT-001 query's retrieval was **never scoped to that case at all** — every
retry ran RRF fusion across the entire unscoped corpus (270+ chunks spanning
40+ unrelated cases: THEFT-010/011/012, BUR-007/008/009, FRAUD-015/016,
HAR-001/002/018, DOM-013/014, CYBER-005/006, ARMS-001/003, etc.). With
`TOP_K_RETRIEVAL=10` and this many same-shaped competitors (every case
document contains the tokens "FIR", "2026", a crime category, and a 3-digit
number), THEFT-001's own 4 documents (FIR, Darkhast/complaint, case diary,
charge sheet) routinely lost the RRF fusion cut entirely — confirmed in the
logs: `FIR-2026-THEFT-001.pdf` itself never appeared in the fused top-10 on
any of the 3 retries, crowded out by THEFT-010/011/012 and others that merely
share the same generic tokens.

The obvious fix — "just select the case first" — doesn't work for this
corpus: the Chroma `case_id` metadata used by this bulk synthetic dataset
(e.g. `CASE-B0-THEFT-001`) uses a different naming scheme than the Postgres
`cases` table's rows (e.g. `CASE-002`), and **the `cases` table has no row
for it at all** (confirmed: `SELECT case_id, fir_number FROM cases WHERE
fir_number LIKE '%THEFT-001%'` → 0 rows). There is currently no way to select
this case from the UI's case picker — it isn't a bug in the picker, this
dataset was never registered as a `cases` row when it was ingested. So for
this whole corpus, the fixed TOP_K_RETRIEVAL window is the *only* mechanism
standing between a specific-case query and drowning in every other case's
chunks, and it wasn't enough once the corpus grew past a handful of cases per
crime category.

### The fix (`src/pipeline/orchestrator.py`, RAG route only)

Before the RAG route's retry loop, if no `case_id` is already active, extract
any FIR-number-shaped identifier from the query text using the existing
`extract_fir_numbers()` (`src/extraction/structured_fields.py` — already used
at ingestion time, never previously wired into the query path). If found,
resolve it to a concrete `case_id` by scanning the already-fetched,
project/global-scoped chunk pool for any chunk whose `source` filename
contains that FIR number, and reuse its `case_id` metadata to scope the rest
of retrieval for this query (`where_clause["case_id"] = resolved`,
`is_cross_case = False`). This works entirely off Chroma's own metadata — it
does not depend on the `cases` Postgres table having a matching row, so it
fixes this corpus's gap without needing to backfill 43 `cases` rows to match
the demo dataset's `CASE-B0-*` scheme.

Deliberately narrow: only fires when no case is already active (never
overrides an explicit UI case selection), and only when the query names an
explicit FIR number — it does not attempt to solve genuinely ambiguous,
case-less aggregate queries like "list of people accused" (see below).

### Verification

- Unit: three new tests in `tests/test_orchestrator.py` —
  `test_fir_number_in_query_auto_scopes_retrieval_even_with_no_case_active`
  (the exact failure mode: a FIR number in the query text, no case active,
  must resolve `where` to `{"case_id": ...}` instead of `{"is_global": True}`),
  `test_no_fir_number_in_query_leaves_case_scoping_untouched` (regression
  guard — a query with no identifier must not trigger the lookup at all), and
  `test_fir_number_auto_scope_never_overrides_an_already_active_case`
  (regression guard — an explicit UI case selection always wins). Full suite:
  `pytest -q` — all passing, no regressions.
- Live re-verification against the real running backend (port 8001, restarted
  to pick up the change), the exact previously-failing query, 3/3:
  `router/done` now reports `case_scope: within_case` (was `cross_case`
  before the fix); retrieval, evaluator, and citation validator all pass on
  the *first* attempt (0 retries needed, vs. exhausting all 3 retries and
  failing before the fix); the response correctly traces the case from the
  2026-01-02 Darkhast complaint through to the charge sheet, fully grounded.

### Still open: "list of people accused" (no case number, no case active)

This is a genuinely different, unsolved problem — not a retrieval bug in the
same sense. With no FIR number and no case active, there is no signal to scope
retrieval to a single case; the query is inherently asking "across the entire
corpus" for something ("the accused") that is only meaningful *relative to a
specific case*. The evaluator's retry-2 behavior (marking scattered,
unrelated-case chunks "relevant") is itself questionable, but the generator's
refusal to synthesize a fabricated cross-case "list of accused" from them is
arguably correct — the alternative would be hallucinating a merged list from
unrelated cases. The real fix here isn't a retrieval change; it's a product
decision (see the session's broader project-status discussion): either the UI
should push users toward selecting a case before asking case-relative
questions, or this class of query should route to XAGG (currently gated to
supervisor-or-higher — see the GRAPH/XGRAPH case_id discussion). Not
addressed in this change.
