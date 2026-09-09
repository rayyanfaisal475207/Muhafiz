# Module 109 — should a Groq-hosted judge replace the Gemini one?

**Measured 2026-09-10, branch `eval/judge-groq-comparison`, based on
`fix/eval-judge-upgrade-and-prompt` @ `657e1bb` (Module 87, not yet merged).**

## Recommendation, up front

**No. Keep `gemini-3.1-flash-lite`.** Every Groq model this account can reach
holds the held-out controls but **fails to reproduce the judge Module 87
shipped**: on the full 96-run re-score the best Groq candidate scores
**0.573 against 0.702**, passes **45 of 96 against 61**, has **three times the
run-to-run spread**, records **one UNSCORED row against zero**, and reverses
Module 87's two most carefully justified findings — **G1 0.97 → 0.20** and
**KB5 0.90 → 0.23**.

Quota was the reason to look and it is not a reason to switch. It is also
weaker than it looked: **the judge in use was never the thing quota was
blocking.** `gemini-3.1-flash-lite` is capped per *minute*, not per day, and ran
all 96 calls in 10 minutes today. What the 20-per-day cap blocks is the
*flash tier* — models Module 87 measured and rejected. Trading a measured
instrument for an unmeasured one to lift a cap that does not bind the instrument
in use is the exact failure mode this module was told to avoid.

**No backend, no Docker, no model server.** All 366 committed judge calls here are
re-scorings of the answers Module 27 already captured
(`gold32_pass{1,2,3}_outputs.json`); the system under test is frozen and the only
variable is the judge.

---

## 1. The candidate set is smaller than the brief assumed

**`llama-3.3-70b-versatile` and `moonshotai/kimi-k2-instruct` do not exist on
this account.** `GET /openai/v1/models` returns **14** models on all five key
env-vars, and neither is among them. Queried before fixing the set, as
instructed (`module109_quota_and_models.json`).

| Groq model | usable as a judge? |
|---|---|
| `openai/gpt-oss-120b` | yes — **this is `GROQ_MODEL`, the generator: the self-grading arm** |
| `openai/gpt-oss-20b` | yes |
| `qwen/qwen3.8-27b` | yes |
| `groq/compound-mini` | yes (agentic wrapper; 250 req/day, 70k TPM) |
| `qwen/qwen3.6-27b` | **no — see below** |
| `allam-2-7b` | excluded: 7B Arabic-first model, below the tier the project already retired a judge from |
| `openai/gpt-oss-safeguard-20b`, `meta-llama/llama-prompt-guard-2-*` | excluded: safety classifiers |
| `whisper-*`, `canopylabs/orpheus-*` | excluded: speech |

**`qwen/qwen3.6-27b` is unusable and the failure is invisible.** Its output cap
is **1,000 OTPM**, and DeepEval's default expected-output is 2,048, so *every*
call returns `429 Request too large … reduce max_tokens`. The harness classifies
429 as a rate limit (correctly — Module 54), retries it eight times with backoff,
and rotates keys, so the run does not fail: it **hangs**, producing nothing, for
as long as you let it. It also emits a `<think>` block inside `message.content`,
which DeepEval cannot parse into a score — that surfaces as `score=None`, i.e.
as UNSCORED, indistinguishable from a judge outage. `evaluation/groq_judge.py`
strips reasoning traces so the second failure mode cannot masquerade as the
first; the OTPM ceiling is not fixable from our side.

**The Cloudflare trap is real and worth the warning.** A plain `urllib` request
to `api.groq.com` returns `403 error code: 1010` — a WAF block on the user agent
that looks exactly like five dead keys. `httpx` and the `openai`/`groq` SDKs are
fine.

---

## 2. Quota and throughput, re-measured — and a caveat that matters more

### 2a. `.env` changed during this module

`.env` is **shared mutable state** and was rewritten at **02:21:30** while this
module was running. Both measurements are committed
(`module109_quota_and_models.json`), because a quota claim without a timestamp is
not a measurement.

| | 01:50 (as briefed) | 03:04 (after the rewrite) |
|---|---|---|
| distinct Gemini keys | **4** (`GEMINI_API_KEY` == `GEMINI_API_KEY_1`) | **5**, all different |
| live on `gemini-3.1-flash-lite` | 1 (`_2/_3/_4` → **401**) | **5** (200) |
| `gemini-2.5-flash` | 429 on the live key | 200 on one, **404** on three, 429 on one |

So **Module 98's dead-key half appears to have been fixed by another track
mid-session** — three keys that returned 401 now authenticate. The flash-tier
story is unchanged: it still cannot serve one 96-call re-score (one key at
20/day, three keys where the model is not available to the project at all, one
already 429). Nobody should quote the brief's *"one run a day with four calls of
headroom"* without re-measuring first; the number was true when it was taken and
is not now.

### 2b. Groq: four keys, not five, and the token limit binds first

`GROQ_API_KEY` and `GROQ_API_KEY_1` are **byte-identical** (same sha256 prefix,
both measurements). There are **four** distinct Groq keys, so the headroom is
~4,000 requests/day, not ~5,000. `key_manager.py` loads the numbered keys only
and so rotates exactly those four — correct here, by luck rather than design
(new defect **110**).

Measured from response headers:

* `x-ratelimit-limit-requests: 1000`, and `reset-requests` scales with the
  fraction consumed (10 used → 14m24s = 1/100 of a day) → **1,000 per DAY**.
* `x-ratelimit-limit-tokens: 8000` with `reset-tokens` always under 60 s →
  **8,000 per MINUTE**.

A FactualCorrectness call carries the ~985-token prompt plus question, gold and
answer: **~1,730 tokens mean, ~2,390 max**. So the **token limit binds first**,
at **~4.6 calls/min/key** — exactly as the brief suspected, and now measured
rather than reasoned about.

| judge | s/call, 96-run re-score | wall clock | daily headroom |
|---|---|---|---|
| `gemini-3.1-flash-lite` | **6.4 s** | **10 min** | 15/min, no daily cap hit |
| `openai/gpt-oss-20b` | 13.3 s (max 60.1) | 21 min | ~4,000 req/day |

**The Groq judge is the slower of the two**, at twice the wall clock, because the
per-minute token ceiling throttles it. The quota advantage is real but it buys
capacity nobody is short of: the shipped judge completes a full re-score in ten
minutes and is not the thing the 20/day cap blocks.

---

## 3. M7 — the headline test

Every candidate passes. The bar was ≥0.5; all five draws are 1.0 for all four.

| judge | M7, 5 draws | spread |
|---|---|---|
| `gemini-3.1-flash-lite` (baseline) | 1.0 ×5 | 0.0 |
| `openai/gpt-oss-20b` | 1.0 ×5 | 0.0 |
| `qwen/qwen3.8-27b` | 1.0 ×5 | 0.0 |
| `groq/compound-mini` | 1.0 ×5 | 0.0 |
| `openai/gpt-oss-120b` *(self-grading)* | 1.0 ×5 | 0.0 |

M7 does not discriminate. That is itself worth saying: **a judge comparison that
stopped at the headline question would have concluded that all five are
equivalent**, and §4–§7 show they are not.

---

## 4. Variance — one fixed answer, five draws, Module 87's exact protocol

The only protocol that isolates the judge from the pipeline's own
non-determinism. Nine probe questions × 5 draws = 45 calls per candidate. Cells
are `mean` then `s<spread>`.

| judge | CP1 | CP6 | CR2 | D1 | G1 | KB2 | KB6 | M2 | M7 |
|---|---|---|---|---|---|---|---|---|---|
| **`gemini-3.1-flash-lite`** | 1.0 s0.0 | 0.9 s0.0 | 0.0 s0.0 | 1.0 s0.0 | **0.9** s0.0 | 0.1 s0.0 | **0.8** s0.0 | 0.2 s0.0 | 1.0 s0.0 |
| `openai/gpt-oss-20b` | 1.0 s0.0 | 1.0 s0.0 | 0.0 s0.0 | 1.0 s0.0 | **0.2** s0.1 | 0.0 s0.0 | **0.2** s0.0 | 0.1 s**0.2** | 1.0 s0.0 |
| `qwen/qwen3.8-27b` | 0.9 s0.0 | 0.5 s**0.5** | 0.0 s0.0 | 1.0 s0.0 | **0.2** s0.0 | 0.0 s0.0 | **0.2** s0.0 | 0.2 s0.0 | 1.0 s0.0 |
| `groq/compound-mini` | 1.0 s0.0 | 1.0 s0.0 | 0.0 s0.0 | 1.0 s0.0 | **0.2** s0.1 | 0.0 s0.0 | **0.3** s**0.4** | 0.1 s0.1 | 1.0 s0.0 |
| `openai/gpt-oss-120b` *(self)* | 0.9 s**0.7** | 1.0 s0.1 | 0.0 s0.0 | 1.0 s0.0 | **0.2** s**0.2** | 0.0 s0.1 | **0.3** s**0.3** | 0.1 s0.0 | 1.0 s0.0 |

| judge | spread 0.0 on | mean spread |
|---|---|---|
| **`gemini-3.1-flash-lite`** | **9 / 9** | **0.000** |
| `openai/gpt-oss-20b` | 7 / 9 | 0.033 |
| `qwen/qwen3.8-27b` | 8 / 9 | 0.056 |
| `groq/compound-mini` | 6 / 9 | 0.067 |
| `openai/gpt-oss-120b` *(self)* | **4 / 9** | **0.156** |

**No candidate reproduces the property Module 87 was chosen for.** Removing
run-to-run variance outright was the measured reason the current judge shipped;
switching to any of these puts it back.

Two columns already show the substantive problem. **G1 collapses from 0.9 to 0.2
on every Groq model**, and **KB6 from 0.8 to 0.2–0.3**. G1 is the question
Module 87 raised from 0.53 to 0.97 after establishing that the answer carries all
four of gold's findings with gold's own figures. `gpt-oss-20b`'s own reason for
0.2 is the retired judge's mistake verbatim — *"it uses 92 accused individuals
and 28 FIRs … instead of the expected"* — which is precisely the difference the
close-numbers rule and its worked example exist to excuse. These models are not
stricter; **they stop honouring a rule that is in the prompt.**

### The self-grading arm, measured

`openai/gpt-oss-120b` is `GROQ_MODEL` — the model that generated the answers.
Judging with it is self-grading and it must not be the default regardless of how
it scores. Measuring the size of the bias is still useful, and the answer is
**there is no leniency bias to speak of**:

| judge | mean of the nine per-question means |
|---|---|
| `gemini-3.1-flash-lite` | 0.656 |
| `groq/compound-mini` | 0.513 |
| **`openai/gpt-oss-120b` (self)** | **0.500** |
| `openai/gpt-oss-20b` | 0.500 |
| `qwen/qwen3.8-27b` | 0.444 |

The generator grading itself lands **exactly on** the non-self `gpt-oss-20b` and
**below** `compound-mini`. Self-grading did not inflate the scores here. What it
did do is produce **the worst variance of any candidate** — 4/9 at spread 0.0,
including **0.7 on CP1**, a 157-character answer that is byte-identical across
all three passes. The reason to keep it out of the default is unchanged; the
reason is not the one usually given.

---

## 5. The held-out controls — every candidate passes

Module 87's three controls, reconstructed from `MODULE87_RESULT.md` §6 (their
input text was never committed — new defect **111**) and **validated**: on
Module 87's own judge the reconstruction reproduces Module 87's published numbers
exactly, so the texts are equivalent to the ones it used. Three draws each.

| judge | A — held-out polarity flip (**want HIGH**) | B — M7's facts REVERSED (**want LOW**) | C — 1401.3 → 18.0, a 78× error (**want LOW**) |
|---|---|---|---|
| `gemini-3.1-flash-lite` | **1.0 / 1.0 / 1.0** | 0.2 / 0.2 / 0.2 | 0.2 / 0.2 / 0.2 |
| `openai/gpt-oss-20b` | **1.0 / 1.0 / 1.0** | 0.0 / 0.0 / 0.0 | 0.0 / 0.2 / 0.0 |
| `qwen/qwen3.8-27b` | **1.0 / 1.0 / 1.0** | 0.0 / 0.0 / 0.0 | 0.0 / 0.0 / 0.0 |
| `groq/compound-mini` | **1.0 / 1.0 / 1.0** | 0.0 / 0.1 / 0.0 | 0.1 / 0.2 / 0.0 |
| `openai/gpt-oss-120b` *(self)* | **1.0 / 1.0 / 1.0** | 0.1 / 0.1 / 0.0 | 0.1 / 0.1 / 0.2 |

**Nobody is disqualified here.** Every candidate fires the polarity rule on a
question it has never seen and refuses to let it excuse a reversed fact or a 78×
magnitude error. That is the integrity test and all five hold it.

It is also why the controls cannot decide this on their own: they establish that
a judge has not learned leniency, and none of these had. What separates them is
§4 and §7 — whether the judge is *stable*, and whether it honours the rest of the
checklist once polarity is settled.

---

## 6. KB9 — the failure mode Module 87 caused and fenced

Module 87's unfenced polarity rule took KB9 from 0.27 to **1.0** on an answer
that never reaches gold's CrPC s.174 — the judge settled polarity and stopped
checking facts. Every candidate was checked for it. Five draws on pass 1's
answer:

| judge | KB9 ×5 |
|---|---|
| `gemini-3.1-flash-lite` (correct: fenced) | 0.4 / 0.4 / 0.4 / 0.4 / 0.4 |
| `openai/gpt-oss-20b` | 0.0 / 0.0 / 0.2 / 0.2 / 0.0 |
| `qwen/qwen3.8-27b` | 0.2 ×5 |
| `groq/compound-mini` | 0.1 / 0.1 / 0.1 / 0.2 / 0.1 |
| `openai/gpt-oss-120b` *(self)* | 0.1 ×5 |

**No candidate exhibits the failure mode** — nothing approaches 1.0. But the
reason is not that they respect the fence; it is that they are indiscriminately
low on this question, as they are on G1, KB5, KB6 and KB8. A judge that never
scores a KB answer highly cannot be caught inflating one, and that is not the
same as being right.

---

## 7. The full 96-run re-score — `openai/gpt-oss-20b`

Run for the strongest candidate on §3–§6 (best variance of the four, cleanest
controls), so it is comparable row-for-row with `module87_rescore_3pass.json`.
Same 96 captured answers, same prompt, same temperature 0.

| Q | gemini ×3 | mean | gpt-oss-20b ×3 | mean | Δ |
|---|---|---|---|---|---|
| A1 | 1.0/1.0/1.0 | 1.00 | 1.0/1.0/1.0 | 1.00 | +0.00 |
| A7 | 1.0/1.0/1.0 | 1.00 | 1.0/1.0/1.0 | 1.00 | +0.00 |
| CP1 | 1.0/1.0/1.0 | 1.00 | 1.0/1.0/1.0 | 1.00 | +0.00 |
| CP6 | 0.9/0.9/0.9 | 0.90 | 1.0/1.0/1.0 | 1.00 | +0.10 |
| CR2 | 0.0/0.0/0.0 | 0.00 | 0.0/0.0/0.0 | 0.00 | +0.00 |
| CR3 | 1.0/1.0/1.0 | 1.00 | 1.0/1.0/1.0 | 1.00 | +0.00 |
| CR4 | 1.0/1.0/1.0 | 1.00 | 1.0/1.0/1.0 | 1.00 | +0.00 |
| **CR6** | 1.0/1.0/1.0 | 1.00 | 0.9/0.4/0.4 | **0.57** | **−0.43** |
| CR7 | 1.0/1.0/1.0 | 1.00 | 1.0/1.0/1.0 | 1.00 | +0.00 |
| CR8 | 0.9/0.9/0.9 | 0.90 | 1.0/1.0/1.0 | 1.00 | +0.10 |
| CS4 | 1.0/1.0/1.0 | 1.00 | 1.0/1.0/1.0 | 1.00 | +0.00 |
| D1 | 1.0/1.0/1.0 | 1.00 | 1.0/1.0/1.0 | 1.00 | +0.00 |
| **G1** | 0.9/1.0/1.0 | 0.97 | 0.2/0.2/0.2 | **0.20** | **−0.77** |
| G2 | 0.3/0.3/0.3 | 0.30 | 0.3/0.2/0.3 | 0.27 | −0.03 |
| G3 | 1.0/1.0/1.0 | 1.00 | 1.0/1.0/1.0 | 1.00 | +0.00 |
| G5 | 0.4/0.4/0.4 | 0.40 | 0.3/0.3/0.4 | 0.33 | −0.07 |
| G6 | 0.1/0.1/0.1 | 0.10 | 0.3/0.3/0.4 | 0.33 | +0.23 |
| KB1 | 0.3/0.3/0.3 | 0.30 | 0.2/0.2/0.2 | 0.20 | −0.10 |
| KB2 | 0.1/0.2/0.2 | 0.17 | 0.0/0.0/0.0 | 0.00 | −0.17 |
| KB3 | 0.0/0.0/0.0 | 0.00 | 0.0/0.0/0.0 | 0.00 | +0.00 |
| KB4 | 0.2/0.2/0.2 | 0.20 | 0.0/0.0/0.0 | 0.00 | −0.20 |
| **KB5** | 0.9/0.9/0.9 | 0.90 | 0.2/0.3/0.2 | **0.23** | **−0.67** |
| KB6 | 0.4/0.4/0.9 | 0.57 | 0.3/0.4/0.3 | 0.33 | −0.23 |
| **KB8** | 0.9/1.0/1.0 | 0.97 | 0.3/0.3/0.3 | **0.30** | **−0.67** |
| KB9 | 0.4/0.4/0.4 | 0.40 | 0.0/0.0/0.0 | 0.00 | −0.40 |
| M1 | 0.2/0.2/0.2 | 0.20 | 0.2/0.2/0.2 | 0.20 | +0.00 |
| M2 | 0.2/0.2/0.2 | 0.20 | 0.0/0.2/0.2 | 0.13 | −0.07 |
| **M4** | 1.0/1.0/1.0 | 1.00 | 0.4/**UNSCORED**/1.0 | 0.70 | −0.30 |
| **M5** | 1.0/1.0/1.0 | 1.00 | 1.0/0.3/0.4 | **0.57** | **−0.43** |
| M7 | 1.0/1.0/1.0 | 1.00 | 1.0/1.0/1.0 | 1.00 | +0.00 |
| S2 | 1.0/1.0/1.0 | 1.00 | 1.0/1.0/1.0 | 1.00 | +0.00 |
| S3 | 1.0/1.0/1.0 | 1.00 | 1.0/1.0/1.0 | 1.00 | +0.00 |

| | `gemini-3.1-flash-lite` | `openai/gpt-oss-20b` |
|---|---|---|
| all-32 FactualCorrectness mean, 96 runs | **0.702** | 0.573 |
| pass (≥0.5) | **61 / 96** | 45 / 96 |
| questions with spread ≥0.3 | **1** | 3 |
| mean spread across 32 questions | **0.025** | 0.078 |
| UNSCORED rows | **0** | **1** |
| wall clock | **10 min** | 21 min |

**It is not a stricter judge, it is a noisier one.** CP6, CR8 and G6 go *up*
while G1, KB5, KB8, CR6 and M5 collapse — the moves are not in one direction, so
this cannot be read as a tighter standard. CR6 and M5 are answers Module 27
verified correct and Module 87 scored 1.0/1.0/1.0; here they score 0.9/0.4/0.4
and 1.0/0.3/0.4 — **the same answer, three passes, half a point apart.**

And DeepEval delivers its own verdict on the M4 row:

> `DeepEvalError: Evaluation LLM outputted an invalid JSON. Please use a better
> evaluation model.`

That is one UNSCORED row out of 96 (Module 45's rule keeps it out of the mean
rather than reading it as a zero). Against a judge with zero, on the instrument
every number in this programme is measured with, that alone would be enough.

---

## 8. Change — the seam, which ships whether or not the judge moves

`_judge()` hard-coded `GeminiModel`, so the only thing an experiment could vary
was the model *name*. That is what made Module 87's decision a quota decision.
The seam is the durable part of this module: the next candidate can be measured
without editing the scorer.

| File | Change |
|---|---|
| `evaluation/gold32_score.py` | `DEFAULT_JUDGE_PROVIDER` / `JUDGE_PROVIDER` / `GOLD32_JUDGE_PROVIDER`; `_judge()` builds Gemini (default) or Groq; temperature pinned identically on both paths; an unknown provider raises rather than falling through to Gemini |
| `evaluation/groq_judge.py` | **new** — OpenAI-compatible `DeepEvalBaseLLM` against `https://api.groq.com/openai/v1`, drawing its key from `key_manager.py`'s existing rotation and rotating on 429; strips `<think>` traces so an unparseable reply is not recorded as an outage |
| `evaluation/module109_judge_probe.py` | **new** — the comparison harness itself, including the three held-out controls **in code**, so nobody has to reconstruct them from prose again |
| `tests/test_gold32_score.py` | 2 new classes, 7 new tests (§9) |
| `HOW_TO_REPRODUCE_THIS_EVALUATION.md` | §3.1 records the provider seam, the re-measured key position, and that the default did **not** move |
| `docs/gold-qa-wave2-results/module109_*.json` | all 366 judge calls, as raw rows |

**`GOLD32_JUDGE_MODEL` still works, the Gemini path is still selectable, and the
default is unchanged**, so a reviewer can reproduce Module 87's numbers exactly.
Anything run with `GOLD32_JUDGE_PROVIDER=groq` is a different instrument and must
say so.

Rotation is wired through `key_manager.py` rather than reading one key — the
rotation was the whole argument for looking at Groq, so measuring the provider
without it would have measured the wrong thing.

---

## 9. Unit tests

```
PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/test_gold32_score.py -q
45 passed          (38 pre-existing, all still passing)

PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/test_gold32_score.py                                               tests/test_provider_failure_visibility.py -q
63 passed          (Module 87's 56, plus this module's 7)
```

| Test | Pins |
|---|---|
| `test_the_default_provider_is_still_gemini` | the instrument did not move; Module 87's numbers stay reproducible |
| `test_the_provider_is_env_overridable` | `GOLD32_JUDGE_PROVIDER`, by module reload |
| `test_an_unknown_provider_fails_loudly` | a typo must not fall through to Gemini and produce numbers under a judge no report names |
| `test_temperature_is_pinned_on_both_paths` | a judge measured at one temperature and run at another is a different instrument |
| `test_a_closed_reasoning_block_is_removed` | `<think>` traces, the measured `qwen3.6-27b` failure |
| `test_an_unclosed_reasoning_block_is_removed` | a trace truncated at max_tokens breaks the JSON identically |
| `test_ordinary_content_is_untouched` | the stripper does not corrupt a normal reply |

§3.3's rule count is untouched — the prompt is byte-identical to Module 87's, and
`test_the_doc_lists_as_many_rules_as_the_prompt_has_steps` still passes. **This
module changes no evaluation prompt and no published score.**

---

## 10. New defects found — left unfixed, tracked as their own modules

**Module 110 — `key_manager.py` rotates only the numbered keys, and `.env`
advertises one more key per provider than it has.** `_load_keys()` reads
`GEMINI_API_KEY_*` / `GROQ_API_KEY_*` and falls back to the bare
`GEMINI_API_KEY` / `GROQ_API_KEY` *only when no numbered key exists*. Today that
is harmless because `GROQ_API_KEY` and `GROQ_API_KEY_1` are byte-identical — but
that identity is also the defect: **five env names, four distinct keys**, on both
providers at the 01:50 measurement. Two modules have now computed daily headroom
by counting env names (the brief's *"5 × 20/day = 100"*; ~5,000 Groq
requests/day). The real figures are 20% lower. Either de-duplicate on load and
log the distinct count, or make the bare key participate; do not leave the
arithmetic depending on a coincidence.

**Module 111 — Module 87's held-out controls are described in prose but their
input text is not committed.** `MODULE87_RESULT.md` §6 is the only record of what
A, B and C actually said, so verifying the strongest integrity claim in the judge
upgrade required reconstructing three test cases from a table and checking the
reconstruction against the published scores. It matched exactly, so nothing is
wrong with Module 87's result — but a control that cannot be re-run is not a
regression guard. Module 109 commits its own controls in
`evaluation/module109_judge_probe.py`; the fix is to make that the home for
Module 87's too, and to add a test that pins each control's expected band.

**Module 112 — a per-model output-token cap is retried as a quota failure and
hangs the run silently.** `qwen/qwen3.6-27b` returns `429 Request too large … on
output tokens per minute (OTPM): Limit 1000, Requested 2048` on every call.
`_is_rate_limit()` matches `429`, so `measure()` spends eight retries with
backoff, then three null-retries, per metric — and no rotation can help, because
the limit is the model's, not the key's. The run does not fail; it stops making
progress, with no line in the log distinguishing it from a busy provider. A
`Request too large` / `reduce max_tokens` signature is a **permanent** failure
for that model and should abort the candidate immediately, naming it.

---

## 11. Files

| File | Contents |
|---|---|
| `module109_quota_and_models.json` | both `.env` measurements, per-key status, rate-limit headers, the 14-model inventory |
| `module109_m7_*.json` | §3, 5 draws × 4 candidates |
| `module109_probe_*.json` | §4, 9 questions × 5 draws × 4 candidates |
| `module109_controls_*.json` | §5, 3 controls × 3 draws × 5 judges (incl. the Gemini re-measurement that validates the reconstruction) |
| `module109_kb9_*.json` | §6, 5 draws × 5 judges |
| `module109_rescore_gptoss20b.json` | §7, the full 96-run re-score |
