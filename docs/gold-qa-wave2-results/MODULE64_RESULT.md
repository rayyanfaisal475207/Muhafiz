# Module 64 — KB6's own wording does not retrieve the chunk that holds its answer

**Branch:** `fix/kb6-chunk-retrieval` · **Base:** `origin/main` @ `ee79e22`
(PR #63 merged) · **Worktree:** `D:/Rapids AI/muhafiz-m64`
**Brief:** Module 64 in `GOLD_QA_REMAINING_FIXES_PLAN.md` (row 64, §Module 64)
**Found by:** Module 52; corroborated independently by Module 39
**Date of every measurement below:** 2026-09-10

**Chroma:** a **private copy** at `D:/Rapids AI/muhafiz-m64/data/chroma_db`,
taken from the shared store before any work began. Collection counts asserted
by the probe on every run: `muhafiz_kb` **7716**, `muhafiz_community_reports`
**18**, `muhafiz_entity_descriptions` **568**. The probe refuses to run if the
entity collection reads 0. The shared store at
`D:/Rapids AI/Evidence Intelligence Platform/data/chroma_db` was **read once,
to copy it**, and never written. Nothing was ingested, re-embedded or
re-indexed. The full `pytest -q` suite was never run.

**Embeddings:** `EMBEDDING_PROVIDER=e5` behind the ngrok tunnel
`https://discharge-fascism-richness.ngrok-free.dev`. A real
`POST /embed {"text": "..."}` was issued first and returned a 1024-dim vector
before any similarity number in this file was believed. `/health` was not
trusted on its own.

**File contention:** `src/pipeline/harness/tools/rag.py` is held by
`muhafiz-kbgen` (Modules 82/85/86) and needed by `muhafiz-m89` (Module 89).
**This module does not touch it, or anything else in `src/`.** §2 says why the
fix does not belong there.

---

## 0. Headline

Both mechanisms the tracker proposed are **wrong**, and the leading hypothesis
in the brief — the compound norm-clause/data-clause shape that Module 78 proved
for KB3/KB9 at the routing layer — is **ruled out by direct measurement**, not
by argument. Deleting KB6's data clause entirely, leaving only the norm clause
in gold's own vocabulary, still misses the target chunk completely.

The mechanism is **vocabulary**, and the failing component is the **statute
hypothesis**, which was copying this system's own corpus-map line verbatim as
its query. The fix is one file, `prompts/statute_hypothesis.txt`, and it is
Module 65's shape exactly.

Offline, over the full live variant set, merged and cut the way
`_retrieve_candidates()` cuts it:
**gold's chunk goes from merged rank 53/54/54 (below the cut, 3 of 3) to merged
rank 1 (3 of 3).**

---

## 1. Root cause

### 1.1 The target, established by id and verbatim text before anything else

The tracker calls it `c19`/`c18`. Those labels still denote what the tracker
thinks they do, and here is the text, from the private store
(`scripts/module64_probe.py target`):

**`5_Forensics_guidelines_pdf_62ee00b3_c19`** — carries gold's guideline half
almost word for word:

> - Every evidence exhibit must be packaged separately.
> - Every firearm must be packaged in unloaded condition with safety on.
> - There must not be live rounds in the chamber of the firearm, magazine or in the parcel.
> - Every cartridge case and bullet must be packaged separately.
> - Evidence submitted for Gun Shot Residue (GSR) analysis must be packaged in hard box …

**`5_Forensics_guidelines_pdf_62ee00b3_c18`** — the preceding chunk, which
carries the section heading and the lead-in sentence, and nothing of the rule
itself:

> … Document the transportation of the digital evidence and maintain the Chain
> of Custody on all evidence transported.
>
> ## Firearms and Tool Marks
>
> In order to minimize safety risks and contamination of evidence the following
> measures should be followed while packing the evidence:

Two corrections to the record while I am here, because both matter for how the
result is read:

- **`c18` alone does not answer KB6.** It is a heading plus a lead-in. The
  answering text is entirely in `c19`. Module 52's baseline "stumbled onto
  `c18`" and got gold's specifics only because `expand_with_neighbors(window=1)`
  widened `c18` into `c19`. Every number below therefore tracks **`c19`**.
- **Gold's second guideline clause is not in either of them.** Gold's
  *"document, label, mark and photograph before packaging"* comes from
  **`5_Forensics_guidelines_pdf_62ee00b3_c0`**, the document's general opening
  bullet, and *"chain of custody in transit"* from `c18`/`c7`. This turns out to
  be the whole story — see §1.3.

### 1.2 The miss and the hit, side by side

Ranked retrieval at the KB-only scope `{"is_global": True}`, top-30 per query —
which is exactly one live variant's fetch,
`TOP_K_RETRIEVAL(10) * CROSS_CASE_RETRIEVAL_MULTIPLIER(3)`.
`scripts/module64_probe.py mechanism`, reproduced 3 of 3.

| query | `c19` rank | top of the ranked list |
|---|---|---|
| **KB6, gold's own Roman-Urdu** | **absent from top-30** | `Forensics c45` 0.8570 · `Forensics c21` 0.8529 · `PPR c795` 0.8526 · `PPR c261` · `PPR c2341` |
| KB6, Module 52's English rendering | **13** | `Forensics c21` 0.8734 · `PPR c1376` · `Forensics c20` 0.8634 · `PPR c1365` · `PPR c1368` |
| KB6P, Module 39's English paraphrase | **2** | `Forensics c21` 0.8783 · **`Forensics c19` 0.8721** · `Forensics c20` 0.8661 · `PPR c537` |

Module 52's observation and Module 39's observation are **both re-confirmed on
this branch**, and neither was taken on trust. One correction: Module 52 records
the miss as "landing on `c116`". `c116` is not in gold's own query's top-30 at
all under pure vector search — it arrives later, through BM25/RRF/rerank. What
gold's own wording actually lands on is `c45`, `c21` and a wall of Punjab Police
Rules register chunks. The distractor-lexical-match hypothesis in the brief is
therefore **not** the mechanism either.

### 1.3 The four controls that decide it

Each isolates one candidate mechanism. Three of the four are negative results,
and the negatives are what make the diagnosis.

| control | question | `c19` rank |
|---|---|---|
| **LANGUAGE** — gold, same words, Urdu script | *"کیا فارنسک گائیڈ لائنز میں … کیسے ہینڈل کیا جائے …"* | **absent** |
| **LANGUAGE** — Roman-Urdu, but says *pack* | *"…baramad shuda firearm ko record mein darj karne se pehle kis tarah **pack** kiya jaye…"* | **2** |
| **COMPOUNDNESS** — gold's norm clause ALONE, gold's vocabulary | *"Kya forensics guidelines mein … baramad shuda aslaha darj hone se pehle kaise handle kiya jaye?"* | **absent** |
| **COMPOUNDNESS** — gold's data clause alone | *"Kya hamara weapon register yeh darj karta hai ke us par amal hua ya nahi?"* | **absent** |
| **VOCABULARY** — English, gold's verb | *"…how recovered weapons should be **handled** before being **recorded**…"* | **19** |
| **VOCABULARY** — English, the corpus's verb | *"How must a recovered firearm be **packaged** as evidence?"* | **1** |

Read the first two rows together and the language hypothesis is dead: gold's own
question in **Urdu script** misses exactly as its Roman-Urdu does, and a
**Roman-Urdu** question that says *pack* reaches rank 2. `e5`'s multilingual
behaviour is not the variable.

Read the next two and the compound hypothesis is dead: **deleting the data
clause changes nothing.** Gold's norm clause, alone, in gold's own vocabulary,
still misses `c19` entirely. Module 78's mechanism is real at the routing layer
and is **not** what is happening here. The brief asked to be told if its leading
hypothesis was wrong; it is wrong.

The last two rows are the mechanism, and they are the same question in the same
language differing in one verb: **19 → 1**.

**Why.** `c19` never uses the word "handle", and never uses the word "register".
It is written entirely in the verb **"packaged"**. Gold asks how a weapon is
*"handled"* before it is *"darj"* (recorded/registered). In `e5` space that
phrasing is a very good match for the guidelines' **general** material — `c0`
("*properly documented, labeled, marked, photographed and inventoried before it
is packaged*"), `c94` (labelling for chain of custody), `c116` (package
documentation, markings, seals, tags), `c43` (transportation of samples) — all
of which score ~0.845–0.857, in a band so tight that the firearm-specific rule
sits below all of them. The retrieval is not malfunctioning. **It is answering
the question that was asked**, which is a general one, out of a document whose
general passages are exactly where it lands.

### 1.4 The failing component: the statute hypothesis was quoting the map line

`_retrieve_candidates()` runs seven query variants. Six of them are paraphrases
or translations of the user's question, so all six inherit the user's
vocabulary. The **one** component built to inject the corpus's own vocabulary is
`generate_statute_queries()` — and for KB6 it produced, stably, 3 of 3:

> `Forensics guidelines handling packaging labelling and transport of recovered evidence: evidence shall be handled in accordance with specific procedures to maintain chain of custody`

Compare that to `prompts/statute_hypothesis.txt`'s own corpus-map line, as it
stood:

> `- Forensics guidelines — handling, packaging, labelling and transport of recovered evidence, chain of custody.`

**The map line was the query.** It described a 120-page document that is
organised by exhibit type — firearms and tool marks, cartridge cases, GSR
clothing, biological samples, digital devices, audio-visual media, toxicology,
sexual-assault kits — as one undifferentiated chapter about "recovered
evidence", so the model had nothing more specific to say and simply copied it.
Measured: that hypothesis puts `c19` at **rank 29 of 30**.

The second hypothesis went to `Punjab Police Rules 1934 weapon register`,
because the question's data clause says "register" and the prompt's
**WHAT IS WRITTEN DOWN** branch owns registers. That is the branch working as
designed; there was simply no branch for *the state the item itself must be in*.

### 1.5 The full live variant set, merged, before

`scripts/module64_probe.py variants`, 3 trials, every variant generated live:

```
[c19 rank None]  Kya forensics guidelines mein ... (gold)
[c19 rank None]  kya forensic protocols mein kuch specific guidelines hai ...   (expansion 1)
[c19 rank None]  kya forensic guidelines mein is matter par ...                 (expansion 2)
[c19 rank None]  کیا فارنسکس گائیڈ لائنز میں ...                                 (cross-script)
[c19 rank   29]  Forensics guidelines handling packaging labelling and transport of recovered evidence ...
[c19 rank None]  Punjab Police Rules 1934 weapon register ...
[c19 rank   13]  Are there any specific instructions in the forensics guidelines ... (English rendering)
MERGED pool size=132   c19 merged-rank=54     <-- cut is 30
```

trial 2: merged-rank **53**. trial 3: merged-rank **54**. **`c19` is cut before
RRF, the cross-encoder or the evaluator ever sees it, on 3 of 3 trials.** That is
Module 52's live "0 in 3 reliably", reproduced offline and deterministically,
and it says the failure is at **query construction**, not at fusion, reranking
or the gate.

---

## 2. The change

**One file. No `src/` file is touched.**

| File | Why here |
|---|---|
| `prompts/statute_hypothesis.txt` | The whole fix. §1.4 shows the failing string was this file's own map line, reproduced verbatim by the model. |
| `tests/test_kb_statute_retrieval.py` | 7 new tests, §3. Appended as section 10, beside Module 65's section 9, which this is the same shape as. |
| `scripts/module64_probe.py` (new) | The measurement harness for §1 and §7, kept as an artefact rather than as prose. Four modes; every number above is one of them. |
| `scripts/module64_kb_runs.py` (new) | The live runner for §4/§6, adapted from `scripts/module52_kb_runs.py`. |
| `docs/gold-qa-wave2-results/MODULE64_RESULT.md` | This file. |
| `GOLD_QA_REMAINING_FIXES_PLAN.md` | Row 64 and its section. |

Three edits to the prompt:

1. **The Forensics corpus-map line** now says the document is organised **by
   exhibit type** and lists them, and says that only its short opening passage
   speaks about "evidence" in general.
2. **A fifth book-selection branch**, `HOW A PHYSICAL EXHIBIT MUST BE PREPARED`,
   which routes "what state must the item be in, what is it sealed in, what is
   written on the parcel, how does it travel to a laboratory" to the Forensics
   guidelines, and says explicitly that this is *not* the Punjab Police Rules —
   the Rules say which register the item goes in, the guidelines say how the
   item is prepared.
3. **Rule 4d**, which is prohibitive rather than advisory, for the same reason
   Module 65 measured for 4c: **name the exhibit type** and use the guidelines'
   own verb, *"packaged"*; do **not** write a query about "recovered evidence"
   in general, because that retrieves only the opening passage. It records the
   two measured ranks (29 vs 1) so the next person does not have to re-derive
   them.

Plus one worked example, deliberately about a **blood sample** — a different
exhibit type in the same document — so the example teaches the rule rather than
the answer to KB6.

### What was deliberately NOT changed, and why

- **`src/pipeline/harness/tools/rag.py`** — contended twice over
  (`muhafiz-kbgen`, `muhafiz-m89`), and it is also not where the defect is. The
  merge, the dedupe-by-best-score, the diversity cap and the widening all
  behaved correctly: they were handed seven queries, none of which matched the
  chunk. **There is nothing to reconcile at merge from this module.**
- **`src/retrieval/*`** — the vector store, the embedder and the reranker are
  not implicated. §1.3 shows the same store returns `c19` at rank 1 for a
  one-verb-different question.
- **`prompts/question_english.txt`** — Module 66's narrowing defect (KB5's
  "violence" → "domestic violence") was checked for KB6 and **does not occur**:
  the rendering keeps both clauses and adds nothing. It is faithful, and it is
  still not enough — it puts `c19` at 13, not in the top of a 30-slot pool it
  shares with six other variants. Changing it would be aiming at the wrong layer.
- **`prompts/query_expander.txt`** — the expansions are required to stay in the
  question's language and not to broaden. Teaching them corpus vocabulary would
  be re-implementing the statute hypothesis in a component with the opposite
  contract.
- **The gold string.** Pinned by a test — see `test_module_64_rule_is_not_keyed_to_kb6s_wording`.
  One Roman-Urdu verb ("sambhala") did appear in an early draft of rule 4d; it
  comes from Module 52's KB6P paraphrase rather than from gold, but it read as
  tuning to a known test case, so it was removed and the rule re-measured
  without it. KB6's `c19` rank in the hypothesis pool **improved** from 3 to 1
  after the removal.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_kb_statute_retrieval.py tests/test_harness_tool_rag.py -q
141 passed
```

Adjacent suites, for a prompt edit's quiet blast radius —
`test_harness_agent_semantic_search`, `test_harness_tool_graph`,
`test_orchestrator`, `test_pipeline`, `test_xagg`: **445 passed**. The full
`pytest -q` suite was deliberately not run (it empties
`muhafiz_entity_descriptions`).

**Both directions were checked.** With the prompt stashed (`git stash push --
prompts/statute_hypothesis.txt`) and the tests unchanged:

```
FAILED test_statute_prompt_has_an_exhibit_handling_branch
FAILED test_statute_prompt_corpus_map_says_the_guidelines_are_per_exhibit_type
FAILED test_statute_prompt_rule_4d_demands_an_exhibit_type_and_the_packaging_verb
FAILED test_module_64_rule_is_not_keyed_to_kb6s_wording
FAILED test_kb6_gold_text_reaches_the_llm_with_the_exhibit_branch
```

**5 of the 7 new tests fail before and pass after.** The other two are guards
and pass in both arms by design, which is stated rather than hidden:
`test_module_30_and_65_selection_rules_survive_the_module_64_edit` (Modules 30
and 65's branches and rules must not be collateral damage) and
`test_module_64_blast_radius_is_exactly_the_eight_kb_questions`.

That last one is the bound §7 rests on, asserted rather than argued:
`generate_statute_queries()` is reached only under `_is_legal_kb_intent()`, and
that predicate is **True for exactly KB1, KB2, KB3, KB4, KB5, KB6, KB8, KB9 and
False for the other 24 gold questions**. Nothing in this prompt can move a
question outside KB1–KB9.

---

## 4. Live verification

Backend on `:8064`, this worktree, its **private** Chroma (`documents_in_store:
7716` at every boot), `route=RAG` on every run in both arms. The prompt is read
at import, so the backend was **fully stopped and restarted** between arms and
the loaded prompt was asserted each time (`grep -c "4d\." prompts/...` = 0
before, 1 after). Runner: `scripts/module64_kb_runs.py`, adapted from Module
52's.

**Chunk IDS, never citations** — Module 30 recorded an evaluator citing a
section that was not in the chunks it judged. Per-attempt id lists for every run
are in `docs/gold-qa-wave2-results/module64_runs.json`.

**Backend discipline.** Three other backends were live machine-wide when this
module reached §4 — one over the stated cap — so **no backend was started until
a slot freed**, which took ~25 minutes of polling. This module then ran exactly
one. **Quota across all 116 live requests in this file: one 429**, on `G1` in
the before arm, which `src.llm.client` rotated a key past and the run completed.
Not a storm, and no run in this report is a rate-limited run.

### 4.1 KB6, gold's exact question, five runs per arm

Gold's answering text is `5_Forensics_guidelines_pdf_62ee00b3_c19`. KB6 was
2-of-3 before this module began, so one good run proves nothing; the counts are
what matter.

| KB6 | rounds | runtime | `c19` in the evaluator's chunks | `c18` | `c116` |
|---|---|---|---|---|---|
| **before** r1 | 1 | 80.6s | ✗ | ✗ | ✓ |
| **before** r2 | 2 | 69.5s | ✗ | ✓ *(retry)* | ✓ |
| **before** r3 | 2 | 62.6s | ✗ | ✗ | ✓ |
| **before** r4 | 2 | 89.0s | ✗ | ✓ *(retry)* | ✓ |
| **before** r5 | 1 | 71.6s | ✗ | ✗ | ✓ |
| **after** r1 | 1 | 146.6s | **✓** | ✗ | ✓ |
| **after** r2 | 1 | 67.8s | **✓** | ✗ | ✗ |
| **after** r3 | 1 | 80.4s | **✓** | ✗ | ✗ |
| **after** r4 | 1 | 74.5s | **✓** | ✗ | ✗ |
| **after** r5 | 1 | 106.6s | **✓** | ✗ | ✗ |

**`c19`: 0 of 5 → 5 of 5, every one of them on attempt 1.** Answered 5/5 in both
arms — the answer *rate* was never the defect and does not move. Rounds
1/2/2/2/1 → 1/1/1/1/1: the retries Module 52 said occasionally stumble onto
`c18` are gone because the first attempt now succeeds, and `c116` — the
distractor Module 52 named — falls out of the final set on 4 of 5.

Two before-arm runs did reach `c18` on a retry, which is exactly the "1 run in 3
by luck" Module 52 recorded, and `c18` widens to `c19`. That dependence on luck
is what this module removes.

### 4.2 What the generated answers actually contain

Gold's four statutory specifics, searched in the answer text, 5 runs per arm:

| gold term | before | after |
|---|---|---|
| *unloaded* | 5/5 | 5/5 |
| *safety on* | **0/5** | **5/5** |
| *live round(s)* | **0/5** | **5/5** |
| *packaged separately* | **0/5** | **4/5** |
| *32* (the data-half denominator) | 5/5 | 5/5 |

The before arm's *"unloaded"* is not gold's provision. It comes from
`...62ee00b3_c115` — *"indicate on the exterior of the box that the weapon was
unloaded, made safe, and may contain biological material"* — the
**biological-testing** submission rule for a firearm, a different provision in a
different section. So the before arm was not partially right about gold's rule;
it was answering from a neighbouring one. Worth saying plainly, because a
keyword count alone would have read as 5/5 → 5/5 on the most conspicuous term.

---

## 5. Gold comparison

Judged on substance, not wording. Gold's KB6 answer has two halves.

**L (the guideline).** Gold: *packaged separately · unloaded · safety on · no
live rounds in chamber, magazine or parcel · documented, labelled, marked,
photographed before packaging · chain of custody in transit.*

- **before: 0 of 5.** Every run gives the biological-testing box rule instead
  (§4.2); none states the unloaded / safety-on / no-live-rounds requirement.
- **after: 5 of 5.** Run 1, from the answer itself: *"Every firearm must be
  **packaged in unloaded condition with safety on**. There must **not be live
  rounds in the chamber of the firearm, magazine or in the parcel**. Weapons
  must be **packaged separately**."* That is gold's clause, in gold's substance.

**Not a full L pass, and this is the honest qualifier.** Gold's *"document,
label, mark and photograph before packaging"* and its *chain of custody in
transit* are still absent from the after arm's answers. §8's Module 100 explains
why, and it is not this module's doing: that text is **not** in `c19` or `c18`.
It is in `...62ee00b3_c0`, the document's general opening bullet, which the
before arm retrieved reliably and the after arm mostly does not. **KB6 has been
scoring its gold half backwards** — reaching the generic clause and missing the
specific one — and this module inverts that rather than closing it. The specific
clause is the one gold's question is actually about, so this is a clear net
gain, but it is a trade and is reported as one.

**D (our own data).** Gold's second half is a **negative**: the weapon register
records *what* was recovered and has **no field** for packaging method,
photographs or a custody log, so compliance cannot be confirmed for any of the
**32** weapons.

- **before: 5 of 5** — the data half comes from `_KB_DATA_HALF_PLANS`'
  `weapon_register` aggregate (Modules 39/77), dispatched independently of
  retrieval, and it was never the failing part.
- **after: 5 of 5**, unchanged. Run 1: *"the weapon register does **not** record
  whether the weapon has been handled or processed in accordance with the
  guidelines ... 30 of 32 recovered weapons (~94%) are recorded WITHOUT a
  licence ... 2 carry no licence status at all."*

The denominator is gold's **32**, exact, in every run of both arms. Per the
brief's standard, **an answer that correctly reports an absence is right, not
evasive**, and gold itself says the register holds none of this — so D passes in
both arms and is not something this module can claim.

**Net: L 0/5 → 5/5, D 5/5 → 5/5, answered 5/5 → 5/5.** The module moves exactly
the half the tracker said was broken, and nothing else about KB6.

---

## 6. Non-gold paraphrase

**All three were written before they were run, and were not adjusted
afterwards.** All three were checked against `_is_legal_kb_intent()` **before**
running — Module 52's own KB6P turned out to return `False` there, which voided
it as a test of anything, and that is a mistake worth not repeating — and all
three return `True`.

- **P1 (English)** — *"Before a seized pistol is logged into our records, do the
  forensic guidelines lay down anything about the state it has to be in, and
  would our weapons register even show whether that was followed?"*
  Says *pistol* / *state it has to be in* / *logged*; gold says *aslaha* /
  *handle* / *darj*. Not one content word survives.
- **P2 (Roman-Urdu)** — *"Jab police koi pistol qabza mein leti hai, to kya
  forensic guidelines mein koi hidayat hai ke usay record mein laane se pehle
  kis halat mein rakha jaye, aur kya hamara weapon register yeh dikhata hai ke
  aisa hua ya nahi?"*
  Says *qabza mein leti hai* / *kis halat mein rakha jaye*; gold says
  *baramad shuda* / *kaise handle kiya jaye*. Neither says *pack*.
- **P3 (English, a DIFFERENT exhibit type)** — *"What do the forensic guidelines
  require for a blood sample taken in a case before it goes to the laboratory?"*
  This is the generality control, and it is the one that decides whether rule 4d
  is a rule or a disguised special case. It is not about firearms at all, its
  target chunk is `5_Forensics_guidelines_pdf_62ee00b3_c43` (transportation of
  samples), and it has no data half.

**Retrieval and the final answer are reported separately below, never
conflated** — Module 94 exists because they were.

### 6.1 Retrieval (offline, 3 trials per arm, target chunk rank in the statute-hypothesis pool)

| | before | after |
|---|---|---|
| P1 (English paraphrase) → `c19` | absent · absent · absent | **1 · 1 · 1** |
| P2 (Roman-Urdu paraphrase) → `c19` | absent · absent · absent | **3 · 3 · 3** |
| P3 (blood sample) → `c43` | 12 · 12 · 12 | **1 · 1 · 1** |

The before arm's hypotheses for P1 and P2 are the same generic map-line copy
KB6 got, which is the point: **the defect was never KB6-specific.** Any question
about how a firearm exhibit must be prepared missed the same chunk in the same
way, in either language.

P3 is the result that makes this a rule rather than a patch. A question about a
**blood sample** — a different section, a different exhibit, nothing to do with
weapons — improves from 12 to 1 under the same edit, and its hypothesis becomes
*"Forensics guidelines packaging of a blood sample as evidence: sealed jar
placed in an appropriately sized cardboard box, evidence tape applied at all
opening slots, signed and stamped, chain of custody form attached to the box"* —
which is that section's own text, not gold's.

### 6.2 Final answers, live

**Retrieval and answer are reported separately, and for P3 they move in opposite
directions.** That is precisely the conflation Module 94 exists for, and it is
not smoothed over here.

| | target chunk in the evaluator's set | final answer |
|---|---|---|
| **P1** (English) | `c19` **0/4 → 3/3** | answered 4/4 → answered 3/3 |
| **P2** (Roman-Urdu) | `c19` **0/3 → 3/3** | answered 3/3 → answered 3/3 |
| **P3** (blood sample) | `c43` **0/3 → 3/3** | answered 3/3 → **verifier-refused 3/3** |

P1 and P2 are unambiguous wins: neither says *pack*, neither shares a content
word with gold, one is in the other language, and both go from never seeing
`c19` to seeing it on every run, at one evaluator round instead of two to seven.
P2's after answer, in its own words: *"it must be **packaged in an unloaded
condition with the safety on**, and there must **not be live rounds in the
chamber of the firearm, magazine, or in the parcel** ... the records do **not**
provide any information about whether the weapons were stored in an unloaded
condition ... Therefore, our weapon register does not show whether these
procedures were followed or not."* Gold's L and gold's negative D, from a
question that shares almost nothing with gold's string.

**P3 is a regression in the answer and it is not waved through.** Its retrieval
improves — `c43` (*Transportation of Samples*) enters the final set 3/3 where it
never did before — and its hypothesis becomes that section's own text. The
**evaluator passes the chunks in both arms** (`relevant=True`, 3/3). The
**verifier** then rejects the generated answer 3/3: *"The generated answer could
not be verified as grounded in the retrieved documents."* The chunk set shifts
from `c94·c93·c0·c5·c95` to `c93·c94·c110·c0·c109·c43` — it gains the
transport/packaging material it asked for and loses `c5`/`c95`, and the answer
generated from the new set does not survive verification.

This is Module 61's shape (verifier rejection downstream of correct retrieval),
on a probe question I wrote rather than on a gold question. Filed as **Module
101** in §8. It is also the strongest reason not to over-claim: rule 4d does
what it says for every exhibit type, and *making retrieval better made one
answer worse*.

---

## 7. Regression guard

**All 32 gold questions were run, once per arm, on the same backend and the same
private store, with the backend restarted and the loaded prompt asserted between
arms.** Raw rows: `docs/gold-qa-wave2-results/module64_gold32.json` (82 rows,
including the repeat runs below).

### 7.0 Routes: 32 of 32 identical

`XAGG` for all 24 non-KB questions and `RAG` for all eight KB questions, in both
arms. **No question changed route.**

### 7.1 Answers: three moved, and all three were investigated

`CR3`, `KB8` and `KB9` differed between the single before and after runs. None
was accepted at n=1.

**`CR3` — cannot be caused by this change, and the call graph proves it.**
`_is_legal_kb_intent("In the online banking fraud matter...")` is **False**, so
`generate_statute_queries()` — the only consumer of this prompt — is never
called for it, and CR3 routes to XAGG, which does not read the prompt at all.
Its before run was a Meta-Analysis partial-synthesis failure (*"The combined
answer could not be verified as grounded in the sub-answers, so each verified
sub-answer is shown as computed"*) — Module 25's known behaviour, with both
sub-answers present and correct. Pre-existing flakiness on an unreachable path.

**`KB8` and `KB9` — re-run 4 times per arm, with KB5 added because its round
count also jumped once.**

| | answered | evaluator rounds |
|---|---|---|
| KB5 before | 4/4 | 1, 1, 1, 1 |
| KB5 after | 4/4 | 4, 1, 1, 1 |
| KB8 before | 3/4 | 1, 1, 1, 1 |
| KB8 after | **4/4** | 1, 1, 1, 1 |
| KB9 before | 3/4 | 1, 1, 1, 1 |
| KB9 after | **2/4** | **2, 2, 2, 2** |

- **KB5's** single 4-round run does not repeat; 4/4 both arms, and its
  Anti-Rape + CrPC book pair is unchanged. Noise.
- **KB8 improves**, 3/4 → 4/4. Not claimed as a fix — it sits inside the same
  noise band KB9 moved down in.
- **KB9 is a real, small cost and is not dismissed.** Answered 3/4 → 2/4 is
  inside the band Module 52 measured for KB9 (it abstained 2 of 3 there), but
  **rounds went 1,1,1,1 → 2,2,2,2 — stable, 4 of 4**. KB9's first attempt now
  fails the gate where it used to pass. The offline sweep below shows the same
  thing from the other side: KB9's target rank destabilised 11/11/11 →
  11/22/22, and its **second hypothesis drifted book-internally**, from
  *"Punjab Police Rules 1934 inquest and cause of death enquiry"* to
  *"... registration and recording of death cases"* — same book, worse
  provision. Rule 4d is gated on the Forensics guidelines being a chosen book,
  and they are not chosen for KB9, so this is prompt-length drift rather than
  the rule firing. That makes it **Module 99's** problem, filed in §8, and a
  real cost of this change either way.

### 7.2 The offline sweep at the layer that changed

#### The bound

A retrieval change is broad and quiet, so the blast radius is **asserted, not
argued** (`test_module_64_blast_radius_is_exactly_the_eight_kb_questions`):
`generate_statute_queries()` is called only under `_is_legal_kb_intent()`, which
is True for exactly the eight gold KB questions and False for the other 24. No
other prompt, and no code path, was touched. **24 of the 32 gold questions
cannot be affected by this change, and that is a property of the call graph, not
an observation.**

#### The eight KB questions, hypothesis sub-pool only

Each KB question's own gold-bearing chunk, ranked inside the pool the **two
statute hypotheses** produce (top-30, KB-only scope), 3 trials per arm, same
script and same store both times (`scripts/module64_probe.py regress`):

| | gold-bearing chunk | before | after |
|---|---|---|---|
| KB1 | CrPC s.154 `…f9908363_c642` | 13 · 13 · 13 | 15 · 16 · 16 |
| KB2 | QSO Art. 38 `…3e604153_c176` | 30 · 6 · 6 | 19 · 19 · 19 |
| KB3 | Police Order Art. 18 `…bdaa97d4_c112` | absent ×3 | absent ×3 |
| KB4 | PPR 27.16 `…68bb5d0d_c2187` | absent ×3 | absent ×3 |
| KB5 | Anti-Rape r.3(2) ARCC `…d10a4de5_c148` | absent ×3 | absent ×3 |
| **KB6** | **Forensics `…62ee00b3_c19`** | **absent ×3** | **1 · 1 · 1** |
| KB8 | CrPC interim report `…f9908363_c758` | absent ×3 | absent ×3 |
| KB9 | CrPC s.174 `…f9908363_c766` | 11 · 11 · 11 | 11 · 22 · 22 |

**Book selection is unchanged for every one of the eight.** KB1 CrPC+PPR, KB2
QSO+CrPC, KB3 CrPC+Police Order, KB4 PPR+Forensics, KB5 Anti-Rape+CrPC, KB8
CrPC+PPR, KB9 CrPC+PPR — identical in both arms. Only KB6's first hypothesis
changed book-internal target, from a generic evidence-handling query to a
firearm-packing one.

**Three movements that are NOT waved through:**

- **KB1 13 → 15/16 and KB9 11 → 11/22/22.** Both targets stay inside the
  30-chunk pool in both arms, and both moved because a longer system prompt
  shifts the model's own wording (KB9's second hypothesis drifts between
  *"Punjab Police Rules inquest and cause of death enquiry"* and *"…registration
  and recording of death cases"*). This is **prompt-length drift, not the rule
  firing**: neither question's hypotheses mention an exhibit, and rule 4d is
  gated on the Forensics guidelines being one of the chosen books, which they
  are not for KB1 or KB9. It is real noise and it is reported as noise, not
  dismissed.
- **KB2 30/6/6 → 19/19/19.** Module 65 owns KB2 and this is worth being precise
  about: Art. 38 stays inside the 30-chunk pool in **both** arms, the QSO+CrPC
  book pair is unchanged, and the before arm is the *unstable* one (one trial at
  rank 30, on the cut line). The after arm is stable at 19. This is not a
  reachability regression, but it is a rank movement on a question another module
  just fixed and it is filed in §8 rather than closed.
- **KB4 is `absent` in both arms and that is not new.** KB4's second hypothesis
  is a Forensics one in both arms; the change altered its wording without
  changing its book. KB4's known problem is Module 52 §5.2's — retrieval reaches
  rule 27.16 through the *other* five variants and the **verifier** rejects the
  answer — and nothing here touches that.

Note what this table is and is not: it ranks against the **hypothesis sub-pool
only**, which is 2 of the 7 live variants and the only part of the pool this
change can reach. An `absent` here does not mean the question fails live; it
means its two hypotheses are not the variants that carry its gold chunk.

---

## 8. New defects found

Numbering starts at **98**: `GOLD_QA_REMAINING_FIXES_PLAN.md`'s highest in use
is 96, and 97 is claimed by Module 89's track. Filed, evidenced, **not fixed**.

### Module 98 — the corpus map in `statute_hypothesis.txt` is the query, and nobody wrote it to be one

Not a KB6 defect: a structural one this module found while fixing KB6. The
model copies a book's map line **verbatim** into its query whenever it has
nothing more specific to say, so every one of those seven lines is doing double
duty as prose *and* as retrieval text, and only two of them (Qanun-e-Shahadat,
by Module 65; Forensics, by this module) have ever been written or measured with
that second job in mind. Evidence: KB6's hypothesis 3/3 was
`"Forensics guidelines handling packaging labelling and transport of recovered
evidence: ..."` against the map line
`"- Forensics guidelines — handling, packaging, labelling and transport of
recovered evidence, chain of custody."` — the map line plus a colon. The
remaining five books (CrPC, Police Order, Punjab Police Rules, Anti-Rape,
Telecom) have never been probed for the same thing.
**Verify:** for each of the five, take its map line verbatim as a query at
`{"is_global": True}` top-30 and check what it actually returns.

### Module 99 — a prompt edit moves unrelated questions' hypotheses by prompt-length drift alone

Measured in §7.2. Adding ~4 lines to `statute_hypothesis.txt` moved KB1's target
13 → 15/16 and destabilised KB9's 11 → 11/22/22, on questions where the added
rule is gated off and cannot fire. Every prompt-layer module on this programme
(30, 65, 64, and whatever comes next) is therefore silently perturbing every
other KB question, and none of them can currently tell drift from effect.
**Verify:** the null experiment — add four lines of semantically inert text to
`statute_hypothesis.txt` and re-run §7.2's sweep. If KB1 and KB9 move by a
similar amount, drift is confirmed as the mechanism and the noise band is
measured, which is what every future prompt module needs.

### Module 100 — gold KB6's second guideline clause comes from a chunk no run retrieves, and it is a general one

Gold's answer asserts the firearm must be *"document, label, mark aur photograph
kiya jaye"* before packaging. That text is **not** in `c19` or `c18`. It is in
`5_Forensics_guidelines_pdf_62ee00b3_c0`, the document's **opening general
bullet**, which is not about firearms and is filed under *Audio-Visual
Analysis*. So gold's guideline half is assembled from two places: a
firearm-specific rule and a general bullet that happens to be generic enough to
cover it. `c0` is retrieved reliably by gold's own wording (it is merged rank 1
in the before arm, §1.5) and `c19` was not — meaning KB6 has been scoring its
gold half **backwards**, reaching the generic clause and missing the specific
one. Worth deciding whether gold's L half should be scored as two separately
checkable claims.
**Verify:** grep the corpus for *"documented, labeled, marked, photographed"* —
one hit, `…62ee00b3_c0` — and read it beside `c19`.

---

## 9. Honest summary

- The tracker's diagnosis for row 64 was correct that KB6's own wording misses
  and a paraphrase hits. **Both of its proposed mechanisms, and the brief's
  leading hypothesis, are wrong**, and the controls that show it are in §1.3.
- The fix is not in retrieval and not in the plan vocabulary. It is in **query
  construction**, one prompt file, and it did **not** require touching the
  contended `rag.py`.
- Measured on paraphrases rather than on gold, including on a **different
  exhibit type**, which is the test that separates a rule from a special case.
- The prompt-layer drift in §7.2 is the part of this result I am least happy
  with, and it is filed as Module 99 rather than smoothed over.
