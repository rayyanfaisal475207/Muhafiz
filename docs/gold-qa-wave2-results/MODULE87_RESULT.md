# Module 87 — the Gold-32 judge: a stronger model, and a prompt that stops it making the mistake we measured

**Run 2026-09-09/10 against `main` @ `6cf89fb`, branch `fix/eval-judge-upgrade-and-prompt`.**

Nothing in this module changes the platform. It changes the **instrument**. Every
score in wave 2 — including the three-pass Module 27 run — was produced by
`gemini-flash-lite-latest` reading a prompt with no rule about polarity, and this
module measures what that instrument got wrong and what replacing it costs.

**No backend, no Docker, no model server.** All 96 question-runs here are
re-scorings of the answers Module 27 already captured
(`gold32_pass{1,2,3}_outputs.json`), so the system under test is frozen and the
only variable is the judge.

> **Where the inputs live.** Module 27's artefacts were on the then-unmerged
> branch `eval/module27-final` (`0113c47`) when this work started and were read
> from that commit; they have since landed on `main`, and the
> `evaluation/gold32_pass{1,2,3}_{outputs,results}.json` now in the tree are
> **content-identical** to what was scored here (verified per row: no answer,
> gold or score differs; the files differ only in line endings). Nothing here
> overwrites them, or `gold32_results.json`. This module's own measurements are
> committed as `module87_*.json` beside this file.

---

## 1. Root cause

Three defects, and the brief was right about all three — but **wrong about which
one causes which symptom**, and that matters for the fix.

### 1a. The judge model was pinned to a literal, and it was the wrong pick

`_judge()` hard-coded `gemini-flash-lite-latest`. That is a 2.5-generation lite
model, and the project's own history already records one judge (Qwen-27B via
Groq) being replaced for grading too literally. A value that shapes every
published number had no name a report could quote and no env var an experiment
could move — exactly the defect Module 46 fixed for the answer cap.

### 1b. The judge had no polarity rule, and M7 is the measured cost

M7's question is *"Are people reporting incidents to police as quickly in 2026 as
in 2024?"*. Gold answers the **implied** question ("is there a difference?") with
*"Haan, bohat zyada farq se"* — 2024 mean 15.0 minutes, 2026 mean 1401.3. The
system answers the **literal** question with *"No, people are not reporting as
quickly"* — 15.0 minutes across 13 FIRs in 2024, 1401.3 minutes (~23.4 h) across
51 in 2026.

**Identical figures. Identical meaning. Opposite opening word.** The judge read
the polarity flip as contradiction and scored 0.1 / 0.3 / 0.2. Module 43
separately measured M7 returning gold on **6 live runs of 6**.

### 1c. The variance was the MODEL, not the prompt — and the brief's premise about *which* questions is partly wrong

The brief cites four questions with spread ≥0.3 "on identical answers". Checked
byte-for-byte across the three Module 27 passes:

| Q | captured answer identical across passes 1/2/3? |
|---|---|
| CP1 | **yes** (157 chars, all three) |
| M2 | **yes** (306 chars, all three) |
| M7 | **yes** (252 chars, all three) |
| KB6 | **no** — 1,840 / 1,929 / 1,943 chars |
| G1 | **no** — 2,949 / 2,649 / 2,371 chars |

So only CP1's and M2's 0.5 spreads were pure judge variance; KB6's and G1's mixed
judge variance with the pipeline's own non-determinism. Every variance figure in
§5 below is therefore measured the only way that isolates the judge: **the same
captured answer, five independent draws.**

And the cause is not temperature. DeepEval's `GeminiModel` already defaults to
`temperature=0.0`, and the retired judge produced 0.2/0.0/0.4/0.3/0.2 on M7 **at
temperature 0**. Pinning temperature explicitly (done anyway, so the file states
it rather than inheriting a library default) changes nothing. What removed the
variance was the model.

---

## 2. Change

Files touched — **`evaluation/` and the reproduction doc only**; nothing under
`src/` or `prompts/`.

| File | Change |
|---|---|
| `evaluation/gold32_score.py` | `DEFAULT_JUDGE_MODEL` / `JUDGE_MODEL` / `GOLD32_JUDGE_MODEL`; `JUDGE_TEMPERATURE` pinned at 0; `_judge(model, temperature)`; `FACTUAL_EVALUATION_STEPS` hoisted to module scope; **the polarity step and its fence**; `build_metrics(judge, evaluation_steps=None)`; `ATTEMPT_TIMEOUT_S` raised 120 → 300 and made env-overridable |
| `HOW_TO_REPRODUCE_THIS_EVALUATION.md` | §3.1 judge model + the quota finding; §3.3 grows from 7 numbered rules to 8 |
| `tests/test_gold32_score.py` | 3 new classes, 16 new tests (§3) |
| `docs/gold-qa-wave2-results/module87_*.json` | every measurement below, as raw rows |

**Why the model choice is not simply "the biggest one".** Free-tier quota on this
project's keys turned out to be the binding constraint, so it was measured rather
than assumed:

| candidate | free-tier cap (both working keys) | M7 | wall clock / call |
|---|---|---|---|
| `gemini-flash-lite-latest` *(retired)* | 500 / **day** | 0.22 mean, 5 draws, spread **0.4** | 5–6 s |
| `gemini-2.5-flash` | **20 / day** | **0.92**, 5 draws, spread 0.1 *(old prompt)* | 16–68 s |
| `gemini-3.5-flash` | **20 / day** | **1.0**, 3/3 | 20–90 s |
| `gemini-3.7-flash` | **20 / day** | **1.0**, 3/3 (CP1 also 1.0 3/3) | 7–354 s |
| **`gemini-3.1-flash-lite`** ← chosen | **15 / minute** | **1.0**, 5/5, spread **0.0** | **8.1 s mean over 96 calls** |

A single three-pass FactualCorrectness re-score is **96 judge calls**. A
20-per-day model cannot produce one, on any number of retries — `gemini-2.5-flash`
would have fixed M7 on the *old* prompt (0.92 over five draws) and it still
cannot be the default here, which is the whole of the "measure, do not assume"
instruction. `gemini-3.1-flash-lite` is the only candidate newer than the retired
judge whose quota completes a run, and it is also the one that removed the
run-to-run variance outright.

Two supporting fixes fell out of the measurement:

* **`ATTEMPT_TIMEOUT_S` was 120 — *lower* than the DeepEval per-attempt override
  right beside it (180).** Under machine contention two questions (S3, A7) lost
  all three attempts to that cap and were recorded `UNSCORED` while returning
  1.0 in 6 s once the machine was quiet. A call that is merely **slow** must not
  become a judge **failure**; that is Module 45's rule one layer out. Now 300,
  overridable with `GOLD32_ATTEMPT_TIMEOUT_S`.
* **The polarity rule needed a fence, and finding that out was the most useful
  thing in this module.** See §7.

### The polarity rule, as shipped

Module 20 proved an abstract rule alone is not honoured — the close-numbers rule
only started working once a real worked example was attached to it — so this rule
carries M7's own figures, in the same house style:

> Judge the **SUBSTANTIVE CLAIM**, not the yes/no token that opens it. A question
> can be read literally or as the difference it implies, and the EXPECTED OUTPUT
> and the ACTUAL OUTPUT may each answer a different one of those readings — which
> makes them open with OPPOSITE words while asserting exactly the SAME thing.
> Before calling anything a contradiction, check the supporting facts: if the two
> outputs' figures, directions and conclusions agree, the answers AGREE, and the
> opposite yes/no is NOT an error. *Worked example …* 'Yes' against 'No', but both
> assert that reporting became dramatically slower and the figures are identical —
> under this rule that answer MATCHES and should score HIGH. A real contradiction
> reverses a FACT …, never merely the polarity of the opening word. **This rule
> excuses the OPENING WORD AND NOTHING ELSE.** It is not a shortcut past the rest
> of this checklist: once polarity is settled, go back and check every key fact
> the question asked for, one at a time. An ACTUAL OUTPUT that agrees with the
> EXPECTED OUTPUT in direction but leaves out its specific legal provision, count,
> name or entity is still materially INCOMPLETE and must score LOW.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_gold32_score.py tests/test_provider_failure_visibility.py -q
56 passed
```

(40 pre-existing, all still passing — including the docs-match tests in
`test_provider_failure_visibility.py`, which is why `HOW_TO_REPRODUCE` §3 is
edited in the same commit as the code.)

New, each pinned to the real case that produced it:

| Test | Pins |
|---|---|
| `test_default_model_is_a_named_constant` | the model has a name a report can quote |
| `test_the_retired_judge_is_no_longer_the_default` | `!= "gemini-flash-lite-latest"` — the model that scored M7 0.1/0.3/0.2 |
| `test_the_default_is_a_pinned_version_not_a_moving_alias` | no `-latest`: an alias silently changes the judge under the reports citing it |
| `test_model_is_env_overridable` | `GOLD32_JUDGE_MODEL`, by module reload |
| `test_temperature_is_pinned_not_inherited` | 0.0 stated in the file, not inherited from DeepEval |
| `test_a_polarity_rule_is_present` | **the polarity rule exists in `evaluation_steps`** |
| `test_the_polarity_rule_carries_its_own_worked_example` | M7's literal gold figures — `15.0 minutes`, `1401.3` — are in the prompt |
| `test_the_polarity_rule_is_fenced_so_it_cannot_excuse_an_omission` | the §7 regression |
| `test_the_close_numbers_rule_still_carries_its_worked_example` | Module 20's rule, unbroken |
| `test_build_metrics_{uses_the_constant,accepts_an_override}` | one prompt, diffable by an experiment |
| `TestDocsMatchTheJudgeContract` (4) | §3.1 names the model in the code; §3.3 lists **as many numbered rules as the prompt has steps** |

That last one is the durable part: the doc and the prompt cannot drift apart
silently again.

---

## 4. Live verification — the three-pass re-score, old judge vs new

96 question-runs re-scored, same captured answers, **0 unscored**, 8.1 s mean per
call. Old = the committed Module 27 rows (`gemini-flash-lite-latest`, no polarity
rule). New = `gemini-3.1-flash-lite` + the fenced prompt
(`module87_rescore_3pass.json`).

| Q | Type | old ×3 | old mean | old spr | new ×3 | new mean | new spr | delta |
|---|---|---|---|---|---|---|---|---|
| D1 | Fact Retrieval | 1.0/1.0/1.0 | 1.00 | 0.0 | 1.0/1.0/1.0 | 1.00 | 0.0 | +0.00 |
| S2 | Fact Retrieval | 1.0/1.0/1.0 | 1.00 | 0.0 | 1.0/1.0/1.0 | 1.00 | 0.0 | +0.00 |
| S3 | Fact Retrieval | 0.9/1.0/0.9 | 0.93 | 0.1 | 1.0/1.0/1.0 | 1.00 | 0.0 | +0.07 |
| A1 | Fact Retrieval | 1.0/1.0/1.0 | 1.00 | 0.0 | 1.0/1.0/1.0 | 1.00 | 0.0 | +0.00 |
| A7 | Fact Retrieval | 1.0/1.0/1.0 | 1.00 | 0.0 | 1.0/1.0/1.0 | 1.00 | 0.0 | +0.00 |
| **CP6** | Fact Retrieval | 0.3/0.3/0.3 | 0.30 | 0.0 | 0.9/0.9/0.9 | **0.90** | 0.0 | **+0.60** |
| CR2 | Complex Reasoning | 0.0/0.0/0.0 | 0.00 | 0.0 | 0.0/0.0/0.0 | **0.00** | 0.0 | +0.00 |
| CR3 | Complex Reasoning | 1.0/1.0/1.0 | 1.00 | 0.0 | 1.0/1.0/1.0 | 1.00 | 0.0 | +0.00 |
| CR4 | Complex Reasoning | 1.0/1.0/1.0 | 1.00 | 0.0 | 1.0/1.0/1.0 | 1.00 | 0.0 | +0.00 |
| CR6 | Complex Reasoning | 1.0/1.0/1.0 | 1.00 | 0.0 | 1.0/1.0/1.0 | 1.00 | 0.0 | +0.00 |
| CR7 | Complex Reasoning | 1.0/1.0/1.0 | 1.00 | 0.0 | 1.0/1.0/1.0 | 1.00 | 0.0 | +0.00 |
| CR8 | Complex Reasoning | 1.0/1.0/1.0 | 1.00 | 0.0 | 0.9/0.9/0.9 | 0.90 | 0.0 | −0.10 |
| CS4 | Complex Reasoning | 1.0/1.0/1.0 | 1.00 | 0.0 | 1.0/1.0/1.0 | 1.00 | 0.0 | +0.00 |
| CP1 | Complex Reasoning | 1.0/0.5/0.9 | 0.80 | **0.5** | 1.0/1.0/1.0 | 1.00 | **0.0** | +0.20 |
| M1 | Ctx Summarization | 0.4/0.7/0.4 | 0.50 | 0.3 | 0.2/0.2/0.2 | 0.20 | 0.0 | −0.30 |
| M2 | Ctx Summarization | 0.3/0.8/0.3 | 0.47 | **0.5** | 0.2/0.2/0.2 | 0.20 | **0.0** | −0.27 |
| M4 | Ctx Summarization | 1.0/1.0/1.0 | 1.00 | 0.0 | 1.0/1.0/1.0 | 1.00 | 0.0 | +0.00 |
| M5 | Ctx Summarization | 0.9/1.0/0.9 | 0.93 | 0.1 | 1.0/1.0/1.0 | 1.00 | 0.0 | +0.07 |
| **M7** | Ctx Summarization | 0.1/0.3/0.2 | 0.20 | 0.2 | 1.0/1.0/1.0 | **1.00** | 0.0 | **+0.80** |
| **G1** | Creative Generation | 0.3/0.7/0.6 | 0.53 | 0.4 | 0.9/1.0/1.0 | **0.97** | 0.1 | **+0.43** |
| G2 | Creative Generation | 0.5/0.5/0.5 | 0.50 | 0.0 | 0.3/0.3/0.3 | 0.30 | 0.0 | −0.20 |
| G3 | Creative Generation | 1.0/1.0/1.0 | 1.00 | 0.0 | 1.0/1.0/1.0 | 1.00 | 0.0 | +0.00 |
| G5 | Creative Generation | 0.5/0.6/0.6 | 0.57 | 0.1 | 0.4/0.4/0.4 | 0.40 | 0.0 | −0.17 |
| G6 | Creative Generation | 0.3/0.4/0.2 | 0.30 | 0.2 | 0.1/0.1/0.1 | 0.10 | 0.0 | −0.20 |
| KB1 | Knowledge Base | 0.3/0.3/0.3 | 0.30 | 0.0 | 0.3/0.3/0.3 | 0.30 | 0.0 | +0.00 |
| KB2 | Knowledge Base | 0.0/0.0/0.0 | 0.00 | 0.0 | 0.1/0.2/0.2 | **0.17** | 0.1 | +0.17 |
| KB3 | Knowledge Base | 0.0/0.0/0.0 | 0.00 | 0.0 | 0.0/0.0/0.0 | **0.00** | 0.0 | +0.00 |
| KB4 | Knowledge Base | 0.1/0.2/0.2 | 0.17 | 0.1 | 0.2/0.2/0.2 | 0.20 | 0.0 | +0.03 |
| KB5 | Knowledge Base | 0.9/0.8/0.9 | 0.87 | 0.1 | 0.9/0.9/0.9 | 0.90 | 0.0 | +0.03 |
| KB6 | Knowledge Base | 0.8/0.4/0.9 | 0.70 | 0.5 | 0.4/0.4/0.9 | 0.57 | **0.5** † | −0.13 |
| KB8 | Knowledge Base | 0.9/1.0/1.0 | 0.97 | 0.1 | 0.9/1.0/1.0 | 0.97 | 0.1 | +0.00 |
| KB9 | Knowledge Base | 0.2/0.4/0.2 | 0.27 | 0.2 | 0.4/0.4/0.4 | 0.40 | 0.0 | +0.13 |

† KB6 is the only remaining spread ≥0.3, and it is **not the judge's** — see §5.

### The headline numbers, and the caveat that governs them

| | old judge | new judge |
|---|---|---|
| all-32 FactualCorrectness mean, 96 runs | 0.666 | **0.702** |
| per-pass mean | 0.647 / 0.684 / 0.666 | 0.691 / 0.700 / 0.716 |
| per-pass pass rate (≥0.5) | 20 / 22 / 21 | 20 / 20 / 21 |
| passes on **all three** runs | 19 | **20** |
| questions with spread ≥0.3 | **4** | **1** |
| mean spread across 32 questions | 0.106 | **0.025** |

**A rising mean is only good if each rise is individually justified**, so:
**exactly three questions rose by ≥0.3, and seven FELL** (M1 −0.30, M2 −0.27, G2
−0.20, G6 −0.20, G5 −0.17, KB6 −0.13, CR8 −0.10). The Creative Generation bucket
mean goes **down**, 0.580 → 0.553. This is a stricter judge that is also right
about three specific questions — not a leniency shift. **The pass rate barely
moves** (20/22/21 → 20/20/21); what moves is the *stability*.

Bucket means: Fact Retrieval 0.872 → 0.983 · Complex Reasoning 0.850 → 0.863 ·
Contextual Summarization 0.620 → 0.680 · **Creative Generation 0.580 → 0.553** ·
Knowledge Base 0.408 → 0.438.

---

## 5. Gold comparison — is each move right?

### The three rises, each justified on its own evidence

**M7 +0.80 (0.20 → 1.00, 3/3).** The defect this module exists for. The judge's
own new reason names the rule: *"the difference in the opening polarity (No vs
Haan) is not an error because the underlying …"*, having first confirmed both
outputs carry 15.0 and 1401.3. Module 43 measured M7 returning gold on 6 live
runs of 6. **Justified.**

**CP6 +0.60 (0.30 → 0.90, 3/3) — and the brief's question answered.** The
tolerance rule was **not** being ignored; the retired judge applied it and then
overrode it. Its own words, pass 3: *"the number is very close and within an
acceptable tolerance, [but] the actual output completely omits the specific
details about the placeholders."* The question is *"Kitne cases abhi tak bina
kisi tafteeshi afsar ke … pade hue hain?"* — **how many**. Gold volunteers a
breakdown (8 Naamzad ASI, 3 Naamzad SI) that the question never asks for, and
10 against 11 is 9 % — inside the stated 5–10 %. Step (b) of the prompt already
said *"omitting a key fact the question specifically asked for"*; the retired
judge dropped the qualifier, the new one keeps it: *"a count of 10, which is
within the acceptable margin of error (11 expected) according to the evaluation
rules."* **0.30 was not fair. Justified — and note no rule was added for it.**

**Corroborated independently, and more strongly than expected.** While this
module was running, **Module 91** (PR #60) re-derived CP6 from the data and found
that **gold's 11 was wrong**: the aggregate returns *10 current* / 11 ever, and
gold was corrected to 10 by PR #57 because `fir-205-26` has since been assigned a
real officer. So the answer the retired judge scored 0.30 was not merely *within
tolerance* — it was **exactly right**, and the tolerance rule was doing the work
that a stale gold made necessary. The re-score here still uses gold **as
captured** (11), which makes 0.90 a conservative figure.

**G1 +0.43 (0.53 → 0.97).** A second false negative, found by this module rather
than the brief. G1's captured answer carries **all four** of gold's flagged
findings, with gold's own figures: ages **24–49, average 31.5** (gold: 24–49,
~31); *"اجنبی (stranger)"* dominant, 15 of 24 relationship entries; **13** items
to a forensic lab and **7** held for a deceased's heirs (gold: 13 and 7,
exactly); and the time-of-day distribution. The retired judge scored it
0.3/0.7/0.6 — spread 0.4 — with a pass-1 reason claiming it *"misses key
conclusions … such as the uniform offender a[ge]"*, which is in the answer's
first bullet. **Justified.**

### Questions that should fail, and still do

| Q | new | why it is a genuine failure |
|---|---|---|
| **CR2** | **0.00**, 3/3 | answers that *no* person resurfaced where gold names an identity with a prior conviction (FIR 891/24 → 214/26). A reversed **fact**, not a reversed word — precisely what the polarity rule's last sentence excludes |
| **KB2** | **0.17** | the hedge. Answers that the system *does* keep interview statements where gold says it deliberately does not, under Qanun-e-Shahadat / CrPC s.162 |
| KB3 | 0.00, 3/3 | refuses outright (*"No cross-case connections or patterns were found"*) where gold has Article 18 and 68-of-74 |
| KB1 | 0.30, 3/3 | correct on s.154, then abstains on the half the question actually asks |
| KB4 | 0.20 | never reaches gold's Punjab Police Rules 27.16 |
| M2 | 0.20 | gold's growth framing is contested (Module 44) and the answer matches neither reading |
| G6 | 0.10 | *"a raw, unformatted list of statistical data points"* where an orientation note was asked for |

### Variance — before and after, on the four questions the brief named

Measured the only way that isolates the judge: **the pass-1 answer held fixed,
five independent draws per arm.**

| Q | retired judge | new model, old prompt | **new model + new prompt** |
|---|---|---|---|
| CP1 | 0.82 · 0.9/0.8/0.8/0.8/0.8 · **spr 0.1** | 1.00 · spr **0.0** | 1.00 · spr **0.0** |
| KB6 | 0.76 · 0.7/0.9/0.8/0.8/0.6 · **spr 0.3** | 0.60 · spr **0.0** | 0.80 · spr **0.0** |
| M2 | 0.20 · 0.2/0.0/0.5/0.2/0.1 · **spr 0.5** | 0.20 · spr **0.0** | 0.20 · spr **0.0** |
| G1 | 0.46 · 0.6/0.5/0.4/0.3/0.5 · **spr 0.3** | 0.90 · spr **0.0** | 0.90 · spr **0.0** |
| M7 | 0.22 · 0.2/0.0/0.4/0.3/0.2 · spr 0.4 | 0.90 · spr **0.0** | **1.00** · spr **0.0** |
| CR2 | 0.00 · spr 0.0 | 0.00 · spr 0.0 | **0.00** · spr 0.0 |
| KB2 | 0.00 · spr 0.0 | 0.10 · spr 0.0 | 0.10 · spr 0.0 |
| CP6 | 0.38 · 0.3/0.5/0.5/0.3/0.3 · spr 0.2 | 0.60 · spr **0.0** | 0.90 · spr **0.0** |
| D1 | 1.00 · spr 0.0 | 1.00 · spr 0.0 | 1.00 · spr 0.0 |

**Spread 0.0 on nine of nine, five draws each — and the middle column shows it is
the MODEL that did it, not the prompt.** The prompt's contribution is accuracy
(M7 0.90 → 1.00, CP6 0.60 → 0.90), not stability. On the full 32, spread ≥0.3
goes 4 → 1 and the mean spread 0.106 → 0.025.

**The one residual is honest and is not the judge's.** KB6 still shows 0.4/0.4/0.9
across the three passes — but KB6's three *captured answers differ* (1,840 /
1,929 / 1,943 chars). Held fixed, it is 0.4 five times out of five with zero
spread. Its remaining variance belongs to the pipeline, and no module owns it
(filed as **Module 108**).

---

## 6. Non-gold control — does the rule generalise, or was it written for M7?

The judge analogue of a non-gold paraphrase: cases **written for this control and
never shown to the prompt**, including two the rule must NOT rescue. Three draws
each, on the shipped judge (`module87_polarity_controls.json`).

| Control | expect | result |
|---|---|---|
| **A — held-out polarity flip.** A different question (unassigned-officer FIRs). Gold opens *"No — not all of them do … 11 FIRs …"*; the answer opens *"Yes, there is a gap: 11 FIRs …"*. Same substance, opposite word | HIGH | **1.0 / 1.0 / 1.0** |
| **B — reversed FACT.** M7's question, "Yes … faster in 2026", with 1401.3 and 15.0 **swapped between the years** | LOW | **0.2 / 0.2 / 0.2** |
| **C — wrong MAGNITUDE.** M7's polarity flip, correct direction, but 2026 given as **18.0** minutes instead of 1401.3 | LOW | **0.2 / 0.2 / 0.2** |

A and the deltas are the point: the rule fires on a case it has never seen, and
it does **not** license contradiction (B) or dissolve the magnitude limit (C) —
*"tolerance is for phrasing, not magnitude"* survives at a 78× error. The cost is
visible and small: B scored 0.0 without the polarity rule and 0.2 with it, still
far below the 0.5 pass line.

---

## 7. Regression guard — including one the rule itself caused

**All 32 questions × 3 passes were re-scored** (§4); that *is* the regression
guard, and it caught something.

**The first version of the polarity rule broke KB9, and the fence is the fix.**
Unfenced, KB9 went from 0.27 (retired judge) to **1.0 on all three passes** — on
an answer that never reaches gold's **CrPC s.174**, never gives gold's **10
PPC-302 FIRs**, and hedges (*"some of which may involve deaths … specific details
… are not included"*). The judge settled polarity and stopped checking facts,
citing the "correctly stating the data lacks something is a PASS" rule.

Attributed, not guessed (`module87_attribution_kb9.json`):

| arm | KB9 |
|---|---|
| retired judge | 0.2 / 0.4 / 0.2 |
| new model, **rule removed** | 0.3 / 0.3 / 0.3 |
| new model, **rule unfenced** | **1.0 / 1.0 / 1.0** ← the rule caused it |
| new model, **rule fenced** (shipped) | **0.4 / 0.4 / 0.4** |

So the rule was the cause, and *"This rule excuses the OPENING WORD AND NOTHING
ELSE"* removes it while M7 stays at 1.0 and CP6 at 0.9. Both arms are committed
(`module87_rescore_3pass_unfenced.json` vs `module87_rescore_3pass.json`) so the
claim is checkable. The fence is pinned by
`test_the_polarity_rule_is_fenced_so_it_cannot_excuse_an_omission`.

Fencing also made the judge stricter on KB6 (0.8 → 0.4 on the fixed answer),
which is the same rule doing the same job; KB6's answer omits gold's
firearm-specific handling steps.

**KB3's AnswerRelevancy 0.07 is not a metric artefact.** Its captured answer is
*"No cross-case connections or patterns were found … nearest cluster found was
distance 0.179 against a relevance cutoff of 0.145"* — a refusal discussing
community clusters, against a question about whether registering and
investigating officers must be different people. Nothing in it addresses the
question, which is what AnswerRelevancy measures; the retired judge's 0.0 / 0.0 /
0.2 is the correct reading. Re-measured on the new judge with two controls
(`module87_kb3_answer_relevancy.json`): **KB3 0.2 / 0.2 / 0.2, KB2 1.0 / 1.0 /
1.0, D1 1.0 / 1.0 / 1.0** — the metric is discriminating, not broken, and its own
reason is *"the response includes significant amounts of irrelevant technical
metadata and cross-case analysis that do not address the specific procedural
question."* Note KB2, whose answer is substantively **wrong** (FC 0.17), scores
AR 1.0: AnswerRelevancy asks whether the answer is *about the question*, not
whether it is *true*, so an extreme AR is a signal about topicality only. Its
FactualCorrectness 0.00 stands in both arms, and
the cause is routing (**Module 78**: KB3 reaches XNETWORK, so its data-half plan
is never consulted), not the judge.

**Every other sub-0.5 judge reason in the three committed passes was read.**
Beyond M7, CP6 and G1, no further false negative was found: CR2, KB1, KB2, KB3,
KB4, KB9, M2, G2, G6 are all genuinely wrong answers, and M1 is borderline —
gold's crime-type breakdown *is* what "what kinds of cases" asks for, so 0.20 is
defensible.

---

## 8. New defects found — left unfixed, tracked as their own modules

**Module 98 — three of the five Gemini keys in `.env` are dead, and the model
`GEMINI_MODEL` names cannot serve one evaluation run.** Measured directly:
`GEMINI_API_KEY_2/_3/_4` return **401 UNAUTHENTICATED** on the models-list
endpoint; only `GEMINI_API_KEY` and `_1` work. On both of those,
`gemini-2.5-flash` — the value of `GEMINI_MODEL` in `.env` — is capped at **20
requests per day**, as are `gemini-3.5-flash` and `gemini-3.7-flash`; the retired
judge's tier is 500/day and **both keys hit that ceiling mid-session** while
other tracks were running. Consequences beyond this module: every Gemini caller
in the project is running on two keys, not five, and can exhaust a model's daily
quota part-way through a run — which surfaces as slow calls and `UNSCORED` rows,
not as an obvious credential error. This is the same class of failure as the
`SHARE/.env` incident in `HOW_TO_REPRODUCE` §1.1, which produced 1,410 rate-limit
errors and an invalid KB bucket. **Verify:** hit `/v1beta/models` with each key
and record the code; then hit each candidate model once and record `limit:` and
`PerDay`/`PerMinute` from the 429 body.

**Module 108 — KB6 is the last question whose score spread is ≥0.3, and it is the
pipeline's, not the judge's.** Its three captured Module 27 answers differ (1,840
/ 1,929 / 1,943 chars) and score 0.4 / 0.4 / 0.9; the *same* answer judged five
times scores 0.4 every time, spread 0.0. Module 27's variance section attributes
all four ≥0.3 spreads to the judge, which is right for CP1 and M2 (byte-identical
answers) and wrong for KB6 and G1. **Verify:** several live KB6 runs, diffing the
answers, before attributing any KB6 movement to a code change.

---

## 9. Reproducing this

```bash
# No backend, no Docker. Judge calls only.
export GOLD32_JUDGE_MODEL=gemini-3.1-flash-lite      # the default; set to compare
PYTHONPATH=. .venv/Scripts/python.exe evaluation/gold32_score.py
```

Quota check on the log, Module 81's fixed pattern:

```bash
grep -ciE "rate limit|RESOURCE_EXHAUSTED|quota|UNAVAILABLE|(^|[^0-9.,:])(429|503)([^0-9]|$)" <log>
```

The shipped 96-run re-score reported **0 unscored rows** and 8.1 s mean per call.
Provider failures were hit during the *candidate search* (§2) and are reported
there rather than hidden: `gemini-2.5-flash`, `gemini-3.5-flash` and
`gemini-3.7-flash` all exhausted their 20/day allowance, and both keys exhausted
`gemini-flash-lite-latest`'s 500/day, which is why the retired judge could not be
re-run for a same-session baseline. **The old-judge column in §4 is Module 27's
own committed rows, not a re-derivation** — those numbers were not re-measured
here, and the 45-draw probe in §5 is what re-measures the retired judge on the
questions that matter.
