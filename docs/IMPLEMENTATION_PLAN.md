# Evidence Intelligence Platform — Implementation Plan

This is the build plan derived from `EVIDENCE_INTELLIGENCE_PLATFORM_ARCHITECTURE.md`, updated against the finalized model roster and the Apache AGE graph-database decision. It supersedes the architecture report's own §13 roadmap for execution purposes — the architecture report remains the source of truth for *design intent*, this document is the source of truth for *build order*.

## Finalized model roster

| Role | Model |
|---|---|
| Query Rewriter, Router, Query Expander, Relevance Evaluator, SQL Parameter Extraction, Verifier judge, Domain-entity extraction, NER-fallback, Resolution-adjudication | **Qwen3-14B** (single instance, nine prompted roles) |
| Embeddings | **multilingual-e5-large-instruct** |
| Reranker | **bge-reranker-v2-m3** |
| Sentence splitting | **Rule-based regex** (no model) |
| Generation (final answer) | **Qalb-8B** — confirmed; weights/access to be provided by the user before Phase 0.2 can be completed |

Domain-entity extraction, NER-fallback escalation, and entity-resolution adjudication (Phase 4.5, 4.6, 4.8) are confirmed on Qwen3-14B rather than a dedicated model: it already showed strong structured-extraction and correct-abstention behavior in SQL parameter extraction, and reusing the already-loaded reasoning instance avoids a new VRAM slot. Revisit with a dedicated NER model only if Qwen3-14B's entity-extraction accuracy proves insufficient in Phase 3's synthetic testing.

## Architecture deltas from the original report

- **Graph database: Apache AGE, not Neo4j Community Edition.** AGE runs as a Postgres extension — no standalone graph service to install, back up, or monitor. Its tables are ordinary Postgres tables, so the Postgres RLS policies planned for `cases`/`documents` (Phase 7) apply to the graph directly, which Neo4j Community's no-native-RBAC limitation would not have allowed without a separate application-layer Cypher-scoping mechanism.
- **OCR / scanned-handwritten documents stay excluded** from this build, per the architecture report's client-directed deferral (§4.1, §13). Nothing in this plan reopens that decision.
- **Policy Agent (§10.2)** stays out of scope for this build. It is not silently absent — Phase 7.10 below is the explicit marker for that decision.

## Open decisions blocking specific phases

All model decisions are now confirmed. The one remaining blocker is delivery, not choice:

| Item | Status | Blocks |
|---|---|---|
| Qalb-8B weights/access | Model confirmed; user to provide | Phase 0.2's generation endpoint (0.1, 0.3–0.8 and Phases 1–2 are unaffected and can proceed now) |

---

## Phase 0 — Model-stack swap (foundation)

Everything downstream reuses `call_llm` / `stream_llm` / `embed_text`, so this lands first and once.

| # | Task | Files | Depends on |
|---|---|---|---|
| 0.1 | Stand up vLLM serving Qwen3-14B; point a local endpoint config at it | `src/config.py`, `.env` | — |
| 0.2 | Split the single local-model slot into a **reasoning** endpoint (Qwen3-14B — router/rewriter/expander/evaluator/SQL-extraction/entity-extraction/adjudication) and a **generation** endpoint (Qalb-8B). Add `LOCAL_GEN_LLM_URL`/`LOCAL_GEN_LLM_MODEL`; give `call_llm`/`stream_llm` a role param or split client getters. **Blocked on the user providing Qalb-8B weights/access** — everything else in Phase 0 can proceed without it | `src/llm/client.py`, `src/config.py` | 0.1 |
| 0.3 | Stand up serving for multilingual-e5-large-instruct and bge-reranker-v2-m3 (e.g. TEI or a small FastAPI/sentence-transformers wrapper) | new infra, `docker-compose.yml` | — |
| 0.4 | Add `_embed_local_e5()` to `embedder.py`: instruction-prefixed queries, unprefixed documents; make it the default `EMBEDDING_PROVIDER` | `src/retrieval/embedder.py`, `config.py` | 0.3 |
| 0.5 | New cross-encoder rerank step using bge-reranker-v2-m3, applied to RRF's fused top-N before truncating to `TOP_K_RERANK` | new `src/retrieval/cross_reranker.py`, wire into `orchestrator.py` | 0.3 |
| 0.6 | Remove/repurpose the hardcoded `provider_override="groq"` in `router.py` and `sql_extractor.py` | `src/pipeline/router.py`, `sql_extractor.py` | 0.2 |
| 0.7 | Validate existing prompts (`router.txt`, `evaluator.txt`, `query_expander.txt`, `sql_param_extractor.txt`, `query_rewriter.txt`) produce reliable JSON under Qwen3-14B; adjust wording if needed | `prompts/*.txt` | 0.1 |
| 0.8 | Full Chroma wipe + re-ingest — run after Phase 2, not before, so the corpus is only re-embedded once | — | 0.4, Phase 2 |

---

## Phase 1 — Case & Evidence Data Model

| # | Task | Files | Depends on |
|---|---|---|---|
| 1.1 | Migration: `cases` table (case_id, fir_number, crime_category, investigation_officer, police_station, incident_date, investigation_status, location, description, victim_info JSONB, suspect_info JSONB) | `migrations/004_case_model.sql` | — |
| 1.2 | Add `case_id` FK to `documents`; add structured-record fields (or a new `evidence_items` table) for the structured-record evidence type | same migration | 1.1 |
| 1.3 | SQLAlchemy models: `Case`, update `Document` with the FK + relationship | `src/database/models.py` | 1.1 |
| 1.4 | Extend Chroma metadata schema + `_build_where()` to filter by `case_id`, mirroring the existing `project_id`/`is_global` logic | `src/retrieval/vector_store.py` | 1.3 |
| 1.5 | New `src/api/cases.py`: create/list/get Case, same pattern as `src/api/projects.py`; register in `src/main.py` | `src/api/cases.py`, `src/main.py` | 1.3 |
| 1.6 | `ingest_file()` requires `case_id` — no evidence without a case | `src/ingestion/service.py` | 1.5 |
| 1.7 | Thread `case_id` through `process_query()`'s `where_clause`, SQL route, BM25 filtering | `src/pipeline/orchestrator.py` | 1.4 |
| 1.8 | Minimal Case-picker UI, extending the existing project-selection pattern | `frontend/src/components/layout/*` | 1.5 |
| 1.9 | **Load Cases and attach existing evidence** (added — was an unplanned gap). Phase 1 built the Case model but nothing populated it: 0 cases existed and all 100 documents / 270 chunks carried `case_id = NULL`, so case-scoped retrieval had nothing to scope to. `scripts/load_cases.py` loads the 34 Cases from `case_index.csv`, backfills `documents.case_id` from each Case's `linked_doc_ids`, and writes `case_id` into Chroma chunk metadata (metadata-only, no re-embedding). Idempotent. **Result: 34 cases, 80/100 documents and 214/270 chunks linked**; the 20 unlinked are the 9 real scraped English SOPs plus the Missing-Person/Traffic-Accident reports, which are reference material rather than case evidence and are deliberately left global. Verified: a `case_id`-scoped query returns only that Case's evidence, with no cross-case leakage | `scripts/load_cases.py` | 1.3–1.7 |

---

## Phase 2 — Urdu-Aware Text Processing

| # | Task | Files | Depends on |
|---|---|---|---|
| 2.1 | New `src/ingestion/sentence_splitter.py`: regex splitter on Urdu sentence-final marks (، ۔) + ASCII `.!?`, unit-tested on mixed Urdu/English/Roman-Urdu samples | new file | — |
| 2.2 | **Word tokenization decision** — evaluate and pick `urduhack` vs. Stanza's tokenizer as an explicit choice (not folded into NER); verify library health/Python-version compatibility; document the decision. Shared dependency: NER (Phase 4.5) and BM25 (2.5) both consume whichever tokenizer wins here | new decision doc + integration | — |
| 2.3 | Rework `chunker.py`'s break-point search to snap to 2.1's sentence boundaries instead of raw character offsets | `src/ingestion/chunker.py` | 2.1 |
| 2.4 | Extend `text_normalizer.py` with Urdu character/diacritic normalization | `src/ingestion/text_normalizer.py` | 2.2 |
| 2.5 | Replace `.lower().split()` in `bm25_retriever.py` with 2.2's chosen tokenizer | `src/retrieval/bm25_retriever.py` | 2.2 |
| 2.6 | Lightweight Roman-Urdu detection tag at ingestion time | `src/ingestion/service.py` | 2.4 |

Run Phase 0.8's re-ingest after this phase, once.

---

## Phase 3 — Synthetic Dataset Generation

Sequenced ahead of the graph/entity-resolution phase deliberately: Phase 4's CNIC-tier resolution eval is meaningless without labeled ground truth to validate against, so the test data has to exist before the resolution logic ships.

**Status update, post-audit:** this phase is already substantially built — a full toolchain exists (`scripts/batch1_plan.py`, `batch1_generate.py`, `batch1_render.py`, `batch1_validate_tier1.py`, `batch1_repair_failed.py`, `batch1_write_manifest.py`, `build_case_index.py`, `build_entity_roster.py`, `build_structured_records.py`, `batch1_eval_set.py`, plus a separate `dry_run_*` OCR-testing toolchain). 3.1–3.7 below are done; a direct audit (opening the actual PDFs and ground truth, not just trusting the manifests) found concrete bugs, tracked as 3.8–3.14.

| # | Task | Files | Status |
|---|---|---|---|
| 3.1 | Rule-based templating for structural fields (FIR number formats, station names, section headers, date formats) | `scripts/batch1_plan.py` | Done |
| 3.2 | LLM-generated narrative content (incident narratives, witness statements, IO remarks) | `scripts/batch1_generate.py`, `batch1_render.py` | Done — Urdu only; English/Roman-Urdu narrative variants not yet produced (see 3.11) |
| 3.3 | Recurring synthetic entity cast (people, vehicles, phones, addresses, an organization/gang) with realistic name-spelling variation | `batch1_plan.py`, `build_entity_roster.py` | Done |
| 3.4 | CNIC-variation seeding: consistent-CNIC entities, no-CNIC entities, same-name-different-CNIC hard case | `data/memory/entity_roster.csv` | Done — design exceeds the original spec (confusable pairs, cross-case name drift, a deliberately-unlinked repeat offender); has data bugs, see 3.12 |
| 3.5 | Case-centric corpus organization (`case_index.csv`) | `scripts/build_case_index.py` | Done — but FIR-number references are broken, see 3.9 |
| 3.6 | Entity-resolution labeled ground-truth set | `data/memory/_ground_truth/*.json` (~76 files) | Done |
| 3.7 | Query eval set generation | `scripts/batch1_eval_set.py` | Done — not yet re-verified against the fixes below |

### Remediation tasks — all resolved except 3.14

| # | Task | Status |
|---|---|---|
| 3.8 | **Ingestion-source gap.** `scripts/sync_ingestion_source.py` syncs every `"clean"`-tier document from `data/memory/` into `data/documents/` (what `ingest_directory()` actually reads), using each document's ground-truth `rendering` tag to exclude scanned/handwritten. | **Done.** 96 documents now in the real ingestion source (was ~38); 22 non-clean docs correctly excluded. |
| 3.9 | **`case_index.csv`/`case_management_export.csv` FIR-number mismatch.** Root cause confirmed: `build_case_index.py`'s `NEW_CASES` list hardcoded a stale planned numbering, never reconciled with the real render output. Fixed at the source in both `build_case_index.py` and `build_structured_records.py`; also replaced the empty `linked_doc_ids`/`structured_record_ids` columns with auto-discovery from what's actually on disk (surfaced a `recovery_memos/` evidence type that wasn't linked to any case before). | **Done**, verified — every case row links to real files. |
| 3.10 | **Rendering-style cleanup.** All 14 remaining `"scanned"`-tagged documents converted to `"handwritten"` (`scripts/convert_scanned_to_handwritten.py`), consolidating the deferred-OCR bucket into one style. Backups in `data/_backup_pre_scanned_to_handwritten/`. | **Done**, verified visually. |
| 3.11 | **Bilingual balance.** Recounted post-3.8: 96 total documents, 9 English (real scraped), 87 Urdu (synthetic) — not the "37 English" initially assumed; the corpus was always Urdu-dominant once Batch-1 is actually ingested. | **Done** — no further action needed, ratio is already Urdu-first as intended. |
| 3.12 | **CNIC-consistency audit.** Confirmed root cause: `build_entity_roster.py` deliberately replaced the CASE-DRY-001 entities' real-looking CNICs (e.g. `61101-...`, a genuine Islamabad-area prefix) with clearly-synthetic `00000-...` ones for safety, but the already-rendered PDFs/ground truth were never regenerated to match. Fixed ground truth + generator source for P-DRY-003 and P-DRY-004; re-rendered `FIR-2026-ARMS-001` + both witness statements. Also cleared an inaccurate `cnic_shown_in` claim for P-DRY-001 (no `accused_cnic` field exists anywhere in the schema). | **Done**, verified visually. |
| 3.13 | **Bidi/numeral rendering defects.** Root cause was much broader than the one reported case: the raster pipeline (`_hb_raster`/`_shape_line` in `batch1_render.py`) shaped each field as one whole HarfBuzz buffer, and `guess_segment_properties()` auto-detecting RTL direction whenever Arabic script is present caused HarfBuzz to reverse the *entire* buffer — not just Arabic letters but embedded Latin/digit runs (`13`→`31`, `1965`→`5691`, `14-06-2026`→`6202-60-41`). Fixed with per-run shaping (Arabic runs shaped RTL for correct glyph joining, everything else LTR, concatenated in source order) for **structured fields** (dates, CNICs, sections — confirmed correct across all 20 re-rendered documents). Deliberately reverted narrative-text shaping to the original whole-buffer approach, since per-run splitting broke multi-word sentence order (worse than the narrower numeral-reversal defect it would fix) — narrative-embedded raw digits (rare; most narratives spell dates in words) remain a known, accepted low-priority residual, since these documents are excluded from POC ingestion regardless. Same category of bug also existed in the reportlab clean-tier pipeline (`render_clean_pdf`) for the 2 PECA-referencing category's sections field — fixed for `CYBER-005`/`CYBER-006` via a targeted override; `HAR-018` resisted every fix attempt (the string is provably correct at the call site, byte-verified, yet renders wrong — appears to be a non-deterministic or unidentified reportlab quirk, reproduced and un-reproduced across many otherwise-identical test permutations). | **Mostly done.** `HAR-018`'s sections field is the one known, accepted, low-severity residual defect (one field, one document) — not resolved, not blocking anything. |
| 3.14 | Update `data/memory/README.md` and `data/dataset_manifest.csv` — both stale, describing only the original 40-document batch. | **Not yet done.** |

---

## Phase 4 — Apache AGE Graph, Entity Extraction, Resolution, Versioning

| # | Task | Files | Depends on |
|---|---|---|---|
| 4.1 | Swap the Postgres image for an AGE-enabled one in `docker-compose.yml`; migration to `CREATE EXTENSION age; CREATE GRAPH evidence_graph;` | `docker-compose.yml`, `migrations/005_age_graph.sql` | Phase 1 |
| 4.2 | Document the graph schema (node/edge types) as `docs/graph_schema.md` — AGE has no schema enforcement, so this is enforced at the app layer | new doc | 4.1 |
| 4.3 | `src/extraction/structured_fields.py` — regex-only extractors for CNIC/phone/plate/FIR#/dates, never LLM | new file | — |
| 4.4 | **Document-level metadata classification** — doc-type classification (FIR/diary/challan/statement) via a Qwen3-14B pass, regex-validated date extraction. Separate task from 4.3 (structured fields) and 4.5 (entity NER) — this classifies the document, not entities within it. `case_id` ownership is never inferred here; it always comes from Phase 1.6's ingestion-time attachment | new file | Phase 0 |
| 4.5 | `src/extraction/ner.py` — generic entity NER using Phase 2.2's chosen tokenizer; low-confidence span escalation to **Qwen3-14B** (confirmed — reuses the already-loaded reasoning instance rather than a dedicated NER model; revisit only if accuracy proves insufficient in Phase 3 testing) | new file | 2.2, Phase 0 |
| 4.6 | `src/extraction/domain_entities.py` — few-shot **Qwen3-14B** extraction for vehicle/weapon/gang/alias entities and relations | new file | Phase 0 |
| 4.7 | `src/graph/age_client.py` — thin wrapper issuing parameterized Cypher via AGE's `cypher()` SQL function over the existing asyncpg connection | new file | 4.1 |
| 4.8 | `src/graph/entity_resolution.py` — CNIC-first/name-fallback confidence tiers, medium-confidence adjudication via **Qwen3-14B**, validated against Phase 3.6's labeled ground truth | new file | 4.3, 4.7, 3.6, Phase 0 |
| 4.9 | **Graph versioning** — append-only edge writes with `as_of`/`superseded_by`, plus a locked/verified state on timeline events (`OCCURRED_ON` edges), with a minimal investigator lock/unlock action (API + UI toggle), matching SOW Module 6's "adjust, annotate, and lock as verified." This is a write-layer primitive every subsequent graph-writer (4.8, and Phase 8.2's conflict detection) goes through | new `src/graph/versioning.py` | 4.7 |
| 4.10 | Wire 4.3–4.9 into `ingestion/service.py`'s `ingest_file()`, `case_id`-scoped | `src/ingestion/service.py` | Phase 1.6, 4.9 |
| 4.11 | Entity-resolution review queue (API + admin-frontend), reporting precision/recall against Phase 3.6's ground truth | `src/api/admin.py`, `admin-frontend/src/pages/*` | 4.8 |

---

## Phase 5 — Case-Scoped Routing & Graph Retrieval

| # | Task | Files | Depends on |
|---|---|---|---|
| 5.1 | Extend `router.txt`/`router.py`: case-scope classification (default within-case vs. explicit cross-case) + a `GRAPH` route | `prompts/router.txt`, `src/pipeline/router.py` | Phase 4 |
| 5.2 | `src/retrieval/graph_retriever.py` — case_id-filtered Cypher traversal via `age_client.py`, capped 2-3 hops, fetches source-chunk provenance per hop | new file | 4.7 |
| 5.3 | Extend `orchestrator.py`'s retry loop to dispatch the `GRAPH` branch and the combined graph+hybrid branch; cross-case fuse/eval path kept structurally separate | `src/pipeline/orchestrator.py` | 5.1, 5.2 |
| 5.4 | **Cross-case aggregate queries (`XAGG`)** — aggregate query mode over case/graph metadata (trends, hotspots), alongside the cross-case graph-traversal branch in 5.3; router signal added in 5.1 | `orchestrator.py`, `router.py` | 5.3 |
| 5.5 | Bilingual output, backend: target-language parameter on generation, sourced from the existing `preferred_language` user-profile field | `prompts/*.txt`, `orchestrator.py` | — |
| 5.6 | **Bilingual interface, frontend** — language toggle + dual-language UI chrome in `frontend/` (and `admin-frontend/` where relevant), wired to the same `preferred_language` setting driving 5.5 | `frontend/src/*` | 5.5 |
| 5.7 | **Guarded web search** — domain allowlist + relevance/safety filtering on top of the existing WEB route (Tavily/Gemini fallback in `orchestrator.py`), visual/structural separation of web citations from case-evidence citations in the response envelope, and a full route-disable flag for air-gap mode | `src/retrieval/web_search.py`, `orchestrator.py` | — |

---

## Phase 6 — Verifier Agent

| # | Task | Files | Depends on |
|---|---|---|---|
| 6.1 | `prompts/verifier.txt` — claim-vs-source-chunk grounding judgment, JSON schema like `evaluator.txt` | new file | Phase 0 |
| 6.2 | `src/pipeline/verifier.py` — `verify_grounding(answer, cited_chunks, case_id)`, modeled on `evaluator.py`, calls Qwen3-14B | new file | 6.1 |
| 6.3 | Wire into `orchestrator.py` between response generation and history-save; regenerate via the existing retry loop or emit an explicit abstention on failure. Also verifies web-sourced claims from Phase 5.7's guarded route, not just case-evidence claims | `src/pipeline/orchestrator.py` | 6.2, 5.3 |
| 6.4 | Track verifier pass/fail rate in `pipeline_logger`/`PipelineRun`, surfaced in admin dashboard | `src/database/pipeline_logger.py` | 6.3 |

---

## Phase 7 — Security & Access Control

| # | Task | Files | Depends on |
|---|---|---|---|
| 7.1 | Migration: replace/extend `User.is_admin` with a `role` enum (investigator/supervisor/station-admin/platform-admin); update JWT claims | `migrations/006_rbac.sql`, `src/auth/jwt.py` | — |
| 7.2 | `case_assignments` table (case_id, user_id, role) anchoring ABAC to the Case's IO field | migration | Phase 1, 7.1 |
| 7.3 | Postgres RLS policies on `cases`, `documents`, and AGE's underlying tables, keyed on `case_id` via a session variable | migration | 7.2, Phase 4 |
| 7.4 | Audit every retrieval layer (vector/BM25/SQL/graph) against "filter before ranking" | `orchestrator.py`, retrieval modules | 7.3 |
| 7.5 | Append-only audit log table (query/evidence-access/admin-action/graph-write) | migration, `src/database/models.py` | — |
| 7.6 | **Encryption** — full-disk/volume encryption on the GPU server and Postgres data directory; TLS on every internal service-to-service call | infra | — |
| 7.7 | **Secure model serving** — vLLM endpoints (Phase 0) bound to localhost or an internal-only network segment, never exposed beyond the app server | infra | Phase 0 |
| 7.8 | **Secrets management** — env-based for the POC, with a documented upgrade path to a self-hosted secrets store (e.g. Vault) before any multi-station rollout | `.env`, docs | — |
| 7.9 | **Zero-trust service auth** — authenticate every internal call (Postgres/AGE, Chroma, vLLM endpoints), not just the perimeter | infra | 7.6, 7.7 |
| 7.10 | **Policy Agent** — no build task. One-line note in the architecture/security docs marking it as an intentionally deferred future-phase exploration (§10.2 of the architecture report), so it reads as a decision, not a gap | docs only | — |

---

## Phase 8 — Observability Extensions

| # | Task | Files | Depends on |
|---|---|---|---|
| 8.1 | Entity-resolution precision/recall dashboard, sourced from Phase 3.6's ground truth | admin API + `admin-frontend` | 4.11 |
| 8.2 | `src/graph/conflict_detection.py` — writes `CONFLICTS_WITH` edges through Phase 4.9's versioning primitive | new file | 4.9 |
| 8.3 | Chain-of-custody/audit log viewer in admin-frontend | `admin-frontend/src/pages/*` | 7.5 |

---

## Phase 9 — Deployment, Serving & Evaluation

| # | Task | Depends on |
|---|---|---|
| 9.1 | `docker-compose.yml`: vLLM service(s) for Qwen3-14B + generation model; AGE-enabled Postgres image — no separate graph-DB container | Phase 0, 4 |
| 9.2 | GPU load test: Qwen3-14B + generation model + e5 embeddings + reranker concurrently on the 24GB card | Phase 0 |
| 9.3 | **Keyword-backend checkpoint** — run the Urdu-aware `tsvector` analyzer (Phase 2) against Phase 3's synthetic eval set; explicitly confirm quality/scale is adequate before locking it in for the POC, rather than assuming. Document OpenSearch as the fallback path if it isn't | Phase 2, 3.7 |
| 9.4 | Eval query set (Phase 3.7, ~140 queries) run against the finalized stack; lock go/no-go numbers before real-data cutover | 3.7, 9.3 |
| 9.5 | Air-gap dry run: outbound disabled, confirm Postgres+AGE / Chroma / vLLM still run; guarded web search (5.7) fully disabled in this mode | all prior phases |

---

## Execution order

Phase 0 → Phase 2 → (0.8 re-ingest) → Phase 1 → Phase 3 → Phase 4 → Phase 5 → Phase 6 → Phase 7 → Phase 8 → Phase 9.

Phase 1 has no dependency on Phase 2 and can run in parallel if work is split across more than one person.

## Explicitly out of scope for this build

OCR / scanned-handwritten documents, media evidence (audio/video/image), SOW Module 8 (Collaboration), the specialized multi-agent architecture split, 50-100-user growth-stage hardware, and the Policy Agent (Phase 7.10) — all deferred by design per the architecture report, not gaps in this plan.
