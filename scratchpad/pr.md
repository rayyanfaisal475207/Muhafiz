## What this does

Modules 31–36 each built, unit-tested and **live-verified** an XAGG aggregate, and each one deferred the wiring because `harness/agents/meta_analysis.py` was owned by another track throughout. Module 31's own result file says it plainly: *G1's answer is unchanged.* Six correct aggregates sat behind three `sub_queries` tuples that never named them.

**The defect was an absent call, not a wrong answer.** Confirmed on the base commit by dispatching each of the six pinned strings individually through `/api/chat` — all six reached their aggregate and returned gold's numbers, and all six were unreachable from G1/G6/CR3.

| Plan | Question | Added |
|---|---|---|
| `record_consistency` | CR3 | **M36 filtered FIR listing**, placed first so the synthesis goal can cite it as `[Document 1]` |
| `orientation_note` | G6 | **M35 arrest rate** |
| `caseload_review` | G1 | **M31 age**, **M32 relationship**, **M33 seized property**, **M34 time-of-day** |

Every string is copied **byte-identical** from where its module pinned it in `tests/test_xagg.py`, with a test asserting both copies stay equal — a human-invisible reword changes which `run_aggregate()` family matches.

`caseload_review` is **re-composed, not extended**. Gold's G1 states its own method — *"profile the accused, the victims, the property and the timing"* — and its four findings are exactly Modules 31–34's four aggregates.

## `_MAX_SUB_QUERIES` — settled at 5, with the measurement

The brief's framing of the cost turned out to be wrong. It is not latency or model spend: `META_ANALYSIS_SUBQUERY_TIMEOUT` (60 s) is **one wall-clock deadline shared by the whole fan-out**, and the shared model server serialises the sub-queries into a staircase — so it measures **queue position**, not sub-query cost.

| N | Last sub-answer | Timeouts | Synthesis |
|---|---|---|---|
| 3 (CR3) | +25.0 s / +29.2 s | 0 / 0 | grounded |
| 5 (G1 baseline) | +56.7 s / +53.5 s | 0 / 0 | grounded |
| 6 (G6) | +41.1 / +57.7 / **+58.1** s | 0 / 0 / **1** | last run **NOT grounded** |
| 9 (G1) | +55.4 s for 5 of 9 | **4** | 4 findings missing |

At N=9, two of the four killed sub-queries were the **age and time-of-day aggregates this PR exists to reach**. Raising the cap buys timeouts, not coverage.

The plan cap is split out as `_MAX_PLAN_SUB_QUERIES` — without it a nine-entry `caseload_review` would have dispatched only its first five, **silently**.

**Cost of holding the line, stated rather than glossed:** `orientation_note` drops `_SQ_GENDER` to fit the arrest rate, so gold's "mostly men" element is no longer computed.

## Live results

Port 8013, platform-admin, All Cases. **Every wired aggregate fires on every run:** `caseload_review` **7/7** for all four kinds, `orientation_note` **6/6**, `record_consistency` **3/3**.

- **G1** — clean run reports all four gold findings: 24–49 mean **31.5**; **اجنبی 15 of 24**; **13** forensic-lab / **7** heirs; the time-of-day distribution. Previously *none* of gold's four findings was reachable.
- **CR3** — 2 of 3 runs correct, now quoting gold's exact **`CMS-ISB-2026-0341`** (Module 29 had `CMS-KHI-2026-0417`).
- **G6** — arrest rate reported in **3 of 4** runs, having reported it in **none** before.

**Regression guard:** M2, G2, G5, S2, S3, D1 all unchanged and correct; Module 41's guard still fires for G2/G5, which still do not decompose.

**Unit:** 613 passed (meta-analysis + supervisor + xagg + tool-xagg + router); 795 passed, 1 xpassed on the broader harness sweep. One existing test changed **premise, not assertion** — Module 29's composition pin, which this PR deliberately changes.

## Reported honestly, not tuned

- Gold's G1 *"fairly flat across the day"* is a **date-only artefact** (Module 34: 14 rows at 00:00:00). The answer gives the corrected reading instead, and no wording was tuned toward gold's.
- The measured arrest rate is **1 in 6.6**, not gold's 1 in 9.
- "All Pakistani nationals" and CR3's complainant name are **not in the data model**.
- Several runs degraded because **three other backends (8012/8014/8015)** were driving the same model server, against the runbook's cap of two.
- **Two paraphrase runs died on provider `503` / `429 quota exceeded`.** No clean synthesised paraphrase answer was captured, and none is claimed — though the paraphrase is proven live to reach `caseload_review` and dispatch all five sub-queries.

## New defects, split out rather than folded in

- **Module 51** — the 60 s timeout kills whichever sub-query is served *last*. The `XAGG <kind>` log line proves the aggregate had already computed; only its paraphrase is discarded. Prerequisite for ever reconsidering the cap.
- **Module 52** — the wave's prescribed `grep -c "rate limit"` check returned **0** for both real provider failures, which say `RESOURCE_EXHAUSTED` / `UNAVAILABLE`.

Full write-up: `docs/gold-qa-wave2-results/MODULE50_RESULT.md`.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
