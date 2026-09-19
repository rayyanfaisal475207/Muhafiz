# QA Execution Report — Aggregate Query Pipeline

Blind execution of a 25-question aggregate suite against the current
aggregate intelligence pipeline. Measurement only: no code was modified, no
answer was computed from the database to grade against, and no prior QA
corpus, benchmark or failure analysis was consulted.

| | |
|---|---|
| Execution window | 2026-09-17 13:43:59Z — 14:31:03Z |
| Queries | 25 |
| Runs per query | 3 (75 executions) |
| Entry point | `src.pipeline.aggregate.orchestrator.answer_question()` |
| Registry snapshot | `2026-09-17T13:43:53Z` — 13 labels, 34 relationship triples |
| Scope | `kind=cross_case, user_role=supervisor` |
| Harness | `blind_qa_runner.py` (calls the production path; computes nothing itself) |

---

## 1. Pipeline under test — entry point verification

The brief required confirming *which* aggregate path was exercised. Verified
before execution:

**1. Callable entry point.** `src/pipeline/aggregate/orchestrator.py:197` —
`answer_question(snapshot, question, scope, schema_card=...)`. This is the
pipeline described in the brief. Every run in this report carries evidence
of all six stages: generated `AggregateSpec`, validation verdict,
verification decision, routes executed, reconciliation classification, and
final answer.

**2. Does chat/API reach this pipeline? — No.**
`POST /api/chat` (`src/main.py:396`) dispatches to `process_query` in
`src/pipeline/orchestrator.py`, a *different* module from
`src/pipeline/aggregate/orchestrator.py`. Aggregates on that path are
computed by the legacy `run_aggregate` (from `src/pipeline/xagg.py`) at
`src/pipeline/orchestrator.py:476` and `:2153`. A grep for the new engine
across `src/api/` and `src/pipeline/harness/` returns zero hits. The only
callers of `answer_question` are tests and QA runner scripts.

**3. Legacy bypass? — Yes, and it is the only live path.**
Live user traffic: chat → harness supervisor → `xagg_tool()` →
`xagg.run_aggregate()`. This matches the new orchestrator's own docstring,
which states `executor.run_aggregate()` is deliberately not called so
shadow comparisons stay valid.

**Consequence.** This report measures the new architecture, which is real
and working but **not user-reachable**. It is not a measurement of what a
user currently gets from chat. Chat integration is missing; no code was
modified to add it.

---

## 2. Execution summary

| Outcome | Queries |
|---|---|
| Answered consistently across 3 runs | 12 |
| Refused consistently across 3 runs | 8 |
| **Non-deterministic** (status or value varied) | **6** |
| Unhandled exception (at least one run) | 1 (QA-018) |

Independent QA classification (behaviour, not arithmetic):

| Class | Count | Queries |
|---|---|---|
| PASS | 9 | QA-001, QA-002, QA-003, QA-004, QA-005, QA-006, QA-010, QA-021, QA-023 |
| SUSPICIOUS | 3 | QA-011, QA-016, QA-017 |
| FAIL | 13 | QA-007, QA-008, QA-009, QA-012, QA-013, QA-014, QA-015, QA-018, QA-019, QA-020, QA-022, QA-024, QA-025 |

Non-deterministic: **QA-008, QA-015, QA-018, QA-019, QA-022, QA-024**.

### What works well

- **Simple and single-filter entity counts** are correct, stable and fast
  (QA-001, QA-002, QA-003, QA-005, QA-010, QA-023).
- **Cross-language literal resolution works.** QA-006 translated "female"
  to the Urdu literal `عورت`, fired `M3_value_literal`, ran all three
  routes and reached `AGREEMENT`. The schema card's value examples are
  doing their job.
- **Verification machinery is real.** Where `M3`/`M2` fired, the AGE route
  ran an independently-planned computation and reconciliation compared
  them. QA-004, QA-005, QA-006, QA-013, QA-023 reached `AGREEMENT`.
- **Adversarial control handled correctly.** QA-021 ("unicorn weapons")
  returned 0 with `INSUFFICIENT_DATA_COVERAGE` and *"Only 0 record(s)
  qualify — too few to report a meaningful figure."* It did not invent a
  category.
- **Refusals are safe, not crashes.** 8 queries refused deterministically
  with specific, actionable validator messages.
- **Audit trail is excellent.** Every answer carries the executed Cypher,
  gate profile, gate-by-gate evidence, exclusion counts and coverage. Every
  figure in this report was reproducible from the receipt alone.

### The dominant failure

**Silent question substitution** — the system drops a semantically load-
bearing part of the question, answers a different question, and reports
`coverage: COMPLETE` with no warning. Observed in five queries:
QA-008, QA-013, QA-015, QA-022, QA-024.

Two of these are marked as verified or fully checked while wrong:

- **QA-013** returned 206 (all persons involved in incidents) for a
  question about *accused* persons — and carries
  `independently_verified: True`. Both routes computed the same wrong
  question and agreed.
- **QA-022** returned **208** — every person in the system — for "how many
  persons have a criminal risk score above 80?", at verification level
  `FULL`, coverage `COMPLETE`, zero warnings. Two of three runs correctly
  refused this same question.

Structural observation: every safeguard in this architecture validates the
**spec**, never question→spec fidelity. Gates, coverage, verification
triggers and AGE cross-checking all operate downstream of interpretation.
When interpretation silently substitutes a question, all of them pass, and
FULL verification can confirm the wrong question's answer.

---

## 3. Result matrix

Values are the three runs. "Verify" is the verification level(s) reached.

| ID | Value (3 runs) | Verify | Mandatory triggers | Routes | Reconciliation | Stable |
|----|----------------|--------|--------------------|--------|----------------|--------|
| QA-001 | 73 | NONE | – | structured | SINGLE_ROUTE_VALID | YES |
| QA-002 | 32 | PARTIAL | – | semantic, structured | SINGLE_ROUTE_VALID | YES |
| QA-003 | 19 | NONE | – | structured | SINGLE_ROUTE_VALID | YES |
| QA-004 | 30 | FULL | M3_value_literal | age, semantic, structured | AGREEMENT | YES |
| QA-005 | 73 | FULL | M3_value_literal | age, semantic, structured | AGREEMENT | YES |
| QA-006 | 34 | FULL | M3_value_literal | age, semantic, structured | AGREEMENT | YES |
| QA-007 | REFUSED ×3 | – | – | – | – | YES |
| QA-008 | 208 / 194 / REFUSED | FULL, PARTIAL | – | VARIED | SINGLE_ROUTE_VALID | **NO** |
| QA-009 | REFUSED ×3 | – | – | – | – | YES |
| QA-010 | 32 | NONE | – | structured | SINGLE_ROUTE_VALID | YES |
| QA-011 | 76 | FULL, PARTIAL | M2_fanning_traversal | VARIED | SINGLE_ROUTE_VALID | NO¹ |
| QA-012 | REFUSED ×3 | – | – | – | – | YES |
| QA-013 | 206 | FULL | M2_fanning_traversal | age, semantic, structured | AGREEMENT | YES |
| QA-014 | REFUSED ×3 | – | – | – | – | YES |
| QA-015 | REFUSED / 55 / REFUSED | FULL | M2_fanning_traversal | VARIED | SINGLE_ROUTE_VALID | **NO** |
| QA-016 | REFUSED ×3 | PARTIAL | M1_multi_source | semantic, structured | REFUSED | YES |
| QA-017 | REFUSED ×3 | NONE, PARTIAL | M1_multi_source | VARIED | REFUSED | YES¹ |
| QA-018 | **EXCEPTION** / REFUSED / REFUSED | – | – | – | – | **NO** |
| QA-019 | 0 / REFUSED / REFUSED | FULL | M2_fanning_traversal | VARIED | SINGLE_ROUTE_VALID | **NO** |
| QA-020 | REFUSED ×3 | – | – | – | – | YES |
| QA-021 | 0 | FULL | M3_value_literal | age, semantic, structured | SINGLE_ROUTE_VALID | YES |
| QA-022 | REFUSED / **208** / REFUSED | FULL | – | VARIED | SINGLE_ROUTE_VALID | **NO** |
| QA-023 | 208 | FULL | M2_fanning_traversal | age, semantic, structured | AGREEMENT | YES |
| QA-024 | 0 / 32 / 32 | NONE, PARTIAL | – | VARIED | SINGLE_ROUTE_VALID | **NO** |
| QA-025 | REFUSED ×3 | – | – | – | – | YES |

¹ Value and status stable; verification level and route set varied between
runs. Recorded because the assurance story changes run to run even when the
number does not.

---

## 4. Verification policy behaviour

The policy behaved as designed wherever it was reached:

- `M3_value_literal` fired on every chosen-string-literal filter
  (QA-004, QA-005, QA-006, QA-021) and escalated to FULL.
- `M2_fanning_traversal` fired on fanning traversals
  (QA-011, QA-013, QA-015, QA-019, QA-023).
- `M1_multi_source` fired on the Postgres-authoritative time windows
  (QA-016, QA-017).
- The LLM recommendation was never observed lowering a level; the
  `level_max` escalation-only contract held.

The gap is not in the policy. It is that a spec which dropped the
question's substance presents as a *simple* spec, so no trigger fires on
the thing that actually went wrong (QA-008 run 1, QA-022 run 2,
QA-024 runs 2–3 all had `predicates: []`).

`independently_verified` is correctly conservative in mechanism — it
requires `AGREEMENT` plus a real second computation — but it is a claim
about two routes agreeing, not about either route answering the question
asked. QA-013 is the demonstration.

---

## 5. Latency

Dominated by NL→spec generation, not by the verification routes.

| Stage | Observed |
|---|---|
| Spec generation | 5.3 s – 46.3 s |
| Structured route | ~0.1 s (58 ms typical) |
| AGE route | ~11 s |
| Semantic route | 0.8 s – 4.6 s |

Generation variance is Groq rate-limit key rotation (`src/llm/client.py`
rotates across 4 keys with up to 3 retries).

---

## 6. Per-query detail

Full interpretation, validation, verification, route outputs and
reconciliation for every query — including the executed Cypher — are in
`qa_execution_results.json`. The narrative findings for each failing query
are in `QA_FAILURE_ANALYSIS.md`.

Selected evidence for the queries that passed:

**QA-005 — "How many documents are FIR records?" → 73**
Entity `Document`, predicate `doc_type = "fir_structured"`, coverage
`COMPLETE`. A genuine filtered count, not a collapse to `Case` (73 FIRs for
73 cases is one FIR per case). FULL verification, `AGREEMENT`.

**QA-006 — "How many female persons?" → 34**
Predicate `gender = "عورت"`. `M3_value_literal` → FULL → AGE planned its
own computation and returned 34 with population *"Person entities with
gender marked as female"*. `AGREEMENT`, `independently_verified: True`.

**QA-010 — "How many cases have a weapon linked to them?" → 32**
`MATCH (m0:Weapon)-[e0:BELONGS_TO_CASE]->(n:Case) RETURN count(DISTINCT
n.case_id)`. Correct entity and correct inbound direction. 32 equals the
total weapon count, but the computations differ — a near 1:1 weapon-to-case
mapping, not a substitution. Ran at verification `NONE`, so no independent
cross-check occurred.

**QA-021 — "How many unicorn weapons were seized?" → 0**
`canonical_name = "unicorn"`, deterministic across 3 runs, and the zero was
*not* presented as a finding: `INSUFFICIENT_DATA_COVERAGE` plus *"Only 0
record(s) qualify — too few to report a meaningful figure."*

---

## 7. Reproducing this run

```
PYTHONPATH=. .venv/Scripts/python.exe blind_qa_runner.py \
    --runs 3 --out qa_execution_results.json
```

Requires the live Postgres/AGE instance, the AGE evaluator database, the
remote embedding server (`MODEL_SERVER_BASE_URL`) and Groq credentials.
Because spec generation is an LLM call, re-running will not reproduce the
non-deterministic queries identically — that instability is itself one of
the findings.

---

## 8. Operational note

`.env` in this working tree contains live unredacted credentials — four
Groq keys, five Gemini keys, and Postgres passwords for the app, MCP
read-only and AGE evaluator roles. Rotation is worth considering
independently of this QA.
