# Module 52 — the relevance gate could not judge a non-English question against English statute text

**Branch:** `fix/kb-evaluator-english-rendering` · **Base:** `main` @ `a841f5d`
(Module 38 / PR #40 merged)
**Brief:** Module 52 in `GOLD_QA_REMAINING_FIXES_PLAN.md`
**Found by:** Module 42 (`MODULE42_RESULT.md` §1.2)
**Date of every measurement below:** 2026-09-09, worktree
`D:/Rapids AI/muhafiz-m52`, backend on `:8018`, `muhafiz-postgres` healthy
(73 cases), model-server tunnel `/health` = 200 throughout.

**Chroma:** a **private copy** of the vector store at
`D:/Rapids AI/muhafiz-m52/data/chroma_db` (`documents_in_store: 7716`,
confirmed at every backend boot). The shared store at
`D:/Rapids AI/Evidence Intelligence Platform/data/chroma_db` was neither read
nor written by this module. Nothing was ingested, re-embedded or re-indexed.
The full `pytest -q` suite was never run (it empties
`muhafiz_entity_descriptions`).

**`prompts/evaluator.txt` was NOT changed.** Module 19b's compound rule stands
untouched, and §1 explains why changing it would have been the wrong fix.
Every verdict below is that evaluator's own.

**Quota, checked with Module 42's own wider pattern**
(`grep -ciE "rate limit|RESOURCE_EXHAUSTED|429|quota" backend.log`): **0**
across the 24-run baseline sweep, **0** across the 24-run post-change sweep,
**0** across every probe. No run in this report is a rate-limited run.

**Concurrency:** one other backend (`:8016`, the Modules 53/57/40 track) was
live for parts of both sweeps. It was live for parts of **both** arms, so it
is not a differential explanation — but see §5's caveat on wall-clock
comparisons.

---

## 1. Root cause

The brief's hypothesis is **confirmed**, and re-measured on this branch rather
than inherited.

The legal KB corpus is seven **English** statute books. Module 30 gave
*retrieval* an English statute phrasing and gave the *cross-encoder* the same
phrasing to score against. It never gave anything to the **evaluator**, which
was the last component in that path still reading the raw Roman-Urdu or
Urdu-script question.

### 1.1 Layer 1, re-measured on this branch with this module's own rendering

`evaluate_relevance()` called directly (`scripts/module52_layer1_probe.py`),
`prompts/evaluator.txt` unmodified, temperature 0.0, the chunk set held
**constant** and **asserted** to contain gold's own statutory text
(`"safety on"` is present in the widened set — the probe asserts it rather
than assuming it). Chunk set = Module 42's IDEAL set, widened by
`expand_with_neighbors(window=1)` exactly as the live KB path does:

```
5_Forensics_guidelines_pdf_62ee00b3_c19   <- carries gold's statutory half verbatim
5_Forensics_guidelines_pdf_62ee00b3_c18
5_Forensics_guidelines_pdf_62ee00b3_c20
5_Forensics_guidelines_pdf_62ee00b3_c114
```

| question, same chunks | run 1 | run 2 | run 3 |
|---|---|---|---|
| KB6 as gold asks it (Roman-Urdu) | `relevant=False` | `relevant=False` | `relevant=False` |
| the same question, English rendering | `relevant=True` | `relevant=True` | `relevant=True` |

**0/3 against 3/3 on identical evidence**, reproducing Module 42's 1/6-vs-6/6
independently and with a rendering produced by the code that ships here rather
than by hand.

### 1.2 What the rendering actually produces

All eight gold KB questions, one call each:

| | language | rendering |
|---|---|---|
| KB1 | en | *returned verbatim, unchanged* |
| KB2 | en | *returned verbatim, unchanged* |
| KB3 | en | *returned verbatim, unchanged* |
| KB4 | ur | "When the police take possession of items related to a case, is there an established standard for how they are recorded and eventually disposed of — and does our property records follow that?" |
| KB5 | ur | "When domestic violence against a woman is involved in a case, does the law require a different procedure from a regular case — and if so, does our data show that those additional measures were actually taken?" |
| KB6 | roman_ur | "Are there any specific provisions in the forensics guidelines regarding how recovered weapons should be handled before they are registered, and does our weapon register record whether action has been taken on them?" |
| KB8 | roman_ur | "If an investigation in a case becomes lengthy, does the law require the police to report something to the court before the investigation is completed — and does our case-tracking data show whether this happened or not?" |
| KB9 | roman_ur | "When a person dies in suspicious circumstances, the police must conduct a thorough investigation into the cause of death — does our system record this anywhere, especially when so many of our cases involve death?" |

Both clauses survive in every compound case, and the three English questions
come back byte-identical, so nothing about what KB1–KB3's gate reads changes.

**One rendering is not faithful and is reported rather than hidden:** KB5's
"عورت پر تشدد" (violence against a woman) became "**domestic** violence
against a woman". The prompt forbids narrowing, and this narrows. It did not
cost KB5 anything measurable (§4: 3/3 answered in both arms, same statute), but
it is a real defect in the rendering and is filed in §8.

### 1.3 Why the fix is not in the prompt, and not a re-use of Module 30

Module 42 measured four cheaper wirings, all at Layer 1, and all four fail:
statute hypothesis as `rewritten_query` **1/3**; as both arguments **2/3**; the
live retry rewrite **0/3**; an English rendering *appended* to the original
**0/3**. Only a genuine English **question** reaches 3/3. Those are not
re-derived here — they are taken as measured, and §1.1 confirms the one cell
that matters on this branch.

`prompts/evaluator.txt` is not the place either: Module 42's 2×2 shows the
Roman-Urdu **norm-only** cell (the compound clause deleted — the exact clause
Module 19b's rule and Module 39 are about) is the **worst** cell, 0/3. Removing
compoundness makes it worse, so compoundness is not what fails.

---

## 2. Change

| File | Why here |
|---|---|
| `prompts/question_english.txt` (new) | The translation contract: return the same question in English, keep every clause, add no statute name or legal term the question did not use, return an English question unchanged. |
| `src/pipeline/statute_hypothesis.py` | `render_question_in_english()` — sits beside `generate_statute_queries()` because it is the same *kind* of thing (an English string for machine consumers, never shown to a user, `None`/`[]` on every failure) and shares its Arabic-script guard and truncation reasoning. It is a separate function because it has a different consumer and a different requirement: the hypotheses paraphrase the **provision**, this paraphrases the **question**, and §1.3 shows the provision phrasing does not work in the gate. |
| `src/pipeline/harness/tools/rag.py` | The wiring: generated once per request in `rag_tool()` under `_is_legal_kb_intent()` (so the KB-only→mixed scope retry does not pay a second LLM call), folded into `_retrieve_candidates()`'s variant list, and handed to `evaluate_relevance()`. |
| `tests/test_kb_statute_retrieval.py` | 18 new tests (§3). |
| `tests/test_harness_tool_rag.py` | The `_spy_retrieve` stub gains the new keyword and stubs the rendering to its failure return, so those tests keep asserting exactly the scope behaviour they were written for. |
| `scripts/module52_kb_runs.py`, `scripts/module52_layer1_probe.py` (new) | The measurement harnesses for §4 and §1. Kept as artefacts rather than as prose. `evaluation/` was not touched. |

**What the gate is handed, exactly.** On attempt 1 **both** arguments are the
English rendering — §1.3's 2×2 reaches 3/3 only when the field the gate reads
holds a genuine English question and nothing else. On a retry the search query
has genuinely moved on, so `rewritten_query` carries the retry rewriter's own
output (already English) and only the question field stays substituted.

**Degradation.** `english_query` is `None` for every non-legal-KB query and on
any rendering failure — a dead LLM, unparseable output, an empty string, an
Arabic/Urdu-script response. Both call sites then behave byte-for-byte as they
did before this module. `prompts/evaluator.txt`, `meta_analysis.py`,
`verifier.py`, `supervisor.py`, `xagg.py`, `router.py`, `xnetwork.py` and
`evaluation/` are untouched.

**Cost.** One extra small LLM call per legal-KB question, and one extra
embedding for the extra retrieval variant. Both are inside
`_is_legal_kb_intent()`; a case-narrative question pays neither.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_kb_statute_retrieval.py tests/test_harness_tool_rag.py -q
89 passed in 22.32s
```

Adjacent suites, run to catch blast radius on the shared RAG path
(`test_harness_agent_case_summarization`, `..._investigative_analysis`,
`..._local_search`, `..._semantic_search`, `test_harness_tool_graph`,
`test_harness_tool_local_search`, `test_orchestrator`, `test_pipeline`):
**219 passed, 1 xpassed**. The full `pytest -q` suite was deliberately not run.

18 of the 89 are new, and the brief's required pin is
`test_non_english_kb_question_reaches_the_evaluator_in_english`, parametrized
over the **five** gold KB questions that are not asked in English — KB4/KB5
(Urdu script) and KB6/KB8/KB9 (Roman-Urdu), each against its literal gold
text — asserting that `evaluate_relevance()` is called with the English
rendering in both arguments and that the raw question reaches the gate in
neither. Its counterpart,
`test_case_narrative_question_is_unaffected`, asserts a plain case-data
question makes no rendering call at all and hands the gate exactly the two
strings it saw before Module 52.

The rest pin: an already-English gold question is rendered to itself and not
embedded twice; a `None` rendering degrades to the pre-Module-52 arguments and
adds no retrieval variant; a retry puts the rewrite in `rewritten_query` while
the question field stays English; and `render_question_in_english()`'s own
contract — the model's question returned, `None` on unparseable output / empty
string / LLM exception / an Urdu-script response, no LLM call at all for an
empty question, and truncation rather than discard for an over-long response.

---

## 4. Live verification — all eight KB questions, three runs per arm

Two full sweeps of KB1–KB9 (there is no KB7), 3 runs each, sequential, same
backend, same private Chroma, same session shape as `gold32_run.py`
(platform-admin, All Cases, no `case_id`). `route=` is read from the
`supervisor:dispatch` SSE event; `rounds` is the number of
retrieve→rerank→evaluate rounds, read from `backend.log`'s
`N chunk(s) to evaluator (attempt K)` lines for exactly the window each
request occupied.

**Chunk ids were captured for every attempt of every run, never citations** —
Module 30 recorded an evaluator citing "section 174" that was not in the chunks
it judged, so answer text cannot be trusted about what was actually read. The
full per-attempt id lists are in `docs/gold-qa-wave2-results/module52_runs.json`.

### 4.1 The re-baselined BEFORE numbers (this is not Module 42's baseline)

Module 38 changed KB behaviour and Module 42's numbers predate it, so the
baseline below was measured on this branch's base commit, not inherited.

| | before: route / rounds / runtime / outcome |
|---|---|
| KB1 r1/r2/r3 | RAG/1/176.0s/answered · RAG/1/189.4s/answered · RAG/1/200.5s/answered |
| KB2 r1/r2/r3 | RAG/1/179.6s/**refused** · RAG/1/162.3s/answered · RAG/1/109.0s/**refused** |
| KB3 r1/r2/r3 | **XAGG**/0/56.3s/answered · RAG/6/435.0s/answered · RAG/6/295.0s/**abstained** |
| KB4 r1/r2/r3 | RAG/1/270.8s/answered · RAG/1/153.6s/answered · RAG/1/100.9s/**refused** |
| KB5 r1/r2/r3 | RAG/1/237.0s/answered · RAG/1/152.9s/answered · RAG/3/201.1s/answered |
| KB6 r1/r2/r3 | RAG/3/373.1s/answered · RAG/2/168.7s/answered · RAG/1/124.3s/answered |
| KB8 r1/r2/r3 | RAG/2/217.7s/answered · RAG/2/260.7s/answered · RAG/3/397.9s/answered |
| KB9 r1/r2/r3 | RAG/6/502.4s/**abstained** · RAG/3/280.3s/answered · RAG/6/478.6s/**abstained** |

**18 of 24 runs produced an answer. Mean 2.25 evaluator rounds, mean 238.5s.**
"refused" is the verifier's *"The generated answer could not be verified as
grounded in the retrieved documents"*; "abstained" is the RAG tool's own
*"No sufficiently relevant documents were found"*.

Three things in that table are worth stating plainly, because they contradict
things currently written down:

- **KB6 no longer has the 4-of-5 abstention Module 42 measured.** It answers
  3/3 here. Module 38 was right that fusion gave it a window; the brief's
  warning that this does not mean Module 52 is solved is also right — see §5.
- **KB3 is not a stable RAG question at all.** It routed to **XAGG** on run 1
  and to RAG on runs 2 and 3, and RAG abstained on one of those. Any
  single-run number for KB3 is a coin flip.
- **KB9 abstains 2 of 3**, at 6 rounds and ~500s — the exact
  six-round/timeout-pressure signature Module 42 documented for KB6, now
  sitting on KB9.

### 4.2 AFTER, same eight questions, three runs each

| | after: route / rounds / runtime / outcome |
|---|---|
| KB1 r1/r2/r3 | RAG/1/94.9s/answered · RAG/1/96.6s/**refused** · RAG/1/99.0s/answered |
| KB2 r1/r2/r3 | RAG/1/83.4s/answered · RAG/1/87.8s/answered · RAG/1/88.0s/answered |
| KB3 r1/r2/r3 | RAG/6/320.5s/**abstained** · RAG/4/232.3s/answered · RAG/5/277.3s/answered |
| KB4 r1/r2/r3 | RAG/1/109.1s/**refused** · RAG/1/117.0s/**refused** · RAG/1/113.8s/**refused** |
| KB5 r1/r2/r3 | RAG/6/352.2s/answered · RAG/3/200.4s/answered · RAG/3/228.7s/answered |
| KB6 r1/r2/r3 | RAG/1/94.7s/answered · RAG/1/90.8s/answered · RAG/1/90.9s/answered |
| KB8 r1/r2/r3 | RAG/2/150.8s/answered · RAG/2/152.0s/answered · RAG/2/167.6s/answered |
| KB9 r1/r2/r3 | RAG/2/161.6s/answered · RAG/4/235.3s/answered · RAG/5/313.1s/answered |

**19 of 24 runs produced an answer. Mean 2.33 evaluator rounds, mean 164.9s.**

### 4.3 The named winner and the named loser

**KB9, before → after: abstained 2/3 → answered 3/3.** The gate's own words,
run 1 of each arm, on the same question:

- before, six consecutive refusals, e.g. *"The retrieved documents describe
  procedural requirements under CrPC 174/176 …"* — then abstain.
- after, attempt 1 `relevant=False`, attempt 2 `relevant=True`: *"The retrieved
  documents describe the procedural requirement for police to conduct…"*

**KB4, before → after: answered 2/3 → refused 3/3.** This is a real
worsening and it is not hidden. §5.2 has the isolation experiment that says
what caused it — and it is not the gate.

---

## 5. Gold comparison, and the attempts/runtime cross-check

### 5.1 Thematic coverage against gold

Judged on facts and ideas, not wording, per the brief's standard. Gold's KB
answers all have two halves: **L** — the governing provision and its
substance; **D** — what our own data does or does not record. A run scores L
if it names the right instrument and its substance in any words.

| | gold's L | before L | after L | before answered | after answered |
|---|---|---|---|---|---|
| KB1 | CrPC s.154 — written, read back, signed, entered in a register | 3/3 | 2/3 | 3/3 | 2/3 |
| KB2 | QSO Arts 38/39 + CrPC s.162 — police-recorded statements not usable, so by design | **0/3** | **0/3** | 1/3 | 3/3 |
| KB3 | Police Order 2002 Art. 18 — separate Investigation Wing, DPO may not interfere | 1/3 | 2/3 | 2/3 | 2/3 |
| KB4 | Punjab Police Rules 27.16 — case-property register, destroyed only 3 years after completion | 2/3 | 0/3 | 2/3 | **0/3** |
| KB5 | Anti-Rape Rules 2022 r.3(2) — woman police officer, statement at ARCC, chain of custody | 3/3 | 3/3 | 3/3 | 3/3 |
| KB6 | packaged separately, unloaded, safety on, no live rounds in chamber/magazine/parcel | 1/3 | 1/3 | 3/3 | 3/3 |
| KB8 | CrPC s.173 — 14 days, interim report to magistrate via prosecutor within 3 days | 3/3 | 3/3 | 3/3 | 3/3 |
| KB9 | CrPC s.174 — inquest, report on apparent cause of death to nearest magistrate | 1/3 | 3/3 | 1/3 | 3/3 |
| **total** | | **14/24** | **14/24** | **18/24** | **19/24** |

**Honest headline: the module moved consistency, not correctness.** L is
**14/24 in both arms**. KB9 gained 2 and KB3 gained 1; KB1 lost 1 and KB4 lost
2. Answer *availability* went 18/24 → 19/24, and three questions
(KB2, KB6, KB9) went from mixed to 3/3 — but KB4 went from mixed to 0/3.

The **D** half is where gold is barely met at all, in either arm. Gold's data
halves ("68 of 74 pairs", "45 malkhana entries", "8 reports") come from the
case database; the RAG route reads the statute corpus and answers the data half
with *"the documents do not confirm whether…"*. That is **not** a pass under
the brief's standard, because gold does not agree that the data is silent — it
gives the number. This is Module 39's subject, and Module 52 neither fixes nor
worsens it: **0 of 48 runs across both arms produce gold's data half.**

The one place the brief's "correctly stating the data lacks something, where
gold agrees, is a pass" rule applies is KB8's second half and KB6's second
half, where gold itself says the schema has no such field. Both arms get those.

### 5.2 KB4's regression — isolated, and it is not the gate

KB4 refuses 3/3 after the change. Two candidates: the English rendering
changed *retrieval* (an extra embedded variant), or it changed what the gate
passed through to generation.

Probe: a third arm with the rendering removed from `_retrieve_candidates()`
and left **only** in `evaluate_relevance()`, 3 runs:

| KB4 | rounds | runtime | outcome |
|---|---|---|---|
| gate-only run 1 | 1 | 110.6s | refused |
| gate-only run 2 | 1 | 109.8s | refused |
| gate-only run 3 | 1 | 113.1s | refused |

Still 0/3, so the retrieval fold is not the cause. And the gate is demonstrably
reading the right thing — its own verdict in all three runs names gold's rule:
*"The retrieved documents from Punjab Police Rules III (rules 22.16, **27.16**,
and 27…"*. The final chunk set in all three gate-only runs is **identical** to
the baseline's own run 3, `c334 · c1645 · c2197 · c2209` plus two Anti-Rape
chunks — and baseline run 3 **also refused**.

So: retrieval reaches gold's rule 27.16, the gate accepts it, and the
**verifier** rejects the answer generated from it. That is exactly what Module
38 recorded for KB4 — "retrieval is fixed and *generation* is now the binding
constraint" — and it is downstream of everything Module 52 touches. What
Module 52 appears to have done is make the previously-variable retrieval
**deterministic** on a chunk set that was already one of the baseline's own
failing sets. Filed in §8; not fixed here.

### 5.3 The cross-check the brief asked for: does the gate collapse the rounds?

Partly, and the honest answer is that it holds where the diagnosis applies and
does not hold elsewhere.

| | rounds before | rounds after |
|---|---|---|
| KB6 | 3, 2, 1 | **1, 1, 1** |
| KB8 | 2, 2, 3 | **2, 2, 2** |
| KB9 | 6, 3, 6 | **2, 4, 5** |
| KB1 | 1, 1, 1 | 1, 1, 1 |
| KB2 | 1, 1, 1 | 1, 1, 1 |
| KB4 | 1, 1, 1 | 1, 1, 1 |
| KB3 | 0, 6, 6 | 6, 4, 5 |
| KB5 | 1, 1, 3 | **6, 3, 3** |

- **KB6 collapses exactly as predicted** — every run passes on attempt 1, and
  runtime falls 373/169/124s → 95/91/91s. Module 42's predicted "~600s → ~250s"
  is beaten.
- **KB9 improves** (mean 5.0 → 3.7 rounds, 420s → 237s) and stops abstaining.
- **KB5 gets worse** (mean 1.7 → 4.0 rounds). KB5 is Urdu-script and its
  rendering is the unfaithful one (§1.2) — the most likely explanation, and it
  is a lead rather than a measurement.
- **Mean rounds across all 24 runs is flat: 2.25 → 2.33.** Mean runtime falls
  238.5s → 164.9s, but a second backend was live for uncontrolled parts of both
  sweeps, so **the wall-clock number is not a clean measurement** and should not
  be quoted on its own. The per-question round counts are.

Applying the brief's own test — *"if answers improve but attempts/runtime do
not, the diagnosis is incomplete"* — the diagnosis is **correct but partial**.
The gate was genuinely refusing English-answerable evidence and it stopped;
KB6's and KB9's round counts prove it. But the *bucket* did not get better,
because on KB6 the gate now passes on attempt 1 over a chunk set that does
**not** contain `c19`:

```
after, KB6, all 3 runs, final chunk set:
  5_Forensics_guidelines_pdf_62ee00b3_c116 · _c0 · Punjab-Police-Rules-III c1279 · c477 · …
before, KB6 run 2 (the one run that quoted gold's specifics):
  5_Forensics_guidelines_pdf_62ee00b3_c18 · _c116 · _c0 · …
```

`c18` widens to carry gold's *"unloaded … safety on … no live rounds"*; `c116`
does not. So Module 52 removed the retries that occasionally *stumbled onto*
`c18`, and KB6's statutory specifics went from "1 run in 3 by luck" to
"0 runs in 3 reliably" while its answer rate went 3/3 → 3/3 and its runtime
fell by 65%. **A gate fix cannot compensate for a retrieval miss, and this one
does not.** The brief's own prediction — that the same rendering reaching
retrieval would surface `c19` — did **not** come true.

---

## 6. Non-gold paraphrase

Two were used, and the first one produced a finding of its own.

**Paraphrase A** (`KB6P`): *"Kya forensics ke usoolon mein yeh wazeh kiya gaya
hai ke baramad shuda hathyar ko record mein laane se pehle kis tarah sambhala
aur band kiya jaye, aur kya hamare hathyar wale register se pata chalta hai ke
aisa kiya gaya ya nahi?"* — deliberately keeping the hard vocabulary
("baramad shuda hathyar") rather than Module 42's "zabt shuda pistol" loanword,
which is the phrasing already known to pass.

| | before | after |
|---|---|---|
| KB6P r1/r2/r3 | abstained · abstained · abstained | abstained · abstained · answered |

**This result is void as a test of Module 52, and saying so matters more than
the numbers.** `_is_legal_kb_intent()` returns **False** for this paraphrase
(measured directly), so it never reached the legal-KB path in either arm: no
rendering was generated (`english_rendering: []` in every run), the KB-only
scope was never tried, and every chunk the evaluator saw was an FIR narrative
(`psrms_fir_fir-417-26#narrative_…`), not statute text. Both arms measured the
same code path. Filed as a new defect in §8.

**Paraphrase B** (`KB6Q`): *"Kya qanoon ya guidelines yeh kehti hain ke baramad
shuda hathyar ko record mein laane se pehle kis tarah sambhala jaye, aur kya
hamara register yeh darj karta hai ke aisa kiya gaya?"* — same hard vocabulary,
and confirmed (measured) to trip `_is_legal_kb_intent()`. The before arm was
re-run against the base commit's `rag.py`/`statute_hypothesis.py` on the same
backend, so this is a like-for-like comparison.

| | rounds | runtime | outcome |
|---|---|---|---|
| before r1 | 4 | 204.1s | answered — generic "documented, labeled, marked, photographed" |
| before r2 | 6 | 278.1s | **abstained** |
| before r3 | 5 | 247.9s | answered — generic, same |
| after r1 | 5 | 275.9s | answered — generic |
| after r2 | 2 | 134.2s | answered — **"packaged separately, unloaded with safety on, and without live rounds in the chamber, magazine, or parcel"** |
| after r3 | 2 | 136.3s | answered — **the same three specifics** |

Rendering used in all three after-runs: *"Does the law or guidelines specify
how to handle seized weapons before recording them, and does our register
record that this was done?"*

**This is the module's strongest evidence.** On a question gold never saw:
answered 2/3 → 3/3, mean rounds 5.0 → 3.0, mean runtime 243s → 182s, and
gold's three statutory specifics — the ones KB6's own gold wording produced on
**zero** of the six after-runs — appear on 2 of 3 after-runs and **0 of 3**
before-runs. The capability is real; what §5.3 shows is that KB6's own gold
phrasing does not reliably reach the chunk that carries it.

---

## 7. Regression guard

- **All eight KB questions, both arms, 3 runs each** — that is §4, and it is
  the regression guard as well as the verification, because the change sits on
  the shared RAG path.
- **The non-legal path**: `test_case_narrative_question_is_unaffected` pins
  that a case-data question makes no rendering call and hands the gate the same
  two strings as before. Live, KB3 run 1 of the baseline routed to **XAGG** and
  was unaffected either way.
- **Adjacent suites**: 219 passed, 1 xpassed across the eight test files that
  touch `tools/rag`, `evaluate_relevance`, `_retrieve_candidates` or
  `statute_hypothesis` (§3). No change to `test_harness_tool_rag.py`'s
  assertions was needed beyond teaching its stub the new keyword.
- **Regressions found and reported rather than smoothed over**: KB4
  (answered 2/3 → 0/3, isolated to generation/verification in §5.2), KB1
  (3/3 → 2/3, one verifier refusal), KB5 (mean rounds 1.7 → 4.0), and KB6's
  loss of the `c18` window described in §5.3.

---

## 8. New defects found, deliberately not fixed here

1. **`_is_legal_kb_intent()` misses an ordinary Roman-Urdu paraphrase** — filed as **Module 58**.
   Measured: the paraphrase in §6A returns `False` and is routed to the mixed
   FIR-narrative pool, never to the KB corpus, so *every* KB fix — Module 8c's
   scope narrowing, Module 30's statute hypotheses, Module 38's fusion and this
   module's rendering — is silently skipped. The gate's patterns key on
   English/loanword surface forms ("forensics guidelines", "qanoon") and a
   legitimate Roman-Urdu rephrasing ("forensics ke usoolon") falls through.
   This gates the whole Roman-Urdu half of the KB bucket ahead of everything
   else in the path and should be the next module in this line.

2. **KB6's `c19`/`c18` window is still not retrieved reliably** (§5.3) — filed as **Module 59**. The
   brief predicted the English rendering in retrieval would fix it; measured,
   it does not — all three after-runs land on `c116` instead. The chunk that
   carries gold's answer is one `expand_with_neighbors` hop away and the
   pipeline does not get there from KB6's own wording, only from paraphrase B's.

3. **KB4: the verifier rejects an answer generated from the correct rule.**
   No new module — this belongs to Module 38's own open KB4 note rather than a
   duplicate of it. Isolated in §5.2 — retrieval reaches Punjab Police Rules 27.16, the gate
   names it, and the answer is refused as ungrounded, 6 runs out of 6 after the
   change and 1 of 3 before it. Downstream of Module 52; overlaps Module 38's
   own open KB4 note.

4. **The rendering can narrow a question's scope** (§1.2, KB5: "violence" →
   "domestic violence") — filed as **Module 61**. It breaks
   `prompts/question_english.txt`'s own rule 4. KB5's rounds
   also worsened (1.7 → 4.0 mean). Worth a targeted prompt fix and a
   faithfulness check, not worth blocking this module.

5. **KB2's gold is not reachable from the corpus by any run in either arm**
   (0 of 6) — filed as **Module 60**. Every run answers from CrPC s.161 / case-diary material; gold's
   answer rests on Qanun-e-Shahadat Arts 38/39 and CrPC s.162. That is a
   Module-30-shaped retrieval gap on a question Module 30 never covered.

---

## 9. Verdict on Modules 48, 49 and 39 (required assessment)

The brief asks whether Module 52 subsumes these. **It subsumes none of them.**
It advances one half of Module 48 and leaves the rest exactly where it was.
Each verdict below rests on this report's own 48 runs, not on an opinion.

### Module 39 — the "and does our data show it?" half — **still needed, entirely unchanged**

§5.1 is decisive: **0 of 48 runs, across both arms, produce gold's data half.**
Every run answers it with some form of *"the documents do not confirm
whether…"*, because the RAG route reads the statute corpus while gold's numbers
("68 of 74 pairs", "45 malkhana entries", "8 reports", "32 weapons on record")
live in the case database. Module 52 changed the *language* the gate reads; it
changed nothing about which corpus the data half is answered from. Module 42
had already measured that deleting the data clause makes the gate verdict
*worse* (0/3, the weakest cell), so the two modules do not overlap at all.
**Not subsumed. Not advanced. It is now the single largest remaining cause of
KB failure**, and on this evidence it blocks KB1, KB3, KB4, KB5, KB6, KB8 and
KB9 simultaneously.

### Module 48 — KB2 and KB9, "retrieval fixed, synthesis still wrong" — **half advanced, half still needed, and re-diagnosed**

Module 48 bundles two questions that turn out to have nothing in common, and
this report separates them.

**KB9: substantially advanced, and Module 48's own hypothesis is now
answerable.** Module 48 asks whether KB9 "is reasoning from Punjab Police Rules
25.31's cross-reference rather than from CrPC s.174 itself, and whether that is
why it misses gold". Measured here, before the change KB9 **abstained 2 of 3
runs** — it was not mis-synthesising, it was producing no answer at all, at 6
evaluator rounds and ~500s. After the change it answers **3 of 3**, and the law
half is covered 3/3 against 1/3: it names CrPC s.174, Rule 25.31 **and** Rule
25.35's inquest report in duplicate (Forms 25.35(1) A/B/C) with the apparent
cause of death and marks of violence — which is gold's L half in gold's own
terms. Per §4, chunk ids confirm this is grounded in
`4_Punjab-Police-Rules-III_pdf_68bb5d0d_c1542/c1547/c1559` **plus** genuine
`1_1898_Code_of_Criminal_Procedure` chunks, so the answer is no longer resting
only on the cross-reference. What KB9 still misses is the data half, and that
is Module 39, exactly as Module 48 suspected it might be. **KB9's share of
Module 48 should be closed as "was an abstention, not a synthesis failure —
fixed by 52; the residue is 39."**

**KB2: not advanced at all, and now diagnosed.** Module 48 records that KB2 has
"no recorded diagnosis". Here is one. KB2's answer *availability* improved
(1/3 → 3/3 answered), and its thematic coverage did **not** move: **0/3 in both
arms**, 0 of 6 runs total. Gold's answer rests on **Qanun-e-Shahadat Order 1984
Arts 38 and 39** (a confession to a police officer is not evidence) and **CrPC
s.162** (a police-recorded statement is barred at trial), from which gold
concludes the missing statement text is *by design, not a gap*. Every one of
the six runs instead answers from **CrPC s.161 and Punjab Police Rules 25.54's
case diaries**, and several conclude the opposite of gold — that the system
*does* have recording mechanisms. So KB2 is not a synthesis failure either: it
is a **retrieval** failure of exactly Module 30's shape, on a statute book
(Qanun-e-Shahadat) Module 30's own probe never targeted. **Still needed, and it
is a Module-30-style retrieval module, not a synthesis one.**

### Module 49 — KB3 quotes Article 18 without drawing gold's conclusion — **still needed, unchanged, and its own predicted outcome is now supported**

KB3's law half went 1/3 → 2/3, which is inside this question's own noise, and
it never draws gold's conclusion in either arm. Three findings for Module 49 to
start from rather than re-derive:

1. **KB3 is not reliably a RAG question.** Baseline run 1 routed to **XAGG**
   (56.3s, "the provided data does not include information about the roles of
   officers"), runs 2 and 3 to RAG. Module 49's plan assumes the RAG path; it
   needs to account for the router first, or its measurements will be coin
   flips.
2. **The Article 18 material does arrive**, in both arms — the after-arm answers
   name the Investigation Wing as a distinct unit and the officer in charge's
   power to depute a subordinate — but they read it as *"the law does not
   require the same officer"* (permission) rather than gold's *"the law expects
   separation"* (design). That is a genuine synthesis gap and Module 49 owns it.
3. **Module 49's own stated honest outcome is now supported by measurement.**
   It says "if gold's 68-of-74 comparison is unreachable from the RAG path, KB3
   cannot pass without Module 39, and this module's honest outcome is to say
   that and stop." §5.1 measures that half as unreachable on **24 of 24** runs.
   Module 49 should be sequenced **after** Module 39, not before it.

### Module 52 itself

**Done, and honestly partial.** The defect it names is real and is fixed — the
gate no longer refuses English-answerable statute text because the question was
asked in Roman-Urdu (§1.1: 0/3 → 3/3 on identical chunks; §6B: gold's three
statutory specifics on 2 of 3 runs where the baseline got 0 of 3; §5.3: KB6's
six rounds collapse to one, runtime down 65%). It is not, on its own, worth a
point in the KB bucket: thematic L coverage is **14/24 in both arms**, and the
binding constraints are now the intent gate (§8.1), retrieval (§8.2, §8.5) and
Module 39 — not the evaluator.
