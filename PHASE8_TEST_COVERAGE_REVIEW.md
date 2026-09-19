# Phase 8 — Test Coverage Review

**478 passed, 10 skipped** across 12 files (~457 test functions after
parametrisation).

| File | Tests | Area |
|---|---|---|
| `test_age_safe_gateway.py` | 82 | plan contract, compiler, injection, boundary |
| `test_aggregate_engine.py` | 68 | spec validation, compilation, grain |
| `test_aggregate_gate_profiles.py` | 64 | catalogue, selection policy, evaluation |
| `test_aggregate_multipath.py` | 50 | authority, populations, consensus |
| `test_aggregate_three_way.py` | 44 | reconciliation |
| `test_aggregate_gate_enforcement.py` | 34 | D1–D4 regressions |
| `test_aggregate_temporal.py` | 33 | time windows |
| `test_age_eval_containment.py` | 26 | evaluator isolation |
| `test_aggregate_shadow.py` | 17 | shadow comparison |
| `test_aggregate_corpus.py` | 16 | corpus integrity |
| `test_age_client.py` | 12 | AGE client wrapper |
| `test_age_gateway_phase6_fixes.py` | 11 | Phase 6 regressions |

---

## Coverage by area

### Planning — **strong**
Strict parsing (14 forbidden-field cases), closed enums, frozen plans,
alias restriction, invalid-plan refusal, profile selection across all five
policy outcomes, requirement derivation from typed specs only.

### Routing — **adequate**
Direct vs composite decisions covered for simple / filtered / grouped /
windowed / multi-constraint / ratio-fallback shapes. Composite constraint
propagation and intersection ordering covered.

*Gap:* no test asserts the routing decision for a spec with **two**
property filters across different sources.

### Safety — **strong**
Gate blocking on both paths, unapplied-constraint refusal, no-silent-
fallback (source-level assertion that the composite failure branch returns
before `execute(`), raw-Cypher prevention (structural, plus
forbidden-field parametrisation), injection (8 hostile values proven to
stay parameters), Unicode/homoglyph evasion.

*Gap:* no test exercises a `CompilerDefect` path end-to-end through
`route_age.run()` — only `assert_read_only()` in isolation.

### Data correctness — **adequate**
Time-window resolution (33 tests), tombstone/supersession injection,
fanout detection in three directions, distinct-key proofs against measured
uniqueness.

*Gap:* the six live multi-path tests are **skipped** (see
`PHASE8_REMAINING_ITEMS.md` §C2), so composite correctness against real
data is verified only by standalone scripts and corpus runs, not by pytest.

### Reconciliation — **strong**
AGREEMENT, CONFLICT (4-vs-70 pinned), SEMANTICALLY_DIFFERENT, invariant
violations, canonicalization proofs and refusals, snapshot-gate removal.

*Gap:* no test covers reconciliation when **all three** routes disagree.

---

## Important missing tests

Listed, not written — §8 says identify, not expand.

1. **Live composite via pytest** (6 skipped). Highest value; blocked on a
   product decision.
2. **`compile_failed` through the full route.** The corpus exercises it;
   no unit test pins it.
3. **Two-source property filters** in the routing decision.
4. **Three-way disagreement** in reconciliation.
5. **End-to-end `CompilerDefect`** refusal path.

None is a correctness risk today; each is a blind spot if the surrounding
code changes.
