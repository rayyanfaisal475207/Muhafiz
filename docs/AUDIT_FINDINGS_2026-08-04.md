# Audit — Graph + Hybrid Retrieval Stack, EN/UR (2026-08-04)

**Method:** Full stack was actually brought up and driven live (Docker Desktop →
`muhafiz-postgres` w/ Apache AGE → backend on `:8000` → Groq cloud-fallback LLM,
ngrok-tunneled Qwen3-14B/e5/bge-reranker as the local-first path), not just read
statically. Live dataset: 101 documents / 43 cases in Postgres, 270 chunks in
Chroma, `evidence_graph` AGE graph with 20 Case / 15 Person / 124 Address / 7
Incident / 4 Date / 3 Organization / 2 Weapon nodes. Three fresh test accounts
were created (`audit_investigator`, `audit_supervisor`, `audit_stationadmin`)
and driven through the real `/api/chat` SSE pipeline with real queries in
English, Urdu script, and Roman-Urdu. Static code audit covered every file in
scope, cross-checked against the prior `docs/AUDIT_FINDINGS_2026-07-23.md` to
avoid re-reporting fixed issues, and grounded against real sample documents /
`data/memory/_ground_truth/*.json` where relevant.

Legend: 🔴 Critical · 🟠 High (correctness bug) · 🟡 Medium (quality gap) ·
🟢 Low / defense-in-depth · 🔒 security-relevant

---

## Executive summary

| Query type × language | EN | UR (script) | Roman-Urdu |
|---|---|---|---|
| Single-fact lookup (within-case) | ✅ pass (smoke test) | ✅ pass (live-tested) | not tested |
| Cross-case entity relationship | ⚠️ blocked by router gap below | ⚠️ blocked | ⚠️ blocked |
| Recurring-entity aggregate (XAGG Vehicle/Person) | 🔴 **fails live** — router never selects XAGG, and Vehicle/PhoneNumber nodes don't exist in the graph anyway | 🔴 fails (router) | 🔴 fails (router) |
| Relational aggregate (XAGG station/status/category) | ⚠️ router-gated (see below) | 🔴 **fails live** — router never reaches XAGG | 🔴 **fails live** — same, plus zero Roman-Urdu keyword coverage even if it did |
| Case listing/enumeration | not live-tested (static: `_LIST_ALL_KEYWORDS` OK for EN/UR script, none for Roman-Urdu) | same | same |
| Network/timeline reasoning | not live-tested | not live-tested | not live-tested |
| Within-case inconsistency flagging | static: verified correct, language-agnostic | same | same |
| Fuzzy/partial entity refs | static: CNIC/plate digit-normalization correct; Roman-Urdu↔Urdu-script name bridging absent | — | — |
| RBAC / role gate | static + live: correctly fails closed | — | — |

**Worst cells, by a wide margin: every cross-case query type (XAGG, XGRAPH), in
every language, live-tested this session.** The root cause is not primarily a
language gap — it's upstream of that. See Finding G-1.

---

## 🔴 G-1 — CRITICAL: the router almost never selects XAGG/XGRAPH in the live deployment, in any language — cross-case features are effectively unreachable right now

**File:** `src/pipeline/router.py:53-144`, `prompts/router.txt`, `src/pipeline/json_extract.py:107-` (`call_llm_json`)

**What was tested (live, this session, via real `/api/chat` calls):**
1. EN, supervisor: *"How many recurring vehicles have appeared across multiple cases?"* → routed **RAG**, failed after 2 retries ("no sufficient evidence").
2. EN, supervisor, **the router prompt's own literal few-shot example**: *"Which police stations have the most open theft cases?"* → routed **RAG**, failed after 2 retries.
3. UR script, supervisor: *"بند کیسز کی تعداد بتائیں"* (how many closed cases) → routed **RAG**, failed after 2 retries.
4. Roman-Urdu, supervisor: *"band cases kitne hain"* → routed **RAG**; worse, `title_generation`'s own LLM call misread it as **Hindi** ("band" = musical band), unrelated to the routing failure but symptomatic of the same underlying model-quality issue.

Backend log for this session: `grep -c "XAGG" backend_audit_run.log` → **0**. `grep -c "XGRAPH"` → **0**. Every cross-case-shaped query this session, in every language, defaulted to RAG.

**Root cause, traced in the log and code:** `call_llm_json` (used by `router.route_query`, `src/pipeline/json_extract.py:198`) logged `invalid JSON on attempt 1/3` for these exact calls — the local reasoning model (Qwen3-14B via the ngrok-tunneled `LOCAL_LLM_URL`) frequently answers the router prompt **conversationally instead of with the required JSON** (e.g., for "band cases kitne hain" it produced a multi-paragraph explanation of what "band" could mean, in Hindi). By design (`json_extract.py:126-138`, `router.py:74-84`, explicitly documented in code comments as a deliberate choice to avoid burning Groq's free-tier quota on every content-quality retry), this failure mode is **not escalated to cloud** — it only gets up to 3 local-only correction attempts, and if `result` still doesn't parse, `router.py:134-144`'s `except` block **silently defaults to `route: "RAG", confidence: "low"`**. `router.py`'s own code comments confirm this is a known, previously-observed failure mode ("Qwen3 sometimes ignores the 'respond with ONLY JSON' instruction entirely and answers conversationally instead") that already received mitigation attempts (increased `max_attempts` from 2→3, explicit schema hints) — but live testing this session shows those mitigations are **not sufficient**: every cross-case query attempted still ended up on the RAG default.

For case #2 above (router's own few-shot example, verbatim), `call_llm_json` did eventually produce syntactically valid JSON after correction (no `"Router failed to parse JSON"` error was logged), meaning the model's **final, corrected** answer was a genuine (not just malformed) classification of `RAG` — a real comprehension failure on top of the more frequent formatting failure, since XAGG's own calibration prompt should have anchored this exact phrasing.

**Impact:** This is upstream of, and more severe than, every other router/XAGG finding in this report (the Urdu/Roman-Urdu keyword gaps inside `xagg.py`, the router few-shot Urdu/Roman-Urdu coverage gaps) — those gaps only matter once a query actually reaches `xagg.py`'s keyword matcher, which in live testing this session it never did, in any language. The RAG fallback isn't just "wrong route" — it silently retries 2x, burns 3 full retrieval+rerank+evaluate cycles, and returns a generic "insufficient evidence" abstention, with no signal to the user or any log-visible alarm that a structurally-supported query type (XAGG) was never attempted. A supervisor asking the platform's flagship cross-case aggregation question gets a wrong "I don't know" instead of the intended structured answer, indistinguishable from a case where the data genuinely doesn't exist.

**Severity:** 🔴 Critical / correctness bug. Not security-relevant on its own (fails closed to "no answer" rather than leaking data), but it defeats the primary advertised capability of the XAGG/XGRAPH routes across the board, independent of the language-specific findings below.

---

## Findings by component

### A. Apache AGE graph layer (`src/graph/`)

| # | Sev | Finding | File:line |
|---|---|---|---|
| A-1 | 🟠 | **Phone-number entities are never written to the graph — the entire PhoneNumber node type is dead code.** `src/ingestion/service.py:140` computes `fields = sf.extract_all(full_text)` (includes phones) but never uses `fields` again anywhere in the function (verified by full-file grep). Neither `ner.py` nor `domain_entities.py` ever emit a `"phone"`-typed mention, and `_RESOLVABLE_MENTION_TYPES` doesn't include `"phone"`. **Live-confirmed**: direct Cypher query of the live graph (`MATCH (n) RETURN labels(n)[0], count(*)`) returns zero `PhoneNumber` nodes and — additionally, not flagged by the static audit — **zero `Vehicle` nodes either**, despite `domain_entities.py` listing `"vehicle"` as an entity type and XAGG advertising a "Vehicle recurring-entity" path. Any phone-number or vehicle-based graph/XGRAPH/XAGG query returns empty results, in any language, purely because no such nodes exist — this is upstream of and independent of Finding G-1. | `src/ingestion/service.py:140`, `src/graph/entity_resolution.py:51`, `src/retrieval/graph_retriever.py:75,88` |
| A-2 | 🟡 | **No Roman-Urdu ↔ Urdu-script name bridging in entity resolution.** `entity_resolution._name_similarity()` runs `rapidfuzz.fuzz.token_sort_ratio` over `normalize_urdu()`-normalized strings, but `normalize_urdu()` only unifies Arabic-script letter/digit variants — no Latin↔Arabic transliteration. "ظفر اقبال" vs. "Zafar Iqbal" scores near-zero and never merges (falls below `REVIEW_FLOOR=0.40`), a silent under-merge. `is_roman_urdu` (`src/ingestion/script_detector.py`) is used only for retrieval-side query variants (`cross_script_variant.py`), never in resolution's candidate generation. | `src/graph/entity_resolution.py:124-127` |
| A-3 | 🟢 | Latent, currently-unreachable Cypher-injection defense-in-depth gap: `versioning._match_clause`/`_prefixed_match_clause` interpolate dict **keys** into Cypher text with no identifier validation (unlike the sibling `_build_set_clause`, which does validate). Every current caller passes hardcoded literal keys — not exploitable today, but inconsistent with the pattern used elsewhere. | `src/graph/versioning.py:50-60,287-291` |
| — | ✅ | Cypher parameterization (values, not keys) verified safe everywhere traced. RLS/case-scope arming (`scoped_cypher`) verified correctly enforced on every within-case call site; cross-case sites correctly role-gated before any query executes. CNIC-first hard-mismatch block, versioning's 3 documented invariants, and `conflict_detection.py`'s Urdu handling (fully LLM/substring-based, no English-specific patterns) all verified correct and unregressed from the 2026-07-23 audit. | — |

### B. Extraction pipeline (`src/extraction/`)

| # | Sev | Finding | File:line |
|---|---|---|---|
| B-1 | 🟠 | **NER misses the corpus's single most common Urdu self-introduction pattern** — "میں ⟨name⟩، رہائشی ⟨place⟩" ("I am ⟨name⟩, resident of…"), confirmed present in 11+ ground-truth documents (witness statements, complaints, missing-person reports). None of the 4 structural regexes (`_KINSHIP_RE`, `_ROLE_RE`, `_STATION_RE`, `_ORG_SUFFIX_RE`) match it, and the LLM fallback only adjudicates candidates the statistical pass already surfaced — it doesn't backstop names the pass never found. The reporting party's own name — often the most important actor in the document — can be invisible to the whole pipeline. | `src/extraction/ner.py:104-153` |
| B-2 | 🟡 | `doc_classifier.py`'s date-label regex (`_DATE_LABEL_RE`) is effectively dead against this corpus: no Urdu labels at all, and doesn't even match the English labels the corpus actually uses (`Date Reported:`, `Date Last Seen:` in `MP-2026-001.pdf`). `date_registered_confidence` is never actually `"labeled"` in practice — falls back to "first date in document order," which happens to be right in samples checked only by layout coincidence, not by the labeling logic doing its job. | `src/extraction/doc_classifier.py:35-38` |
| B-3 | 🟡 | `structured_fields.py` date regexes are numeric-only (`_DATE_ISO_RE`, `_DATE_DMY_RE`) — no Urdu spelled-out month names (جنوری/فروری/…). Confirmed live in `WITNESS-FIR-2026-BUR-007-01.pdf`: narrative "10 فروری 2026ء" is invisible to `extract_dates()`. Currently harmless where a numeric duplicate exists elsewhere in the same doc, but a document where the narrative date is the only occurrence would silently lose it. | `src/extraction/structured_fields.py:99-104` |
| B-4 | 🟡 | Inverse-direction asymmetry: English text gets **no** structural location/org signal — `_ENGLISH_NAME_RE` tags every capitalized-word-run match as `"person"` regardless of actual type (`ner.py:241`), relying entirely on the LLM fallback to retype. If that LLM call fails, `ner.py:271-273` returns candidates unchanged — an English location/org (e.g. "Kashmir Highway", "Islamabad Traffic Police") stays silently mistagged as a person. | `src/extraction/ner.py:138-144,237-241` |
| — | ⚠️ live | **Corroborating live observation**: sampling the live graph's `Person` nodes turned up `canonical_name: "خلاف قانونی کارروائی"` (literally "illegal action" — a phrase, not a name) and `canonical_name: "Golra"` (a police-station name, per `cases.police_station`) tagged as `Person` entities multiple times. This is a live instance of exactly the kind of NER mistagging B-4 describes, on the Urdu side — worth a follow-up static check the extraction audit didn't specifically target. | live graph sample |
| — | ✅ | Digit normalization (Arabic-Indic/Extended-Arabic-Indic → ASCII) verified correct and consistently applied ingestion-side and query-side; CNIC/plate cross-validation verified script-agnostic; `relationship_extraction.py` and `domain_entities.py` are pure bilingual-prompted LLM calls with no English-specific heuristics; `doc_classifier.py`'s document-*type* classification (as opposed to its date-label helper) is bilingual by design and correctly example-grounded. | — |

### C. Hybrid retrieval (`src/retrieval/`)

No correctness or security-relevant bugs found. Note: the audit brief's premise of a Postgres `tsvector`/BM25 layer is **stale** — that subsystem was already replaced with in-memory `rank_bm25` over a custom Urdu-aware tokenizer (`src/ingestion/tokenizer.py`), which sidesteps the "English tsvector config mangles Urdu" risk by construction. Verified: ingestion-side and query-side tokenization/normalization use the identical `normalize_urdu()` path (no divergence). RRF formula (`reranker.py:97,104`, `k=60`) is standard and correctly 1-indexed; degrades gracefully to pure-semantic ranking when one retrieval list is empty (relevant for the "Urdu query gets zero BM25 hits" scenario). Embedding dimension mismatches are hard-rejected at write time.

| # | Sev | Finding | File:line |
|---|---|---|---|
| C-1 | 🟡 | RRF's `year_boost` heuristic (regex `\b(20\d{2})\b` against source filename) can mis-boost documents whose filename contains a case/FIR number that looks like a year (e.g. `FIR-2026-ARMS-003.pdf`) — not a document date. | `src/retrieval/reranker.py:124-131` |
| C-2 | 🟡 | Cross-reranker matches results back to candidates by **exact document text**; if the (external, out-of-repo) reranker server ever echoes back truncated/modified text, the match silently fails and the chunk is dropped with only a warning — a latent risk that would specifically bite longer Urdu chunks first (more bytes/char than English). Could not be verified further since the reranker server itself is out-of-repo. | `src/retrieval/cross_reranker.py:59-71` |
| — | ⚠️ gap | Whether bge-reranker-v2-m3's truncation length interacts badly with Urdu's higher UTF-8 byte-to-character ratio is **unverifiable from this repository** — the reranker is an external ngrok-tunneled service with no code here. Flag for whoever owns that service, not a finding this audit can confirm or refute. | — |

### D. Query pipeline (`src/pipeline/query_rewriter.py`, `query_expander.py`, `router.py`)

See **G-1** above for the headline live finding (router JSON-compliance / classification failure). Additional static + one live-confirmed finding:

| # | Sev | Finding | File:line |
|---|---|---|---|
| D-1 | 🟠 | **Live-confirmed**: on retry, the query rewriter can emit meta-commentary instead of an actual rewritten query. Observed this session: after the evaluator rejected a search, the rewriter's "Retry query" was `'The original question — "How many recurring vehicles have appeared across multiple cases?" — is similar in intent to the previous search query, but it may be interpreted differently depending on how "recurring vehicles" are defined.'` — verbatim explanatory prose, not a search query, which was then used as the literal retrieval query for the next attempt. Unlike the rewriter's other failure modes (echo, refusal, commentary-instead-of-answer), which all have dedicated structural guards in `_sanitize_rewrite()`, there is **no post-hoc check** that a rewrite is actually a short standalone query rather than an explanation. Static audit had flagged this class of gap as "plausible, not yet confirmed live" — this session confirms it live. | `src/pipeline/query_rewriter.py:131-274` (missing guard) |
| D-2 | 🟡 | `query_expander.py`'s prompt (`prompts/query_expander.txt`) has no instruction to preserve query language/script (unlike `query_rewriter.txt`'s explicit rule 5), and all its few-shot examples are English→English. A plausible, not-yet-directly-observed risk that Urdu/Roman-Urdu queries get English-only expansion variants, an asymmetric recall benefit favoring English. | `prompts/query_expander.txt`, `src/pipeline/query_expander.py` |
| D-3 | 🟡 | Router's few-shot calibration set (`prompts/router.txt`) has only 3 Urdu-script examples and **zero Roman-Urdu examples**; XAGG specifically has **zero non-English examples at all**. This is the calibration-data root of the classification component of G-1 (as distinct from G-1's JSON-formatting component). | `prompts/router.txt` |
| — | ✅ | The 2026-07-23 audit's Phase 0.6 finding (hardcoded `provider_override="groq"` in query_rewriter.py) is confirmed fixed — no longer present. The case-scope guard (`route not in [XGRAPH,XAGG] → case_scope forced to within_case`) is unconditional and runs after route normalization, confirmed unregressed. The echo/refusal detectors are genuinely generalized (structural regex, not literal-phrase lists) per fix-chain git history. | `src/pipeline/router.py:106-107` |

### E. XAGG / XGRAPH cross-case routes (`src/pipeline/xagg.py`, `src/retrieval/graph_retriever.py`)

All Phase 7 findings from the 2026-07-23 audit (the RBAC/RLS section it called its most severe) are **confirmed fixed and unregressed**, verified both statically and live this session (every cross-case attempt by the `investigator`-role test account was blocked or never reached cross-case data — see F below).

| # | Sev | Finding | File:line |
|---|---|---|---|
| E-1 | 🟡 | **Exhaustive keyword-list audit**: all six `_XXX_KEYWORDS` constants in `xagg.py` have Urdu-**script** coverage (a prior fix) but **zero Roman-Urdu coverage**, structurally — `cross_script_variant.py` (the module that could normalize this) is retrieval-only and explicitly never wired into XAGG's dispatch. Specific gaps: `_PERSON_KEYWORDS` is missing **ملزم** ("accused" — the single most common FIR term for a named suspect); `_STATUS_KEYWORDS` is missing **زیر تفتیش** ("under investigation") in either language. Concrete failure: a Roman-Urdu query like "kitni gariyan bar bar cases mein aayi hain" matches none of the 6 lists and silently falls through to the default station/category-count branch — a plausible-looking but wrong answer, not an error. **Live testing this session shows this finding is currently moot in practice** — G-1 means these keyword lists are essentially never reached at all, in any language, so this is latent rather than actively firing right now, but it will resurface as the next-layer bug once G-1 is fixed. | `src/pipeline/xagg.py:29-45` |
| — | ✅ | Query parameterization verified safe in both `xagg.py` and `graph_retriever.py` (no user-text ever string-interpolated into Cypher). RBAC role gate verified: runs first in both `run_aggregate()` and `retrieve_graph()`'s cross-case branch, before any data access; fails closed even if audit-logging itself throws; no alias/secondary route found that reaches either function while skipping the gate. RLS `current_rls_active`/`current_cross_case` context vars verified as genuine per-request `contextvars`, armed only after the role check passes, and cannot leak across pooled-connection reuse (`SET LOCAL` is transaction-scoped and re-issued fresh every `get_session()` call). | `src/pipeline/xagg.py:123-134,155-156`; `src/retrieval/graph_retriever.py:522-535,565-566` |

### F. RBAC / case-scope enforcement (live-verified)

| Test | Result |
|---|---|
| `investigator` role, cross-case-shaped aggregate query ("recurring vehicles across multiple cases") | Routed to RAG (not XAGG) by the router itself — never reached the role gate, consistent with G-1. No PermissionError, no data leak: RAG only ever sees within-case-scoped retrieval by construction. |
| `supervisor` role, same query in EN/UR/Roman-Urdu | Same outcome — router never reaches XAGG for this role either, so the role gate (which *would* have allowed a supervisor through) was never exercised live this session. **This is itself worth flagging**: the audit could not live-verify the "supervisor CAN reach XAGG and get a correct answer" positive path at all, only the static code path — G-1 blocks it end-to-end. |
| `investigator` role, RAG-phrased attempt to get cross-case data ("List all persons named in FIR-2026-BUR-008 and check if any of them appear in other cases across the department") | ✅ No leak. Routed to RAG (case-scope defaults to within-case for any non-XGRAPH/XAGG route per D-line finding — the phrase "across the department" has no effect), retrieved only 5 chunks, evaluator correctly rejected as not satisfying the cross-case ask, abstained after 2 retries with "insufficient evidence." **Same rewriter meta-commentary bug as D-1 recurred** on retry 2 (`'To address your query effectively, we need to follow a structured approach...'` used as the literal next search query) — second live instance this session, reinforcing D-1 is a real, reproducible gap, not a one-off. |
| `investigator` role, within-case query with `case_id` for an assigned case | ✅ Passed cleanly, correctly scoped, correctly cited. |

Static audit (graph + xagg agents) independently confirms `check_case_access()` gates `/api/chat`'s `case_id` before `process_query()` runs, and that role is always server-derived from the authenticated JWT (`current_user.role`), never client-suppliable — so even the router-misclassification in G-1 cannot be leveraged to escalate privilege; it only produces wrong/missing answers, not leaked ones, based on everything traced so far.

---

## Notes on prior audit (2026-07-23) status

Re-verified, still true: Phase 4 (AGE graph/extraction/resolution/versioning) core invariants hold; Phase 5 case-scoped routing guards hold; Phase 6's RAG-retry Gemini-verifier-bypass and Phase 7's RBAC findings are fixed (confirmed via git log `25ca6de`, `aa234af` and direct code re-read, not just re-reading the doc's claims). Phase 0.6's stray `provider_override="groq"` is fixed. Nothing in this audit found a regression of a previously-fixed issue.

