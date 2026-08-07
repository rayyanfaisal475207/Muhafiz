# Muhafiz — Demo Test Script

Every query below was run live against the actual running backend (`:8000`)
on 2026-08-05, through the real `/api/chat` SSE pipeline, as a
`platform-admin` test account — not hand-written, not simulated. Route and
grounding status are read directly from the pipeline's own trace events.
Where a result was weak or wrong, it's kept and marked, not swapped out —
this is a working demo script, not a highlight reel.

## Before you demo

- Backend up (`uvicorn src.main:app`), Postgres/AGE healthy, local model
  tunnel reachable (see `RUN.md`).
- Log in as an account with role `supervisor` or higher — `XGRAPH`, `XAGG`,
  and `XNETWORK` are gated to supervisor+ and will silently be unreachable
  (not: give a wrong answer) for a plain `investigator`.
- The within-case examples below use `CASE-B0-THEFT-001` (FIR 1/2026,
  mobile/vehicle theft, Bhara Kahu) — select or pass this case for those
  specific questions.
- This corpus is small and synthetic (~90 documents, ~40 cases). Don't
  free-improvise queries live without checking they land on real content
  first — several queries below that sound reasonable returned nothing,
  not because the system is broken, but because the corpus doesn't cover
  that specific ask (see each section's notes).

---

## 1. DIRECT — greetings, meta-questions, no retrieval

**Purpose:** skip retrieval entirely for conversational messages — instant
response, no irrelevant context injected.

**Query (English):** `Hello`
**Route:** `DIRECT` | **Grounded:** n/a (no retrieval)
**Response:**
> Hello! How can I assist you today? 😊

**Query (Urdu):** `السلام علیکم`
**Route:** `DIRECT` | **Grounded:** n/a
**Response:**
> معزز خواتین اور حضرات، السلام علیکم!

**Update:** the compound phrasing gap noted here originally ("Hello, what
can you help me with?" misrouting to RAG) is fixed — a few-shot example
was added to `prompts/router.txt` and verified live:
`route_query("Hello, what can you help me with?")` now returns `DIRECT`.
Verified at the classification level only (not re-run through the full
answer-generation pipeline, to conserve this session's Groq quota — see
the note at the end of this document) — the routing decision itself is
confirmed correct.

---

## 2. SQL — structured penal-code lookup

**Purpose:** deterministic lookup against the structured police reference
table (offense → section, cognizability) — not a document search.

**Query:** `What PPC section covers mobile phone theft?`
**Route:** `SQL` | **Grounded:** `True`
**Response:**
> The PPC sections that cover theft, including mobile phone theft, are 379
> PPC and 411 PPC, as indicated in the FIR documents [Document 1] and
> [Document 2]. These sections pertain to theft and receiving stolen
> property, respectively.

**Query:** `Is cyber harassment a cognizable offense?`
**Route:** `SQL` | **Grounded:** `True`
**Response:**
> Yes, cyber harassment is a cognizable offense. This is evident from the
> documents provided, which reference FIR No. 1/2026 registered at Kohsar
> Police Station regarding Harassment/Cyber Harassment [Document 1]. The
> fact that an FIR has been registered indicates that the offense is
> recognized under the legal framework as cognizable, allowing for the
> police to investigate without a warrant [Document 1].

**Demo note:** both are strong. This route was silently broken for most of
this session before a router fix landed (see §9) — it now works reliably
as shown.

---

## 3. RAG — document search over the ingested corpus

**Purpose:** semantic + keyword hybrid search over case documents
(FIRs, case diaries, charge sheets), reranked and cited.

**Query:** `Summarize the FIR registered at Bhara Kahu police station for mobile or vehicle theft.`
**Route:** `RAG` | **Grounded:** `True`
**Response:**
> The FIR registered at Bhara Kahu Police Station is FIR No. 1/2026, dated
> 2026-01-01, and pertains to an incident of Mobile/Vehicle Theft
> [Document 4]. The complaint was filed by Irfan Mirza, whose CNIC is
> 61101-1338908-3, and the application was received by ASI Tahira Latif on
> 2026-01-02 [Document 3]. Despite the investigation conducted by Inspector
> Fariha Saeed, the Mulzim (culprit) could not be traced or identified as
> of the date of the report [Document 1]. The incident occurred at 23:17
> hours on 2026-01-01 [Document 4]. The FIR was registered under Sections
> 379 PPC and 411 PPC of the Pakistan Penal Code [Document 4].

**Query (English, factual follow-up):** `What is stated in the FIR about the stolen motorcycle?`
**Route:** `RAG` (not GRAPH/GRAPH_HYBRID, despite a case being active — see §9) | **Grounded:** `True`
**Response:**
> The FIR states that the complainant parked their Honda CD-70 motorcycle,
> with registration number ICT-3615, outside the roadside stand at I-8
> Markaz on 2026-01-03 at about 23:17. The complainant stepped away for 15
> minutes and upon returning, found the motorcycle missing. The
> surrounding area, including the lane behind the shops, was searched but
> the motorcycle could not be found [Document 2]. Additionally, the total
> value of the stolen motorcycle is stated as approximately PKR 120,000
> [Document 4].

**Query (Urdu — do not use live, kept here as a documented negative result):**
`بھارہ کہو تھانے میں درج موبائل چوری کی ایف آئی آر کا خلاصہ بتائیں۔`
(Same information need as the first English example above.)
**Route:** `RAG` | **Grounded:** none — abstained
**Response:**
> I couldn't find sufficient information in the knowledge base to
> accurately answer your question...

**Demo note — original capture, real at the time:** the identical
information need, asked in Urdu, failed where the English phrasing
succeeded. The likely cause: `FIR-2026-THEFT-001.pdf` (and its case
diary/charge sheet) is itself an **entirely English-language document** —
a distinct rendering tier this session's audit already found and partly
worked around elsewhere (see `git log` — "Fix NER mistagging English
form-field labels as Person entities").

**2026-08-06 re-verification (Priority 2 of `OPEN_GAPS_FIX_PROMPT.md`):
does not reproduce.** Re-ran this exact Urdu query against this exact
case live through `process_query()` four times (once standalone, three
back-to-back). Every run routed `RAG` and retrieved 12-13 chunks — the
same ballpark as the English phrasing's 11 — with `src/pipeline/
cross_script_variant.py`'s translated-query retrieval leg (already wired
into the RAG route, `orchestrator.py:1617`, predating this document) in
play throughout. 3 of 4 runs were fully grounded with a correct, cited
Urdu answer; 1 of 4 came back ungrounded, but for a distinct, already-
tracked reason — the citation validator flagging a well-formed answer
that happened to cite sparsely, the same class of flakiness fixed
elsewhere in this repo's own history (see "Fix XNETWORK verifier
rejections: citation format mismatch, not model quality" and "Fix
verifier's uncited-answer check bypassable by short enumerated lists") —
not a retrieval/cross-script failure. Conclusion: this gap is not
currently open. The original capture was likely affected by this
session's own documented Groq quota rotation (see §9) or was an unlucky
single sample against an already-known, separately-tracked verifier
flakiness — not a persistent cross-script retrieval defect. No code
change made for this priority; re-run this query yourself before
re-opening it as a gap.

---

## 4. GRAPH — within-case entity/relationship lookup

**Purpose:** named-entity relationship/network questions scoped to one
active case.

**Update — root cause found and fixed after this document was first
written.** The original attempt below was classified `GRAPH_HYBRID`
instead of `GRAPH`, with the model's own stated reasoning being that it
wasn't confident "Irfan Mirza" was a literal identifier rather than a
descriptive reference. That's a real, reproducible (3/3) calibration gap,
not a one-off — fixed with an explicit rule added to `prompts/router.txt`
("a capitalized proper-noun-shaped name IS a literal identifier — don't
downgrade out of caution").

**Query:** `Is Irfan Mirza known to associate with anyone else in this case?`
**Route (post-fix, verified at the classification level):** `GRAPH`,
`target_entity: "Irfan Mirza"` (previously `GRAPH_HYBRID`,
`target_entity: null`)

This confirms the routing decision only — re-running the full
answer-generation pipeline for this exact query to capture the actual
response text was deliberately skipped this session to conserve Groq
quota (this session's repeated live testing rotated through nearly all
configured API keys — see §9). Expect it to cite the real
`ASSOCIATED_WITH` edge `Irfan Mirza <-> Bilal Malik` (father/son,
confirmed present in the graph as of this session's cleanup), but capture
the actual response live before presenting it as a demo example — this
document reports what's verified, not what's assumed.

---

## 5. GRAPH_HYBRID — broad within-case questions

**Purpose:** combines graph relationship context with document search for
an open-ended "what do we know" question, still scoped to one case.

**Query:** `Give me a full picture of what we know so far in this case.`
(against `CASE-B0-THEFT-001`)
**Route:** `GRAPH_HYBRID` (correctly — this was misrouted to `RAG` before
the router fix in §9; now correct) | **Grounded:** `False` — abstained
**Response:**
> Based on the available evidence, I cannot provide a confident answer to
> this question — the cited sources do not sufficiently support a
> specific claim. Please consult the original case documents directly.

**Demo note:** the routing is now correct, but the answer itself
abstained — a case this small (4 documents) with a vague "full picture"
prompt doesn't give the generator much to synthesize confidently. **For a
demo, prefer §3's more specific factual RAG example**, or ask something
narrower like "What do we know about the suspect in this case?" and
verify live before presenting.

---

## 6. XGRAPH — cross-case named-entity / recurrence check

**Purpose:** does a specific person/vehicle/phone number appear in more
than one case? Requires supervisor+.

**Query:** `Has Fariha Khan appeared in any other cases?`
**Route:** `XGRAPH` | **Grounded:** n/a (deterministic graph traversal, not generated prose)
**Response:**
> No connections to other cases were found for this entity.

**Query:** `Has phone number 0363-8536477 appeared in other cases?`
**Route:** `XGRAPH` | **Grounded:** n/a
**Response:**
> No connections to other cases were found for this entity.

**Demo note:** both are honest negative results (neither entity recurs
elsewhere in this small corpus) — correct behavior, but not a visually
exciting demo moment. **For a positive-match demo**, use a name confirmed
to be single-case only if you want to show the negative path, or check the
live graph for a genuinely recurring entity before presenting a
"found in multiple cases" narrative — none was confirmed recurring in this
session's testing.

---

## 7. XAGG — cross-case aggregate / count

**Purpose:** counts, rankings, and listings computed directly over case
metadata or graph recurrence. Requires supervisor+.

**Query:** `Which police stations have the most open theft cases?`
**Route:** `XAGG` | **Grounded:** `False` — falls back to raw computed data (by design, see below)
**Response:**
> Here is the cross-case aggregate result computed directly from the case
> database (shown in its original form; a translated summary was not
> consistently faithful to it):
>
> - Industrial Area: 2 cases
> - Tarnol: 1 cases
> - Golra: 1 cases
> - Margalla: 1 cases
> - Sabzi Mandi: 1 cases
> - Koral: 1 cases
> - Shalimar: 1 cases

**Query:** `کل کتنے کیسز ہیں؟` ("how many cases in total")
**Route:** `XAGG` | **Grounded:** `False` — same fallback behavior
**Response (grouped by category, not a bare total — see note):**
> - unknown: 9 cases
> - Burglary/House Theft: 5 cases
> - Cyber Fraud/Online Scam: 5 cases
> - Mobile/Vehicle Theft: 5 cases
> - Domestic Dispute: 4 cases
> - Cheating/Financial Fraud: 4 cases
> - Harassment/Cyber Harassment: 4 cases
> - Illegal Weapon Possession: 3 cases
> - Road Traffic Accident: 3 cases
> - Missing Person: 1 cases

**Demo note, important — explain this rather than let it look broken:**
`grounded: False` here is **expected, normal behavior for every XAGG
query**, not a failure. The underlying data is a deterministic
count/listing computed directly from the database — when the generation
model's paraphrase of that data doesn't verify as a faithful
translation, the system correctly falls back to showing the **raw,
guaranteed-accurate result** rather than a possibly-mistranslated
summary. This is a deliberate design choice, documented in this repo's own
commit history, not a bug — say so if a viewer asks why it looks like a
"grounded: false" red flag.

**2026-08-06 fix (Priority 3 of `OPEN_GAPS_FIX_PROMPT.md`):** the Urdu
"total cases" query answering with a category breakdown instead of one
number is fixed. `src/pipeline/xagg.py` gained a bilingual
`_TOTAL_KEYWORDS` family and a `_total_count()` path, dispatched ahead of
the default group-by fallback whenever a total/grand-total phrasing is
present with no explicit station/category grouping word also in the
query (so "total cases per station" still gets the breakdown). Live-
verified against the real stack: `کل کتنے کیسز ہیں؟` now returns
`"Total cases: 43"` (via the same deterministic-fallback path shown
above, since the citation validator still rejects the generation model's
paraphrase the same way it does for every other XAGG query — expected,
not a regression), and the English phrasing `"How many cases are there
in total?"` returns the identical shape. See the `fix/xagg-grand-total`
branch.

---

## 8. XNETWORK — open-ended cross-case network/pattern synthesis

**Purpose:** "what's the overall picture" style questions, synthesized
across precomputed community summaries rather than a single lookup.
Requires supervisor+. This is the newest route (Section 2 of this
session's work).

**Query:** `What's the overall picture on the network of people connected across these cases?`
**Route:** `XNETWORK` | **Grounded:** `True`
**Response:**
> **Cross-Case Network Finding**: This analysis synthesizes information
> across multiple case clusters to identify the overall picture of the
> network of people connected across these cases.
>
> Upon reviewing the community summaries, it is evident that each case
> involves a distinct set of individuals. For instance, CASE-B0-RTA-001, a
> road traffic accident case, involves Yusra Nawaz, Hamza Awan, and
> Inspector Hassan Khan Tarnol [Document 1]. Similarly, CASE-B0-RTA-002,
> another road traffic accident case, involves Inspector Shahid Abbasi,
> Naveed Raza, Tariq Nawaz, Hassan Awan, and Junaid Mirza [Document 2].
>
> Other cases, such as CASE-B0-BUR-002, a burglary/house theft case,
> involve Inspector Iram Qureshi, Hassan Malik, Tariq Niazi, Usman Mirza,
> and Iram Saeed [Document 3]. CASE-009, a burglary/house theft
> investigation, involves وقاص علی نیازی and سلمان رضا احمد [Document 4].
> Lastly, CASE-B0-FRAUD-002, a cheating/financial fraud case, involves
> Farah Mirza, Inspector Rabia Malik, Sana Malik, Tariq Qureshi, Irfan
> Qureshi, and Adeel Sheikh [Document 5].
>
> However, it is crucial to note that no specific relationships between
> individuals are stated beyond their shared connection to each respective
> case [Document 1, Document 2, Document 3, Document 4, Document 5]. There
> is no evidence to suggest a connection between the individuals across
> different cases. The network of people connected across these cases
> appears to be fragmented, with each case involving a unique set of
> individuals.
>
> In conclusion, the overall picture of the network of people connected
> across these cases is one of distinct, isolated clusters, with no
> apparent connections between individuals across different cases.

**Query (Urdu):** `کیا ان کیسز میں کوئی مجموعی نمونہ سامنے آتا ہے؟`
**Route:** `XNETWORK` | **Grounded:** `True`
**Response:**
> یہ ایک کراس کیس نیٹ ورک فائنڈنگ ہے۔ مختلف کیسز کے جائزے سے پتہ چلتا ہے
> کہ ان میں کوئی واضح مجموعی نمونہ یا رابطہ نہیں ہے۔ ڈاکومنٹ 1 [Document 1]
> میں عدنان قریشی وحید اور عثمان خالد احمد کا ذکر ہے جو ایک فراڈ کیس سے
> متعلق ہیں۔ ڈاکومنٹ 2 [Document 2] میں وقاص علی نیازی اور سلمان رضا احمد
> کا ذکر ہے جو ایک چوری کیس سے متعلق ہیں۔ ... ان تمام کیسز میں، کوئی واضح
> رابطہ یا نمونہ نہیں ہے جو انہیں ایک دوسرے سے جوڑتا ہو۔

**Demo note — this is the strongest demo material in this whole
document.** Both examples are fully grounded, correctly cited per
community, bilingual, and — most importantly — **correctly refuse to
fabricate a cross-case connection that doesn't exist**, explicitly stating
the network is "fragmented" / "کوئی واضح رابطہ ... نہیں ہے" rather than
forcing a false narrative. This is the exact behavior the whole feature
was designed to guarantee (see `prompts/cross_case_network.txt` rule 3:
"do not infer a connection BETWEEN two different communities unless a
summary itself states one"). If you want a **positive**-connection demo
moment instead, the live graph currently has real confirmed relationships
within `CASE-B0-CYBER-001` (Fariha Khan ↔ Tahira Chaudhry,
complainant/accused; Fariha Khan ↔ Hamza Awan, father/daughter) — ask
about that specific case's network for a demo that shows a **found**
connection rather than an honest absence of one.

---

## 9. What changed this session (context for whoever reads this later)

This demo script was captured immediately after two rounds of work:

1. **Section 2**: built `XNETWORK` (community detection + summarization +
   the new route) on top of the existing, audited Apache AGE graph.
2. **A live bug-hunt pass** that found and fixed a router bug active for
   this **entire session up to that point**: the JSON validator for the
   router's classification call accepted a syntactically-valid-but-wrong
   route value (`{"route": {"confidence": 0.1, ...}}`, then later
   `{"route": "unknown"}`), silently defaulting to `RAG` for **every**
   route without a deterministic keyword override — DIRECT, SQL, WEB,
   GRAPH, GRAPH_HYBRID. This is fixed (see commit
   "Fix router's JSON validator accepting a malformed/wrong route value")
   and is why SQL/DIRECT/GRAPH_HYBRID route correctly in this document —
   they did not, reliably, before that fix.
3. A live graph cleanup: 863 corrupted `Person` nodes (form-label/
   place-name extraction noise from before an earlier NER fix) were
   removed, and `ASSOCIATED_WITH` relationship edges were backfilled using
   a separately-fixed extractor — the graph this document's examples run
   against is the **cleaned, current** state, not the original corpus as
   ingested.
4. **A second, smaller fix pass** (after this document's first draft):
   two router.txt calibration gaps this document itself surfaced —
   compound "Hello, what can you help me with?" phrasing (§1) and the
   GRAPH-vs-GRAPH_HYBRID name-caution issue (§4) — were fixed with
   targeted few-shot/rule additions and verified at the classification
   level. This is a real instance of the demo script doing its job: it
   found two gaps just by being written honestly.

**A real operational constraint hit during this session, worth knowing
before you run a live demo:** repeated live testing throughout this
session's fix-verify cycles rotated through nearly all configured Groq
API keys (`GROQ_API_KEY` + `_1` through `_4`) due to free-tier rate
limiting. The system degrades gracefully when this happens (further
key rotation, eventually a slower/queued response) rather than failing
outright, but a demo immediately after a heavy testing session is a bad
time to also be burning the same shared quota — check `GROQ_API_KEY`
rotation state or simply wait a few minutes for the free-tier window to
reset before presenting live.

**2026-08-06 update — XNETWORK community-noise filter (§8):** the
blocklist described below as "an evolving blocklist, not a closed
problem" got a full audit instead of a fourth incremental patch. Live
audit of the real graph (161 Person nodes) found 10 nodes across 7
distinct strings the exact-phrase/suffix/length checks all missed — e.g.
`"Inspector Fariha Saeed Bhara"` (1 occurrence) sitting alongside the
correctly-extracted `"Inspector Fariha Saeed"` (7 occurrences), traced to
a document-rendering artifact (a table/field boundary collapsing with no
separating punctuation, so an adjacent field's text bleeds directly onto
a real name). Fixed structurally in `community_detection.py`, not with
another word list: (1) reject any candidate containing a rendering-
boundary character (newline, `|`, parens) — no real name ever contains
one; (2) reject a candidate that is a low-frequency superstring of a
shorter name already independently common in the same corpus (a
data-driven prefix-contamination check, generalizes past any specific
station/label word). `scripts/cleanup_implausible_person_nodes.py` was
updated to apply the same combined check and confirmed live (`--dry-run`)
to catch exactly these 10 nodes; not yet applied (`--apply`) — that's a
real, destructive graph mutation left for a deliberate follow-up, not
bundled into this fix. See the `fix/xnetwork-noise-filter-audit` branch.
This closes the noise category found in this specific audit; whether it
is the *last* category is not something a single audit pass can prove —
watch for a fifth round the same way this filter has grown before.

Residual, known, not fixed as of this document:
- `relationship_extraction.py` only ever considers people who co-occur in
  the same physical chunk — it cannot find a relationship stated across
  two different chunks/documents about the same case. A structural
  limitation, not a bug; fixing it would mean a document-level (not
  per-chunk) extraction pass, a larger change than anything in this
  document's scope.
