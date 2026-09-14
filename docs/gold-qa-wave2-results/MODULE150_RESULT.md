# Module 150 — Meta-Analysis's fan-out sends its last-served sub-queries to Groq, because the per-call timeout counts time spent queued on a serial model server

**Branch:** `fix/meta-analysis-fanout-deadline` · **Questions:** G6 (`orientation_note`, 8 sub-queries) and G1 (`caseload_review`, 5), the two plans that fell back in the 2026-09-14 32-question run.
**Merge base:** `origin/main` @ `01ddc18` (Modules 143 and 144 in).
**Backend:** own process on `:8150` from this worktree for the 32-question regression (`module150_live/backend_8150.log`); every timed G1/G6 arm ran in process through `module92_inprocess_run.run_one()` as Modules 110, 116 and 143 did, because the timeline needed per-LLM-call instrumentation the SSE stream does not carry.
**Model:** local Qwen3-14B behind the ngrok model server (`/health` 200 before every arm). The server was shared with two other tracks' live runs throughout; every table below records the per-call latency it saw at the time, so the numbers carry their own weather.

---

## 0. Summary

The brief's hypothesis — the 150 s Meta-Analysis deadline cancelling tasks — is **not** what fires. `asyncio.CancelledError` is a `BaseException`; `call_llm()`'s `except Exception` cannot catch it and cannot write *"Local LLM failed: . Falling back to groq"*. The empty message is `str(httpx.ReadTimeout)`, and the six fallbacks in today's log each landed **60–61 s after the call was sent** (G1's fifth call sent 15:51:04.7, fell over 15:52:05.6; G6's five sent 15:53:39–44, fell over 15:54:43–45). The 150 s deadline never fired — `sub-query timed out` appears nowhere in today's log nor in any of this module's 16 runs.

The mechanism is simpler and worse than a deadline: **the model server serves one request at a time** (measured: eight concurrent calls complete at +18, +26, +33, +39, +45, +51, +57, +63 s — a staircase, exactly as Module 50 saw), and `LOCAL_LLM_TIMEOUT` is an httpx read timeout that starts the moment each request is *sent*. An ungated eight-way fan-out therefore parks the last calls in the server's queue for their whole budget. They were not slow; they were served last. Module 110's cap raise from 5 to 8 made it worse only by lengthening the queue — G1 at N=5 sits at 52–53 s for its fifth call, 7 s from the same cliff.

**Fix:** a per-fan-out `asyncio.Semaphore` (`config.META_ANALYSIS_MAX_CONCURRENT_SUBQUERIES`, default **2**) in `_dispatch_one()`, acquired *inside* the existing `wait_for()` so the shared 150 s deadline is unchanged. Against a serial server client-side concurrency buys no throughput, so gating costs no wall time — it moves the queue from the server (where the wait counts against the per-call timeout) to the client (where it does not).

**Measured on G6, 3 runs each arm:** fallbacks **3/3 runs → 0/3**; fan-out wall **65–67 s → 65.2–65.4 s** (unchanged); slowest local call **53–57 s → 21 s**; whole-question wall **144–182 s → 131–136 s**; gold findings 7/7 in every run of both arms. Gate 1 and gate 3 were measured too and both lose (§2.2). The 32-question regression: **0 fallbacks** across the run (was 6), every route equal to the Module 116 baseline, CR3 through Meta-Analysis by name (§7).

**What this module cannot fix, stated plainly (§8):** G6 fully local costs ~131 s because it is 13 LLM calls in strict series on one server — router, classifier, eight paraphrases, synthesis, verifier, validation — at 10–15 s each. The gate removes the fallbacks and the wasted server work; it cannot make a serial server parallel. The 150 s *fan-out* deadline holds with 85 s of headroom; the whole-question time is a model-server capacity question, not a code one.

---

## 1. Root cause

### 1.1 What the log said, read against the code

`src/llm/client.py::call_llm()`:

```python
try:
    return await _call_local(...)
except Exception as e:
    ...
    logger.warning(f"Local LLM failed: {e}. Falling back to {provider}...")
```

Three facts decide it:

1. **A cancelled task cannot reach that line.** Since Python 3.8 `asyncio.CancelledError` derives from `BaseException`; `except Exception` does not catch it (this repo runs 3.13.1). If the 150 s deadline had cancelled a sub-query, `wait_for()` would have raised `TimeoutError` in `_dispatch_one()` and logged *"Meta-Analysis: sub-query timed out after 150s"* — a line absent from today's `evaluation/postfix-check/backend.log` and from every run here.
2. **`str(httpx.ReadTimeout())` is the empty string.** `_post_local()` opens `httpx.AsyncClient(timeout=config.LOCAL_LLM_TIMEOUT)`; `.env` sets that to 60. That is the only exception on the local path whose message is empty, and the instrumented runs (§1.2) confirm the type: every empty-message fallback is `httpx.ReadTimeout`.
3. **The timestamps fit 60 s, not 150 s.** Today's log, G6: plan matched 15:53:38.3; the eight XAGG aggregates computed 15:53:38.7–44.3, each firing its paraphrase call on completion; three came back local at +12, +17, +31 s; the five fallbacks at 15:54:43.5, 44.2, 45.1, 45.3, 45.5 — **60–61 s after each was sent**, 84 s before the deadline would have fired (15:56:08). G1: fifth aggregate computed 15:51:04.7, fell over 15:52:05.6 (60.9 s).

### 1.2 Per-sub-query timeline, instrumented (Phase 1.1)

`evaluation/module150_live_run.py` wraps `client._post_local` and `meta_analysis._dispatch_one` for one run (no source edits) and names every LLM call by its owning sub-query through a contextvar. G6, `origin/main` code (gate 0), three runs — `module150_live/module150_before_g6.json`. Offsets are from the question being asked; the fan-out began at 41.6 / 30.8 / 31.1 s (router + classifier calls precede it).

| Run | 8 paraphrase calls sent within | Local completions (s after fan-out start) | Fallbacks | Slowest local call | Fan-out wall | Whole question |
|---|---|---|---|---|---|---|
| 1 | 1.5–4.5 s of fan-out start | 11.9, 16.5, 20.7, 32.0, 39.3, 47.2, 57.2 | **1** — sent +3.6 s, `ReadTimeout` at +65.4 s | 53.0 s | 66.7 s | 181.6 s |
| 2 | 0.5–4.5 s | 12.4, 16.9, 24.8, 34.9, 44.2, 48.5, 59.8 | **1** — sent +4.4 s, `ReadTimeout` at +66.2 s | 56.9 s | 66.2 s | 169.2 s |
| 3 | 0.5–3.5 s | 6.7, 13.0, 23.6, 31.6, 37.9, 51.9 | **2** — one `ReadTimeout` at +64.7 s; one `RemoteProtocolError` ("Server disconnected without sending a response") at +3.4 s | 48.7 s | 64.7 s | 144.0 s |

Every run: a staircase of local completions ~6–12 s apart, and the call at the bottom of the stair is the one that dies, at 60 s + connect time after it was sent. `sub-query timed out`: 0 of 3. Salvage (Module 53): 0 of 3 — nothing was cancelled, so nothing needed salvaging. The Groq answer to the timed-out paraphrase came back in 1–3 s and the sub-query reported `OK`, which is why today's 32-run scored G6 1.00 with five fallbacks (§5).

Today's 32-run lost five of eight on G6 where these three runs lost one or two: the server was slower then (three completions in 31 s, then nothing for 30 s — another track's calls interleaved in its queue). The mechanism is the same; the count depends on the shared queue at the moment.

### 1.3 The server is serial (Phase 1.2)

`evaluation/module150_server_latency_probe.py` fires one paraphrase-shaped prompt (~180 tokens, `max_tokens=1000`) N times at once through the real `_post_local` — `module150_live/module150_server_latency_before.json`:

| N concurrent | Wall | Completion offsets (s) | Per-call durations |
|---|---|---|---|
| 1 | 9.6 s | 9.6 | 7.7 |
| 2 | 19.2 s | 13.2, 19.2 | 13.2, 18.8 |
| 4 | 43.7 s | 23.9, 31.5, 37.5, 43.7 | 23.9–42.9 |
| 8 | 62.7 s | 18.5, 26.0, 32.6, 38.9, 44.9, 50.7, 56.7, 62.7 | 18.2–**60.4** |

Wall time is linear in N; per-call time is linear in queue position; N=8's last call returned at 60.39 s, under the timeout by the connect phase only. **Concurrency on the client is free of benefit and full of cost** against this server: throughput is identical to serial dispatch, and every call after the first pays the others' service time out of its own timeout budget.

### 1.4 Why Module 110 made it worse, and why its arithmetic did not catch it

Module 110 sized the cap against the *shared deadline* (`8 × 12 s × 1.5 = 144 ≤ 150`), which is the right bound for the quantity it names. But the per-call timeout is a separate, tighter bound the same staircase also has to satisfy: the k-th of N simultaneous calls waits `(k-1) × slot` before its own, so the last call's duration is `N × slot`, and at slot ≈ 8–12 s the eighth call lands at 64–96 s — over a 60 s timeout on every run. At N=5, `5 × 10.5 = 52.5 s`: G1's measured 52.1–52.6 s (§4), 7 s of margin that today's load consumed. Module 110 measured nine post-change runs with zero fallbacks because the server was quieter that day (its slot was ~7 s); the failure was latent, not absent.

---

## 2. The change

### 2.1 What changed

**`src/config.py`** — new `META_ANALYSIS_MAX_CONCURRENT_SUBQUERIES: int` (env-overridable, default **2**, 0 = ungated), placed directly after `META_ANALYSIS_SUBQUERY_TIMEOUT` with the measurement above in its comment.

**`src/pipeline/harness/agents/meta_analysis.py`** — three functions, all in the dispatch/concurrency region (lines ~990–1070 and the `gather` call site ~1560); no plan table, pattern, sub-query constant or synthesis text was touched:

| Function | Change |
|---|---|
| `_fanout_gate()` | **New**, 6 lines. Returns `asyncio.Semaphore(limit)` or `None` when the limit is 0. Built per call so it belongs to the running loop and to one fan-out — two concurrent Meta-Analysis requests do not share it. |
| `_dispatch_one(sub_query, agent_input, on_event, gateway, gate=None)` | New keyword parameter. `Supervisor().handle(...)` is wrapped in `async with gate:` (or called directly when `gate is None`) inside a local `_gated_handle()` coroutine, and **that** is what `asyncio.wait_for(..., timeout=META_ANALYSIS_SUBQUERY_TIMEOUT)` now awaits. The `except asyncio.TimeoutError` / salvage path and the `except Exception` path are byte-identical. |
| `meta_analysis()` | The `asyncio.gather(...)` over `_dispatch_one(...)` now passes `gate=_fanout_gate()`; five lines of comment. Nothing else in the function moved. |

The gate is acquired **inside** `wait_for()` deliberately. The alternative — acquire first, then a per-sub-query `wait_for` — would let a gated fan-out run for up to `N × 150 s`, breaking the 150 s overall bound Module 110 sized the plan cap against. As shipped, `META_ANALYSIS_SUBQUERY_TIMEOUT` remains the one wall-clock deadline for the whole fan-out that Module 53 documents; a sub-query still queued at the gate when it fires is cancelled having done no work, exactly as an ungated one starved by the server would have been. `test_module150_deadline_is_still_one_wall_clock_over_the_whole_gated_fanout` pins this.

Sub-queries still dispatch in plan order; the semaphore's waiters are FIFO, so the plan's first findings are also the first served.

### 2.2 Why 2, measured — both candidates from the brief, and gate 1 / gate 3

**Candidate B, "a deadline that scales with fan-out": rejected on the evidence.** The deadline is not what fires (§1.1). Scaling it would change nothing observable, and raising the *per-call* timeout is an anti-goal (and would only push the cliff from the sixth call to the eighth).

**Candidate A, a concurrency limit: adopted.** Sized by two measurements — G6 fan-out wall (what the user waits) and slowest per-call latency (distance from the 60 s cliff) — `module150_live/module150_after_g6_gate{1,2,3}.json`:

| Gate | Runs | Fallbacks | Fan-out wall | Slowest local call | Whole question | Completion offsets, run 1 |
|---|---|---|---|---|---|---|
| 0 (main) | 3 | 1, 1, 2 | 64.7–66.7 s | 48.7–56.9 s | 144–182 s | 11.9, 16.5, 20.7, 32.0, 39.3, 47.2, 57.2, (65.4 ✗) |
| **1** | 2 | 0, 0 | **76.6–85.3 s** | 15.5–27.1 s | 147–181 s | 10.4, 23.4, 32.2, 38.4, 48.0, 59.8, 70.9, 76.6 |
| **2** | 3 | 0, 0, 0 | **65.2–65.4 s** | **21.2–21.5 s** | **131–136 s** | 10.7, 22.0, 29.3, 33.8, 41.8, 51.8, 61.2, 65.4 |
| **3** | 2 | 0, 0 | 72.7–77.9 s | **32.1–35.6 s** | 159–175 s | 14.2, 25.5, 32.8, 41.4, 49.3, 55.3, 73.7, 77.9 |

- **Gate 1 is slower**, by 11–20 s of fan-out: with nothing queued at the server, every sub-query pays its own ngrok round trip and its ~1 s deterministic-aggregate compute in series, and the server idles between calls. Its per-call latency is the lowest (a call waits for no one) but the user waits longer for the same answer.
- **Gate 2 is the ungated wall time with none of the exposure**: the second-in-line call waits at most one service time (measured 10–15 s; ~30 s under the heaviest shared load seen today) before its own, so its worst case is ~2 × slot — 21 s measured, ~45 s at 2× load — inside 60 s. And the staircase is byte-repeatable across the three runs (65.4, 65.4, 65.2 s), which the ungated arm never was.
- **Gate 3 raises exposure without buying time**: the third-in-line call waited 32–36 s here — at 2× load that is the cliff again — and the fan-out was *slower* than gate 2 on both runs (the other tracks' traffic landed differently, but nothing about three-in-flight can be faster than two against a serial server).

So 2 is not a taste judgement: it is the smallest gate that pipelines the network and compute gaps, and the largest whose worst case stays inside the per-call timeout with the headroom Module 110 used (`2 × 15 s × 1.5 = 45 ≤ 60`; `3 × 15 × 1.5 = 67.5 > 60`). `test_module150_gate_keeps_the_last_in_line_call_inside_the_per_call_timeout` encodes it.

### 2.3 What did not change

- `_MAX_PLAN_SUB_QUERIES` stays 8; every plan still dispatches its whole plan (`test_module50_each_question_dispatches_its_whole_plan` still passes). Module 110's coverage gain is intact — G6 carries 7/7 findings in every run here.
- `LOCAL_LLM_TIMEOUT` (60 s in `.env`) and `META_ANALYSIS_SUBQUERY_TIMEOUT` (150 s) are untouched.
- `src/llm/client.py` is untouched: what happens after a fallback, the Groq path, the empty-response retry, the AIR_GAP refusal — all as before.
- No plan definition, pattern, sub-query constant, synthesis prompt or salvage behaviour in `meta_analysis.py` changed. `muhafiz-m79`'s region of the file (the plan tables, lines ~600–905) has no overlap with this diff; the merge reconciles on `_fanout_gate` / `_dispatch_one` / `meta_analysis`'s `gather` only.
- `xagg.py` and `supervisor.py` (`muhafiz-m145`'s files) are untouched.

---

## 3. Unit tests

`tests/test_harness_agent_meta_analysis.py`, four new tests under the Module 150 banner. Run against `origin/main`'s `meta_analysis.py` (swapped in on disk, then restored) and against this branch:

| Test | On `main` | On this branch |
|---|---|---|
| `test_module150_fanout_never_has_more_than_the_configured_sub_queries_in_flight` — five sub-queries, gate 2: peak live `Supervisor.handle` must be 2, all five contribute, plan order kept | **FAIL** (peak 5) | PASS |
| `test_module150_deadline_is_still_one_wall_clock_over_the_whole_gated_fanout` — three sub-queries × 0.1 s through gate 1 under a 0.15 s deadline: two time out, elapsed < 2 × deadline, no slot left held | **FAIL** (peak 3; nothing was gated) | PASS |
| `test_module150_gate_of_zero_restores_the_ungated_fanout` — the bisecting escape hatch: gate 0 ⇒ peak = N | PASS (trivially: `main` is always ungated) | PASS |
| `test_module150_gate_keeps_the_last_in_line_call_inside_the_per_call_timeout` — the §2.2 arithmetic, both ends, plus `8 × 12 s ≤ 150 s` for the gated staircase against the shared deadline | PASS (config-only) | PASS |

The two that pass on `main` are stated as such: one is an invariant of the old behaviour, the other pins a constant this module added. The two that fail on `main` are the regression tests.

Full files, this branch: `tests/test_harness_agent_meta_analysis.py`, `tests/test_config.py`, `tests/test_harness_cutover.py`, `tests/test_harness_supervisor.py` — **297 passed**. (`test_module53_subquery_timeout_leaves_headroom_over_the_measured_staircase` and Module 110's cap/deadline pins pass unchanged — the deadline arithmetic they encode is exactly what §2.1 preserved.)

---

## 4. Live verification — G1 and G6, 3× each, before / after

In process, `evaluation/module150_live_run.py`, local Qwen3-14B, shared server. "Before" is this branch with `META_ANALYSIS_MAX_CONCURRENT_SUBQUERIES=0` for G1 and `origin/main` code for G6 (the G6 before-arm ran before the change was written). Model per sub-query: **every sub-query in every run below was `OK` with `tools_used=['XAGG']`**; "model" is which model answered its paraphrase call.

### 4.1 G6 — `orientation_note`, 8 sub-queries

| Arm | Run | Fallbacks | Model per sub-query | Fan-out wall | Per-call min / mean / max | Whole question | Findings |
|---|---|---|---|---|---|---|---|
| before | 1 | **1** | 7 local, 1 Groq (`relationship`) | 66.7 s | 11.0 / 26.3 / 53.0 s | 181.6 s | 7/7 |
| before | 2 | **1** | 7 local, 1 Groq (`arrest_rate`) | 66.2 s | 10.0 / 27.6 / 56.9 s | 169.2 s | 7/7 |
| before | 3 | **2** | 6 local, 2 Groq (`arrest_rate`, `district` — the latter a server disconnect at 2 s) | 64.7 s | 6.6 / 21.2 / 48.7 s | 144.0 s | 7/7 |
| after | 1 | **0** | 8 local | 65.4 s | 10.0 / 14.6 / 21.2 s | 136.1 s | 7/7 |
| after | 2 | **0** | 8 local | 65.4 s | 10.2 / 14.7 / 21.5 s | 131.1 s | 7/7 |
| after | 3 | **0** | 8 local | 65.2 s | 10.0 / 14.6 / 21.3 s | 130.7 s | 7/7 |

Fan-out inside the 150 s deadline by 85 s in every after-run. Whole-question wall fell 13–50 s — not because the fan-out got faster (it did not) but because the ungated arm's timed-out requests kept running on the server after the client abandoned them, slowing the synthesis / verifier / validation calls that followed (before-arm post-fan-out phase: 48–73 s; after: 41 s).

### 4.2 G1 — `caseload_review`, 5 sub-queries

| Arm | Run | Fallbacks | Model per sub-query | Fan-out wall | Per-call min / mean / max | Whole question | Findings |
|---|---|---|---|---|---|---|---|
| before (gate 0) | 1 | 0 | 5 local | 54.3 s | 8.2 / 20.6 / **52.6** s | 124.2 s | 4/4 |
| before (gate 0) | 2 | 0 | 5 local | 54.1 s | 8.5 / 21.6 / **52.1** s | 119.9 s | 4/4 |
| before (gate 0) | 3 | 0 | 5 local | 54.3 s | 8.8 / 21.2 / **52.3** s | 119.8 s | 4/4 |
| after (gate 2) | 1 | 0 | 5 local | 59.0 s | 9.9 / 16.5 / 25.8 s | 127.8 s | 4/4 |
| after (gate 2) | 2 | 0 | 5 local | 58.6 s | 9.8 / 16.6 / 25.8 s | 123.6 s | 4/4 |
| after (gate 2) | 3 | 0 | 5 local | 58.4 s | 9.2 / 16.4 / 25.7 s | 122.4 s | 4/4 |

Honest reading: G1 did **not** fall back ungated in this window — its fifth call ran 52.1–52.6 s every time, **7.4 s from the 60 s cliff**, which is the margin today's main run lost (60.9 s). The gate costs G1 ~4 s of fan-out (a 5-way plan has less to pipeline) and halves its worst-case call to 25.8 s. That is the trade: a few seconds on the quiet days for not losing a sub-query on the loud ones.

### 4.3 Live backend, `:8150` — the 32-question run

See §7. `grep -c "Falling back to groq" module150_live/backend_8150.log` = **0** (today's `evaluation/postfix-check/backend.log` on `main`: 6).

---

## 5. Gold comparison — G1 and G6 before / after

`evaluation/module150_findings_check.py` — substance markers per gold finding (the number, or the Urdu / Roman-Urdu / English term), applied to every run rather than a chosen one.

**G6, 7 gold findings** (districts; case-mix shift; accused profile 24–49 / stranger; arrests 11 of 73; 32 of 33 still in court; 2026 reports 1,401 min vs 15 min; 30 of 32 weapons unlicensed): **7/7 in all 6 runs**, both arms. After-arm answers are byte-identical across the three runs (1,644 chars): all-local at temperature 0 is deterministic. Before-arm answers differ run to run (1,946 / 1,709 / 1,900 chars) because one or two sub-answers came from a different model — the same findings, different prose.

**G1, 4 gold findings** (uniform age 24–49; stranger skew; 13 forensic / 7 heirs; time-of-day flat): **4/4 in all 6 runs**, both arms.

So the fallbacks were **not** costing gold coverage — Groq paraphrases the same computed aggregate correctly, and the deterministic verifier checks the numbers either way. What they cost: 13–50 s of wall time per G6 (§4.1), non-determinism in the served text, and — the one that matters for the client install — under `AIR_GAP_MODE` every one of those fallbacks becomes a hard `RuntimeError` in `call_llm()`, the sub-query fails with "encountered an error", and **its finding is lost**. Today's G6 would have carried 3 of 7 air-gapped. The gate is what makes the air-gapped answer equal to the connected one.

---

## 6. Non-gold paraphrase

Two questions of my own, in process, gate 2 unless stated — `module150_live/module150_para_before.json`, `module150_para_after.json`.

**P1 — a rewording that reaches `orientation_note` through a different pattern** (`newly posted`, not `orientation note` / `tawaqqo`): *"A newly posted SHO joins the station tomorrow. What should she expect from the caseload she is inheriting, and what should she keep an eye on first?"* Route XNETWORK → Meta-Analysis, 8 sub-queries, all XAGG.

| Arm | Fallbacks | Model per sub-query | Fan-out wall | Per-call min / mean / max | Whole question | Completion offsets |
|---|---|---|---|---|---|---|
| gate 0 | **2** (`arrest_rate`, `case_mix` — both `ReadTimeout` at +62/+61 s after send) | 6 local, 2 Groq | 67.3 s | 14.0 / 32.2 / **60.6** s | 178.2 s | 28.6, 33.2, 37.4, 45.2, 54.6, 64.7, (66.3 ✗), (67.3 ✗) |
| gate 2, run 1 | **0** | 8 local | 81.3 s | 11.2 / 18.0 / 29.8 s | 163.3 s | 18.9, 30.6, 37.8, 48.8, 55.0, 66.0, 77.1, 81.3 |
| gate 2, run 2 | **0** | 8 local | 78.6 s | 9.7 / 17.0 / 34.2 s | 148.6 s | 21.9, 34.6, 43.4, 47.6, 60.1, 66.1, 73.6, 78.6 |

Same mechanism, same cure, on wording the plan had never seen. Note the ungated arm's one surviving slow call at **60.58 s** — inside the timeout by the connect phase, the same margin as the N=8 probe in §1.3. The server was slower during these three runs than during §4's (per-call mean 17–18 s against 14.6 s — another track's traffic), which is why the gated fan-out here is 79–81 s rather than 65 s; the second-in-line call peaked at 34 s, still 26 s inside the timeout. **This ungated P1 run is also what caused KB9's fallback in §7** — it was running while the regression's last question was in flight; see there.

**P2 — a free compound question with no plan match:** *"Across all cases, compare 2024 with 2026 on three things: how often an arrest is recorded, whether recovered weapons are licensed, and how long after the incident the report is filed."* The router sent it to **XGRAPH**, not Meta-Analysis — answered in 7.5 s with no fan-out, so it exercises nothing this module changed. Reported rather than replaced: it is a reminder that the router, not the plan table, decides whether a compound question fans out at all (Module 158's territory, filed by another track).

---

## 7. Regression — all 32 gold questions

`evaluation/gold32_run.py` against this branch's backend on `:8150` (own process from this worktree, `.env` copied from the main checkout with the absolute `CHROMA_PERSIST_DIR`), platform-admin, All Cases, one pass — `module150_live/module150_gold32_outputs.json`, `backend_8150.log`, and the comparison in `module150_regression_compare.txt` (`evaluation/module150_regression_compare.py`). Compared against the Module 116 route baseline and against the `main` run this module was filed from (`evaluation/postfix-check/`, 2026-09-14 15:44–16:16).

**Routes: 32 of 32 equal to the baseline.** XAGG × 21, XNETWORK × 3, RAG × 8, with `sub_agent` Large-Scale Aggregate / Meta-Analysis / Semantic Search respectively. **CR3 by name:** `route=XNETWORK`, `sub_agent=Meta-Analysis`, `subquery_routes=['XAGG','XAGG','XAGG']` — the `record_consistency` plan's three sub-queries, all local, 100.7 s (main: 108.0 s), answer byte-identical to main's (637 chars, same text). Total wall 33.6 min (main 31.8 min; the difference is KB3/KB9 below).

**Groq fallbacks: 3 in the log, against 6 on `main`** — and the honest breakdown matters more than the count:

| Question | main (15:44 run) | this branch | What happened |
|---|---|---|---|
| G1 | 1 | **0** | Queue-position timeout on the fifth call — gone. |
| G6 | 5 | **2** | **Not queue position.** At 21:39:48 one paraphrase call returned after 40 s with **empty content** (the 14B's thinking trace consumed its whole 3,000-token budget — PR #89's case); `_call_local()` retried locally at 4,000 tokens. That retry is ~53 s of generation *on its own* at this server's rate, and it was second-in-line behind the other in-flight call — `ReadTimeout` at 21:41:29, 60.9 s after send. The abandoned request kept generating on the serial server, so `relationship`, sent 21:40:38.9 and queued behind it, also timed out at 21:41:39.7. Both Groq answers verified; G6 served 8/8 sub-answers, 7/7 gold findings, 224.7 s (main: 175.4 s — this run paid the 40 s empty call, the 60 s dead retry and a 37 s `crosscheck` call behind the dead retry's tail). |
| KB9 | 0 | 1 | **Caused by this module's own measurement.** §6's ungated P1 run started 22:00:23; its 8-way fan-out was saturating the server when KB9's `expand_query` call went out at 22:01:25 and timed out at 22:02:26. Not attributable to the branch — but a clean demonstration that every non-Meta-Analysis call site is exposed to the same queue mechanism (§8, 170). |

So the branch's own count is **2, both from one empty-content retry cascade** in G6 — a stochastic single-call failure the gate does not address (it cannot: a call whose *own* service time nears 60 s dies at any gate), filed as §8 168 with the numbers. Across this module's **four** gated G6 runs (three in process, one live) that is one incident; across the four ungated G6 runs on record today (three in process, one on `main`) queue-position timeouts fired in **all four**, 1–5 calls each.

**Answers.** G1 and G6 carry 4/4 and 7/7 gold findings on both arms (`module150_findings_check.py` over `outputs.json` for both runs). The twenty-one XAGG questions are byte-identical to main's on 18 of 21 (D1, A1, A7, CP6, CR2, CR4, CR6, CR7, CR8, CS4, CP1, M1, M2, M4, M5, M7, G3, G5 — same length, same text); S2, S3 and G2 differ in paraphrase wording over the same computed aggregate. The eight RAG questions differ as RAG does run to run (Module 143 measured KB3 abstaining 4 runs in 5 and KB6 at 247–628 s): KB3 abstained on both arms (main at 339 s with "no sufficiently relevant documents", this branch at the cutover's 360 s stop); **KB9 abstained on both** (main "could not be verified as grounded" at 94.6 s; this branch at the 360 s stop — the run my P1 fan-out contaminated); KB5 *answered* here (1,603 chars) where main abstained (82 chars). None of the eight touches Meta-Analysis or the gate.

**Judge scores.** `gold32_score.py` with the default Gemini judge was run on this run's outputs and stopped after four questions at ~9 min per question on the free tier (a full pass would have taken over four hours): D1 1.0/1.0, S2 1.0/1.0, S3 1.0/1.0, A1 0.9/None (FactualCorrectness / AnswerRelevancy) — `module150_live/module150_gold32_results.json`, all equal to main's (`evaluation/postfix-check/results.json`, mean FactualCorrectness 0.834; G1, G6, CR3 all 1.00). The three Meta-Analysis questions are the ones this module can affect; their gold findings are checked deterministically above, and CR3's answer is byte-identical to main's.

---

## 8. New defects, filed not fixed

Numbers 168–172. Highest in use before this module: **167** (`muhafiz-m145` filed 157–167; `muhafiz-m79` 157–159 — re-checked across every live worktree before assigning).

| # | Defect | Evidence | Where the fix lives |
|---|---|---|---|
| **168** | **The empty-content local retry cannot fit inside `LOCAL_LLM_TIMEOUT` when it is not alone, and its abandoned request keeps burning the serial server.** `client.py`'s comment on `_LOCAL_EMPTY_RETRY_CEILING` says 4,000 "stays low enough that the retry lands inside LOCAL_LLM_TIMEOUT" — false at this server's ~70 tok/s: a trace that eats 3,000 tokens takes ~40 s, and a 4,000-token retry is ~53 s alone, so second-in-line it dies at 60 s. Worse, httpx's timeout abandons the *client* side only; the server finishes generating the 4,000 tokens anyway, so the next queued call dies too (G6 live, §7: two fallbacks from one empty response, 21:41:29 and 21:41:39). Retried alone (KB2, 21:48:34) the same retry finished in 15 s. | `backend_8150.log` 21:39:48–21:42:08; 0 of 6 in-process G6 runs, 1 of 1 live | `src/llm/client.py` — the retry needs either a budget derived from the timeout and generation rate, or the retry to go to the cloud fallback directly when a peer is in flight; and the server needs request cancellation on client disconnect. Outside this module's files. |
| **169** | **The fallback log line hides the exception type.** `Local LLM failed: {e}` prints `str(e)`, which is `""` for `httpx.ReadTimeout` — the empty message the tracker's row 150 read as a cancelled task, and the reason this module's filed diagnosis was wrong. Every other exception on that path (`RemoteProtocolError`, `ValueError`) prints a message; only the most common one prints nothing. | today's `postfix-check/backend.log` lines 131, 175–183; §1.1 | `src/llm/client.py` — `f"{type(e).__name__}: {e}"` on both the `call_llm()` and `stream_llm()` warnings. One line each. |
| **170** | **Every other concurrent local-LLM call site is still exposed to the queue mechanism.** The gate is per-Meta-Analysis-fan-out. Two chat requests at once, or a fan-out plus any other user's request, still share the serial server, and each call's httpx timeout still counts the time it spends queued behind the others. Demonstrated by this module by accident: an ungated 8-way fan-out from one process killed a plain RAG `expand_query` call in another (§7, KB9). The client install runs more than one officer. | §7 KB9; §1.3 probe | A process-wide `asyncio.Semaphore` in `client._post_local()` (one per process, sized like this module's gate), or a server that reports queue position so the timeout can start at service. `src/llm/client.py`. |
| **171** | **`LOCAL_LLM_TIMEOUT` defaults to 20 s in `config.py` while every measurement and `.env` use 60.** A deployment that omits that `.env` line gets a per-call timeout no gate can satisfy — a single 14B paraphrase measured 8–15 s alone and 21 s second-in-line here, so at 20 s roughly half of all calls would fall to the cloud (or fail closed under `AIR_GAP_MODE`). `test_module150_gate_keeps_the_last_in_line_call_inside_the_per_call_timeout` pins the deployed 60, not the code default, and says so. | `config.py:62` vs `.env`; §4 per-call columns | `src/config.py` — raise the default to the measured value, or derive the gate from the timeout. |
| **172** | **G6 fully local is ~131 s of thirteen strictly serial LLM calls, and two pairs of them answer the same question twice.** Router 14.5 s then cutover classifier 10.2 s before the fan-out (both "what kind of question is this"); verifier 10.7 s then validation 14.6 s after the synthesis (both "is this answer grounded"). That is ~50 s of the 131 s. With the fan-out itself at 65 s and un-reducible on a serial server, merging either pair is the only code-side path under ~110 s; the rest is server capacity (a second model replica, or a batching server) — which is the honest answer to the brief's closing question: on this hardware the fallbacks are fixed, the wall time is not a code defect. | §2 top-level call timings, `module150_after_g6_gate2.json` `llm_calls` with `sub_query="<top-level>"` | `src/pipeline/harness/cutover.py` / `supervisor.py` (classification) and `meta_analysis.py` (verify + validate) — not this module's region, not this module's scope. |

Not filed, noted: the one `RemoteProtocolError` ("Server disconnected without sending a response", G6 before-run 3, 2 s after send, under 8 simultaneous requests) did not recur in any of the 12 gated runs; if it does, it is the server's connection limit, not the client's.
