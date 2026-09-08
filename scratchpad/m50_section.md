# Module 50 — G1/G6/CR3 consolidation: the aggregates existed but nothing called them ✅

**Done.** Branch `fix/meta-analysis-wire-g1-g6-aggregates`. Full write-up:
`docs/gold-qa-wave2-results/MODULE50_RESULT.md`.

**The brief's diagnosis was right in every particular** and nothing had to be
re-derived. Confirmed on the base commit by dispatching each of the six
pinned sub-query strings individually through `/api/chat`: all six reached
their aggregate and returned gold's numbers, and all six were unreachable
from G1/G6/CR3. The defect was an **absent call**, not a wrong answer.

**Wired** — the six strings copied byte-identical from where Modules 31–36
pinned them in `tests/test_xagg.py`, with a test asserting both copies stay
equal:

| Plan | Sub-queries now |
|---|---|
| `record_consistency` (CR3) | **M36 filtered FIR listing** (placed first, as `[Document 1]`), person recurrence, CMS linkage |
| `orientation_note` (G6) | district spread, case mix by year, **M35 arrest rate**, reporting speed, weapon licence |
| `caseload_review` (G1) | **M31 age**, **M32 relationship**, **M33 seized property**, **M34 time-of-day**, person recurrence |

`caseload_review` was **re-composed, not extended**. Gold's G1 states its own
method — *"profile the accused, the victims, the property and the timing"* —
and its four findings are exactly Modules 31–34's four aggregates. Module
29's five scans answered a legitimate but different question, as its own
result file recorded.

**`_MAX_SUB_QUERIES` settled at 5, and the brief's framing of the cost was
wrong.** It is not primarily latency or model spend: `META_ANALYSIS_SUBQUERY_TIMEOUT`
(60 s) is a **single wall-clock deadline shared by the whole fan-out**, and
the shared model server serialises the sub-queries into a staircase, so the
deadline measures **queue position**. Measured live:

| N | Last sub-answer | Timeouts | Synthesis |
|---|---|---|---|
| 3 (CR3) | +25.0 s / +29.2 s | 0 / 0 | grounded |
| 5 (G1 baseline) | +56.7 s / +53.5 s | 0 / 0 | grounded |
| 6 (G6) | +41.1 / +57.7 / +58.1 s | 0 / 0 / **1** | last run **NOT grounded** |
| 9 (G1) | +55.4 s for 5 of 9 | **4** | 4 findings missing |

At N=9 two of the four killed sub-queries were the age and time-of-day
aggregates the wiring exists to reach. Raising the cap buys timeouts, not
coverage. The plan cap is now a separate constant (`_MAX_PLAN_SUB_QUERIES`)
from the LLM-decomposer cap, because a plan was previously **silently
truncated** by the LLM-path cap — a nine-entry `caseload_review` would have
dispatched only its first five with nothing saying so.

**Cost of holding the line, stated plainly:** G6 dropped `_SQ_GENDER` to fit
the arrest rate, so gold's "mostly men" element is no longer computed.

**Live:** every wired aggregate fires on **every** run — `caseload_review`
7/7 for all four kinds, `orientation_note` 6/6, `record_consistency` 3/3.
G1's clean run reports all four gold findings (24–49/mean 31.5; اجنبی 15 of
24; 13 forensic-lab / 7 heirs; time-of-day). CR3 now quotes gold's exact
`CMS-ISB-2026-0341` — Module 29 had `CMS-KHI-2026-0417`. G6 reports the
arrest rate in 3 of 4 runs, having reported it in none before.

**Honest caveats.** (a) On gold's G1 finding (4) the answer deliberately does
**not** say "flat across the day": Module 34 established that is a date-only
artefact (14 rows at 00:00:00) and the corrected reading is reported instead.
(b) G6's measured arrest rate is **1 in 6.6**, not gold's 1 in 9. (c) Several
runs degraded because three other backends (8012/8014/8015) were driving the
same model server concurrently, and two paraphrase runs died on provider
`503` / `429 quota exceeded` — reported, not worked around.

---

# Module 51 — the sub-query timeout measures queue position, not cost ⬜

**Found by Module 50**, and the single change that would most improve
Meta-Analysis reliability.

`meta_analysis.py::_dispatch_one()` wraps each sub-query in
`asyncio.wait_for(..., timeout=META_ANALYSIS_SUBQUERY_TIMEOUT)` and
`asyncio.gather()`s all N at once — so every sub-query shares **one 60 s
wall-clock deadline starting at fan-out**. The shared model server does not
run them in parallel; it serialises them into a ~6–10 s staircase. A
sub-query is therefore killed for being **served last**, not for being slow.
Module 50 measured the last slot consuming 53–58 s of the budget at N=5 even
on a quiet machine, and failing outright under contention.

**The waste is specific and avoidable:** the `XAGG <kind>` log line is
present for every timed-out sub-query, so the aggregate had **already
computed**. What is discarded is only its LLM paraphrase.

**Work:** either dispatch in bounded batches so each batch gets a fresh
window, or — cheaper — serve the raw aggregate when a sub-query's paraphrase
times out, exactly as `large_scale_aggregate.py` already does when the
*verifier* rejects a paraphrase. `src/config.py`'s 60 s default was out of
Module 50's scope and should be revisited here.

**Verify:** G1/G6/CR3 several runs each with no other backend running;
confirm zero dropped sub-answers at N=5. **This is the prerequisite for
reconsidering `_MAX_PLAN_SUB_QUERIES`** — Module 50 measured three gold G6
elements that fit only if this is fixed first.

---

# Module 52 — the wave's quota check does not match the provider's errors ⬜

**Found by Module 50.** `WAVE2_ORCHESTRATION_PROMPT.md` and every module
brief prescribe `grep -c "rate limit" backend.log` as the credential/quota
check, on the strength of a previous pass invalidated by a stale `.env`.

Module 50 hit two real provider failures live and **that grep returned 0 for
both**:

```
503 UNAVAILABLE ... 'This model is currently experiencing high demand.'
429 RESOURCE_EXHAUSTED ... 'Quota exceeded for metric:
    generativelanguage.googleapis.com/generate_content_free_tier_requests,
    limit: 20, model: gemini-2.5-flash'
```

Both silently fell back from the harness to `orchestrator.py`
(`src/main.py`'s `Cutover classification failed` path), which changes which
sub-agent answers — indistinguishable from a routing bug unless you read the
warning.

**Work:** widen the prescribed check to
`RESOURCE_EXHAUSTED|429|quota|UNAVAILABLE|503`, in the runbook and in every
brief that copies it. Consider whether a cutover-classification failure
should be surfaced in the SSE stream rather than only in `backend.log`.

