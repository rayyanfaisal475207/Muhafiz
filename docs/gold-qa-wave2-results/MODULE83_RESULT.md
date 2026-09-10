# Module 83 — Meta-Analysis' synthesis intermittently cites nothing at all

Branch `fix/synthesis-citation-durability`, worktree `D:/Rapids AI/muhafiz-m83`,
branched from `origin/main` at `de8cc71`.

**Module 70 is covered in §9 of this file. It is diagnosed, not fixed, and
deliberately so — see that section.**

> **Read this first.** The filed diagnosis is wrong, and saying so is the
> most useful thing in this file. Module 83 was filed as *"the citation rule
> is held only by its position in the prompt"*. It is not. Every refused G6
> synthesis captured live on this branch is a **degenerate repetition loop**
> — the model emitting one phrase several hundred times until it hits
> `max_tokens`. `verifier.py`'s *"substantial … but cites no `[Document N]`
> source at all"* refusal is not a false positive; it is the **only gate**
> catching a broken generation, and it is doing its job. Restating or moving
> the citation rule could not have helped: there is no prose in the
> collapsed text to cite.

---

## 1. Root cause

### 1.1 Module 71's three conditions, reproduced

Module 71 recorded G6 no-citation refusals at **0 of 4** pre-fix, **10 of 12**
with five deterministic lines interleaved into `_format_subanswers_for_prompt`,
and **0 of 8** once removed. Reproduced here on `origin/main` at `de8cc71`,
with the same control (Module 71's deleted per-document figure roster,
reinstated verbatim as one extra line under each sub-answer):

| arm | code | sub-answers section | G6 runs | **"cites no `[Document N]`" refusals** | CR3 runs | CR3 refusals |
|---|---|---|---|---|---|---|
| **A** | `origin/main` | as shipped | 4 | **3** | 4 | 0 |
| **B** | `origin/main` | **lengthened (control)** | 4 | **4** | 4 | 1 *(a grounding rejection, not this one)* |

Two things follow, and only one of them matches what was filed.

* **The prompt-length sensitivity is real and reproduces.** Lengthening the
  sub-answers section took G6 from 3 of 4 to 4 of 4 refusals, and it never
  moved CR3's — the same direction Module 71 measured.
* **Module 71's baseline does not reproduce.** It measured **0 of 4** on
  unmodified code; the same question on current `main` refuses on **3 of 4**,
  and across every baseline arm in this session **12 of 16** G6 runs
  (A 3/4, B 4/4, plus two instrumented probes at 1/2 and 5/6). `main` has
  moved a long way since Module 71 (PRs up to #60). G6 is not an
  occasionally-flaky question any more; it is a mostly-failing one, which is
  consistent with it scoring 0.30 on all three of Module 27's passes.

### 1.2 What the refused synthesis actually contains

The filed diagnosis assumes a good answer that forgot to cite. So the
synthesis text was logged **before the Verifier sees it**, over 20 live G6
runs. Every refused synthesis is the same thing:

```
tokens 758 | unique tokens 18 | unique ratio 0.024
one 4-gram repeated 370 times | zero digits | 6,764 characters
byte-identical across six separate runs
```

It reads, in full, as: *"Naye tainaat hone wale afsar ko mojooda case load se
yeh tawaqqo rakhna chahiye ke kismat-e-murad ki kismat-e-murad ki
kismat-e-murad ki …"* — that fragment repeated to the token cap.

The runs that **pass** measure 235–267 tokens at a **0.57–0.69** unique
ratio, top 4-gram count 2, and cite `[Document 1]`–`[Document 5]` correctly,
one per sub-answer. There is no overlap between the two populations on
either axis.

Corroboration from the deterministic side: on all four arm-C runs, the
Module 83 diagnostic logged `Answer figures: []` — the refused synthesis
states **no number at all**, while its five sub-answers between them state
forty. A Roman-Urdu orientation note that mentions none of its own
aggregates' figures is not an answer that forgot its citations.

### 1.3 Why a plain retry cannot work, and what does

`call_llm` defaults to `temperature=0.0`. That is why six separate runs
produced the **same 6,764 bytes**: greedy decoding walks into the same loop
every time. Re-asking the same model the same prompt at the same temperature
reproduces the collapse exactly — which is also why "the rate is
prompt-length-sensitive" is true without the citation rule being involved:
what the extra lines change is whether *this particular prompt* is one that
collapses, not whether the model remembers a rule stated 40 lines earlier.

What breaks a greedy repetition loop is decoding differently.

---

## 2. The change

All of it is in `src/pipeline/harness/agents/meta_analysis.py`.

| Change | Why |
|---|---|
| **`_repetition_profile()` / `_is_degenerate()`** — deterministic, no model call. A generation is degenerate iff it is ≥ 60 tokens AND its unique-token ratio is < 0.15 AND some 4-gram repeats ≥ 10 times. | Both thresholds sit in the gap the live captures leave (collapses 0.024 / 370; healthy 0.57–0.69 / 2). Whitespace tokens, so it behaves the same on Urdu, Roman-Urdu and English. |
| **One regeneration at `temperature=0.4`** when the synthesis is degenerate. | The collapse is deterministic at 0.0, so the retry must decode differently or it is pointless. 0.4 and not higher: the aim is to leave the greedy path, not to trade a repetition loop for an invention. |
| **A twice-collapsed synthesis is never served AND never verified.** It routes into Module 71's existing deterministic sub-answer composition. | Spending an LLM grounding call on 6,764 characters of one repeated phrase buys latency and quota and nothing else. The user-visible outcome is what the Verifier's refusal already produces — reached by naming the actual defect. |
| **The fallback caveat now distinguishes the two reasons** — *"did not generate cleanly"* vs *"could not be verified as grounded"*. | They are genuinely different failures and the user is told which one applies. |
| **`_attach_provenance()`** — when a synthesis carries no `[Document N]` at all, re-attach provenance deterministically: a sentence is cited to document *i* only when **every** figure it states is stated by document *i* and *i* is the **only** such document. Ambiguous, figure-free and fabricated-figure sentences get nothing; if nothing can be attributed the answer is returned unchanged and the Verifier refuses it exactly as today. | This is the **secondary** change and it is honestly reported as such: **it fired zero times in 20 live G6 runs**, because the real failure there is the collapse. It covers the case Module 83 was *filed* for — a substantial, correct, marker-free synthesis — using the figure roster `_misattributed_figures()` already computes, so provenance stops depending on the model's recall of a prompt rule. |

### What was deliberately NOT changed

* **`verifier.py` — untouched.** `_check_no_citation` is not relaxed,
  narrowed or bypassed. Module 29's and Module 25's findings are the reason:
  it stops ungrounded prose being served as evidence, and here it is the only
  thing standing between a repetition loop and the user. This module's
  evidence *strengthens* the case for it.
  **`verifier.py` is Module 85's territory (`muhafiz-kbgen`) and was avoided
  for that reason as well** — no edit to it was needed or made.
* **`_format_subanswers_for_prompt` — untouched**, and Module 71's
  wording-lock test on it still passes. Nothing was added to the sub-answers
  section, and nothing was moved out of it.
* **`_SYNTHESIS_SYSTEM_PROMPT_TEMPLATE` — untouched.** No fourth restatement
  of the citation rule, no repositioning of the third. The measurement says
  the rule is not what fails.
* **No retry on verifier rejection.** Module 71 pinned that closed and it
  stays closed — `test_module71_the_fallback_makes_no_second_llm_call` passes
  unchanged. The regeneration added here fires only on a deterministically
  detected collapse, never on a rejection, and never twice.
* **`large_scale_aggregate.py` / `xagg.py` — untouched** (see §9).

### Overlap with other live tracks

Only `src/pipeline/harness/agents/meta_analysis.py` and its test file are
modified. `muhafiz-kbgen` owns Modules 82/85/86 (`verifier.py`, KB retrieval
windows, KB4's denominator) and a separate track owns `router.py`/`rag.py`;
none of those files is touched here. **No file in this change is believed to
overlap another live track.**

---

## 3. Unit tests

```
PYTHONPATH=. ".../.venv/Scripts/python.exe" -X utf8 -m pytest \
    tests/test_harness_agent_meta_analysis.py tests/test_verifier.py \
    tests/test_validation.py tests/test_citation_consistency.py \
    tests/test_harness_supervisor.py tests/test_pipeline.py -p no:randomly
```

**445 passed, 0 failed** (430 before this module; **15 new**, all in
`tests/test_harness_agent_meta_analysis.py`).

**Both directions were checked, per test.**

| Test | Pre-change | Post-change |
|---|---|---|
| `test_module83_the_live_collapse_is_detected_and_a_healthy_answer_is_not` | fails | passes |
| `test_module83_a_long_legitimate_list_is_not_degenerate` | fails | passes |
| `test_module83_a_short_answer_is_never_called_degenerate` | fails | passes |
| `test_module83_a_collapsed_synthesis_is_regenerated_once` | fails | passes |
| `test_module83_a_twice_collapsed_synthesis_is_never_served_or_verified` | fails | passes |
| `test_module83_an_uncited_sentence_gets_the_document_that_uniquely_states_it` | fails | passes |
| `test_module83_a_figure_two_sub_answers_state_is_left_uncited` | fails | passes |
| `test_module83_a_fabricated_figure_is_never_given_a_citation` | fails | passes |
| `test_module83_a_sentence_stating_no_figure_is_left_alone` | fails | passes |
| `test_module83_an_answer_that_already_cites_is_never_rewritten` | fails | passes |
| `test_module83_urdu_digits_and_the_urdu_full_stop_are_handled` | fails | passes |
| `test_module83_multi_line_layout_survives_the_rewrite` | fails | passes |
| `test_module83_an_uncited_synthesis_reaches_the_verifier_with_provenance` | fails | passes |
| `test_module83_a_healthy_synthesis_is_never_regenerated` | fails | passes |
| `test_module83_an_unrecoverable_uncited_synthesis_is_still_refused` | **passes** | passes |

The last one passes in both directions **on purpose** — it is the
non-regression guard for the refusal itself. When nothing can be attributed
with unique support, the answer must still reach the Verifier uncited and
still be refused; a change that made this test go red would mean the refusal
had been softened.

The collapse tests are pinned to the **live capture**, not an invented shape:
`_M83_COLLAPSE` is the captured phrase at the captured ratio, `_M83_HEALTHY`
is a trimmed real passing synthesis.
`test_module83_a_long_legitimate_list_is_not_degenerate` exists because that
is the threshold's real risk — this system emits 19-station listings
constantly, and a detector that read structural repetition as collapse would
push every one of them into the fallback.

---

## 4. Live verification

One backend on `127.0.0.1:8011` from this worktree, `LLM_PROVIDER=groq`,
`CHROMA_PERSIST_DIR` pointed at the shared store. No 429/503 storm was hit;
every arm ran to completion. Backend count stayed within the machine-wide
limit of 2 throughout (one other agent's backend on 8001).

**The control condition** is Module 71's deleted figure roster reinstated
into `_format_subanswers_for_prompt` — one extra deterministic line under
each of the five sub-answers — applied identically to the baseline and fixed
arms via `scratchpad/toggle_control.py` (not committed).

| arm | code | sub-answers | question | runs | **synthesis served** | "cites no `[Document N]`" | collapses detected | recovered by regeneration |
|---|---|---|---|---|---|---|---|---|
| A | `origin/main` | as shipped | **G6** | 4 | 1 | **3** | — | — |
| B | `origin/main` | **lengthened** | **G6** | 4 | **0** | **4** | — | — |
| D | **fixed** | as shipped | **G6** | 4 | **3** | **0** | 2 | 1 |
| E | **fixed** | **lengthened** | **G6** | 4 | **4** | **0** | 3 | **3** |
| A | `origin/main` | as shipped | CR3 | 4 | 4 | 0 | — | — |
| B | `origin/main` | **lengthened** | CR3 | 4 | 3 | 0 | — | — |
| D | **fixed** | as shipped | CR3 | 4 | 4 | 0 | 0 | 0 |
| E | **fixed** | **lengthened** | CR3 | 4 | **4** | 0 | 0 | 0 |

*"synthesis served" = the served answer is the cross-cutting narrative
carrying `[Document N]` markers, rather than Module 71's sub-answer
composition.*

**G6, both prompt lengths together: 1 of 8 on `origin/main`, 7 of 8 on the
fixed code.** The one remaining failure (arm D run 2) collapsed **twice** —
the regeneration at 0.4 collapsed as well — and fell back cleanly with the
new caveat, never serving a character of the loop.

**The control is the point.** A fix that only worked at the current prompt
length would have reproduced the defect it was filed for. Arm E is the
longer prompt and it is the arm with **4 of 4**, because a longer prompt
makes the collapse *more* likely and the collapse is exactly what is now
handled. Three of arm E's four runs collapsed on the first generation and
all three were recovered.

Cost: the regeneration adds one LLM call on the runs that collapse (arm E
39–83 s, against arm B's 74–102 s — *cheaper*, because the twice-rejected
path no longer pays for a grounding call on 6,764 characters). CR3, which
never collapses, is unchanged and slightly faster.

**Run counts.** 16 baseline G6/CR3 runs (arms A, B) + 8 instrumented G6
probe runs + 16 fixed G6/CR3 runs (arms D, E) + 2 × 32 gold questions for
§7 = **104 live requests.**

---

## 5. Gold comparison — substance, not wording

**G6** (arm E, run 1, served with all five documents cited):

| gold asserts | served answer |
|---|---|
| 73 open FIRs across 9 districts, heaviest in Faisalabad and Lahore | *"Cases Faisalabad mein 19, Lahore mein 18, Rawalpindi mein 10, Islamabad, Chiniot, Hyderabad, Karachi East aur Karachi Central mein 5-5 aur Multan mein 1"* `[Document 1]` — **9 districts, same top three, same order** |
| caseload has shifted: two years ago almost all armed robbery, now a mix incl. narcotics, cyber fraud, financial fraud | *"2026 mein 51 FIRs … 2024 ke 13 FIRs se zayda"*, then PPC / Arms Ordinance / CNSA 1997 / PECA 2016 / domestic violence / illegal dispossession `[Document 2]` — **same shift, expressed as the statute mix** |
| arrest on roughly one FIR in six | *"73 FIRs mein sirf 11 mein arrest record"* `[Document 3]` — **11 of 73 ≈ 1 in 6.6. Match** |
| fraud and cyber reported long after the event, unlike older robberies reported in minutes | *"2026 mein … 1401.3 minutes (23.4 hours) aur 2024 mein … 15.0 minutes"* `[Document 4]` — **same finding, quantified** |
| weapons recovered are almost always unlicensed | *"30 recovered weapons mein koi licence nahi hai, aur 2 cases mein licence status nahi"* `[Document 5]` — **30 of 32. Match** |
| most accused are men aged 25–40, usually strangers to the complainant | **not covered** — no sub-answer in the `orientation_note` plan computes accused demographics |
| most matters still pending in court | **not covered** |

**Five of gold's seven findings, each cited to the sub-answer that computed
it.** The two misses are decomposition-plan coverage, not synthesis loss —
neither figure is present in any of the five sub-answers, so nothing above
XAGG could have carried them. Filed in §8.

**CR3** (arm E, run 1): *"No, not identically… **FIR 64/26** has a matching
complaint, while **FIR 65/26** does not appear to have one"* — gold's claim
exactly, including which FIR is which. Unchanged from baseline, as intended.

---

## 6. Non-gold paraphrase

Five rewordings, **written before any of them were run** and recorded in
`scratchpad/paraphrases.json` before the first request — three for G6
(including a Roman-Urdu rewording and an Urdu-script one) and two for CR3
(including a Roman-Urdu one). None was adjusted afterwards.

Two runs each, on the shipped code.

| # | question | reworded as | route | outcome |
|---|---|---|---|---|
| 1 | G6 | *Roman-Urdu:* "Naye afsar ke liye ek chhota sa induction note tayyar karein — abhi jo cases chal rahe hain, un se unhein kya umeed rakhni chahiye?" | **XNETWORK** | **Never reached Meta-Analysis.** Honest refusal: nearest cluster 0.164 against a 0.145 cutoff. 2/2 |
| 2 | G6 | *English:* "Write a short orientation briefing for an officer who has just been posted here — what should they expect from the current caseload?" | XNETWORK | Same, nearest cluster 0.206. 2/2 |
| 3 | G6 | *Urdu script:* "نئے تعینات ہونے والے افسر کے لیے ایک مختصر تعارفی نوٹ لکھیں — موجودہ کیس لوڈ سے انہیں کیا توقع رکھنی چاہیے؟" | XNETWORK | Same, nearest cluster 0.187. 2/2 |
| 4 | CR3 | *English:* "For the online banking fraud with two different victims, was the paperwork handled the same way in both cases?" | XAGG | **Full synthesis, three documents cited, gold's finding exactly** — 64/26 has a matching walk-in complaint, 65/26 does not. 2/2 |
| 5 | CR3 | *Roman-Urdu:* "Online banking fraud ke jis maamle mein do alag mutasireen hain, kya dono ke case ek hi tarah se process aur record kiye gaye?" | XAGG | Verifier-rejected on a **grounding** claim about 65/26 and its accused — not a citation issue — and fell back to the sub-answers. 2/2 |

**This is the honest, uncomfortable result, and it is not caused by this
change.** All three G6 rewordings — including a plain English one and an
Urdu-script one — never enter Meta-Analysis at all: `supervisor.py`'s
Meta-Analysis trigger patterns do not match them, so the query goes whole to
XNETWORK and gets that layer's (correct) relevance refusal. **G6 answers its
gold wording and essentially nothing else.** The sharpest evidence that this
is literal-phrase brittleness rather than a judgement call: the
`orientation_note` decomposition plan already carries a `نئے تعینات` pattern
that matches the Urdu-script rewording exactly, and it is never consulted,
because the supervisor gate upstream of it has its own separate literal list.
Filed as a new defect in §8.

The change here is confined to the post-generation half of `meta_analysis()`
and cannot affect routing: across all six G6-paraphrase runs the backend log
records no decomposition-plan line at all — `meta_analysis()` was never
entered.

Paraphrase 5's rejection is Module 61's family, not Module 29's citation
refusal, and no Module 83 line fired on it.

---

## 7. Regression guard — all 32 gold questions

All 32 gold questions, end to end through the same backend, **twice**: once
on `origin/main` (`scratchpad/all32_baseline.json`) and once on the shipped
branch (`scratchpad/all32_fixed.json`), with nothing but `meta_analysis.py`
differing between them.

```
questions: 32   route equal: 32/32   answer equal: 13/32
```

**Routes are identical on 32 of 32.** No question changed family, tool or
sub-agent.

Byte-equality of ANSWERS is not achievable on this pipeline — every route
ends in a non-deterministic LLM call, and 19 of 32 answers differ between two
runs of code that differs in three questions' worth of behaviour. So the
movers were investigated rather than counted, and the decisive fact is this:

**Only three of the 32 questions execute the changed code at all.** Both
backend logs record exactly three deterministic decomposition-plan matches —
`record_consistency` (CR3), `caseload_review` (G1), `orientation_note` (G6).
For the other 29, `meta_analysis()` is never entered, so no line of this
change runs; their differences are ordinary run-to-run generation variance. A
figure-level diff bears that out: of the 16 non-Meta-Analysis movers, **9
state exactly the same set of figures** as the baseline (CR7, CS4, G2, G5,
KB5, KB9, M1, M5, S3), and the rest differ only in how much of the same
aggregate they restate.

The three questions that DO run this code:

| question | baseline | shipped | verdict |
|---|---|---|---|
| **G6** | 1,546 chars — the synthesis happened to succeed on this particular run | **3,294 chars** | **The targeted improvement.** The log carries the collapse signature exactly as §1.2 describes it — *758 tokens, unique-token ratio 0.024, one 4-gram repeated 370 times* — followed by *"regeneration recovered a usable synthesis"*. On `origin/main` this run would have been a refusal. |
| **G1** | 2,311 chars | 2,194 chars | **No regression. Same five findings, same five documents, same figures** (92 accused / 17 with ages / 24–49 / 31.5; stranger 15 of 24 across 6 FIRs; 45 items across 28 FIRs, 13 forensic, 7 to heirs; evening 19, the busiest; the same four recurring accused). The shipped run states the time-of-day breakdown as prose rather than a five-line list, which is why a set-difference flags `10, 14, 32, 50, 64, 75` as "lost". **No Module 83 line fired for G1 in either run** — it did not collapse and it cited its documents, so neither the detector nor the provenance pass did anything at all. Prose-density variance, not loss. |
| **CR3** | 713 chars, synthesis served | 1,546 chars, **verifier-rejected**, Module 71 sub-answer fallback | **Investigated, and not this change.** The rejection reason is a grounding claim — *"the claims about FIR 64/26 and 65/26 being linked … are not supported by Document 3, which only states that 4 c…"* — Module 61's known CR3 flap, which Module 61 itself measured at 7 of 8. No Module 83 line fired. Across this module's own arms CR3 served a full synthesis on **15 of 16** runs (A 4/4, B 3/4, D 4/4, E 4/4), so this all-32 run landed on the known ~1-in-8. Reported, not waved through. |

**Nothing else moved that could have moved.**

---

## 8. New defects found — filed, not fixed

**A — G6 answers its gold wording and essentially nothing else.** All three
pre-registered G6 rewordings (§6) — a Roman-Urdu one, a plain **English**
one, and an **Urdu-script** one — never reach Meta-Analysis:
`supervisor.py`'s Meta-Analysis trigger patterns do not match them, so the
query goes whole to XNETWORK and is refused there (nearest cluster 0.164 /
0.206 / 0.187 against a 0.145 cutoff), 2 of 2 each, 6 of 6 overall. The
sharpest evidence that this is literal-phrase brittleness: the
`orientation_note` decomposition plan already carries a `نئے تعینات` pattern
matching the Urdu-script rewording exactly, and it is never consulted,
because the supervisor gate upstream of it has its own, different literal
list. Module 29's deterministic plans fixed G6's gold phrasing; a user asking
the same question in their own words gets a refusal. **Evidence:**
`scratchpad/text_paraphrase.json`, and the absence of any decomposition-plan
line in the backend log for those six runs.

**B — the `orientation_note` plan computes five of gold's seven findings, and
the two it misses are unreachable from above.** G6's gold answer also asserts
an accused profile (mostly men aged 25–40, usually strangers to the
complainant) and that most matters are still pending in court. Neither is
computed by any of the plan's five sub-queries, so no synthesis — however
good — could carry them. §5 matches five of seven; these two are a
decomposition-coverage gap, not a synthesis loss. Both figures demonstrably
exist in the corpus: G1's own plan computes the age profile and the
stranger relationship from the same data.

**C — the Roman-Urdu synthesis is fluent-sounding but partly non-words.** The
recovered G6 syntheses contain invented Urdu-ish tokens — *"kismatiyadari"*,
*"mohtajr"*, *"tawarruq"*, *"qanuniyadari"* — carrying no meaning, mixed into
otherwise correct, correctly-cited prose. The figures and the citations are
right; the connective language is not. This is the same generation slot whose
greedy decode collapses into `kismat-e-murad ki`, so the two are plausibly
one weakness at two severities — but that is a hypothesis, not a measurement.
**Evidence:** §5's served answer, `scratchpad/runs_E_fix_lengthened.json`.

**D — `_attach_provenance` ships without a live firing.** It is correct,
conservative, and unit-tested in both directions, and it fired **0 times in
20 live G6 runs** and 0 times across all 32 gold questions, because the
failure it was built for is not the failure that occurs. Recorded here so a
future module does not assume it is load-bearing. If a later measurement
shows the marker-free-but-correct synthesis never happens, this pass is dead
code and should be deleted rather than maintained.

**E — Module 70's real rate.** Filed as 1 in 3; measured here at **9 of 9**
(10 of 10 counting §7's baseline run). See §9.

---

## 9. Module 70 — M2's headline number is dropped above XAGG

**Diagnosed, not fixed, and deliberately so.** The diagnosis is materially
worse than what was filed.

### The three-way capture

M2's gold question, nine consecutive live runs on the shipped backend, with
`large_scale_aggregate.py` temporarily instrumented to log the raw computed
rendering and the paraphrase before the gate saw them. The instrumentation
was reverted and is not in the commit.

| layer | what it carries | runs |
|---|---|---|
| **1. Aggregate payload** (`xagg.py`) | `19 station(s), 73 FIR(s); crime_type_specialised=2 station(s)/9 FIR(s); other_specialised=2/10; general_purpose=15/54` | **9 of 9**, identical |
| **2. Rendered text** (`raw_summary_text`) | *"Growth: caseload is rising fastest at the 15 general-purpose station(s) — 7 FIRs in 2024 to 39 in 2026, against 3 to 5 at the 2 single-crime-type station(s) — **though even so, 9 of 73 FIRs (~12.3%) are carried by just 2 of 19 stations**, the ones set up for a single type of crime."* — and the same figure again in the next paragraph | **9 of 9**, byte-identical |
| **3. Paraphrase** (the LLM call in `large_scale_aggregate.py`) | *"The caseload is growing faster at the general-purpose stations… the 15 general-purpose stations saw an increase from 7 FIRs in 2024 to 39 FIRs in 2026. In contrast, the 2 stations set up for one specific type of crime saw a much slower increase, from 3 FIRs in 2024 to 5 FIRs in 2026 [Document 1]."* | **9 of 9 drop the headline** |
| **4. Served answer** | byte-identical to layer 3 | 9 of 9 |

Module 58 filed this as *"the served answer carries it on 2 of 3"*. **On this
branch it is 0 of 9** — 0 of 10 counting §7's independent baseline M2 run,
which also drops it. M2's single-run score is not a coin flip on gold's
headline figure; on this machine the headline is simply gone.

### Root cause, precisely

`verifier.py::verify_structured_aggregate_paraphrase()` — the gate standing
between that paraphrase and the user — computes exactly one numeric test:

```python
unsupported_numbers = sorted(n for n in (ans_nums - src_nums) ...)
```

It is **one-directional by construction**. It catches every number the
paraphrase *invents* and cannot, by design, see one the paraphrase *omits*.
On all nine runs it logged `grounded=True unsupported_numbers=[] —
Paraphrase numbers match the computed source`, which is true, and useless
here: the paraphrase stated a strict subset.

Module 44 already fought this twice from the rendering side —
`render_station_caseload_by_specialisation` carries two comments recording
that a paraphrase kept the growth clause and dropped the share clause on 3 of
3 runs, and that the lead was made ONE sentence precisely so that "a
paraphrase [has] no seam to drop". It drops it anyway. **The rendering side
has been pushed as far as it can go; the gate is where the fix belongs.**

Same family as Modules 61 and 71 — a correct computed finding lost in the one
narrative pass above it — and the codebase already has the remedy shaped for
it: `large_scale_aggregate.py` serves `raw_summary_text` whenever the
paraphrase fails this gate.

### The fix, proposed and not made

Add an **omission** half to the check: take the figures of
`raw_summary_text`'s first rendered line (every renderer leads with its
headline, deliberately), and if any is absent from the paraphrase, treat the
paraphrase as failed and serve `raw_summary_text` — the existing,
already-correct branch, with its existing caveat. No new user-visible
mechanism, no new prompt, no extra LLM call.

### Why it is left open

1. **Blast radius.** That gate sits on the XAGG paraphrase path that **many**
   of the 32 share (G2, G5, M2, M4, M5, CR3's sub-answers, and every KB
   question that composes an aggregate half). A too-strict omission rule
   would flip a broad set of answers from readable prose to raw computed
   text — a broad, quiet regression, which is exactly what §7 exists to
   prevent. Landing it responsibly needs its own all-32 equality control, and
   this session has already spent two full all-32 runs on Module 83.
2. **The check may belong in `verifier.py`**, which is **Module 85's
   territory in `muhafiz-kbgen`** and was avoided throughout this module. A
   `large_scale_aggregate.py`-side implementation is possible and is what I
   would build, but the two tracks should agree where the rule lives before
   either writes it.

Per the brief: 83 is done fully, 70 is reported with evidence and left open.
The plan row now carries the measured 0-of-9 rate so the next owner does not
re-derive it.
