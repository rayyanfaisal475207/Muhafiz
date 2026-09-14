# Module 144 — Count aggregates take no filters

**Branch:** `feat/aggregate-filters` (worktree `D:/Rapids AI/muhafiz-m144`), from
`origin/main` @ `26a9713` (fast-forwarded from `4f09e38` when PR #90 landed
mid-module; the only delta was the plan file).
**Date:** 2026-09-14.
**Verdict:** fixed. *"How many FIRs were registered in 2019 or earlier?"* went
from **73** (3/3 live runs, verifier rejecting the paraphrase every time) to
**0** (3/3 live runs, verifier passing every time), and the rendered text
states the constraint and the span. The other five filters work on the same
mechanism. `resolve_aggregate_kind()` was not touched and is asserted
byte-identical over all 32 gold questions. **Two departures from the brief,
both deliberate and both stated:** the extractor is deterministic rather than a
reuse of `extract_sql_params()` (§2.3), and the age filter is reachable only
through phrasings that avoid the person nouns, because of a pre-existing
dispatch gap this module does not fix (§8, filed as 146).

---

## 1. Root cause, with the fields and the FIR year distribution re-verified

### 1.1 What the code did

`resolve_aggregate_kind()` sent the live query to `total_count` — the right
family: an FIR record is a case record in this corpus, and *"how many FIRs …
registered"* is in `_TOTAL_KEYWORDS`. `_total_count()` then called
`_filtered_cases()`, which knows exactly two filter families: open/closed
status and crime category/act. **No count aggregate could accept a date, an
age, a section or a district.** The year bound in the question had nowhere
to go, so it went nowhere, and the grand total was returned as the answer to a
question it does not answer.

The paraphrase verifier (`Structured-aggregate verifier`) then rejected the
LLM's summary — *"Paraphrase states number(s) not present in the computed
result: 2019"* — and served the raw aggregate, `Total cases: 73`. That was the
right safety behaviour: the model had written "2019" and the computed text
contained no such number. It also hid the real defect, because the raw text
had no way to say which question it was answering.

### 1.2 The four fields, re-verified against `evidence_graph` before any code

All four probes ran through `docker exec … psql -U postgres -d muhafiz` with
`LOAD 'age'; SET search_path = ag_catalog, public;`, output to a file
(`scratchpad/verify.out`, `verify2.out`).

| filter | what the graph holds | verified figure |
|---|---|---|
| **date** | `Incident.report_datetime`, ISO string (`"2024-09-14T22:15:00Z"`), via `(i:Incident)-[:BELONGS_TO_CASE]->(c:Case)` | **73 of 73** Incidents carry it; 73 Cases, 73 Incidents |
| **age** | `Person.age` (agtype integer), reached via `(p:Person)-[r:INVOLVED_IN {role:'accused'}]->(i:Incident)-[:BELONGS_TO_CASE]->(c:Case)` | **19 of 430** Person nodes carry an age; 94 accused `INVOLVED_IN` edges; 19 of them reach an aged Person. **`INVOLVED_IN` runs Person → Incident**, never Person → Case — the direction the brief warned about; confirmed with `labels(x)` on the target |
| **section** | `(s:StructuredRecord {record_type:'fir_section'})-[:BELONGS_TO_CASE]->(c:Case)`, on `s.section_code` + `s.act` | **218** records, **36** distinct (act, code) pairs; e.g. PPC 34 → 40 cases, Arms Ordinance 13 → 29, PPC 302 → 10, CNSA 9(c) → 12 |
| **district** | `(c:Case)-[:FILED_AT]->(:PoliceStation)-[:PART_OF]->(d:District)`, matched on the stored **Urdu** `d.name` (`district_id` is opaque, "DIST-04") | **9** District nodes; all **73** cases reach exactly one: Faisalabad 19, Lahore 18, Rawalpindi 10, Islamabad 5, Chiniot 5, Hyderabad 5, Karachi East 5, Karachi Central 5, Multan 1 |
| city | no field on any label (`MATCH (n) WHERE n.city IS NOT NULL` → 0 rows) | in this data the city **is** the district; treated as a district alias, rendered as "… district" |
| province | no field on any label (`MATCH (n) WHERE n.province IS NOT NULL` → 0 rows across all 13 labels) | **out of scope**; needs ingestion work first — filed as **149** |

### 1.3 The FIR year distribution

`substring(toString(i.report_datetime),0,4)` grouped over the 73 Incidents:

| year | FIRs |
|---|---|
| 2024 | 13 (all in September: 14–25 Sep) |
| 2025 | 3 |
| 2026 | 57 |

Nothing before 2024. **The true answer to the live query is 0.**

---

## 2. The change, and what was not changed

### 2.1 Files touched

| file | change |
|---|---|
| `src/pipeline/aggregate_filters.py` | **new.** `AggregateFilters` (the six-parameter set + `describe()`/`to_log()`), `extract_aggregate_filters(query) -> AggregateFilters`, the district alias table |
| `src/pipeline/xagg.py` | `resolve_filter_case_ids()` (filters → case-id allow-list off the graph, plus the date span for rendering); `FILTERABLE_AGGREGATE_KINDS`; the five kinds below take `filters`/`filter_meta` (and the two gateway-reading ones `filter_case_ids`); each `XAGG <kind>:` log line now carries `filters=…`; three shared renderers `render_total_count()`, `render_total_accused_count()`, `render_filter_line()`; one block in `run_aggregate()` between `kind = resolve_aggregate_kind(...)` and the first `if kind ==` |
| `src/pipeline/orchestrator.py` | both XAGG rendering sites: the two inline f-strings for `total_count` / `total_accused_count` replaced by the shared renderers; the group-by `else` branch gets the filter line |
| `src/pipeline/harness/tools/xagg.py` | the third rendering site (`_render_aggregate_text`), same three replacements |
| `tests/test_xagg.py` | 71 tests, §3 |
| `scripts/module144_live_runs.py` | the live runner (Module 74's machinery + a JSON probe file + model attribution from the `httpx` lines) |
| `GOLD_QA_REMAINING_FIXES_PLAN.md` | row 144 closed; rows 146–149 filed |
| `docs/gold-qa-wave2-results/module144_*` | run records (§4–§7) |

**Not touched:** `resolve_aggregate_kind()` and every keyword tuple it reads;
`src/pipeline/harness/agents/semantic_search.py` (Module 143's file);
`sql_extractor.py` and `prompts/sql_param_extractor.txt`; the router; every
aggregate outside the five below.

### 2.2 The mechanism — a parameter, not a kind

```
run_aggregate():
    kind = resolve_aggregate_kind(query_text)          # unchanged
    if kind in FILTERABLE_AGGREGATE_KINDS:
        filters = extract_aggregate_filters(query_text)
        if not filters.is_empty():
            filter_case_ids, meta = await resolve_filter_case_ids(filters, include_age=…)
            # graph-reading kinds: folded into jurisdiction_case_ids
            # gateway-reading kinds: passed as filter_case_ids, applied after
            #   the status/act keyword filtering they already do
```

`FILTERABLE_AGGREGATE_KINDS` = `total_count`, `station_or_category_counts`,
`fir_section_case_count`, `total_accused_count`, `offender_age_profile`. All
five predate this module. The filter composes with Milestone E1's
`jurisdiction_case_ids` exactly as that allow-list already composes with
every aggregate (`_intersect_allow_lists()`; `None` = don't narrow, a set —
even an empty one — narrows). For the two person-grain kinds the age bound is
applied **per person** inside the aggregate (a case-level "has an accused in
range" allow-list would count every accused in such a case); for the
FIR-grain kinds age is a case-level filter (a case with at least one accused
in range). For `fir_section_case_count` the section is stripped from the
filter set — that family already reads the named section off the query and
needs the full charged-FIR denominator; narrowing to the section would
collapse the denominator onto the numerator.

With no filter in the question **nothing runs**: no extractor cost worth
measuring (pure regex over one string), no graph read, and the rendered text
is byte-identical to before (`Total cases: 73`, `Total distinct accused
persons: 92`) — asserted in tests and measured in §7.

### 2.3 Why the extractor is deterministic and not `extract_sql_params()`

The brief said the SQL route's extractor "already pulls dates and numbers out
of questions … reuse it rather than writing regex". On reading it
(`src/pipeline/sql_extractor.py`, 36 lines): it is a **model call** —
`call_llm_json()` against `prompts/sql_param_extractor.txt` — whose schema is
`category / subject / section_ref / date`, with `date` a single `YYYY-MM-DD`
string or null. It has no range, no lower/upper bound, no age, no district,
and its prompt is the one the SQL route and the admin endpoint depend on.
Reusing it would have meant (a) editing a shared prompt that two other routes
trust, (b) adding a Qwen3 call (measured 4–70 s in this session) to every XAGG
count, and (c) building the all-32 regression control on an output that is
not stable run to run. The control the brief asks for — *the filter must not
fire on a question that has no constraint* — is only a meaningful assertion
over a function that returns the same thing every time.

So `aggregate_filters.py` is pure, deterministic and conservative:

- a year is a bound only next to a bound word (`in/during 2024`, `2019 or
  earlier`, `before 2020`, `since 2025`, `between 2024 and 2025`, `2024
  mein`, `2024 میں`, `2019 ya us se pehle`, `2019 یا اس سے پہلے`, `2024 اور
  2025 کے درمیان`, …); a bare four-digit number never is;
- **two different bounded years with no range connector are a comparison,
  not a filter** — that is gold M7's exact shape (`2026 mein … 2024 mein`),
  and the extractor returns nothing for it;
- a number is an age only next to an age cue (`aged`, `years old`, `umar`,
  `saal`, `عمر`, `سال`), and only in 1–119;
- a section comes from the same shapes Module 76's `_section_code_in_query()`
  trusts (+ `u/s 302`, `302 PPC`), and a **four-digit "section" is rejected**
  because it is an act's year (`PECA 2016`, `CNSA 1997`);
- a district is one of the nine the corpus stores, by English, Roman-Urdu or
  Urdu spelling, whole-word for Latin; `Karachi` is the **union** of both
  Karachi districts (a count filter can take a union where
  `graph_retriever._resolve_district_id()` — a singular resolver — must
  refuse); an unknown place name is not guessed at;
- Urdu digits (`۲۰۱۹`) fold to ASCII first; NFKC + casefold on everything.

**The known limit, stated rather than hidden:** the year rule is
positional, not syntactic. *"how many cases in total? the 2023 audit said
50"* does not fire (no bound word), but *"how many cases in total? in 2023
an audit said 50"* would, because "in 2023" is exactly a bound. No gold
question has that shape (§7), and a deterministic rule that reads clause
structure is out of scope; if it turns out to matter, the fix is a
clause-boundary check, not a model call.

### 2.4 Rendering — the text says what was honoured

| | before | after |
|---|---|---|
| live query | `Total cases: 73` | `0 FIR(s) registered in 2019 or earlier (records span 2024–2026; out of 73 FIRs considered).` |
| no filter | `Total cases: 73` | `Total cases: 73` (byte-identical) |
| district | `Total cases: 73` | `18 FIR(s) registered in Lahore district (out of 73 FIRs considered).` |
| age, accused grain | `Total distinct accused persons: 92` | `15 distinct accused person(s) aged 25–40 (of 17 distinct accused who carry a recorded age; 75 record no age and cannot be placed).` |
| age, profile family | range/mean only | leads with `8 distinct accused aged 30–40 (of the 17 accused who carry a recorded age; 75 record no age and cannot be placed).` then the profile as before |
| group-by / section family | table only | a leading `Filtered to records in 2024 (records span 2024–2026).` line; `0 case(s) match this filter.` when the table would be empty |

"out of N FIRs **considered**", not "in total": `_filtered_cases()` may already
have narrowed the rows by a status/act keyword in the question (*"section 13
of the Arms Ordinance"* → 29), and N is what this filter was applied to. The
first draft said "in total" and §6's P_SECT_1 would have read "29 … of 29 in
total", which is false.

The log line — the only evidence of what a live run computed (Module 55) —
carries the filter on every filterable kind, e.g.
`XAGG total_count: 0 case(s) after filtering; unsupported_filters=none;
filters=date_to=2019-12-31 (of 73 unfiltered; span (2024, 2026))`.

---

## 3. Unit tests — `tests/test_xagg.py`, 71 tests, both directions

Run: `PYTHONPATH=. python -X utf8 -m pytest tests/test_xagg.py -k Module144`.

| class | what it pins | count |
|---|---|---|
| `TestModule144Extractor` | one exact-equality row per phrasing: 36 phrasings across EN / Roman-Urdu / Urdu (incl. Urdu digits, sentence-final year, dotted date rejected); 8 must-not-fire rows (no bound, incidental year, two-year comparison, M7 verbatim, `PECA 2016`, bare number, unknown district); **fires on 0 of 32 gold questions**; `describe()` text | 46 |
| `TestModule144DispatchControl` | `resolve_aggregate_kind()` over all 32 == the pre-module snapshot (literal dict, taken on `4f09e38` before any edit); `FILTERABLE_AGGREGATE_KINDS` is exactly the five pre-existing kinds | 2 |
| `TestModule144DateFilter` | the live defect → 0 with span and denominator in text and `filters=` in the log; between; single year (Roman-Urdu); year-end timestamp inclusive; **unfiltered result and text byte-identical, no graph read**; incidental year | 6 |
| `TestModule144DistrictFilter` | English / Urdu-script / Karachi union; composes with date; composes with `jurisdiction_case_ids` | 5 |
| `TestModule144SectionFilter` | section on the grand total; case-insensitive `9(C)`; KB9's family takes a date bound but keeps the section as its own parameter; unchanged without a bound | 4 |
| `TestModule144AgeFilter` | per-person on the accused count; unfiltered byte-identical; per-case on the FIR count; the profile family leads with the bounded count; unchanged without a bound | 5 |
| `TestModule144NonFilterableKindsAreUntouched` | M7 never enters the filter path (no filter Cypher issued); the group-by takes the filter and says so; and not otherwise | 3 |

**Before / after, measured, not asserted:**

- pre-module tree, extractor file absent: the file fails at collection
  (`ModuleNotFoundError: src.pipeline.aggregate_filters`);
- pre-module tree with only `aggregate_filters.py` present (so the
  aggregate-side assertions actually run): **22 fail** — every test that
  touches `run_aggregate()` or a renderer (`total_cases == 5` where 0 is
  expected, no `filters_applied` key, no `FILTERABLE_AGGREGATE_KINDS`); the
  extractor rows and the dispatch snapshot pass, as they should
  (`scratchpad/before_unit.txt`);
- this tree: **71 passed**.

Broader suites, all green on this tree: `test_xagg.py`,
`test_harness_tool_xagg.py`, `test_orchestrator.py`,
`test_harness_supervisor.py`, `test_harness_tool_rag.py`,
`test_harness_agent_large_scale_aggregate.py` — **853 passed, 1 xpassed**.
(Named files only; never a bare `pytest tests/`.)

---

## 4. Live verification

Backend on **`:8144`** from this worktree (`:8001` from the main checkout
left running, untouched; `muhafiz-m143` had none up, so the machine-wide cap
of 2 held). `.env` copied from the main checkout with `CHROMA_PERSIST_DIR`
made absolute (`/health` → `documents_in_store: 7716`). Model server
`/health` → 200 before each arm. Real `/api/chat`, platform-admin, SSE parsed
for `route=`; `backend.log` sliced per run for the `XAGG` line, the verifier
verdict, the `httpx` request hosts and any `Falling back to` line. Runner:
`scripts/module144_live_runs.py`; records:
`module144_live_before.json`, `module144_live_after.json`.

**Model, every run, both arms: the local model server** (`POST …ngrok…/llm`,
Qwen3). **0** `Falling back to` lines, **0** requests to `api.groq.com` or
`generativelanguage` in either arm's log. Recorded per run in the JSON as
`model` / `llm_hosts` / `fallback_lines`.

### 4.1 The live query — *"How many FIRs were registered in 2019 or earlier?"*

| arm | run | route | `XAGG` line | verifier | answer served | s |
|---|---|---|---|---|---|---|
| before | 1 | XAGG | `total_count: 73 case(s) after filtering` | **rejected** (`unsupported_numbers=['2019']`) | `Total cases: 73` + raw-aggregate note | 19.5 |
| before | 2 | XAGG | same | rejected | same | 9.4 |
| before | 3 | XAGG | same | rejected | same | 9.6 |
| **after** | 1 | XAGG | `total_count: 0 case(s) … filters=date_to=2019-12-31 (of 73 unfiltered; span (2024, 2026))` | **grounded=True** | *"According to [Document 1], **0 FIR(s)** were registered in 2019 or earlier. The records considered span from 2024–2026, and none of the 73 FIRs analyzed date back to 2019 or earlier."* | 18.9 |
| after | 2 | XAGG | same | grounded=True | same text | 10.7 |
| after | 3 | XAGG | same | grounded=True | *"… 0 FIR(s) were registered in 2019 or earlier. The records considered span from 2024 to 2026, and 73 FIRs were analyzed in total."* | 8.8 |

The verifier flip is the second half of the fix: the model's paraphrase used
to be rejected because it mentioned "2019" and the computed text did not; now
the computed text says "in 2019 or earlier", so the paraphrase is grounded and
the reader gets a sentence instead of a bare number with a warning.

### 4.2 One live query per other filter (1 run each, after arm; before arm = 73 for every FIR count, 92 for the accused count)

| id | question | expected (§1) | `XAGG` line (after) | answer served | verifier |
|---|---|---|---|---|---|
| F_RANGE | How many FIRs were registered between 2024 and 2025? | 16 | `total_count: 16 … filters=date_from=2024-01-01, date_to=2025-12-31` | *"Between 2024 and 2025, **16 FIRs** were registered"* | grounded |
| F_DIST | How many FIRs were registered in Lahore? | 18 | `total_count: 18 … filters=district=لاہور` | *"18 FIRs were registered in Lahore district."* | grounded |
| F_SECT | How many cases were registered under PPC 302? | 10 | `total_count: 10 … filters=section=302` | *"10 FIRs were registered under section 302 of the PPC … out of 73 FIRs considered"* | grounded |
| F_AGE | How many accused persons are aged between 25 and 40? | 15 of 17 with an age | `total_accused_count: 15 … filters=age_min=25, age_max=40 (17 of 92 distinct accused carry an age)` | *"**15 distinct accused persons aged 25–40** … from a total of 17 distinct accused who have a recorded age (with 75 records lacking age data)"* | grounded |

Before-arm answers for the same four: F_RANGE `Total cases: 73`; F_SECT
`Total cases: 73`; F_AGE `Total distinct accused persons: 92` (all with the
verifier's raw-aggregate note); F_DIST's paraphrase happened to survive the
verifier and said *"The provided document does not specify the number of FIRs
registered in Lahore. It only states a total of 73 cases across all
locations"* — honest, and no answer.

---

## 5. Gold comparison — M1, M7, CP6, A1, D1

Live, 1 run each, after arm; and in-process for all five on both trees (§7).

| id | family | `filters=` in log | live answer (after) | gold figure |
|---|---|---|---|---|
| **D1** | `total_count` | `none` | *"**73** total cases registered … the number of currently registered FIRs is **73**"* — `XAGG total_count: 73 case(s) … filters=none` | 73 ✅ |
| **M1** | `statute_mix_by_year` (not filterable; text has no year literal) | — | 2024: 13 FIRs (PPC 34/392, Arms Ord 13); 2026: 51 FIRs across PPC / Arms Ord / CNSA / PECA / DV Act … | unchanged ✅ |
| **M7** | `incident_to_report_minutes_by_year` (not filterable; **two** year literals) | — | *"15.0 minutes across 13 FIRs in 2024 … 1401.3 minutes (~23.4 hours) across 51 FIRs in 2026"* | unchanged ✅ |
| **CP6** | `placeholder_officer_count` (not filterable) | — | *"10 FIRs that carry only a placeholder investigating officer … 7 (نامزد ASI) and 3 (نامزد SI)"* | 10 ✅ |
| **A1** | `gender_breakdown` (not filterable) | — | *"67 males, 24 females, and 3 unknown … 67:24"* | 67 / 24 ✅ |

The filter did not fire on any of them: D1 is the only filterable kind among
the five and its log line says `filters=none`; the extractor returns empty
for all 32 gold texts (unit test) — including M7 and M5, which carry year
literals in comparison clauses.

---

## 6. Non-gold paraphrases — written before running (`module144_paraphrases.json`), 1 live run each

| id | filter | language | question | expected | `XAGG` line | answer served |
|---|---|---|---|---|---|---|
| P_DATE_1 | date | EN | Count the FIRs whose registration falls before 2020. | 0 | `relational_aggregate: group_by=crime_category, 0 case(s) considered, 0 bucket(s); filters=date_to=2019-12-31` | raw aggregate: *"Filtered to records in 2019 or earlier (records span 2024–2026). 0 case(s) match this filter."* — **filter honoured, wrong grain**: "Count the FIRs" matches no `_TOTAL_KEYWORDS` phrase and falls to the group-by catch-all (filed as **147**). The verifier rejected the paraphrase (it echoed "2020", the rendered text says "2019 or earlier"), so the raw text above was served — which does state the answer |
| P_DATE_2 | date | Roman-Urdu | 2024 mein kul kitni FIRs darj hui thin? | 13 | `total_count: 13 … date_from=2024-01-01, date_to=2024-12-31` | *"13 FIRs were registered in 2024 … 13 out of the total 73 FIRs analyzed"* ✅ |
| P_DIST_1 | district | EN | How many FIRs does Faisalabad have on the books? | 19 | `total_count: 19 … district=فیصل آباد` | *"19 FIRs were registered in Faisalabad district (out of 73 FIRs considered in total)"* ✅ |
| P_DIST_2 | district | Roman-Urdu | Karachi mein kitni FIRs darj hain? | 10 (5+5) | `total_count: 10 … district=کراچی وسطی\|کراچی ایسٹ` | *"10 FIR(s) were registered in Karachi Central / Karachi East districts (out of 73 FIRs considered)"* ✅ — then hedges that the document "does not specify the total … across all areas of Karachi", which it does (both districts are the whole of Karachi here); a paraphrase wobble, number right |
| P_SECT_1 | section | EN | How many cases in total cite section 13 of the Arms Ordinance? | 29 | `total_count: 29 … section=13 (of 29 unfiltered)` | *"**29 FIRs** were registered under **section 13** of the Arms Ordinance (out of 29 FIRs considered in total)"* ✅ — the 29 denominator is `_filtered_cases()`'s act keyword narrowing, which is why the text says "considered" |
| P_SECT_2 | section | Roman-Urdu | Dafa 420 ke tehat kul kitne cases darj hain? | 11 | `total_count: 11 … section=420` | *"11 FIRs were registered under section 420 (out of 73 FIRs considered in total)"* ✅ |
| P_AGE_1 | age | EN | How many accused persons in total are under 30 years old? | 8 | `total_accused_count: 8 … filters=age_max=29 (17 of 92 distinct accused carry an age)` | *"**8 distinct accused persons aged 29 or younger** (of 17 … 75 record no age …)"* ✅ |
| P_AGE_2 | age | Roman-Urdu | Kitne mulzim 30 se 40 saal ki umar ke hain? | 8 | `offender_age_profile: … filters=age_min=30, age_max=40; in_range=8` | *"**8 distinct accused** aged 30–40 … derived from the 17 accused (out of 92 total) who have a recorded age"* ✅ — reaches the profile family via `_AGE_KEYWORDS` ("umar"), which now leads with the bounded count |

Expected figures were derived from §1's graph probes before the runs (the
age ones re-derived through the canonical-name fold: 17 distinct accused
carry an age once confirmed SAME_AS pairs are folded — the raw 19 nodes
include two folds). **8 of 8 numerically right; 7 of 8 at the intended
grain**, the exception filed rather than patched.

Rewordings in Urdu script were measured through the extractor (§3: `2019 یا
اس سے پہلے …`, `2024 اور 2025 کے درمیان …`, `لاہور میں …`, `دفعہ 302 …`,
`کتنے ملزمان کی عمر 25 سے 40 سال ہے؟`, all correct) rather than live; the
live budget went to the Roman-Urdu rows the brief asked for.

---

## 7. Regression — all 32 gold questions

**Dispatch control.** `resolve_aggregate_kind()` over all 32 gold texts is
asserted equal to a literal 32-entry snapshot taken on `4f09e38` before any
edit (`_M144_DISPATCH_BASELINE` in `tests/test_xagg.py`). It passes; the
function was not edited.

**Answers equal to baseline.** All 32 gold questions (plus the 18 probes of
§4–§6) were run **in-process** through `run_aggregate()` +
`_render_aggregate_text()` against the live graph and gateway, once from the
untouched main checkout's `src` (pre-module) and once from this tree
(`module144_all32_inprocess.py`, `…_before.json`, `…_after.json`). Result:
**32 of 32 rendered texts byte-identical before → after**, dispatch identical,
`filters` = `None`/`none` on every gold question, 0 errors either side. The
18 probes differ exactly as intended (73 → 0/16/18/10/…; 92 → 15/8).

This is the aggregate layer's answer, deterministic and exact, which is what
"equal to baseline" can mean for XAGG; the LLM paraphrase on top is not
byte-stable run to run and was not compared as text. The five gold
questions with counts and years were additionally run live (§5). A full
32-question live pass was not run: it is an hour-plus of model time whose
only extra information over §5 + the in-process equality would be
paraphrase wording.

**The predictable failure mode did not occur.** *"how many cases in total"*
acquires no bound (test), the two gold questions with year literals (M5, M7)
acquire none (test + live for M7), and no gold question carries a district,
section or age phrase the extractor recognises (test:
`test_fires_on_none_of_the_32_gold_questions`).

---

## 8. New defects — filed, not fixed

Numbering re-checked against `origin/main` at commit time (highest in use
**145**, from PR #90) and against the remote branch list; `muhafiz-m143`
had filed nothing beyond 145. Filed as **146–149** in
`GOLD_QA_REMAINING_FIXES_PLAN.md`.

| # | defect | evidence |
|---|---|---|
| **146** | **A FIR-grain count naming the accused lands on the person-recurrence ranking.** *"How many FIRs involve an accused aged 25 to 40?"* → `graph_recurrence_person` (the 4-person repeat-offender list). `_PERSON_KEYWORDS` is matched before `_TOTAL_KEYWORDS`, and the `total_accused_count` guard needs an `_ACCUSED_TOTAL_KEYWORDS` term (has "how many accused", not "how many firs"/"how many cases"). This module's case-level age filter is therefore reachable only through phrasings that avoid the person nouns (*"registered against anyone aged 25 to 40"* → 15). A dispatch change; deliberately not made here | measured with `resolve_aggregate_kind()` over 8 phrasings; pinned in `test_age_is_per_case_on_the_fir_count`'s comment |
| **147** | **An imperative count ("Count the FIRs …") falls to the station/category group-by** instead of `total_count`: no `_TOTAL_KEYWORDS` phrase matches. The filter is honoured there (§6 P_DATE_1 leads *"Filtered to records in 2019 or earlier … 0 case(s) match this filter."*) but the grain is a group-by. Module 56's vocabulary-gap class | §6, live |
| **148** | **The word "district" turns a scoped count into a per-district ranking.** *"How many cases were registered in Lahore district?"* → `top_districts_by`, because `_DISTRICT_KEYWORDS` is the bare word and is checked above `_TOTAL_KEYWORDS`. Without the word, the same question is `total_count` and the district filter answers it (18) | measured with `resolve_aggregate_kind()` |
| **149** | **Province exists nowhere in the graph** — no node, no property, any label. A province filter cannot be honoured and is not attempted; the question falls to the unfiltered total with nothing saying so. Needs ingestion work first (a `Province` node above `District`, or a property on it; the nine districts map cleanly — Punjab ×5, Sindh ×3, ICT ×1). Then one alias table in `aggregate_filters.py` and one more hop in `resolve_filter_case_ids()` | §1.2 |

**Observations that are not defects, recorded so they are not re-found:**

- An exclusive bound is normalised on extraction (`before 2020` →
  `date_to=2019-12-31`) and rendered as *"in 2019 or earlier"*. When the
  paraphrase echoes the question's "2020", the structured-aggregate verifier
  rejects it (`unsupported_numbers=['2020']`) and serves the raw text — which
  states the constraint correctly. Seen once (§6 P_DATE_1). Rendering the
  original phrasing would need the extractor to keep the surface form; not
  worth a second field until it costs a gold question.
- *"over 40"* / *"under 30"* with **no age cue anywhere in the sentence** is
  not treated as an age (conservative by construction; *"How many accused are
  over 40?"* returns the unfiltered 92). With a cue (*"under 30 years old"*,
  *"aged 40 or older"*) it fires.
- The age filter's denominator is 17, not the 19 nodes §1.2 counts: two
  aged Person nodes fold into others under confirmed SAME_AS pairs, exactly
  as `_offender_age_profile()` already reports (`17 of 92 distinct accused`).
