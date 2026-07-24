# Evidence Intelligence Engine — Model & Tool Benchmark Plan

**Purpose of this document.** The architecture report (`EVIDENCE_INTELLIGENCE_PLATFORM_ARCHITECTURE.md`) shortlists candidates for every stage of the pipeline but explicitly defers the final choice to benchmarking against real data ("Status: Pre-build — decisions pending A/B benchmarking"). This document is that benchmark plan: what to test, on what data, how, with what metrics, and what decision each result should drive. It contains **no results** — it is written to be executed on a GPU-equipped machine, not on the machine it was authored on (no CUDA GPU, ~9GB free disk). Nothing here assumes or references the current Muhafiz codebase; it is a standalone test plan against the architecture report's own shortlists.

Every open question below traces back to a specific section of the architecture report, cited inline, so the eventual benchmark report can be read side-by-side with the report it's resolving.

---

## 0. How to use this document

1. Work top to bottom — later stages (embeddings, graph, LLM) benefit from OCR/normalization being tested first, since their test data is cleaner if the upstream noise characteristics are already known.
2. Every stage section ends with a **Decision rule** — read it *before* running the tests, so the test isn't quietly redesigned after seeing results that don't match a prior assumption.
3. Every stage also has a **Kill condition** — the result that would mean the architecture report itself needs revisiting, not just a model swap. Watch for these specifically; they're the most valuable possible output of this exercise.
4. Nothing is "pass/fail" on ~100 pages of one corpus. Every metric should be reported with the sample size next to it, and every decision should say "decisive" or "too close to call, here's what would resolve it" — never round a marginal gap into false confidence.

---

## 1. Test corpus acquisition plan

### 1.1 What's needed and why

| Slice | Approx. target | Why |
|---|---|---|
| Typed/digital-native Urdu PDFs | 40-60 pages | Ground truth for OCR WER measurement; primary embedding/retrieval/NER/LLM test material |
| Scanned Urdu PDFs — printed Naskh | 30-40 pages | Tests the OCR ensemble's actual job (§4.1) — most real evidence scans will look like this |
| Scanned Urdu — Nastaliq specifically | As much as can be found, flagged separately if thin | §4.1 names this the hardest, most important OCR case; a report that skips it isn't testing the real risk |
| Roman-Urdu text (any source, can be short excerpts) | 10-15 short passages | §4.2's flagged risk — multilingual models may silently mishandle this |
| Mixed Urdu/English (bilingual documents) | Included within the above, not a separate acquisition | Matches the stated real document mix (§2) |

Total target: **100+ pages**, weighted toward getting *both* a clean and a scanned version of some overlapping content where possible — that overlap is what makes WER measurement possible without hand-transcribing ground truth from scratch.

### 1.2 Candidate sources (copyright-safe, to be verified at acquisition time)

| Source type | Examples to check | Expected format | License posture |
|---|---|---|---|
| Government gazette notifications | Federal/provincial gazette portals (e.g. Gazette of Pakistan), Islamabad Capital Territory administration notices | Typed PDF, some scanned/stamped | Government works — verify each portal's terms; gazettes are typically public-record |
| Superior court judgments in Urdu | Federal Shariat Court, Lahore High Court, Islamabad High Court judgment archives that publish Urdu-language judgments | Mix of typed and scanned (older judgments more likely scanned, sometimes Nastaliq letterheads) | Court judgments are public record; still confirm each site's reuse terms before bulk download |
| Public SOPs / circulars | Any ministry/department that publishes Urdu SOPs or public circulars | Typed PDF | Verify per-site |
| Urdu Wikipedia dump | `ur.wikipedia.org` dump via Wikimedia dumps | Plain text/HTML, digital-native | CC BY-SA — open, well-suited for embedding/NER/LLM test text, **not useful for OCR testing** (no scan) |
| OPUS Urdu-aligned corpora | OPUS project's Urdu-English parallel/monolingual sets | Plain text | Per-corpus license — check each subcorpus (some are CC, some are more restrictive) |
| National/provincial assembly Urdu proceedings | Where published as public record | Typed PDF | Verify terms |

**Explicitly excluded:** news articles, published books/novels, any copyrighted journalistic content — per the user's constraint, regardless of how easy they'd be to scrape.

### 1.3 If genuine scanned Nastaliq material can't be found

Say so explicitly in the benchmark report rather than silently testing OCR only on clean text or Naskh. Fall back to synthetic scanned-noise generation per `SYNTHETIC_DATASET_PLAN.md`'s noise-injection approach: render Nastaliq-font digital text to images with realistic scan artifacts (skew, compression, blur), and clearly label every downstream OCR number derived from synthetic scans as **optimistic, not representative** — this exact caveat is already anticipated in the architecture report (§12.4: "treat OCR benchmark numbers on synthetic handwritten data as optimistic, not representative").

### 1.4 Provenance record (fill in at acquisition time)

For every downloaded item, record: source name, URL, retrieval date, page count taken, license/terms basis, and whether it's typed or scanned. This table is the traceability artifact the user asked for — without it, no benchmark number below is defensible if challenged later (e.g., before real police data is available and someone asks "what did you actually test this on").

---

## 2. Stage-by-stage benchmark plan

### 2.1 OCR (architecture report §4.1)

| Candidate | Role in shortlist |
|---|---|
| PaddleOCR (PP-OCRv4/v5) | Primary — printed/typed scans |
| Tesseract (`urd` traineddata) | Cross-check / disagreement signal |
| TrOCR (Urdu/Nastaliq fine-tuned) | Hard cases — Nastaliq, handwriting |
| UTRNet | Reference-only comparison point, not a production candidate |

**Test data:** the scanned slice from §1.1, split into Naskh-printed and Nastaliq subsets. Where a typed/clean version of the same content also exists, that's the WER reference; where it doesn't, ground truth comes from manual transcription of a small spot-check sample (state the sample size explicitly — don't imply full manual verification if only a subset was checked).

**Method:**
1. Run each engine independently on every scanned page.
2. Compute WER against ground truth (Levenshtein-based word error rate) for the pages that have a clean reference.
3. For pages without a clean reference, manually spot-check a fixed sample (e.g. 10 pages) per engine and report qualitative error patterns rather than a fabricated WER.
4. Record wall-clock processing time per page, separately for CPU and GPU runs.
5. Run the ensemble logic as designed (Paddle primary, Tesseract cross-check triggering review flags on disagreement) and report how often the cross-check actually would have flagged a real error vs. how often it's noise.

**Metrics:** WER (Naskh vs. Nastaliq reported separately, never blended), time/page, ensemble disagreement rate, disagreement usefulness (does a flagged page actually contain more errors than an unflagged one, on the spot-check sample).

**Decision rule:** PaddleOCR is already the stated primary for printed text (§4.1) on independent benchmark grounds, so this test either confirms that holds on real data or surfaces a reason it doesn't — this isn't a fair 3-way popularity contest, it's a validation of an existing decision plus a genuine open question on TrOCR's real Nastaliq usability.

**Kill condition:** if TrOCR's Nastaliq WER on real scans is high enough that its output isn't a usable first-pass for human review (e.g., error-dense enough that a reviewer would find it faster to transcribe from scratch), that's not "pick a different model" — it's a finding that the human-review-queue sizing in §13/§14 needs to assume most Nastaliq documents are near-fully-manual, which has roadmap and staffing implications the architecture report doesn't currently size for.

---

### 2.2 Normalization, tokenization, sentence-splitting (§4.2)

| Candidate | Role |
|---|---|
| `urduhack` | Primary for normalization + tokenization |
| Stanza (Urdu UD model) | Alternative tokenizer/sentence-splitter, primary for sentence splitting per the report |
| Rule-based (custom regex on ۔ ؟ !) | Fallback for sentence splitting |

**Test data:** the full acquired corpus (typed slice is enough; doesn't need OCR output specifically, though testing on OCR output too is useful for realism).

**Method:**
1. **Compatibility check first, before anything else:** confirm `urduhack` actually installs and imports cleanly on the target Python version — the architecture report flags this explicitly as a "quiet unmaintained-dependency risk" (§4.2, `urduhack` predates Python 3.11+). This is a five-minute check that should happen before any benchmark time is spent on it.
2. Character/diacritic normalization spot-check: construct ~15-20 known Urdu normalization cases by hand (e.g. Arabic ي vs Urdu ی, Arabic ك vs Urdu ک, diacritic stripping, digit unification Eastern-Arabic vs. Urdu-Indic numerals) and confirm each candidate normalizer actually resolves them — a direct pass/fail table per case, not a blended score.
3. Sentence-splitting sanity: run each candidate against a sample of ~30-50 sentences containing the Urdu sentence-final mark ۔ (U+06D4) mixed with regular punctuation, and manually verify split points are correct. Specifically test cases where a naive English-tuned splitter would fail (report's own flagged risk).
4. Tokenization sanity: spot-check word boundaries on ~20 sentences including common trouble cases (izafat constructions, compound words).
5. Processing speed: pages/sec on the full corpus for each candidate.

**Metrics:** pass/fail table on the hand-built normalization case list, sentence-split accuracy on the hand-labeled sample, tokenization spot-check pass rate, throughput.

**Decision rule:** if `urduhack` fails the compatibility check outright, that resolves the decision immediately in favor of Stanza + a re-vendored normalization ruleset — don't spend further benchmark time on a library that doesn't run. If it does install, the normalization-case table is the deciding evidence (the report calls this the single highest-leverage cleanup step); sentence-splitting quality decides between Stanza and rule-based independent of the normalization-library choice.

**Kill condition:** if raw character-count chunking on Urdu text turns out to already be "good enough" empirically (i.e. sentence-boundary-aware chunking shows no measurable retrieval improvement in §2.3's test), that's worth noting as a place the architecture over-engineered relative to what the data needed — cheap to check once the embedding retrieval test exists, worth actually checking rather than assuming.

---

### 2.3 Embeddings (§5)

| Candidate | Dim | License | Included? |
|---|---|---|---|
| BGE-M3 | 1024 | MIT | Yes — primary candidate |
| multilingual-e5-large-instruct | 1024 | MIT | Yes — primary A/B partner |
| LaBSE | 768 | Apache 2.0 | Yes — sanity-check floor |
| jina-embeddings-v3 | 1024 (Matryoshka) | CC BY-NC 4.0 | **Only if a commercial license from Jina has been confirmed** — otherwise excluded before benchmarking starts, per the report's own flag |

**Test data:** the full corpus, chunked using whatever chunking approach §2.2 settled on. Build a retrieval query set by hand: for each of ~20-30 chunks, write a natural-language query whose answer is that specific chunk's content (a fact, a named entity, a date, a procedural detail actually stated in the text) — split explicitly by language (Urdu / English / Roman-Urdu) so a blended score can't hide a Roman-Urdu collapse, exactly as §5's own "what to measure" table specifies.

**Method:**
1. Embed the full corpus with each candidate model.
2. Embed each hand-written query with the same model.
3. Retrieve top-k (k=5 and k=10) for each query; record whether the known-relevant chunk appears, and at what rank.
4. Compute Recall@5, Recall@10, and MRR, broken out by language slice.
5. Measure embeddings/sec (batch size matched to a realistic ingestion batch) and peak GPU memory during embedding.
6. Load the embedding model alongside whatever LLM(s) are being tested in §2.6 simultaneously (matching the real serving scenario) and record total VRAM headroom left on the 24GB budget — the number that matters per §5 is headroom under concurrent load, not the embedding model in isolation.

**Metrics:** Recall@5/10 and MRR per language slice, embeddings/sec, standalone VRAM, concurrent-load VRAM headroom.

**Decision rule:** BGE-M3 and mE5-large-instruct are both viable on paper (§5); the retrieval numbers on the hand-built query set are the deciding evidence specifically because "no single obviously correct winner exists for Urdu" per the report — a small gap (a few points of Recall@5) on a ~20-30 query set should be read as "roughly comparable," not a confident winner. LaBSE exists to confirm both finalists actually beat a known older baseline, not to compete for the primary slot.

**Kill condition:** if the Roman-Urdu slice collapses for *all* candidates (near-zero recall), that confirms the report's flagged risk (§4.2) empirically and means a transliteration-normalization pre-step needs to be added to the roadmap now, not discovered at real-data cutover — this is exactly the scenario the report warns about wanting to avoid.

---

### 2.4 Reranker

> **Gap in the architecture report, flagged here rather than silently filled in.** §5 and §9 both mention "a small reranker" in the GPU budget line, but no section of the report actually shortlists a specific reranking model — there is no candidate table to benchmark against. This benchmark plan proposes evaluating **bge-reranker-v2-m3** (MIT license, same lineage as the BGE-M3 embedding candidate, commonly paired with it) as the sole reranker candidate, purely to answer whether reranking is worth its latency cost at all on this data — not as an A/B between reranker options, since the report never shortlisted more than one.

**Test data:** the same retrieval query set from §2.3.

**Method:**
1. Run the §2.3 retrieval test twice per winning embedding model: once with RRF/vector-only top-k as final results, once with the reranker applied to the top-20 candidates before truncating to top-k.
2. Compare Recall@5/10 and MRR with vs. without reranking.
3. Measure added latency per query (reranker inference time) and additional VRAM.

**Metrics:** precision/recall delta with vs. without reranking, added latency (ms/query), added VRAM.

**Decision rule:** if reranking measurably improves top-k precision on this data and the latency cost is small relative to the rest of the pipeline's per-query time, include it; if the improvement is marginal or negative on a ~20-30 query set, that's a real "too close to call" result — say so, and default to *not* adding reranking complexity for the POC rather than adding a subsystem the report itself never fully specified.

**Kill condition:** if reranking meaningfully hurts recall (a real possibility with a mismatched or over-aggressive reranker on a small, high-precision-already query set), that's worth surfacing loudly — it would mean the "small reranker" line item in the architecture's GPU budget (§9) should be removed rather than defaulted-in.

---

### 2.5 Entity extraction (§7.2)

| Candidate | Role |
|---|---|
| Regex + format validators | Structured entities: phone, CNIC, vehicle plates, FIR/case numbers, dates — never LLM-extracted per the report |
| Stanza Urdu NER | Fast first pass for generic entities: person, location, organization |
| LLM few-shot extraction | Domain-specific entities (vehicle, weapon, gang/alias, informal roles) and fallback on Stanza's low-confidence spans |

**Test data:** the full corpus. Since there's no labeled ground truth for real documents, this is a manual precision spot-check, not a recall benchmark (recall would require exhaustively labeling the corpus, which isn't in scope for a ~100-page test pass).

**Method:**
1. Run the regex/validator layer on the full corpus; manually verify every match found (precision should be at or near 100% by construction — report any exception as a real bug, not an acceptable miss).
2. Run Stanza NER on the full corpus; manually review a fixed sample of extracted entities (e.g. 50-80 spans) and score precision (is this actually a person/location/organization, correctly typed).
3. Run the LLM extraction pass for domain-specific types on the same sample; score precision the same way.
4. Note recall qualitatively where obvious misses are spotted during the manual review (e.g. "this paragraph clearly names a person Stanza didn't tag"), without claiming a formal recall number.

**Metrics:** regex-layer precision (should be ~100%, exceptions are bugs), Stanza NER precision on the reviewed sample, LLM-extraction precision on the reviewed sample, qualitative recall notes.

**Decision rule:** the regex layer isn't really an A/B — it's a correctness check. The Stanza-vs-LLM-fallback split is validated by whether Stanza's precision on generic entity types is good enough to trust as the fast first pass, or whether it's low enough that the "LLM fallback on low-confidence spans" threshold needs to be set aggressively (i.e., escalate to LLM often), which has cost implications the report doesn't currently quantify.

**Kill condition:** if a mis-transcribed CNIC or FIR number slips through the regex validator on real (possibly noisy, possibly OCR'd) text, that's the exact failure mode §4.3 calls "unacceptable" for entity resolution — treat any such miss as a validator bug to fix immediately, not a benchmark data point to average away.

---

### 2.6 Graph write/traversal smoke test (§6, §7)

**Scope note, matching the report's own framing:** at ~100 pages of test corpus, this cannot be a real benchmark of graph performance at scale — it is a smoke test that the chosen stack (Neo4j Community Edition) works end to end, and should be reported as exactly that, not dressed up as a performance verdict.

**Test data:** entities/relationships extracted in §2.5, loaded as graph writes per the schema in §7.1 (Person, Vehicle, PhoneNumber, Address, Organization, Incident, Document nodes; `BELONGS_TO_CASE`, `APPEARS_IN`, `ASSOCIATED_WITH`, etc. relationships), including at minimum one deliberately-constructed multi-entity chain in the test data so a 2-3 hop traversal has something real to traverse (the corpus needs enough recurring entities across documents for this to be meaningful — this may require synthetically weaving 1-2 known entities across a handful of the acquired real documents' extracted-entity sets, clearly labeled as a test-construction step, not something found "naturally" in ~100 pages of unrelated public documents).

**Method:**
1. Stand up Neo4j Community Edition (Docker is the fastest path).
2. Write the extracted entities/relationships with `BELONGS_TO_CASE` and provenance links per Figure 2.
3. Run at least one 1-hop, one 2-hop, and one 3-hop Cypher traversal query matching the query shapes named in §6 ("who else is linked to this address," "map this person's known associates").
4. Record query latency for each hop depth (expect sub-second at this scale — the point is confirming it runs, not stress-testing it).
5. Confirm case-scoped filtering works (a traversal query with a `case_id` filter returns only that case's subgraph).

**Metrics:** query latency per hop depth (reported as a smoke-test number, explicitly not a scale benchmark), confirmation that case-scoped filtering behaves correctly, confirmation the write pipeline (extraction → resolution → graph write) runs end to end without manual intervention.

**Decision rule:** this doesn't decide Neo4j vs. Apache AGE — that choice in the report is driven by future-scale traversal performance and licensing posture (§6), neither of which a 100-page smoke test can speak to either way. The value of this test is purely "does the designed pipeline actually work," not "which graph DB wins."

**Kill condition:** if the ingestion pipeline (NER → relation extraction → resolution → graph write) breaks or requires significant undocumented glue code to run end to end, that's a real signal the §7.2 pipeline design has an integration gap the architecture report didn't anticipate — flag it, don't quietly patch around it without noting it.

---

### 2.7 LLM generation (§9)

| Role | Candidates |
|---|---|
| Generation / final-answer fluency | Qalb-8B-Instruct, Alif-1.0-8B-Instruct |
| Routing / reasoning / structured tool-use | Qwen2.5-14B-Instruct or Qwen3-14B (primary), Qwen2.5-7B-Instruct (fallback if concurrent VRAM is tight) |

**Test data:** a handful of real questions constructed from the downloaded corpus — at minimum one summarization prompt, one direct factual question, and one question requiring synthesis across two different parts of the text, per §9's generation-role test; plus a JSON-structured extraction/routing task for the Qwen candidates to test tool-use reliability specifically.

**Method:**
1. Serve each generation candidate locally (vLLM if the target OS/environment supports it; note explicitly if it doesn't and report what was used instead — e.g. a plain `transformers` pipeline at reduced throughput — rather than silently skipping the stage).
2. Run the constructed question set against each candidate at the quantization level actually intended for production (Q4/Q8, matching §9's stated budget).
3. For each answer: check groundedness against the source text (does every claim trace to something actually in the corpus, or does it drift/hallucinate), do a subjective fluency read, and — as a rough secondary check only, not a rigorous eval — optionally have a second model score the answer's groundedness, clearly labeled as a sanity pass, not a validated metric.
4. Measure tokens/sec throughput and peak GPU memory at the tested quantization.
5. Repeat the JSON-structured extraction/routing task for the Qwen candidates; score JSON validity rate and field-extraction correctness on the test set.
6. Load the generation model, routing model, embedding model, and reranker (if kept per §2.4) concurrently and measure total VRAM against the 24GB budget stated in §9/§11 — this is the specific number the report flags as needing empirical confirmation ("load-test both scales anyway rather than assuming either holds," §9).

**Metrics:** groundedness pass/fail per test question (with reasoning noted, not just a score), fluency read (qualitative), tokens/sec, peak VRAM standalone and under concurrent load, JSON validity rate for the routing candidates.

**Decision rule:** Alif-1.0 vs. Qalb is explicitly flagged in the report as "roughly comparable, ranking not yet reliably established" based on a self-reported ~3-point LLM-judge delta between two papers using different authors' own protocols — this benchmark's own small-sample groundedness/fluency read is a real independent data point, but on a handful of questions it should also be read as directional, not definitive, and reported with that same honesty the report models.

**Kill condition — the important one to watch for:** if the stated GPU budget in §9 (~17-19GB for generation + routing + embeddings + reranker concurrently) doesn't actually hold at the tested quantization — i.e., real VRAM usage measured here exceeds what §9 assumed — that's not a "pick the smaller model" footnote, it's a finding that §9's and §11's hardware sizing needs revision (e.g., serving sequentially instead of concurrently, or confirming the 7B routing fallback is the real default rather than the stated primary). Flag this prominently if it happens.

---

## 3. Benchmark report structure (to fill in once tests are run)

For each stage above, the eventual report should follow this shape — matching what the user asked for in the original request, so nothing here gets diluted into vague conclusions:

```
### <Stage name>
- What was tested: <candidates>, on <exact data slice, page/query count>
- Configuration: <model version/commit, quantization, hardware>
- Measured results: <the actual numbers, with sample size stated next to every metric>
- Recommendation: <a clear pick, IF the result is decisive> — otherwise: "too close to call on this data; would be resolved by <specific next step>"
- Surprises vs. the architecture report's assumptions: <anything that contradicts §X, flagged explicitly, with a note on whether it's a model-choice issue or an architecture-revision issue>
```

---

## 4. Sequencing recommendation

Run stages in this order — each has a dependency on the one before it either for cleaner test data or for resolved tooling:

1. **Corpus acquisition (§1)** — nothing else can start without it.
2. **OCR (§2.1)** — establishes what "real" noisy text looks like for later stages, and is independent of every other decision.
3. **Normalization/tokenization (§2.2)** — the `urduhack` compatibility check specifically should happen almost immediately; it's cheap and blocks nothing else, but a bad answer here changes what text every downstream stage is testing against.
4. **Embeddings (§2.3) → Reranker (§2.4)** — reranker testing needs a working retrieval baseline first.
5. **Entity extraction (§2.5) → Graph smoke test (§2.6)** — the graph test needs extracted entities to load.
6. **LLM generation (§2.7)** — can run in parallel with 4-5 once the corpus and question set exist, but do the concurrent-VRAM measurement (final method step) last, once the actual embedding/reranker winners from §2.3/§2.4 are known, since that's the real serving configuration.

---

## 5. What this plan deliberately does not cover

- It does not re-litigate the Neo4j vs. Apache AGE choice (§6) — that's a licensing/future-scale decision the architecture report already reasons through independently of small-corpus benchmarking, and this plan's graph section is explicit that it's a smoke test, not a comparative benchmark.
- It does not benchmark English-only embedding models, per the architecture report's own explicit guidance (§5) that doing so would waste benchmark time on candidates already known to be disqualified.
- It does not include a rigorous LLM-as-judge evaluation protocol for generation quality — §2.7 is explicit that any secondary-model check is a sanity pass, not a validated eval, matching the user's own framing of what this pass is and isn't.
