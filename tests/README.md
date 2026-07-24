# Test Suite

```bash
pytest                      # everything (~10s)
pytest tests/test_api.py    # one file
pytest -k persistence       # by name
```

## Principles

**No network, ever.** An autouse fixture in `conftest.py` blocks every non-loopback
socket. If a test tries to reach Postgres, Groq, or Gemini it fails loudly with
"a boundary is unpatched" rather than silently hitting production, going slow, or
flaking. The LLM, the database, and the vector store are all faked
(`FakeGateway`, `FakeLLM` in `conftest.py`).

**Tests guard behaviour, not implementation.** Most of these exist because the
behaviour they assert *actually broke in production*. Those tests carry a
`Regression:` note naming the bug, so nobody "cleans up" the assertion later
without understanding what it protects.

**Failures should be loud.** Several tests assert that an error *surfaces* —
silently swallowed failures (a file that never generated, a message that never
saved) were the single most damaging bug class in this codebase.

## What each file covers

| File | Covers |
|---|---|
| `test_persistence.py` | Session ownership, message round-trip, DirectGateway implements the full DataGateway protocol |
| `test_orchestrator.py` | Full pipeline with fakes: session creation with owner, user settings reaching every answer path, file generation on the DIRECT route, error surfacing |
| `test_pipeline.py` | Rewriter/router/evaluator handling of messy LLM output: preambles, fences, malformed JSON, empty responses |
| `test_file_generation.py` | JSON extraction from real-world LLM output, ragged-table normalization, builders vs. `&`/`<`/`>` in tax text, valid PDF/OOXML output |
| `test_api.py` | Auth required, cross-user access denied, download MIME/filename, admin-only routes |
| `test_retrieval_and_memory.py` | RRF fusion, BM25 ranking, chunking, history formatting and token budget |

## The gateway protocol must stay fully implemented

`DirectGateway` is the only `DataGateway` backend. `test_direct_gateway_implements_full_protocol`
fails if a method is declared on the protocol but missing from `DirectGateway` —
catches a partial implementation before it becomes a runtime `AttributeError`.

## Adding a test

Name it after the behaviour, not the function
(`test_cannot_download_another_users_file`, not `test_download_2`). If it's
guarding a bug you just fixed, say so in a comment — the next reader needs to
know why it's there.
