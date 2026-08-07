# Agent Harness compliance suite

Automated checks for the 5 independent enforcement points
[`AGENT_HARNESS_DESIGN.md`](../../../../AGENT_HARNESS_DESIGN.md) §4 requires
every harness tool to respect. Each point is checked in its own module,
independently — per §4's own framing, none of the 5 supersedes another, and
this suite mirrors that: passing `test_enforcement_3_*` says nothing about
whether `test_enforcement_4_*` also passes.

| Module | Enforcement point |
|---|---|
| `test_enforcement_1_api_boundary.py` | API boundary hard 403 (design §4.1) |
| `test_enforcement_2_rls_arming.py` | RLS context arming (design §4.2) |
| `test_enforcement_3_cross_case_role_gate.py` | Per-tool cross-case role checks, ordering (design §4.3) |
| `test_enforcement_4_role_provenance.py` | `user_role` provenance — never from a profile object (design §4.4) |
| `test_enforcement_5_scoped_cypher.py` | `scoped_cypher()`'s within-case chokepoint (design §4.5) |

(Enforcement point 6 — the Verifier's `_check_leakage()` backstop — is not
checked here: the Verifier is an explicitly later phase, out of scope for
Phase 0's foundation layer.)

## Running

```
pytest src/pipeline/harness/compliance/
```

Deliberately **not** under `tests/` and **not** added to `pytest.ini`'s
`testpaths` — this suite checks harness-internal code shape (tool wrapper
source text) plus a handful of security-critical behavioral regressions, not
general application behavior. Wiring it into CI as a merge gate is its own,
later checklist item
([`AGENT_HARNESS_IMPLEMENTATION_PLAN.md`](../../../../AGENT_HARNESS_IMPLEMENTATION_PLAN.md)
§8), not part of this phase — until then, run it explicitly alongside the
main suite before merging any harness change.

## What "fails loudly" means here

Most checks are static source scans (`_source_scan.py`) over the 7 tool
wrapper modules in `src/pipeline/harness/tools/` — e.g. "no wrapper file may
reference `current_rls_active`". A regex/substring hit is a real violation,
not a false positive requiring interpretation: every pattern checked for is
one that should never legitimately appear in a tool wrapper file, by design.
The handful of behavioral checks (role-gate `PermissionError` → `DENIED`,
role-provenance pass-through, `scoped_cypher()`'s own `ValueError` guards)
exercise the actual code paths with fakes, not mocks that assert their own
call shape — a broken wrapper produces a wrong `ToolResult`, not a mock
assertion failure.
