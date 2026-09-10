# Module 55 — pre-Module-31 aggregates emit no `XAGG <kind>:` log line

**Found by:** Module 43 · **Branch:** `fix/xagg-log-lines-and-m2-vocabulary`
· **Date:** 2026-09-09

---

## 1. Root cause

XAGG's SSE stream reports only `route='XAGG'`. It never says which of the
aggregate families inside XAGG actually answered. The `XAGG <kind>: ...` line
in `backend.log` is therefore the **only** evidence of which aggregate
answered a live question — and that is the fact every module in this wave is
verified against.

Modules 23/24 established the convention and Modules 31–36 each added one line
by hand. Everything older emitted nothing.

**Measured, on the pre-fix tree (commit `9942db9`):**

| | count |
|---|---|
| `"kind": ...` returns in `xagg.py` | 38 (39 textual matches, one of them a comment) |
| **distinct** aggregate kinds returned (AST walk) | **32** |
| `"XAGG <label>:"` log format strings | **11** |
| families with no line at all | **21** |

The brief's estimate was "38 returns, 20 log lines, ~18 silent". The 38 is
exact. The "20" counts every source line containing the string `XAGG ` —
comments and a `logger.error` about an unauthorized attempt included; the real
number of `XAGG <kind>:` **format strings** is 11, and the number of silent
**families** is 21, not 18.

The 21 silent families were:

```
case_completeness_scan (G2)          court_readiness_scan (G3)
weapon_compliance_scan (G5)          weapon_evidence_chain (CR4)
criminal_record_court_crosscheck (CR7)
cms_fir_linkage (CR6)                dv_report_fir_match (CR8)
gender_breakdown (A1)                reporting_delay_count (A7)
placeholder_officer_count (CP6)      total_accused_count
district_breakdown                   rate_breakdown (CP1)
time_bucketed_breakdown (M1)         time_bucketed_rate
station_total_count                  total_count
case_listing                         relational_aggregate
graph_recurrence (Vehicle/Person/Weapon)   unsupported_aggregate
```

That list is the point. It contains the whole CR6/CR7/CR8/G2/G3 set, the
entire entity-recurrence tier, the `case_listing` corpus dump several modules
in this wave had to prove they were **not** hitting, and the
`relational_aggregate` catch-all that Module 44 measured M2 falling into.

Module 43 hit this head-on: M7's own aggregate had no line, so *"did M7's
aggregate run, or the neighbouring delay-reason one?"* — the entire point of
that module — could only be answered by matching numbers out of rendered
prose.

**The brief's hypothesis was right; only its arithmetic was off.** Nothing
about the diagnosis changed.

---

## 2. Change

`src/pipeline/xagg.py` — the only source file touched.

- One `logger.info("XAGG <kind>: ...")` per previously-silent family, **21 in
  total**, each carrying the **figures** and not just the kind. A kind alone
  cannot distinguish a correct run from a wrong-metric one, which is the exact
  failure mode Module 43 was investigating.
- The **honest-refusal paths** get their own lines too — `gender_breakdown`,
  `offender_age_profile` and `reporting_delay_count` each return an
  `unsupported: True` result before reaching their main log line, so until now
  a refusal was indistinguishable in the log from XAGG never running at all.
- `graph_recurrence` is returned from three separate `run_aggregate()`
  branches (Vehicle / Person / Weapon), so its line is factored into one
  `_log_graph_recurrence()` helper rather than written three times. The entity
  type is the payload that matters: a person-recurrence answer to a weapon
  question is precisely the wrong-family failure Modules 33 and 35 had to
  diagnose from prose.
- **No behaviour change.** No dispatch order, no computation, no returned key
  and no rendered string moved. The diff is 211 added lines and 0 deleted.

**ASCII rule, and what PR #30 changed.** Every format string stays ASCII. Urdu
**values** are now passed as `%s` arguments and survive: PR #30 reconfigured
the log stream in `src/main.py` to `encoding="utf-8", errors="backslashreplace"`,
so the cp1252 stream that silently destroyed three of Module 36's live calls
is gone. Confirmed in the live output below — station names, district names
and gender labels all render as readable Urdu.

The one pre-existing `ascii()` escape, in `_filtered_fir_listing()`, is
deliberately **left alone**: it predates PR #30, its own regression test is
pinned to it, and it is not this module's file to churn.

---

## 3. Unit tests

```
PYTHONPATH=. .venv/Scripts/python.exe -m pytest tests/test_xagg.py -q
→ 332 passed, 0 failed, 0 skipped
```

Adjacent suites that import `xagg` (supervisor, router, orchestrator,
meta-analysis agent, XAGG agent + tool): **431 passed, 0 failed**.

**The durable fix is the enforcing test, not the 21 lines.**

`test_module55_every_aggregate_kind_emits_an_xagg_log_line` derives the kind
set from the **source** — an AST walk over every literal `{"kind": "..."}`
value in `xagg.py`, so a kind named only in a comment or docstring cannot
count — and asserts each one has a matching `XAGG <label>:` format string. A
family added tomorrow with no log line fails the suite. It also asserts
`len(kinds) >= 32` so the check cannot pass vacuously if a future refactor
moves the kinds off dict literals.

One documented alias: `time_bucketed_mean` is labelled by its **dimension**
(`incident_to_report_minutes_by_year`) rather than its kind, because that
generic container would otherwise be shared by M7's aggregate and any future
mean-by-bucket family. Module 43's result file and its regression test are
both pinned to the existing string, so it is recorded as an explicit alias
rather than relabelled.

Two supporting tests:

- `test_module55_log_format_strings_stay_ascii` — every `"XAGG ...` template
  is ASCII. (This one **failed on first run**, catching three em-dashes I had
  written into the new refusal lines. It works.)
- `test_module55_a_silent_family_now_names_itself_with_its_figures` — one
  worked example end to end on G2's completeness scan, asserting the counts in
  the line and that the returned result is unchanged.

---

## 4. Live verification

**Backend/SSE verification was DEFERRED FOR CONTENTION. Stated plainly, as
the runbook requires.**

At the time of writing, ports **8015** (Module 38, RAG/retrieval) and **8016**
(Modules 53/57/40, Meta-Analysis) were both LISTENING and both actively
posting to the shared model server — their `backend.log` files had lines
timestamped within seconds of the check. Free RAM was **4.2 GB of 16 GB**.
That is exactly the condition Module 50 measured as causing sub-query timeouts
from extra concurrent backends alone. Starting a third backend on 8017 would
have degraded their measurements as well as mine, so port 8017 was never
brought up.

**What was run instead, and why it is not a substitute-in-name-only.** Every
aggregate was invoked **in-process against the live infrastructure** — the
real `DirectGateway` (Postgres, 73 cases) and the real AGE graph — with the
module logger at INFO. This exercises the exact code path the log lines live
on and produces real figures; the only layer it does not cross is uvicorn/SSE,
which the logging is independent of. It adds no model-server load at all.

Command:

```
PYTHONPATH=. .venv/Scripts/python.exe -X utf8 scratchpad/live55.py
```

Verbatim captured lines (Urdu values intact, no `--- Logging error ---`):

```
XAGG total_accused_count: 92 distinct accused person(s) across 94 case-scoped entr(ies)
XAGG gender_breakdown: 94 accused edge(s), 3 distinct value(s); مرد=67, عورت=24, unknown=3
XAGG reporting_delay_count: 8 of 73 FIR(s) record a reporting-delay reason
XAGG placeholder_officer_count: 10 case(s) still carry a placeholder investigating officer (11 ever); ASI=7 SI=3
XAGG criminal_record_court_crosscheck: 33 criminal record(s), 1 settled / 32 in progress; 1 cross-checked against a court outcome, 1 consistent
XAGG cms_fir_linkage: 4 CMS complaint(s), 4 linked to a case, 0 unlinked
XAGG dv_report_fir_match: 8 women-violence report(s), 4 matched to an FIR, 4 unconfirmed
XAGG case_completeness_scan: 73 case(s) scanned; 9 missing an incident date, 52 missing an investigation status
XAGG weapon_compliance_scan: 32 weapon(s); 30 unlicensed, 2 with no licence status recorded
XAGG weapon_evidence_chain: 32 weapon(s); 30 traceable to a named accused, 2 unattributed; richest chain fir-891-24
XAGG court_readiness_scan: 94 accused edge(s), 82 with no recorded relationship; 2 of 32 weapon(s) with no licence status; 9 of 73 case(s) with no incident date
XAGG district_breakdown: entity=Weapon, 8 district(s); فیصل آباد=10, لاہور=8, حیدر آباد=3, راولپنڈی=3, اسلام آباد=2, چنیوٹ=2, کراچی ایسٹ=2, کراچی وسطی=2
XAGG rate_breakdown: dimension=weapon_recovery_rate_by_district, 9 district(s); حیدر آباد=3/5=0.6, فیصل آباد=10/19=0.526, لاہور=8/18=0.444, ...
XAGG time_bucketed_breakdown: dimension=statute_by_year, 2 year bucket(s) [2024:n=26, 2026:n=82]
XAGG time_bucketed_rate: dimension=reporting_delay_rate_by_year, 2 year bucket(s) [2024=0/13=0.0, 2026=7/51=0.137]
XAGG station_total_count: 19 distinct PoliceStation node(s)
XAGG total_count: 73 case(s) after filtering; unsupported_filters=none
XAGG graph_recurrence: entity_type=Vehicle, 0 recurring node(s); none
XAGG graph_recurrence: entity_type=Person, 4 recurring node(s); فیصل=2, طارق=2, شہزیب عرف شابی=2, عاصم رشید=2
XAGG graph_recurrence: entity_type=Weapon, 1 recurring node(s); 30 بور پستول=30
XAGG case_listing: 73 case(s) listed unfiltered (jurisdiction scope none)
XAGG relational_aggregate: group_by=police_station, 73 case(s) considered, 15 bucket(s); تھانہ ماڈل ٹاؤن، لاہور=7, سائبر کرائم سرکل، اسلام آباد=5, ...
XAGG unsupported_aggregate: reason=unsupported_officer
XAGG unsupported_aggregate: reason=unsupported_trend
```

**21 of 21 previously-silent families produced a line with real figures on the
first attempt.** No family was silent; none lost its figures to encoding.

What is **not** claimed here: that these lines were observed in `backend.log`
behind a live SSE request. That check is deferred, and is the one thing
Module 27's rerun will confirm for free the moment it runs — which is the
point of landing this before it.

---

## 5. Gold comparison

Module 55 is observability, so it has no gold answer of its own. The figures
above are nonetheless checkable against figures other modules recorded
independently, and every one agrees:

| Figure in a new line | Independently recorded | Agrees |
|---|---|---|
| `gender_breakdown` 67 M / 24 F / 3 unknown, 94 total | Module 10.1 / 13 (A1), from `GROUND_TRUTH_NOTES.md` §4 | ✅ |
| `placeholder_officer_count` current 10, ever 11 | Module 7 (CP6); gold's 11 reflects the earlier state | ✅ |
| `case_completeness_scan` 9 of 73 missing an incident date | Module 15 (G2), live-confirmed | ✅ |
| `station_total_count` 19 | Module 2a; Module 44's 19-station roster | ✅ |
| `total_count` 73 cases | Postgres, 73 cases (wave baseline) | ✅ |
| `time_bucketed_rate` 2024 = 0/13, 2026 = 7/51 | Module 13's delay-reason rate ("0% of 13 … 14% of 50") | ✅ (51 vs 50 — a one-row difference, within tolerance) |
| `weapon_compliance_scan` 32 weapons | wave baseline graph census: Weapon 32 | ✅ |

That last row is worth stating rather than smoothing: Module 13's figure was
50 FIRs for 2026 and the live read is 51. It is a ~2% difference in a
denominator, well inside tolerance, and consistent with a row gaining a
parseable incident date since. **Reported, not tuned.**

---

## 6. Non-gold paraphrase

Not applicable in the usual sense — no question's answer changed. The
equivalent check for an observability module is that the line is emitted for
queries phrased **outside** any gold text, which is how the `run_aggregate()`
branches above were driven: `"which vehicles appear in multiple cases?"`,
`"which people appear in more than one case?"`, `"which weapons recur across
cases?"`, `"list of all cases"`, `"how many cases per police station?"`,
`"which officer is assigned to the most cases?"`, `"what is the trend over
time?"` — none of these is a gold question, and each produced its family's
line plus the expected `kind`.

---

## 7. Regression guard

- `tests/test_xagg.py` — **332 passed**, including every Module 23/24/31–36/
  41/43/44 regression pinned to literal gold text.
- Adjacent suites — **431 passed** (`test_harness_supervisor.py`,
  `test_harness_agent_large_scale_aggregate.py`, `test_harness_tool_xagg.py`,
  `test_router.py`, `test_orchestrator.py`,
  `test_harness_agent_meta_analysis.py`).
- The full `pytest -q` suite was **deliberately not run** — it empties
  `muhafiz_entity_descriptions` (see `WAVE2_ORCHESTRATION_PROMPT.md`).
- Dispatch guard: all 32 gold questions resolve through
  `resolve_aggregate_kind()` to exactly the same kinds as before this branch —
  see Module 56's all-32 equality control, which covers both commits.
- The five named regression questions were confirmed at the dispatch **and**
  aggregate layer: G2 → `case_completeness_scan` (73 scanned / 9 / 52),
  G3 → `court_readiness_scan` (94 / 82 / 2 of 32 / 9 of 73),
  G5 → `weapon_compliance_scan` (32 / 30 / 2),
  CR7 → `criminal_record_court_crosscheck` (33 / 1 settled / 32 in progress),
  M2 → `station_caseload_by_specialisation` (19 stations / 73 FIRs). The
  end-to-end LLM rendering of those five is part of the deferred live check.

---

## 8. New defects found

**None fixed here, one recorded.**

`time_bucketed_mean` is a **generic container kind** shared by M7's aggregate
and any future mean-by-bucket family, which is why its log line has to be
labelled by `dimension` instead. The same is structurally true of
`time_bucketed_breakdown`, `time_bucketed_rate` and `rate_breakdown` — all
four are containers, and their new lines all carry `dimension=` in the
payload for that reason. Today each container has exactly one implementation,
so the coupling is harmless; the moment a second lands, `kind` alone stops
identifying the family and the enforcing test's alias map is where that shows
up. Recorded here rather than pre-emptively refactored: renaming
`incident_to_report_minutes_by_year` would break MODULE43_RESULT.md's pinned
assertion for no present benefit.

No new module is raised for it — it is a note for whoever adds the second
container implementation, not a defect in the current tree.
