# Module 65 — KB2's gold rests on statutes no run ever retrieved

**Branch:** `fix/kb2-retrieval-qanun-shahadat`
**Found by:** Module 52 (0 of 6 live runs, both arms). This is the diagnosis Module 48 records as missing for KB2.
**Date of every measurement below:** 2026-09-09, worktree `D:/Rapids AI/muhafiz-m65`,
backend on `:8024`, `muhafiz-postgres` healthy (73 cases), model-server tunnel
`https://discharge-fascism-richness.ngrok-free.dev` `/health` = 200 and returning real
1024-dim vectors.

**Chroma:** this module ran against a **private copy** of the vector store at
`D:/Rapids AI/muhafiz-m65/data/chroma_db` (`muhafiz_kb` 7,716, confirmed at every
backend boot). The shared
`D:/Rapids AI/Evidence Intelligence Platform/data/chroma_db` was never read from
and never written to. Nothing was re-embedded or re-indexed — as in Module 30,
the corpus turned out not to need it.

**Quota:** `grep -ciE "rate limit|RESOURCE_EXHAUSTED|429|quota|UNAVAILABLE|503"`
returns 1 across the backend logs, and that single line is a **false positive** —
it matches the substring `503` inside an ephemeral port number in
`INFO: 127.0.0.1:50358 - "POST /api/chat HTTP/1.1" 200 OK`. Zero real provider
failures in any run below. (Filed as a note for Module 54's canonical pattern, §8d.)

**`prompts/evaluator.txt`, `xagg.py`, `meta_analysis.py`, `verifier.py`,
`validation.py`, `supervisor.py`, `router.py`, `xnetwork.py` and `evaluation/`
were NOT touched.**

---

## 1. Root cause

**The brief's first branch is the right one, and the probe settled it before any
code was written: the chunks are in the corpus, and nothing the pipeline
generated ever reached them.** This is not an ingestion gap.

### 1.1 The chunks exist — established by chunk id, from the store

`scripts/module65_probe.py`, run against the private Chroma. All six present,
verbatim text confirmed by reading each id directly — never inferred from a
citation in an answer, which is Module 30's recorded trap.

| gold provision | chunk id | text (verbatim, abridged) |
|---|---|---|
| **QSO Art. 38** | `2_qanun-e-shahadat-order-1984_pdf_3e604153_c176` | *"38. Confession to police officer not to be proved: No confession made to a police officer shall be proved as against a person accused of any offence."* |
| **QSO Art. 39** | `…_3e604153_c177` | *"39. Confession by accused while in custody of police not to be proved against him: Subject to Article 10, no confession made by any person whilst he is in the custody of a police officer, unless it be made in the immediate presence of a Magistrate, shall be proved as against person."* |
| QSO Art. 39 Explanation | `…_3e604153_c178` | *"Explanation: In this Article, 'Magistrate' does not include the head of a village…"* |
| **CrPC s.162 heading** | `1_1898_Code_of_Criminal_Procedure_(Pakistan)_pdf_f9908363_c673` | *"162. Statements to police not to be signed; use of such statements in evidence."* |
| **CrPC s.162(1)** | `…_f9908363_c675` | *"(1) No statement made by any person to a police-officer in the course of an investigation under this Chapter shall, if reduced into writing, be signed by the person making it; nor shall any such statement or any record thereof, whether in a police-diary or otherwise … be used for any purpose (save as hereinafter provided) at any inquiry or trial …"* |
| CrPC s.162 proviso | `…_f9908363_c677` | *"Provided that, when any witness is called for the prosecution … the Court shall on the request of the accused, refer to such writing …"* |

The Qanun-e-Shahadat is 474 chunks in this store and the CrPC 2,577.
**Nothing needed ingesting.**

### 1.2 No query the pipeline generated reached them

Same probe, `{"is_global": True}` scope, top-30 per query — the table shape
Module 30 used:

| query | QSO Art. 38 | QSO Art. 39 | CrPC s.162(1) |
|---|---|---|---|
| the gold question itself | absent | absent | **rank 2** |
| its English rendering (Module 52) | absent | absent | **rank 2** |
| live statute hypothesis 1 (*"Punjab Police Rules 1934 registers and records: entries in the case diary shall contain the statements of witnesses and accused persons…"*) | absent | absent | absent |
| live statute hypothesis 2 (*"CrPC 1898 section 161 examination of witness and accused: the officer in charge shall record the statement…"*) | absent | absent | absent |
| CONTROL — Art. 38 in its own words | **rank 1** | rank 3 | absent |
| CONTROL — Art. 39 in its own words | rank 18 | **rank 5** | absent |
| CONTROL — s.162 in its own words | rank 23 | absent | **rank 1** |
| CONTROL — statute NAME only, *"Qanun-e-Shahadat Order 1984 Article 38"* | **absent** | absent | absent |

Module 30's third row reproduces exactly: **a bare statute name retrieves
nothing.** The provision's own wording is what matches.

**The mechanism.** Both hypotheses read *"why doesn't our system keep a record of
what a witness said?"* as a **record-keeping** question and spent themselves on
the Punjab Police Rules' case diary and CrPC s.161. `prompts/statute_hypothesis.txt`
gave the model three book-selection branches — a STEP (CrPC), WHO inside the
force (Police Order), WHAT IS WRITTEN DOWN (Punjab Police Rules) — and a question
about whether material may be **used** fell into the third. There was no evidence
branch, and the Qanun-e-Shahadat's map entry read only *"evidence: what may be
proved and by whom, confessions, witness competence and testimony"*.

### 1.3 The lexical half, measured

Book selection alone is not enough. The model's first evidence-shaped attempt
phrased its query as *"statements made to police … may not be used as evidence"*,
which is the wrong register. Ranks at top-200:

| query phrasing | Art. 38 | Art. 39 |
|---|---|---|
| *"…may not be **used as evidence** against the accused"* | 88 | 67 |
| *"…**shall be proved** as against a person accused"* | **3** | **2** |

The Qanun-e-Shahadat's verb is *proved*, and the noun it files this material
under is *confession*, not *statement*. A hypothesis saying *"statements made to
police officers shall not be proved"* put Art. 38 at rank 17 and lost Art. 39
entirely; the same hypothesis saying *"confession made to a police officer"* put
them at ranks 3 and 6.

### 1.4 One query is not enough — measured live, not assumed

An intermediate version of the fix reached Art. 38 at **rank 3** on its own query
and still retrieved **0 of 2** gold chunks live. Its second hypothesis was the
Punjab Police Rules' case diary, and that one query pulled the whole reranked
window back to case-diary material; under RRF fusion a chunk with a single vote
at rank 3 loses to chunks with two. **Both** hypotheses have to point at the
evidence neighbourhood, which is why rule 4c had to become prohibitive ("spend NO
query on the recording or register provision") rather than advisory ("spend at
least one on…"). The advisory wording was written, shipped to a live arm and
measured to fail; that is recorded here rather than deleted.

---

## 2. Change

| File | Why here |
|---|---|
| `prompts/statute_hypothesis.txt` | The whole behaviour change: one new book-selection branch (WHETHER SOMETHING MAY BE USED), one new rule (4c), and the Qanun-e-Shahadat's map entry enlarged to describe what it actually covers. |
| `src/pipeline/statute_hypothesis.py` | Comment only, no behaviour change. `DEFAULT_HYPOTHESES`' n=3 rejection cited a mechanism Module 38 removed, and Module 38 asked its successor to rewrite it. Re-measured and rewritten (§8a). |
| `tests/test_kb_statute_retrieval.py` | Six new tests pinning the prompt mechanism (§3). |
| `scripts/module65_probe.py` (new) | The §1.1/§1.2 probe: existence by chunk id, then reachability by rank. |
| `scripts/module65_kb_runs.py` (new) | Live KB1–KB9 runner, adapted from `scripts/module52_kb_runs.py`, with two non-gold KB2 paraphrases. |
| `scripts/module65_hypothesis_control.py` (new) | The before/after equality control on all eight KB questions (§7). |
| `scripts/module65_summarise.py` (new) | Per-run gold-chunk presence, by id. |

**Nothing in `src/retrieval/` or `src/pipeline/harness/tools/rag.py` was
changed.** The probe showed the retrieval machinery Modules 30 and 38 built is
perfectly capable of reaching these chunks; what it was never given was a query
that named them. `DEFAULT_HYPOTHESES` stays at **2**.

### What was probed and rejected

| Direction | Result |
|---|---|
| **`DEFAULT_HYPOTHESES = 3`** | **Rejected, measured.** Module 38 showed a wrong third slot is no longer *harmful* under RRF. It is still not *useful* here. On the unfixed prompt the third slot produced *"Qanun-e-Shahadat Order 1984 section 14 statement of witness"* — right book, wrong subject, retrieving none of gold's chunks. On the fixed prompt the first two hypotheses were byte-identical to n=2 and the third was a redundant third phrasing, again retrieving nothing. |
| **Enlarging the CrPC's corpus-map entry** with s.162's subject | **Rejected on a measured regression.** It did put the CrPC s.162 query in slot 2 and fixed KB2 live (3/3) — and it **hijacked KB5's first hypothesis** away from the Anti-Rape Act to *"CrPC sections 174/175 inquest and cause of death"*. KB5 went 5/5 answering to 1/3, at ~45 s to 97–172 s and 3–5 evaluator rounds. Reverted. The same material lives in the WHETHER branch instead, which fires on the question's shape rather than on the book. |
| A worked example of the evidence shape in the prompt's few-shot block | **Rejected — it made retrieval WORSE.** With the example the model produced a CrPC-first pair reaching Art. 38 only at rank 12; with the rules alone and no example, Art. 38 came back at rank 3 and Art. 39 at rank 6. Removing it also keeps every example in the prompt away from gold's wording. |
| An advisory rule 4c (*"spend at least one query on…"*) | **Rejected, live.** §1.4. |
| Changing `src/retrieval/*` or `rag.py` | **Never attempted.** §1.2 shows the pool machinery was never the failing part. |
| Changing `prompts/evaluator.txt` | **Never attempted.** Out of scope by the brief. |

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_kb_statute_retrieval.py tests/test_harness_tool_rag.py -q
```

**96 passed, 0 failed** (73 + 23). No full `pytest -q` run — it empties
`muhafiz_entity_descriptions`.

Six new tests, in a new §9 of `tests/test_kb_statute_retrieval.py`. The mechanism
changed is a **prompt**, and every other test in that file stubs the LLM, so
nothing existing would notice the rule being deleted. They pin it at both ends:

- `test_statute_prompt_has_an_evidence_book_selection_branch` — the branch exists,
  names the book it routes to, and carries the measured lexical point
  (`"shall be proved"` in, `"admissible"` named as the phrasing to avoid).
- `test_statute_prompt_routes_an_absent_record_to_admissibility` — rule 4c exists
  **and is prohibitive**, with §1.4's live measurement in the comment.
- `test_statute_prompt_corpus_map_describes_the_qso_confession_bar` — asserts the
  Qanun-e-Shahadat map entry names the confession-to-police and in-custody bars,
  **and asserts the CrPC map entry was left as Module 30 wrote it**, so §2's KB5
  regression cannot be reintroduced silently.
- `test_statute_prompt_names_the_evidence_books_own_noun` — §1.3's
  confession/statement distinction.
- `test_module_30_selection_rules_survive_the_module_65_edit` — Module 30's three
  branches and rules 3/4/4b are all still there.
- `test_kb2_gold_text_reaches_the_llm_with_the_evidence_branch` — the rule
  survives `{n}`/`{query}` substitution and is in the system prompt actually sent
  for KB2's **literal gold text**, loaded from
  `evaluation/Gold_QA_Dataset_Final32_With_Answers.json`.
- `test_default_hypotheses_stays_at_two` — pins the constant with its reason.

---

## 4. Live verification

`/api/chat` on `:8024`, `admin@example.com`, platform-admin, All Cases, the same
worktree and private Chroma throughout. The **before** arm is this module's own
measurement on the branch's merge-base prompt, not an inherited claim. Chunk ids
are read from `backend.log`'s Module 30 diagnostic line for exactly the window
each request occupied — **never from a citation in an answer**.

### KB2 — the exact gold question, 3 runs each arm

> *Why doesn't our system keep a record of what a witness or an accused person actually said in a police interview — is that a data gap?*

`route=RAG` on all six runs, both arms.

| | r1 | r2 | r3 | Art. 38 (`…_c176`) | Art. 39 (`…_c177`) | s.162 (`…_c673/675/677`) |
|---|---|---|---|---|---|---|
| **before** | verifier refusal, 36.1 s | answered 632 ch, 41.5 s | verifier refusal, 34.8 s | **0/3** | **0/3** | **0/3** |
| **after** | answered 950 ch, 77.4 s | answered 950 ch, 74.6 s | answered 950 ch, 52.3 s | **3/3** | 0/3 | **3/3** |

The after-arm chunk set, identical on all three runs:
`…f9908363_c675` (s.162(1)) · `…0519abd8_c590` · `…0519abd8_c601` ·
`…0519abd8_c602` · `…f9908363_c1010` ·
`2_qanun-e-shahadat-order-1984_pdf_3e604153_c176` (Art. 38).

Statute hypotheses, identical on all three after-runs:

1. *"Qanun-e-Shahadat Order 1984 section 14 confession or admission made to police officer: shall not be proved as against the accused"*
2. *"Code of Criminal Procedure 1898 section 161 statement by accused or witness during investigation: shall not be signed by the person making it and shall not be used for any purpose at any inquiry or trial"*

The section numbers the model attaches — "14", "161" — are wrong; the surrounding
vocabulary is what retrieves, exactly as rule 3 says and as Module 30 measured.
The chunks that came back are the right ones.

**Verbatim after answer (r1; r2 and r3 identical):**

> According to the provided documents, statements made by a person to a police officer during an investigation can be reduced into writing, but such statements are not to be signed by the person making them, and they cannot be used for any purpose (except as provided) at any inquiry or trial in respect of any offence under investigation at the time when such statement was made [Document 1].
>
> However, it is also noted that a police officer may reduce into writing any statement made to him in the course of an examination and make a separate record of the statement [Document 2]. This suggests that while there is a process for recording statements, there are limitations on their use in legal proceedings.
>
> Therefore, the system does have mechanisms for recording statements, but there are legal restrictions on their use, which may be considered a limitation or data gap in terms of the admissibility and use of such records in legal proceedings.

**Verbatim before answer (r2, the one run of three that answered at all):**

> The system does keep a record of what a witness or an accused person says during a police interview. According to the rules and procedures outlined in the documents, statements made by witnesses or accused persons can be recorded by a magistrate under Section 164 of the Code of Criminal Procedure … Therefore, **it is not a data gap; the system has mechanisms in place to record such statements.**

---

## 5. Gold comparison

**Gold:** *No, that's by design, not a gap.* QSO Art. 38 (a confession to a police
officer cannot be used as evidence against the accused), Art. 39 (extends that to
custody unless in a magistrate's immediate presence), CrPC s.162 (bars a
police-recorded statement at trial). Because none of that content is legally
usable while it is in police hands, the witness records store identity and
contact only.

Judged on facts and ideas in the answer's own words, per the brief's standard.

**Retrieval: fixed. 0/3 to 3/3, on two of gold's three provisions.** Art. 38's
chunk and s.162(1)'s chunk are in the window on every after run and were in none
before. That is the defect Module 65 was filed for, and it is closed.

**The answer: improved, and still not gold.** The before answer concluded the
**opposite** of gold ("it is not a data gap; the system has mechanisms in place to
record"). The after answer states s.162's bar correctly and in gold's substance —
not signed, not usable at any inquiry or trial — which is one of gold's three
elements, in its own words. It then **hedges into the wrong conclusion anyway**:
*"which may be considered a limitation or data gap"*. Gold's whole point is that
this is **not** a gap. **Honest verdict: KB2 is a partial pass on the law half and
a fail on gold's conclusion.**

**Why, measured.** Art. 38's chunk is in the pool (position 6) and the generator
does not cite it. It cites Documents 1 and 2, and **Document 2 is
`…0519abd8_c590`** — one of the 2,243 phantom CrPC rows Module 30 filed as
**Module 37**, an orphaned re-ingestion still in `chunk_fulltext` but absent from
Chroma. Those rows carry s.161/s.164 *recording* text, they are in the window on
every KB2 run in both arms, and they are what the generator reasons from. So
KB2's residue is **generation over a pool that now contains the right provision**,
with a live index-hygiene defect feeding it the wrong one. That is Module 48's
original hypothesis, now standing on evidence rather than on nothing — filed as
**Module 77** (§8b).

**Art. 39 is not retrieved for gold's own wording** (0/3), though it is for the
paraphrase (§6). Reported, not tuned: gold's phrasing gives the model nothing
about *custody*, so its hypothesis never carries Art. 39's distinguishing
vocabulary.

**The data half** — gold's *"our witness records only store identity and
contact"* — is absent in both arms, as it is for every other KB question. That is
**Module 39**, untouched here.

---

## 6. Non-gold paraphrase

Two, each run 3× in both arms, and both measured to trip `_is_legal_kb_intent()`
(the fix is gated on it). Neither uses gold's wording, and — since the worked
example was rejected in §2 — neither has a counterpart anywhere in the prompt.

### KB2P — English, asks the admissibility question the other way round

> *If an investigating officer writes down what a suspect admits during questioning at the police station, can that written admission be used against him in court under our law — and is that why our case files hold no interview transcripts?*

| | answered | Art. 38 | s.162 |
|---|---|---|---|
| before | 1/3 | **0/3** | 3/3 |
| after | 2/3 | **3/3** | 3/3 |

### KB2Q — Roman-Urdu, no gold vocabulary at all

> *Kya qanoon ijazat deta hai ke mulzim ne police ke saamne jo iqbal-e-jurm kiya ho use adalat mein saboot ke tor par pesh kiya jaye — aur kya isi wajah se hamare record mein bayanat mehfooz nahin hote?*

| | answered | Art. 38 | Art. 39 | s.162 |
|---|---|---|---|---|
| before | **0/3** (verifier refusal 3/3) | 0/3 | 0/3 | 0/3 |
| after | **3/3** | **3/3** | **3/3** | **3/3** |

**KB2Q is the run that carries this module's weight.** It is the only
question-and-arm in this report that reaches **all three** of gold's provisions,
and it draws gold's conclusion:

> The law does not allow a confession made to a police officer to be used as evidence against the accused in court. According to [Document 4], "No confession made to a police officer shall be proved as against a person accused of any offence." … Furthermore, [Document 1] states that statements made to a police officer during an investigation shall not be used for any purpose at any inquiry or trial … **This is why statements or confessions made to the police are not preserved in the records as evidence. They are not admissible in court, which is why they are not kept as formal evidence in the records.**

Grounding checked by id, not by the citation: the window held `…_3e604153_c176`,
`…_3e604153_c177` and `…f9908363_c675`, whose verbatim text is in §1.1. **The
pipeline can now reach gold's answer for this question shape, in Roman-Urdu, from
a question that shares no wording with gold. It is gold's own phrasing that the
generator still mishandles** — which is why §5 calls the residue generation and
not retrieval.

---

## 7. Regression guard

All eight KB questions, at least 2 runs per arm, plus a **before/after equality
control on the statute-hypothesis step itself**
(`scripts/module65_hypothesis_control.py`, 3 runs per question per arm, comparing
which statute BOOK each hypothesis names — the thing that decides retrieval — so
model wording noise is not mistaken for a behaviour change).

**Equality control: 6 of 8 unchanged.**

| Q | before books | after books | |
|---|---|---|---|
| KB1 | CrPC+PPR ×3 | CrPC+PPR ×3 | same |
| **KB2** | PPR+CrPC ×3 | **QSO+CrPC ×3** | **the intended change** |
| KB3 | CrPC+PoliceOrder ×3 | CrPC+PoliceOrder ×3 | same |
| KB4 | PPR+Forensics ×3 | PPR+Forensics ×3 | same |
| KB5 | AntiRape+CrPC ×3 | AntiRape+CrPC ×3 | same |
| KB6 | Forensics+PPR ×3 | Forensics+PPR ×3 | same |
| KB8 | CrPC+PPR ×3 | CrPC+PPR ×3 | same |
| KB9 | CrPC+PoliceOrder ×3 | **CrPC+PPR ×3** | second slot drifts (§7b) |

**Live, KB1–KB9:**

| Q | route | before | after | verdict |
|---|---|---|---|---|
| KB1 | RAG | 2/2 answered, CrPC ss.154/155 | 2/2 answered, CrPC ss.154/155 | **unchanged** |
| **KB2** | RAG | 1/3 answered, 0/3 gold chunks, concludes the opposite of gold | 3/3 answered, **3/3 Art. 38 + 3/3 s.162** | **fixed (retrieval); see §5** |
| KB3 | **XNETWORK** | 2/2 "no cross-case connections" | 2/2, byte-identical | **unchanged — never reaches this code** |
| KB4 | RAG | 2/2 answered | 0/2 verifier refusal | **§7a — pre-existing instability, not a new failure mode** |
| KB5 | RAG | 2/2 answered, Anti-Rape Act | 2/2 answered, Anti-Rape Act | **unchanged** (and see §2's rejected direction) |
| KB6 | RAG | 2/2 answered, forensic packaging/sealing | 2/2 answered, adds *"unloaded, made safe"* | **unchanged / marginally better** |
| KB8 | RAG | 2/2 answered, s.173 + 14 days + 3-day interim report | 1/2 answered (r2 keeps `…_c752`/`…_c757`), r1 verifier refusal | **§7a** |
| KB9 | **XAGG** | 2/2 answered from the case graph | 2/2 answered, same | **unchanged — never reaches this code** |

**KB4, KB5, KB6 and KB9's data half (Module 39) is not touched by this module**
and is present after exactly as before: KB9 still answers from the case graph
with its "4 individuals appear in multiple cases" figure, KB5 still returns the
Anti-Rape procedure, KB6 still returns the forensic handling list, and KB4's
window still contains `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2209` — the chunk
that names **rule 27.16(1)** — on every after run.

### 7a. KB4 and KB8's verifier flap is pre-existing, and measured to be

KB4 going 2/2 to 0/2 was investigated rather than assumed, because it would
otherwise be this module's regression. **The prompt was reverted to the merge-base
version, the backend restarted, and KB4 re-run 3× on the old prompt: it answered 1
of 3, refusing twice** — with its own hypothesis wording varying run to run on the
*unchanged* prompt (*"entry of property in the **station** register"* vs *"in the
**malkhana** register"*), and that variation alone flipping the outcome.
Cumulative on this machine today: **old prompt 3 of 5 answering, new prompt 0 of
5**, with the *same book pair* and the *same governing rule* (`…_c2209`, rule
27.16) in the window either way. So the failure mode is KB4's documented verifier
instability — Modules 38, 52 and 61 each record it — and this module moved KB4
within that band rather than introducing a new one. **It is still a worsening on
the numbers and is reported as one, not explained away**: nothing here isolates it
to noise at N=5.

KB8's single refusal (r1 of 2) is the same shape. Its r2 answers with s.173, the
14-day limit and the interim report, from `…_c752`/`…_c757`, which is gold.

### 7b. KB9's second hypothesis drifts, and it does not matter today

KB9's slot 2 moves Police Order to Punjab Police Rules. Gold's book (CrPC, for
s.174) is named first in **both** arms, unchanged. And KB9 does not route to RAG
on this `main` at all (§7c), so the statute step never runs for it live.

### 7c. Two KB questions no longer route to RAG at all

Module 52 measured KB3 and KB9 as `route=RAG`. On this branch's merge-base
(`e412327`, after Module 67's router work) **KB3 routes to XNETWORK and KB9 to
XAGG, 2/2 in both arms**, returning in 8–16 s instead of 200–500 s. That is a
change in the world this module inherited, not one it made, and it means
**Modules 48/49's KB3 and KB9 work is now aimed at a route those questions no
longer take.** Flagged for whoever owns them; not this module's rows to edit.

---

## 8. New defects found, deliberately left unfixed

### 8a. `DEFAULT_HYPOTHESES` — Module 38's open question, answered and closed here

Module 38 §8a asked its successor to revisit `DEFAULT_HYPOTHESES = 2`, because
Module 30's rejection of n=3 rested on `cross_rerank_multi()`'s max-score merge,
which Module 38 replaced with RRF. **Revisited and measured** on KB2 — the
question with the most to gain, since it genuinely needs two books and has a
plausible third reading. Result in §2: the third slot is now *affordable* and
still buys *nothing measured*. It stays at 2, and
`src/pipeline/statute_hypothesis.py`'s comment now says so with this measurement
instead of citing a mechanism that no longer exists. **No follow-up module
needed.**

### 8b. Module 77 — KB2's answer contradicts the provision it was given

**The defect this module creates the conditions to see.** With Art. 38 and s.162
both in the window, 3 runs of 3, the generator states s.162's bar correctly and
then concludes *"which may be considered a limitation or data gap"* — the opposite
of gold, and of what it has just quoted. The same pipeline, on the same corpus,
reaches gold's conclusion for **both** non-gold paraphrases (§6), so this is not a
capability limit; it is specific to how gold's own phrasing ("is that a data
gap?") is answered. **This is a response-generation module, not a retrieval one,
and it is the honest successor to Module 48's KB2 half.**

### 8c. Module 37 is still live and is now demonstrably load-bearing

Module 30 filed the 2,243 orphaned `…_0519abd8` CrPC rows in `chunk_fulltext` as
an index-hygiene defect, with the note that "answers are not wrong". **They are
now.** Three of the six chunks in KB2's after-arm window are orphans (`_c590`,
`_c601`, `_c602`), they carry s.161/s.164 *recording* text, and they are the
documents the generator cites to reach the wrong conclusion (§5). They resolve to
nothing in Chroma, so a reader cannot look them up. Module 37 should be
re-prioritised on this evidence.

### 8d. Module 54's canonical quota pattern false-positives on port numbers

`grep -ciE "…|429|quota|UNAVAILABLE|503"` matched `503` inside the ephemeral port
`50358` in a routine `200 OK` access-log line. Every quota check in this wave's
prescriptive docs uses this pattern, so a clean run reports a phantom provider
failure. A word boundary (`\b(429|503)\b`) would fix it. Module 54 owns
`LOG_GREP_PATTERN`; flagged, not edited.

---

## Artefacts

- `docs/gold-qa-wave2-results/module65_probe.json` — §1.1/§1.2: existence by id, then rank by query.
- `docs/gold-qa-wave2-results/module65_runs.json` — all 63 live rows, every arm, including the two intermediate prompt variants (`after-v1`) and the old-prompt `recheck-before` control of §7a.
- `docs/gold-qa-wave2-results/module65_hypothesis_control.json` — §7's equality control, 48 generations.
