# Module 27 — Final Gold-32 rerun

**Run 2026-09-09 against `main` @ `6cf89fb`** (41 PRs merged this wave).
**Three full passes**, 32 questions each — 96 question-runs — so consistency is
measured rather than inferred.

**Why three passes.** Every previous report in this project is a single sample.
That cannot separate "fixed" from "lucky": on 2026-09-08 M4 produced **four
distinct outcomes in six runs**, CR3 failed three different ways in three runs,
and G1's clean run was 1 of 4. A mean over one pass hides exactly the property
the platform is being asked for.

---

## 1. Headline

| Metric | Module 18 | 2026-09-08 report | **This run (96 runs)** |
|---|---|---|---|
| FactualCorrectness | 0.428 | 0.572 | **0.666** |
| AnswerRelevancy | 0.687 | 0.886 | **0.931** |
| Pass rate (per pass) | 14/32 | 19/32 | **20 / 22 / 21** |

**Consistency — the number that matters:**

| | |
|---|---|
| Pass on **all three** runs | **19 of 32** |
| Pass on some runs | 4 |
| Never pass | 9 |
| **Routes identical across all 3 passes** | **32 of 32** |

## 2. Run integrity

Every failure mode this wave uncovered was checked, and none fired:

| Check | Result |
|---|---|
| Judge `null` scores | **0** — "no nulls, nothing excluded" |
| Answer cap | **none** (Module 46) |
| Transport failures / timeouts | **0 of 96** |
| Empty answers | **0 of 96** |
| Quota / provider lines | **0** |
| Cutover fallbacks | **0** |

The last two matter. A cutover fallback means *a different sub-agent answered
than the one being measured* (Module 54); it never happened. And the quota
pattern used here is Module 81's fixed one — the previous form matched a log
line's own milliseconds, so every earlier "clean run" claim was reading
timestamps.

## 3. Per question, three passes

| Q | Type | Route | FC ×3 | mean | **n-of-3 ≥0.5** | spread | AR |
|---|---|---|---|---|---|---|---|
| D1 | Fact Retrieval | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| S2 | Fact Retrieval | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 0.67 |
| A1 | Fact Retrieval | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| A7 | Fact Retrieval | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 0.89 |
| S3 | Fact Retrieval | XAGG | 0.9 / 1.0 / 0.9 | 0.93 | **3/3** | 0.1 | 1.00 |
| CP6 | Fact Retrieval | XAGG | 0.3 / 0.3 / 0.3 | 0.30 | 0/3 | 0.0 | 1.00 |
| CR3 | Complex Reasoning | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| CR4 | Complex Reasoning | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| CR6 | Complex Reasoning | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 0.76 |
| CR7 | Complex Reasoning | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| CR8 | Complex Reasoning | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| CS4 | Complex Reasoning | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| CP1 | Complex Reasoning | XAGG | 1.0 / 0.5 / 0.9 | 0.80 | **3/3** | 0.5 | 0.83 |
| CR2 | Complex Reasoning | XAGG | 0.0 / 0.0 / 0.0 | 0.00 | 0/3 | 0.0 | 1.00 |
| KB8 | Knowledge Base | RAG | 0.9 / 1.0 / 1.0 | 0.97 | **3/3** | 0.1 | 0.73 |
| KB5 | Knowledge Base | RAG | 0.9 / 0.8 / 0.9 | 0.87 | **3/3** | 0.1 | 1.00 |
| KB6 | Knowledge Base | RAG | 0.8 / 0.4 / 0.9 | 0.70 | 2/3 | 0.5 | 0.86 |
| KB1 | Knowledge Base | RAG | 0.3 / 0.3 / 0.3 | 0.30 | 0/3 | 0.0 | 1.00 |
| KB9 | Knowledge Base | XAGG | 0.2 / 0.4 / 0.2 | 0.27 | 0/3 | 0.2 | 0.83 |
| KB4 | Knowledge Base | RAG | 0.1 / 0.2 / 0.2 | 0.17 | 0/3 | 0.1 | 1.00 |
| KB2 | Knowledge Base | RAG | 0.0 / 0.0 / 0.0 | 0.00 | 0/3 | 0.0 | 0.86 |
| KB3 | Knowledge Base | XNETWORK | 0.0 / 0.0 / 0.0 | 0.00 | 0/3 | 0.0 | 0.07 |
| M4 | Contextual Summ. | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| M5 | Contextual Summ. | XAGG | 0.9 / 1.0 / 0.9 | 0.93 | **3/3** | 0.1 | 1.00 |
| M1 | Contextual Summ. | XAGG | 0.4 / 0.7 / 0.4 | 0.50 | 1/3 | 0.3 | 1.00 |
| M2 | Contextual Summ. | XAGG | 0.3 / 0.8 / 0.3 | 0.47 | 1/3 | 0.5 | 1.00 |
| M7 | Contextual Summ. | XAGG | 0.1 / 0.3 / 0.2 | 0.20 | 0/3 † | 0.2 | 1.00 |
| G3 | Creative Gen. | XAGG | 1.0 / 1.0 / 1.0 | 1.00 | **3/3** | 0.0 | 1.00 |
| G5 | Creative Gen. | XAGG | 0.5 / 0.6 / 0.6 | 0.57 | **3/3** | 0.1 | 1.00 |
| G1 | Creative Gen. | XAGG | 0.3 / 0.7 / 0.6 | 0.53 | 2/3 | 0.4 | 1.00 |
| G2 | Creative Gen. | XAGG | 0.5 / 0.5 / 0.5 | 0.50 | **3/3** | 0.0 | 1.00 |
| G6 | Creative Gen. | XAGG | 0.3 / 0.4 / 0.2 | 0.30 | 0/3 | 0.2 | 1.00 |

† **M7 is answered correctly and scored wrong — see §5.**

## 4. By question type

| Type | Module 18 | 09-08 | **Now** | Always-pass |
|---|---|---|---|---|
| Fact Retrieval | 0.98 | 0.95 | 0.872 | 5/6 |
| Complex Reasoning | 0.64 | 0.86 | **0.850** | **7/8** |
| Contextual Summarization | 0.02 | 0.30 | **0.620** | 2/5 |
| Creative Generation | 0.40 | 0.28 | **0.580** | 3/5 |
| Knowledge Base | 0.06 | 0.35 | **0.408** | 2/8 |

**Contextual Summarization 0.02 → 0.620 and Creative Generation 0.28 → 0.580**
are the largest moves. Those buckets hold M4, M1, M2, M7 and G1/G2/G3/G5/G6 —
the questions Modules 50, 53, 61 and 71 targeted.

**The KB bucket remains the laggard at 0.408**, and §6 explains exactly why.

## 5. Scores that are wrong, and in which direction

**M7 — the system is right, the judge is wrong.** Its answer carries gold's
figures *exactly*: mean 15.0 minutes across 13 FIRs in 2024, 1401.3 minutes
(~23.4 h) across 51 in 2026. Gold's Urdu answers an implied "is there a
difference?" with *"Haan, bohat zyada farq se"* (yes, a very big one); the
system answers the literal question asked — "are people reporting as quickly?"
— with "No". **Both say reporting became dramatically slower.** The judge read
the polarity flip as contradicting the conclusion and scored 0.1–0.3.

Under the stated standard — same facts in the answer's own words — M7 passes.
Module 43 independently measured it returning gold on **6 of 6** live runs.
**Counting M7, the honest pass figure is 20 of 32 always-passing.**

**Three gold answers were corrected during this wave**, each on independent
measurement, so their scores now reflect the data rather than the document:

| Q | Gold said | Data says | Derivations |
|---|---|---|---|
| G1 | "all accused are Pakistani nationals" | no nationality field exists anywhere | schema + full-text |
| G6 | arrest on ~1 in 9 FIRs | ~1 in 6 | 4 |
| KB9 | 8 FIRs cite PPC 302 | **10** | 4 |

**M2's gold is still contested and not corrected.** It asks which station group
is growing *faster* and answers with a static share (9 of 73 from 2 of 19 —
which is arithmetically right). On growth the data points the other way:
general-purpose **7 → 39** FIRs vs crime-type units **3 → 5**. M2's 1-of-3 is
partly this.

**Judge variance is real and measured**: 4 of 32 questions moved ≥0.3 across
identical answers — CP1 and KB6 and M2 by 0.5, G1 by 0.4. Module 45 previously
measured 0.3 on D1 across five draws. **Do not read a sub-0.3 single-question
movement as a code effect.**

## 6. Why the KB bucket is still 0.408

Not retrieval — that was this wave's main repair. The remaining causes are
specific and each has an open module:

- **KB3 (0.00, AR 0.07)** routes to **XNETWORK**, not RAG, so its data-half
  plan is never consulted. Module 77 demonstrated that an ordinary **English
  paraphrase of KB3 routes correctly and answers** *"in 68 out of 74 recorded
  assignment pairs (92%), the roles are not separated"* — gold's own figure.
  The defect is the router discriminating on KB3's specific gold wording
  (**Module 78**), not a capability gap.
- **KB9 (0.27)** routes to XAGG for the same reason; same paraphrase evidence.
- **KB2 (0.00)** retrieves the right provisions after Module 65 (Arts 38/39 and
  s.162, 3/3) but then **hedges against them**, where gold says the gap is by
  design (**Module 82**).
- **KB4 (0.17)** retrieves gold's rule 27.16 3/3 after Module 38, and the
  verifier rejects the answer built from it (**Modules 85/86**).
- **KB1 (0.30)** and **KB6 (0.70, 2/3)** are answer-quality, not retrieval.

So four of the eight KB questions **retrieve the right law and then lose it**
downstream. That is a different, later-stage problem than the one this wave
started with.

## 7. Two questions that regressed or stalled

- **CR2 (0.00, 3/3 consistent)** answers *"there is no indication that any
  person has appeared in an earlier case and then resurfaced"*, where gold
  names a specific identity with a prior conviction. Module 28 recorded CR2 as
  byte-identical and working. **This needs its own module** — it is the one
  clear regression in the set.
- **CP6 (0.30, 3/3)** reports 10 FIRs without an investigating officer against
  gold's 11, and omits gold's breakdown (8 Naamzad ASI, 3 Naamzad SI). The
  count is within tolerance; the missing breakdown is not.

## 8. What actually moved, separated

The 0.572 → 0.666 delta is **three different things**, and only the first means
the platform got better:

**Capability gained** — M4 (0.0 → 1.0, 3/3, including a court-stage half that
had never appeared in any recorded run) · CS4 (0.0 → 1.0) · CR3 (0.2 → 1.0) ·
CR4 · M5 · G2 and G5 restored from a regression this wave caused · KB8 (0.97,
with gold's 26 challans) · the KB data half from **0 of 48 runs → present on
KB4/KB5/KB6/KB8**.

**Measurement error removed** — the 300 s client ceiling that silently failed
5 of 8 KB questions · a judge `null` scored as zero (0.031 on CR8 alone) · the
900-char answer cap · the quota grep matching timestamps · a logger destroying
every Urdu record.

**Gold corrected** — G1, G6, KB9 (above).

**And consistency, which no earlier run reported at all:** 32 of 32 routes
stable, 19 questions passing on every run, 0 transport failures in 96 runs.

## 9. Reproducing this

```bash
docker compose up -d postgres
```

Then, with no other backend running:

```bash
PYTHONPATH=. .venv/Scripts/python.exe -m uvicorn src.main:app --host 127.0.0.1 --port 8001
```

Pre-flight (all verified before this run): 73 cases · **94** accused edges
(189 would mean a duplicated projection) · `incident_datetime` on 64 · 30
weapons unlicensed rendering as `بغیر لائسنس` · Chroma 7,716 / 18 / 568 ·
tunnel `/health` 200.

**Clear `evaluation/gold32_pipeline_outputs.json` before running.** Its resume
feature reported *"resuming — 32 done"* against the stale 2026-09-06 artefacts
and would have re-scored three-day-old answers without sending a question —
which is how the 2026-09-08 report came to cite figures it could not reproduce
(Module 47).

Artefacts for all three passes are in `evaluation/`.
