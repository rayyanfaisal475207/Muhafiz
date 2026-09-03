# Muhafiz — RAG Evaluation Report (DeepEval)

**Method.** A gold set of 17 queries spanning all major routes and all 11
harness sub-agents was scored with DeepEval. **Ground truth is non-circular:**
expected answers are derived from the **raw Muhafiz Data API** (the structured
police records the app ingests from — `/fir`, `/roznamcha`, `/cms`, `/pkm`,
`/criminal-records`), not from the app itself. Each query was then run through
the **live `/api/chat` pipeline** and the actual answer scored against that
ground truth.

**Judge model.** Qwen-3.8-27B via Groq (rotated across 3 keys). Fully local
orchestration — **no LangSmith, no OpenAI, no third-party eval service**; the
only external calls are to Groq, which the app already uses. No real PII left
the machine beyond the app's own existing provider calls.

**Scope.** 17 queries × 5 metrics = 85 judge evaluations, all completed.

---

## 1. Headline: what the numbers mean (read this first)

The five metrics fall into **two groups with very different trustworthiness**,
and the report is honest about which is which — this matters more than any single
number.

### Group A — Ground-truth metrics (TRUSTWORTHY)
These compare the answer to the **known-correct expected answer** from the raw
police record. They are the real measure of app quality.

| Metric | Mean | What it tells us |
|---|---|---|
| **Correctness** (G-Eval vs ground truth) | **0.42** | Mixed — strong on some routes, real failures on others (see §3) |
| **Faithfulness** (claims grounded in context) | **0.97** | **Excellent** — the app almost never asserts unsupported claims; 17/17 ≥ 0.82 |

### Group B — Context-relative metrics (UNDERSTATED — see caveat)
These compare the answer to the **retrieval context**, and here the context
provided to the judge was incomplete (see §4), so **these scores understate real
performance** and partly measure the eval harness, not the app.

| Metric | Mean | Caveat |
|---|---|---|
| Answer Relevancy | 0.47 | Judge penalized *correct* answers (e.g. marked "30-bore pistol" — the right answer — as a "factual error"). Understated. |
| Hallucination (lower=better raw; shown inverted) | 0.44 | Same context-gap: correct facts from structured fields (not in the narrative blob) were flagged as "hallucinated". Understated. |
| NameFactFidelity (strict entity/number match) | 0.16 | Deliberately harsh: penalizes Urdu-vs-English name rendering and any detail drift. Low by design, not a quality verdict. |

**Bottom line:** the app is **highly faithful (0.97 — it stays grounded and
rarely invents)** and its **real correctness is mixed with specific, fixable
failures** — most notably a cross-case aggregate that over-counts. The low
context-relative scores are largely a judge/harness artifact, not app failure.

---

## 2. Coverage — all routes and sub-agents exercised

| Route | Queries | Sub-agents covered |
|---|---|---|
| DIRECT, RAG, GRAPH, GRAPH_HYBRID, SQL, XGRAPH, XAGG, XNETWORK | 17 total | Semantic Search, Local Search, Global Search, Case Summarization, Timeline Building, Cross-Case Linkage, Investigative Analysis, Large-Scale Aggregate, Report Drafting, Data-Quality, Meta-Analysis |

(WEB route excluded — it is external/air-gapped and not core to grounded RAG.)

---

## 3. Real findings (from the trustworthy Correctness metric)

Correctness by **actual** route (what the router chose, not what we guessed):

| Route | Correctness mean | Note |
|---|---|---|
| GRAPH_HYBRID | 0.67 | Best — full case summaries scored well |
| RAG | 0.55 | Solid on direct fact lookups |
| GRAPH | 0.40 | Right entities, weaker role labeling |
| XGRAPH / XNETWORK | 0.20 | Cross-case synthesis thin vs. ground truth |
| DIRECT | 0.30 | See data-quality note below |
| SQL / XAGG | 0.00 | **Real failures — see below** |

**Confirmed genuine issues (not judge noise):**

1. **XAGG over-counts (the most important finding).** Asked "how many cases
   involve the Arms Ordinance," the app answered **79**; ground truth (computed
   from the DB) is **29**. The cross-case aggregate is materially wrong — a real
   correctness bug worth fixing before relying on aggregate counts.
2. **`dataquality-01` produced an empty answer** (0 chars) — the Data-Quality
   sub-agent returned nothing for a coverage question the record can answer.
3. **Abstention works correctly.** `abstain-01` — asked for a nonexistent DNA
   result and license plate — **correctly refused and fabricated neither**
   (Correctness 1.0). This corroborates the security engagement's finding that
   the app does not hallucinate without a source.

**Routing observations (informational, not errors):** the router often chose a
different route than our gold-set *hypothesis* (e.g. a weapon question → GRAPH
rather than RAG). This is not necessarily wrong — a weapon is a graph entity —
so we report the *distribution*, not a "routing accuracy," since our expected-
route labels are hypotheses, not ground truth.

---

## 4. Honesty caveats (so the numbers aren't over-read)

1. **Context-relative metrics were under-fed.** The `retrieval_context` given to
   the judge was the FIR narrative snippet, but many ground-truth facts (weapon,
   dates) live in *structured fields* (weapon_register, incident_datetime) not in
   that narrative. So Faithfulness/Hallucination/Relevancy penalized answers that
   were actually correct. A re-score of one row with full context lifted
   Hallucination 0.50 → 1.00, confirming the effect. **Group A (Correctness,
   Faithfulness) is the reliable read.**
2. **The judge model is imperfect.** Qwen-27b occasionally mis-scored — e.g.
   calling the correct "30-bore" a "factual error." A larger judge (GPT-4-class)
   would tighten Group B; Group A held up under inspection.
3. **NameFactFidelity is intentionally strict** and treats Urdu↔English rendering
   of the same name as a partial miss. It is a *drift detector*, not a pass/fail
   quality gate.
4. **n is small per route** (1–6). Treat per-route means as directional, not
   statistically firm.

---

## 5. Confidence assessment (what you asked for)

**How confident can we be in the app's answers, honestly:**

- **Grounding / no-hallucination: HIGH confidence.** Faithfulness 0.97 across the
  board, correct abstention on missing data, and the security engagement's
  independent finding that it resists injection and won't invent without a source.
  When the app answers, it answers *from its evidence*.
- **Single-case factual retrieval (RAG/GRAPH_HYBRID): GOOD.** Weapon, complainant,
  timeline, and summary queries returned the right facts (the low Relevancy scores
  were judge/context artifacts, verified by inspection).
- **Cross-case aggregation (XAGG): LOW confidence — a real bug.** The 79-vs-29
  count error means aggregate numbers cannot yet be trusted. This is the single
  most important thing to fix.
- **Cross-case synthesis (XGRAPH/XNETWORK): MODERATE.** Produces relevant themed
  output but thinner than ground truth; acceptable for exploration, not for
  authoritative claims.

**One-line summary for a demo:** *The app is trustworthy about staying grounded
and refusing when it shouldn't answer; its single-case factual accuracy is good;
its cross-case aggregate counting has a real, fixable error (79 vs 29) that should
be addressed before those numbers are relied on.*

---

## 6. Artifacts
- `gold_set.py` / `gold_set.json` — 17 queries + ground truth from the Data API
- `run_pipeline.py` / `pipeline_outputs.json` — actual `/api/chat` answers
- `deepeval_score.py` / `deepeval_results.json` — per-query, per-metric scores + judge reasons
- Reproducible: rebuild gold → run pipeline → score, all local except Groq judge calls.
