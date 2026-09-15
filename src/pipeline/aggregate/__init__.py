# -*- coding: utf-8 -*-
"""
Aggregate engine (Phase 0/1 foundation) — src/pipeline/aggregate/.

OFFLINE / SHADOW ONLY. Nothing in this package is wired into the live
aggregate path. `src/pipeline/xagg.py`'s 43 keyword-dispatched functions
remain the production source of truth, unchanged, until shadow comparison
has been implemented and evaluated (a later phase).

WHAT THIS PACKAGE IS FOR. The production engine dispatches a question to
one of 43 hand-written functions by first-match-wins keyword lists, and
each function hardcodes its own Cypher and its own grain decisions. That
design cannot answer a question nobody coded, and — measured live during
the architecture review — it has no structural defence against counting
at the wrong grain. This package is the compositional replacement: a
registry of what the data actually contains, a typed spec describing what
was asked, a deterministic validator, and a deterministic compiler. The
LLM's only job (in a later phase) is to fill the spec; it never writes
executable query text.

THE CENTRAL SAFETY RULE, and the reason the compiler exists at all:

    The LLM may choose semantics. It may never generate executable
    SQL/Cypher. Query STRUCTURE comes from a validated spec; query VALUES
    are always bound parameters.

`src/graph/age_client.execute_cypher()`'s own contract requires this — its
`cypher_query` argument is interpolated into SQL text (AGE's `cypher()`
declares `query_string` as `cstring`, which libpq cannot bind as a wire
parameter), so it is safe ONLY for literal templates written in this
codebase. A compiler that emits from a closed algebra satisfies that
contract; an LLM writing Cypher would violate it.

MODULES
  registry.py  — Phase 0: the live-data schema mirror (labels, property
                 presence, relationship cardinality, logical-field
                 authority, tombstone/version policy).
  spec.py      — Phase 1: AggregateSpec and the population algebra.
  validator.py — Phase 2: deterministic rejection of unsafe specs.
  compiler.py  — Phase 3/4: spec -> parameterized Cypher/SQL, with
                 tombstone and supersession predicates injected by the
                 compiler rather than supplied by the caller.
  shapes.py    — Phase 5: post-execution result-shape guards.
  coverage.py  — Phase 6: the coverage model.
  receipt.py   — Phase 7: AggregateReceipt.
"""
