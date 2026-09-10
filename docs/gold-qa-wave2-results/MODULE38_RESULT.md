# Module 38 — `cross_rerank_multi()` merged by max score across queries

**Branch:** `fix/cross-rerank-rrf-fusion`
**Question:** KB4 (primary), KB1–KB9 (regression bucket)
**Chroma:** ran against a **private copy** at `D:/Rapids AI/muhafiz-m38/data/chroma_db`
(`muhafiz_kb` 7,716 · `muhafiz_community_reports` 18 ·
`muhafiz_entity_descriptions` 568 — verified before and after). The shared store
under the main checkout was never opened.

---

## 1. Root cause

`cross_rerank_multi()` (added by Module 30) scores one candidate pool against
several query phrasings — the user's question plus the generated statute
hypotheses — and, until this module, kept each candidate's **maximum** score
across them.

Cross-encoder scores are a *per-query* relevance judgement, not a calibrated
absolute. Nothing normalises them between queries. So the merge was not
"whichever phrasing recognises this chunk"; it was "whichever phrasing happens
to produce the largest numbers", and that phrasing took the entire final
window.

**The brief's hypothesis was right, and the live numbers are unusually clean
about it.** Probed on KB4 with the real cross-encoder, one shared candidate
pool of 32, so that only the merge differs between the two arms:

| phrasing | score range over the same 32 candidates |
|---|---|
| q0 — the Urdu-script question | 0.00002 – 0.16390 |
| q1 — "Punjab Police Rules 1934 case property and malkhana…" (**the governing book**) | 0.00003 – 0.34222 |
| q2 — "Forensics guidelines handling and chain of custody…" (**not the governing book**) | 0.00022 – **0.96709** |

The wrong hypothesis's *worst* candidate scores about as high as the right
hypothesis's *best*. Under a max merge that is decisive by itself.

And the ranks show it is only the scale that is wrong — every phrasing's
*ordering* is sane:

| chunk | q0 rank (score) | q1 rank | q2 rank |
|---|---|---|---|
| `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2197` | **1** (0.1639) | 5 (0.2270) | 13 (0.0286) |
| `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2209` | **2** (0.0705) | **1** (0.2638) | 11 (0.0478) |
| `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c348` | 5 (0.0075) | 4 (0.2436) | 22 (0.0078) |
| `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c1645` | 6 (0.0062) | 3 (0.2504) | 21 (0.0084) |
| `5_Forensics_guidelines_pdf_62ee00b3_c0` | 24 (0.0004) | 30 (0.0001) | **2** (0.9366) |
| `5_Forensics_guidelines_pdf_62ee00b3_c99` | 30 (0.0001) | 32 (0.0000) | 5 (0.8617) |

The forensics query itself ranks the register chunks 11th–24th — it does not
think they are irrelevant *relative to its own list*, it just scores everything
on a different scale. The question and the right hypothesis both put them 1st–6th.
A merge that reads ranks gets this right; a merge that reads scores cannot.

**Second-order consequence, confirmed:** this is also why Module 30 measured a
*third* statute hypothesis as actively harmful — see §8.

---

## 2. Change

| File | Why here |
|---|---|
| `src/retrieval/cross_reranker.py` | The defect. `cross_rerank_multi()` now fuses the per-query ranked lists by reciprocal rank, plus a bounded rescue. |
| `src/retrieval/reranker.py` | Two keyword-only parameters on `reciprocal_rank_fusion()`, both defaulting to exactly what every pre-existing caller already got, so the second fusion pass can reuse the first one's implementation. |
| `src/pipeline/harness/tools/rag.py` | Comment only — the call site's `[Module 30]` note said the merge keeps each chunk's best score, which is no longer true. |
| `tests/test_kb_statute_retrieval.py`, `tests/test_reranker.py` | See §3. |

**The fusion is `reranker.py`'s own `reciprocal_rank_fusion()`, called, not
reimplemented** — as the brief required. Two implementations of one idea in one
retrieval stack would drift. Reusing it needed exactly two things:

- **`score_key`** — the chunks being fused *already carry* an `rrf_score` from
  the semantic-vs-BM25 fusion that built the candidate pool, and that earlier
  number is what `pipeline_logger`, the retrieval diagnostics and provenance
  mean by `rrf_score`. The second pass writes `cross_rerank_rrf` instead of
  silently overwriting it.
- **`apply_year_boost`** — the recency tie-breaker is a prior over the candidate
  *pool*, and belongs to the one fusion that builds that pool. It is worth up to
  +0.003, while the gap between adjacent RRF ranks near the top is
  1/61 − 1/62 = 0.00026, so re-applying it would let a filename's year move a
  chunk a dozen places for reasons unrelated to the question.

### The bounded rescue

Consensus ranking has a real cost: it drops the chunk that exactly **one**
phrasing is certain about — which is the precise property Module 30 added this
function for. So each phrasing keeps a guaranteed voice for its own rank-1
candidate, **appended** rather than promoted, and capped at
`MAX_RESCUED_TOP_HITS = 2`. Same shape and same reasoning as `reranker.py`'s
existing `SEMANTIC_FLOOR_MAX_RESCUED`, which the codebase already uses for the
identical failure of rank-only math one layer up.

Measured, not assumed: with the rescue disabled on the same pools, the chunk it
saves on **KB8** and **KB9** turned out to be the **original question's own
rank 1**, not a hypothesis's — Punjab Police Rules `…_c1662` (rank 1 for the
Roman-Urdu question, ranks 19 and 25 of 32 for the two hypotheses) and
`…_c1561` (rank 1 for the question, 18 and 15). Both are register/case-diary
material the question is directly about. That is a *different* justification
from the one the WIP commit's comment asserted, and the comment was corrected
to what actually reproduces (see §9).

### A knob that was built, measured, and removed

The WIP commit added a per-list `weights` multiplier to
`reciprocal_rank_fusion()` and an `ORIGINAL_QUERY_WEIGHT` constant, so the
user's own question could outvote a generated hypothesis. **Both were removed.**
Swept over 0.25 / 0.5 / 1.0 / 2.0 / 3.0 on the live KB1, KB4, KB8 and KB9 pools:

| question | w=0.25 | w=0.5 | w=1.0 | w=2.0 | w=3.0 |
|---|---|---|---|---|---|
| KB4 | 3× PPR + **2× Anti-Rape** | 5× PPR | 5× PPR | 5× PPR | 4× PPR + 1× Police Order |
| KB8 | 4× CrPC + 1× PPR | 4× CrPC + 1× PPR | 4× CrPC + 1× PPR | 4× CrPC + 1× PPR | 4× CrPC + 1× PPR |
| KB9 | 5× PPR | 5× PPR | 4× PPR + 1× CrPC | 4× PPR + 1× CrPC | 4× PPR + 1× CrPC |
| KB1 | 4× PPR + 1× Police Order | 4× PPR + 1× Police Order | 4× PPR + 1× Police Order | 5× PPR | 5× PPR |

Every value from 0.5 to 3.0 returns the same governing statute book in the same
window; 0.25 is the only one that makes KB4 worse. A knob whose entire measured
range is flat is not a tuning opportunity, it is a footgun on a function the
main retrieval path also calls — so `reciprocal_rank_fusion()` keeps the
signature it had, plus the two parameters that are actually load-bearing.

---

## 3. Unit tests

```
PYTHONPATH=. .venv/Scripts/python.exe -m pytest \
  tests/test_cross_reranker.py tests/test_reranker.py \
  tests/test_retrieval_and_memory.py tests/test_kb_statute_retrieval.py \
  tests/test_harness_tool_rag.py tests/test_harness_tool_graph.py \
  tests/test_harness_tool_global_search.py tests/test_harness_tool_local_search.py
→ 153 passed
```

(`PYTHONPATH=.` is not optional in this worktree — without it the run silently
imports the **main checkout's** `src/`, i.e. tests the code this branch is
supposed to be changing.)

### The test that pins the fusion, and fails against the merge it replaces

`test_cross_rerank_multi_fuses_by_rank_not_by_score_scale` builds KB4's measured
shape: three phrasings whose **score scales** differ by an order of magnitude
and whose **rankings disagree** — the wrong hypothesis scores every candidate
0.90–0.99 while the question and the right hypothesis top out at 0.0021 and
0.30, yet both of those rank the register chunks first.

It asserts the fused order follows rank, and — so it cannot pass for the wrong
reason on data that never separated the two merges — it *also* asserts, in the
same test, that the pre-Module-38 merge would have returned
`["for1", "for2", "for3"]` on that same input.

Verified to fail against the old implementation, not merely to pass against the
new one. With `src/retrieval/cross_reranker.py` reverted to `origin/main`:

```
FAILED test_cross_rerank_multi_fuses_by_rank_not_by_score_scale
FAILED test_cross_rerank_multi_rescues_a_phrasings_rank_one_chunk
FAILED test_cross_rerank_multi_rescue_is_capped
FAILED test_cross_rerank_multi_gives_every_phrasing_an_equal_vote
FAILED test_cross_rerank_multi_does_not_overwrite_the_pools_rrf_score
```

### The rest of the new coverage

| Test | Pins |
|---|---|
| `test_cross_rerank_multi_keeps_a_chunk_only_one_phrasing_recognises` | Module 30's property survives the change — the chunk that is noise for the question and rank 1 for the statute phrasing still reaches the window |
| `test_cross_rerank_multi_rescues_a_phrasings_rank_one_chunk` | The rescue fires, appends rather than promotes (consensus still owns the head), and the counterfactual with `MAX_RESCUED_TOP_HITS = 0` loses the chunk |
| `test_cross_rerank_multi_rescue_is_capped` | Four phrasings each certain about a different chunk cannot grow the window past `top_k + 2` |
| `test_cross_rerank_multi_gives_every_phrasing_an_equal_vote` | The fused order is unchanged when the query list is reordered — the fusion cannot regrow a scale of its own (this is what removing `weights` buys) |
| `test_cross_rerank_multi_does_not_overwrite_the_pools_rrf_score` | The pool's `rrf_score` survives the second fusion pass |
| `test_score_key_leaves_an_existing_rrf_score_untouched`, `test_year_boost_can_be_switched_off_for_a_second_fusion_pass` | The two new `reciprocal_rank_fusion()` parameters, at the reranker level |
| `test_cross_rerank_multi_with_one_query_matches_cross_rerank` (pre-existing) | Single-query behaviour is byte-identical — the non-KB retrieval path is untouched |

---

## 4. Live verification

Two independent live measurements, both against the **private** Chroma copy.

**Reranker liveness was asserted before every batch**, because with
`RERANKER_URL` unreachable `cross_rerank()` logs a warning and *keeps the RRF
order* — a degradation indistinguishable from this module's own change:

```
POST /rerank  → 200, "malkhana register…" 0.9065 vs "forensic evidence…" 0.0000
grep -ciE "cross-encoder rerank failed|RERANKER_URL not configured" backend.log → 0
grep -ciE "rate limit|RESOURCE_EXHAUSTED|429|quota"                 backend.log → 0
```

### 4a. Controlled A/B — one candidate pool, two merges

The decisive measurement. For each question the pipeline's own KB-only
retrieval path is run once (statute hypotheses → `_retrieve_candidates` → RRF
→ 32 candidates), the **live cross-encoder** is called once per phrasing, and
then *both* merges are computed from that one set of ranked lists. No reranker
variance, no LLM variance, nothing between the arms but the merge.

**KB4 — the primary target.** Gold cites **Punjab Police Rules 1934 Vol. III,
rule 27.16** (register of case property and unclaimed property, destroyed only
three years after completion).

| | BEFORE — max-over-queries (`main`) | AFTER — reciprocal-rank fusion |
|---|---|---|
| 1 | `7_Anti-Rape…_d10a4de5_c158` — chain of custody | `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2209` |
| 2 | `5_Forensics_guidelines_pdf_62ee00b3_c0` — packaging | `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2197` |
| 3 | `5_Forensics_guidelines_pdf_62ee00b3_c116` — sharp-weapon packaging | `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c1645` |
| 4 | `7_Anti-Rape…_d10a4de5_c156` — contamination | `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c348` |
| 5 | `5_Forensics_guidelines_pdf_62ee00b3_c99` — biological evidence | `7_Anti-Rape…_d10a4de5_c156` |
| *rescued* | — | `…_68bb5d0d_c654`, `7_Anti-Rape…_d10a4de5_c158` |

**Zero** register chunks before; four after. And they are the right ones —
**chunk text, read out of the store by id, not inferred from a citation**
(Module 30 recorded an evaluator citing "section 174" that was not in the
chunks it judged):

- `…_c2209` — *"Unclaimed property sent in by the police shall be made over to
  the sheriff as soon after arrival as possible and a receipt thereof taken in
  register No. 1 **[rule 27.16(1)]**"* — gold's rule, by number.
- `…_c2197` — *"…which has been in the custody of the police for **over three
  years**. … This register is a permanent record."* — gold's three-year rule.
- `…_c334` — *"**Rule 22.16. Case property.** — (1) The police shall seize
  weapons, articles and property in connection with criminal cases and take
  charge of property which may be unclaimed"*.
- `…_c1645` — *"Detailed lists of stolen property, or of property seized in the
  course of a search, shall be entered in the first case diary…"*.

### 4b. The full pipeline, live over `POST /api/chat`

Backend on **port 8015**, `admin@example.com`, All Cases (no `case_id`),
`route='RAG'` on every question in both arms. The eight KB questions were sent
through the *after* build, then the three retrieval files were reverted to
`origin/main` (`git checkout origin/main -- src/retrieval/cross_reranker.py
src/retrieval/reranker.py src/pipeline/harness/tools/rag.py`), the backend
restarted against the same Chroma, and the same eight re-sent — so the baseline
is **re-derived in this session**, not inherited.

**KB4, after — chunk ids that actually reached the evaluator** (from rag.py's
own log line, attempt 1, `relevant=True`):

```
4_Punjab-Police-Rules-III_pdf_68bb5d0d_c334
4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2209
4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2197
4_Punjab-Police-Rules-III_pdf_68bb5d0d_c1645
7_Anti-Rape…_pdf_d10a4de5_c157
7_Anti-Rape…_pdf_d10a4de5_c158
```

The retrieval half of KB4 is fixed, and the evaluator agreed on the first
attempt: *"The retrieved documents contain formal standards/procedures for
handling and disposal…"*. See §5 for what happened next, which is not good news
and is not tuned away.

---

## 5. Gold comparison

**KB4's gold answer** names **Punjab Police Rules, 1934 (Vol. III) rule 27.16**
— the register of case property and unclaimed property, destroyable only three
years after completion — plus the data half (45 malkhana entries carrying serial
number, registration number, entry date, description, quantity, condition).

Because a single run of this pipeline is noisy (Module 42 measured KB6
abstaining on 4 of 5 identical runs), KB4 was run **three times in each arm**.

| | BEFORE — `main` | AFTER — this branch |
|---|---|---|
| Punjab Police Rules chunks in the evaluator's window | **0 of 3 runs** | **3 of 3 runs** (5–6 each) |
| Rule 27.16 present, verified in chunk text by id | never | every run (`…_c2209`) |
| Three-year rule present | never | every run (`…_c2197`) |
| `status` | done, done, done | error, error, **done** |
| What the answer actually said | *"the question about disposal standards and property record compliance **cannot be answered** based on the provided materials"* · *"the provided documents **do not explicitly detail procedures for disposal**"* · *"The documents provided **do not explicitly mention** procedures for the **disposal**…"* | 2× verifier refusal; 1× a substantive answer (below) |
| Run 2's fallback | 3 KB-only attempts all `relevant=False`, then fell back to the **mixed case pool** and answered off `psrms_fir_…` narratives | — |

**Read the `status` column carefully, because it is the opposite of what it
looks like.** All three `main` runs are recorded as "done", and all three are
fluent statements that *the answer is not in the corpus* — while the governing
rule was sitting four chunks away in the same candidate pool the whole time.
That is not an answer; it is a confident miss with a green light on it.

The one after-arm run that clears the verifier:

> Yes, there are established standards for recording and disposing of property
> in police custody… **Register No. 1** (Document 1) is explicitly designed to
> track property in police custody for **over three years**… It serves as a
> **permanent record**… Unclaimed property is transferred to the sheriff
> (Document 2, **Rule 27.16(1)**), and property in cases with absconding
> accused is retained until proceedings under **Section 512** of the Code of
> Criminal Procedure are complete… **Conclusion:** The property records (e.g.,
> Register No. 1, Form 27.18(1)) are structured to adhere to these standards…

**Verdict — an honest partial.** Right book, right rule, cited by number and
grounded in chunks verified by id. It is **not** gold: gold's three-year clock
runs from *completion of the register*, this answer attaches three years to
*property held in custody*; and the data half (45 entries) is absent, which is
**Module 39's** gap and unreachable from the RAG sub-agent by construction.

**And the failure is reported, not tuned away:** 2 of 3 after-arm runs are
rejected by the *verifier*, after the evaluator accepted the chunks on attempt
1. The two rejection reasons are different and both narrow:

- *"The answer incorrectly attributes alignment with the Anti-Rape Act to the
  property record system, but no chunk mentions the Anti-Rape Act"* — the
  answer over-reached on `7_Anti-Rape…_c157/_c158`, one of which is the
  **rescued** rank-1 chunk of the wrong statute hypothesis.
- *"One claim about property destruction documentation in Register No. 1 lacks
  direct support in the chunks"* — i.e. the answer was already writing about
  gold's rule and lost on a single unsupported sub-claim.

So KB4's `done`-count went 3/3 → 1/3 while its *retrieval* went 0/3 → 3/3.
**Retrieval is fixed; generation is now the binding constraint, and it was not
reachable before because the right documents never arrived.** No attempt was
made to make the verifier more permissive to improve the number — that would be
tuning the measurement, and Module 35's and Module 43's precedent is that a
reported mismatch beats a manufactured match.

---

## 6. Non-gold paraphrase

Two were run. The first one **failed for a reason that has nothing to do with
this module**, and it is reported rather than quietly replaced.

**Paraphrase 1** — *"Maal-e-muqadma jo police apni tahweel mein leti hai, uska
record kis register mein rakha jata hai aur kitne arse baad us property ko
tabah ya nilaam kiya ja sakta hai?"*

Result: **abstained**, three evaluator attempts, all `relevant=False`. The
cause is upstream of everything this module touches: `_is_legal_kb_intent()`
did not match it, so the query never narrowed to the KB corpus, **no statute
hypotheses were generated, and `cross_rerank_multi()` was never called at
all**. Every chunk it retrieved was a case narrative (`psrms_fir_fir-266-26#…`
and friends). A perfectly ordinary legal question about a register and a
disposal period searched the FIR corpus. Filed as a new defect — §8.

**Paraphrase 2** — the same question with a phrasing the KB-intent gate does
recognise: *"**Kis qanoon ke tehat** police ko maal-e-muqadma ka register
rakhna parta hai, aur us property ko kitne arse baad tabah ya nilaam kiya ja
sakta hai — kya hamare record mein iski paabandi nazar aati hai?"*

Roman-Urdu, non-gold, and it exercises the same capability KB4 does from a
different direction. `route='RAG'`, answered on **attempt 1** in both arms:

- **After:** grounded in `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2197` — the
  same "custody of the police for over three years … this register is a
  permanent record" chunk the KB4 fix brings in — plus CrPC disposal
  provisions. *"…property retained for over three years in cases where the
  accused are absconding may be transferred to a permanent register… the
  documents do not impose a specific time limit for destruction or auction
  beyond judicial discretion."*
- **Before:** also answered, citing Punjab Police Rules **27.18(1)**.

**Honest reading: this is a positive control that passes in both arms, not
evidence of new capability.** It is worth reporting because it shows the change
does not break a question the old merge handled — but a paraphrase that passes
before and after cannot distinguish the two merges, and this one does not. The
measurement that does is §4a, where both merges see one identical candidate
pool.

---

## 7. Regression guard — all eight KB questions

Module 30 moved this bucket from **3 of 8 answering at all** to **7 of 8**, and
the instruction was not to reduce that. The baseline below is **re-derived in
this session**, not inherited: same backend, same private Chroma, same model
server, only the three retrieval files swapped to `origin/main`.
`route='RAG'` on all sixteen runs; zero degraded-reranker lines; zero quota hits.

| Q | BEFORE — `main` | AFTER — this branch | |
|---|---|---|---|
| **KB1** | answers — CrPC ss.154/155 via Police Rules + Police Order chunks | answers — same substance | = |
| **KB2** | answers (5 evaluator attempts) | verifier refusal (evaluator accepted on attempt 1) | ~ |
| **KB3** | answers (3 attempts) — CrPC 156/157 framing, not gold's Article 18 conclusion | answers (3 attempts) — same framing, Police Order `…bdaa97d4_c114` in window | = |
| **KB4** | "answers" — *the corpus does not contain this*; **0 register chunks** | verifier refusal; **rule 27.16 + the three-year rule in the window** | see §5 |
| **KB5** | answers — Anti-Rape Rules special measures | answers — same, female officer / private statement / chain of custody | = |
| **KB6** | **abstains** — 6 evaluator attempts, 290s | **answers** — 1 attempt, 93s: forensic packaging + documentation, and correctly states the KB holds no weapon register | **↑** |
| **KB8** | answers — s.173, 14 days, interim report within 3 days | answers — same, cited as **s.173(1)** | = |
| **KB9** | **abstains** — 6 attempts, 272s | **answers** — s.174, Rule 25.31, s.174(3) post-mortem, inquest report | **↑** |

**Answering at all: 6 of 8 before → 6 of 8 after.** The bucket is not reduced.
Composition changed: **KB6 and KB9 gained, KB2 and KB4 lost.**

Two of those four movements are noise and one is not:

- **KB2 is variance, not a regression.** Run three times per arm on the two
  questions that changed verdict: `main` gave **done / error / done**, this
  branch gave **error / done / done**. Identical 2-of-3, opposite ordering. The
  after-arm's *retrieval* is if anything better — the evaluator accepts on
  attempt 1 rather than 5, and the window is CrPC + Qanun-e-Shahadat, which are
  exactly gold's two cited documents.
- **KB4 is real and is analysed in §5** — 3/3 → 1/3 on `status`, 0/3 → 3/3 on
  getting gold's rule into the window.
- **KB6 and KB9 are the two the brief predicted nothing about**, and both moved
  from a full six-attempt abstention to answering with the correct statute.

**On KB6 specifically:** Module 42 established that KB6's abstention is a
cross-language relevance-gate defect, filed as **Module 52**, which runs after
this one. KB6 answering here is **not** a claim that Module 52 is solved. What
changed is upstream of the gate: the max merge handed KB6's window to the
Forensics query (4 of 5 chunks), while the fusion balances Forensics against
the Punjab Police Rules weapon-register hypothesis the question's second half
is actually about — and that window passed the gate on the first attempt
instead of failing six times. Module 52 should re-baseline KB6 against this
branch rather than against `main`.

**Note for Module 52 and anything else touching this path:** the KB bucket's
answer/abstain verdict is *not* reproducible from a single run. Of the four
(question, arm) pairs repeated three times here, **three produced a mixed
verdict** across otherwise identical runs — KB2 varied in both arms and KB4
varied in the after arm. Any future before/after on these questions needs at
least three runs per arm; a single run will manufacture whichever conclusion it
happens to land on.

---

## 8. New defects found

Each is left **deliberately unfixed** and belongs in its own module.

### 8a. Module 30's third statute hypothesis is no longer harmful

Module 30 measured `DEFAULT_HYPOTHESES = 3` as *actively* harmful — "the third
slot forced the model onto a book that does not govern the question at all (the
Anti-Rape Act for both KB8 and KB9), and **those chunks then WON the
cross-encoder rerank**". That "won the rerank" is this module's defect, so the
finding had to be re-measured. It was, on live pools with `--n-hyp 3`:

| | max merge (`main`) | RRF fusion |
|---|---|---|
| **KB9** (3rd hypothesis = *"Forensics guidelines handling of deceased persons…"* — the wrong-book shape Module 30 warned about) | CrPC s.174 + PPR inquest | CrPC s.174/176 + PPR inquest, **no forensics chunk in the window**; the forensics query's own rank-1 chunk is a CrPC chunk two other phrasings also rank highly |
| **KB4** (3rd = CrPC s.165 search and seizure) | 3× Forensics + Anti-Rape + CrPC, **0 register chunks** | PPR register chunks at ranks **1–3**, plus the s.165 chunk at 4 |
| **KB8** | — | the model returned only 2 hypotheses even when asked for 3 |

Under rank fusion a wrong third hypothesis buys one appended slot instead of the
whole window, which is what it should always have cost. **Raising
`DEFAULT_HYPOTHESES` now looks safe and possibly useful — but it is not
measured on gold-answer quality, only on retrieval composition, so it is filed
as its own module rather than folded in here.** `src/pipeline/statute_hypothesis.py`
is untouched by this branch; its `n=2` comment now cites a rejection whose
mechanism no longer exists, and the successor module should rewrite it.

### 8b. `_is_legal_kb_intent()` misses a plain "which law / which register" question

Found by paraphrase 1 (§6). *"Maal-e-muqadma jo police apni tahweel mein leti
hai, uska record **kis register** mein rakha jata hai aur **kitne arse baad**
us property ko tabah ya nilaam kiya ja sakta hai?"* matches none of
`_LEGAL_KB_INTENT_PATTERNS`, so the question searched the mixed case pool,
generated no statute hypotheses, and abstained after three attempts — while the
governing rule sat in the KB corpus, four chunks from where §4a found it. The
near-identical *"**Kis qanoon ke tehat** police ko maal-e-muqadma ka register
rakhna parta hai…"* answers correctly. The gate turns on the presence of the
word *qanoon*, not on the question being about the law.

This is `src/pipeline/harness/tools/rag.py`'s intent gate, deliberately outside
this module's fix. Worth noting that the existing patterns were already, by
their own comment, "mined from the ACTUAL text" of the gold KB questions — so
a paraphrase falling outside them is the predicted failure mode of that
approach, not a surprise.

### 8c. Module 37's orphaned `chunk_fulltext` rows are still reaching the evaluator

Not new — Module 37 already tracks it — but this module produced fresh evidence
and the ids are worth recording. Chunks with the **`_0519abd8_`** prefix (the
orphaned re-ingestion of the CrPC PDF that exists in the BM25 index but not in
Chroma) reached the evaluator on KB2, KB3, KB8 and KB9 in *both* arms, e.g.
`1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_0519abd8_c652` at fused rank
2 for KB8. They carry real statute text so the answers are not wrong, but they
duplicate the live copy `_f9908363_` in the pool and resolve to nothing for
provenance lookups.

### 8d. The verifier rejects a KB4 answer for over-attributing a rescued chunk

See §5. Reported because it is a *consequence* of this change reaching further
into the pipeline, not because it is this module's to fix.

---

## 9. What was kept from the WIP commit

`7736989 wip(rerank): Module 38 in progress — preserved at the model-server
outage` was **revised, not replaced**. Its core judgement was right and its
reasoning is preserved; three things in it did not survive contact with a
working model server.

**Kept:**

- The mechanism — fuse the per-query lists by reciprocal rank, reusing
  `reranker.py`'s implementation rather than growing a second one. Correct, and
  §4 confirms it live.
- `score_key` and `apply_year_boost` on `reciprocal_rank_fusion()`. Both are
  genuinely load-bearing for a second fusion pass and both default to the
  pre-existing behaviour; the arithmetic in the `apply_year_boost` docstring
  (+0.003 boost vs. a 0.00026 rank gap) checks out.
- `CROSS_RRF_SCORE_KEY` as a separate key rather than overwriting `rrf_score`.
- The bounded rescue. The WIP author's last note said they were moving to "a
  bounded rescue, mirroring the existing semantic-floor pattern" — that work was
  in fact already committed, so the resume point was *verifying* it, not writing
  it. It verifies: with the rescue disabled, KB8 and KB9 both lose a chunk they
  should keep (§2).
- The test structure, including `_max_merge_order()` — asserting what the
  replaced merge would have returned on the same input is the right way to stop
  a fusion test passing for the wrong reason.

**Removed:**

- The per-list **`weights`** parameter on `reciprocal_rank_fusion()` and the
  `ORIGINAL_QUERY_WEIGHT` constant, together with their three tests. At the
  shipped default of 1.0 the constant was a no-op, and the measured sweep in §2
  shows the whole 0.5–3.0 range is flat. Adding a weight multiplier to the
  function the *main* retrieval path also calls, on the strength of a knob that
  changes nothing, trades a real footgun for no measured gain. Replaced by
  `test_cross_rerank_multi_gives_every_phrasing_an_equal_vote`, which pins the
  property that actually matters: the fused order depends on ranks alone, not on
  a phrasing's position in the list.

**Corrected:**

- The rescue's justifying comment claimed a specific KB8 measurement — "the CrPC
  s.173 proviso chunk … is rank 1 for the statute hypothesis and rank 25 of 32
  for the Roman-Urdu question, and pure fusion put it at 10". **That does not
  reproduce**, and it could not have been measured: it was written during the
  outage, when `RERANKER_URL` was unreachable and `cross_rerank()` degrades
  silently to RRF order. Re-measured live, the rescue does earn its place on KB8
  and KB9 — but the chunk it saves is the **original question's** rank 1, not a
  hypothesis's. The comment now says what reproduces, with the chunk ids.
- The `cross_rerank_multi()` docstring said Module 30's third-hypothesis finding
  was caused by this defect and left it there. It now also records that the
  finding **no longer reproduces** under rank fusion (§8), and that raising
  `DEFAULT_HYPOTHESES` is deliberately somebody else's module.

Every live measurement in this document was taken **after** the outage commit,
with `/rerank` confirmed reachable at the start of each batch and
`grep -ciE "cross-encoder rerank failed|RERANKER_URL not configured"` on
`backend.log` returning **0** for every run reported here. Nothing measured
before `7736989` was reused.
