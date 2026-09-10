# Module 39 — the KB questions' "and does our data show it?" half

**Branch:** `feat/kb-data-half-composition` · **Base:** `main` @ `c5533ba`
(Module 52 / PR #44 merged)
**Brief:** Module 39 in `GOLD_QA_REMAINING_FIXES_PLAN.md`
**Found by:** Module 30; re-baselined and named the largest KB gap by Module 52
**Date of every measurement below:** 2026-09-09, worktree
`D:/Rapids AI/muhafiz-m39`, backend on `:8021`, `muhafiz-postgres` healthy
(73 cases), model-server tunnel `/health` = 200 throughout.

**Chroma:** a **private copy** at `D:/Rapids AI/muhafiz-m39/data/chroma_db`
(`documents_in_store: 7716`, asserted at every backend boot). The shared store
at `D:/Rapids AI/Evidence Intelligence Platform/data/chroma_db` was neither
read nor written. Nothing was ingested, re-embedded or re-indexed. The full
`pytest -q` suite was never run (it empties `muhafiz_entity_descriptions`).

**Files the brief put out of bounds were not touched:** `verifier.py`,
`xagg.py` (either copy), `supervisor.py`, `router.py`, `xnetwork.py`,
`evaluation/`, `prompts/evaluator.txt`. `git diff --stat` against the base is
three source files and two docs.

**Quota**, checked with Module 54's canonical pattern
(`grep -ciE "rate limit|RESOURCE_EXHAUSTED|429|quota|UNAVAILABLE|503"`):
**0** across all 54 live runs. No run in this report is a rate-limited run.

**Concurrency:** port 8020 (the Module 61 track) held a listener throughout.
It was up for both arms and for the regression and paraphrase sweeps alike, so
it is not a differential explanation — but see §5.4 on wall-clock numbers.

---

## 1. Root cause

The brief's diagnosis is **confirmed**, and this module adds the part it could
not know without probing: **which of the six figures are computable today, and
by what.**

Every gold KB answer is compound — a statutory norm **plus** a figure from our
own case database. Module 52 re-baselined all eight KB questions over 48 live
runs (two arms × 8 × 3) and found **0 of 48** producing gold's data half in
either arm. The RAG sub-agent searches seven English statute PDFs; the figure
gold asks for is in none of them, so it answers the second clause with *"the
documents do not confirm whether…"* — which gold explicitly contradicts,
because gold gives the number.

### 1.1 The probes — every figure derived independently, before any code

Hand-written Cypher against the live AGE graph
(`scratchpad/probe39*.py`), plus `resolve_aggregate_kind()` called statically
to find out what — if anything — each question already dispatches to.

| Q | Gold's data half | Independently derived | Agrees? | Existing aggregate |
|---|---|---|---|---|
| KB3 | 68 of 74 registering/investigating pairs are the same person (92%); 6 split | **68 of 74 (91.9%)**, 6 split | ✅ **exact** | ❌ **none** — `unsupported_officer` |
| KB4 | 45 property entries | **45** entries across 28 FIRs | ✅ exact | ✅ `seized_property_disposition` |
| KB5 | 8 violence-against-women reports | **8** | ✅ exact | ✅ `dv_report_fir_match` |
| KB6 | 32 weapons | **32** | ✅ exact | ✅ `weapon_compliance_scan` |
| KB8 | 26 challans sent to court | **26** rows / 26 distinct cases | ✅ exact | ❌ **none** — resolves to `criminal_record_court_crosscheck` (33 criminal records) |
| KB9 | 8 FIRs cite PPC 302 | **10** distinct FIRs | ❌ **diverges** | ⚠️ only the generic `statute_court_stage_join` |

The Cypher, verbatim:

```cypher
-- KB3.  144 ASSIGNED_TO edges: 74 role='investigating', 70 role='recording'.
MATCH (o:Officer)-[r:ASSIGNED_TO]->(c:Case)
RETURN c.case_id, o.canonical_name, r.role
-- Pairing per case: 71 cases carry both roles -> 68 same person, 3 different
--   (fir-205-26, fir-245-26, fir-88-26); 3 further investigating assignments
--   have no recording counterpart. 68/74 = 91.9%; 3+3 = gold's "only 6".

-- KB4
MATCH (s:StructuredRecord) WHERE s.record_type='malkhana_register'
RETURN count(s)                                              -- 45

-- KB5
MATCH (r:StructuredRecord)
WHERE r.record_type='pkm_application' AND r.service_type='women_violence_report'
RETURN count(r)                                              -- 8

-- KB6
MATCH (w:Weapon) RETURN count(w)                             -- 32

-- KB8
MATCH (r:StructuredRecord) WHERE r.record_type='chalaan_dispatch'
RETURN count(r)                                              -- 26
MATCH (r:StructuredRecord)-[:BELONGS_TO_CASE]->(c:Case)
WHERE r.record_type='chalaan_dispatch'
RETURN count(DISTINCT c.case_id)                             -- 26
-- `chalaan_outcome` (the record type xagg.py DOES read) is 20, not 26.

-- KB9
MATCH (s:StructuredRecord)-[:BELONGS_TO_CASE]->(c:Case)
WHERE s.record_type='fir_section' AND s.act='PPC' AND s.section_code='302'
RETURN count(DISTINCT c.case_id)                             -- 10
```

**Gold challenged — KB9.** Ten distinct FIRs cite PPC 302
(`fir-409-26`, `fir-421-26`, `fir-202-26`, `fir-213-26`, `fir-214-26`,
`fir-431-26`, `fir-218-26`, `fir-340-25`, `fir-466-26`, `fir-77-26`), not
gold's 8. Checked twice by different routes: exactly one `fir_section` row per
case (no double counting), and no `302`-variant section code exists in the
corpus (`section codes containing '302' -> ['302']`). 10 vs 8 is a 25 % gap,
outside the brief's 5–10 % tolerance. **Nothing in this module is tuned toward
8.** This is the third gold figure this wave has challenged from a probe
(Modules 35 and 44 are the others).

### 1.2 What that means for scope

Four of the six figures are reachable through an aggregate that already
exists. **Two are not**, and building them means editing `xagg.py`, which this
module does not own. Per the brief's own rule they are filed as their own
modules — **67** (KB3's officer pairs) and **68** (KB8's challans) — with the
derived numbers attached so neither has to re-probe.

KB1 and KB2 get no plan either, and for a different reason: their data halves
are **schema claims**, not counts ("every FIR entry carries a recording
officer, a report timestamp and complainant details"; "there is no field
anywhere for confession or interview-statement text"). No aggregate states
either, and `case_completeness_scan` — the nearest thing — reports the
*opposite* shape (9 of 73 FIRs missing an incident date, 52 of 73 missing an
investigation status). Wiring it would have put a true-but-different fact where
gold makes a structural claim, on two questions that answer 3/3 today.

### 1.3 The correction the first live sweep forced

This is the substantive finding of the module and it was **measured, not
predicted**.

The first wiring added the aggregate to the tool's **result**, after
`evaluate_relevance()`. KB4, KB5 and KB6 answered with both halves — and
**KB9 abstained 3 of 3**, with the gate's own reason, on all 18 of its
refusals, a variant of:

> *"The retrieved documents describe procedural requirements for conducting
> inquests …"* — and do not say whether our system records one.

That is a **correct** verdict on a compound question judged against
statute-only evidence: half of what was asked genuinely was not in front of
it. The chunk that answers that half existed, 0.6 s away, on the other side of
the gate. The fix is to show it to the gate, which is the same reasoning
`orchestrator.py` already applies with its `_case_record_chunk()` injection.
Both arms are reported in §4.

---

## 2. Change

| File | Why here |
|---|---|
| `src/pipeline/harness/tools/rag.py` | `_KB_DATA_HALF_PLANS` (four question shapes → one canned XAGG sub-query each), `_match_kb_data_half_plan()`, `_run_kb_data_half()`, the three-way gate and the concurrent dispatch in `rag_tool()`, the fold-in before the relevance gate in `_run_retrieval_loop()`, and `_to_evidence_chunk()` honouring an explicit `source_tool`. |
| `src/pipeline/harness/agents/semantic_search.py` | `_COMPOUND_ANSWER_RULE` / `_compound_block()` — one paragraph added to the generation prompt **only** when a data-half chunk is present. |
| `tests/test_kb_statute_retrieval.py` | 14 new tests (§3), including the all-32 equality control. |
| `scripts/module39_kb_runs.py` | The live harness for §4/§6/§7 — a copy of Module 52's, plus recorders for dispatch / answered / dropped. |
| `GOLD_QA_REMAINING_FIXES_PLAN.md` | Module 39's row and section; new rows **67**, **68**, **69**. |

### 2.1 Why the composition sits in `rag.py` and not in the decomposer

`meta_analysis.py::_DECOMPOSITION_PLANS` is the obvious prior art and the shape
here is deliberately copied from it — a question SHAPE mapped to a canned,
deterministically-dispatched sub-query, selected without an LLM call. But
Meta-Analysis is only ever reached on the **XAGG** route, and a legal-KB
question routes to **RAG** → Semantic Search. Sending KB questions to
Meta-Analysis would be a `router.py` change (out of bounds), and would cost
them the entire statutory half Modules 30 and 52 just fixed. So the data half
is composed where the norm half already lives.

Two further departures from Module 29's shape, both deliberate:

- **No router hop.** Module 29's sub-queries are phrased to satisfy
  `router.py`'s deterministic overrides. These are handed straight to
  `xagg_tool()`, so they cost zero router calls and cannot be stolen by an
  override.
- **A runtime family check.** `_run_kb_data_half()` asserts the aggregate that
  answered is the one the plan named, and **drops** it otherwise. Module 33's
  and Module 44's result files each record a sub-query silently landing on the
  wrong `xagg.py` family and returning a confidently wrong answer; injecting
  that into a KB answer would be strictly worse than the missing half.

### 2.2 The gate is three-way

1. `_is_legal_kb_intent()` — the same predicate Modules 8c/30/52 use.
2. `all_cases` scope **and** `include_global` — the same two conditions that
   gate the KB-only scope retry. A case-scoped question, or a caller who
   excluded global material, is byte-for-byte unaffected.
3. A `_KB_DATA_HALF_PLANS` pattern match.

**The AND matters, and CR8 is the proof.** CR8 ("کیا واقعی گھریلو تشدد کی
رپورٹیں…") matches the violence-against-women plan on subject alone, but
`_is_legal_kb_intent(CR8)` is False and it already reaches the same aggregate
on its own XAGG route. A subject-only gate would have added a redundant
dispatch to a question that scores correctly today. This is pinned as a test.

### 2.3 Degradation, and what is deliberately NOT rescued

A permission denial (an investigator has no cross-case reach), an upstream
failure, a timeout against the private `_DATA_HALF_TIMEOUT`, an empty
rendering and a drifted aggregate family **all** return the byte-for-byte
pre-Module-39 answer. All five are pinned.

An **abstaining retrieval is not rescued.** After §1.3's change the gate has
seen the data half and still said no, so such a run has rejected *both*
halves; answering it with a bare corpus figure and no norm would be a new
failure mode — a confident half-answer replacing an honest abstention.

### 2.4 Module 63 is inherited, not fixed

`_is_legal_kb_intent()` misses some paraphrases (Module 63, filed and
unfixed). A paraphrase that never reaches the legal-KB path never reaches this
either. Not touched here; §6 measures where the boundary actually falls.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_kb_statute_retrieval.py tests/test_harness_tool_rag.py \
            tests/test_harness_agent_semantic_search.py -q
124 collected, all passed
```

Adjacent suites, run for blast radius on the shared RAG path
(`test_harness_agent_meta_analysis` 60, `test_xagg` 332, `test_harness_tool_graph`
11, `test_harness_agent_case_summarization` 19, `..._investigative_analysis` 15,
`..._local_search` 20, `test_orchestrator` 81, `test_pipeline` 46): **584
collected, all passed, 1 xpassed** (pre-existing). The full `pytest -q` suite
was deliberately not run.

14 of the 124 are new. The brief's required pins:

- `test_module39_all32_data_half_gate_is_exactly_the_four_compound_questions`
  — the **all-32 EQUALITY control**. Exactly `{KB4: property_register,
  KB5: violence_against_women, KB6: weapon_register,
  KB9: death_investigation_charging}`; the other 28 resolve to `None`, with
  G2, G5, G3, CR7, M2, CR3, G1 and G6 named individually.
- `test_module39_compound_kb_question_returns_a_norm_half_and_a_data_half`
  — KB4's **literal gold question**, All-Cases scope: the result carries at
  least one retrieved statutory chunk **and** exactly one machine-computed
  chunk, the data half is **last** (so every legal chunk keeps its
  `[Document N]` position), and exactly one aggregate was dispatched, with the
  sub-query the plan names.
- `test_module39_the_data_half_is_shown_to_the_relevance_gate` — §1.3's
  correction, pinned so it cannot be undone by accident.

The rest pin: each of KB4/KB5/KB6/KB9 against its own literal gold text
(Urdu script and Roman-Urdu included); CR8's two-clause proof; every plan
sub-query resolving to the aggregate it names via `resolve_aggregate_kind()`;
the property sub-query being byte-identical to Module 33's pinned string
(third copy, via Module 50's `_SQ_SEIZED_PROPERTY`); unique plan names and
kinds; KB1/KB2/KB3/KB8 and a plain case-narrative question dispatching nothing;
a case-scoped caller getting nothing; the five degradation paths; the
abstention not being rescued; one aggregate call across scope retries and
evaluator rounds; and `_compound_block()` firing only on a data-half chunk and
containing none of gold's own figures.

---

## 4. Live verification — all eight KB questions, three runs each, two arms

Same session shape as `gold32_run.py` (platform-admin, All Cases, no
`case_id`), sequential, same backend, same private Chroma. `route=` is read
from the `supervisor:dispatch` SSE event; `rounds` from `backend.log`'s
`N chunk(s) to evaluator (attempt K)` lines for exactly the window each request
occupied; the dispatch/answer of the data half from `rag.py`'s own two log
lines. Full per-run records:
`module39_runs.json` (arm B), `module39_runs_arm1.json` (arm A),
`module39_regression.json`, `module39_paraphrase.json`.

- **Arm A — result-only** (the data half added after the relevance gate).
- **Arm B — gate-side**, the code that ships (§1.3).

### 4.1 Dispatch: the gate fired exactly where it was designed to

**24 of 24 runs in each arm.** The data half was dispatched on **KB4, KB5, KB6,
KB9 and on no other question**, in both arms — 12 dispatches per arm, 12
aggregates returned, **0 dropped** (no timeout, no denial, no family mismatch,
no failure) across all 24 dispatches plus 6 more in §6. KB1, KB2, KB3 and KB8
dispatched nothing, as designed.

### 4.2 The per-question both-halves table (arm B, the shipped code)

`L` = the governing provision and its substance, in any words. `D` = a figure
or finding read off our own records. `n of 3`.

| | route | sub-queries dispatched / returned | answered | **norm half L** | **data half D** | measured figure vs gold |
|---|---|---|---|---|---|---|
| KB1 | RAG 3/3 | 0 / 0 (by design) | 3/3 | **3/3** (CrPC s.154) | 0/3 | — (no aggregate; §1.2) |
| KB2 | RAG 3/3 | 0 / 0 (by design) | 3/3 | 0/3 (QSO 38/39 never retrieved — **Module 65**) | 0/3 | — (schema claim; §1.2) |
| KB3 | RAG 3/3 | 0 / 0 (**Module 67**) | 2/3 | 1/3 | **0/3** | not computed — 68/74 derived by hand, §1.1 |
| KB4 | RAG 3/3 | 3 / 3 | **3/3** | **3/3** (PPR 22.16; **27.16 named in 2 of 3**) | **3/3** | **45 entries / 28 FIRs** = gold's 45 ✅ |
| KB5 | RAG 3/3 | 3 / 3 | 1/3 | 1/3 (Anti-Rape Act, female officer, ARCC) | **1/3** | **8 reports** = gold's 8 ✅ |
| KB6 | RAG 3/3 | 3 / 3 | 2/3 | **2/3** (packaging, sealing, marking) | **2/3** | **32 weapons** = gold's 32 ✅ |
| KB8 | RAG 3/3 | 0 / 0 (**Module 68**) | 3/3 | **3/3** (CrPC s.173) | **0/3** | not computed — 26 derived by hand, §1.1 |
| KB9 | RAG 3/3 | 3 / 3 | 2/3 | **2/3** (CrPC s.174, PPR 25.31/25.35) | **2/3** ⚠ | present but **not gold's figure** — see §5.2 |

**Headline: the data half went from 0 of 48 runs (Module 52, both arms) to
8 of 12 runs on the four questions that have an aggregate to reach.** On the
other four it is still 0, and §1.2/§1.1 say exactly why for each.

### 4.3 Arm A vs arm B — what showing the gate the data half bought

| | arm A (result-only) | arm B (gate-side) |
|---|---|---|
| KB4 answered / D | 2/3 · 2/3 | **3/3 · 3/3** |
| KB5 answered / D | 2/3 · 2/3 | 1/3 · 1/3 |
| KB6 answered / D | 3/3 · 3/3 | 2/3 · 2/3 |
| KB9 answered / D | **0/3 · 0/3** (abstained 3/3, 6 rounds each) | **2/3 · 2/3** |
| all-24 answered | 18/24 | **19/24** |
| all-24 mean rounds | 2.29 | 2.33 |
| all-24 mean runtime | 314.8 s | 255.3 s |

KB9 is the named winner: **abstained 3/3 at 6 evaluator rounds and ~530 s
→ answered 2/3 at 1–3 rounds and ~230 s.** KB4 is the second: 2/3 → **3/3**,
which also un-does the regression Module 52 recorded for KB4 in its own §5.2
(0/3 there).

KB5 and KB6 each lost one run, and both losses are the **verifier**, not the
gate or retrieval — verbatim, *"The generated answer could not be verified as
grounded in the retrieved documents."* §5.3 has the reason and files it.

### 4.4 Verbatim answers (arm B, run 1 of each)

**KB4** — both halves, and gold's own rule number:

> **Part 1: Governing Legal Requirements**
> - **Rule 22.16(1)** of the Punjab Police Rules mandates that police seize
>   property in criminal cases … - **Rule 22.16(4)** and **Rule 27.16(1)**
>   (Documents 2, 3) specify that unclaimed property must be handed over to the
>   sheriff, with receipts recorded in register No. 1 …
>
> **Part 2: Case Records Compliance**
> - **Document 7** (our case records summary) shows:
>   - **Disposition of seized property**: 45 entries across 28 FIRs, including
>     confiscation (14 items), forensic lab dispatch (13 items), holding for
>     heirs (7 items), and other methods.

**KB6** — the data half, and gold's own verdict that the register holds none of
the handling steps:

> **Part 2: Weapon Register Compliance** … - **30 of 32 weapons** (~94 %) are
> recorded **without a licence**. - **2 weapons** have **no licence status** at
> all. - **No field** in the weapon register indicates whether the above
> forensic handling guidelines were followed (e.g., packaging, labeling,
> documentation) … Thus, the weapon register does **not** record whether the
> guidelines were applied.

**KB5**:

> **Part 2: Case Records** — Our case records (Document 7) show: **4 out of 8
> domestic-violence reports** have **forwarded FIR numbers** matching real,
> active FIRs (e.g., `fir-97-26`, `fir-416-26`). **4 reports** have **no
> forwarded FIR number**. The records do **not explicitly mention** whether
> female police officers were present during statements …

**KB9** (run 2 — the run that reaches gold's section):

> **Part 2: What Our Case Records Actually Show** … - The 73 cases involving
> death are charged under FIR sections (e.g., PPC §302, §392, Arms Ordinance
> §13) but **do not include data** on whether police conducted Section 174
> investigations or completed inquest reports.

**KB3 and KB8** — the two the module deliberately did not wire, answering
exactly as the brief predicted they would:

> *(KB3)* "Regarding whether this matches actual practice, the documents do not
> provide data or examples of real-world implementation."
>
> *(KB8)* "**Case-tracking data** is not discussed in the documents, so it
> cannot be confirmed whether such data exists or would indicate delays."

---

## 5. Gold comparison, the divergence, and the cost

### 5.1 The three figures that reproduce gold exactly

KB4's **45**, KB5's **8** and KB6's **32** appear in the answers, come from a
machine-computed aggregate, and match gold's numbers exactly — not within
tolerance, exactly. KB6 additionally reproduces gold's own *negative* finding
(the register records nothing about handling), which the brief's standard
scores as a pass because gold agrees; so does KB5's "chain of custody is not
recorded".

### 5.2 KB9 — the data half is present and it is not gold's figure

Two separate things, both reported rather than smoothed:

1. **Gold's 8 is wrong.** §1.1's probe says **10** FIRs cite PPC 302, twice by
   different routes.
2. **The answer states neither.** `statute_court_stage_join` is the only
   aggregate publishing a per-section case count, and its rendering is a
   ~2.3 KB two-view report whose statute list is capped at 15 rows; `PPC §302:
   10 case(s)` is row 6 of that list. Run 2 names PPC §302 as a charged section
   but not its count; run 1 read past it and asserted that *none* of the listed
   sections pertains to death — **factually wrong**, from a chunk that contains
   the right row.

So KB9's D column is "present" in the sense this module measures (the answer
now speaks about our records at all, where 6 of 6 Module 52 runs did not), and
"not gold's" in the sense that matters to a scorer. The honest reading is that
KB9 needs a **dedicated "how many FIRs cite section X" aggregate**, not a
whole-caseload statute report — filed as **Module 69**. It is not fixed here
for the same reason 67 and 68 are not: it is an `xagg.py` change.

### 5.3 KB5 and KB6's lost runs — the verifier, and it is Module 61's shape

Three of the twelve compound runs ended in a verifier rejection. The reasons,
verbatim from `backend.log`:

> *(KB4, arm A)* "Claims about the absence of specific record-keeping practices
> in 'our records' are not supported by any chunk, which only outline legal
> requirements …"
>
> *(KB5, arm A)* "The claim about the Code of Criminal Procedure lacking
> gender-specific measures is not supported by any chunk …"

The verifier **is** handed the data-half chunk (`semantic_search.py` passes
every chunk in `tool_result.chunks`), so this is not a plumbing bug. It is the
defect Module 61 already has open in a different place: a **negative inference
over a complete listing** — "our register has no field for X" is exactly the
shape of CR3's "FIR 65/26 is absent from the linkage list", and the verifier
refuses both. Compound KB answers make that shape much more common, because
half of what gold itself says is a negative. **Module 61's scope widens; it is
not a new defect**, and the plan row is annotated accordingly rather than a new
number being minted.

### 5.4 Cost — measured, and it is not detectable

The brief asked specifically because Module 53 measured the Meta-Analysis
sub-query deadline as a *shared* wall clock. This deliberately does not
reproduce that shape: the aggregate has its own private deadline, is dispatched
concurrently with retrieval, and is memoised across scope retries and evaluator
rounds.

| | value |
|---|---|
| Aggregate wall time, all 16 dispatches on the shipped code | **mean 0.59 s, max 0.88 s** (`dv_report_fir_match` 0.50 s, `weapon_compliance_scan` 0.49 s, `seized_property_disposition` 0.59 s, `statute_court_stage_join` 0.86 s) |
| Arm B mean runtime, the 4 questions **with** a data half | **256.1 s** |
| Arm B mean runtime, the 4 questions **without** one | **254.5 s** |
| Difference | **+1.6 s (+0.6 %)** — inside this stack's own run-to-run spread |

The compound questions are, if anything, *faster* than the same questions were
in arm A (KB9 530 s → 230 s), because the gate stops burning six retrieval
rounds on a half it could not see. Wall-clock numbers here carry Module 52's
caveat — a second backend was live — but the aggregate's own 0.59 s is measured
from `rag.py`'s two log lines in the same process and is not subject to it.

---

## 6. Non-gold paraphrase

Two, written **before** the live sweeps and not adjusted afterwards. Both move
deliberately away from the gold wording, including its language.

**KB4P** (Roman-Urdu; gold KB4 is Urdu script, and this says *maal khana*
where gold says *پراپرٹی ریکارڈ*):
> *"Kya koi qanooni zabta maujood hai ke police jo maal apni tehveel mein le,
> usay kaise register kiya jaye aur baad mein kaise tehleel kiya jaye — aur kya
> hamara maal khana record us par amal karta hai?"*

**KB6P** (English; gold KB6 is Roman-Urdu, and this says *firearm* / *packed*
where gold says *aslaha* / *sambhala*):
> *"Do the forensics guidelines set any rule for how a recovered firearm must
> be packed before it is entered in the record, and does our own weapon
> register show whether that was done?"*

| | route | dispatched / returned | answered | D present |
|---|---|---|---|---|
| KB4P | RAG 3/3 | 3 / 3 (`seized_property_disposition`) | **3/3** | **3/3** — 14/13/7/6 items across 28 FIRs, in the register's own Urdu wording |
| KB6P | RAG 3/3 | 3 / 3 (`weapon_compliance_scan`) | 2/3 | **2/3** — 30 of 32 weapons |

**The capability is not tied to the gold string.** Not one content word of
KB4's Urdu phrasing survives in KB4P and the data half still lands, 3 of 3.

**And an unexpected result worth recording:** KB6P **run 1 and run 3 retrieved
gold's exact statutory specifics** — *"Unloaded with the safety on, and without
live rounds in the chamber, magazine, or parcel"* — which is Module 64's open
defect ("KB6's `c19`/`c18` window is still not retrieved from KB6's own
wording", 0 of 3 there). This module did not fix Module 64 and does not claim
to; it independently reproduces Module 52's own observation that a paraphrase
of KB6 reaches the window its gold wording does not. Noted on Module 64's row.

Two further paraphrases (KB5P, KB9P) were checked against the gate statically
and both match their intended plan; they were not run live.

---

## 7. Regression guard

The five questions most likely to be caught by a widened gate (**G2, G5, G3,
CR7, M2**) plus the three that live in the decomposition machinery this module
borrows its shape from (**CR3, G1, G6**), one live run each, same backend, same
session shape.

| | route | data half dispatched | outcome |
|---|---|---|---|
| G2 | XAGG | **no** | ✅ 9 FIRs with no incident date, 52 with no investigation status — unchanged |
| G5 | XAGG | **no** | ✅ 30 of 32 weapons without a licence, 2 with no status — unchanged |
| G3 | XAGG | **no** | ✅ Urdu, the three court-readiness gaps — unchanged |
| CR7 | XAGG | **no** | ✅ 1 of 33 criminal records with a verdict, 32 in progress; FIR 891-24 consistent — unchanged |
| M2 | XAGG | **no** | ✅ 15 general-purpose stations 7→39 vs 2 specialised 3→5 — Module 44's answer, unchanged |
| CR3 | XAGG | **no** | ✅ "not handled identically", FIR 64/26 has CMS-ISB-2026-0341, FIR 65/26 does not — gold's verdict |
| G1 | XAGG | **no** | ⚠️ *"The synthesized answer could not be verified as grounded in the sub-answers"* — the pre-existing Meta-Analysis synthesis-verifier rejection (Modules 53/57/61), **not** reachable from this module's code: G1 never enters `rag.py` |
| G6 | XAGG | **no** | ✅ full orientation note, districts + case mix + arrest rate — unchanged |

**All eight route to XAGG and none of them enters the RAG tool at all**, so
this module's code is not merely inert for them, it is unreachable. Combined
with the all-32 equality control in §3, the negative side is covered both
statically (32 questions) and live (8 questions).

---

## 8. New defects found — filed, not fixed

| # | Defect | Evidence from this module |
|---|---|---|
| **67** | KB3's officer-pair figure has no aggregate — the question resolves to `unsupported_officer` | The figure is computable and **reproduces gold exactly**: 68 of 74 investigating assignments name the case's own recording officer, 91.9 %, with 3 genuine splits (`fir-205-26`, `fir-245-26`, `fir-88-26`) and 3 investigating-only cases = gold's "only 6". Needs an `xagg.py` family; one line here when it lands |
| **68** | KB8's "26 challans sent to court" has no aggregate — resolves to `criminal_record_court_crosscheck`, which counts 33 criminal records | `chalaan_dispatch` is 26 rows across 26 distinct cases, exactly gold. `xagg.py` reads `chalaan_outcome` (20) and never `chalaan_dispatch`. Deliberately left unwired rather than answered with the wrong number |
| **69** | KB9 needs a "how many FIRs cite section X" aggregate; `statute_court_stage_join` is the wrong grain | Its 15-row capped statute list carries `PPC §302: 10 case(s)` as row 6; one live run named the section without its count and another read past it and asserted no death-related sections exist. **Gold's own 8 is also wrong — the corpus has 10** |
| — | **Module 61 widens** (no new number) | Three of twelve compound runs died on the verifier refusing a *negative inference over a complete listing* — "our register has no field for X" — the same shape as CR3's "FIR 65/26 is absent". Compound KB answers make that shape routine, because half of what gold says is itself a negative |
| — | **Module 64 corroborated** (no new number) | KB6P, an ordinary English paraphrase, retrieves gold's *"unloaded … safety on … no live rounds"* window on 2 of 3 runs, which KB6's own wording does not reach |

**Not filed, and worth saying so:** no new defect was found in the gate's
selection (the all-32 control is exact), in the dispatch (0 of 30 dropped), or
in the cost (0.59 s mean, +0.6 % end to end).
