# Muhafiz — Phase 4, Module 4.1 Closeout (live-infra verification)

Module 4.1 (case-scoped `doc_id` generation and retrieval-filter consistency)
was implemented on branch `fix/phase-4-module-4.1-doc-id-case-scoping-v2`
(merged to `main` — check `git log --oneline -5` for the merge commit) in a
session **without live Postgres/ChromaDB access**. Everything unit-testable
without live infra was written and passed (508 passed, 4 skipped, 0 failed).
Two things explicitly could not be verified there and need a session with
real `psql`/Postgres + a real ChromaDB collection (same setup used for the
Phase 0-3 closeout — `localhost:5432`, live Chroma persist dir).

## What changed, for context

- `src/ingestion/document.py` (`Document._generate_id`): `doc_id` hash seed
  now includes `case_id`/`project_id` (`scope::source::page::text[:200]`),
  so two different cases ingesting a same-named file no longer collide to
  the same id in Chroma.
- `src/ingestion/chunker.py` / `src/ingestion/service.py`: `chunk_documents`
  tags each parent doc with `case_id`/`project_id` and re-derives `doc_id`
  before deriving chunk ids.
- `src/data_gateway/direct_backend.py:621-628` (`insert_documents`):
  `ON CONFLICT (doc_id) DO NOTHING` → `DO UPDATE SET case_id = EXCLUDED.case_id,
  project_id = EXCLUDED.project_id, is_global = EXCLUDED.is_global, doc_type =
  EXCLUDED.doc_type`. **This SQL has never been executed against a real
  Postgres instance — only read for syntax correctness.**
- `src/pipeline/orchestrator.py` (two `where_clause` sites, ~889 and ~1265):
  a case-scoped query with no `project_id` now filters on `case_id` alone,
  instead of incorrectly ANDing `is_global: True` (which excluded real case
  evidence, since it's always ingested with `is_global=False`).
- New: `scripts/find_doc_id_collisions.py` — a read-only audit script that
  cross-references Postgres's `documents` table against Chroma's
  `get_all_metadata()` to find any `doc_id` whose Chroma chunks disagree
  among themselves on `case_id`/`project_id`, or whose Postgres row
  disagrees with what Chroma actually carries (the historical damage from
  the `DO NOTHING` bug, before this fix). **Never run against real data.**

## Task 1 — Verify the `ON CONFLICT DO UPDATE` SQL for real

1. Using a live Postgres connection (`DATABASE_URL` or `MCP_DATABASE_URL`,
   whichever this environment has configured — check `.env`), call
   `DirectGateway.insert_documents()` (or run the equivalent raw SQL
   directly via `psql`) twice for the same `doc_id`, with different
   `case_id`/`project_id`/`is_global`/`doc_type` values the second time.
2. Confirm the row's columns after the second call reflect the **second**
   call's values (`DO UPDATE` fired), not the first (which would mean the
   syntax silently fell back to `DO NOTHING` behavior or errored and was
   swallowed somewhere upstream).
3. Confirm no constraint violation or error is raised by the new syntax
   (foreign key on `case_id`/`project_id` still resolves normally on
   update, just as it did on insert).
4. Clean up the test row afterward (`DELETE FROM documents WHERE doc_id =
   '<test id>'`) so this doesn't leave synthetic data in a real database —
   confirm first this isn't a shared/production instance before deleting
   anything.

## Task 2 — Run the collision-finder audit script against live data

1. Run `python scripts/find_doc_id_collisions.py` against the real
   Postgres + Chroma instance.
2. Report the full output: how many `doc_id`s (if any) show Chroma-internal
   `case_id`/`project_id` disagreement, and how many show Chroma-vs-Postgres
   divergence (the `DO NOTHING`-era damage, if any exists in this
   environment's data).
3. This script is read-only and makes no writes — do not extend it to
   auto-fix anything found. If it flags real collisions, list them plainly;
   deciding which case's data is authoritative for a flagged `doc_id` is a
   human call (per `solution.md` §10, deliberately deferred), not something
   to resolve in this closeout.

## Task 3 (optional, if time permits) — End-to-end ingest sanity check

Ingest the same small test file under two different fake `case_id`s (e.g.
via `ingest_file(file_path, case_id="TEST-CASE-A")` then again with
`case_id="TEST-CASE-B")`), and confirm:
- The two ingests produce two different `doc_id`s (visible in Chroma/
  Postgres), not one overwriting the other.
- A case-scoped retrieval query for `TEST-CASE-A` (via `query_similar` with
  `where={"case_id": "TEST-CASE-A"}`, matching the orchestrator's new
  `where_clause` construction) returns only that case's chunk, not the
  other's.

Clean up both test documents (Postgres row + Chroma chunks) afterward.

## Report back

State plainly: which of Tasks 1-3 ran, their actual output/results (not
"should work"), and whether anything found contradicts what Module 4.1's
implementation assumed. If the `DO UPDATE` SQL or the collision script
surfaced anything unexpected, flag it — don't silently patch around it.
