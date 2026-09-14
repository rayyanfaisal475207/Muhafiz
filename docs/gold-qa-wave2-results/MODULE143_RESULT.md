# Module 143 — an unanswerable RAG question costs ~108 s to abstain, because the retry loop re-retrieves against a corpus that scored zero relevance on the first pass

**Branch:** `fix/fast-abstention-zero-signal` · **Question:** the live query the
module was filed from — *"How often is the officer who registers an FIR also the
officer who investigates the case?"* — plus the eight gold KB questions as the
near-miss control and a plain-path control set this module had to build.
**Merge base:** `origin/main` @ `c52bfd7` (rebased twice, from `4f09e38` and `26a9713`;
the bases differ only in tracker rows 143–151 and Module 144's `xagg.py` / `sql_extractor.py`, neither on this path).
**Backend:** own process on `:8143` from this worktree; `:8001` (main checkout)
left running and untouched. **Model:** local Qwen3-14B via the ngrok model server
for every LLM call in every run below — `generation` is recorded per row in every
evidence file and no row fell back to Groq/Gemini (`local_llm_fallbacks = 0`
throughout; `cloud_fallbacks = []` in the in-process files).

---

## 0. Summary

The defect reproduces and is worse than filed: **173.7 s in-process, 190 / 211 /
441 s live** (the third run hit the cutover's 360-second stop). Retries are
**not** dead weight — 64 committed rows and this module's own plain-path probe
show attempt 2 or 3 rescuing a first-pass rejection — so the fix must separate
*zero-signal* from *near-miss* at attempt 1. **Nothing retrieval-side does
that**: the filed query's top cross-encoder score (0.12) is *higher* than a
near-miss the loop rescues (0.06), and a Roman-Urdu case question the loop
answers on attempt 1 scores 0.0006. The evaluator's verdict is a bare bool. The
one place the distinction exists is the evaluator's free-text `reason` —
*"no statistical data … frequency of such overlap"* against *"none mention Iqbal
Town"* — so the fix is a second, small classification of that reason, consulted
once, after the first rejected pass, on the plain (non-legal-KB) path only, that
fails open. Validated offline on **every committed rescue (67/67 say retry, two
passes)** and ten hand-built hazards (10/10), then live: **M143 60–64 s
(from 190–441 s)**, the plain-path near-miss still rescued 2/2, the eight gold
KB questions byte-identical in behaviour because their path never calls the gate.

---

## 1. Root cause

### 1.1 Per-cycle timing (Phase 1.1)

In-process, real pipeline, instrumented at every boundary
(`scripts/module143_inprocess_probe.py`; `module143_inprocess_before.json`):

| attempt | retrieval | of which `expand_query` + `cross_script` | rerank | evaluate | rewrite | verdict |
|---|---|---|---|---|---|---|
| 1 | 23.0 s | ~16 s + ~9 s | 1.2 s | 9.4 s | 11.2 s | False |
| 2 | 34.9 s | (two more LLM calls on the rewritten query) | 1.3 s | 11.8 s | 10.0 s | False |
| 3 | 61.6 s | (two more) | 0.9 s | 8.5 s | — | False |
| **total** | **173.7 s** | | | | | abstain |

Generation and verification: **0 s — never reached.** The cost is three
retrieve→rerank→evaluate cycles at 35–75 s each. Each retrieval re-runs two
query-expansion LLM calls (`expand_query`, `generate_cross_script_variant`) on
whatever query it is given, so a retry pays ~25 s before a single embedding is
computed; that is a separate defect (§8, 153), not this module's.

Live, through `/api/chat` with the gate off (`module143_live.json`, arm
`before`): **190.3 s, 210.6 s, 441.4 s.** The third run's third attempt was still
running when the cutover stopped it at 360 s and served *"Document search was
still retrying after 360 seconds and was stopped"* — the loop has no budget of
its own.

### 1.2 What "zero-signal" looks like at the evaluator (Phase 1.2)

The evaluator returns `{"relevant": bool, "reason": str}` and nothing else — no
score, no category. Its three verdicts on the filed query, in-process, verbatim:

1. *"The retrieved documents discuss procedures for FIR registration,
   investigation oversight, and transfer of cases between officers, but none
   explicitly address whether the same officer typically registers and
   investigates FIRs, or the frequency of such overlap. The documents do not
   contain specific policy language, **statistical data**, or procedural rules
   governing the relationship between FIR registration and investigation roles."*
2. *"… contain specific FIR case details … but do not mention procedural
   guidelines, roles, or **frequency** of officers handling both …"*
3. *"… do not mention officer roles, assignments, or procedural information
   about whether the same officer handles both … entirely absent …"*

Every retrieval-side proxy the brief suggested was measured and none separates
this from a near-miss:

| probe | path | attempt-1 top `rerank_score` | outcome |
|---|---|---|---|
| **M143** (filed) | plain | **0.1245** | FFF, abstain |
| Z2 average days FIR→zimni | plain | 0.1246 | FFF, abstain |
| Z3 highest conviction rate | plain | 0.0062 | FFF, abstain |
| Z4 Roman-Urdu M143 | plain | 0.0016 | FFF, abstain |
| **N1** armed robbery, Iqbal Town (vague) | plain | **0.0603** | FF**T** — rescued on attempt 3 |
| N2 Roman-Urdu, what was seized from Kashif | plain | 0.0006 | T — answered on attempt 1 |
| P1 / P2 / P3 answerable case questions | plain | 0.99 / 0.99 / 0.998 | T |
| KB3 / KB5 / KB6 / KB8 / KB9 | legal-KB (`cross_rerank_multi`, best-of-phrasings) | 0.94 / 0.99 / 0.60 / 0.91 / 0.93 | FFFFFF / FFT / T / T / FT |

A threshold that stops M143 at 0.12 kills N1 at 0.06; any threshold at all
kills every Roman-Urdu question on the plain path, where the cross-encoder's
score against the raw question is noise (Module 30 measured the same
0.0007–0.0022 band). Semantic cosine is 0.87–0.90 for all of them (Module 92's
finding again) and RRF is rank-derived. The Jaccard overlap between attempt-1
and attempt-2 windows is 0.0 for M143 *and* 0.0 for the KB5 rescue — retries
"move the window" in both cases; whether the move helps depends on what was
missing, not on how far it moved.

The reason text is where the distinction lives. On the filed query it names a
**frequency / statistic** — a figure computed across cases, which no document
holds at any wording. On every rescued near-miss it names a **specific**: a
place (*Iqbal Town*), a provision (*whether the law requires the same officer*),
a case type (*domestic violence, not rape*). The rewriter can target the second
kind; nothing can target the first.

### 1.3 Retries rescue near-misses — the loop is not dead weight (Phase 1.3)

Scanning every JSON in `docs/gold-qa-wave2-results/` for rows whose attempt-1
verdict is `False` and a later verdict `True`:

- **64 rescued rows** (`scripts/module143_gate_offline.py::committed_sets`):
  KB3 ×9, KB5 ×13, KB6 ×15 (incl. KB6P/KB6Q/KB6-P1/KB6-P2), KB8 ×13, KB9 ×12,
  KB2 ×1, KB4P ×1 — Modules 37, 39, 52, 64, 65, 78.
- **0 of 62** with a window available were rescued on an *identical* window
  (Jaccard 0.09–0.88); every rescue came with a moved window.
- **All 64 are legal-KB-path questions.** The committed evidence has no
  plain-path retry at all — the 24 non-KB gold questions route to XAGG — so
  this module built its own plain-path control (§1.2's table): **N1** is a
  genuine plain-path rescue (in-process FFT; live before FFT×1 and FT×1; live
  after FFT×2).

This changes nothing about the fix's shape — retries must survive — but it
does mean the near-miss control is the KB set plus N1, and it decides the
gate's scope (§2).

---

## 2. The change

**One new decision, consulted once.** After the evaluator rejects the **first**
pass on the **plain** path, `src/pipeline/retry_gate.py::retry_could_help()`
is asked, given the question and the evaluator's own reason, whether a reworded
search of the same corpus could plausibly find what the reason says is missing.
`False` — only when the reason names a figure computed across cases (count,
rate, average, frequency, ranking, statistic) — ends the loop at once with the
same `status=EMPTY / evaluator_verdict="not_relevant"` retry exhaustion
produces, plus a new observability flag `zero_signal_abstention=True`. `True`,
any exception, or unparseable output → the loop retries exactly as before.

**Why this signal and not a number** — §1.2. The threshold is the classifier's
decision, and it is not a guess: §3's offline set is every committed rescue
(67 rows including this module's probes, run twice, 0 false stops), every
committed never-rescued row (15, all retry — they are all near-miss-shaped KB
rejections), the four zero-signal probes (3 stop; the Roman-Urdu one says retry,
harmlessly, because the evaluator itself framed the gap as "procedural policy"),
and ten hand-written hazards where a count or frequency is *inside* one case or
*inside* a rule — *"how often must the case diary be submitted"*, *"within how
many hours must an arrested person be produced"* — all 10 retry.

**Scope, deliberately narrow:**
- `not statute_queries` — the legal-KB path (`cross_rerank_multi`, Module
  30/39/52 machinery, every gold KB question) never calls the gate. All 64
  committed rescues live there; its retries are byte-for-byte unchanged.
- `retry_count == 0` — first pass only, as filed. A later pass has already
  spent the money.
- `reranked` non-empty — an empty window keeps the old loop so Module 5's
  `global_corpus_appears_empty` (needs *every* attempt empty) is unaffected.
- `config.RETRY_GATE_ENABLED` (env, default on) — an off switch for bisecting.

**Cost:** one short local LLM call, 4–6 s (`module143_gate_offline.json`
`secs`), paid only on a rejected first pass on the plain path. A plain-path
near-miss pays it once (N1: 195 s after vs 192 s in-process before — inside
run-to-run noise); a zero-signal question saves two full cycles.

**Files touched:**
- `src/pipeline/retry_gate.py` — new; `retry_could_help()`.
- `prompts/retry_gate.txt` — new; the classification prompt. Its worked
  examples are shaped on the measured reasons of §1.2 (N1, KB3, KB5, Z2, Z3,
  M143); the held-out checks are the 67 committed reasons, the 15 never-rescued
  reasons and the 10 hazards, none of which appear in it.
- `src/pipeline/harness/tools/rag.py` — the gate in `_run_retrieval_loop()`
  after the not-relevant branch; `RagToolResult.zero_signal_abstention`; the
  `"Not relevant — retrying"` trace event now fires only when a retry follows
  (the `"Relevant"` event is unchanged).
- `src/config.py` — `RETRY_GATE_ENABLED`.
- `tests/test_harness_tool_rag.py` (+6), `tests/test_retry_gate.py` (new, 7).
- `scripts/module143_inprocess_probe.py`, `scripts/module143_gate_offline.py`,
  `scripts/module143_live_runs.py` — the three measurement tools.
- `GOLD_QA_REMAINING_FIXES_PLAN.md` row 143; this file and the evidence JSONs.

**What was NOT changed:** `config.MAX_RETRIES` (still 2 in `.env`, still
honoured on every path); `prompts/evaluator.txt` and `evaluate_relevance()`
(the gate reads the reason, it does not shape it); the abstention caveat text in
`semantic_search.py` and `_SAFE_RESPONSE` semantics; `rewrite_for_retry()`;
anything on the legal-KB path; `xagg.py` / `sql_extractor.py` (Module 144's);
the retry-exhaustion branch's no-web-fallback contract (`fallback_to_rag` is
still pinned `False`, and the gate's early return goes through the same
`RagToolResult` shape).

---

## 3. Unit tests — fail before, pass after

`tests/test_harness_tool_rag.py` (six new, `-k module143`) and
`tests/test_retry_gate.py` (seven new):

| test | before (`origin/main`, new tests copied in) | after |
|---|---|---|
| `zero_signal_first_pass_abstains_without_a_single_retry` | **FAIL** `assert 3 == 0` (three attempts ran) | pass |
| `near_miss_keeps_every_retry` | FAIL (no `zero_signal_abstention` field) | pass — 3 evals, 2 rewrites, gate asked once |
| `gate_is_consulted_after_the_first_pass_only` | FAIL `assert 0 == 1` | pass |
| `legal_kb_path_never_consults_the_gate` | FAIL | pass — gate call count 0 with statute queries present |
| `off_switch_restores_the_old_loop_exactly` | FAIL | pass |
| `empty_window_keeps_module5_empty_corpus_signal` | FAIL | pass — `global_corpus_appears_empty` still True |
| `test_retry_gate.py` ×7 (verdict pass-through, fail-open on no-JSON / exception / empty reason, validator rejects a quoted `"false"`, prompt loaded) | collection error (module absent) | 7 pass |

The fixtures `monkeypatch` the two names this module introduces with
`raising=False`, so the before-direction fails on the behavioural assertions,
not on the fixture. Full named-file run on the branch:
`tests/test_harness_tool_rag.py tests/test_retry_gate.py
tests/test_harness_agent_semantic_search.py` → **81 passed**.

**Offline validation of the classifier** (`scripts/module143_gate_offline.py`,
`module143_gate_offline.json`; local Qwen3-14B, temperature 0, two passes):

| set | rows | must say | pass 1 | pass 2 |
|---|---|---|---|---|
| MUST_RETRY — every committed rescue + N1, KB5, KB9 from this module | 67 | retry | 67/67 | 67/67 |
| NEVER — committed all-`False` rows (all KB near-miss shapes) | 15 | (retry, informational) | 15/15 | 15/15 |
| SHOULD_STOP — M143, Z2, Z3, Z4 | 4 | stop | 3/4 | 3/4 |
| hazards H1–H10 (`module143_gate_hazards.json`) — single-case counts, statutory frequencies, "which section" | 10 | as marked | 10/10 | — |

Note the committed reasons are the 80-character truncations `evaluator.py`
logs (§8, 155) — the gate saw *less* context on those 67 than it does live and
still never said stop. The one SHOULD_STOP miss (Z4) is the conservative
direction: the evaluator called the gap *"procedural policy, frequency, or
general practice … a formal standard or procedure"*, and the prompt's rule is
that a named procedure wins.

---

## 4. Live verification — the filed query

`/api/chat` on `:8143`, platform-admin, All Cases, `module143_live.json`.
Route `RAG` on every run (the filed routing miss, Module 145, still stands —
this module measures what happens after it).

| arm | run | attempts | gate | model | wall | served |
|---|---|---|---|---|---|---|
| before (gate off) | 1 | FFF | — | local | **190.3 s** | abstention caveat |
| before | 2 | FFF | — | local | **210.6 s** | abstention caveat |
| before | 3 | FF + stopped | — | local | **441.4 s** | cutover 360-s stop message |
| after | 1 | F | stop | local | **63.6 s** | abstention caveat |
| after | 2 | F | stop | local | **64.0 s** | abstention caveat |
| after | 3 | F | stop | local | **60.4 s** | abstention caveat |

Gate line on all three after-runs, verbatim from `backend.log`: *"Retry gate:
retry_could_help=False — The question asks for a frequency across cases (how
often), which is a statistical aggregate not present in any document"*. The
remaining ~60 s is attempt 1 itself — ~25 s of query expansion, embedding and
BM25, ~10 s evaluator, ~5 s gate, plus the router and cutover overhead — none of
which this module touches (§8, 153).

The three before-arm attempt-1 reasons were also fed to the gate offline
(80-char log truncations): all three → stop; N1's two → retry.

---

## 5. The near-miss control

Every rescue found in Phase 1.3 is a legal-KB-path question, and that path
does not call the gate — the control there is structural (unit test
`legal_kb_path_never_consults_the_gate`) and confirmed live: **no `Retry gate:`
or `zero-signal first pass` line appears in `backend.log` for any of the eight
gold KB questions in §7**, and their evaluator-round counts are the ordinary
retry pattern. Beyond that, the KB rescues are exercised at the reason level:
all 67 committed rescue reasons say retry (§3), twice.

The plain-path control that this module found itself:

| question | arm | attempts | outcome |
|---|---|---|---|
| N1 *armed robbery … Iqbal Town … pistol seized* | in-process before | FFT | answered, grounded (191.8 s) |
| N1 | live before, run 1 | FFF | abstained (233.5 s) |
| N1 | live before, run 2 | FT | rescued; verifier rejected |
| **N1** | **live after, run 1** | **FFT** | **rescued, gate said retry, answered 644 chars (195.0 s)** |
| **N1** | **live after, run 2** | **FFT** | **rescued, gate said retry; verifier rejected (187.0 s)** |
| N2 *Kashif ke qabze se kya baramad hua* (Roman-Urdu, score 0.0006) | live before / after | T / T | answered both arms; gate never consulted (relevant on attempt 1) |

Gate line for N1, verbatim: *"retry_could_help=True — A specific place (Iqbal
Town) is missing; a reworded search may find the case file that names it."*

### 5.1 KB3 control — both arms on the same backend

§7's single KB3 pass abstained, so KB3 was run again on `:8143`, gate on then
gate off (`RETRY_GATE_ENABLED=false`, backend restarted), two runs each
(`module143_kb3_control.json`):

| arm | run | verdicts | outcome | model | wall |
|---|---|---|---|---|---|
| after (gate on) | 1 | FFF, still retrying | **cutover 360-s stop** | cloud | 360.1 s |
| after (gate on) | 2 | T | answered, 1,521 chars | cloud | 224.6 s |
| before (gate off) | 1 | FFFF, still retrying | **cutover 360-s stop** | local | 360.2 s |
| before (gate off) | 2 | FFFF, still retrying | **cutover 360-s stop** | local | 360.1 s |

No `Retry gate:` line on any of the four (legal-KB path). KB3's abstentions
are KB3's own flakiness plus the cutover's 360-s ceiling — its six-attempt
loop now runs 60–90 s per attempt live and cannot finish inside it (§8, 154)
— and they happen **more** with the gate off than on in this sample. Not
this module's regression; not this module's to fix.
The rescue rate itself is noisy across arms (the evaluator is a 14B model at
temperature 0 on a moving window) — what the control shows is that the gate
never removed a retry from it.

---

## 6. Non-gold paraphrases — written before running

- **M143-P1 (English):** *"In what proportion of our cases does the same
  officer both register the FIR and carry out the investigation?"*
- **M143-P2 (Roman-Urdu):** *"Hamare cases mein kitni dafa aisa hota hai ke
  FIR likhne wala afsar hi tafteesh bhi karta hai?"*

**Live, both arms: neither reaches this module's code.** The router sends both
to **XAGG** (`module143_live.json`: P1 45.1 s / 23.8 s, P2 5.7 s / 5.5 s), as it
does Z3, Z4 and Z2 — only the filed wording with *"also"* falls through to
Semantic Search, exactly the one-word gap Module 145 describes. That is the
routing layer's finding, recorded here as evidence for 145, and it means the
paraphrases have to be run in-process to test *this* module's behaviour:

| paraphrase | run | attempt-1 top score | evaluator | gate | wall | before-equivalent |
|---|---|---|---|---|---|---|
| M143-P1 (English) | 1 | 0.034 | False — *"do not contain any data or statistics regarding the proportion of cases…"* | **stop** (11.6 s) | **76.2 s** | three attempts (M143 in-process: 173.7 s) |
| M143-P1 | 2 | 0.034 | False — *"no information about officer assignments … nor procedures governing whether the same officer…"* | **stop** (6.8 s) | **50.9 s** | |
| M143-P2 (Roman-Urdu) | 1 | 0.001 | False — *"…nor provide any statistical/qualitative data about the…"* | **stop** (12.3 s) | **54.2 s** | |
| M143-P2 | 2 | 0.001 | False (same reason) | **stop** (15.9 s) | **65.9 s** | |

All four runs local Qwen3-14B (`cloud_fallbacks = []`), all four
`status=abstained` with the unchanged caveat text. The Roman-Urdu paraphrase
stops here where Z4 (a different Roman-Urdu wording, §3) did not — the
difference is entirely in what the evaluator wrote, which is the point: the
gate reads the evaluator's diagnosis, not the question's surface form. Note
the second P1 reason names no statistic at all and the gate still stopped —
that one is a judgement call the classifier made on the question text
(*"in what proportion of our cases"*), and it is the right call here, but it is
also the direction in which this gate can err; the 67/67 and 10/10 sets in §3
are what bound that risk.

---

## 7. Regression — all 32 gold questions

Live, `/api/chat` on `:8143` with the gate on, one pass over all 32 gold
questions (`module143_gold32.json`). The run was interrupted by a session
loss after 14 rows and resumed on a fresh backend process from the same
branch for the remaining 18 — the runner is idempotent on `(id, run, arm)`, and
every row below is from one of those two processes, gate on in both. Baseline
columns are Module 37's committed six-run `module37_gold32.json` (routes and
statuses across its runs, `d` = done, `e` = error).

**Routes: 32/32 identical to baseline** (24 XAGG, 8 RAG). **Gate: never fired
on any gold question** — 0 `Retry gate:` lines in the 32 rows' log windows,
`zero_signal_abstention=False` on all 32, as the legal-KB path guarantees by
construction. Every RAG question ran its ordinary retry pattern.

| id | route | base route | status | base status ×6 | evaluator verdicts | model | wall s |
|---|---|---|---|---|---|---|---|
| D1 | XAGG | XAGG | done | dd | — | local | 4.5 |
| S2 | XAGG | XAGG | done | dd | — | local | 5.7 |
| S3 | XAGG | XAGG | done | dd | — | local | 13.7 |
| A1 | XAGG | XAGG | done | dd | — | local | 31.9 |
| A7 | XAGG | XAGG | done | dd | — | local | 6.0 |
| CP6 | XAGG | XAGG | done | dd | — | local | 12.2 |
| CR2 | XAGG | XAGG | done | dd | — | local | 10.1 |
| CR3 | XAGG | XAGG | done | dd | — | local | 100.8 |
| CR4 | XAGG | XAGG | done | dd | — | local | 11.1 |
| CR6 | XAGG | XAGG | done | dd | — | local | 5.7 |
| CR7 | XAGG | XAGG | done | dd | — | local | 7.4 |
| CR8 | XAGG | XAGG | done | dd | — | local | 6.6 |
| CS4 | XAGG | XAGG | done | dd | — | local | 7.1 |
| CP1 | XAGG | XAGG | done | dd | — | local | 5.9 |
| M1 | XAGG | XAGG | done | dd | — | local | 18.3 |
| M2 | XAGG | XAGG | done | dd | — | local | 11.3 |
| M4 | XAGG | XAGG | done | dd | — | local | 9.4 |
| M5 | XAGG | XAGG | done | dd | — | local | 11.6 |
| M7 | XAGG | XAGG | done | dd | — | local | 7.1 |
| G1 | XAGG | XAGG | done | dd | — | cloud | 113.8 |
| G2 | XAGG | XAGG | done | dd | — | local | 12.1 |
| G3 | XAGG | XAGG | done | dd | — | local | 11.8 |
| G5 | XAGG | XAGG | done | dd | — | local | 7.1 |
| G6 | XAGG | XAGG | done | dd | — | cloud | 221.5 |
| KB1 | RAG | RAG | done | dddddd | T | local | 169.0 |
| KB2 | RAG | RAG | done | ddeddd | FFT | local | 209.4 |
| KB3 | RAG | RAG | error | dddddd | FFFFFF | local | 337.6 |
| KB4 | RAG | RAG | done | dddddd | T | local | 118.1 |
| KB5 | RAG | RAG | done | eeeddd | FT | local | 200.3 |
| KB6 | RAG | RAG | done | dddddd | T | local | 123.0 |
| KB8 | RAG | RAG | done | dddddd | T | cloud | 241.9 |
| KB9 | RAG | RAG | done | ddeddd | FT | cloud | 324.3 |

Reading the eight RAG rows against the baseline:
- KB1, KB4, KB6, KB8: relevant on attempt 1 and answered, as in the baseline.
- KB2 (FFT), KB5 (FT), KB9 (FT): rescued by a retry — the near-miss behaviour
  this module had to preserve, preserved, with no gate call on the path.
- **KB3: abstained after 6 rejections (337.6 s)** where Module 37's six runs
  all answered. This is **not** the gate: no gate line, legal-KB path, and the
  in-process *before* arm (§1.2, gate code absent) produced the identical
  FFFFFF / 349.3 s on the same day. KB3 is the flakiest of the KB set
  (Modules 39/52 record FFFFFF runs beside rescued ones) and this pass caught
  a bad draw; the dedicated control in §5.1 runs it on both arms of the same
  backend: gate off, 2/2 hit the cutover stop; gate on, 1/2.
- G1, G6, KB8, KB9 were generated by the **cloud** fallback (local Qwen3-14B
  timed out on generation under the model server's load) — noted per Module
  101 §1.4; none of these is on the gate's path and the routes/verdicts are
  unaffected, but their answer text is not comparable to a local run's.

Answers: the 24 XAGG answers are the same aggregates as the baseline (same
routes, same figures — spot-checked D1 *73 cases*, S2 *Model Town, Lahore, 7 cases*); the KB
answers are the same shape (norm half + data half) as Modules 39/64/101 record.
This module changes no prompt and no retrieval on their path, so answer
equality there is structural, and the evaluator-verdict column is the
operative check.

---

## 8. New defects found — filed, not fixed

Highest number on `origin/main` at the time of writing is **151** (Modules
146–151 were filed by other tracks while this module ran), so these start at
**152**.

| # | defect | evidence |
|---|---|---|
| 152 | **The abstention caveat over-claims after a gate stop.** `semantic_search.py` serves *"No sufficiently relevant documents were found for this question after retrying with query refinements"* for every `EMPTY / not_relevant` result; after this module a zero-signal abstention has retried nothing. The message was out of scope by the brief's own anti-goal; `RagToolResult.zero_signal_abstention` is there so the caller can word it honestly. | `module143_live.json` after-arm rows: `retry_gate` says stop, `actual_answer` says "after retrying" |
| 153 | **Every retry re-runs two query-expansion LLM calls on an already-refined query.** `_retrieve_candidates()` calls `expand_query()` and `generate_cross_script_variant()` on whatever query it is given; on a retry that is the rewriter's output, so each cycle pays ~20–45 s of LLM time before embedding (measured: 16 + 9 s on attempt 1, 25 + 19 s on N1's attempt 2, 12 + 22 s on Z2's). This is the dominant cost of a near-miss rescue (N1: ~60 s of 192 s) and of the ~60 s a zero-signal question still costs after this module. Under model-server contention it is also what pushed M143 run 3 past the cutover's 360-s stop (441 s). Whether the paraphrases of a rewrite ever contribute a rescued chunk is unmeasured — that measurement is the prerequisite for removing them. | `module143_inprocess_before.json` / `module143_plain_before.json` `events` (`expand_query`, `cross_script` per attempt) |
| 154 | **The retry loop has no time budget of its own; the cutover's 360-s stop is the only ceiling.** M143 before-arm run 3: two attempts done at ~300 s, third still retrieving at 360 s, served the cutover's stop message at 441 s wall. A per-attempt or per-loop budget inside `_run_retrieval_loop()` would abstain cleanly instead of being killed mid-retrieval. | `module143_live.json` M143/before/3 |
| 155 | **`evaluate_relevance()` logs `reason[:80]`, so every committed evidence file carries 80-character reasons.** Module 37/52/64's runners capture `(.*)$` from the log line, but the line itself is truncated at the source — the 64 rescue reasons this module validated against are all cut mid-word. The reason is the loop's only diagnostic of *why* a pass failed and, after this module, the gate's input; the log should carry it whole (it is the retry rewriter's input already). | `docs/gold-qa-wave2-results/module52_runs.json` `evaluator_verdicts[*].reason` (all exactly 80 chars); `src/pipeline/evaluator.py` line `logger.info("Evaluator: relevant=%s — %s", …[:80])` |
| 156 | **XAGG serves a raw `(no matching cases found)` for the officer-role proportion paraphrase.** M143-P1 routes to XAGG (correctly — Module 145's aggregate exists), the paraphrase verifier rejects the summary, and the raw aggregate served is *"(no matching cases found)"* — a result with no figure and no explanation of which aggregate ran or why nothing matched. Both arms, 45.1 s / 23.8 s. Z4 and M143-P2 (Roman-Urdu) route to XAGG too but land on a *statute-count* aggregate and get an honest refusal. Which aggregate P1 dispatched, and whether "no matching cases" is a filter miss (Module 144's shape) or an empty projection, is unmeasured. | `module143_live.json` M143-P1 / M143-P2 / Z4 rows, both arms |

---

## 9. Artefacts

- `docs/gold-qa-wave2-results/module143_inprocess_before.json` — M143 + KB3/5/6/8/9, per-attempt events, scores, reasons.
- `docs/gold-qa-wave2-results/module143_plain_before.json` — P1–P3, N1–N2, Z2–Z4, same instrumentation.
- `docs/gold-qa-wave2-results/module143_gate_offline.json` — the 86-row two-pass classifier validation, model per call.
- `docs/gold-qa-wave2-results/module143_gate_hazards.json` — the ten hazard probes.
- `docs/gold-qa-wave2-results/module143_live.json` — live before/after rows for M143, N1, N2, Z2–Z4, P1, the two paraphrases; evaluator rounds, verdicts, gate lines, model, cutover fallbacks.
- `docs/gold-qa-wave2-results/module143_paraphrases_inprocess.json` — the §6 in-process runs.
- `docs/gold-qa-wave2-results/module143_gold32.json` — the §7 regression rows.
- `docs/gold-qa-wave2-results/module143_kb3_control.json` — §5.1's four KB3 runs, both arms.
- `scripts/module143_inprocess_probe.py`, `scripts/module143_gate_offline.py`, `scripts/module143_live_runs.py`.
