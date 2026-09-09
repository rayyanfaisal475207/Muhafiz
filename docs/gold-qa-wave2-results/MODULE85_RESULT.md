# Module 85 — KB4 answers from the neighbouring subject · Module 86 — and drops the denominator

**Module 86 is folded into this document**, because one attempted change covered
both and the evidence for each is the same six live runs. Where the two differ
it is said so.

**Branch:** `fix/kb-downstream-generation`
**Question:** KB4 (primary), with Module 38's Roman-Urdu paraphrase (KB4P) as the
positive control.
**Sibling result:** `MODULE82_RESULT.md` — KB2, same branch, same attempt, same
batches. It carries the machine/store/provider header, §2's full account of the
change that was reverted, §3's test list, §4a's run-integrity failures, §4c's
forced-hallucination control and §7's regression table in full; they are
summarised rather than repeated here.

**Outcome, stated first.** Both defects are reproduced and root-caused, with
chunk text read out of the store by id. **The generation change written for them
was measured live, found to be a net regression on exactly these two questions,
and deleted.** `git diff origin/main -- src/` is empty.

**Machine, store, provider health, quota:** as `MODULE82_RESULT.md`'s header.
Shared Chroma, **read-only**, 7,716 documents; reranker live on every batch;
`verifier.py`, `validation.py`, `prompts/verifier.txt`, `router.py`,
`supervisor.py`, `evaluation/`, `xagg.py`, `statute_hypothesis.py`,
`src/retrieval/` and `rag.py` untouched.

---

## 1. Root cause

### 1.1 Module 85 did not reproduce as a verifier rejection

The tracker's Module 85 row records *"the grounding verifier rejects a composed
KB answer that has already been handed its figure"* — Module 77's KB8 at 1 of 3
and a KB9 paraphrase at 1 of 2 — and Module 38 recorded KB4 at **2 of 3
rejected**, on reasons Module 61 classified as *"over-attribution to a RAG chunk,
which is never exhaustive and which this module deliberately gives no licence
to."*

**On this `main` @ `6cf89fb`, KB4 is not rejected at all: `status=done` 3 of 3,
and the entire shipped-code arm — 12 primary runs plus 17 regression runs —
carries zero Semantic-Search verifier rejections.** Reported as measured: no
before/after improvement in KB4's rejection rate could honestly be claimed here.
Module 61 saw the same instability from the other side (5 of 5 answering where
Module 38 saw 1 of 3) and warned the 1-of-3 figure should not be treated as
fixed. It still should not.

**What did reproduce, 3 of 3, is the defect underneath the rejection** — the
over-attribution itself, now passing the judge instead of being caught by it.

### 1.2 The window holds gold's substance; the answer is built on the neighbours

KB4's window is **identical on all three shipped-code runs**, seven chunks. Every
text below is read out of the shared store by id with
`scripts/module82_window_probe.py`, never from a citation — Module 30 recorded an
evaluator citing a "section 174" absent from the chunks it judged.

| pos | chunk id | what it is |
|---|---|---|
| 1 | `7_Anti-Rape…_pdf_d10a4de5_c157` | marking/packaging of evidence, chain of custody |
| **2** | `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2197` | **gold's substance** — *"…which has been in the custody of the police for over three years. … **This register is a permanent record.**"* |
| 3 | `7_Anti-Rape…_pdf_d10a4de5_c156` | contamination precautions, packaging |
| **4** | `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c2421` | **gold's substance** — *"FORM No. 27.18(1) REGISTER OF ISSUE FROM AND RETURNED TO THE PROSECUTING INSPECTOR'S MALKHANA OF CASE PROPERTY…"*, the register's own column list |
| 5 | `4_Punjab-Police-Rules-III_pdf_68bb5d0d_c1645` | *"Detailed lists of stolen property, or of property seized in the course of a search, shall be entered in the first case diary…"* |
| 6 | `5_Forensics_guidelines_pdf_62ee00b3_c5` | tamper-evident seals, transport |
| 7 | `kb-data-half:property_register` | the computed summary — §1.3 |

Gold's own rule chunk `…_c2209` (*"…a receipt thereof taken in register No. 1
**[rule 27.16(1)]**"*), which Module 38 measured 3 of 3, is **0 of 3** here for
gold's wording and **3 of 3** for the paraphrase (§6). Same split as KB2's, filed
together as **Module 97** (`MODULE82_RESULT.md` §8a).

**The answer, 3 of 3 runs, spends its entire "Part 1" on positions 1, 3 and 6 —
the packaging and chain-of-custody chunks — and never touches positions 2, 4 or
5.** The question asks how case property is *recorded and eventually disposed
of*. Verbatim, run 1:

> ### Part 1: What the law, rule or guideline requires
> … - **Marking, packaging, and documenting evidence** (Document 1) …
> - **Chain of custody** (Document 1) … - **Precautions to avoid
> contamination** (Document 1) … - **Packaging and sealing** (Document 6) …

and it pays for that choice in Part 2, on all three runs:

> The records **do not show any entries that explicitly mention the marking,
> packaging, or chain of custody as required by the legal provisions**.

— a false negative manufactured by having answered a question nobody asked. The
three Punjab Police Rules chunks that *do* govern recording and retention sat
unread at positions 2, 4 and 5.

**This is the same defect Module 38's verifier rejection named** (*"incorrectly
attributes alignment with the Anti-Rape Act to the property record system"*), one
step earlier. Nothing in `_SYSTEM_PROMPT_TEMPLATE` says that a
similarity-retrieved window contains neighbours, or that a claim belongs to the
document that states it.

### 1.3 Module 86 — the 45 is computed, handed over, and dropped

`rag.py`'s data-half plan fires on **6 of 6** KB4-shaped runs
(`RAG tool: KB data-half plan 'property_register' answered by aggregate
'seized_property_disposition' (1369 chars)`), and — this is the part that makes
it a generation defect rather than an upstream one — **the 45 is the first
number in the chunk's first line.** `xagg.py::render_seized_property_disposition`
opens with:

```
What happens to seized property — 45 property-register entr(ies) across 28
FIR(s), grouped by the disposition recorded against each item:
```

followed by the nine per-disposition rows and the forensic/heirs pair. So the
generator is handed the total, in the lead sentence, on every run.

**Gold's data half is the 45** — *"ریکارڈ میں موجود 45 اندراجات"*, each carrying
serial number, registration number, entry date, description, quantity and
condition. The answer reproduces the 28, all nine dispositions, and the
13-forensic / 7-heirs pair — and states the 45 on **0 of 6 runs** (KB4 3/3 and
KB4P 3/3). Module 77 measured 3 of 3; this is six.

**Why the existing rule could not catch it.** Module 39's `_COMPOUND_ANSWER_RULE`
already says *"with the figures exactly as that summary gives them"*, and the
answers satisfy it: every figure served is exactly as given. A breakdown is a
faithful **subset** of a summary. The rule was scoped to the *fidelity* of the
figures carried; the property it needed was *which* ones must be. Same shape as
Module 71's finding about its own rule 2 — the fabricated 73 *did* appear
literally in a sub-answer above, just not the one the claim was about.

---

## 2. Change — none shipped

**`src/pipeline/harness/agents/semantic_search.py` is byte-identical to `main`.**
`MODULE82_RESULT.md` §2 gives the full text of what was written and §2.2 the
directions considered and rejected. In summary, three things were written and all
three were reverted:

| Written | Aimed at | Measured result |
|---|---|---|
| **Rule 1 — SUBJECT** (*"…retrieved by similarity, so some will be about a neighbouring subject… Cite a document only for what that document itself states…"*) | §1.2 | KB4 **3 of 3 → 1 of 3** answering |
| **Rule 2 — CONCLUSION** (*"…state your conclusion plainly and stand behind it… An absence the law requires is a finding, not a gap."*) | KB2 | the source of the new rejections (§4b) |
| **The Module 86 clause** (*"INCLUDING the totals and denominators it states and not only the breakdown beneath them"*) | §1.3 | the 45 on **0 of 5** runs — unchanged |

---

## 3. Unit tests

**386 passed, 0 failed** over the eight suites listed in `MODULE82_RESULT.md` §3.
Six new tests in `tests/test_module82_generation_findings.py`, of which two are
this document's:

- `test_module86_the_compound_rule_is_module_39s_unchanged` — the denominator
  clause is absent and Module 39's rule survives intact, with §1.3's measurement
  in the docstring.
- `test_module82_the_measured_generation_rules_are_not_in_the_prompt` — carries
  §4b's table, so re-adding rule 1 without redoing the measurement fails here.

---

## 4. Live verification

`route=RAG` on all 12 KB4/KB4P runs, both arms. Run-integrity failures (one
cutover fallback per arm; a Postgres restart) are disclosed in
`MODULE82_RESULT.md` §4a; neither touched a KB4 or KB4P row.

### 4a. KB4 and KB4P, shipped code — the baseline this module measured

| | KB4 (gold, Urdu) | KB4P (Roman-Urdu paraphrase) |
|---|---|---|
| answered | **3 of 3** | **3 of 3** |
| verifier rejections | 0 | 0 |
| `…_c2197` (three-year permanent register) in window | **3 of 3** | **3 of 3** |
| `…_c2209` (rule 27.16(1) by number) in window | **0 of 3** | **3 of 3** |
| data-half plan fired | 3 of 3 | 3 of 3 |
| answer names the register / retention rule | **0 of 3** | **3 of 3** |
| answer states the **45** | **0 of 3** | **0 of 3** |

### 4b. With the rules — the regression that caused the revert

| | KB4 | KB4P |
|---|---|---|
| answered | **1 of 3** | **1 of 3** |
| verifier rejections | **2** | **2** |
| the 45 stated | 0 of 1 | 0 of 1 |

The four rejection reasons, verbatim:

```
The claims about Rule 27.16(1) and Rule 27.16(4) are not directly stated in the
cited chunks. The claim about no explicit mention of property disposal…
Multiple claims are attributed to cited chunks but are absent from their text.
```

and the mechanism is visible in the one KB4 run that survived. Where the shipped
code wrote *"the records do not show any entries that explicitly mention the
marking, packaging, or chain of custody"*, the rules' arm wrote:

> It is clear from the records that the police have **followed the procedures
> for marking, packaging, and maintaining the chain of custody** for evidence.

**Rule 2's *"state your conclusion plainly and stand behind it"* turned an honest
negative into an unsupported compliance verdict, and rule 1 — written to *stop*
over-attribution — did not prevent it.** The verifier caught it four times of
four, which is the guarantee holding and the change failing in the same
measurement.

### 4c. A hallucinated answer is still rejected

`scripts/module82_forced_hallucination_control.py`, on the shipped (reverted)
code, KB4's literal gold question, 3 runs: the real `verify_grounding()` rejected
an invented `rule 27.41(3)`, an invented seven-year clock, an invented
`FIR 512/26` and a *"fully compliant"* conclusion **3 of 3**, and none of those
markers reached the served text. Full table and the judge's verbatim words in
`MODULE82_RESULT.md` §4c.

---

## 5. Gold comparison

**Gold (KB4):** Punjab Police Rules 1934 (Vol. III) **rule 27.16** binds the
police to keep a register of case property and unclaimed property in the
prescribed form, destroyable only three years after completion; and **our
malkhana register mirrors that structure — 45 entries, each carrying serial
number, registration number, entry date, description, quantity and condition.**

Judged on facts and ideas in the answer's own words, numbers within ~5–10%.

| Gold element | shipped code, 3 runs |
|---|---|
| A formal standard exists for recording case property | **3 of 3**, but attributed to the wrong instruments (Anti-Rape/Forensics packaging rules) |
| Punjab Police Rules rule 27.16 / the register | **0 of 3** — `…_c2197` and `…_c2421` in the window, unread |
| Destroyable only three years after completion | **0 of 3** — the "over three years / permanent record" chunk is at position 2 on every run |
| **45 register entries** | **0 of 3** (Module 86) |
| The register's own fields (serial no., reg. no., date, description, quantity, condition) | 0 of 3 — `…_c2421` is Form 27.18(1)'s column list and is never opened |
| Our records *do* record dispositions (13 forensic-lab, 7 heirs, 28 FIRs) | **3 of 3**, gold's own pair |

**KB4 is a partial: the data half's breakdown is right and its denominator is
missing; the norm half is answered from the wrong statute.** Module 27's 0.17 is
consistent with that. Neither arm moved it.

**Not tuned toward gold.** Nothing was worded toward gold's answer; the one
production edit attempted named no statute, provision, conclusion or figure, and
it was deleted after measurement.

---

## 6. Non-gold paraphrase

**KB4P**, Module 38's paraphrase 2, verbatim — Roman-Urdu, no gold vocabulary,
and the phrasing Module 38 measured to trip `_is_legal_kb_intent()`:

> *Kis qanoon ke tehat police ko maal-e-muqadma ka register rakhna parta hai, aur
> us property ko kitne arse baad tabah ya nilaam kiya ja sakta hai — kya hamare
> record mein iski paabandi nazar aati hai?*

**Shipped code, 3 of 3 answered, and it reaches gold's norm half every time:**

> The law requiring the police to maintain a register of case property is
> **Rule 27.16** of the **Punjab Police Rules**. This rule mandates that the head
> of the police prosecuting agency maintain a register of case property and
> unclaimed property in **Form 27.16(1)**. This register may be destroyed
> **three years after being completed** [Document 1].

Its window carries `…_c2209`, `…_c2197`, `…_c2195` and `…_c348` — an
all-Punjab-Police-Rules set — on 3 of 3 runs.

**Two things follow.** First, the capability is there: the same pipeline, same
corpus, same session gets gold's rule, gold's form and gold's three-year clock
from a differently-worded question — so KB4's norm-half failure is **Module 97's
retrieval split**, not a reasoning limit. Second, **Module 86 is not**: KB4P has
the total in its data-half chunk exactly as KB4 does, and it drops the 45 on 3 of
3 too. The denominator miss is independent of which chunks the norm half got.

Under the rules, KB4P dropped to **1 of 3** — the positive control regressing is
what made the revert unambiguous.

---

## 7. Regression guard

`MODULE82_RESULT.md` §7 in full: **17 of 17 shipped-code runs answered** across
KB1, KB5, KB6, KB8, KB9, CR3, G1, G6 and M2. Module 39's data halves survive
(KB9's, and KB4's own, which fires 6 of 6 here). Two Meta-Analysis verifier
rejections occurred and both were absorbed by Module 71's `PARTIAL` fallback.

**Nothing shipped, so the baseline cannot have moved**: `git diff origin/main --
src/` is empty and the 19 questions passing on all three passes of
`evaluation/gold32_pass{1,2,3}_results.json` are untouched.

---

## 8. New defects found

All four are written up in `MODULE82_RESULT.md` §8 and shared with it:

- **Module 97** — gold's own wording loses the provision its paraphrase
  retrieves, on **both** KB2 and KB4 (`…_c2209` 0 of 3 vs 3 of 3). The single
  most consequential finding for this question, and out of bounds here.
- **Module 37 confirmed load-bearing** on KB2, by id, on the shared store.
- **No deterministic pre-check guards the legal-KB path** — the LLM judge is the
  only hallucination guard for KB2/KB4-shaped questions.
- **Module 71's G6 citation defect fired unforced**, and its `PARTIAL` fallback
  held.

Specific to this document:

### 8f. Module 86 is unfixed, and the next attempt should not start at the prompt

Two facts constrain it. The total **is** in the chunk, first line, every run
(§1.3) — so it is not an upstream rendering gap and `xagg.py` does not need
touching. And the one prompt instruction that names it explicitly did **not**
produce it (0 of 5 with the clause, 0 of 6 without). A third restatement of the
same instruction is the least likely thing to work; the measurement that would
actually discriminate is whether the model ever states the total when the
breakdown is removed — i.e. whether the nine-row list is crowding it out — and
that is a `rag.py`/`xagg.py` rendering question, not a `semantic_search.py` one.

### 8g. KB4's window is not deterministic, and one measurement is not enough

Every KB4 run in both arms returned the same seven chunks, which reads as
determinism — but §4c's control run 3, same question and same code, returned a
**different** window including `…_c2209` at position 2. Any future KB4 claim
about "the window" needs several runs and the chunk ids printed, exactly as
Module 27 argued for three passes rather than one.

---

## Artefacts

Shared with `MODULE82_RESULT.md`: `module82_runs.json` (23 primary rows, both
arms), `module82_regression.json` (17 shipped-code rows), `module82_control.json`
(3 forced-control rows), and the five `scripts/module82_*.py`.
