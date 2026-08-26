# Phase 3B — Sub-Agent Readiness Audit

**Date:** 2026-08-25 · **Branch:** `agent-harness` @ `c96dead`
**Status:** READ-ONLY AUDIT COMPLETE — no code, prompts, tests, graph, Postgres, or Chroma modified.

---

## 1. Executive Summary

Phase 3A's foundation repairs (T1, T2, C1, CITES) **all hold on real data**, verified
directly rather than assumed. The `"entry None"` bug and the missing-description
placeholder are genuinely dead code on the current corpus, and XNETWORK is unblocked.

Two categories of finding emerged:

**a) The 3A repairs surfaced a second-order problem they did not cause.** T1 fixed
"Incident has no description." It did not make descriptions *per-event*. Every one of
the 73 incidents now carries 3–9 timeline events that each repeat the same ~1,347-char
narrative, differing only by a ~20-character suffix. This is the single highest-value
finding in the audit and it was **not** on the pre-existing findings list.

**b) A projection defect adjacent to T2, missed in earlier passes.** 65 `position`
OCCURRED_ON edges carry `detail = ""`, rendering as dated timeline events with zero
content, plus 9 where the detail is a person's *role* mis-dated to the incident date.
Same root file as T2, same `or ""` idiom.

Everything else divides cleanly: **Data Quality is materially clean** (all four suspected
defects are either correct-by-design or genuinely repaired by 3A), while **Cross-Case
Linkage carries the remaining real risk** (C3 HIGH, C2/C4 MEDIUM).

**Corrections to my own earlier reporting are recorded in §14.** Three figures I
previously stated were wrong and are corrected here from direct measurement.

---

## 2. Current Dataset / Graph State (measured, not assumed)

| Metric | Value |
|---|---|
| Cases / Incidents | 73 / 73 |
| **Incident.description populated** | **73 / 73** (verbatim `narrative_text`) |
| Description length | min 359 · **avg 1,347** · max 2,070 chars |
| OCCURRED_ON | **568** (none superseded — see §14 correction) |
| `detail == "entry None"` | **0** (was 188) |
| Valid `entry N` | 133 |
| OCCURRED_ON per Incident | min 3 · **avg 5.9** · max 9 |
| SAME_AS | 1,248 → **pending 1,224** / confirmed 19 / rejected 5 |
| Pending tier split | **`flagged_unverified` 1,204 / `human_review` 20** (98.4% weakest) |
| **Confirmed SAME_AS crossing a case boundary** | **0** |
| Persons spanning >1 case | **4** |
| ASSOCIATED_WITH | 248 total, **19 cross-case** |
| CITES / CROSS_VERSION_OF / CONFLICTS_WITH | 9 / 0 / 0 |
| `conflicts_checked_at` set | **0 / 73** |
| Chroma `muhafiz_kb` / community reports | 823 / **19** |
| Community reports spanning >1 case | **2 of 19** |
| Reference data | 21 rows, **all `synthetic`**; **0** covering CNSA/Arms |
| Cases whose statutes have no reference coverage | **39 / 73 (53%)** |

---

## 3. Sub-Agent Architecture Map

| Agent | File | Entry | Consumes | Reachable from live orchestrator? |
|---|---|---|---|---|
| Timeline Building | `harness/agents/timeline_building.py` | `timeline_building()` | `scoped_cypher` → Incident/OCCURRED_ON/Date; `gateway.get_case()` | **No** |
| Case Summarization | `harness/agents/case_summarization.py` | `case_summarization()` | RAG tool + GRAPH tool | **No** (called only by Report Drafting `:475`) |
| Report Drafting | `harness/agents/report_drafting.py` | `report_drafting()` | Case Summarization output + `file_structurer` + builders | **No** |
| Investigative Analysis | `harness/agents/investigative_analysis.py` | `investigative_analysis()` | RAG + GRAPH + SQL tools | **No** |
| Cross-Case Linkage | `harness/agents/cross_case_linkage.py` | `cross_case_linkage()` | XGRAPH + XNETWORK tools | **No** |
| Data Quality | `harness/agents/data_quality.py` | `data_quality()` | `scoped_cypher`, Chroma metadata, Postgres | **No** |

**Shared with the live orchestrator** (fixes here affect live traffic):
`graph_retriever.py` (`orchestrator.py:32`), `verifier.py` (`:25`), `vector_store.py`
(`:27`), `sql_extractor.py` (`:22`), `file_structurer` + `generation/*` (`:42-45`).

**Harness-only** (free to fix): all six agents, `harness/tools/*`, `validation.py`,
`case_scope.py`, and — importantly — **`xnetwork.py`** (the orchestrator has its own
cross-case-network route and does not import `run_network_query`).

---

## 4. Data Contracts — mismatches that matter

| Contract | Agent expects | Dataset provides | Severity |
|---|---|---|---|
| `Incident.description` per event | distinguishing per-event text | one narrative shared by 3–9 events | **HIGH** |
| `OCCURRED_ON.detail` | present or absent | present, absent, **or `""`** (65 edges) | **HIGH** |
| `position` detail semantics | a status event | sometimes a person's *role* | **MEDIUM** |
| XNETWORK `case_ids` | the link's own community footprint | **union across all top-k** | **HIGH** |
| Cross-case identity | confirmed SAME_AS | **0 confirmed cross boundary**; 1,224 pending guesses | **MEDIUM** |
| `police_reference_data` | authoritative penal-code law | 21 rows, all `synthetic`, 0 covering 53% of cases | **MEDIUM** |
| Jurisdiction names | resolvable EN/UR | no English district resolves | **MEDIUM** |
| `Incident` reachable by graph traversal | seed or hop target | **neither** — not in `_SEED_LABELS`, `_HOP_EDGE_TYPE` is ASSOCIATED_WITH only | **MEDIUM** |
| `conflicts_checked_at` | set after detection | 0/73 | **NO ISSUE** (guard handles) |

---

## 5. Timeline Building Audit

**Verdict: the 3A fixes hold; two new defects found.**

| ID | Finding | Sev | Evidence | Records | Sys/Iso | Fix layer |
|---|---|---|---|---|---|---|
| TB-1 | **Every event repeats the entire FIR narrative.** `base_description` is the full `Incident.description`; all 73 incidents have 3–9 OCCURRED_ON edges. Distinguishing content is a ~20-char suffix. | **HIGH** | `:386, :394`; avg 1,347 chars × avg 5.9 events | **73/73** | Systemic | projection (per-event text) or agent-logic (elide repeated base) |
| TB-2 | **`position` edges render as contentless dated events.** `detail = ""` is falsy → renders `…narrative… — position` with no content. | **HIGH** | 65 edges measured; source `structured_projection.py` `or ""` | **65 edges** | Systemic | projection (skip blank rows) |
| TB-3 | **`position` detail is sometimes a person's ROLE, not an event** — `ملزم`, `مدعیہ`, `تفتیشی افسر`, `نامزد ملزم`, dated to the incident date. | **MEDIUM** | 9 edges measured | 9 edges | Systemic | projection |
| TB-4 | T1 fix reaches it; placeholder is dead code | **NO ISSUE** | `MATCH (i:Incident) WHERE i.description IS NULL` = **0** | 0/73 | — | none |
| TB-5 | Absent `detail` handled correctly (truthiness) | **NO ISSUE** | `:394`; no `"entry None"` reachable | — | — | none |
| TB-6 | Temporal ordering correct | **NO ISSUE** | `:335-349`; all dates parse ISO | 73/73 | — | none |
| TB-7 | **Conflict guard refuses a false all-clear** — `conflicts_checked_at` 0/73 → all events UNKNOWN, PARTIAL + caveat, never "0 conflicts found" | **NO ISSUE — guard works** | `:406-411, :547-551` | 73/73 protected | Systemic (guard) | none |
| TB-8 | `fir-97-26`'s missing victim edge does not corrupt the timeline | **NO ISSUE** | Timeline reads OCCURRED_ON, not INVOLVED_IN | 1 isolated | Isolated | none |

---

## 6. Cross-Case Linkage Audit

**Verdict: C1 restore genuinely unblocked XNETWORK; C3 is the remaining HIGH.**

| ID | Finding | Sev | Evidence | Records | Sys/Iso | Fix layer |
|---|---|---|---|---|---|---|
| CCL-C3 | **Single-case clusters presented as cross-case links.** `xnetwork.py:105` flattens `case_ids` into a **union across all top-k results**, discarding per-community attribution; `_xnetwork_links` then stamps that union onto every link with `is_unconfirmed=False`. | **HIGH** | `xnetwork.py:105`; `cross_case_linkage.py:505-521`; **17 of 19 communities single-case** | 17/19 (89%) | Systemic | **retrieval** (`xnetwork.py` return per-result `case_ids`) |
| CCL-C2 | **XGRAPH misattributes aggregate footprint to one entity.** With `target_entity=None` renders "A recurring entity appears across N case(s)" from `case_ids_touched` — the footprint of ALL seeds. Unchanged post-3A. | **MEDIUM** | `:437-460` (verified verbatim) | every `target_entity=None` call | Systemic (conditional) | agent-logic |
| CCL-C4 | **Unconfirmed-link flood.** `status='pending'` with no tier floor, no cap; 1 link + 1 caveat each. | **MEDIUM** | `graph_retriever.py:552-593`; **1,224 pending, 1,204 `flagged_unverified` (98.4%)**. Fan-out measured directly (query completed after an earlier timeout): **max 64 pending edges on one Person**, mean 1.98, across **418** Persons carrying any pending edge → one query on the worst seed emits **64 links + 64 caveats** | 1,224 edges / 418 Persons | Systemic | **retrieval** (floor + cap; must preserve the 20 `human_review`) |
| CCL-C5 | Undirected pending-SAME_AS query is a latency risk (repeatedly exceeded 120s read-only) | **LOW** | `graph_retriever.py:561-567` | 1 query | Isolated | data (index on `SAME_AS.status`) |
| CCL-C1 | **XNETWORK unblocked** — `query_similar_communities` returns live results (3–5, distances 0.186–0.191) | **NO ISSUE (repaired)** | verified read-only | 19/19 | — | none |
| CCL-C6 | **Validation gate now actually runs** — was structurally dead pre-3A (0 embeddings → SKIPPED) | **NO ISSUE (repaired)** | `:713-721`, `tier="full"` | — | — | none |
| CCL-C7 | **No cross-case leakage.** Role gate enforced *before* retrieval in `run_network_query` (`xnetwork.py:67-78`) and `retrieve_graph`; `unconfirmed_links` gated on `if cross_case`. **No authorization boundary is crossed.** | **NO ISSUE** | verified; no path constructible | 0 | — | none |
| CCL-C8 | CITES (9) / CROSS_VERSION_OF (0) correctly not traversed, with written rationale | **NO ISSUE** | `graph_retriever.py:81-89` | 0 | — | none |

**Structural context worth recording:** genuine cross-case signal in this corpus is
extremely thin — **0 confirmed SAME_AS cross a case boundary**, only **4 Persons** span
>1 case, and only **19 of 248** ASSOCIATED_WITH edges are cross-case. C3/C4 matter
precisely because the real signal is small enough to be drowned by unconfirmed noise.

---

## 7. Case Summarization Audit

| ID | Finding | Sev | Evidence | Records | Sys/Iso | Fix layer |
|---|---|---|---|---|---|---|
| CS-1 | **`Incident.description` never reaches it.** `Incident` is not in `_SEED_LABELS` (Person/Vehicle/PhoneNumber/Organization/Officer) and `_HOP_EDGE_TYPE = "ASSOCIATED_WITH"` only — `INVOLVED_IN`/`PART_OF` explicitly excluded. So T1's fix does not reach this agent. **Mitigated:** RAG carries the same text (description is a verbatim copy of `narrative_text`). Impact is confined to the GRAPH-only degradation branch. | **MEDIUM** | `graph_retriever.py:128, :69-78, :211` — independently verified | GRAPH-only branch | Systemic | retrieval |
| CS-2 | **Prompt demands a Status section the corpus cannot supply** — `"e.g. open, closed, under investigation"` vs empty status in 52/73. Guard present (`"say so plainly rather than guessing"`). | **MEDIUM** | `:156-175` | **52/73** | Systemic | prompt |
| CS-3 | **Prompt names near-absent entity categories** — "vehicles, phone numbers, organizations" vs Vehicle=2, PhoneNumber=2, Organization=0 corpus-wide. | **MEDIUM** | `:161-162` | **72/73** | Systemic | prompt |
| CS-4 | Empty-field-as-fact risk bounded by three stacked guards (prompt instruction, `verify_grounding` rule 4, `validate_answer`) | **LOW** | `:170-171` | — | — | none |
| CS-5 | GRAPH-only disclosure correctly gated post-verifier | **NO ISSUE** | `:488-518` | — | — | none |

---

## 8. Investigative Analysis Audit

| ID | Finding | Sev | Evidence | Records | Sys/Iso | Fix layer |
|---|---|---|---|---|---|---|
| IA-I1 | **Synthetic reference rows presented as authoritative law.** `_to_evidence_chunk` drops `source_type`; all 21 rows are `synthetic`. Neither Verifier nor user can tell. | **MEDIUM** | `tools/sql.py:73-78`; measured `source_type=synthetic` ×21 | 21 rows | Systemic | **harness-only** (tool wrapper) |
| IA-I2 | **53% of cases have no reference coverage** — 0 rows cover CNSA 1997 / Arms Ordinance. SQL leg silently contributes nothing. | **MEDIUM** | measured **39/73** | 39/73 | Systemic | data (disclosed via `degraded_from`) |
| IA-I3 | `sql_param_extractor.txt` hardcodes old-corpus subject vocabulary **in its schema**, not just examples | **MEDIUM** | `prompts/sql_param_extractor.txt:12, :23` | all SQL queries | Systemic | prompt (**SHARED** — `orchestrator.py:22`) |
| IA-I4 | Verifier cannot catch internally-consistent-but-wrong citations | **MEDIUM** | `:466-470, :542-546` | — | Systemic | none (inherent limit) |
| IA-I5 | Honest degradation (`ABSTAINED`, `degraded_from`, verifier-rejection reset) | **NO ISSUE** | `:497-504, :549-556` | — | — | none |
| IA-I6 | GRAPH leg improved — ASSOCIATED_WITH 0→248 makes one-hop expansion functional | **NO ISSUE** | — | — | — | none |

Stale-taxonomy scan across `prompts/`: `community_summarizer.txt`, `doc_classifier.txt`,
`router.txt` reference old vocabulary **only in few-shot examples** (harmless).
**Only `sql_param_extractor.txt` embeds it in the schema** — the genuine I3.

---

## 9. Report Drafting Audit

| ID | Finding | Sev | Evidence | Records | Sys/Iso | Fix layer |
|---|---|---|---|---|---|---|
| RD-1 | **Single-synthetic-chunk regime makes the Verifier a paraphrase check.** All summary text becomes one `[Document 1]`; `valid_citation_count=1`. Upstream factual error is by construction perfectly grounded. | **MEDIUM** | `:517, :533-542` (documented at `:58-96`) | all reports | Systemic | none (composition property) |
| RD-2 | Inherits Case Summarization degradation into a durable PDF/DOCX | **MEDIUM** | `:475, :618-639` | — | Systemic | upstream (CS-1/2/3) |
| RD-3 | **No `crime_category`/`investigation_status` coupling** — verified by grep across drafting, summarization, `file_structurer`, and its prompt | **NO ISSUE** | — | 0 | — | none |
| RD-4 | `session_id` validated upfront | **NO ISSUE** | `:457-470` | — | — | none |
| RD-5 | Builders schema-generic; `_normalize_payload` coerces defensively | **NO ISSUE** | `file_structurer.py:12-80` | — | — | none |
| RD-6 | 3,000-token structuring cap vs Urdu (tokenizes poorly) | **LOW** | `:76-80` | — | Systemic | agent-logic |

---

## 10. Data Quality Audit

**Verdict: materially clean. All four suspected defects are correct-by-design or repaired.**

| ID | Finding | Sev | Evidence |
|---|---|---|---|
| DQ-1 | `Organization`=0 is **not** a dead metric — primary key is `total_entities` (sum over 7 labels); a per-label zero is reported as data, never gates readiness | **NO ISSUE** | `:231-239, :254, :463-464` |
| DQ-2 | **Embedding coverage now READY** — Chroma and Postgres both carry 73 `fir-*` case_ids, so the comparison matches. Pre-3A UNAVAILABLE is genuinely resolved | **NO ISSUE (repaired)** | `:412-430` |
| DQ-3 | `_fetch_conflict_coverage` keys on `incidents_checkable`, **not** `conflicts_found`, and carries an unconditional caveat stating it cannot distinguish "checked, none found" from "never run" — exactly the 0/73 state | **NO ISSUE** | `:372-409, :271-274` |
| DQ-4 | Vehicle/PhoneNumber sparsity produces no false positives | **NO ISSUE** | `:318-319` |
| DQ-5 | UNKNOWN (fetch raised) correctly separated from UNAVAILABLE (successful zero) | **NO ISSUE** | `:443-465` |

**My earlier "dead Organization label" flag was wrong** and is withdrawn — see §14.

---

## 11. Prompt Assumption Audit

| Assumption | Still valid? | Causes failure? | Fix layer |
|---|---|---|---|
| Status is an enum (`open/closed/under investigation`) | **No** — empty in 52/73, free-text Urdu otherwise | Steers vocabulary; guard limits harm | prompt (`case_summarization`) |
| Vehicles/phones/organizations are populated | **No** — 2/2/0 corpus-wide | Invites empty sections or confabulation | prompt (`case_summarization`) |
| Reference subjects are crime types | **No** — statutes now | Wrong extraction → empty result (disclosed) | prompt (`sql_param_extractor`, **SHARED**) |
| Community clusters span multiple cases | **No** — 2/19 | **CCL-C3** | retrieval |
| Every Incident has a description | **Yes now** (73/73) | — | none |
| Every OCCURRED_ON has detail | **No** — and `""` exists | **TB-2** | projection |
| English-resolvable jurisdiction names | **No** | Silent scope widening | retrieval (alias map) |
| Verifier rule 4 protects absence statements | **Yes** | — | none |

---

## 12. Retrieval / Graph Dependency Audit

- `Incident` unreachable by traversal (not a seed, not a hop target) → **CS-1**
- `xnetwork.py:105` union-flattening → **CCL-C3**
- `_unconfirmed_same_as_links` unbounded → **CCL-C4**
- Jurisdiction resolution: no EN↔UR↔ASCII-ID alias map; **no English district name resolves**
- CITES/CROSS_VERSION_OF deliberately not traversed, with written rationale — correct

---

## 13. Performance Baseline (read-only, no mutation)

| Operation | Latency |
|---|---|
| Incident scan (incl. warmup) | 241 ms |
| OCCURRED_ON full scan (568) | 60 ms |
| SAME_AS pending scan (1,224) | 53 ms |
| Person label scan (620) | 46 ms |
| BELONGS_TO_CASE scan (2,523) | 56 ms |
| **Single embedding call** | **966 ms** |
| Community retrieval (embed + search) | 1,127 ms |

**The embedding round-trip dominates.** ~966 ms of the 1,127 ms community retrieval is the
remote ngrok call; graph queries are ~50 ms by comparison. Any future optimization
belongs at the embedding/caching layer, not in Cypher.

**Exception:** the undirected pending-SAME_AS aggregate repeatedly exceeded 120 s
(CCL-C5) — the one genuinely slow graph shape, and one the codebase already documents as
unreliable at scale.

---

## 14. Corrections to Prior Reporting

Recorded because the remediation discipline requires it:

1. **"SAME_AS 340"** (my Phase 3B brief) → actual **1,248** total, **1,224 pending**.
2. **Pending tier split** — I said 1,215/33; a sub-agent said 1,193/7. **Both wrong.**
   Direct measurement: **1,204 `flagged_unverified` / 20 `human_review`.**
3. **"432 live OCCURRED_ON after `superseded_by` filter"** (sub-agent claim) — **wrong**.
   Filtering `superseded_by IS NULL` still returns **568**; nothing is superseded.
4. **"`data_quality` Organization is a dead metric"** (my earlier finding) — **withdrawn.**
   The primary key is `total_entities`; a per-label zero never gates readiness.
5. **Description length** — I previously implied ~800 chars; actual **avg 1,347, max 2,070**.

---

## 15. Findings by Severity

**HIGH (3)**
- **TB-1** — every timeline event repeats the full ~1,347-char narrative (73/73)
- **TB-2** — 65 contentless `position` events
- **CCL-C3** — 17/19 single-case clusters presented as cross-case

**MEDIUM (10)** — TB-3, CCL-C2, CCL-C4, CS-1, CS-2, CS-3, IA-I1, IA-I2, IA-I3, RD-1/RD-2

**LOW (3)** — CCL-C5, CS-4, RD-6

**NO ISSUE / already protected (17)** — TB-4…TB-8, CCL-C1/C6/C7/C8, CS-5, IA-I5/I6, RD-3/4/5, DQ-1…DQ-5

---

## 16. Systemic vs Isolated

**Systemic** (fix at the layer, not the record): TB-1 (73/73), TB-2 (65 edges),
CCL-C3 (17/19), CCL-C4 (1,224), CS-2 (52/73), CS-3 (72/73), IA-I2 (39/73), IA-I3.

**Isolated** (do NOT generalize): `fir-97-26`'s victim edge (n=1), CCL-C5.

**Withdrawn**: `data_quality` Organization label.

---

## 17. Recommended Fix Order

**3C-a — Projection (harness-only blast radius, highest value)**
1. **TB-2 / TB-3** — stop writing `detail = ""` for blank `position`; skip blank rows.
   Same file and idiom as T2, so the pattern is established.
2. **TB-1** — decide whether Incident description belongs on every event. Likely
   agent-logic (render base once, per-event suffix after) rather than projection.

**3C-b — Harness-only retrieval/agent fixes (zero live risk)**
3. **CCL-C3** — `xnetwork.py` return per-result `case_ids`. `xnetwork.py` is
   **harness-only**, so this is low blast radius.
4. **CCL-C2** — suppress singular framing when `target_entity` is None.
5. **IA-I1** — preserve `source_type` in `tools/sql.py`.
6. **CS-2 / CS-3** — prompt vocabulary.

**3C-c — Shared modules (one at a time, orchestrator verification each)**
7. **CCL-C4** — tier floor + cap in `_unconfirmed_same_as_links` (**SHARED**). Must
   preserve the 20 `human_review` links; cap only `flagged_unverified`.
8. **Jurisdiction alias map** (**SHARED**).
9. **IA-I3** — `sql_param_extractor.txt` (**SHARED**).
10. **CS-1** — Incident reachability in traversal (**SHARED**, highest blast radius).

---

## 18. What NOT to Fix

- **`fir-97-26` victim edge** — n=1. Do not build normalization for one record.
- **Anything in Data Quality** — changing `_ENTITY_LABELS` or the conflict primary key
  would *introduce* defects.
- **C3 by regenerating communities** — that is capability tuning, not drift repair.
- **CCL-C4 with a blunt threshold** — would suppress the 20 genuine `human_review` links.
- **RD-1** — a composition property, not a patchable bug.
- **Out of scope, unchanged:** `cross_silo_projection.py:602`, `muhafiz_records.py:171`.
- **Out of scope, unchanged:** query decomposition, `section_code` classification.

---

## 19. Phase 3B → 3C Handoff

**Ready to fix, harness-only:** TB-1, TB-2, TB-3, CCL-C3, CCL-C2, IA-I1, CS-2, CS-3
**Needs shared-module care:** CCL-C4, jurisdiction alias, IA-I3, CS-1
**Do not fix:** §18

**Phase 4 (mandatory) fixture implications** — every finding above passes the current
~1,600 green tests, because fixtures are synthetic (`CASE-001`, `P-1`/`V-1`) and tools
are stubbed above the data layer. Minimum fixtures: a real-shape timeline row
(`fir-117-26`, multi-event, `detail=""`), a single-case XNETWORK result asserting it is
not rendered as cross-case, a `source_type="synthetic"` SQL chunk, and a
corpus-consistency test (Chroma community count == Postgres `community_reports`).

**Nothing was modified in this phase.** `git status` unchanged; HEAD `c96dead`.
