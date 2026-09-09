# Module 54 — the two provider-failure gaps Module 46 did not close

**Branch:** `fix/provider-failure-visibility` off `main` @ `a841f5d`
**Type:** verification-integrity module. No pipeline behaviour changes, no
answer changes, no scores moved. What changes is whether a reader can *tell*
that a run was degraded.

**Credit where it belongs.** Half of the brief's first item was already fixed
before this module started. **Module 46**, prompted by Module 42's transient
Gemini 429, widened the eval preflight to
`grep -ciE "rate limit|RESOURCE_EXHAUSTED|429|quota"` and established the fact
the whole thread rests on: *Groq says `rate limit`; Gemini says
`RESOURCE_EXHAUSTED`.* Module 50 then reproduced that same failure
independently, which is good corroboration that it is real and recurring —
but it is corroboration, not a second discovery. **That half needed no further
work and this module did none.** What follows is the residual.

---

## 1. Root cause

Two defects, one shape. It is the shape Modules 42 and 45 already named: **a
failed call that leaves no trace is indistinguishable from a successful one**,
and every check that was supposed to catch it returns a clean zero. Module 45
put it as "a judge null is not a zero"; Module 42 put it as "an empty answer is
not an abstention". This is the same principle applied to the provider layer.

### (1) `503 UNAVAILABLE` was in neither grep

Module 50 captured, live:

```
503 UNAVAILABLE ... 'This model is currently experiencing high demand.'
```

That is a transient **capacity** failure, not a quota failure. It is not a 429,
it does not say `RESOURCE_EXHAUSTED`, and it does not contain the word "quota"
— so it is invisible to both the bare `grep -c "rate limit"` **and** to Module
46's widened pattern. The consequence is identical to a quota failure: the call
did not happen, and the request completed down a degraded path.

**Why this matters concretely, and it is worse than it first sounds.** Every
module in this wave certified its live runs by reporting `grep -c "rate limit"`
= 0 — Modules 35, 36, 41, 44 all say so verbatim in their result files. That
number is not evidence of a clean run. It is 0 over a Groq rate limit only by
accident of wording, and it is 0 over a Gemini 429, a bare 429, and a 503
*always*. Module 50 hit exactly this and said so at the time.

A second, smaller gap was found while pinning this down, and it belongs to the
same root cause: `gold32_score.py`'s in-code classifier `_is_rate_limit()`
matched `"RateLimit"` and `"rate_limit"` **case-sensitively**, and Groq's actual
message is `due to rate limits` — a space, lower case. The code classifier and
the prescribed log grep disagreed about the *most common* of the four
signatures, in opposite directions. A Groq throttle was therefore being charged
to the 3-attempt null-retry budget instead of the 8-attempt rate-limit budget it
was designed to have.

### (2) A failed cutover classification was silently invisible in the SSE stream

Both of Module 50's failures went through `src/main.py`:

```python
logger.warning("Cutover classification failed, falling back to orchestrator.py: %s", exc)
```

The fallback itself is correct and is not the defect. The defect is that it was
a `logger.warning` **and nothing else**. When it fires, `cutover_route` stays
`None`, the harness is skipped, and `process_query()` re-routes the question for
itself — so **a different sub-agent answers than the one being measured**.
Module 50's G1 paraphrase moved from Meta-Analysis to Cross-Case Linkage's
relevance-gate refusal purely this way. A reader of the SSE stream saw a normal,
fluent, wrong answer and had no way to distinguish it from a routing
regression without opening `backend.log`.

### Where the brief was wrong

The brief and the plan both state that **`WAVE2_ORCHESTRATION_PROMPT.md` still
prescribes the bare `grep -c "rate limit"` for live module verification**. It
does not. `WAVE2_ORCHESTRATION_PROMPT.md` contains **no log check at all** —
`grep -ni 'rate\|limit\|429\|quota'` over it returns nothing relevant, and its
§4 "Non-negotiable verification standard" has five numbered requirements, none
of which mention the backend log.

That is arguably worse than the brief's version, not better: the wave's
orchestration document never told anyone to check for provider failure, and
every module that did it anyway inherited the bare form from
`HOW_TO_REPRODUCE_THIS_EVALUATION.md`. The fix is therefore an **addition** to
§4 rather than an edit, and it is reported here rather than silently
reinterpreted.

---

## 2. Change

Seven files. The canonical pattern now lives in exactly one place and the
tests assert that the documents agree with it.

| File | Change |
| --- | --- |
| `evaluation/gold32_score.py` | `_RATE_LIMIT_MARKERS` gains `UNAVAILABLE`, `503` and `rate limit`; `_is_rate_limit()` is now case-insensitive. New module-level `LOG_GREP_PATTERN` — the same four signatures as a `grep -E` alternation, so the docs and the code cannot drift. |
| `src/main.py` | The cutover-classification failure is carried out of the `except` into `event_generator()`, which emits one SSE event carrying `cutover_classification_failed: True` before any pipeline event. The fallback is otherwise untouched. |
| `evaluation/gold32_run.py` | `parse()` records `cutover_classification_failed` per row (explicit `False` when clean, `None` when the stream was never read), and the console line calls it out during a run. |
| `HOW_TO_REPRODUCE_THIS_EVALUATION.md` | Bare `grep -c "rate limit"` → the widened pattern, with the four signatures explained and the cutover check added alongside. |
| `WAVE2_ORCHESTRATION_PROMPT.md` | New §4 item 6 — both checks, stated as invalidating a run. This file previously prescribed no log check at all. |
| `docs/gold-qa-wave2-results/MODULE27_PREFLIGHT.md` | Module 46's pattern gains `\|UNAVAILABLE\|503`; a new required item 3 for the cutover check (subsequent items renumbered). This protects Module 27's final rerun, which is the point of the module. |
| `GOLD_QA_REMAINING_FIXES_PLAN.md` §"Before any future run" | Same widening. |

**Why the SSE event, and why there.** Three options were considered.

- *A field on the existing `response` event* — rejected: it arrives last, so a
  run that times out mid-answer loses the marker precisely when it matters
  most.
- *Leave it to the log and only widen the grep* — rejected: the brief's
  requirement is that a reader of the stream, or of
  `gold32_pipeline_outputs.json`, can tell. A log-only signal fails both.
- *A separate `step: "routing"` event emitted first* — chosen. It costs three
  lines at the top of `event_generator()`, changes no control flow, and is
  additive for every consumer: `gold32_run.py`'s `parse()` and the frontend's
  answer accumulator both key off `step == "response"`, so neither sees it
  unless it looks. `status` is `"warning"`, not `"error"` — the request did
  succeed; it just succeeded somewhere else.

The event carries the provider exception verbatim, so the reader learns *why*
as well as *that* — and so the same widened pattern matches the stream text,
not only the log.

`src/llm/client.py`'s own `_is_rate_limit()` (the retry classifier) was
deliberately **not** touched — see §8.

---

## 3. Unit tests

```
PYTHONPATH=. .venv/Scripts/python.exe -m pytest \
  tests/test_provider_failure_visibility.py tests/test_gold32_score.py \
  tests/test_gold32_transport_failure.py tests/test_harness_cutover.py \
  tests/test_api.py tests/test_uuid_validation.py tests/test_llm_client.py

197 passed, 1 warning in 7.89s
```

New file `tests/test_provider_failure_visibility.py` — **18 tests**, no
network, no live stack. This module fixes no gold question, so there is no
literal-gold-text regression test to pin; the equivalent anchor here is the
**verbatim provider strings**, which is what actually regressed:

- All four signatures are pinned as the providers emit them, including Module
  50's `503 UNAVAILABLE ... 'This model is currently experiencing high
  demand.'` verbatim, and each is asserted to match `LOG_GREP_PATTERN`.
- Six lines of **ordinary healthy-log noise** are asserted *not* to match. A
  check that always fires certifies as little as one that never fires.
- `test_the_bare_pattern_is_the_thing_being_fixed` states the regression as an
  assertion: the bare `rate limit` grep sees exactly **one of the four**, and
  the three it misses are named.
- `test_judge_error_classifier_agrees_with_the_grep_pattern` pins the code
  classifier and the documented grep to each other. This is the test that
  caught the case-sensitivity gap in §1.
- Every prescriptive doc is asserted to contain `LOG_GREP_PATTERN`, and to
  contain no runnable bare-grep command. Result files under
  `docs/gold-qa-wave2-results/` are deliberately **excluded** — they are the
  record of what was measured at the time, and rewriting the check they ran
  would falsify the record.
- The fallback event is exercised through the **real FastAPI app** via
  `TestClient` against `/api/chat`: only `route_query` (made to raise a real
  503 string) and `process_query` are faked. Asserted: exactly one flagged
  event, it is **first** in the stream, it names `orchestrator.py`, it carries
  the provider string, and — the regression guard on the fix itself — the
  legacy pipeline still ran and still received `precomputed_route=None`.
- The mirror case: a *successful* classification emits no marker.
- `parse()` records `True`, records explicit `False` rather than omitting the
  key (Module 42's principle — a missing key reads as "did not happen" when it
  means "was not measured"), and does not disturb existing `route=` or answer
  capture.

---

## 4. Live verification — **deferred, deliberately**

**No live run was performed. This is a deferral, not an omission, and not a
claim of success.**

Machine state at decision time: `8016` (Modules 53/57/40, Meta-Analysis) and
`8018` (Module 52, RAG/evaluator) both `/health` 200 and mid-wave; **5.02 GB
free, 70.4% memory in use**. Module 50 measured that extra concurrent backends
*alone* caused sub-query timeouts, and this brief is explicit that a degraded
measurement is worse than a deferred one.

Two further reasons specific to this module:

1. A live confirmation would require **forcing** a classification failure —
   the real 503s are transient and cannot be summoned on demand. Forcing it
   means patching the router at runtime, which is exactly what the unit test
   already does, against the same real `event_generator()`, through the same
   real `/api/chat` route. A third backend would buy a slower copy of a test
   that already passes.
2. This module changes no answer, no route, and no score. There is no gold
   question whose output could differ, so the usual live-verification value —
   reading the actual answer — does not apply.

**What is therefore unverified:** that the event survives the real
`StreamingResponse` chunking end-to-end over the wire to a real client, and
that no frontend consumer chokes on an unrecognised `step: "routing"` event.
The first is exercised by `TestClient`, which does run the real ASGI stack; the
second is reasoned about (the frontend accumulates on `step == "response"`) but
**not measured**. Whoever runs Module 27 will get the real end-to-end
confirmation for free, and should say so.

---

## 5. Gold comparison

**Not applicable, and that is the honest answer rather than a skipped
section.** This module fixes no gold question and moves no metric. Its output
is not an answer but a property of the measurement apparatus: after it, a run
that reports a clean provider check has actually been checked against all four
signatures instead of one.

The nearest thing to a measurable claim is the count of signatures covered:

| Signature | bare `grep -c "rate limit"` | Module 46's pattern | This module |
| --- | --- | --- | --- |
| Groq `due to rate limits` | ✅ | ✅ | ✅ |
| Gemini `RESOURCE_EXHAUSTED` | ❌ | ✅ | ✅ |
| bare `429` | ❌ | ✅ | ✅ |
| `503 UNAVAILABLE` | ❌ | ❌ | ✅ |
| `Cutover classification failed` | ❌ | ❌ | ✅ (stream + artefact + log) |

1 of 5 → 3 of 5 → 5 of 5. Module 46 owns the middle column.

---

## 6. Non-gold paraphrase

The analogue of a paraphrase here is **a failure the module was not written
against**. The 503 string is the one Module 50 captured, so matching it proves
little on its own. The generalisation checks are:

- The bare `429` case (`"HTTP error 429 returned by the provider"`) — a
  wording neither Module 42 nor Module 50 captured, constructed for this test,
  matched.
- Six healthy-log lines drawn from *different* subsystems (uvicorn access,
  router, embedder, supervisor, verifier, startup) — none match. Note that
  `"INFO: 127.0.0.1:52134 - \"POST /api/chat HTTP/1.1\" 200 OK"` is a
  deliberately adversarial one: it is a line full of digits, and a lazier
  pattern (`[45]\d\d`, or an unanchored `50`) would have fired on it.
- The fallback test raises a **503**, not the generic exception the code was
  originally written around, confirming the two halves compose: the marker
  event's own text matches the widened log pattern.

---

## 7. Regression guard

The changed files are load-bearing for the eval harness and for every chat
request, so the guard is the suites that own them:

| Suite | Result |
| --- | --- |
| `tests/test_gold32_score.py` | pass — including `test_rate_limits_do_not_consume_the_null_retry_budget` and `test_rate_limit_retries_are_themselves_bounded`, the two that pin the behaviour `_is_rate_limit()` feeds |
| `tests/test_gold32_transport_failure.py` | pass — Module 42's `transport_ok` contract, which `parse()`'s new key sits beside |
| `tests/test_harness_cutover.py` | pass — the cutover adapter, unchanged |
| `tests/test_api.py` | pass — the HTTP layer, including `/api/chat` auth and ownership |
| `tests/test_uuid_validation.py` | pass — the `/api/chat` boundary validation just above the changed code |
| `tests/test_llm_client.py` | pass — `src/llm/client.py` untouched, pinned as such |
| **Total** | **197 passed** |

Two behaviour changes were checked for blast radius by hand:

- **`_is_rate_limit()` is now case-insensitive and matches more strings.** The
  only consequence is *which retry budget* a failed judge call is charged to.
  It can now spend up to 8 attempts where it previously spent 3 on a Groq
  throttle — which is the behaviour Module 46 designed and which the
  case-sensitivity was silently defeating. It cannot turn a scored row into an
  unscored one; it can only turn an unscored row into a scored one.
- **A new SSE event type.** Emitted only on the failure path, only once, ahead
  of everything else, and ignored by both known consumers.

The full `pytest -q` suite was **not** run, per the brief — it empties
`muhafiz_entity_descriptions`.

---

## 8. New defects found

1. **`src/llm/client.py:41`'s `_is_rate_limit()` does not know about 503
   either**, and it is a *different* function from the eval one — it decides
   whether the pipeline retries and rotates keys. A 503 currently is not
   treated as retryable there at all, which is plausibly why Module 50's
   requests fell through to the cutover fallback rather than surviving on a
   retry. **Deliberately left unfixed:** that function governs live retry and
   key-rotation behaviour on every model call in the system, `_is_payload_too_large()`
   sits right beside it precisely because a previous over-broad classification
   there burned the retry budget and killed a chat turn (scenario-verify
   Finding U), and changing it is a behaviour change, not a visibility one.
   This module was scoped to visibility. **Worth its own module**, with a
   measured before/after on retry counts.

2. **`WAVE2_ORCHESTRATION_PROMPT.md` never prescribed a log check at all** —
   see §1. Fixed here by addition, but it means the wave's modules were each
   independently improvising the check, which is how the bare form propagated
   through six result files.

3. **Six result files record `grep -c "rate limit" = 0` as their provider
   certification** (Modules 35, 36, 41, 44, and two runs inside 42 and 50).
   Those numbers are now known to be weak evidence. They were **deliberately
   not rewritten** — a result file is the record of what was measured, and
   editing it would falsify the record. The correct remedy is that Module 27's
   rerun uses the widened check, which is what
   `MODULE27_PREFLIGHT.md` now requires. Anyone re-reading those six modules
   should treat "no rate limit lines" as "no *Groq* rate-limit lines".

4. **The fallback's blast radius is still unmeasured.** We now know when it
   fires, but nothing measures how often it fires across a full 32-question
   run, or which routes it costs. Once Module 27's rerun lands with this
   change in place, `gold32_pipeline_outputs.json` will carry that number for
   the first time — one `python -c` over the artefact. Worth reading before
   trusting the run.
