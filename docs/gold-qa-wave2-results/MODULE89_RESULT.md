# Module 89 — KB1: the compound question's data half

**Question (KB1, `language=en`, `route=RAG`):**

> *"What legal requirement governs how a report of a crime becomes a formal FIR,
> **and does our recordkeeping actually follow it?**"*

**Before:** 0.30 on all three of Module 27's passes, AnswerRelevancy 1.00.
**After:** see §4.

Branch `fix/kb1-data-half-plan`, worktree `D:/Rapids AI/muhafiz-m89`, branched
from `origin/main` at `ee79e22`.

---

## 1. Root cause, and the three coverage figures re-derived

### 1.1 The defect

The law half has been right since Module 30. All three captured passes in
`evaluation/gold32_pass{1,2,3}_outputs.json` are **byte-identical** — this is
deterministic, not a flaky draw — and the second half of every one of them
reads:

> *"Regarding whether the recordkeeping actually follows this requirement, the
> documents do not provide specific information on whether the recordkeeping
> practices are compliant with the legal requirements."*

That is a correct statement about the **statute corpus**, which is all the RAG
sub-agent can see: seven English PDFs, no database access. Gold's second half is
a claim about **our graph**. Nothing on the RAG route was computing it, so the
answer could only decline.

**This is Module 39's defect on a question Module 39 deliberately excluded.**
Its result file records KB1 and KB2 as unwired because their data halves are
*"schema claims, not counts"*. That call was **right for KB2** — gold there is
*"no, by design"*, and it is Module 96's problem. It was **wrong for KB1**,
whose gold asserts three checkable coverage figures.

### 1.2 The figures, re-derived (the brief's table is correct)

Derived directly against `evidence_graph` **before any code was written**, not
inherited from the brief:

```sql
LOAD 'age'; SET search_path = ag_catalog, public;
-- cases
MATCH (c:Case) RETURN count(c)                                       -- 73
-- complainant details
MATCH (p:Person)-[r:INVOLVED_IN {role:'complainant'}]->(i:Incident)
      -[:PART_OF]->(c:Case) RETURN count(DISTINCT c)                 -- 73
-- recording officer
MATCH (:Officer)-[:ASSIGNED_TO {role:'recording'}]->(c:Case)
      RETURN count(DISTINCT c)                                       -- 70
-- report timestamp
MATCH (i:Incident)-[:PART_OF]->(c:Case)
      WHERE i.report_datetime IS NOT NULL AND i.report_datetime <> ''
      RETURN count(DISTINCT c)                                       -- 64
```

| s.154 element | coverage | gaps |
|---|---|---|
| complainant details | **73 / 73** | — |
| recording officer | **70 / 73** | `fir-117-26`, `fir-954-26`, `fir-955-26` |
| report timestamp | **64 / 73** | `fir-205-26`, `fir-211-26`, `fir-340-25`, `fir-406-26`, `fir-410-25`, `fir-422-26`, `fir-457-26`, `fir-467-26`, `fir-77-26` |

**All three reproduce, and so do gold's "3 missing" and "9 missing".** The two
gap sets are disjoint, so **61 of 73** cases carry all three — a fourth figure
gold's *"present for most cases"* rests on and which is **not** the minimum of
the three.

**One correction to the brief's schema sketch, and it matters.** The brief gives
the complainant edge as `Person-[:INVOLVED_IN {role:'complainant'}]->Case`. It is
not: `INVOLVED_IN` runs **Person → Incident** (221 edges, `Person`→`Incident`
only), and the case is reached through `[:PART_OF]`. Written as the brief has
it, the query returns **0**, which is how this was caught. The corpus is 1:1
(73 incidents, 73 cases), so the figure is the same once the walk is added — but
the walk is what makes the three counts share a denominator rather than happen
to agree, and it is what the aggregate does.

**`complainant_cms` is deliberately excluded.** The graph carries 6 role values
on `INVOLVED_IN`: `complainant` (73), `accused` (94), `witness` (37), `victim`
(9), `applicant_pkm` (4), `complainant_cms` (4). The last is CR6's walk-in CMS
complaint — a different record type on the same edge. Counting it would inflate
the numerator with something s.154 says nothing about, and would break gold's
73.

### 1.3 What the schema cannot say, and why that is not a gap

s.154 also requires the statement to be **read back** to the informant and
**signed** by them. Neither has a field anywhere in this schema. The aggregate
reports those as **unmodelled**, not as missing — an unrecorded step is not a
skipped one, and gold does not claim it is. Same honesty rule Module 75 applied
to KB8's absent interim-report record type.

---

## 2. The change

There was **no aggregate producing these figures**, so this is not a one-entry
plan addition: the aggregate came first, exactly as Modules 74/75/76 did for
KB3/KB8/KB9, and the plan entry is the landing step Module 77 was for those.

### Files touched

| File | Change |
|---|---|
| `src/pipeline/xagg.py` | `_FIR_REGISTER_TERMS` / `_FIR_REGISTER_FIELD_TERMS` + `_is_fir_register_completeness()`; `_S154_UNMODELLED_ELEMENTS`; `_fir_register_completeness()` (the aggregate, 4 Cypher reads) and `render_fir_register_completeness()`; one line in `resolve_aggregate_kind()`'s chain; one branch in `run_aggregate()`'s dispatch |
| `src/pipeline/harness/tools/xagg.py` | renderer import, the hand-maintained `AggregateKind` Literal (**twelfth** family), and the third rendering branch |
| `src/pipeline/orchestrator.py` | renderer import and **both** twin XAGG rendering branches (~line 545 and ~line 2230) |
| `src/pipeline/harness/tools/rag.py` | `_KB_DATA_HALF_PLANS` entry **(7)**, `fir_register_completeness` |
| `tests/test_xagg.py` | Module 89 block: the pinned sub-query constant, 8 aggregate/renderer tests, `TestModule89Dispatch` |
| `tests/test_harness_tool_rag.py` | Module 89 block: plan firing, the sub-query pin against `resolve_aggregate_kind()`, the four pre-registered paraphrases, the three measured over-matches |
| `tests/test_kb_statute_retrieval.py` | the existing all-32 plan-gate control moved KB1 from the negative list to `_EXPECTED_DATA_HALF_GATE` |
| `docs/gold-qa-wave2-results/module89_paraphrases.json` | the four paraphrases, committed before the first run |
| `scripts/module89_live_runs.py` | live runner (a wrapper over Module 74's, so both arms use identical machinery) |
| `GOLD_QA_REMAINING_FIXES_PLAN.md` | row 89 + its section; new defect **97** |

**Module 91's de-duplication does not cover these branches.** Checked on `main`
first, as the brief asked: `render_officer_role_pair_overlap`,
`render_chalaan_dispatch_count` and `render_fir_section_case_count` each still
have **three** call sites (`harness/tools/xagg.py` once, `orchestrator.py`
twice). Module 91 de-duplicated the *placeholder-officer* rendering, not this
family. So this module follows the established convention — one shared renderer
in `xagg.py`, imported by all three sites — rather than inventing a fourth
copy or a fourth pattern.

### Placement in the dispatch chain, in both directions

`resolve_aggregate_kind()` is the single source of dispatch truth (Module 41),
ordered and first-match-wins, so **where** matters more than **what**.
`_is_fir_register_completeness()` sits **immediately above**
`_COMPLETENESS_KEYWORDS` (G1/G2's `case_completeness_scan`):

- **Below** every subject-specific family above it (challan, criminal record,
  DV, CMS linkage, court readiness). None carries a register-FIELD term.
- **Above** `case_completeness_scan`, the nearest neighbour in meaning. That
  family asks which cases are weak overall and reads the gateway's case rows;
  this one asks whether one specific statutory register entry is present, off
  the graph. G1 and G2 both score today and both keep it.

The predicate needs **two** signals — a register/recordkeeping term **and** an
s.154 register field. One signal over `fir register` alone would claim half this
corpus; one over `complainant` alone would claim CR6's family.

### What was NOT changed, and why

- **`router.py`** — owned by `muhafiz-m92`, and not needed: KB1 already routes
  RAG (Module 27, 3/3; re-measured here, §4).
- **KB2** — Module 39's exclusion stands. Gold there is *"no, by design"*;
  Module 96 owns it. Pinned by a test.
- **Module 93's CR6 override** — `_XAGG_OVERRIDE_PATTERNS`' `complaint … FIR`
  entry still intercepts one KB1 paraphrase before RAG. Reported in §6, not
  fixed: the brief says do not take Module 93 on.
- **`_is_officer_role_pair_comparison()`** — found broken for ordinary KB3
  rewordings while probing boundaries. Filed as Module 100 (§8), not fixed.
- **No gold text was changed.** Gold was already corrected in PR #57 and this
  module's independent derivation agrees with it.
- **Nothing is narrowed toward KB1.** No figure in the aggregate is produced by
  a rule chosen to hit 73/70/64; each element is a presence test over the same
  case set.

---

## 3. Unit tests

`tests/test_xagg.py` — 19 tests; `tests/test_harness_tool_rag.py` — 13 tests.

**Both directions were checked.** With `src/pipeline/{xagg.py,orchestrator.py,
harness/tools/{rag,xagg}.py}` stashed back to `origin/main` and the test files
left in place, **23 of the 32 fail** (the aggregate, the renderer, the
predicate, the plan, the sub-query pin, all four paraphrases, and the all-32
plan-equality control); the rest are boundary assertions that are true either
way and are kept because they pin behaviour this module must not change. With
the source restored, **32/32 pass**.

Highlights:

- `test_module89_counts_the_three_s154_elements_over_one_case_denominator` —
  the three counts and `fully_complete_count`, which is not the minimum.
- `test_module89_a_cms_complaint_is_not_a_register_complainant` — the
  `complainant_cms` exclusion.
- `test_module89_a_blank_report_timestamp_counts_as_missing` — a
  projected-but-empty timestamp.
- `test_module89_an_edge_outside_the_case_set_cannot_inflate_a_count` — every
  element is intersected with the denominator's case set.
- `test_module89_renderer_states_the_verdict_and_names_both_gaps` — *"in outline
  but not completely"*, both gap lists, and the unmodelled-steps sentence.
- `test_module89_emits_its_xagg_log_line_with_the_figures` — Module 55's
  convention, with the coverage figures in the line.
- **`test_module89_sub_query_is_pinned_against_resolve_aggregate_kind`** — the
  test the brief asked for: the plan's sub-query is checked against
  `resolve_aggregate_kind()`, so a drift is a loud failure rather than a silent
  0-of-N (`_run_kb_data_half()` drops a data half whose aggregate is not the one
  the plan named — which is exactly what the pre-fix stub run in §3 of the
  regression log showed it doing).
- `test_module89_sub_query_is_byte_identical_to_the_pinned_string_in_test_xagg`
  — the Module 74/75/76 convention: the aggregate's own test file owns the
  string, the plan copies it.

Broader suites, all green: `test_xagg.py`, `test_harness_tool_rag.py`,
`test_kb_statute_retrieval.py`, `test_router.py`, `test_orchestrator.py`,
`test_harness_tool_xagg.py`.

---

## 4. Live verification

Backend on `:8089`, real `/api/chat`, SSE parsed for the `route=` event. Two
log lines out of `backend.log` per run, because the SSE stream says only
`route='RAG'` and never which aggregate ran (Module 55):

- `RAG tool: KB data-half plan '<plan>' answered by aggregate '<kind>' (N chars)`
- `XAGG <kind>: ...` with the figures.

Runner: `scripts/module89_live_runs.py`, a wrapper over Module 74's, so this
arm and Modules 74/75/76/77's own numbers were measured by identical
machinery. `CHROMA_PERSIST_DIR` was pointed at the **main repo's** store
(`/health` reports `documents_in_store: 7716`) — a worktree-relative path
silently creates an empty one.

### 4.1 Before — deterministic, and read from the evaluation capture

Module 27's three passes are **byte-identical** for KB1 (`route=RAG` 3/3). The
second half of every one:

> *"Regarding whether the recordkeeping actually follows this requirement, the
> documents do not provide specific information on whether the recordkeeping
> practices are compliant with the legal requirements. Therefore, it cannot be
> determined from the provided documents whether the recordkeeping actually
> follows the legal requirements."*

Not one figure in it. This is the arm the judge scored 0.30 ×3.

### 4.2 After — KB1's exact gold question, 3 runs

| run | route | plan consulted? | aggregate | data half in answer? | status | elapsed | quota lines |
|---|---|---|---|---|---|---|---|
| 1 | **RAG** | **fired** | `fir_register_completeness` (870 chars) | **yes** | done | 117.9 s | 0 |
| 2 | **RAG** | **fired** | `fir_register_completeness` (870 chars) | **yes** | done | 41.4 s | 0 |
| 3 | **RAG** | **fired** | `fir_register_completeness` (870 chars) | **yes** | done | 200.5 s | 0 |

**3 of 3, and all three answers are byte-identical to each other** (1603 chars)
— the same determinism the before arm had, now with the figures in it.

The `XAGG` line, identical on all three runs:

```
XAGG fir_register_completeness: 73 case(s); complainant 73/73;
recording_officer 70/73; report_datetime 64/73; all three elements present on
61 case(s); 2 s.154 element(s) unmodelled in the schema
```

### 4.3 The composed answer, verbatim (run 3)

> ### Legal Requirement:
> The legal requirement governing how a report of a crime becomes a formal FIR
> is outlined in **Section 154 of the Code of Criminal Procedure**. According to
> this provision, every information relating to an offence, whether cognizable
> or non-cognizable, must be recorded in writing by the officer in charge of a
> police station. This includes entering the substance of the information in the
> police station daily diary, which is the book provided for the purpose
> [Document 2].
>
> ### Recordkeeping Compliance:
> Our recordkeeping follows the structure outlined in Section 154 for most cases
> but not completely. Specifically:
> - **Complainant details** are recorded in all 73 cases.
> - **The officer who recorded the report** is recorded in 70 of 73 cases, with
>   3 cases missing this information (fir-117-26, fir-954-26, fir-955-26).
> - **The time the report was made** is recorded in 64 of 73 cases, with 9 cases
>   missing this information (fir-205-26, fir-211-26, fir-340-25, fir-406-26,
>   fir-410-25, fir-422-26, fir-457-26, fir-467-26, fir-77-26).
> - All three elements … are present together in 61 of 73 cases, meaning the
>   structural equivalent of the Section 154 register entry is present for most
>   cases but not all [Document 6].
>
> Additionally, two of Section 154's steps have no field in the current schema —
> that the statement was read back to the informant, and that the informant
> signed it — so the records can neither confirm nor contradict those steps;
> this is an unmodelled step, not a skipped one [Document 6].

**The two halves compose.** They did not have to: the brief warned that a plan
can fire into a void, and Module 77 measured the grounding verifier rejecting a
composed KB answer 1 run in 3 (filed as Module 85). That did not happen here —
`status=done` 3/3, no verifier rejection, no quota line over the whole session.

### 4.4 The canned sub-query through the real `/api/chat`

`SQ89` was sent directly, which is the only thing that exercises the harness
wrapper's hand-maintained `AggregateKind` Literal — a trap Module 74's comment
records as having bitten four times, always as an EMPTY answer with
`status=None`, invisible to every unit test. See §6's table for the result.

---

## 5. Gold comparison

Judged on substance, not wording, per the brief's standard.

| gold claim | live answer | verdict |
|---|---|---|
| CrPC s.154 is the governing requirement | *"Section 154 of the Code of Criminal Procedure"* | **match** |
| oral information reduced to writing by the officer-in-charge | *"must be recorded in writing by the officer in charge of a police station"* | **match** |
| entered in a register in the prescribed form | *"entering the substance of the information in the police station daily diary, which is the book provided for the purpose"* | **match in substance** — the daily diary is the register, named from the retrieved text |
| read back to the informant, signed by them | named explicitly, as steps the schema has **no field for** | **addressed** — see below |
| "follows that structure in outline but not completely" | *"follows the structure outlined in Section 154 for most cases but not completely"* | **match** |
| all 73 cases carry complainant details | *"recorded in all 73 cases"* | **exact** |
| 70 of 73 carry a recording officer | *"70 of 73 cases"*, and it names all 3 gaps | **exact, plus** |
| only 64 of 73 carry a report timestamp | *"64 of 73 cases"*, and it names all 9 gaps | **exact, plus** |
| "3 cases missing the recording officer and 9 missing the report time" | stated, with case ids | **exact** |
| "present for most cases, with…" | *"present together in 61 of 73 cases … present for most cases but not all"* | **match, sharper** |

**Every figure gold asserts is reproduced exactly.** Nothing was tuned to make
that happen: the three counts were derived from the graph before the aggregate
existed, and gold had already been corrected independently in PR #57.

**One deliberate divergence, in gold's favour rather than ours.** Gold's law
half lists "read back … signed" as part of the requirement and then says our
records follow the structure "in outline". The live answer says the same and
adds that those two steps have **no field at all** in this schema. That is more
than gold says, and it is the more honest statement: without it, "follows in
outline" could be read as a claim that we checked read-back and signature and
found them present. Module 75 took the same line on KB8's absent
interim-report record type and Module 71's work is the same instinct.

---

## 6. Non-gold paraphrases

**Pre-registered.** All four were written down and committed to
`docs/gold-qa-wave2-results/module89_paraphrases.json` **before the first run**,
with `written_before_any_run: true`, and a unit test asserts the copies in
`tests/test_harness_tool_rag.py` are byte-identical to that file — so none of
them can be quietly softened after a miss. Two English, one Roman-Urdu, one
Urdu-script.

**Route and "plan fired" are reported separately.** Module 94 exists because
they were conflated. A question can route to RAG and fire nothing; a question
can also fire nothing because it never reaches RAG at all. Both happen below.

### 6.1 Live, 2 runs each

| id | lang | route | plan consulted? | `XAGG` line | 73/70/64/61 in the answer? |
|---|---|---|---|---|---|
| **P1** | en | **XAGG 2/2** | **never** | `XAGG relational_aggregate` | **0/2** |
| **P2** | en | **RAG 2/2** | **fired 2/2** | `XAGG fir_register_completeness` | **2/2** |
| **P3** | roman-ur | **RAG 2/2** | **fired 2/2** | `XAGG fir_register_completeness` | **2/2** |
| **P4** | ur | **RAG 2/2** | **fired 2/2** | `XAGG fir_register_completeness` | **2/2** |
| `SQ89` | (the canned sub-query) | XAGG 2/2 | n/a — dispatched directly | `XAGG fir_register_completeness` | **2/2** |

`status=done` on all 10, **0 quota lines**.

### 6.2 P1 is a miss, and it is reported rather than smoothed

> *"Under what law does a complaint made at a police station have to be turned
> into a written FIR, and do our own case records actually meet that standard?"*

Two separate things went wrong with P1, and only one of them is this module's:

1. **A pattern miss, found by measurement and fixed.** The first draft of entry
   (7)'s "X becomes an FIR" pattern demanded a literal `becomes?`. P1 says
   *"turned into"* and missed outright, offline, before any live run. That is
   **Module 56's finding for the fifth time**, so the verb list is now an
   alternation (`becomes | turned into | converted into | treated as | registered
   as | recorded as | counts as | amounts to`) and the comment in `rag.py` says
   it was widened *after* the measurement, not before. **The paraphrase was not
   adjusted; the pattern was.**
2. **A routing interception, which is Module 93 and is NOT fixed here.** With
   the widened pattern P1 now matches the plan offline —
   `_match_kb_data_half_plan(P1).name == "fir_register_completeness"` and
   `_is_legal_kb_intent(P1) is True` — and it still fires nothing live, because
   it never reaches RAG. `router.py`'s `_XAGG_OVERRIDE_PATTERNS` entry **#29**
   (Module 15, question CR6) —
   `\b(complaint|walk[- ]?in)\b.{0,60}\b(f\.?i\.?r\.?|linked|attached|sepa…`
   — claims it and sends it to XAGG, where it lands on
   `relational_aggregate` and comes back with no statutory half and no figures.

**That is Module 93 reproduced live, on a second and independently-written KB1
paraphrase.** Module 93 was filed by Module 78 off *"What rule decides when a
written complaint has to be turned into a formal FIR…"*; P1 is a different
sentence with the same shape and the same fate. The brief says do not take
Module 93 on, so this module does not touch `router.py` — but the interaction
is now measured twice rather than once, and the RAG-side half of it is already
closed: **the moment that route is fixed, P1 fires the plan**, because the
offline match already holds.

### 6.3 What P3 says that is worth noting

P3 is Roman-Urdu. Module 92 measured that a Roman-Urdu paraphrase keeps its
deterministic XAGG route **0 of 8** times; this one keeps `route=RAG` **2 of 2**
and fires the plan both times. That is not a contradiction — Module 92's probe
is about questions that take an XAGG *override* at their gold wording, and KB1
takes none — but it is worth recording that the RAG side generalises here where
the XAGG side does not.

---

## 7. Regression guard

This module inserts into an **ordered, first-match-wins** chain in two different
places (`resolve_aggregate_kind()` and `_KB_DATA_HALF_PLANS`). A regression here
is silent and broad, and the brief records that skipping this step is how
regressions have shipped before. Three controls, two of them exhaustive.

### 7.1 The all-32 dispatch equality control — exhaustive, offline

`tests/test_xagg.py::test_module56_all_32_gold_questions_resolve_exactly_as_before`
already pins `resolve_aggregate_kind()` for every one of the 32 gold questions
as an **equality**, not an absence. It passes **unchanged** — no entry added to
`_GOLD32_KINDS_MOVED_BY_MODULES_74_TO_76`, because **0 of the 32 questions
move**. KB1's own gold text still resolves to `station_or_category_counts`,
exactly as KB4/KB8/KB9's do: a gold question reaches this family only through
the canned sub-query `rag.py` dispatches, never on its own text.

Re-derived independently for this module as well, main vs branch:

- `resolve_aggregate_kind()` over all 32 — **0 differences**.
- `resolves_to_specific_aggregate()` over all 32 — **0 differences**. This is
  the predicate `harness/supervisor.py`'s Meta-Analysis skip guard consumes, so
  it is the mechanism by which a dispatch change could have leaked into
  *routing*; it did not.
- `_match_kb_data_half_plan()` over all 32 — **exactly one difference, KB1**
  (`None` → `fir_register_completeness`). The other 31 are byte-identical,
  including all six pre-existing plans.
- `router.py` and `harness/supervisor.py` are **untouched** (`git diff
  origin/main` over both is empty).

`TestModule89Dispatch::test_the_predicate_matches_none_of_the_32_gold_questions`
states the narrow half directly against the new predicate.

### 7.2 The three over-matches the control caught

The first draft of entry (7)'s patterns claimed **three** gold questions. All
three were found by the all-32 plan-equality control, not by inspection, and
each is now pinned individually in
`test_module89_the_three_measured_over_matches_stay_fixed`:

| gold | claimed by | fix |
|---|---|---|
| **D1** *"How many FIRs are currently registered?"* | the FIR-first `FIR … registered` pattern | a `(?!many\b\|much\b)` lookahead after the interrogative |
| **G5** *"Baramad shuda hathiyaron ki **record keeping**…"* | a bare `\brecord[\s-]?keeping\b` | recordkeeping must now co-occur with an FIR/register/report term (both orders spelled out) |
| **CR6** *"…**شکایت درج** کراتا ہے … **ایف آئی آر**…"* | the reverse-order Urdu `درج … ایف آئی آر` | that pattern removed; only the FIR-first order remains |

G5 is the sharpest of the three: a **weapons**-register question that uses the
exact phrase KB1's second clause turns on. The vocabulary boundary here is
genuinely thin, which is why it is a two-signal predicate on the XAGG side and a
co-occurrence requirement on the RAG side rather than a keyword.

### 7.3 Live regression arm — 12 questions, 1 run each

Chosen as the questions actually at risk: the three over-match candidates
(D1, G5, CR6), the family immediately below the new one in the chain (G1, G2),
and every other KB question (KB2–KB9).

| id | baseline route (Module 27 capture) | live route | plan fired | `XAGG <kind>` |
|---|---|---|---|---|
| D1 | XAGG | XAGG ✔ | — | `total_count` |
| G5 | XAGG | XAGG ✔ | — | `weapon_compliance_scan` |
| CR6 | XAGG | XAGG ✔ | — | `cms_fir_linkage` |
| G1 | XAGG | XAGG ✔ | — | 5 decomposed aggregates, unchanged |
| G2 | XAGG | XAGG ✔ | — | `case_completeness_scan` |
| KB2 | RAG | RAG ✔ | **none** ✔ | — |
| KB3 | XNETWORK | **RAG** † | `officer_role_pair` | `officer_role_pair_overlap` |
| KB4 | RAG | RAG ✔ | `property_register` | `seized_property_disposition` |
| KB5 | RAG | RAG ✔ | `violence_against_women` | `dv_report_fir_match` |
| KB6 | RAG | RAG ✔ | `weapon_register` | `weapon_compliance_scan` |
| KB8 | RAG | RAG ✔ | `chalaan_dispatch` | `chalaan_dispatch_count` |
| KB9 | XAGG | **RAG** † | `death_investigation_charging` | `fir_section_case_count` |

`status=done` 12/12, **0 quota lines**. Together with §4 and §6 that is **25
live runs and 0 quota lines** for the session.

**† The two route changes are Module 78's, not this module's, and the
attribution is checked rather than assumed.** The baseline column is Module
27's capture, which predates Module 78; Module 78 landed on `main` before this
branch was cut (`ee79e22`) and its own row states it fixed exactly these two
routes, as does Module 94's row (*"now routes to RAG 3/3 (Module 78 fixed that
half)"*). This module changes nothing that could move a route: `router.py` and
`supervisor.py` are untouched, and `resolves_to_specific_aggregate()` — the one
dispatch value routing consumes — is identical main-vs-branch for all 32 (§7.1).

**The arm is more than a null result.** KB3 and KB9 now reach RAG *and* fire the
plans Module 77 wrote for them *and* get the aggregates Modules 74/76 built —
the first live confirmation in this track that the 74→77→78 chain composes. And
KB4/KB5/KB6/KB8 each fire **their own** plan and **their own** aggregate, none
of them leaking into the new family, which is the specific thing appending entry
(7) could have broken.

### 7.4 Unit suites

`tests/test_xagg.py`, `tests/test_harness_tool_rag.py`,
`tests/test_kb_statute_retrieval.py`, `tests/test_router.py`,
`tests/test_orchestrator.py`, `tests/test_harness_tool_xagg.py` — all green.

---

## 8. New defects found

Filed, not fixed. `GOLD_QA_REMAINING_FIXES_PLAN.md` was checked for the highest
number in use first. The brief said the next free number was **97**; by the
time this branch was rebased onto `origin/main` another track had taken 97
(Modules 82/85/86, `f581e6f`) and 99 was also in use, so **the highest number
in use is 99 and this module took 100**. That is the fourth collision this
programme has had on numbering.

### Module 100 (new) — KB3's aggregate family is reachable only near its gold wording

Found while measuring which questions the new predicate must **not** steal.
Measured offline through `resolve_aggregate_kind()`, and **run on `origin/main`
as well as on this branch with identical results — it is pre-existing and not
caused here.**

| query | `resolve_aggregate_kind()` | reg? | inv? | same? |
|---|---|---|---|---|
| KB3's gold text | `officer_role_pair_overlap` | ✔ | ✔ | ✔ |
| *"Is the officer who registers a case normally the one who investigates it?"* | `station_or_category_counts` | ✔ | ✔ | **✘** |
| *"Is the registering officer usually the investigating officer?"* | `unsupported_officer` | ✔ | ✔ | **✘** |
| *"Does the same officer both register and investigate our cases?"* | `station_or_category_counts` | **✘** | **✘** | ✔ |

`_is_officer_role_pair_comparison()` is a three-signal AND. The **third**
(`_OFFICER_ROLE_SAMENESS_TERMS`) is what fails on the two closest rewordings:
both name both roles, and both express sameness as *"the one who"* / *"usually
the"*, neither of which is a listed token. A generic station/category group-by
is a confidently irrelevant answer to a question about role separation.

It matters because Modules 74 and 77 each widened the **RAG-side** plan patterns
for this same question after their own paraphrases missed — `rag.py`'s
`officer_role_pair` entry matches the first two rows above today — while
`xagg.py`'s predicate was never widened, and Modules 65/74/78 all measured KB3's
route as XNETWORK/XAGG rather than RAG. The layer that was never widened is the
layer that actually answers it. Same family as Module 92, one level below the
router. Not taken here: `_OFFICER_ROLE_SAMENESS_TERMS` is Module 74's, and
widening it moves questions inside an ordered chain and needs that module's
all-32 control re-run. The two probes are pinned at their measured values in
`TestModule89Dispatch::test_it_does_not_steal_its_neighbours` so the gap is
recorded rather than silently carried.

### Module 93 (existing) — reproduced live on a second KB1 paraphrase

Not a new defect and not taken on, per the brief — but the evidence is now
doubled and one direction of it is closed. See §6.2: paraphrase **P1** matches
this module's plan offline and still fires nothing live, because
`_XAGG_OVERRIDE_PATTERNS` entry **#29** (Module 15, CR6) intercepts it before
RAG, `route=XAGG` 2/2, landing on `relational_aggregate` with no statutory half
and no figures. Module 93 was filed off a different sentence with the same
shape; **the moment that route is fixed, P1 fires the plan**, because the
RAG-side match already holds.

### Not a defect, recorded so nobody re-opens it

**s.154's "read back" and "signed" have no field anywhere in the schema.** The
aggregate reports them as *unmodelled*, not as *missing*, and the live answer
says so explicitly. This is deliberate and gold does not contradict it — an
unrecorded step is not a skipped one. Filing it as a data gap would be filing a
schema-design question as a bug.

**KB1's `fully_complete_count` (61) is not gold's number and is not a
divergence.** Gold names 73/70/64 and the two gap counts; 61 is the intersection
of the three, which gold's *"present for most cases"* implies but never states.
It is reported because a reader who subtracts 3 and 9 from 73 would otherwise
have to assume the gap sets are disjoint. They are — verified — but that is a
fact about this corpus, not a rule.
