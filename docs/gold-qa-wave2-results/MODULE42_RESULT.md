# Module 42 — KB6's `route=None` is an evaluation-harness timeout, not a pipeline failure

**Branch:** `fix/kb6-hard-failure-route-none`
**Brief:** Module 42 in `GOLD_QA_REMAINING_FIXES_PLAN.md`
**Date of every measurement below:** 2026-09-08, worktree `D:/Rapids AI/muhafiz-m42`,
backend on `:8011`, `muhafiz-postgres` healthy (73 cases), model-server tunnel
`/health` = 200.

**Chroma:** the shared store at
`D:/Rapids AI/Evidence Intelligence Platform/data/chroma_db`, **read-only**
throughout. Nothing was ingested, re-embedded, deleted or re-indexed. The full
`pytest -q` suite was never run (it empties `muhafiz_entity_descriptions`).

**Credentials ruled out first, as the brief required — and the brief's own
check turned out not to be sufficient.** `grep -c "rate limit" backend.log` was
**0** throughout. But that string matches only the Groq wording; it does **not**
match Gemini's, which is `429 RESOURCE_EXHAUSTED`. Grepping for both found a
real quota failure later in the session (§7). So the check used here, and
recommended for future runs, is:

```
grep -cE "rate limit|RateLimit|RESOURCE_EXHAUSTED|429" backend.log
```

**For the five KB6 runs specifically, the window is provably clean:** across
16:37-17:20, covering all five runs end to end, `backend.log` contains **zero
`ERROR` lines and zero `WARNING` lines** — no rate limits of either flavour, no
quota errors, no fallbacks. The first `429` in the whole session is timestamped
**17:20:16**, during the *regression sweep* that ran afterwards. KB6's numbers
predate it and are not affected by it. The 1,410-rate-limit failure that
invalidated an earlier pass is not present here and is not KB6's cause.

**`src/` is untouched by this module.** The change is evaluation-harness-only,
exactly like Module 45.

---

## 1. Root cause

**The brief's hypothesis is wrong, and it is worth saying plainly why, because
the wrong hypothesis is written into the report as a finding.**

`EVALUATION_REPORT_POST_FIXES.md` §4.1 records KB6 as the only gold question
scoring FactualCorrectness **0.0 AND** AnswerRelevancy **0.0** with
**`route=None`**, and calls it a *"genuine error, did not recover"*. The brief
reasons from that: *"`route=None` means it failed before or during
classification, not in synthesis. Start from the backend log."*

Started from the backend log. **KB6 never fails to classify.** Live, five runs,
it returns **`route='RAG'` 5 times out of 5**.

What `route=None` actually meant is that the evaluation harness **never read the
SSE stream at all**:

```python
# evaluation/gold32_run.py, before this module
return urllib.request.urlopen(req, timeout=300).read().decode("utf-8")
...
except Exception as e:
    p = {"actual_answer": "", "route": None, "status": "error", "error": str(e)}
```

KB6 measured live at **628.2s, 599.0s, 513.5s, 428.8s and 247.1s** — **four of
five runs past the hard-coded 300s ceiling**. On those four the request was
still in flight when `urlopen()` gave up. `route` is parsed out of the
`supervisor:dispatch` event in that stream, so no stream means no route; the
empty `actual_answer` then reached the scorer, which did:

```python
actual = truncate_for_scoring(o.get("actual_answer") or "(no answer produced)")
```

— handing the judge a string the pipeline never produced. The judge scored it
0.0 on both metrics. That is the entire "genuine error".

It also explains the one detail the brief flagged as most diagnostic: KB6 **did
not recover when the credential problem was fixed**, unlike 15 of the other 17
failures. Of course it didn't. Correct API keys do not make a request finish
inside 300 seconds.

This is Module 45's defect one layer earlier — *a judge `null` is not a zero*
becomes *a client timeout is not an answer* — and it is the same failure of the
harness to distinguish **"measured badly"** from **"not measured"**.

### 1.1 Why KB6 alone is slow enough to hit it

KB6 is a legal-KB question asked in **All-Cases** scope, so `rag_tool()` takes
the KB-only corpus first and the mixed pool as a fallback (`RAG tool:
legal-KB-intent query in all-cases scope → trying KB-only corpus first, mixed
pool as fallback`). Each pass runs `config.MAX_RETRIES`-bounded attempts, and
every attempt is rejected, so KB6 pays **six** full retrieve → rerank →
evaluate → rewrite rounds:

```
RAG tool: 5 chunk(s) to evaluator (attempt 1|2|3)   ← KB-only pass
Evaluator: relevant=False ... ×3
RAG tool: 5 chunk(s) to evaluator (attempt 1|2|3)   ← mixed-pool fallback
Evaluator: relevant=False ... ×3
```

Every other low scorer answers on an earlier attempt and finishes inside 300s,
which is why KB6 is the *only* row with this signature. Nothing about the
signature is special to KB6's content — it is special to KB6's **duration**.

And duration is itself downstream of the gate: KB6's one run that passed the
gate early finished in 247.1s and was captured normally, while the four that
were refused ran the full six rounds and were not. The 300s ceiling therefore
does not sample KB6 randomly — **it systematically discards the failing runs and
keeps the passing ones**, or in the reference run's case the reverse. Either
way the published row is decided by a coin flip, not by the pipeline.

### 1.2 Why it abstains — and why that is NOT Module 39's gap

The brief asked specifically whether KB6's failure is Module 39's already-filed
"the *does our data show it?* half is unreachable from the RAG sub-agent". **It
is not, and this was measured rather than argued.**

Applying §4.1's Layer 1 to the RAG path — calling `evaluate_relevance()`
directly, `prompts/evaluator.txt` unmodified, temperature 0.0, **holding the
chunk set constant** and varying only the question's phrasing.

First, the corpus is not the gap. Chunk
`5_Forensics_guidelines_pdf_62ee00b3_c19` carries gold's statutory half
verbatim:

> "Every evidence exhibit must be packaged separately. **Every firearm must be
> packaged in unloaded condition with safety on. There must not be live rounds
> in the chamber of the firearm, magazine or in the parcel.**"

Its predecessor `_c18` — which live retrieval *does* surface — ends exactly at
the heading `## Firearms and Tool Marks  In order to minimize safety risks and
contamination of evidence the f…`, and Module 30's `expand_with_neighbors()`
correctly widens it to include `_c19`'s text (asserted, not assumed: the widened
`_c18` contains the string `safety on`).

So the evaluator *was* shown gold's own words. It rejected them anyway. The 2×2,
each cell 3 runs, chunk set held constant and containing gold's text in every
cell:

| | roman-Urdu | English |
|---|---|---|
| **compound** (gold KB6 shape) | **1/3 relevant** | **3/3 relevant** |
| **norm-clause only** | **0/3 relevant** | **3/3 relevant** |

**Language decides the verdict; compoundness does not.** English 6/6, roman-Urdu
1/6. Deleting the "*aur kya hamara weapon register…*" clause — the exact clause
Module 39 is about — makes it **worse**, not better (0/3, the weakest cell).

A representative English verdict, quoting gold back:

> `relevant=True` — "The retrieved documents specify handling procedures for
> recovered firearms, including packaging separately, ensuring unloaded
> condition with safety on, and maintaining chain of custody."

and the roman-Urdu verdict on the *same chunks*:

> `relevant=False` — "The retrieved documents discuss physical handling and
> storage protocols for evidence … but do not mention any speci[fic guidelines]"

So the defect is that **the relevance gate cannot judge a roman-Urdu question
against English statutory text**. Module 30 fixed precisely this asymmetry for
*retrieval* (English statute hypotheses) and for the *cross-encoder*
(`cross_rerank_multi`) — it was never fixed for the *evaluator*, which is the
one remaining component still reading the raw roman-Urdu question.

This is a distinct defect, filed as **Module 52**. It is **not** Module 39 (which is about
composing the data half into the answer — a problem KB6 never reaches), and it
is not the credential problem.

`prompts/evaluator.txt` already contains Module 19b's compound rule
("*answering the norm clause is sufficient, full stop*"), and §1.2's 2×2 shows
that rule is not what is failing — the norm-only cell fails hardest.

**Full artefact:** `evaluation/kb6_evaluator_language_experiment.json` — all
five experiments, every verdict and reason, including the negative results.

---

## 2. Change

| File | Why here |
|---|---|
| `evaluation/gold32_run.py` | `TIMEOUT_S` (env `GOLD32_TIMEOUT_S`, default 900) replaces the hard-coded 300 that expired mid-KB6. A failed request now records `transport_ok: False` and its exception type, and the console line says the row will be left unscored. `BASE` also became env-overridable (`GOLD32_BASE_URL`) so a run on a non-default port needs no edit. |
| `evaluation/gold32_score.py` | `no_answer_captured()` / `no_answer_reason()`, and main() leaves such a row **unscored** (`None`) instead of judging `"(no answer produced)"`. `summarize()` already excludes unscored rows (Module 45), so nothing downstream changed. |
| `tests/test_gold32_transport_failure.py` (new) | 16 tests, pinned to KB6's literal gold text. |
| `evaluation/kb6_evaluator_language_experiment.json` (new) | The §1.2 measurements, kept as an artefact rather than only as prose. |

**Why the harness and not `src/`:** the reported defect — `route=None`, FC 0.0,
AR 0.0 — is *produced entirely inside the harness*. No change to `src/` can fix
a number that a 300s client timeout created. The separate, real pipeline defect
found in §1.2 is deliberately **not** fixed here; see §8.

`transport_ok` is checked with `is False`, never falsiness, so outputs files
written before the key existed keep being scored exactly as they were.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_gold32_transport_failure.py tests/test_gold32_score.py -q
```

**38 passed**, no failures, no new warnings. (`test_gold32_score.py` is Module
45's file, re-run unchanged to confirm this module did not disturb it.)

`tests/test_gold32_transport_failure.py` is new and loads KB6's question and
gold answer from `evaluation/Gold_QA_Dataset_Final32_With_Answers.json` — the
only Gold-32 file that exists — and **asserts it is present rather than
skipping**, following Module 30's precedent after PR #21 found a test that had
silently skipped for weeks on a filename that does not exist.

What it pins:

- `TIMEOUT_S != 300` and `>= 700` — the specific number that caused this, and a
  floor above the slowest run actually measured (628.2s). Env-overridable, and
  still finite so a hung request cannot wedge a run.
- `parse()` reads the route from a dispatch event, returns `None` for an unread
  stream, and — the KB6 case — **still reports `route='RAG'` for a response that
  is a deliberate abstention**. That is the distinction the report's row lost.
- A `transport_ok: False` row is unscored, and its reason says "Not a zero".
- `summarize()` excludes it: two 1.0 rows plus a transport failure still mean
  1.0, where zeroing it would have printed **0.667** — the test asserts that
  number too, so the size of the old error is pinned, not just the new code.
- **Three negative controls**, which matter more than the positives here: a
  genuine abstention, a completed request that returned an empty string, and a
  legacy row with no `transport_ok` key are **all still scored**. This fix must
  cost coverage, never launder a real failure into an excused one.

No network in the file.

---

## 4. Live verification

Question sent: KB6's **exact gold text**, byte-for-byte from
`Gold_QA_Dataset_Final32_With_Answers.json`:

> Kya forensics guidelines mein is bare mein kuch makhsoos likha hai ke baramad
> shuda aslaha darj hone se pehle kaise handle kiya jaye, aur kya hamara weapon
> register yeh darj karta hai ke us par amal hua ya nahi?

`admin@example.com` / platform-admin, All Cases, `POST /api/chat` on `:8011`,
client timeout raised to 900s so that a slow request is *observed* instead of
being turned into `route=None`.

**Five runs, as the brief required — and the fifth run changed this module's
conclusion, so it is reported first.**

| Run | `route` (from `supervisor:dispatch`) | Elapsed | Answer | Would the old 300s harness have captured it? |
|---|---|---|---|---|
| 1 | `RAG` | 628.2s | abstention (102 ch) | **no — `route=None`, published as 0.0/0.0** |
| 2 | `RAG` | 599.0s | abstention (102 ch) | **no** |
| 3 | `RAG` | 513.5s | abstention (102 ch) | **no** |
| 4 | `RAG` | 428.8s | abstention (102 ch) | **no** |
| 5 | `RAG` | **247.1s** | **substantive (851 ch)** | **yes** |

**`route='RAG'` on 5 of 5. Never `None`, on any run.** The dispatch event is
emitted by `supervisor.py` as a `PipelineEvent`, not a log line, which is why it
is absent from `backend.log` and must be read from the SSE stream.

`grep -c "rate limit" backend.log` = **0** across all five.

**KB6 is intermittent, and the brief's warning about this was well placed.**
Four runs abstain; one answers. The abstaining runs take **428.8-628.2s** — all
past the old 300s ceiling — while the answering run takes **247.1s**, inside it.
That is not a coincidence, it is the same mechanism seen from the other side: a
run that passes the relevance gate early skips the remaining retrieve → rerank →
evaluate rounds, so **answering is exactly what makes a run fast enough to be
recorded at all**.

The consequence for the report is worse than a simple timeout. Under the old
harness, whether KB6 was published as a scored answer or as a `route=None`
0.0/0.0 "genuine error" depended on which side of a **nondeterministic gate** a
single run happened to land. One run was published as a fact.

The live abstention rate (**4 of 5**) independently matches the Layer-1
measurement in §1.2, where the compound/roman-Urdu cell returned
`relevant=True` **1 of 3** on a fixed chunk set. Same gate, same flakiness, two
different measurement methods.

**Verbatim answer — runs 1-4, identical:**

> No sufficiently relevant documents were found for this question after retrying
> with query refinements.

**Verbatim answer — run 5, in full:**

> The forensic guidelines provided do not explicitly mention a "weapon register"
> or any system to track whether action has been taken on a weapon. However,
> they do specify handling procedures for evidence before registration, such as:
> - Packaging weapons in appropriate containers (e.g., cardboard boxes) and
>   sealing them with tamper-evident seals [Document 3].
> - Labeling packages with case numbers, item numbers, dates, and initials of
>   the person who packaged the evidence [Document 1, 3].
> - Maintaining the integrity of evidence through proper documentation, seals,
>   and labels [Document 3].
>
> Regarding a weapon register, the documents do not mention such a system or its
> functionality. The focus is on physical handling, packaging, and documentation
> rather than digital tracking or registration systems.
>
> Source: [Document 1], [Document 3].

So: the pipeline classifies KB6 correctly and routes it correctly on every run.
It does **not** error and it does **not** fail to classify. What varies is the
relevance gate.

---

## 5. Gold comparison

Gold (both halves):

> **Norm half** — each recovered firearm packaged separately, unloaded, safety
> on, no live round in chamber/magazine/parcel; documented, labelled, marked and
> photographed before packaging; chain of custody maintained in transport.
> **Data half** — our weapon register records almost none of that.

| Gold element | Gold | Runs 1-4 | Run 5 (the answering run) | Verdict |
|---|---|---|---|---|
| packaged separately | yes | abstains | not stated | miss |
| **unloaded, safety on** | yes | abstains | **absent** | **miss** |
| **no live round in chamber/magazine/parcel** | yes | abstains | **absent** | **miss** |
| documented / labelled / marked | yes | abstains | **labelling, documentation, seals — present** | partial hit |
| photographed before packaging | yes | abstains | not stated | miss |
| chain of custody in transport | yes | abstains | "integrity … documentation, seals, labels" | partial hit |
| **data half**: register records none of it | yes | abstains | says *the documents* don't mention a register | **wrong basis** |
| `route` | — | `RAG` | `RAG` | correct |
| Report's claimed `route` | `None` | never observed | never observed | **the report is wrong** |

**Honest verdict: KB6 still does not reach gold, and this module did not make it
reach gold.**

Even on its best run it misses the three specifics that are the heart of gold's
norm half — *unloaded*, *safety on*, *no live round* — because those words live
in chunk `_c19`, which retrieval never surfaces (§8.2). It returns the generic
evidence-packaging rules instead of the firearms rule.

Its data-half sentence looks superficially close to gold's conclusion but is
reasoning from the wrong thing: gold says *our weapon register* records none of
the handling process, whereas the answer says *the retrieved documents* do not
mention a weapon register. That is a statement about retrieval scope, not about
our data — the answer has no database access to make gold's claim. (That gap
is Module 39's, and it is real; it is simply not what makes KB6 fail.)

What this module changed is that the score is now **honestly attributed**.
Before, KB6 was published as a pre-classification crash that never happened —
`route=None`, AnswerRelevancy 0.0 — on the strength of one run that timed out.
Now it is recorded as what it is: a question that classifies and routes
correctly on every run, and is then refused by a flaky cross-language relevance
gate 4 times in 5.

**This module improves the accuracy of the measurement, not the score**, and
that distinction is the whole point of it. With the timeout raised, KB6's row
becomes either a scored abstention or a scored partial answer — both of which
the judge will rate low — instead of a fabricated 0.0/0.0. The bucket mean will
barely move; what moves is whether the number can be trusted.

---

## 6. Non-gold paraphrase

Deliberately re-worded throughout — different noun for the weapon, different
word for the guidelines, different verb for the recording step — while keeping
KB6's norm + our-data shape:

> Kya koi rehnuma usool mojood hain ke **zabt shuda pistol** ko register mein
> likhne se pehle kis tarah mehfooz kiya jaye, aur kya hamare record se pata
> chalta hai ke us par amal hua?

**Two runs. Both answered. `route='RAG'`, 97.9s and 103.8s** — no abstention,
and roughly a sixth of KB6's abstaining runtime, because the relevance gate
passes on the first attempt instead of refusing six times.

Verbatim, run 1 (excerpt):

> "Firearms must be **packaged separately**, **unloaded with the safety on**,
> and **without live rounds** in the chamber, magazine, or parcel [Document 1].
> … All evidence (including firearms) must be **marked, inventoried, and
> packaged** before leaving the crime scene … A **safe chain of custody** must
> be maintained …"

**This is the single most important measurement in this module, and it was not
the expected result.** The paraphrase returns **gold's statutory half in full**
— packaged separately, unloaded, safety on, no live round in chamber/magazine/
parcel, marked, documented, chain of custody — including the three specifics
that KB6's *own gold wording* never produces on any of its five runs.

So the corpus contains the answer, retrieval can reach it, the reranker can
keep it, the gate can pass it and the generator can write it. **Nothing in the
pipeline is missing.** What fails is specifically KB6's phrasing.

And the paraphrase shows why, which sharpens Module 52's diagnosis
considerably: the paraphrase says **"pistol"** — a word spelled identically in
English — where KB6 says **"baramad shuda aslaha"**, which shares no surface
form with "firearm" at all. The defect is not "roman-Urdu questions fail"
generically; it is that a roman-Urdu term with **no lexical overlap with the
English corpus** fails both retrieval and the relevance gate, while a loanword
sails through both.

This is the check that separates a capability fix from curve-fitting, and it
cuts the other way from usual here: it proves this module did **not** fix KB6,
and it proves the capability was there all along.

---

## 7. Regression guard

**`src/` is untouched by this module, so no pipeline regression is possible by
construction.** The seven other KB questions were nevertheless re-run live, as
the brief required, to check for a shared cause — and the sweep turned up
something more useful than a regression check.

All eight, `admin@example.com`, All Cases, `:8011`, one run each (KB6 five):

| Q | `route` | Elapsed | Outcome | Over the old **300s** ceiling? |
|---|---|---|---|---|
| KB1 | `RAG` | 268.0s · **403.0s** (2 runs) | answers (1,092 / 1,830 ch) | **YES on 1 of 2** |
| KB2 | `RAG` | **383.3s** | answers (1,148 ch) | **YES** |
| KB3 | `RAG` | **481.2s** | verifier refusal (82 ch) | **YES** |
| KB4 | `RAG` | 219.8s | answers (1,045 ch) | no |
| KB5 | `RAG` | 247.0s | answers (1,382 ch) | no |
| **KB6** | `RAG` | **428.8-628.2s** (4/5) · 247.1s (1/5) | abstains 4/5, answers 1/5 | **YES on 4 of 5** |
| KB8 | `RAG` | 184.4s | verifier refusal (82 ch) | no |
| KB9 | `RAG` | **341.0s** | abstains (102 ch) | **YES** |

**`route='RAG'` on all eight.** Not one `None` anywhere, on any run. Whatever
produced `route=None` in the reference run, it is not something the router or
the supervisor does to KB questions.

**Nothing got worse.** KB1, KB2, KB4 and KB5 answer, as Module 30 left them.
**KB4 answers (1,045 ch)** — its known open regression (Module 38) is a
*content* regression, and this module did not touch it either way. KB3 and KB8
return the verifier refusal *"The generated answer could not be verified as
grounded in the retrieved documents"* rather than Module 30's recorded answers;
that is a change from Module 30's run, but this module changed no `src/` file,
so it cannot be its cause — Module 30 also ran against a **private** Chroma copy
while this ran against the shared store, which is the more likely difference.
Recorded as an observation, not claimed as a finding.

### The generalisation — this is not a KB6-only problem

**Five of the eight KB questions exceed the old 300s ceiling on this machine:
KB1, KB2, KB3, KB9, and KB6.** Every one of them would have been recorded by the
old harness as `route=None` with an empty answer and published as
FactualCorrectness 0.0 **and** AnswerRelevancy 0.0.

KB1 is the plainest demonstration that this is a coin flip rather than a
property of the question: run 1 took **268.0s** and run 2 took **403.0s**. The
*same question* lands on either side of the ceiling from one run to the next,
and KB1 is a question the report scores as **working**.

**KB2 is the one that should worry a reader most.** It takes 383.3s and returns
a perfectly serviceable 1,148-character answer — which the old harness would
have thrown away and scored 0.0/0.0. The report lists KB2 at FC 0.0 / AR **1.0**,
so it evidently finished inside 300s in the reference run; on this machine it
does not. That is the whole hazard in one row: **the same question, the same
code, scored two completely different ways depending on how loaded the box was.**

This is why the fix is a `transport_ok` flag and an unscored row rather than
just a bigger number. A slower machine will still cross whatever ceiling is set;
what matters is that it then says so, instead of publishing a 0.0.

### One environment failure, reported rather than smoothed over

KB1's **first** attempt failed at 72.4s with no answer and no route:

```
google.genai.errors.ClientError: 429 RESOURCE_EXHAUSTED ...
Quota exceeded for metric: generativelanguage.googleapis.com/
generate_content_free_tier_requests, limit: 20, model: gemini-2.5-flash
```

The Gemini free-tier **daily** quota (20 requests) was exhausted by the cloud
escalation path partway through the session. Retried, KB1 answers normally in
268.0s, so the row above is the retry and the failure was transient.

Two things follow. First, this is the credential/quota class of failure the
brief warned about, in a **different provider from the one the brief names** —
and `grep -c "rate limit"`, the check the brief specifies, **does not match it**
(0 hits while it was happening). Future runs should grep for
`RESOURCE_EXHAUSTED` and `429` as well. Second, the first `429` in the session is
timestamped 17:20:16, **after** all five KB6 runs completed at 17:19, and the
16:37-17:20 window contains zero `ERROR` and zero `WARNING` lines — so KB6's
measurements are unaffected by it.

---

## 8. New defects found

### 8.1 The relevance gate cannot judge a roman-Urdu question against English statute text — **filed as Module 52**

§1.2's measurement, restated as the defect: with gold's own statutory text in
the chunk set, `evaluate_relevance()` returns `relevant=True` **6/6** for an
English phrasing and **1/6** for the roman-Urdu one. This gates the whole
roman-Urdu half of the KB bucket, not just KB6.

**Deliberately not fixed here.** Three reasons, in order of weight:

1. **The obvious place to fix it is out of bounds.** `prompts/evaluator.txt` is
   explicitly excluded by this module's brief ("Module 19b's fix is correct and
   merged"), and §1.2 shows the compound rule already in that file is not the
   failing part anyway.
2. **No cheap wiring change works.** `evaluate_relevance()` already receives a
   second query argument, and every way of feeding it English through the
   existing signature was measured and is unreliable: the Module 30 statute
   hypothesis as `rewritten_query` **1/3**, as both arguments **2/3**, the live
   retry rewrite **0/3**, appending an English rendering to the original
   **0/3**. Only a genuine English *question* reaches 3/3. That needs a real
   translation step — a new prompt, a new call on the KB path, and its own live
   re-verification of all eight KB questions.
3. **The blast radius is the whole KB bucket**, which is already carrying an
   open regression (Module 38, KB4). Shipping a broad change to the shared RAG
   path without time to re-verify all eight questions live would be worse
   engineering than reporting it precisely.

**Proposed fix, evidence-backed:** on the legal-KB path only, give the evaluator
an English rendering of the *question* (not a statute hypothesis and not a
keyword rewrite), mirroring exactly what Module 30 did for the cross-encoder.
§6's paraphrase is the existence proof that this works: change one noun to a
word English shares, and the same pipeline returns gold's rule verbatim.
Expected side benefit: if the gate passes on attempt 1, KB6 stops paying six
retrieve/rerank/evaluate rounds and its runtime collapses from ~600s to ~100s,
which removes the timeout pressure at its source.

### 8.2 KB6's gold chunk `_c19` is never retrieved directly

Across five runs, retrieval surfaced `_c0`, `_c5`, `_c18`, `_c114`, `_c116` and
Anti-Rape Act chunks, but **never `_c19`**, the chunk that states gold's rule.
It arrives only indirectly, via `expand_with_neighbors()` widening `_c18` — and
only on runs where `_c18` is retrieved at all, which was not all of them.

Lower priority than 8.1, and **fixing it alone would not fix KB6**: §1.2's
IDEAL-set experiment put `_c19` in the pool explicitly and the roman-Urdu
verdict was still `relevant=False`. Recording it so it is not re-discovered.

§6 shows both halves are the same underlying cause. The non-gold paraphrase —
which says **"pistol"**, a word identical in English — retrieves `_c19` *and*
passes the gate *and* quotes gold's rule in full, in ~100s. KB6's
**"baramad shuda aslaha"** shares no surface form with "firearm" and fails at
both stages. So 8.2 is not a separate retrieval bug to fix on its own; it is
Module 52 seen one stage earlier, and an English rendering of the question
would address both.

### 8.3 Half the KB bucket crosses the old 300s ceiling — measured, not feared

This was filed as a suspicion and the §7 sweep turned it into a measurement.
**Five of the eight KB questions exceed 300s on this machine** — KB1 (403.0s on
the second of two runs), KB2 (383.3s), KB3 (481.2s), KB9 (341.0s) and KB6 (4
runs of 5). Under the old harness all
four would have been published as `route=None` / FC 0.0 / AR 0.0, including
**KB2, which returns a good 1,148-character answer**.

The report lists KB2 at FC 0.0 / AR **1.0**, so it finished inside 300s in the
reference run and does not here. Same question, same code, two different
scores, decided by machine load. That is the defect generalised: KB6 is simply
the question that lost the coin flip on 2026-09-08.

**The 2026-09-08 numbers cannot be re-checked against this** — the artefacts
are not on this machine (Module 47). What can be said is that any row in that
report showing `route=None` should now be treated as *unmeasured* rather than
as a pipeline finding, and that with `transport_ok` recorded a future run says
so out loud instead of publishing a zero.
