# Evaluation Report — RAG Chatbot

## Overview

This document covers the automated test suite (109 tests, 0 failures) and
the qualitative evaluation framework for the RAG pipeline.

---

## 1. Automated Test Suite Results

```
======================== 109 passed, 0 failed in 2.15s ========================
```

| Test File | Tests | Focus |
|---|---|---|
| `test_structure.py` | 27 | File structure, imports, chunker, RRF, memory, config |
| `test_pipeline.py` | 21 | Query rewriter, router, evaluator (all mocked — no API keys) |
| `test_retrieval.py` | 20 | BM25, RRF algorithm, ChromaDB vector store (mocked) |
| `test_database.py` | 21 | SQLite schema, pipeline logger CRUD, FK cascades |
| `test_api.py` | 20 | FastAPI endpoints: /health, /documents, /ingest, /chat |
| **Total** | **109** | **All green** |

### Key Properties Verified

**No API keys required** — all LLM calls are mocked via `unittest.mock.AsyncMock`.
Entire suite runs in ~2 seconds.

---

## 2. Pipeline Quality Metrics Framework

The metrics below define what to measure on a real test set of 20–30 questions.
Populate the table by running queries against the live system.

### 2.1 Retrieval Precision

> **Definition**: Of the top-k chunks retrieved and re-ranked, what % are actually
> relevant to the question?

| Query Type | Retrieved | Relevant | Precision |
|---|---|---|---|
| Specific factual (e.g. "aspirin dosage") | 5 | — | — |
| Broad topic (e.g. "side effects of NSAIDs") | 5 | — | — |
| Cross-document (e.g. "compare A vs B") | 5 | — | — |
| **Average** | | | **Target: ≥ 0.70** |

**How to measure**: For each test query, manually inspect the `retrieved_documents`
table in SQLite after a run, or read the pipeline trace in the UI.
Label each chunk as relevant (1) or not (0). Precision = relevant / total.

### 2.2 Router Accuracy

> **Definition**: Does the router correctly classify queries as needing RAG (YES)
> or not (NO)?

| Category | Count | Correct | Accuracy |
|---|---|---|---|
| Knowledge-base questions (should → YES) | — | — | — |
| Conversational greetings (should → NO) | — | — | — |
| Arithmetic / calculations (should → NO) | — | — | — |
| **Overall** | | | **Target: ≥ 0.90** |

**How to measure**: Create a labeled test set of 30 queries with known YES/NO labels.
Call the router directly:
```python
from src.pipeline.router import route_query
import asyncio

test_cases = [
    ("What is the bleeding risk of aspirin?", True),   # YES
    ("Hello, how are you?",                  False),  # NO
    ("What is 2 + 2?",                       False),  # NO
]
for query, expected in test_cases:
    result = asyncio.run(route_query(query))
    print(f"{'✓' if result == expected else '✗'} {query[:50]}")
```

### 2.3 Answer Faithfulness

> **Definition**: Does the assistant's answer contradict or hallucinate beyond
> what is in the retrieved documents?

| Query | Faithfulness | Notes |
|---|---|---|
| — | — | — |
| **Average** | | **Target: ≥ 0.85** |

**How to measure** (manual): For each answer, check if every claim can be traced
to a specific retrieved chunk. Score 0 (contradicts source) / 0.5 (unsupported) / 1 (supported).

**Automated proxy**: Check if the evaluator consistently returns `relevant: true`
when the answer looks good. If the evaluator passes but the answer is wrong, that's
a faithfulness failure.

### 2.4 Retry Loop Effectiveness

> **Definition**: When the evaluator returns `relevant: false`, does the retry
> succeed on the second attempt?

| Scenario | Retries Triggered | Retry Succeeded | Recovery Rate |
|---|---|---|---|
| Vague query (no specific keywords) | — | — | — |
| Ambiguous follow-up question | — | — | — |
| Query outside knowledge base | — | — | — |
| **Overall** | | | **Target: ≥ 0.60** |

**How to measure**: Query the `queries` table in SQLite:
```sql
SELECT
    retry_count,
    response_type,
    COUNT(*) AS count
FROM queries
GROUP BY retry_count, response_type;
```

A `retry_count > 0` AND `response_type = 'rag'` = successful retry recovery.
A `retry_count > 0` AND `response_type = 'safe'` = retry exhausted, safe fallback.

---

## 3. What Good Looks Like

### Successful RAG Path (happy path)
```
query_rewriter  done   "What are the side effects of aspirin?" (300ms)
router          done   "RAG required: YES" (120ms)
retrieval       done   "8 chunks retrieved" (450ms)
reranker        done   "Top 4 selected after RRF" (5ms)
evaluator       done   "relevant: true — Documents contain detailed side effects" (280ms)
response        done   "Aspirin can cause..." (1200ms)
memory          done   "History saved" (2ms)
```

### Retry Path (evaluator fires)
```
query_rewriter  done   "What about it?" (280ms)
router          done   "RAG required: YES" (100ms)
retrieval       done   "8 chunks retrieved" (430ms)
reranker        done   "Top 4 selected after RRF" (4ms)
evaluator       done   "relevant: false — Documents don't contain specific dosage" (260ms)
  ↳ RETRY #1
query_rewriter  done   "What is the recommended aspirin dosage for adults?" (250ms)
retrieval       done   "8 chunks retrieved" (440ms)
reranker        done   "Top 4 selected after RRF" (4ms)
evaluator       done   "relevant: true — Dosage section found" (255ms)
response        done   "The recommended adult dose is..." (1100ms)
memory          done   "History saved" (2ms)
```

### Direct Path (no retrieval)
```
query_rewriter  done   "Hello!" (280ms)
router          done   "RAG not required: NO" (95ms)
response        done   "Hello! How can I help you?" (800ms)
memory          done   "History saved" (2ms)
```

---

## 4. Known Limitations

| Limitation | Impact | Mitigation |
|---|---|---|
| BM25 built in-memory each query | Slow for large corpora | Pre-build index on startup |
| Evaluator uses same LLM provider as response | Correlated failures | Use a separate evaluator model |
| Max 2 retries before safe fallback | May miss some queries | Tune `MAX_RETRIES` in `.env` |
| No cross-encoder reranking | Lower precision on subtle queries | Add `cross-encoder/ms-marco-MiniLM-L-6-v2` |
| Session memory is file-based JSON | Not suitable for multi-instance | Move to Redis for production |
