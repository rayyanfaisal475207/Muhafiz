# Module 30 — KB3/KB8/KB9: the governing statutory chunk never reached the pool

**Branch:** `fix/kb-retrieval-completeness-statutory-chunks`
**Brief:** `MODULE30_KB_RETRIEVAL_COMPLETENESS_PROMPT.md`
**Split out of:** Module 19b (PR #15)
**Date of every measurement below:** 2026-09-08, worktree `D:/Rapids AI/muhafiz-m30`,
backend on `:8005`, `muhafiz-postgres` healthy, model-server tunnel `/health` = 200
and `POST /embed` returning real 1024-dim vectors.

**Chroma:** this module ran against a **private copy** of the vector store at
`D:/Rapids AI/muhafiz-m30/data/chroma_db` (`muhafiz_kb` 7,716 · counts confirmed
live at start and at every backend boot: `documents_in_store: 7716`). The shared
`D:/Rapids AI/Evidence Intelligence Platform/data/chroma_db` was never written to
and never read from by this module. Nothing was re-embedded or re-indexed in the
end — the corpus turned out not to need it.

**`prompts/evaluator.txt` was NOT changed.** Module 19b's fix stands untouched.
Every verdict below is that evaluator's own.

---

## 1. Root cause

The brief's hypothesis — "the correct statutory chunk never enters the candidate
pool" — is confirmed, and the probe found **four** separate mechanisms behind it,
not one. Each was individually load-bearing: with any of them left in place, at
least one of the three questions still lost its provision before the evaluator
saw it.

### 1.1 No query the pipeline generates ever contains the statute's vocabulary

Every query string the RAG route builds is a paraphrase of the **question**,
never of the **provision**:

- `prompts/query_expander.txt` forbids changing language, and
  `query_expander.py` structurally drops any variant that switched script.
- `cross_script_variant.py` sends a Latin-script query (English **or**
  Roman-Urdu) to **Urdu script**, never to English.

The legal KB corpus is seven **English** statute books (verified by dumping the
store: CrPC 2,577 chunks · Punjab Police Rules 2,677 · Police Order 580 ·
Qanun-e-Shahadat 474 · PTA Act 323 · Anti-Rape Act 161 · Forensics guidelines
134). So a Roman-Urdu question (KB8, KB9) is embedded and BM25'd only as
Roman-Urdu and Urdu-script text against an English corpus.

Measured, `{"is_global": True}` scope, top-30 per query:

| query | KB3 targets (Police Order c111–c115) | KB8 target (CrPC c757) | KB9 target (CrPC c763) |
|---|---|---|---|
| the gold question itself | c114 at rank 15, rest absent | absent | absent |
| an English statute-vocabulary query | **c114 rank 1, c115 rank 2, c113 rank 18** | **rank 1** | **rank 6** |
| statute NAME only ("Police Order 2002 Article 18") | **all absent** | rank 8 | rank 1 |

The third row matters as much as the second: a bare statute name is **not**
enough. The corpus's chunk text mostly does not repeat its own section number —
the CrPC s.173 interim-report chunk contains the string "section 154" and never
the string "173". What retrieves a provision is the provision's own wording.

Also confirmed while probing: at the mixed `all_cases` scope the roman-Urdu
questions' top-8 was **entirely FIR case narratives** (cosine ~0.85), not statute
text at all. Module 8c's KB-only narrowing is what stops that, and it is
already in place — this module builds on it rather than duplicating it.

### 1.2 The multi-variant dedupe kept the FIRST score, not the best

`_retrieve_candidates()` deduped by chunk id and kept whichever score the first
query variant to surface a chunk gave it. That is not a tie-break detail: it is
the score the pool is ordered by next.

Measured on KB3: the Article 18 chunk carrying *"All registered cases shall be
investigated by the investigation staff"* was surfaced weakly by the question
itself (0.87, rank 15 of that query's own 30) and strongly by the statute
variant (0.91, rank 1). Locked to 0.87 it sorted **54th** in the merged pool and
was cut, while its weaker neighbouring chunks survived.

### 1.3 The case-diversity cap truncated the case-less KB corpus to five

`cap_case_diversity(per_case_cap=5)` exists to stop any one **case** filling the
window. Every chunk in the KB corpus is case-less, so they all bucket under
`case_id=None` and the cap applied to the whole corpus at once — diversifying
nothing and simply truncating. Measured: deduped pools of **71, 76 and 85**
statutory candidates cut to **5** before RRF ever saw them.

### 1.4 The cross-encoder cut the right chunk even when RRF ranked it first

Even with c757 at RRF rank 1, `cross_rerank()` — scored against the Roman-Urdu
question — dropped it. Every candidate came back inside **0.0007–0.0022**: noise.
Scored against an English statute phrasing of the same question, the same chunk
ranked 2nd of 12.

### 1.5 A fifth cause, found only after the first four were fixed

With all four fixed, KB3's Article 18 chunk came back at **rank 1 on every
attempt** — and the evaluator still returned `relevant=False` three times in a
row. Correctly: the chunk ends mid-sentence.

> "…(4) All registered cases shall be investigated by the investigation staff in
> the district under"

The words that answer the question — *"under the supervision of the head of
investigation"* and *"(5) The District Police Officer shall not interfere with
the process of investigation"* — are in the **next** chunk. This corpus is
chunked at roughly 350 characters and Article 18 spans five of them. Retrieval
was right and the answer was still absent.

---

## 2. Change

| File | Why here |
|---|---|
| `prompts/statute_hypothesis.txt` (new) | The corpus map and the rules that make the LLM name a governing statute **and** carry its wording. |
| `src/pipeline/statute_hypothesis.py` (new) | `generate_statute_queries()` — up to 2 English retrieval queries, `[]` on any failure, same contract as `expand_query()`. |
| `src/retrieval/cross_reranker.py` | `cross_rerank_multi()` — score candidates against several phrasings, keep each one's best. Single-query behaviour unchanged. |
| `src/retrieval/vector_store.py` | `expand_with_neighbors()` + `ChromaVectorStore.get_by_metadata()` — the sentence-window read for §1.5. |
| `src/pipeline/harness/tools/rag.py` | All the wiring, plus the best-score dedupe (§1.2), the scoped cap change (§1.3) and a diagnostic INFO line naming the chunks that reached the evaluator. |

Everything is gated on `_is_legal_kb_intent()` (Module 8c's own gate) or on the
KB-only `{"is_global": True}` scope. A case-scoped or mixed all-cases query pays
neither the extra LLM call nor the extra reranker pass, keeps `TOP_K_RETRIEVAL`
unchanged, keeps the diversity cap, and reads unwidened chunks.

The pool for a legal question in the KB-only scope is widened to
`TOP_K_RETRIEVAL * CROSS_CASE_RETRIEVAL_MULTIPLIER` — the brief's option 3,
scoped to KB intent exactly as the brief required, never globally. Six query
variants merging into a pool of 10 is one and a half slots each, and a provision
spans five chunks. The cross-encoder still cuts to `TOP_K_RERANK`, so the
evaluator's and the generator's input size is unchanged.

**Why the harness and not `orchestrator.py`:** `HARNESS_CUTOVER_ROUTES` includes
`RAG`, so this is the live path, and Module 8c's KB-only scoping — which this
builds on — exists only there. The legacy route was deliberately not touched.

### What was probed and rejected

| Direction | Result |
|---|---|
| Statute-name-only expansion | **Rejected.** Retrieved none of the Article 18 chunks in the top 30 (table in §1.1). |
| BM25 / statute-token boost | **Rejected on evidence.** BM25 never returned any of the three target chunks for any query tried, before or after the fix. The section number is not in the chunk text, so there is no token to boost. |
| Raising `TOP_K_RETRIEVAL` / the multiplier alone | **Insufficient alone.** With the cap lifted but the first-seen-score dedupe still in place, KB3's c114 sat at pool rank 55 and was still cut. Kept, scoped to KB intent, as one of four. |
| 3 statute hypotheses instead of 2 | **Rejected, measured.** The third slot forced the model onto a book that does not govern the question (the Anti-Rape Act for both KB8 and KB9); those chunks then won the rerank and pushed the correct CrPC s.173 and s.174 chunks out of the final five. |
| Changing `prompts/evaluator.txt` | **Never attempted.** Out of scope by the brief, and §1.5 shows the evaluator's refusals were correct on what it was shown. |

---

## 3. Unit tests

```
PYTHONPATH=. .venv/Scripts/python.exe -m pytest \
  tests/test_kb_statute_retrieval.py tests/test_harness_tool_rag.py \
  tests/test_cross_reranker.py tests/test_reranker.py \
  tests/test_chroma_vector_store.py tests/test_harness_tool_graph.py \
  tests/test_retrieval_and_memory.py tests/test_harness_agent_semantic_search.py \
  tests/test_query_expander.py tests/test_cross_script_variant.py \
  tests/test_harness_cutover.py tests/test_harness_supervisor.py -q
```

All pass, no new failures. `tests/test_kb_statute_retrieval.py` is new (29
tests) and is pinned to the **literal gold text** of the KB questions, loaded
from `evaluation/Gold_QA_Dataset_Final32_With_Answers.json` — the only Gold-32
file that exists, asserted present rather than skipped, because a test
referencing a bare `Gold_QA_Dataset_Final32.json` silently skipped for weeks
before PR #21 caught it.

It pins one test per mechanism:

- `_is_legal_kb_intent` fires on all **eight** KB questions' gold text (English,
  Urdu script, Roman-Urdu) — the statute step is gated on it, so a gate that
  stopped matching would silently disable the whole fix.
- KB3/KB8/KB9's gold text each reach retrieval **and** the rerank with the
  statute phrasings, the original question still among them.
- `test_dedupe_keeps_the_best_similarity_across_query_variants` — KB3's §1.2
  failure in one assertion (0.87 vs 0.91).
- `test_cross_rerank_multi_keeps_each_chunks_best_query_score` — KB8's §1.4
  failure in one assertion.
- `test_kb_only_scope_is_not_truncated_by_the_case_diversity_cap`, with its
  negative control that a pool which really does span cases is still capped.
- `test_expand_with_neighbors_widens_a_mid_sentence_statutory_chunk` — §1.5,
  asserting both that the interfering-DPO sentence arrives and that the chunk's
  id, source and score are untouched.
- Degradation: a failed statute call, an unparseable one, and a broken Chroma
  each fall back to the exact pre-Module-30 path.

---

## 4. Live verification

Both runs used the same worktree, the same private Chroma, the same model
server, `admin@example.com` / platform-admin, All Cases, `/api/chat` on `:8005`.

**Baseline** was measured by this module, not inherited: the worktree detached to
the branch's merge-base `6b86d9e` (PR #22 merged), backend restarted, the same
eight questions sent. Every "before" figure below comes from that run
(`backend_baseline.log`).

`route=RAG` on all eight, before and after.

### Before → after

| Q | Before (merge-base `6b86d9e`) | After (this branch) |
|---|---|---|
| **KB3** | *"No sufficiently relevant documents were found for this question after retrying with query refinements."* (abstained, 242.7s) | Answers, quoting Article 18's *"investigated by 'investigation staff' under the supervision of the head of investigation"* (237.2s) |
| **KB8** | *"No sufficiently relevant documents were found…"* (abstained, 254.1s) | Answers with **Section 173**, the **14-day** limit and the **interim report within three days** (87.4s) |
| **KB9** | *"No sufficiently relevant documents were found…"* (abstained, 225.5s) | Answers with **Section 174**, the inquest duty and the inquest report forms (215.7s) |
| KB1 | Answers with CrPC ss.154/155 | Answers with CrPC ss.154/155 — unchanged |
| KB2 | **Verifier refusal** — *"could not be verified as grounded"* | Answers, but hedges and names no statute |
| KB4 | Answers: Register No. 1, permanent register, sealing | Answers: forensic chain-of-custody, register material gone |
| KB5 | **Verifier refusal** | **Verifier refusal** — unchanged |
| KB6 | Answers on forensic handling | Answers on forensic handling — packaging, sealing, labelling |

### The chunks that actually reached the evaluator

Captured from `backend.log`'s new diagnostic line, then each id looked up
directly in the store — never inferred from a citation in the answer (Module
19b's recorded trap).

**KB3**, every attempt: `3_1527157863PoliceOrder2002_pdf_bdaa97d4_c114` at
position 1. Verbatim, after neighbour widening:

> …(3) The head of investigation in a District shall not be below the rank of
> Superintendent of Police and shall be responsible to his own hierarchy subject
> to general control of the District Police Officer … Provided that the
> **Investigation Wing** shall be located within the Police Station and shall be
> responsible to its own hierarchy … (4) **All registered cases shall be
> investigated by the investigation staff** in the district under the
> supervision of the head of investigation … (5) **The District Police Officer
> shall not interfere with the process of investigation.**

That is all three elements the gold answer cites.

**KB8**: `…_f9908363_c752` **and** `…_f9908363_c757` both in the final five.
c752 is the heading — *"173. Report of police-officer. (1) Every investigation
under this Chapter shall be completed without unnecessary delay…through the
Public Prosecutor"* — and c757 is the operative proviso — *"where investigation
is not completed within a period of fourteen days from the date of recording of
the first information report under section 154, the officer in charge…shall,
within three days of the expiration of such period, forward to the Magistrate
through the Public Prosecutor, an interim report…"*.

**KB9**: `4_Punjab-Police-Rules-III_…_c1542` and `_c1543`. **Verified directly,
because this is exactly where Module 19b caught a hallucination:** c1542's text
literally reads *"Rule 25.31. Inquests. — (1) An officer in charge of police
station shall, upon receipt of information of the sudden or unnatural death of
any person…immediately send information to the nearest magistrate authorized to
hold inquests and shall proceed to the place where the body is and hold an
investigation **in the manner prescribed by Section 174, Code of Criminal
Procedure**."* The answer's "Section 174" is grounded in the retrieved text. It
is **not** the CrPC s.174 chunk itself (`…_f9908363_c763`) — see §5.

---

## 5. Gold comparison

**KB8 — the legal half matches gold exactly.** Gold: *"dafa 173 … chaudah din …
officer in charge us muddat ke khatam hone ke teen din ke andar magistrate ko
(public prosecutor ke zariye) ek interim report bhejne ka paband hai."*
Answer: Section 173, 14 days from the FIR, interim report to the court within
three days of that period expiring, through the interim-report mechanism. Same
section, same two time limits, same duty. The data half is a **mismatch**: gold
says our data records the challan (26 cases) and the recorded outcome but has no
interim-report field; the answer says only that the documents contain no
case-tracking data. The RAG sub-agent has no access to the case database, so
this half is out of its reach by construction, not a retrieval failure.

One artefact worth recording: the citation-consistency checker appended *"A
cited claim ([Document 5]) could not be confirmed against its source: Claim
cites figure(s)/identifier(s) not found in its source text: 173."* It is
correct — c757's text genuinely never contains the string "173"; the number is
in c752, a different document in the same answer. The claim is sound, the
per-document attribution is not.

**KB9 — the legal duty matches, the citation route differs.** Gold cites CrPC
s.174 directly. The answer reaches the same duty through Punjab Police Rules
25.31/25.35, which cross-reference s.174 by name, and adds the inquest-report
forms. CrPC c763 itself was **not** retrieved: the model's hypothesis named
*section 176* (the Magistrate's inquiry into cause of death) rather than *174*
(the police's), and s.176's vocabulary pulls its own neighbourhood. Measured
directly: with the model's own s.176 phrasing c763 sits at rank 27–31 and a
wider pool does not rescue it (tested at pool sizes 30/45/60 — the cross-encoder
prefers other chunks either way); with a s.174 phrasing it is rank 1. **Honest
verdict: the answer is right, by a route gold did not take, and the CrPC chunk
gold cites is still not being retrieved.** See §8.

**KB3 — retrieval fixed, conclusion still not gold's.** Gold: *"The law expects
separation."* The answer says the law *"does not explicitly require the same
officer to both register and investigate…but it also does not mandate that these
roles be separate"*, then quotes the Article 18 investigation-staff language and
calls the separation *"implied"*. Gold reaches its conclusion by inference from
the same three elements; the generator declines to. The data half (68 of 74
pairs, 92%) is absent for the same structural reason as KB8's. **This is no
longer a retrieval defect** — the provision, its Investigation Wing proviso and
the non-interference clause are all on the page the generator was given.

**Bucket score, measured by this module rather than inherited:** the KB bucket
went from **3 of 8 answering at all** (KB1, KB4, KB6) to **7 of 8** — KB3, KB8
and KB9 all stopped abstaining, and KB2 stopped tripping the verifier. On gold
match rather than on abstention, **KB8 is a clear pass and KB9 a substantive
pass by a different citation route**; KB3 is improved but not a match.

**A correction to the plan's framing:** `GOLD_QA_REMAINING_FIXES_PLAN.md` records
Module 19b as taking the KB bucket to 5/8. That does **not** reproduce on this
machine today. At the merge-base, with the evaluator fix present, KB2 and KB5
both fail with the verifier's *"could not be verified as grounded"* — before any
change of mine. Whatever the 5/8 was measured against, it is not the current
main. The plan's Module 19b section is left alone (not this module's rows), but
the number should not be reused without re-deriving it.

---

## 6. Non-gold paraphrase

Three non-gold questions, each run on **both** the merge-base and this branch,
same backend, same corpus, same day.

### P1 — "case diary" duty, Roman-Urdu, no statutory vocabulary in the question

> Tafteesh ke doran police ko roz-ba-roz kya likhna zaroori hota hai, aur kya
> qanoon is ke liye koi tay-shuda tareeqa muqarrar karta hai — aur kya hamara
> system yeh kahin darj karta hai?

**Before (merge-base):** *"The generated answer could not be verified as
grounded in the retrieved documents."* — the verifier rejected it. (The
evaluator had passed on Rule 25.53 chunks, so this was a generation failure on
top of a thin retrieval.)

**After:** answers correctly, and cites both books:

> During an investigation, the police are required to maintain a **case diary**
> daily, recording: the steps taken by the police, circumstances ascertained
> through the investigation, the time when information was received, the
> investigation began/closed, and places visited. This is mandated by **Section
> 172(i) of the Code of Criminal Procedure** [Document 2, 3], which is
> implemented via **Police Rule 25.53** [Document 1]…

Statute hypotheses generated: *"Punjab Police Rules 1934 case diaries and duties
of station staff…"* and *"Code of Criminal Procedure 1898 sections on
investigation and report: police officer in charge shall maintain a daily record
of investigation progress…"* — two books, as designed. Grounding verified by id,
not by the citation: `…_f9908363_c748` reads *"172. Diary of proceedings in
investigation. (1) Every police-officer making an investigation under this
Chapter shall day by day enter his proceedings in the investigation in a
diary…"*. **This one is a genuine before→after capability gain, and it is not
one of the 32 gold questions.**

### P2 / P3 — 24-hour production before a magistrate (Roman-Urdu and Urdu script)

> Agar police kisi shakhs ko bina warrant giraftar kare, to kya qanoon zaroori
> karta hai ke use kisi muqarrara waqt ke andar magistrate ke samne pesh kiya
> jaye — aur kya hamara record yeh darj karta hai?

> کیا قانون یہ طے کرتا ہے کہ گرفتار شخص کو کتنے وقت میں مجسٹریٹ کے سامنے پیش کیا
> جانا لازم ہے — اور کیا ہمارا ریکارڈ یہ ظاہر کرتا ہے؟

Both answer correctly **before and after**, naming CrPC s.61 and the twenty-four
hours. After the change the Urdu-script one retrieves
`…_f9908363_c281` — verified verbatim: *"61. Persons arrested not to be detained
more than twenty-four hours. No police-officer shall detain in custody a person
arrested without warrant … exceed twenty-four hours exclusive of the time
necessary for the journey from the place of arrest to the Magistrate's Court."*

**Honest reading:** these two were already within the pipeline's reach, so they
demonstrate **no new capability** — they are a useful negative control (a legal
question that worked before still works, in both scripts) but they are not
evidence for the fix. P1 is the paraphrase that carries that weight. Reported
this way rather than presenting three passes as three wins.


---

## 7. Regression guard

The five Module 19b fixed — KB1, KB2, KB4, KB5, KB6 — were all re-run, each
against this module's own merge-base baseline rather than an inherited claim.
Full table in §4.

- **KB1 — no change.** CrPC ss.154/155 before and after.
- **KB2 — improved, still not gold.** The verifier refusal is gone; the answer
  now names no statute where gold wants Qanun-e-Shahadat Arts 38/39 and CrPC
  s.162. It failed at baseline too, so this is not a regression introduced here.
- **KB4 — changed for the worse, and neither version matches gold.** Gold wants
  Punjab Police Rules 27.16 (case-property register, destroyed only three years
  after completion). Baseline reached register material (Register No. 1, the
  permanent register, sealing); after the change the answer is dominated by the
  Forensics guidelines. Cause identified: the two statute hypotheses for KB4
  were *"Punjab Police Rules 1934 case property and malkhana…"* (right) and
  *"Forensics guidelines handling and chain of custody…"* (wrong for this
  question), and `cross_rerank_multi()`'s max-over-queries merge let the
  forensics query — whose absolute cross-encoder scores run higher — take the
  final five. **Reported, not tuned away.** See §8.
- **KB5 — no change.** Verifier refusal before and after.
- **KB6 — no meaningful change.** Forensic handling guidance both times, with
  different chunks selected; neither run reaches gold's "unloaded, safety on, no
  live round" list.

No question that answered at baseline stopped answering.

---

## 8. New defects found, deliberately left unfixed

**(a) `chunk_fulltext` holds an orphaned re-ingestion of the CrPC PDF.** The
BM25 candidate pool returned chunks with ids like
`1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_0519abd8_c570` that **do not
exist in Chroma** (`get_by_ids` returns nothing for them). Postgres:

```
1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_f9908363 | 2577   <- current, in Chroma
1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_0519abd8 | 2243   <- orphan, not in Chroma
```

2,243 phantom rows from an earlier ingestion that was replaced in Chroma but
never deleted from `chunk_fulltext`. They carry real statute text, so answers
are not wrong, but they duplicate the CrPC in BM25's pool, compete for RRF
slots, and their ids resolve to nothing for provenance lookups. Several of them
reached the evaluator in the runs above. This is an index-hygiene fix
(`fulltext_index.delete_by_source` on re-ingest, plus a one-off cleanup), not a
retrieval-logic one — **it belongs in its own module.**

**(b) `cross_rerank_multi()` merges by max score across queries, and
cross-encoder scores are not comparable across queries.** This is what cost KB4
(§7) and what made a third statute hypothesis actively harmful (§2). A
rank-based fusion across the per-query reranked lists (RRF, as
`src/retrieval/reranker.py` already does for semantic-vs-BM25) would let each
phrasing contribute proportionally instead of letting whichever query produces
the largest absolute scores take the whole window. Not changed here because it
would invalidate every live measurement in this document and needs its own
before/after — **its own module.**

**(c) KB3's remaining gap is generation, not retrieval.** With Article 18's
Investigation Wing, investigation-staff and non-interference text all on the
page, the generator still declines to draw gold's conclusion. Any further work
on KB3 is a response-generation or evaluator question and is explicitly out of
this module's scope.

**(d) The KB questions' "and does our data show it?" half is unreachable from
the RAG sub-agent.** Every gold KB answer is compound: a statutory norm plus a
figure from the case database (68 of 74 pairs, 26 challans, 45 property entries,
8 PPC-302 FIRs). The RAG tool has no database access, so no amount of retrieval
work closes that half. It is a composition question — whether a KB-intent
question should fan out to an aggregate as well — and it is the single largest
remaining gap in this bucket. **Its own module.**
