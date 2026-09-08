# How to Reproduce This Gold-32 / DeepEval Evaluation

**Purpose:** so you can run the exact same evaluation on your machine, verify
the numbers independently, and check the two specific findings that affect your
work. Nothing here is hidden or hand-tuned — every model, prompt, threshold and
command used is written out below.

**Why this document exists:** the evaluation reports several of your modules as
still failing or only partially fixed, which does not match what you saw when
verifying them individually. Rather than argue about the numbers, this lets you
reproduce them yourself and check whether the disagreement is in the fix, in the
test method, or in the environment.

**Reference run:** 2026-09-08, `main` @ `06de8ea`, DeepEval 4.2.0, Python 3.12.3.

---

## Part 1 — The environment (this part matters more than it sounds)

A first pass of this evaluation produced **completely invalid results** because
of one missed setup step. Please do not skip these.

### 1.1 Copy the SHARE files into place

```powershell
cd <repo root>
Copy-Item SHARE\.env .env
Copy-Item SHARE\frontend.env frontend\.env
Copy-Item -Recurse SHARE\certs certs
```

**This is the step that went wrong.** In the first pass, `SHARE/.env` was not
copied, so a stale local `.env` was used in which **3 of the 5 Groq API keys
differed** from the ones in your SHARE folder. Result: **1,410 rate-limit
errors** in the backend log, of the form:

```
Exception: Failed to call groq after 3 attempts due to rate limits.
```

Every KB question abstained after 224–600 seconds and the whole KB bucket
scored 0.000. That was an artefact of wrong credentials, **not** a measure of
your KB work. After copying the correct `.env` and re-running only the failed
questions, the KB bucket went to 0.350 with 3 of 8 passing, and KB1 correctly
cited "Sections 154 and 155 of the Code of Criminal Procedure".

**Sanity check before running anything:**

```bash
grep -ciE "rate limit|RESOURCE_EXHAUSTED|429|quota|UNAVAILABLE|503" <your backend log>   # should stay near 0 during a run
```

If that number climbs into the hundreds, stop — the run is not measuring the code.

**Why the alternation and not the bare `grep -c "rate limit"` this file used to
prescribe:** the providers do not agree on wording. Groq says `rate limit`,
Gemini says `RESOURCE_EXHAUSTED`, both can surface a bare `429`, and Module 50
also hit `503 UNAVAILABLE ... 'This model is currently experiencing high
demand.'` — a transient capacity failure, not a quota one, but with the same
consequence. The bare check returns **0** over every one of those, i.e. it
certifies a clean run on top of a failed call. The canonical pattern lives in
`evaluation/gold32_score.py`'s `LOG_GREP_PATTERN` (Modules 46 and 54).

Check the second provider-failure mode in the same breath — a failed agent
harness cutover classification, which silently hands the question to the legacy
orchestrator so a *different* sub-agent answers it:

```bash
grep -c "Cutover classification failed" <your backend log>   # also 0
```

Since Module 54 that is visible without the log too: the SSE stream carries a
`cutover_classification_failed` flag and `gold32_pipeline_outputs.json` records
it per row.

### 1.2 Restore the data

```powershell
# Postgres — use docker cp + psql -f, NOT a PowerShell pipe
docker exec muhafiz-postgres psql -U postgres -d postgres -c "DROP DATABASE IF EXISTS muhafiz;"
docker exec muhafiz-postgres psql -U postgres -d postgres -c "CREATE DATABASE muhafiz;"
docker cp SHARE\database\muhafiz_dump.sql muhafiz-postgres:/tmp/muhafiz_dump.sql
docker exec muhafiz-postgres psql -U postgres -d muhafiz -q -f /tmp/muhafiz_dump.sql

# role migrations (the dump excludes roles)
# 009_mcp_readonly_role.sql, 015_app_least_privilege_role.sql,
# 031_muhafiz_app_age_grants.sql, 032_muhafiz_app_missing_table_grants.sql
# then set muhafiz_app / muhafiz_mcp_readonly passwords to match .env

# Chroma
Remove-Item -Recurse -Force data\chroma_db
Expand-Archive -Path SHARE\database\chroma_db.zip -DestinationPath data -Force
```

> **Note on the dump:** use `docker cp` + `psql -f`, not
> `Get-Content dump | docker exec psql`. The PowerShell pipe re-encodes UTF-8
> as CP437 and silently corrupts every Urdu value in the database. This was a
> real failure in an earlier round — weapon licence status became mojibake and
> the weapon-compliance aggregate returned 0 instead of 30.

### 1.3 Verify the data before evaluating

```python
# Chroma collection counts — should be 7716 / 18 / 568
import chromadb
c = chromadb.PersistentClient(path="data/chroma_db")
for n in ["muhafiz_kb", "muhafiz_community_reports", "muhafiz_entity_descriptions"]:
    print(n, c.get_collection(n).count())
```

```python
# Graph integrity — Urdu intact, no duplicate edges
# Weapon license_status should show 30x 'بغیر لائسنس' (NOT mojibake '╪¿╪║...')
# Person nodes: 430 total, 0 with mojibake
# accused INVOLVED_IN edges: exactly 94 (if you see 189, edges are duplicated)
# Incidents with incident_datetime: 64
```

Case count should be **73**.

### 1.4 Start the backend

```bash
PYTHONPATH=. .venv/Scripts/python.exe -m uvicorn src.main:app --host 127.0.0.1 --port 8001
curl http://127.0.0.1:8001/health
# expect: vector_store_status ok, database_status ok, documents_in_store 7716
```

Set the eval admin account to `admin@example.com` / `MuhafizAdmin2026!` with
role `platform-admin`.

---

## Part 2 — Running the evaluation

### 2.1 Generate answers

```bash
EVAL_ADMIN_EMAIL=admin@example.com EVAL_ADMIN_PASSWORD=MuhafizAdmin2026! \
  PYTHONPATH=. .venv/Scripts/python.exe evaluation/gold32_run.py
```

- Sends all 32 gold questions to `POST /api/chat` on `127.0.0.1:8001`
- Payload: `{"session_id": "<uuid4>", "message": "<question>"}`, cookies
  `access_token` + `csrf_token`, 300 s timeout
- Parses the SSE stream, capturing the final `step: response` text plus the
  classified `route`
- Writes `evaluation/gold32_pipeline_outputs.json`
- **Resumes** from that file, so re-running only fills in what is missing

**Expect to re-run it several times.** The runner stalls roughly every 15–20
questions on a constrained machine; each invocation picks up where the last
stopped. This is normal and does not corrupt earlier results — each question is
written atomically when it completes.

### 2.2 Score the answers

```bash
PYTHONPATH=. .venv/Scripts/python.exe evaluation/gold32_score.py
```

Writes `evaluation/gold32_results.json`. Also resumes — it skips questions that
already have both metrics. **To force a re-score of specific questions, delete
those rows from the results file and re-run.**

---

## Part 3 — Exactly how scoring works (the part to scrutinise)

### 3.1 Judge model

```python
GeminiModel(model="gemini-flash-lite-latest", api_key=GEMINI_API_KEY)
# DEEPEVAL_PER_ATTEMPT_TIMEOUT_SECONDS_OVERRIDE = 180
```

An earlier Qwen-27B judge was replaced because it graded literally and unfairly
penalised correct answers phrased differently.

### 3.2 Metrics — two, both with threshold 0.6

| Metric | Type | Threshold |
|---|---|---|
| `FactualCorrectness` | GEval (custom, vs ground truth) | 0.6 |
| `AnswerRelevancy` | DeepEval built-in | 0.6 |

Faithfulness was deliberately dropped: it scored against retrieved context that
the harness does not fully expose, making it noisy.

**Pass in this report means FactualCorrectness ≥ 0.5**, which is *more lenient*
than the metric's own 0.6 threshold.

### 3.3 The full FactualCorrectness prompt

This is the complete `evaluation_steps` list given to the judge — nothing else
shapes the score:

1. Read the QUESTION, the EXPECTED OUTPUT (verified ground truth), and the ACTUAL OUTPUT.
2. List the key facts the EXPECTED OUTPUT asserts (counts, names, statuses, relationships, conclusions).
3. For each key fact, check whether the ACTUAL OUTPUT states an equivalent fact **in its own words**. Different phrasing, different order, or extra correct information is **NOT an error**.
4. For any number, compare **by size, not exact digit match**. Treat numbers as matching when within roughly 5–10%, or off by a couple of units on a small total. *(Worked example in the prompt: expected 67 males / 24 females / 94 total vs actual 65 / 24 / 92 → MATCHES, should score HIGH.)*
5. Only score low when the answer: (a) **contradicts** the expected output in kind not degree, (b) is **materially incomplete**, omitting a key fact asked for, or (c) **refuses or abstains** when real content was available.
6. Correctly stating that data does NOT contain something, where the gold agrees, is a **PASS**.
7. Score high whenever key facts are covered, even with close-but-not-identical numbers or extra detail.

This encodes the testing team's rule: *cover the main points in your own way,
with the right facts, and it passes; only opposite or incomplete fails.*

### 3.4 Known judge quirk

One question (CR8) returned **no score at all** (`null`) rather than a number —
a judge-side failure. Re-scoring that single question returned 1.0/1.0. **A null
score should be re-run, never read as a zero.**

---

## Part 4 — Verifying the two specific findings

### 4.1 Finding A — your modules that still score low

Several modules you consider fixed still score 0.0. To check this yourself,
compare **three layers** for any such question — this separates "the aggregate is
wrong" from "the question never reaches the aggregate":

**Layer 1 — does the aggregate itself return correct data?**

```python
import asyncio
from src.pipeline.xagg import <your_aggregate_function>
print(asyncio.run(<your_aggregate_function>()))
```

**Layer 2 — does `run_aggregate` dispatch the question to it?**

```python
from src.pipeline.xagg import run_aggregate
from src.data_gateway.selector import get_gateway
r = asyncio.run(run_aggregate(<gold question text>, None, await get_gateway(),
                              user_id="admin", user_role="platform-admin"))
print(r.get("kind"))   # should be your aggregate's kind
```

**Layer 3 — what does the live pipeline actually do?**

Send the question to `/api/chat` and read the SSE stream for:

```
supervisor:dispatch: Classified query as route='...' -> sub-agent='...'
```

If layers 1 and 2 are correct but layer 3 shows a different sub-agent, the fix
is fine and the **dispatch** is the problem. That is exactly the situation for
G2/G5 below.

**Current status of the low scorers, with their live route:**

| Q | FC | AR | Route | Symptom |
|---|---|---|---|---|
| M4 | 0.0 | 1.0 | **XGRAPH** | routes to XGRAPH, not its own aggregate |
| M7 | 0.0 | 1.0 | XAGG | answers, facts wrong |
| M2 | 0.0 | 1.0 | XAGG | answers, facts wrong |
| CS4 | 0.0 | 1.0 | XAGG | answers, facts wrong |
| KB2 | 0.0 | 1.0 | RAG | answers, facts wrong |
| KB9 | 0.0 | 1.0 | RAG | answers, facts wrong |
| KB6 | 0.0 | 0.0 | None | genuine error, did not recover |

**Worth noting:** all of these except KB6 now score **AnswerRelevancy 1.0** with
FactualCorrectness 0.0. They are producing on-topic, well-formed answers that
are factually wrong — not refusing. That is a different (and arguably better)
failure mode than the abstentions seen before your fixes, and it means routing
and retrieval are working while synthesis accuracy is not. Judging that
distinction is the point of running layers 1–3 above.

### 4.2 Finding B — two previously-working questions now abstain

**G2 and G5 regressed from 0.4 and 0.6 to 0.0.** They were re-run with correct
credentials and still fail, so this is not the environment issue from Part 1.

Reproduce it in three steps:

**Step 1 — the aggregates are correct:**

```python
from src.pipeline.xagg import _case_completeness_scan, _weapon_compliance_scan
# _case_completeness_scan -> 73 cases, 9 missing incident dates, 52 missing status
# _weapon_compliance_scan -> 32 weapons, 30 unlicensed
```

**Step 2 — dispatch is correct:**

```python
# run_aggregate(G2) -> kind == "case_completeness_scan"
# run_aggregate(G5) -> kind == "weapon_compliance_scan"
```

**Step 3 — but the live pipeline diverges:**

```
supervisor:dispatch: Classified query as route='XAGG' -> sub-agent='Meta-Analysis'
```

The question routes to XAGG correctly, then goes to **Meta-Analysis** instead of
the aggregate sub-agent. Meta-Analysis decomposes it, a sub-question errors, and
the synthesis is rejected:

> "The synthesized answer could not be verified as grounded in the sub-answers;
> Could not answer sub-question (encountered an error)."

**Why this happens.** `supervisor.py` already contains a guard for exactly this,
whose own comment reads:

> "Meta-Analysis decomposes a question XAGG already answers in one call into two
> independently-dispatched, undirected sub-queries ... each sub-query then DROPS
> the language that made this pattern list match in the first place, so its own
> re-classification is left to the flaky LLM router call one level down."

The guard is correct but fires **only** for time-comparison questions
(`_TIME_COMPARISON_XAGG_PATTERNS`). G2 and G5 meet every other condition — XAGG
route, working single-call aggregate — but do not match those patterns, so the
guard never fires.

Checked and ruled out: the deterministic `_DECOMPOSITION_PLANS`
(record-consistency, orientation-note, caseload-review) do **not** match G2 or
G5. The decomposition comes from the LLM decomposer fallback path.

```python
from src.pipeline.harness.agents.meta_analysis import _match_decomposition_plan
_match_decomposition_plan(<G2 text>)   # -> None
_match_decomposition_plan(<G5 text>)   # -> None
```

**Two possible fixes:**

1. **Narrow** — add a second pattern list to the guard covering the
   completeness-scan and weapon-compliance shapes, so G2/G5 skip decomposition
   the way M1 already does.
2. **Structural (suggested)** — instead of a parallel pattern list that will
   drift, have the guard ask `run_aggregate` whether it has a dispatch for the
   query; if it resolves to a real aggregate kind, skip decomposition. Self
   maintaining, and closes the whole class of regression.

---

## Part 5 — Reference results from the 2026-09-08 run

| Slice | FactualCorrectness | Pass | AnswerRelevancy |
|---|---|---|---|
| All 32 | 0.572 | 19/32 | 0.886 |
| Excluding KB (24) | 0.646 | 16/24 | — |
| KB bucket (8) | 0.350 | 3/8 | — |

**By type:** Fact Retrieval 0.95 (6/6) · Complex Reasoning 0.86 (7/8) ·
Knowledge Base 0.35 (3/8) · Contextual Summarization 0.30 (2/5) ·
Creative Generation 0.28 (1/5)

**Confirmed fixed this round:** M5, CR4, CR3, KB8, KB5, M1 (outright); KB1, KB3,
KB4, G1, G6 (improved).

If your reproduction differs materially from these numbers, the most likely
causes in order are: (1) `SHARE/.env` not copied, (2) stale dump or Chroma,
(3) duplicate graph edges from re-projecting over an existing graph, (4) judge
`null` scores read as zeros.

---

## Part 6 — Files

| File | Contents |
|---|---|
| `evaluation/gold32_run.py` | answer generator |
| `evaluation/gold32_score.py` | judge + metric definitions |
| `evaluation/Gold_QA_Dataset_Final32_With_Answers.json` | the 32 questions + ground truth |
| `evaluation/gold32_pipeline_outputs.json` | captured answers, routes, timings |
| `evaluation/gold32_results.json` | per-question scores **and the judge's stated reason for each** |
| `evaluation/EVALUATION_REPORT_POST_FIXES.md` | full write-up of this run |

The `reasons` field in `gold32_results.json` is worth reading directly — it is
the judge's own justification per question, and the fastest way to check whether
a given score is fair.
