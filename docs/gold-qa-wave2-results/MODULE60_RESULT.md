# Module 60 — M4 does not skip decomposition live; Module 41's guard never fires for it

**Found by:** Modules 53/40's regression guard · **Branch:**
`fix/m4-routing-and-station-predicate` · **Date:** 2026-09-09 · **Backend:**
`127.0.0.1:8022`

---

## 1. Root cause

Two statements that both looked true, and the reconciliation is the finding.

- Module 41 recorded M4 as **skipping** Meta-Analysis decomposition. It
  derived that **statically**, from `resolve_aggregate_kind()` →
  `statute_court_stage_join`, which is still correct and re-derived on this
  branch.
- Modules 53/40 measured M4 **decomposing on 7 live runs of 7**, across two
  code states.

The guard Module 41 built is at `supervisor.py`:

```python
if route == "XAGG" and (
    any(pat.search(query_text) for pat in _TIME_COMPARISON_XAGG_PATTERNS)
    or _xagg_answers_in_one_call(query_text)
):
```

Its `route == "XAGG"` precondition is deliberate — its own comment says it
exists so the guard "can never suppress a genuine Meta-Analysis decomposition
for a DIFFERENT cross-case route". M4's route is decided by the **LLM router**,
because `_deterministic_route_override()` returned `None` for M4's text. So
the precondition fails before `_xagg_answers_in_one_call()` — which returns
`True` for M4 — is ever consulted.

**Reading the resolver in isolation gives the wrong answer; only the live
route does.** That is the whole defect, and it is a defect in how the fix was
verified as much as in the code.

The LLM router's classification of M4 was measured directly on this branch
with Module 60's override disabled, 8 consecutive `route_query()` calls on
M4's literal Urdu gold text:

| Call | Route | Router's own reason (truncated) |
|---|---|---|
| 1 | XAGG | "Cross-case aggregate question comparing case-filing trends (sections)…" |
| 2 | XAGG | same |
| 3 | **XNETWORK** | "Open-ended synthesis request comparing case-load severity factors…" |
| 4 | XAGG | same as 1 |
| 5 | XAGG | same as 1 |
| 6 | **XNETWORK** | same as 3 |
| 7 | XAGG | "Asks for a cross-case comparison between two metrics…" |
| 8 | XAGG | same as 7 |

**6 XAGG / 2 XNETWORK — `confidence: medium` on all eight.** This is the
mechanism behind the non-determinism the tracker recorded: roughly a quarter
of runs land on XNETWORK, and *those* are the runs where Module 41's guard is
silently skipped and M4 is decomposed. It also explains why Modules 53/40 saw
7-of-7 and then 2-of-3 — the router's own output is the flaky part, and the
live pipeline's query rewriter feeds it a slightly different string each time.

---

## 2. Change

The plan listed three options. **This module took option 3 — fix the routing
— and explicitly did not take options 1 or 2.**

**Why not option 2 (widen the guard past `route == "XAGG"`).** It does not
actually work. With `route = "XNETWORK"`, skipping Meta-Analysis makes the
supervisor fall through to `_ROUTE_TO_SUBAGENT["XNETWORK"]` — Cross-Case
Linkage, or Global Search under Module 9's override. Neither can reach
`_statute_court_stage_join()`. So option 2 would have repealed the guard's
stated protection for every cross-case route in exchange for sending M4
somewhere else that is also wrong. Pinned as an executable fact by
`test_module60_m4_under_the_live_xnetwork_route_still_decomposes`.

**Why not option 1 (take Module 40's `statute_vs_court_stage` plan).** A
matched deterministic plan **vetoes** `_xagg_answers_in_one_call()`, so M4
would decompose on *every* run — including the ~75% of runs where the router
does say XAGG and the one-call aggregate is the better answer. Pinned by
`test_module60_m4_is_not_owned_by_a_module29_decomposition_plan`.

**What shipped —** `src/pipeline/router.py`, one new helper and one new
override block, inside `_deterministic_route_override()`:

```python
def _resolves_to_statute_court_stage_join(query: str) -> bool:
    try:
        from src.pipeline.xagg import resolve_aggregate_kind
    except Exception:
        logger.warning(...)
        return False
    return resolve_aggregate_kind(query) == "statute_court_stage_join"
```

```python
if _resolves_to_statute_court_stage_join(query):
    return {"route": "XAGG", "case_scope": "cross_case", ...}
```

Three deliberate properties:

1. **Gated on `resolve_aggregate_kind()`, not on a new regex.** Module 41 made
   that function the single source of dispatch truth and Module 62 warns that
   widening pattern lists measures nothing. Asking XAGG's own chain "would you
   dispatch this to M4's aggregate?" cannot drift from what XAGG will actually
   run, and it inherits every precedence rule **above**
   `_is_statute_court_stage_join()` for free — G3's `court_readiness_scan` and
   CR7's `criminal_record_court_crosscheck` both resolve earlier in the chain
   and therefore can never reach this override. **This is the PR #8 collision
   guarded structurally rather than by hand.**
2. **One aggregate kind, not `resolves_to_specific_aggregate()`.** The broad
   form — "route to XAGG whenever XAGG resolves to something specific" — was
   measured against all 32 gold questions and would move **seven** of them:
   A1, CS4, CP1, M2, M4, M7, G1 and **KB5**, a legal-KB question that resolves
   to `gender_breakdown` purely as a resolver false positive. That measurement
   is the bound on this module's risk, and it is kept executable in
   `test_module60_broad_form_was_rejected_for_a_measured_reason`.
3. **Placed after the XNETWORK loop and after the active-case
   short-circuit.** XNETWORK's stated precedence over XAGG is untouched, and a
   within-case *"how far has this case got in court?"* is still GRAPH.

**`_statute_court_stage_join()` (Module 24's aggregate) was not modified.**
Neither was `supervisor.py`'s guard. Neither was `meta_analysis.py`,
`verifier.py`, `rag.py`, `statute_hypothesis.py`, `src/retrieval/`,
`xnetwork.py`, `evaluation/` or `prompts/evaluator.txt`.

---

## 3. Unit tests

`tests/test_router.py` (+8) and `tests/test_harness_supervisor.py` (+4). All
three assigned suites pass: `tests/test_router.py`, `tests/test_xagg.py`,
`tests/test_harness_supervisor.py` — plus
`test_harness_agent_meta_analysis.py`,
`test_harness_agent_large_scale_aggregate.py`, `test_harness_tool_xagg.py`,
`test_harness_cutover.py` and `test_case_scope.py` as blast-radius checks.
**0 failures.**

| Test | What it pins |
|---|---|
| `test_module60_m4_gold_text_routes_deterministically_to_xagg` | M4's **literal Urdu gold text** → `route="XAGG"`, `case_scope="cross_case"` |
| `test_module60_m4_gold_text_is_in_the_dataset_verbatim` | that the literal above really is M4's, not a paraphrase |
| `test_module60_override_is_gated_on_the_aggregate_resolver_not_a_regex` | the resolver gate, **and G3 explicitly not captured** |
| `test_module60_an_active_case_still_short_circuits_the_new_override` | within-case "how far in court" stays GRAPH, by `case_id` and by `CASE-009` in the text |
| `test_module60_all_32_gold_questions_route_exactly_as_before_except_m4` | **the all-32 EQUALITY negative control** |
| `test_module60_gold_question_variants_route_exactly_as_before` | the 32 questions' own paraphrase variants |
| `test_module60_broad_form_was_rejected_for_a_measured_reason` | the seven-question blast radius of the rejected broad form |
| `test_module60_m4_gold_text_skips_decomposition_once_the_route_is_xagg` | Module 41's **unmodified** guard fires for M4 once the route is right |
| `test_module60_m4_under_the_live_xnetwork_route_still_decomposes` | why option 2 was rejected — fails if a later module widens the guard |
| `test_module60_m4_still_matches_the_meta_analysis_triggers` | the negative half: without the guard M4 *would* still decompose |
| `test_module60_m4_is_not_owned_by_a_module29_decomposition_plan` | why option 1 was rejected — no plan veto |

The equality control is stated as **equality against a captured map**, not as
"M4 now routes to XAGG". Asserting only the latter would pass if the override
had also dragged G3 or CR7 sideways.

---

## 4. Live verification

**Infrastructure checked before the batch, as required:** model server
`/health` → `{"status":"ok"}`; backend 8022 `/health` → ok, 7716 documents;
port 8020 up (Module 61's track), 8021 down; quota grep over the whole run
`0`.

Both arms are the **same backend, same corpus, same session shape** — the only
difference is `src/pipeline/router.py`, reverted to `main` @ `c5533ba` for the
before arm and restored for the after arm.

### Before — `main` @ `c5533ba` router, same backend, 6 runs

| Run | Route events | Sub-agents | Seconds | Outcome |
|---|---|---|---|---|
| 1 | `XNETWORK` → `XAGG`, `RAG` | Meta-Analysis, Large-Scale Aggregate, Semantic Search | 328.9 | **partial** — statute half only |
| 2 | `XNETWORK` → `XAGG`, `RAG` | same | 324.6 | **partial** — statute half only |
| 3 | `XAGG` | Large-Scale Aggregate | 86.9 | answers |
| 4 | `XAGG` | Large-Scale Aggregate | 69.7 | answers |
| 5 | `RAG` | Semantic Search | 456.0 | **fails** — `status=error` |
| 6 | `RAG` | Semantic Search | 486.6 | **fails** — `status=error` |

**2 of 6 fully answer. Four distinct outcomes in six runs of one unchanged
question** — which is a stronger statement of the defect than the tracker's,
and it is why a single pass proves nothing here.

- Runs 1–2 are the tracker's recorded shape: decomposed, the court-stage
  sub-query lost, the answer explicitly saying *"the provided data does not
  specify how many cases have reached trial, been dismissed, or resulted in
  convictions"* — gold's second half, absent.
- Runs 3–4 are the runs where the LLM router happened to say XAGG, Module 41's
  guard fired, and M4 answered correctly. **These are the runs the tracker's
  static verification assumed were universal.**
- Runs 5–6 are a **failure mode not previously recorded for M4 at all**: the
  router classified it as plain **RAG**, and after 456 s / 487 s of retrieval
  retries it returned *"No sufficiently relevant documents were found for this
  question after retrying with query refinements."* Filed below as Module 67.


### After — this branch

| Run | Route events | Sub-agents | `XAGG <kind>` log line | Seconds | Outcome |
|---|---|---|---|---|---|
| 0 (smoke) | `XAGG` | Large-Scale Aggregate | `statute_court_stage_join` | 16.6 | answers |
| 1 | `XAGG` | Large-Scale Aggregate | `statute_court_stage_join` | 16.8 | answers |
| 2 | `XAGG` | Large-Scale Aggregate | `statute_court_stage_join` | 13.6 | answers |
| 3 | `XAGG` | Large-Scale Aggregate | `statute_court_stage_join` | 18.8 | answers |
| 4 | `XAGG` | Large-Scale Aggregate | `statute_court_stage_join` | 18.8 | answers |
| 5 | `XAGG` | Large-Scale Aggregate | `statute_court_stage_join` | 29.7 | answers |
| 6 | `XAGG` | Large-Scale Aggregate | `statute_court_stage_join` | 16.3 | answers |

**7 of 7 — one route event, one dispatch, no decomposition, every run.** The
log line is identical on every run:

```
XAGG statute_court_stage_join: 73 case(s) carry a recorded section across 36
distinct statute(s); 33 criminal record(s), 1 settled / 32 in progress;
4 record(s) join to a case in this corpus; agree=False
```

Runtime collapses from minutes to **13.6–29.7 s**, because the two sub-queries
and their model round-trips are gone.

---

## 5. Gold comparison

Judged on facts and ideas, in the answer's own words; numbers by magnitude.

M4's gold: *"No — the two lag one another. The FIR sections already show
serious crime is common, but of 33 criminal records only 1 is a final
conviction; 30 are still under trial. Current caseload severity should be read
off the FIR/section counts, not off convictions, which lag."*

| Gold element | Present in after-runs |
|---|---|
| "No — the two do not give the same impression" | **7 / 7** |
| statute/section side shows a broad, serious caseload | **7 / 7** (73 cases, 218 section entries, 36 statutes; PPC §34 40, Arms Ordinance §13 29, PPC §392 21) |
| 33 criminal records | **7 / 7** |
| only 1 has reached a verdict | **7 / 7** ("Convicted, on bail pending appeal") |
| ~30 still under trial | **7 / 7** — reported as **32 in progress**; gold says 30. Same magnitude (~7% apart), inside this wave's stated tolerance, and the same figure Module 24's aggregate has always produced |
| read severity from FIR/section counts, not convictions | **7 / 7** |

**Every gold element is covered on every run.** This is the first time M4's
court-stage half has appeared at all: in the before arm it is lost to the
sub-query timeout, and on the older base the whole answer was *"No information
was found."*

One honest note, unchanged by this module and not in its scope: the answers
come back in **English** while M4's gold is Urdu. That is the same
answer-language behaviour the wave has recorded elsewhere; the judging
standard is facts and ideas, and it is met.

---

## 6. Non-gold paraphrase

Module 62's warning applies here too, so the override's reach was probed with
phrasings written for this section and not adjusted afterwards. Every row
resolves through `resolve_aggregate_kind()`, so the router and XAGG agree by
construction:

| Paraphrase | Route | Resolved kind |
|---|---|---|
| "What sections are cases being charged under, and how far have those cases actually got in court?" | **XAGG** (override) | `statute_court_stage_join` |
| "Under which sections are people being prosecuted, and how far along in court have those prosecutions actually got?" | **XAGG** (override) | `statute_court_stage_join` |
| "Which statutes are cases filed under, and what stage have they reached in court?" | **XAGG** (override) | `statute_court_stage_join` |
| "Do the charge sheets and the court outcomes tell the same story about how serious our caseload is?" | *no override* | `criminal_record_court_crosscheck` — CR7's family, correctly, since it names no stage |
| "Kya dafaat aur adalati marhale dono ek hi tasveer dete hain caseload ki sanginee ki?" | *no override* | `station_or_category_counts` |

**Three of five reach it; the capability is not tied to M4's gold string** —
not one content word of the Urdu gold survives in the three that pass. The
last two rows are the honest bound, and they are exactly the shape Module 62
describes: this override is **as wide as `_is_statute_court_stage_join()` and
no wider**, which requires a court term *and* a progression term. The
Roman-Urdu row has neither in that predicate's vocabulary. Widening the
predicate is Module 62's problem; doing it here would have changed the blast
radius the negative control measures, which is the one thing this module was
told not to do quietly.

---

## 7. Regression guard

`G3` is mandatory — it shares M4's keyword space and the M4/G3 collision is
PR #8's precedent.

### Static, all 32 (the non-negotiable control)

`_deterministic_route_override()` over every one of the 32 gold questions,
before vs. after:

**Exactly one question changed: `M4`, from *no deterministic override* to
`XAGG`.** All 31 others are byte-identical, including the six that already had
an XAGG override and the eleven that had none. The 32 questions' own
`question_variants` were swept as well: **none** newly reaches the override.

`resolve_aggregate_kind()` over all 32 is **unchanged by Module 60** (it is
only *read*), and unchanged by Module 58 as well — see `MODULE58_RESULT.md`.

### Live — same backend, this branch, 2 runs each

Every question routed `XAGG`, dispatched to **Large-Scale Aggregate in one
call**, and hit its own aggregate. No decomposition, no route drift, no error.

| Question | Runs | Route events | `XAGG <kind>` | Gold check |
|---|---|---|---|---|
| **G3** (mandatory — shares M4's keyword space; PR #8's collision) | 2/2 | `XAGG` | `court_readiness_scan`: 94 accused edges, **82 with no recorded relationship**; 2 of 32 weapons with no licence status; 9 of 73 with no incident date | gold's three findings, all present (gold says 81 of 94 — same magnitude) |
| **CR7** | 2/2 | `XAGG` | `criminal_record_court_crosscheck`: 33 records, 1 settled / 32 in progress, 1 cross-checked, 1 consistent | gold's 33 / 30 under trial / 1 convicted with the court record agreeing — present |
| **G2** | 2/2 | `XAGG` | `case_completeness_scan`: 73 scanned, 9 missing an incident date, 52 missing a status | gold's 9-missing-dates finding — present |
| **G5** | 2/2 | `XAGG` | `weapon_compliance_scan`: 32 weapons, **30 unlicensed**, 2 with no licence status | gold's headline 30-of-32 (94%) — present |
| **M5** | 2/2 | `XAGG` | `weapon_statute_cooccurrence`: 32 weapon cases, 2 year buckets | gold's 2024 Arms §13 + PPC 34/392 and the 2026 widening — present |
| **M2** (Module 58's question, run on this same tree) | 3/3 | `XAGG` | `station_caseload_by_specialisation` | see `MODULE58_RESULT.md` |

**G3 is the important row.** It carries the Urdu word عدالت, the exact token
whose earlier collision with M4 cost PR #8, and it resolves to
`court_readiness_scan` **before** `_is_statute_court_stage_join()` is ever
reached. Because the new override is gated on the resolver rather than on a
regex over that word, this is structural, not a coincidence that held on two
runs.

**Not re-run live:** the 24 gold questions outside this set. They are covered
by the static all-32 equality control, which is the stronger instrument for a
routing change — it is exhaustive, where a live sweep of 32 × N is not
affordable and would be noisier.

---

## 8. New defects found — filed, not folded in

### Module 67 — M4 routes to plain **RAG** on some runs, and then fails hard

**Found by this module's own before-arm, and not previously recorded for M4.**
Runs 5 and 6 of the before arm classified M4's gold text as **`RAG`**, ran
Semantic Search, spent **456 s and 487 s** on retrieval retries, and returned
`status=error`:

> "No sufficiently relevant documents were found for this question after
> retrying with query refinements."

Module 60's override masks this **for M4 specifically** — the deterministic
route now wins before the LLM is consulted, and 7 of 7 after-runs are XAGG.
But the underlying behaviour is untouched: the router can classify a
plainly cross-case aggregate question as within-case RAG, and when it does,
the cost is ~8 minutes and a hard failure rather than a wrong-but-fast answer.
Every gold question **without** a deterministic override (11 of the 32,
including all eight KB questions and G6, G1, CR3) is exposed to it.

**Verify:** `route_query()` over the eleven override-less gold questions, many
runs each, counting how often the route is one the question cannot be answered
from; and whether the ~8-minute retry budget on a doomed RAG classification is
the right one.

### Module 68 — the router's own `confidence` is `medium` for a question it gets wrong 25% of the time

**Found by §1's 8-call probe.** All eight calls returned
`confidence: "medium"`, including the two that landed on XNETWORK and the six
that landed on XAGG. The field therefore carries no information about the
classification's actual stability, while reading as if it does. Nothing in the
pipeline consumes it today, which is the only reason this is a defect and not
an incident.

**Verify:** whether `confidence` correlates with run-to-run route stability
over a sample of gold questions; if it does not, either make it do so or stop
emitting it.

### Not a new defect, but recorded

M4's answers come back in **English** while its gold is Urdu, on 7 of 7
after-runs. This is the wave's known answer-language behaviour and is
unchanged by Module 60; under this wave's judging standard (facts and ideas,
in the answer's own words) it is not a failure, and it is not this module's
to fix.
