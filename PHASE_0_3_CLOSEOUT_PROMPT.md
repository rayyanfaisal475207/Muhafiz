# Muhafiz — Phase 0–3 Closeout: Live-Infra Verification & Remaining Work

You are continuing work on the Muhafiz Evidence Intelligence Platform (FastAPI +
Postgres/Apache AGE + ChromaDB backend, two React frontends), in this exact working
directory (`D:\Rapids AI\Evidence Intelligence Platform`). A prior Claude Code session
implemented Phases 0–3 (Module 3.1 only) of an audit remediation plan, but had **no live
Postgres, Apache AGE, or GPU/model-server access** — so several fixes were implemented and
unit-tested but never verified against a real instance, and one script was never even
started because it requires live DB access to design safely. This prompt is the closeout
list for exactly that gap.

**This prompt does NOT cover Phase 4 onward.** That forward continuation is already scoped
in `IMPLEMENTATION_PROMPT.md`'s own "Start here" section — do not touch it from this prompt
unless explicitly told to.

## Read first

- `issues.md` — the 125-finding audit (source of truth for what's broken).
- `solution.md` — the approved implementation plan (source of truth for what to do about
  it). §3 (graph contamination) and Module 1.2 (MCP least-privilege) are the most relevant
  sections for this closeout.
- `IMPLEMENTATION_PROMPT.md` — the master continuation prompt; its "Progress so far" section
  has the full history of what's been done and why, including two deviations from the
  original plan (Phase 2's router-scoping redesign, Phase 3.1's read-path graph threading)
  that matter if you touch these files again.
- `MANUAL_RLS_VERIFICATION.md` — the exact procedure for Task 2 below. Don't improvise a
  different verification approach; this one was written specifically against the real code.

## First: confirm you actually have what this closeout needs

Before doing anything else, confirm:
- A live Postgres instance with the Apache AGE extension installed and reachable, with
  `DATABASE_URL` pointed at it.
- It is a **disposable/staging database**, not shared production data — several tasks below
  are destructive by nature (RLS policy rewrites, a graph wipe via the eval script, a purge
  script) and are explicitly designed to be dry-run/backed-up first, not run blind.

**If you don't have this, stop here and report that plainly** — do not fake, assume, or
imply verification succeeded. This entire prompt is pointless without real DB access; say so
rather than unit-testing your way around it.

## Non-negotiable working rules (same as the rest of this project)

- **One task at a time.** Work through the task list below in order. After each task, stop
  and report — do not proceed to the next task until told to.
- **Stay inside each task's declared scope.** If a fix genuinely requires touching something
  outside what's listed, stop and flag it before editing.
- **Report outcomes honestly.** Show actual command/query output, not a paraphrase. If
  something fails or can't be verified even with live infra (e.g. `/security-review` isn't
  available in this environment), say so plainly.
- **Branch before any code change** — reuse the branch names given per task below.
- **Commit only when explicitly asked**, one commit per task, message referencing the phase/
  module and the relevant `issues.md` finding titles — same convention as every prior commit
  on this repo (`git log --oneline` to see the pattern).
- **Merge to `main` and push to origin only when explicitly asked.** Nothing in this repo has
  been pushed to origin yet — don't be the first to do it without being told.
- **Take a real backup before any destructive live-DB operation** (Tasks 2, 3, and
  especially 5) — "disposable/staging database" is not a substitute for a backup if you're
  not 100% sure what "disposable" means in your environment.

## Git / branch state you're starting from

- `main` is at Phase 1 (commit `4f7edae`, "Phase 1, Module 1.3: auth/registration
  hardening").
- `fix/phase-2-rls-redesign` — 1 commit ahead of `main` (`715fbfc`, "Phase 2: Row-Level
  Security & Apache AGE isolation redesign"). Done, unit-tested, **unmerged**.
- `fix/phase-3-module-3.1-eval-graph-isolation` — branched off the Phase 2 branch, 1 commit
  ahead of it (`4a053ce`, "Phase 3, Module 3.1: eval/production Apache AGE graph
  isolation"). Done, unit-tested, **unmerged**. Contains Phase 2's commit too, since it was
  branched from there (Module 3.1 depends on Phase 2's `src/graph/case_scope.py`).
- None of these branches exist on `origin` — this is all local-only state in this exact
  working copy.
- Full suite (`python -m pytest tests/ --continue-on-collection-errors`, ~100s) currently
  reports **496 passed, 4 skipped, 0 failed** on the tip branch. The 4 skips are
  `tests/test_rls_integration.py`, marked `requires_postgres` — Task 2 below is what
  actually runs them.

---

## Task 1 — Phase 1, Module 1.2 closeout: verify + tighten the MCP least-privilege role

**Branch:** `fix/phase1-module1.2-mcp-verify`, off `main`.

**Issues addressed:** *"MCP Postgres server connects with the same superuser DB role as the
entire application — no least-privilege scoping for the SQL route."*

Module 1.2 (already on `main`) provisioned a least-privilege `muhafiz_mcp_readonly` role
(`migrations/009_mcp_readonly_role.sql`) and wrote `scripts/verify_mcp_role.py` to confirm
it, but neither was ever run against a real database, and `src/mcp/client.py` still falls
back to the superuser `DATABASE_URL` (with a loud warning) if `MCP_DATABASE_URL` isn't set —
`solution.md`'s Module 1.2 blast-radius note explicitly says to remove that fallback **once
the role is confirmed working end-to-end**, not before.

1. Apply `migrations/009_mcp_readonly_role.sql`.
2. Set a real password: `ALTER ROLE muhafiz_mcp_readonly WITH PASSWORD '...'`.
3. Set `MCP_DATABASE_URL` in `.env` to a connection string using that role.
4. Run `python scripts/verify_mcp_role.py`. It must report every check passing: `SELECT` on
   `police_reference_data` succeeds; `SELECT` on `users`/`audit_logs`/`cases`/`sessions`/
   `messages` is denied; `INSERT` on `police_reference_data` is denied.
5. Manually confirm the actual SQL route (whatever currently calls `src/mcp/client.py`'s
   `execute_query`, e.g. the admin `mcp_demo` endpoint) still works end-to-end against the
   new role, not just the raw-SQL checks above.
6. **Only if 4 and 5 both pass:** remove the superuser fallback in `src/mcp/client.py`
   (currently `raw_url = MCP_DATABASE_URL or DATABASE_URL`, ~line 33) so a missing
   `MCP_DATABASE_URL` fails loudly instead of silently degrading to superuser access.
7. Run the full test suite, report the result.

**Report:** the actual `verify_mcp_role.py` output, whether the fallback was removed (and
why, if not), test suite result, rollback command (`git branch -D` if uncommitted, or revert
the commit if committed).

---

## Task 2 — Phase 2 live verification (RLS + Apache AGE isolation)

**No new branch** — verify against `fix/phase-2-rls-redesign` directly (or the tip branch,
which contains it).

This is the single riskiest change in the whole plan per `solution.md`'s own framing.
**Follow `MANUAL_RLS_VERIFICATION.md` exactly** — do not improvise a different verification
approach. Summary of what it asks for (the file has the precise SQL for each):

1. Create a real non-superuser role (`muhafiz_app`) to test as — `BYPASSRLS`/`SUPERUSER`
   roles will make every check below look like RLS isn't restricting anything even when the
   policies are correct.
2. Apply migrations `001` through `010` in order (`010` is the one this phase adds).
3. **Check 1** — a general (no-case) chat session survives RLS (the direct regression test
   for the NULL-vs-NULL bug — this used to fail silently on every general chat message).
4. **Check 2** — case-scoped rows in `sessions`/`messages`/`pipeline_runs` are invisible
   outside their own case, confirmed via direct SQL probes with `SET LOCAL app.case_id`.
5. **Check 3** — the cross-case RLS bypass is armed only after the XGRAPH/XAGG role check
   passes, never before, and never leaks into a later unrelated query in the same session
   after a denial. Test as both an `investigator` (should be denied) and a `supervisor`+
   (should succeed).
6. **Check 4** — REST CRUD endpoints (e.g. `GET /api/cases/{case_id}`) get a real RLS-level
   backstop independent of the app-layer `require_case_access` check — probed by connecting
   directly as `muhafiz_app` and confirming zero rows even when nothing ran the app-layer
   check. Also confirm you understand which routers (`sessions.py`/`attachments.py`/
   `admin.py`/`graph_review.py`/`projects.py`) are *intentionally* NOT case-restrictive
   backstops per this phase's documented design — don't mistake that for a regression.
7. Alternative/additional: `RUN_POSTGRES_TESTS=1 TEST_DATABASE_URL=<staging-db-url> python -m
   pytest tests/test_rls_integration.py -v` — these encode the same 4 checks as executable
   tests.
8. Run `/security-review` on the `fix/phase-2-rls-redesign` branch (a slash command in this
   environment) — report its findings too.

**Report:** actual pass/fail + SQL output for all 4 checks, `/security-review` findings.
**Do not merge to `main` yet** — that's Task 4, and only once you're told to.

---

## Task 3 — Phase 3, Module 3.1 live verification (eval/production AGE graph isolation)

**No new branch** — verify against `fix/phase-3-module-3.1-eval-graph-isolation`.

1. Apply `migrations/011_age_eval_graph.sql`. Confirm it creates `evidence_graph_eval` as a
   second, physically separate AGE graph with the same vlabel/elabel catalog as
   `evidence_graph` (10 vertex labels, 11 edge labels — see `migrations/005_age_graph.sql`
   for the exact list).
2. **Before** running the eval script, record `evidence_graph`'s current node/edge counts
   (e.g. `MATCH (n) RETURN count(n)`, and per-label if you want finer detail).
3. Run `python scripts/eval_entity_resolution.py`.
4. Confirm: (a) `evidence_graph`'s counts are **unchanged**, byte-for-byte, before vs. after;
   (b) `evidence_graph_eval` now contains the eval fixtures (`Case`/`Document`/`Person`/etc.
   nodes with `EVAL-*`-prefixed `source_doc_id`s); (c) the script's own printed report
   (named test cases, tier precision) runs to completion without error.
5. Confirm the hard runtime guard actually works: temporarily edit
   `scripts/eval_entity_resolution.py`'s `EVAL_GRAPH` constant to drop "eval" from it (e.g.
   `"evidence_graph"`), run the script, confirm it raises `RuntimeError` **immediately**,
   before touching any graph — then revert the edit (do not commit the temporary change).
6. Run the full test suite once more, confirm it's still 496/4/0.

**Report:** actual before/after counts, guard-test output, confirmation the temporary edit
was reverted (not committed).

---

## Task 4 — Merge Phase 2 and Phase 3.1 to `main`

**Do this only after Tasks 2 and 3 are both clean and you've been explicitly told to
proceed** — not automatically because the checks passed.

```bash
git checkout main
git merge --ff-only fix/phase-2-rls-redesign
git merge --ff-only fix/phase-3-module-3.1-eval-graph-isolation
python -m pytest tests/ --continue-on-collection-errors
```

Both merges should fast-forward cleanly (the branch history is linear: `main` →
`fix/phase-2-rls-redesign` → `fix/phase-3-module-3.1-eval-graph-isolation`). If either
doesn't fast-forward, stop and report rather than force-merging — that means something
diverged and needs a human decision, not an automatic resolution.

**Do not push to origin** unless separately, explicitly asked.

---

## Task 5 — Phase 3, Module 3.2: purge existing eval-graph contamination

**Not started at all yet.** Do not start until Task 4 is done and you're explicitly told to
proceed. **Branch:** `fix/phase3-module3.2-purge-contamination`, off `main` (post-merge).

**Issues addressed:** same finding as Module 3.1 — *"The Apache AGE graph contains synthetic
eval-harness test fixtures permanently written into real cases, indistinguishable from real
evidence at the entity level"* — this is the cleanup half; Module 3.1 only closed the
ongoing-damage landmine.

1. **Reconcile the 33-vs-26 `Person` count discrepancy FIRST — this is a hard prerequisite,
   not optional.** Run both of these against the real `evidence_graph` and understand any
   disagreement before deleting anything:
   ```cypher
   MATCH (p:Person) WHERE p.source_chunk_id IS NULL RETURN count(p)
   MATCH (p:Person) WHERE p.source_doc_id STARTS WITH 'EVAL-' RETURN count(p)
   ```
   If they disagree, it means either some real `Person` nodes also lack a `source_chunk_id`
   for an unrelated reason (worth flagging separately — it would mean the "no
   `source_chunk_id` ⇒ never citable" safety property has other causes too), or some `EVAL-*`
   fixtures unexpectedly have a `source_chunk_id`. **The purge's identification query must be
   the `source_doc_id STARTS WITH 'EVAL-'` prefix match — never the `source_chunk_id IS NULL`
   proxy**, which is what produced this discrepancy in the first place.
2. Write `scripts/purge_eval_contamination.py`:
   - Identify via `source_doc_id STARTS WITH 'EVAL-'` (covers the `EVAL-P-*`, `EVAL-NV-*`,
     `EVAL-CP-*`, `EVAL-DRY-*` prefixes the eval script actually generates, plus the generic
     `EVAL-` prefix).
   - Delete **edges first, then nodes** (respect ordering so nothing is orphaned mid-delete):
     all edges where either endpoint's `source_doc_id` starts with `EVAL-` or the edge's own
     `source_doc_id` does; then all nodes across every label (`Document`, `Person`,
     `Vehicle`, `PhoneNumber`, `Organization`, `Address`, and anything else Step 1's
     reconciliation turns up) whose `source_doc_id` starts with `EVAL-`.
   - **Never delete `Case` nodes** (`CASE-002` through `CASE-020`, `CASE-DRY-001` — these are
     real cases). Only their spurious `BELONGS_TO_CASE`/`SAME_AS` edges to fixture entities
     get removed.
   - **Dry-run first**: count-only queries, no `DELETE`, printed against the audit's own
     snapshot numbers so any drift since the 2026-07-27 audit is caught before an irreversible
     write:
     - `Document`: 72
     - `Person`: 26-or-33 (per Step 1's reconciliation)
     - `Vehicle`: 8
     - `PhoneNumber`: 11
     - `Organization`: 6
     - `Address`: 10
     - `BELONGS_TO_CASE` edges: 144
     - `SAME_AS` edges: 88
3. **Take a full backup of the graph/Postgres instance immediately before the real delete** —
   `Cypher DELETE` is not otherwise reversible.
4. Run the dry-run, show the output, **pause and get explicit confirmation before running the
   real delete** — do not chain dry-run → real-delete automatically in one invocation.
5. After the real delete, verify: re-run the audit's original counting queries and confirm
   zero `EVAL-*`-prefixed nodes/edges remain; spot-check a handful of the 18 real cases to
   confirm their genuinely-real entities (populated `source_chunk_id`) are untouched.
6. Run the full test suite, report the result.

**Report:** the reconciliation finding (and what it meant), the dry-run counts vs. audit
snapshot, explicit confirmation a backup was taken before the real delete, post-delete
verification results.
