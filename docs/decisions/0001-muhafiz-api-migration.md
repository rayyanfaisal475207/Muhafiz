# 0001 — Migrate the evidence source from the synthetic corpus to the Muhafiz Data API

**Status:** in progress (Milestone A landing; see checklist at the bottom)
**Date:** 2026-08-20

## Context

Muhafiz's evidence graph and vector store were built from a locally generated
synthetic document corpus: 96 rendered PDFs in `data/documents/`, produced by
`scripts/batch1_*.py` from hand-written Python literals in
`build_entity_roster.py` / `build_case_index.py`, then OCR'd, chunked, embedded,
and run through LLM extraction (`ner.py`, `domain_entities.py`,
`doc_classifier.py`) to guess back the entities that were invented in the
first place.

A PSRMS-shaped REST API was stood up (`https://muhafiz.onrender.com`, see
`API_CONSUMER_GUIDE.md`), backed by a schema reverse-engineered from two
genuine filled Islamabad Police case files (`muhafiz_schema.dbml.txt`,
revision 11). It serves FIR bundles with all 12 child tables, plus CMS
complaints, PKM service applications, criminal records and roznamcha entries —
already structured, with `updated_since` incremental sync support.

**Decision:** make that API the source of evidence. Discard derived artefacts
built from the synthetic corpus, and rebuild the graph from ground-truth
structured fields instead of LLM re-extraction, so that cross-case linkage,
entity resolution, and timeline building work on real identity keys rather
than guesses.

## Confirmed scope boundary

`muhafiz.onrender.com` is a **same-schema stand-in**, not the real police
system. The real integration (actual police system, same schema, larger
volume) arrives **post-MVP** as a separate text-format handoff. This decision
record and the modules built under it deliberately do **not**:
- redesign for air-gap operation (this endpoint is a plain internet call,
  gated by `MUHAFIZ_API_BASE_URL` being configured — see `src/config.py`);
- add a second, file-drop ingestion path in anticipation of the real
  integration's eventual shape — that is explicitly deferred, to avoid
  building against a shape that isn't known yet.

## What was measured on the live API (not assumed)

All five endpoints were fetched and analysed directly, on 2026-08-20:

| Fact | Value | Consequence |
|---|---|---|
| Volume | 73 FIR, 74 roznamcha, 4 CMS, 14 PKM, 33 criminal = 198 records | Full re-ingest is minutes, not hours. Batch, not streaming. |
| Free text | narrative 59K + zimni 66K + roznamcha 4K ≈ 130K chars → ~350 chunks | At e5's 1-request + 0.3s per chunk, a full re-embed is ~5–10 min. |
| CNIC coverage | accused 94/94, witness 33/37, complainant on FIR | CNIC-auto-merge has near-total coverage on this dataset. |
| Cross-silo CNIC overlap | PKM 10/10, CMS 4/4, criminal 31/32 overlap FIR CNICs | This is the cross-case backbone, and it is designed-in. |
| Cross-case CNIC | only 4 CNICs appear in >1 FIR | Genuine cross-case merges are few but real. |
| Name collisions | 44 names span >1 FIR (e.g. one name in 10 FIRs, another in 8) | Drives the entity-resolution corroboration-gate decision below. |
| `cross_version` | 0 rows — table is entirely empty | Cross-FIR linkage must come from elsewhere (prose citations). |
| FIR→FIR refs in prose | 9 FIRs cite another real FIR (one FIR cited by 3 others — a crime series) | Recoverable cross-case signal `cross_version` doesn't carry; not written as fact — see entity-resolution section below. |
| `e_tag_number` | 5/73 populated, but CMS `case_tag_number` matches 4/4 | Complaint→FIR escalation link works where present. |
| `criminal_record_ref` | 0 of 6 match `external_record_ref` | The documented soft-ref is broken in the data — join criminal records by CNIC instead. |
| PKM `forwarded_fir_number` | 4 of 8 women_violence reports resolve to a real FIR | Second cross-silo link. |
| Structure | 19 stations, 8 districts, 36 sections, 6 acts | Real multi-jurisdiction aggregates become meaningful. |
| Null-heavy fields | `fir_position.position` 65/94 null, `zimni.entry_type` 188/259 null, `witness_type` 0/37 populated | Downstream loaders must not treat these as reliable. |
| Schema drift | API returns legacy `crime_scene_description/_distance_km/_direction/_beat_number` (all null) alongside the merged `crime_scene_location` the DBML documents | Loaders tolerate both shapes (`FirRecord.crime_scene_location` in `src/data_gateway/muhafiz_api/models.py`). |
| ID shape | `fir_id` = `"fir-1001-26"`, `police_station_id` = `"PS-ISB-CYBER"` — slugs, not the UUIDs the DBML declares | Never parsed as UUID anywhere in the client. |
| Provenance | `source` field = `"synthetic"` on 21 FIRs and all PKM applicants | Real-schema, synthetic-content data — a large upgrade over the generated corpus, but not real case records. Threaded through as metadata (M3/M6a), not hidden. |

## Accepted risk — entity resolution name-fallback

Name-fallback resolution (`entity_resolution.py`) stays enabled, including for
structured records. Because accused/witness names in this dataset are common
single-name mononyms and `father_name` is only 22/94 populated, every accused
whose CNIC is new also risks generating a name-based `SAME_AS(status='pending')`
candidate against every same-named person already in the graph. Measured
baseline: ~44 name groups span multiple FIRs, of which only 4 are genuine.

Mitigation, not a behaviour change: a **corroboration gate** specific to
structured-record mentions (M6a) — a name-fallback candidate only reaches
FLAGGED/REVIEW tier if corroborated by shared case, matching address, or an
existing structured-id hit (phone/plate/father_name); otherwise it mints a new
node with no `SAME_AS` candidate at all. Governed by
`ENTITY_RESOLUTION_NAME_FALLBACK_FOR_STRUCTURED` (default `True`). Verified by
reporting pending-`SAME_AS` counts by tier, before vs. after the gate, against
this baseline — if the gate doesn't materially close the gap, that is the
trigger to revisit the default, not a decision made in advance of the
measurement.

FIR→FIR prose citations (the 9 measured above) follow the same discipline for
the same reason — a regex hit against free text carries the same false-positive
risk profile as a name match, so it gets the same bar: written as a `CITES`
edge (`Case`/`Incident` → `Case`/`Incident`, not `SAME_AS` — that edge type is
reserved for entity-identity claims) with `status: pending`, reviewed through
the same human-confirmation discipline before being treated as fact.

## Consequences

- Cross-case entity resolution becomes testable against real CNIC collisions
  instead of hand-designed synthetic ones.
- `StructuredRecord`, and the `INVOLVED_IN`/`PART_OF`/`LOCATED_AT`/`OWNS`/
  `REGISTERED_TO` edge types declared in the schema since the graph's original
  design but never written by any code path, get real writers for the first
  time (M6a).
- `data/memory/entity_roster.csv` (the hand-built entity-resolution eval
  ground truth) is superseded — real CNIC gives must-merge/must-not-merge
  pairs directly (M11).
- The synthetic corpus and its derived Chroma/AGE state are wiped, not kept
  alongside the new data (M5) — `data/memory/` is retired as a source, not
  deleted.
- `police_reference_data` (6 seeded rows) is extended additively with the 36
  sections / 6 acts observed in the real data (M7) — not replaced.

## Milestone checklist

- [x] **M1** — `src/data_gateway/muhafiz_api/` (client, models, errors,
      snapshot), config, `.env.example`. This document.
- [ ] **M2** — `ingest_documents()` entry-point extraction.
- [x] **M3** — record → `Document` rendering (`src/ingestion/muhafiz_records.py`),
      Chroma metadata extended in both allowlist places, `docs/INGESTION.md` updated.
- [x] **M4** — case provisioning from FIRs (`src/ingestion/muhafiz_cases.py`,
      `scripts/sync_muhafiz_cases.py`); CMS/PKM escalation matching measured
      and locked in by test (4/4 CMS, 4/8 PKM resolve to a real FIR).
- [x] **M5** — evidence-state reset built (`scripts/reset_evidence_state.py`):
      graph → both Chroma collections → derived Postgres rows → filesystem,
      dry-run default, `--execute --yes-i-am-sure` double-gate. **Built and
      tested only — NOT yet run against live infrastructure.** Postgres was
      not running locally during this session; the AGE graph drop/recreate
      path needs a live instance and is covered by
      `TestRequiresLivePostgres` (skipped by default) plus this record's own
      Milestone C verification checklist, to run manually before any real
      `--execute`.
- [x] **M6a** — deterministic structured graph projection + corroboration gate
      (`src/graph/structured_projection.py`): Person nodes with real CNIC for
      complainant/accused/witness; `Weapon` nodes with in-FIR-only `OWNS`
      matching; `StructuredRecord` written for the first time in this
      codebase's history (`fir_section`/`malkhana_register`/
      `chalaan_dispatch`/`chalaan_outcome`/`fir_zimni_index`); `INVOLVED_IN`/
      `PART_OF`/`LOCATED_AT` written for the first time; `OCCURRED_ON` from
      typed timestamps; `docs/graph_schema.md` updated. 20 new tests
      (control-flow + a full zero-error sweep over all 73 real FIRs).
- [x] **M6b** — cross-silo linking + `CITES` prose-citation candidates
      (`src/graph/cross_silo_projection.py`): CMS/PKM StructuredRecord nodes
      with `BELONGS_TO_CASE`/complainant-or-applicant `Person` resolution
      when linked (measured 4/4 CMS, 4/8 PKM), criminal records linked to
      an *existing* Person by `subject_cnic` (never the broken
      `criminal_record_ref`), and `CITES{status: pending}` edges for the
      measured 9 FIR→FIR prose citations — reviewed through a **separate**
      queue in `src/api/graph_review.py` (`/citations/pending`,
      `/citations/{id}/confirm`, `/citations/{id}/reject`), never merged
      into the `SAME_AS` queue since `Case` nodes have no `entity_id`.
      `docs/graph_schema.md` updated. 19 + 19 new tests (cross-silo
      projection + graph_review's new endpoints), including a full
      real-snapshot sweep and a regression lock on the measured 9-citation
      count.
- [x] **M7** — extraction adaptation + additive `police_reference_data` load:
      `structured_fields.extract_fir_display_codes()` for real `NNN/YY` codes
      (label-anchored, additive alongside the synthetic-corpus regex);
      `doc_classifier.DOC_TYPES` extended with 2 real record types and fixed
      to stop discarding a validated `date_registered` when the LLM's
      `doc_type` is unrecognized (`src/ingestion/service.py`'s write site
      also fixed to omit rather than null out `doc_type` on a re-run);
      `ner.py`'s location gazetteer extended from Islamabad-only to the
      real dataset's 9 districts/19 stations; the `_adjudicate_low_confidence`
      fail-open hole closed (a confidence floor now drops weak
      English-capitalized-run candidates on LLM failure instead of flooding
      the graph with unreviewed noise, while still preserving stronger
      uncertain candidates). `scripts/load_real_offense_sections.py` —
      additive load of the measured 36 real (section_code, act) pairs across
      6 acts into `police_reference_data`, alongside (not replacing) the 6
      hand-curated seed rows. ~20 new/updated tests.
- [x] **M8** — graph read-path/label fixes:
      `migrations/020_age_date_and_cites_labels.sql` pre-creates `Date`
      (previously exposed to the concurrent-first-write race migration 005
      exists to prevent) and `CITES` (introduced by M6b, never declared in a
      migration) for both `evidence_graph` and `evidence_graph_eval` — written
      and unit-tested this session, **not yet applied against a live
      instance** (no Postgres running locally); `_SEED_LABELS`'
      `PhoneNumber`/`Organization` entries fixed to match what is actually
      written (`canonical_name`/`phone`, never the `number`/`name` the
      lookup previously checked — confirmed live during this migration's
      investigation that the seed lookup for these two labels could never
      have matched a real node, a pre-existing bug, not one M6a introduced);
      `reranker.py`'s recency boost now prefers a real `record_date` field
      (`src/ingestion/muhafiz_records.py`, threaded through both
      `vector_store.py` metadata places) over regexing a filename, since
      API-sourced `source` strings carry no year at all; `graph_retriever.py`'s
      module docstring extended with an explicit, documented traversal
      decision for `INVOLVED_IN`/`PART_OF`/`CITES` (none traversed, each with
      its own stated reasoning) alongside the pre-existing
      `LOCATED_AT`/`OWNS`/`REGISTERED_TO` decision.
- [x] **M9** — idempotent `--full` re-ingest (`scripts/sync_muhafiz_data.py`).
      `updated_since` watermark automation cut per round-2 review (stand-in
      API, real integration is post-MVP). What's kept: a per-record
      edge-purge-by-source-prefix before every (re-)projection, since
      `write_edge` is `CREATE`-only — proven, not just claimed, by a test
      that runs the same FIR through the sync twice (and three times) and
      asserts the graph edge count never grows.
      `ingest_documents()` gained a `run_graph_extraction` flag (default
      `True`, unchanged for every existing caller) so Muhafiz Data API
      records skip the legacy LLM/NER pass entirely — M6a/M6b already
      extract these from ground truth, and running NER a second time over
      the same text would both waste cost/latency and tag the graph with a
      second, hashed family of `source_doc_id`s the purge could never
      target. Order: cases (M4) → FIRs (M6a) → CMS/PKM/criminal records
      (M6b) → citations (M6b, last, once every FIR's `Case` node exists).
      10 new tests. **Built and tested only — not yet run against live
      infrastructure** (no Postgres/AGE running locally this session); a
      dry-run smoke test against the full real 73-FIR snapshot did run and
      passed.
- [x] **M10** — harness adaptation to real data shape:
      `data_quality.py`'s `_ENTITY_LABELS` was missing `StructuredRecord`
      entirely — the label M6a/M6b implement for the first time this
      session, now one of the largest node populations a real case has;
      every case's entity-coverage metric would have silently undercounted.
      `timeline_building.py`'s `_fetch_dated_incidents` assumed one
      Incident carries at most one live `OCCURRED_ON` edge (true for the
      legacy LLM-derived path) and de-duplicated on `entity_id` — wrong
      under M6a, where one Incident deliberately carries several
      (incident date, each zimni entry, dispatch date), each a genuinely
      different event; the old logic silently collapsed a real case's
      whole timeline down to one arbitrary entry. Fixed to dedup on the
      edge's own `id(occ)` instead, with distinct per-event ids and
      event_type-aware descriptions. `cross_case_linkage.py` and
      `large_scale_aggregate.py` reviewed and found to need no change —
      both are already insulated by the tool-layer abstraction
      (`SUBAGENT_INTERFACES.md`'s own "no graph types cross a sub-agent
      boundary" invariant) and already benefit transitively from M8's
      `graph_retriever.py` fixes and M4's real station/district data.
      `cross_case_linkage.py`'s hedging language deliberately left as-is —
      the measured corroboration-gate false-candidate risk (§4 above)
      means caution is still warranted regardless of CNIC reliability
      improving. The pre-existing `ingestion_jobs`-has-no-`case_id` gap
      `data_quality.py` already documents is **not** resolved by this
      migration — `scripts/sync_muhafiz_data.py` (M9) doesn't write
      `ingestion_jobs` rows either, so the gap is unchanged, not newly
      introduced. 4 new tests.
- [x] **M11** — eval set regenerated from real data:
      `scripts/build_real_entity_roster.py` derives must-NOT-merge
      ("confusable-pair") and must-MERGE ("name-variant") entity-resolution
      ground truth directly from real CNIC data — 41 real name-collision
      groups, 4 real cross-FIR CNIC matches — replacing
      `data/memory/entity_roster.csv`'s hand-invented cast; consumed
      unchanged by `scripts/eval_entity_resolution.py` via its new
      `--roster` flag, same column schema. `scripts/build_real_eval_set.py`
      regenerated `data/eval/eic_eval_set.json` (old synthetic set backed
      up alongside it, both gitignored) — deliberately smaller than the
      synthetic corpus's 204 hand-authored queries (11), every question
      and answer verified programmatically against the live record it was
      built from rather than hand-guessed; genuinely hand-authored
      investigative questions are real, separate future work once a human
      reviewer can author and verify them against this dataset.
      `scripts/eval_end_to_end.py` and `scripts/eval_keyword_search.py`
      were more broken than the plan assumed — not just the wrong eval-set
      schema (`{"queries": [...]}` vs. the actual bare list) but an import
      of `src.retrieval.hybrid_search`, a module that has never existed in
      this codebase — rewritten against the real retrieval stack
      (`embed_text`/`query_similar`/`retrieve_bm25`/`rerank_results`,
      reproduced from `orchestrator.py`'s own sequence). Along the way,
      found and fixed a real functional gap while auditing
      `test_orchestrator.py`'s FIR-auto-scope test against real data:
      `orchestrator.py`'s query auto-scope only recognized the synthetic
      FIR-number format, and even extracting a real display code
      wouldn't have matched anything (the substring check runs against
      chunk `source`, which for API-sourced chunks is a slug id, not the
      human-readable code a user types) — fixed with a new
      `fir_display_code` chunk-metadata field
      (`src/ingestion/muhafiz_records.py`) and a second match path.
      `test_structured_fields.py`/`test_ner.py`/`test_urdu_text_processing.py`'s
      synthetic-format literals were reviewed and found to need no
      change — they test regex/NER/tokenizer behavior against real Urdu
      edge cases (borrowed from the synthetic ground truth's genuinely
      real narrative sentences) or the synthetic regex path that M7
      deliberately kept alongside the real one, not "the synthetic corpus"
      as a concept. ~35 new tests.
- [x] **M12** — final documentation pass. `docs/DATABASE_DESIGN.md` rewritten
      from scratch — it predated `cases`/`case_assignments`/`audit_logs`/
      community-detection tables/RLS entirely (~5 tables documented vs. 18
      real ones), and its own "checked against `docs/schema-snapshot.json`"
      claim was stale too (that generated dump is *also* missing the same
      tables, and nothing in this migration regenerates it — no live
      Postgres was available this session; noted explicitly in the doc
      rather than silently trusted). `docs/graph_schema.md` and
      `docs/INGESTION.md` were already kept current incrementally, module
      by module (M3/M4/M6a/M6b/M8), per the confirmed round-2 decision to
      land docs with the change rather than batching them at the end.
      `API_CONSUMER_GUIDE.md` committed to version control — the one new
      tracked file.

## Status: all 12 modules complete

Every module landed as its own branch, merged `--no-ff` into local `main`,
full test suite + harness compliance suite green at every step. Nothing
pushed to `origin` — that remains the operator's call.

**What was NOT run this session, stated plainly:** no live Postgres/AGE
instance was available locally, so `scripts/reset_evidence_state.py` (M5)
and `scripts/sync_muhafiz_data.py` (M9) — the two scripts that actually
wipe and repopulate a real graph — were built and thoroughly unit-tested
(including a proven idempotency guarantee against a fake graph store) but
**never executed against live infrastructure**. `migrations/020_age_date_and_cites_labels.sql`
was likewise written and never applied. Before running any of this for
real: `docker-compose up -d`, confirm the model-server tunnel
(`GET $MODEL_SERVER_BASE_URL/health`), then dry-run each script before
`--execute`/`--full`, in the order M5 → apply migration 020 → M9.
