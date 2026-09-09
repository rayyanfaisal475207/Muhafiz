# Module 82 — KB2 answers from the recording provision and never from the bar

**Branch:** `fix/kb-downstream-generation`
**Question:** KB2 (primary), with Module 65's Roman-Urdu paraphrase as the
positive control.
**Sibling result:** `MODULE85_RESULT.md` — KB4, Modules 85 and 86, same branch,
same attempt, same batches.

**Outcome, stated first because it is a negative one.** The root cause is
identified and grounded in chunk ids read out of the store. **The generation
change written for it was measured live, found to be a net regression, and
deleted.** `src/` on this branch is byte-identical to `main`. What ships is the
diagnosis, the harness that produced it, the tests that pin what was
established, and two new defects — including one that undermines the brief's
own premise: on this `main`, **gold's own wording no longer retrieves gold's own
provision, while a paraphrase of it does.**

This is the same posture Module 71 took toward its figure roster ("built,
measured, found harmful, and deleted") and Module 21 toward its investigation
("no fix belongs here"). Modules 35 and 43 set the precedent that a reported
mismatch beats a manufactured match.

**Date of every measurement below:** 2026-09-09, worktree
`D:/Rapids AI/muhafiz-kbgen`, backend on `:8031`, `muhafiz-postgres` (73 cases),
model server `https://discharge-fascism-richness.ngrok-free.dev` `/health`
returning `{"cuda":{"available":true,…},"models_loaded":{"embedder":true,
"reranker":true}}` before every batch.

**Chroma:** the **shared** store at
`D:/Rapids AI/Evidence Intelligence Platform/data/chroma_db`, opened
**read-only** — `documents_in_store: 7716` at every backend boot. Nothing was
written, embedded, re-indexed or deleted; `scripts/module82_window_probe.py`
only ever calls `get_by_ids()`.

**Reranker liveness asserted on every batch**, because with `RERANKER_URL`
unreachable `cross_rerank()` logs a warning and keeps the RRF order — a
degradation indistinguishable from a change in this module (Module 38 §4).
`POST /rerank → 200` throughout; `grep -ciE "cross-encoder rerank failed|
RERANKER_URL not configured"` = **0** on every log.

**`router.py`, `supervisor.py`, `evaluation/`, `xagg.py`,
`statute_hypothesis.py`, `src/retrieval/`, `src/pipeline/harness/tools/rag.py`,
`verifier.py`, `validation.py` and `prompts/verifier.txt` were NOT touched.**

---

## 1. Root cause

**The brief asks whether KB2's residue is the orphaned s.164 chunks, the
generation prompt, or both. Measured: both — and a third thing the brief did not
anticipate, which is that on this `main` gold's Art. 38 chunk is not retrieved
at all.**

### 1.1 The premise, re-derived rather than inherited

Module 65 measured Art. 38 (`2_qanun-e-shahadat-order-1984_pdf_3e604153_c176`)
and CrPC s.162(1)
(`1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_f9908363_c675`) reaching
KB2's window **3 of 3**, against a **private** Chroma copy on merge-base
`e412327`. Re-derived here on `main` @ `6cf89fb` against the **shared** store, it
does not reproduce for gold's own wording:

| KB2, 3 runs, `route=RAG` | Art. 38 `…3e604153_c176` | Art. 39 `…_c177` | s.162(1) `…f9908363_c675` |
|---|---|---|---|
| **gold's literal text** | **0 of 3** | 0 of 3 | **3 of 3** |
| **KB2Q**, Module 65's Roman-Urdu paraphrase | **3 of 3** | **3 of 3** | **3 of 3** |

Chunk ids read from `rag.py`'s own `chunk(s) to evaluator` line for exactly the
window each request occupied — never from a citation in an answer, which is
Module 30's recorded trap.

Not an infrastructure failure and not an ingestion gap:
`scripts/module82_window_probe.py` reads `…_c176` out of the shared store by id
and it is there verbatim — *"38. Confession to police officer not to be proved:
No confession made to a police officer shall be proved as against a person
accused of any offence."* Module 65's prompt work is also still firing:
hypothesis 1 on every run is *"Qanun-e-Shahadat Order 1984 section 118 and 120
confession and admission: confession made to a police officer shall not be
proved as against the accused"* — right book, right verb, wrong section number,
exactly the shape Module 65 documented. The book is selected; the provision
loses its window slot anyway, and only on gold's phrasing.

**`src/retrieval/` and `rag.py` are out of bounds for this module and this is
not repaired here.** Filed as **Module 97** (§8a). It also means Module 82's own
premise is only half available today — but **s.162(1) is in the window 3 of 3
and is enough for gold's conclusion on its own**, so there is still a real
generation defect to see, and §1.2 sees it.

### 1.2 What the generator reasons from — orphans, by id

KB2's window is **identical on all three runs**, five chunks:

| pos | chunk id | what it is |
|---|---|---|
| **1** | `1_1898_…_pdf_f9908363_c675` | **CrPC s.162(1)** — *"…nor shall any such statement or any record thereof … be used for any purpose … at any inquiry or trial…"* |
| 2 | `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c1521` | Rule 25.28, statements recorded **by a magistrate** under s.164, *"in order that it may be available as evidence at a later stage"* |
| **3** | `1_1898_…_pdf_0519abd8_c590` | **Module 37 orphan** — s.161 recording text. `get_by_ids()` returns **nothing** for it |
| 4 | `1_1898_…_pdf_f9908363_c1010` | supply of s.161/s.164 statements to the accused |
| **5** | `1_1898_…_pdf_0519abd8_c601` | **Module 37 orphan** |

**Two of the five chunks resolve to nothing in Chroma.** That is Module 37's
2,243 orphaned `chunk_fulltext` rows, confirmed live here by id rather than
inherited: they reach the window through BM25's candidate pool, which reads
Postgres, and they are absent from the vector store a reader would look them up
in.

**And they are what the answer is built on.** All three runs cite **Document 3**
first; one of the three cites nothing else:

> The system does keep a record of what a witness or an accused person says
> during a police interview. According to the rules, the police officer may
> reduce any statement made to him into writing and make a separate record of
> the statement **[Document 3]**. These records **can be used as evidence at a
> later stage**, and they are part of the investigation process **[Document 2]**.
> Therefore, **it is not a data gap; the system does maintain such records.**

The [Document N] → chunk mapping is not assumed. It is confirmed two ways: by
content (*"available as evidence at a later stage"* is `…_c1521`'s own phrase,
verbatim from the store; *"reduce … into writing and make a separate record"* is
the orphan's s.161 text), and structurally on KB4, whose answer calls the
computed data-half chunk **"Document 7"**, and that chunk is the 7th and last
entry in the logged window.

**Document 1 — s.162(1), the bar, at window position 1 — is cited on none of the
three runs.**

### 1.3 The defect, stated

Module 65 recorded KB2 stating s.162's bar correctly and then hedging into
*"which may be considered a limitation or data gap"*. On this `main` the failure
is one step earlier and worse: the bar is never quoted, and the answer concludes
the **opposite of gold on the facts** — that the system *does* maintain interview
records — while landing on gold's words *"not a data gap"* for the wrong reason.
Gold's point is that the records do not exist and that this is required by law;
the answer's point is that they exist.

**It is not a capability limit, and that is measured the same day, same build,
same corpus.** KB2Q — Roman-Urdu, sharing no wording with gold — reaches all
three provisions and draws gold's conclusion, **3 of 3, with no change to any
code**:

> The law does not allow a confession made to a police officer to be used as
> evidence against the accused in court. According to [Document 4], "No
> confession made to a police officer shall be proved as against a person
> accused of any offence." … **This is why statements or confessions made to
> the police are not preserved in the records as evidence.**

Two things separate the arms, and a prompt can only reach one of them: the
paraphrase's window contains Art. 38, and the paraphrase does not ask *"is that
a data gap?"* — a question whose easiest answer is a hedge about the data. §2 is
the attempt to reach the second one, and what happened when it was measured.

---

## 2. Change — none shipped, and the measurement that decided it

**`src/pipeline/harness/agents/semantic_search.py` on this branch is
byte-identical to `main`.** `git diff origin/main -- src/` is empty.

What was written, shipped to a live arm, and reverted:

| Written | Aimed at |
|---|---|
| **Rule 1 — SUBJECT.** *"ANSWER FROM THE DOCUMENTS THAT ARE ABOUT THE QUESTION'S OWN SUBJECT. These documents were retrieved by similarity, so some of them will be about a neighbouring subject… Cite a document only for what that document itself states: never carry a requirement, standard, figure or conclusion from one document onto a subject a different document is about."* | KB4 (§1.2 of `MODULE85_RESULT.md`) and Module 38's recorded rejection reason |
| **Rule 2 — CONCLUSION.** *"ANSWER THE QUESTION THAT WAS ASKED, AND DO NOT TAKE THE ANSWER BACK… Do not state such a rule and then call the same situation a possible limitation, shortcoming or data gap… An absence the law requires is a finding, not a gap."* | KB2 (§1.3) and Module 65's recorded hedge |
| **The Module 86 clause** in `_COMPOUND_ANSWER_RULE`: *"INCLUDING the totals and denominators it states and not only the breakdown beneath them."* | Module 86 |

Both rules were placed **before** the documents block, with the citation rule
left in the position it already had — Module 71's measured lesson about
prompt-tail proximity was respected, and is not the explanation for what
follows.

### 2.1 The live result, 3 runs per question per arm

| `route=RAG` throughout | shipped code | **with the rules** |
|---|---|---|
| **KB4** answered | **3 of 3** | **1 of 3** |
| **KB4P** (Roman-Urdu paraphrase) answered | **3 of 3** | **1 of 3** |
| KB2 answered | 3 of 3 | 2 of 2 (one run void, §4a) |
| KB2 reached gold's conclusion | 0 of 3 | **0 of 2** |
| KB2Q reached gold's conclusion | 3 of 3 | 3 of 3 |
| the **45** stated (Module 86) | 0 of 6 | **0 of 5** |
| Semantic-Search verifier rejections | **0** | **4** |

**Nothing improved and two questions halved.** All four new rejections are the
verifier correctly refusing a **compliance** claim no chunk supports:

```
The claims about Rule 27.16(1) and Rule 27.16(4) are not directly stated in
the cited chunks…
Multiple claims are attributed to cited chunks but are absent from their text.
```

and the mechanism is legible in the answers themselves. Where the shipped code
wrote *"the records do not show any entries that explicitly mention the marking,
packaging, or chain of custody"*, the rules' arm wrote *"It is clear from the
records that the police have followed the procedures for marking, packaging, and
maintaining the chain of custody"* — an over-reach that rule 2's *"state your
conclusion plainly and stand behind it"* invited and that no document supports.

**The rules made the exact defect they were written to prevent more likely.**
Rule 1 was aimed at over-attribution; rule 2 produced more of it. That is the
finding, and it is why the change is deleted rather than tuned: a second
iteration would be guessing against a mechanism this measurement has not
isolated, and the brief's own instruction is that a verifier which passes
everything is worse than one that is too strict — the corollary being that a
prompt which manufactures claims for it to catch is worse than no prompt change.

### 2.2 What was considered and not done

| Direction | Why not |
|---|---|
| **Deleting Module 37's 2,243 orphaned `chunk_fulltext` rows** | Very likely part of KB2's real fix — 2 of its 5 window slots are orphans and the answer's lead citation is one of them, 3 of 3. The store is **shared** and this module is read-only on it; the brief says to file rather than delete. Filed with live evidence in §8b. |
| **Any change to `verifier.py` / `validation.py` / `prompts/verifier.txt`** | The brief's hard constraint and Modules 17/25/40/61/71's shared posture. KB4's recorded rejections are over-attribution to a RAG chunk, which Module 61 deliberately gave no licence to; §2.1 is direct evidence that loosening there would have served exactly the claims the judge refused. |
| **A second prompt iteration** | §2.1. The first arm produced a *worse* failure of the same kind the rules targeted; nothing in the data says which wording would not. |
| **A worked example in the prompt** | Module 65 measured a worked example making retrieval *worse*, and it would also put gold's shape into the prompt. |
| **A per-document figure roster** | Built and deleted by Module 71 at a measured cost of 7-of-8 G6 rejections. |
| **A retry loop around a rejection** | Closed by Modules 61 and 71: it hides the non-determinism the measurement exists to expose. |

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" -m pytest \
  tests/test_module82_generation_findings.py tests/test_harness_agent_semantic_search.py \
  tests/test_verifier.py tests/test_validation.py tests/test_kb_statute_retrieval.py \
  tests/test_harness_agent_meta_analysis.py tests/test_pipeline.py \
  tests/test_citation_consistency.py -p no:randomly
```

**386 passed, 0 failed.** Six are new, in
`tests/test_module82_generation_findings.py`. No full `pytest -q` run — it
empties `muhafiz_entity_descriptions`.

Because no production behaviour changed, the new tests pin **findings**, and two
of them are wording locks in the *negative* direction — the useful direction when
the result is that a change did not work:

| Test | Pins |
|---|---|
| `test_module82_the_measured_generation_rules_are_not_in_the_prompt` | The two rules are absent, with §2.1's table in the docstring, so re-adding them without redoing the measurement fails here |
| `test_module86_the_compound_rule_is_module_39s_unchanged` | The denominator clause is absent and Module 39's rule survives intact |
| `test_module82_the_documents_block_is_last_in_the_prompt` | Module 71's prompt-tail lesson, and a pointer to §2 for the next person who wants to add a rule here |
| `test_module82_the_fabricated_citation_check_still_fires_where_it_governs` | The verifier's one deterministic anti-fabrication pre-check, unchanged and still firing on the shape it governs |
| `test_module82_no_deterministic_pre_check_guards_the_legal_kb_path` | **A finding, not a guard** — see below |
| `test_module82_a_rejected_answer_is_still_never_served` | `SubAgentResult`'s [PRESERVE] rule, asserted marker-by-marker against the exact string the live control injects |

**One of these exists because a first draft was wrong and the code said so.** It
asserted that a fabricated FIR id in a KB4-shaped answer is caught by a
deterministic pre-check. **It is not, and the test failed against the real
code.** On the global legal-KB corpus no chunk carries a `case_id`, so
`_check_fabricated_case_ids()` returns early by design (its own comment records
the KB1 false positive that put the early return there); `_check_hedging()` never
fires because RAG computes no per-chunk confidence
(`semantic_search.py::_chunk_to_verifier_dict`'s own note); and
`_check_refusal()`/`_check_no_citation()` catch a refusal or an uncited answer,
which a fluent fabrication is not. **On this path the hallucination guard IS the
LLM judge.** The test was rewritten to assert that emptiness rather than paper
over it, and it is why §4c is a live control and not a mock.

---

## 4. Live verification

`/api/chat` on `:8031`, `admin@example.com`, platform-admin, All Cases.
`route=RAG` on every KB2 and KB4 run in both arms. Chunk ids from `rag.py`'s own
log line; chunk **text** from the store by id.

### 4a. Run integrity — two failures fired, and both are disclosed

Neither is visible in `status`, and both would have silently corrupted the
numbers:

- **Cutover fallback (Module 54's trap), 1 per arm.** The cutover classifier's
  provider returned `429 RESOURCE_EXHAUSTED` and `src.main` fell back to
  `orchestrator.py` — **a different pipeline answered than the one being
  measured**. The contaminated runs are identifiable by their output: the
  affected KB2 run retrieved FIR narratives (`fir-88-26`, `fir-245-26`, zimni
  entries) instead of statutes. **KB2/with-rules/run 1 and KB9/shipped/run 2 are
  dropped**, which is why KB2's with-rules arm is n=2 and KB9's shipped arm is
  n=1. `scripts/module82_recheck_integrity.py` and the runner's new
  `cutover_fallbacks` field make this countable rather than a matter of noticing.
- **Postgres restarted mid-batch.** `muhafiz-postgres` went to *"Up less than a
  second"* and every request in that window returned HTTP 500 with
  `asyncpg … ConnectionRefusedError`. The whole affected batch was discarded and
  re-run from a fresh backend; the with-rules regression batch could not be
  cleanly attributed to a single process afterwards and is **discarded entirely**
  rather than reported (§7).

Module 81's corrected quota pattern returns **0** on the shipped-code log and
**0** on the with-rules log apart from the two cutover lines above.

### 4b. KB2, both arms

| | shipped code | with the rules |
|---|---|---|
| runs | 3 | 2 valid |
| answered | 3 of 3 | 2 of 2 |
| Art. 38 in window | 0 of 3 | 0 of 2 |
| s.162(1) in window | 3 of 3 | 2 of 2 |
| Module 37 orphans in window | 2 of 5 chunks, every run | 2 of 5 chunks, every run |
| reaches gold's conclusion | **0 of 3** | **0 of 2** |

With the rules, run 2 cites Document 1 (s.162(1)) for the first time — and
attributes the *orphan's* s.161 recording text to it, then concludes *"the
absence of such records is not a data gap, but rather a situation that is
governed by the legal framework, which allows for the creation of written records
of statements"*. The citation moved; the reasoning did not.

### 4c. The forced-hallucination control — the brief's non-optional proof

Modelled on `scripts/module71_forced_hallucination_control.py`, which the brief
names, and moved to the path this module is about: `scripts/module82_forced_
hallucination_control.py` starts the ordinary backend with **one** boundary
replaced — `semantic_search.call_llm` returns a fixed fabricated answer instead
of generating one. Everything downstream is real: real retrieval, real reranker,
real relevance evaluator, the **real** `verify_grounding()`, the real ABSTAINED
contract. Nothing in `src/` is modified; the patch lives in the script.

The fabrication is plausible rather than absurd, because a verifier that only
catches nonsense proves nothing — an invented rule number (`27.41(3)`), a
retention period attached to the wrong subject (*"destroyed exactly seven years
after the register is closed"*), an invented identifier (`FIR 512/26`) and an
unsupported conclusion (*"fully compliant"*).

Run on the **shipped (reverted) code**, 3 runs, KB4's literal gold question:

| | result |
|---|---|
| Control fired | **3 of 3** |
| Verifier rejected the fabrication | **3 of 3** |
| `status` | `error` 3 of 3 |
| Fabricated markers in the served text | **0 of 3** (`27.41`, `seven years`, `512/26`, `fully compliant`) |
| Elapsed | 55.5 – 85.9 s |

The judge's own words, unprompted:

```
Semantic Search: verifier rejected answer: The claims about rule 27.41(3) and
the audit of FIR 512/26 are not supported by any of the cited chunks.
Semantic Search: verifier rejected answer: The claims about rule 27.41(3) of
the Punjab Police Rules and the compliance of records are not supported by any
of the provided chunks.
```

**And there is a second, unforced proof in the same session**, which is stronger
because nothing was injected: the four rejections in §2.1 are the real verifier
refusing real over-claims that this module's own prompt produced, and the
shipped-code regression bucket carries a fifth — CR3 run 2's *"The claim that
fir-64-26 does not appear in the walk-in-complaint linkage list is unsupported,
as Document 3 does not mention fir-64-26"*, a **fabricated negative** in exactly
the direction Module 61's exhaustive-listing rule refuses to rescue. Modules 17,
25, 40, 61 and 71's guarantee holds.

---

## 5. Gold comparison

**Gold:** *No, that's by design, not a gap.* QSO Art. 38 (a confession to a
police officer cannot be proved against the accused), Art. 39 (extends it to
custody unless in a magistrate's immediate presence), CrPC s.162 (bars a
police-recorded statement at trial); because none of it is legally usable while
in police hands, the witness records hold identity and contact only.

Judged on facts and ideas in the answer's own words, per the brief's standard —
including its rule that correctly stating the data lacks something, where gold
agrees, is a PASS.

| Gold element | shipped code, 3 runs |
|---|---|
| "No — by design, not a gap" | **0 of 3** — the answer says "not a data gap" but for the opposite reason: that the records *do* exist |
| Art. 38 / Art. 39, confession not provable | **0 of 3** — not in the window (§1.1) |
| s.162 bars a police-recorded statement at trial | **0 of 3** — the chunk is in the window at position 1 on every run and is never used for this |
| Witness records hold identity and contact only | 0 of 3 — Module 39's data half, no plan for KB2 |

**KB2 is a fail, on both arms, and this module did not move it.** The shape the
brief describes — *correctly stating the data does not contain something, where
gold agrees* — is not what the answer produces: it asserts the data **does**
contain the thing gold says is absent.

**Not tuned toward gold.** No wording was changed to match gold; the only
production edit attempted is in §2, it named no statute, provision, conclusion or
figure, it was measured, and it was deleted.

---

## 6. Non-gold paraphrase

**KB2Q**, Module 65's Roman-Urdu paraphrase, verbatim — no gold vocabulary, and
run 3× in both arms.

> *Kya qanoon ijazat deta hai ke mulzim ne police ke saamne jo iqbal-e-jurm kiya
> ho use adalat mein saboot ke tor par pesh kiya jaye — aur kya isi wajah se
> hamare record mein bayanat mehfooz nahin hote?*

| | shipped | with the rules |
|---|---|---|
| answered | 3 of 3 | 3 of 3 |
| Art. 38 `…c176` in window | **3 of 3** | **3 of 3** |
| Art. 39 `…c177` in window | **3 of 3** | **3 of 3** |
| s.162(1) `…c675` in window | 3 of 3 | 3 of 3 |
| reaches gold's conclusion | **3 of 3** | **3 of 3** |

**This is the measurement that carries the module's weight, and it points at
retrieval, not generation.** The same pipeline, same corpus, same session, same
day answers gold's question correctly when it is asked in different words — and
the difference in the window is Art. 38 and Art. 39, present for the paraphrase
and absent for gold on every run. Module 65 read this split as generation; with
the retrieval half re-measured on the shared store it reads more naturally as
**Module 97**.

The with-rules arm's KB2Q answers are more repetitive and carry a new validation
caveat (*"Claim cites figure(s)/identifier(s) not found in its source text: 1984,
38"*), which is a small cost with no matching benefit — consistent with §2.1.

---

## 7. Regression guard

Run on the **shipped code**, which is `main`'s code, so this is a
same-session baseline for whoever takes Module 97 rather than a before/after.
2 runs each; 17 valid rows after §4a's one drop.

| Question | Route | Runs | Answered | Notes |
|---|---|---|---|---|
| **KB1** | RAG | 2 | **2 of 2** | CrPC s.154, and correctly says the documents cannot confirm practice |
| **KB5** | RAG | 2 | **2 of 2** | Anti-Rape Act s.3(2)(d), female officer present — gold's substance |
| **KB6** | RAG | 2 | **2 of 2** | Forensic packaging/sealing of sharp weapons |
| **KB8** | RAG | 2 | **2 of 2** | CrPC s.172 case diary + s.173 |
| **KB9** | XAGG | 1 | **1 of 1** | Module 39's data half intact; run 2 void (§4a) |
| **CR3** | XAGG | 2 | **2 of 2** | Module 61's 7-of-8 holds. Run 2's synthesis was verifier-rejected for a **fabricated negative** and Module 71's `PARTIAL` fallback served the sub-answers — `status=done` |
| **G1** | XAGG | 2 | **2 of 2** | Module 71 holds: 92 accused / 17 with ages / mean 31.5, denominators correctly attached |
| **G6** | XAGG | 2 | **2 of 2** | District concentration (فیصل آباد 19, لاہور 18, راولپنڈی 10). Run 1's synthesis hit Module 71 §8's *"cites no [Document N] source at all"*; the fallback served the sub-answers |
| **M2** | XAGG | 2 | **2 of 2** | 15 general-purpose stations 7→39 vs 2 specialised 3→5 |

**17 of 17 answered.** Module 39's data halves survive (KB9, and KB4's in
`MODULE85_RESULT.md`). Two Meta-Analysis verifier rejections occurred and **both
were absorbed by Module 71's `PARTIAL` fallback** — the second and third
unforced live activations of that fallback on record, after Module 71's own §4d.

**The baseline is not regressed because nothing shipped.** `git diff origin/main
-- src/` is empty, so the 19 questions passing on all three passes of
`evaluation/gold32_pass{1,2,3}_results.json` cannot have moved. **The with-rules
regression batch is discarded entirely** rather than reported: the backend was
restarted during it and no row's log slice can be attributed to one process
(§4a). Had it shipped, that batch would have had to be re-run in full.

---

## 8. New defects found

### 8a. Module 97 — gold's own wording loses the provision its paraphrase retrieves

**The largest finding here, and a retrieval one, which is why it is filed rather
than fixed.** On `main` @ `6cf89fb`, shared Chroma, reranker live, 3 runs each:

| | gold's literal wording | its non-gold paraphrase |
|---|---|---|
| **KB2** — QSO Art. 38 `…3e604153_c176` | **0 of 3** | **3 of 3** |
| **KB2** — QSO Art. 39 `…_c177` | 0 of 3 | **3 of 3** |
| **KB4** — rule 27.16(1) `…68bb5d0d_c2209` | **0 of 3** | **3 of 3** |

Module 65 measured KB2's Art. 38 at 3 of 3 and Module 38 measured KB4's `…_c2209`
at 3 of 3 — both against **private** Chroma copies on earlier merge-bases.
Neither reproduces here for the gold question; both reproduce for a paraphrase,
same build, same store, same session. So this is not "the fix regressed" in
general — **it is gold's own phrasing that loses the slot, on two independent
questions, in the same direction.**

Both prompts' book selection still works. What varies is the section number and
surrounding wording the model attaches, and Module 65 §7a already measured that
variation alone flipping a KB4 outcome on an unchanged prompt. The window is also
not fully deterministic: §4c's control run 3 — same question, same code — *did*
have `…_c2209` at position 2.

Whoever takes this owns `src/retrieval/` and/or `prompts/statute_hypothesis.txt`,
both out of bounds here. Two probes exist: `scripts/module65_probe.py` (rank by
query) and `scripts/module82_window_probe.py` (text by id, read-only). Module 65
§1.4 — one off-target hypothesis pulls the whole window — is the place to start.

**Related but not the same as Module 92**, which landed on `main` while this
module was running and which measures the *router* losing a question's route on
a paraphrase. This is the mirror image and one layer down: the route is
identical (`RAG` on all twelve runs), and it is the **retrieval window** that
differs — the paraphrase keeps gold's chunk and gold's own wording loses it.
Module 92's conclusion that "a capability is reachable only from a narrow
neighbourhood of the gold wording" now has a fifth sighting, in a component it
did not examine. Whoever takes 92 should read this as evidence the problem is
not confined to `router.py`.

### 8b. Module 37 is load-bearing on KB2, confirmed live by id

Module 65 §8c asked for Module 37 to be re-prioritised. This confirms it on the
**shared** store rather than a copy, and raises it: **2 of KB2's 5 window chunks
(`…_0519abd8_c590`, `…_c601`) return nothing from `get_by_ids()`**, they are in
the window 3 of 3, and **the answer's lead citation is one of them, 3 of 3.**

**This is very likely part of KB2's real fix and it is deliberately not done
here.** The rows live in Postgres `chunk_fulltext`, shared with two other live
tracks and with the running platform; deleting 2,243 rows to improve one
question's answer is not a change this module gets to make on its own judgement.
Module 37's own filed remedy — `fulltext_index.delete_by_source()` on re-ingest,
a one-off cleanup, and a `chunk_fulltext`-vs-Chroma count check on the admin
surface — is unchanged and now has live evidence behind it.

### 8c. `verify_grounding()` has no deterministic guard on the legal-KB path

Pinned by `test_module82_no_deterministic_pre_check_guards_the_legal_kb_path` and
explained in §3. On the global legal-KB corpus all four deterministic pre-checks
are inert and the LLM judge is the only guard. Not a regression and nothing here
made it worse, but no module has written it down, and it means every future "the
verifier will catch it" argument about a KB question is an argument about a
sampled verdict. Module 61's posture — a deterministic post-pass *and* a prompt
rule — is the template if someone closes it.

### 8d. Module 71's G6 citation defect fired unforced, and its fallback held

`Meta-Analysis: verifier rejected synthesized answer: Answer is substantial …
but cites no [Document N] source at all` — once, on G6, on shipped code. Module
71 §8 filed exactly this and it is still live. The run still returned
`status=done` carrying gold's district concentration, because Module 71's
`PARTIAL` fallback served the sub-answers. Reported as a live unforced activation
of that fallback, alongside CR3 run 2's (§7).

### 8e. Module 96 was filed for KB2 while this module was running, and §1 answers two of its three branches

`main` gained **Module 96 — "KB2, the last question with no module"** during
this module's live batches. It asks which of three things is true, *"with
evidence, and only then decide what to change"*:

1. **"Our answer is wrong."** — **Confirmed, and specifically.** §1.2: the
   answer asserts the system *does* maintain interview records, built on a
   Module 37 orphan it cites first on 3 of 3 runs, with s.162(1) sitting uncited
   at window position 1. It does not "call the absence a data gap"; it denies the
   absence.
2. **"Gold is wrong."** — **Not supported.** §1.1 confirms all three of gold's
   provisions exist in the corpus by id, and §6 shows the pipeline reproducing
   gold's conclusion from them, 3 of 3, for a paraphrase. Gold's law half is
   reachable and correct. (Gold's *schema* claim — witness records hold identity
   and contact only — was not checked here and remains Module 96's to verify.)
3. **"The judge cannot score a correct negative."** — **Not reached, and not
   ruled out.** The answers measured here never state the correct negative, so
   there was nothing for the judge to mis-score. That branch stays open and needs
   an answer that gets the law right first.

Module 96 should also be read against **Module 97** (§8a): KB2's law half is
0 of 3 on gold's own wording and 3 of 3 on a paraphrase, so a KB2 fix that does
not start at retrieval will be working with two of gold's three provisions
missing from the window.

### 8f. Module 71 §8 filed a different defect under the number "Module 82"

`GOLD_QA_REMAINING_FIXES_PLAN.md` line 3756 files the G6 "cites no [Document N]"
defect as **Module 82**, which is this module's number. §8d is that defect,
still live. Flagged rather than edited — it is Module 71's row, not this
module's — but the number needs reassigning by whoever owns it. 82 is KB2's in
the tracker's own §-list, and 88–92 have all been taken on `main` since (CR2,
KB1, M1, CP6, and the router's paraphrase generalisation), which is why this
module's own new defect is **97** and not 88 as first drafted.

---

## Artefacts

- `docs/gold-qa-wave2-results/module82_runs.json` — all 23 valid primary rows (KB2, KB4, KB2Q, KB4P), both arms, with chunk ids, statute hypotheses, verifier reasons, data-half firings and cutover flags.
- `docs/gold-qa-wave2-results/module82_regression.json` — the 17 shipped-code regression rows.
- `docs/gold-qa-wave2-results/module82_control.json` — §4c's forced-hallucination control, 3 runs.
- `scripts/module82_kb_runs.py` · `module82_window_probe.py` · `module82_forced_hallucination_control.py` · `module82_control_runs.py` · `module82_recheck_integrity.py`.
