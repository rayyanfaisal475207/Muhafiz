# Muhafiz — Recommended Fixes for the Remaining Gold-QA Gaps

After the four committed app-code fixes (root causes 1–4), the before/after
showed real but modest gains. This document recommends the **most promising**
fixes for what's left, grounded in the actual code. Ranked by
**impact ÷ effort**.

The remaining gaps, from the targeted re-run:
- Graph answers list case-IDs, not the person NAMES the question asks for (CR2, S3)
- Routing is non-deterministic (D1 answers "79" one run, a statute breakdown the next)
- KB questions retrieve the wrong legal chunks (CrPC content isn't surfaced)
- A few aggregates (reporting-delay, officer) have no data path

---

## Recommendation 1 — Thread entity NAMES into the graph summary (HIGH impact, MEDIUM effort)

**Problem.** CR2/S3 expected "Yes — شہزیب عرف شابی in FIRs 891/24 and 214/26; عاصم
رشید in…". The app now correctly says "Yes — the same entity recurs across 6
cases: fir-202-26, fir-214-26…" but names **case-IDs, not people**. That's why
CR2 plateaued at 0.3 and S3 at 0.0 despite the phrasing fix.

**Root cause (verified in code).** `_xgraph_summary_line()`
([cross_case_linkage.py:595](../src/pipeline/harness/agents/cross_case_linkage.py#L595))
receives only `case_ids`, `hop_count`, counts, and `chain_confidence` — **not
the entity names.** But the names ARE available: the traversal returns
`tool_result.chunks`, each with `.text` naming the person, and the tool takes a
`target_entity` name. The summary just doesn't thread them through.

**Recommended fix.**
- Extend `_xgraph_summary_line()` to accept the resolved entity name(s) — the
  recurring person's `canonical_name` from the traversal's seed/chunk data.
- Change the lead line from "the same entity recurs across N cases" to
  "**<name>** appears across N cases: …". The data model already carries
  `canonical_name` on Person nodes (entity_resolution writes it), and the
  chunks already contain it — pass it into the summary the same way `case_ids`
  is passed.
- Where multiple recurring people exist (S3 expects two), list each by name
  with their own case set, rather than one flat case-ID list.

**Why it's the top pick.** It directly converts the two lowest-scoring
reasoning questions from "found a link" to "named the people," which is what the
ground truth rewards. The names are already in memory — this is plumbing, not
new retrieval. **Est. gain: CR2/S3 from ~0.15 avg toward ~0.7+.**

---

## Recommendation 2 — Make the aggregate the DEFAULT for "how many X" totals (HIGH impact, LOW–MEDIUM effort)

**Problem.** D1 ("how many FIRs?") is non-deterministic — one run answers "79",
the next returns a per-statute breakdown. My sum-total verifier fix (#2) lets a
stated total survive, but only when generation *chooses* to state it; the
aggregate engine still sometimes computes a grouped breakdown instead of a plain
total.

**Root cause.** The router runs at `temperature=0.0`
([router.py:414](../src/pipeline/router.py#L414)) — so this is NOT a temperature
problem. The variation is the LLM classifier itself plus the aggregate-KIND
selection inside XAGG picking "group by statute" vs "count total" for the same
query. It's the aggregate *kind* that flips, not just the route.

**Recommended fix (two options, do the first):**
- **(a) Deterministic total intent.** In the XAGG kind-selection, add an
  explicit rule: a query matching "how many / total number of / count of" a
  base entity (FIRs, cases, accused) with **no grouping dimension named**
  resolves to a **plain total count**, never a group-by. This is a keyword/
  regex pre-check on the query — the same deterministic-override pattern the
  router already uses (`_deterministic_route_override`,
  [router.py:248](../src/pipeline/router.py#L248)) — applied to aggregate-kind
  selection. Deterministic, no LLM in the loop for this common shape.
- **(b) Always include the grand total.** When XAGG returns a grouped
  breakdown, prepend the summed total ("79 FIRs total, broken down as: …").
  Combined with the shipped sum-total verifier fix, the total always survives
  and the answer always leads with the number asked for.

**Why.** "How many X" is the single most common question shape and currently the
least reliable. Making it deterministic fixes D1 and hardens every count query.
**Est. gain: D1 and similar counts from ~0.2 to ~0.9, and removes run-to-run
flakiness.**

---

## Recommendation 3 — Re-chunk the CrPC PDF and prefer it for legal queries (HIGH impact, but INGESTION — teammate's domain)

**Problem.** KB1 now *accepts* retrieved legal docs (evaluator fix #1 works) but
retrieval surfaces the Anti-Rape Act and Police Rules, **not** the CrPC Section
154 content the question needs.

**Root cause (verified).** The CrPC 1898 PDF was ingested with broken chunking:
**2,360 chunks, only 5 mention "154"**, and the top chunks are table-of-contents
fragments ("## C", "Definitions. Words referring to acts…"). The actual
statutory text is fragmented, so it ranks poorly for its own topic.

**Recommended fix (NOT app code — for the ingestion owner):**
- **Re-ingest the CrPC (and the other legal PDFs) with structure-aware
  chunking** — split on section boundaries ("154.", "155.") rather than
  fixed-size windows, and **drop or down-weight the table-of-contents/index
  pages** so they don't dominate retrieval. Docling (already in the stack)
  supports layout-aware extraction; the issue is the chunker downstream.
- **Optionally add a small metadata tag** (`section: "154"`) per chunk so a
  "which section governs X" query can filter/boost by section number.

**Why it's ranked below 1 & 2.** Highest ceiling (it unblocks all ~8 KB
questions), but it's **not fixable in app code** — it needs re-ingestion by
whoever owns the KB pipeline. App-side, the evaluator fix already did its part.

---

## Recommendation 4 — Ingest the missing aggregate fields (MEDIUM impact, INGESTION — teammate's domain)

**Problem.** A7 (reporting-delay count) and CP6 (officer-placeholder count) score
low because the fields aren't queryable — they exist only in the raw Data API,
**not in the DB `cases` table or the graph** (verified: 0 graph nodes carry
`reporting_delay_reason`).

**Recommended fix (NOT app code):**
- Extend ingestion to extract `reporting_delay_reason` and the
  investigating-officer placeholder state into the DB/graph (the same way gender
  and age were recently added — commit `8245bd7` is the pattern to copy). Then
  XAGG's existing "honest can't-answer" for these becomes a real answer path.

**Why lower.** These are 2–3 specific questions, and the honest "can't answer"
is not a *wrong* answer — just an incomplete one. Lower value than 1–3.

---

## Recommendation 5 — Router determinism, if flakiness persists (MEDIUM impact, MEDIUM effort)

**Problem.** Beyond D1, the same question occasionally takes different routes
across runs (seen throughout evaluation).

**Root cause.** Router is already `temperature=0.0`, so residual non-determinism
is the LLM classifier's own variance on ambiguous queries. The deterministic
regex pre-router (`_deterministic_route_override`) only covers a narrow set.

**Recommended fix.**
- **Expand the deterministic pre-router** to cover the high-frequency
  unambiguous shapes: counts → aggregate; "which section/law" → document
  search; "list/show cases where" → RAG. These never reach the LLM classifier,
  so they can't flip.
- Lower priority than 1–2 because Recommendation 2(a) already fixes the most
  visible instance (counts), and expanding the pre-router is a broader,
  ongoing effort.

---

## Suggested order of work

| Order | Fix | Owner | Effort | Impact |
|---|---|---|---|---|
| 1 | **Name-threading in graph summary** (Rec 1) | App dev | Medium | High — fixes CR2/S3 |
| 2 | **Deterministic total for "how many X"** (Rec 2a) | App dev | Low–Med | High — fixes D1 + all counts |
| 3 | **Re-chunk CrPC + legal PDFs** (Rec 3) | Ingestion/KB | Medium | High — unblocks 8 KB questions |
| 4 | **Ingest delay/officer fields** (Rec 4) | Ingestion/KB | Low–Med | Medium |
| 5 | **Expand deterministic pre-router** (Rec 5) | App dev | Medium | Medium |

Fixes 1, 2, and 5 are app-code and I can implement + test them the same way as
the last four. Fixes 3 and 4 are ingestion changes for the teammate who owns the
KB/data pipeline. Doing 1 + 2 alone should produce a visible jump in the
FactualCorrectness score on the next full run.
