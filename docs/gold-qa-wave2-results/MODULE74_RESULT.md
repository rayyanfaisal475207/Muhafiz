# Module 74 — KB3's registering-vs-investigating officer pair

**Branch:** `feat/xagg-kb-data-half-aggregates` · **Base:** `main` @ `0bc728d`
(Module 39 / PR #48 merged)
**Brief:** Module 74 in `GOLD_QA_REMAINING_FIXES_PLAN.md`
**Filed by:** Module 39, which derived the figure independently and attached
it to the row so this module would not have to re-probe. It re-probed anyway.
**Date of every measurement below:** 2026-09-09, worktree
`D:/Rapids AI/muhafiz-m74`, backend on `:8025`, `muhafiz-postgres` healthy
(73 cases), model-server tunnel
`https://discharge-fascism-richness.ngrok-free.dev/health` = 200 throughout
(the tunnel rotated today; the URL in this worktree's `.env` is the new one,
asserted rather than assumed because the OLD url still answers 200 and a
stale reference therefore fails silently).

**Chroma:** the shared store at
`D:/Rapids AI/Evidence Intelligence Platform/data/chroma_db`
(`documents_in_store: 7716`, asserted at backend boot), **read only**. Nothing
was ingested, re-embedded or re-indexed, and no collection was written. The
full `pytest -q` suite was never run (it empties
`muhafiz_entity_descriptions`).

**Files the brief put out of bounds were not touched.** `git diff --stat`
against the base is `src/pipeline/xagg.py`,
`src/pipeline/harness/tools/xagg.py`, `src/pipeline/orchestrator.py`,
`tests/test_xagg.py`, `tests/test_router.py` (one measurement updated, see
§7), `scripts/module74_live_runs.py` and the docs. **`rag.py`,
`statute_hypothesis.py`, `src/retrieval/`, `meta_analysis.py`, `verifier.py`,
`validation.py`, `supervisor.py`, `router.py`, `xnetwork.py`, `evaluation/`
and `prompts/evaluator.txt` are all untouched** — Module 65 holds the
retrieval path on `:8024`, which held a listener throughout this session.

**Quota**, checked with Module 54's canonical pattern: **0 real lines across
38 live runs.** The one grep hit is a false positive and is reported as a new
defect in §8.

---

## 1. Root cause

The brief's diagnosis is **confirmed exactly**, and the figure reproduces
gold.

KB3's gold answer is compound: the law (Police Order 2002 Article 18 sets up
a separate Investigation Wing) **plus** a figure from our own database — *"the
officer who records the FIR and the officer who ends up investigating it are
the same person in 68 of 74 pairs (92%); role-splitting happens in only 6
cases."* Before this module the canned data-half sub-query for that clause
reached `_OFFICER_KEYWORDS` in `xagg.py`'s dispatch chain and got
`unsupported_officer`:

> *"Officer-assignment aggregates are not available: investigating-officer
> identity is not currently modeled as a queryable field in this system."*

That is an honest refusal, and its premise had stopped being true.

### 1.1 The probe — derived independently, before any code

Hand-written Cypher against the live AGE graph
(`scratchpad/probe74.py`, `scratchpad/probe74b.py`):

```cypher
MATCH (o:Officer)-[r:ASSIGNED_TO]->(c:Case)
RETURN c.case_id AS case_id, o.canonical_name AS name, r.role AS role
```

| Measured | Value |
|---|---|
| `ASSIGNED_TO` edges | **144** |
| `role='investigating'` | **74** |
| `role='recording'` | **70** |
| investigating assignments whose officer also holds that case's `recording` edge | **68** |
| **68 / 74** | **91.9 %** |
| not matching | **6** (across 6 distinct cases) |
| of those 6, no `recording` counterpart on the case at all | **3** — `fir-117-26`, `fir-954-26`, `fir-955-26` |
| genuine role splits | **3** — `fir-245-26`, `fir-88-26`, and `fir-205-26` |

| | gold | Module 39 | **this module** | verdict |
|---|---|---|---|---|
| same-person pairs | 68 of 74 (92 %) | 68/74 = 91.9 % | **68 of 74 = 91.9 %** | ✅ exact, three ways |
| split | "only 6 cases" | 6 (3 splits + 3 no-counterpart) | **6 pairs across 6 cases** | ✅ exact |

**One correction to Module 39's breakdown, and it matters for the grain.**
Module 39 recorded the three genuine splits as `fir-205-26`, `fir-245-26`,
`fir-88-26`. `fir-205-26` is subtler than that: it carries **two**
`investigating` edges — `(نامزد ASI)`, which is also its recording officer
and carries `superseded_by`, and `سلمان`, the real successor, who is not.
So at the CASE grain the case is both "same" and "split", and the corpus has
73 cases carrying 74 investigating assignments.

That is why this aggregate counts at the **assignment (edge) grain**, not the
case grain. Gold says "68 of 74 **pairs**" and 74 is the edge count. A
per-case count gives 68 same / 5 split — a true statement about a different
denominator that does not reproduce gold. Both denominators are returned;
the renderer leads with the pair one.

### 1.2 Why the refusal is not simply deleted

`unsupported_officer` is still the right answer for a general *"which officer
is on this case"* identity question — the data model genuinely has no officer
lookup surface — and the brief requires that be preserved. So the carve-out
is a **three-signal predicate**: a registering term AND an investigating term
AND a sameness/separation term. A question naming one role, or naming both
without asking whether they coincide, still falls through to the refusal.

---

## 2. Change

| File | Why here |
|---|---|
| `src/pipeline/xagg.py` | `_OFFICER_REGISTERING_TERMS` / `_OFFICER_INVESTIGATING_TERMS` / `_OFFICER_ROLE_SAMENESS_TERMS`, `_is_officer_role_pair_comparison()`, `_officer_role_pair_overlap()`, `render_officer_role_pair_overlap()`, the `resolve_aggregate_kind()` entry and the mirrored `run_aggregate()` dispatch branch. |
| `src/pipeline/harness/tools/xagg.py` | `AggregateKind` Literal entry + renderer import + render branch. |
| `src/pipeline/orchestrator.py` | The other two render sites. |
| `tests/test_xagg.py` | 21 new tests (§3). |
| `tests/test_router.py` | One MEASUREMENT updated, not a behaviour (§7). |
| `scripts/module74_live_runs.py` | The live harness for §4/§6/§7. |

**Dispatch placement**, stated in both directions and mirrored identically in
`resolve_aggregate_kind()` (Module 41's single source of dispatch truth) and
`run_aggregate()`:

- **BELOW** every subject-specific family above it. CP6's
  `placeholder_officer_count` is the closest neighbour — it reads the *same*
  `ASSIGNED_TO` edges for a different question — and it is checked far
  earlier in the chain, so it keeps first claim structurally, not by keyword
  luck.
- **IMMEDIATELY ABOVE** `_OFFICER_KEYWORDS`' `unsupported_officer`.

The Literal, the two orchestrator render sites and the harness render site
were all added **in the same commit as the aggregate**, before any live run.
That trap — a kind missing from `XAggToolResult.aggregate_kind` raising a
Pydantic `literal_error` that `main.py` swallows into an **empty answer with
`status=None`**, invisible to every unit test — has bitten this project four
times. §4.1 is the live proof it is cleared here.

### 2.1 The widening, and what forced it

The three keyword tuples shipped mined from KB3's own gold wording, and
**both** non-gold paraphrases written for this module missed
(`station_or_category_counts` and `graph_recurrence_person`; §6). That is
Module 56's finding a second time: a narrow trigger list is the **safe**
default for a refusal and the **wrong** default for a real aggregate, because
a missed match no longer means "a generic answer" — it means the very refusal
this family exists to replace. The tuples were widened across English /
Roman-Urdu / Urdu script, bounded by the two controls that already existed
(§3), and one of four paraphrases written *after* the widening still misses
and is pinned as a recorded boundary rather than tuned away.

---

## 3. Unit tests

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_xagg.py -q
419 passed
```

```
PYTHONPATH=. ... -m pytest tests/test_router.py tests/test_kb_statute_retrieval.py \
  tests/test_harness_tool_xagg.py tests/test_orchestrator.py \
  tests/test_harness_supervisor.py tests/test_harness_tool_rag.py -q
all passed
```

The tests that carry the argument:

- `test_module74_reproduces_golds_68_of_74` — the measured live shape as a
  fixture: 68 same / 3 split / 3 no-counterpart over 74 investigating and 71
  recording edges. Asserts `68 of 74`, `91.9 %` and `(92%)` in the rendering.
- `test_module74_officer_role_pair_overlap_counts_at_the_pair_grain` —
  includes the two-investigating-edges case, so a refactor to case grain
  fails here rather than live.
- `test_module74_emits_its_xagg_log_line_with_the_figures` — Module 55's
  convention, asserting the FIGURES, not just the kind.
- `test_module55_every_aggregate_kind_emits_an_xagg_log_line` — the AST test
  that would fail if this family were silent. **Passes with the new kind.**
- `test_module55_log_format_strings_stay_ascii` — passes; the format string
  is ASCII and Urdu officer names travel as `%s` arguments.
- `test_every_new_aggregate_kind_is_accepted_by_the_harness_tool_result` —
  extended with `officer_role_pair_overlap` (the ninth family).
- `TestOfficerRolePairBoundary` — **both** directions. Six pair-comparison
  phrasings reach the family, including KB3's **literal gold text**; five
  officer-IDENTITY questions still resolve to `unsupported_officer`, and one
  asserts `run_aggregate()` still returns the refusal BODY (resolver
  agreement alone is not enough). CP6 keeps `placeholder_officer_count`.
- `test_module74_only_kb3_reaches_the_officer_pair_predicate` — of the 32
  gold questions, exactly **KB3** matches the predicate.
- `test_module56_all_32_gold_questions_resolve_exactly_as_before` — the
  all-32 **EQUALITY** control. Extended with one documented entry
  (`_GOLD32_KINDS_MOVED_BY_MODULES_74_TO_76`): **KB3 only**, from
  `station_or_category_counts` to `officer_role_pair_overlap`. The other 31
  are byte-identical. A new entry in that dict is a decision, not a test
  update.

---

## 4. Live verification

### 4.1 The aggregate, through the real pipeline

The canned data-half sub-query, sent to `/api/chat` on `:8025`:

> *"How many cases record the same person as both the recording officer and
> the investigating officer, and how many split those roles, across all
> cases?"*

| run | route | status | wall | `XAGG <kind>` line in `backend.log` |
|---|---|---|---|---|
| 1 | **XAGG** | done | 5.9 s | `XAGG officer_role_pair_overlap: 144 assignment edge(s), 74 investigating / 70 recording; same officer on 68 of 74 pair(s) (91.9%), 6 split across 6 case(s), 3 investigating assignment(s) with no recording counterpart` |
| 2 | **XAGG** | done | 5.7 s | identical |
| 3 | **XAGG** | done | 2.1 s | identical |

**Verbatim answer, run 1 (identical on all three):**

> *"In the corpus, the same person is recorded as both the recording officer
> and the investigating officer in 68 out of 74 recorded assignment pairs,
> which is 92% of the cases [Document 1]. The roles are split in only 6
> case(s), across 6 pair(s)."*

3 of 3 · non-empty · `status=done` — which is also the live proof that the
`XAggToolResult` Literal trap is cleared for this kind.

The same path was exercised **in process** three more times
(`scratchpad/harness_probe.py`, the exact
`XAggToolInput -> xagg_tool -> XAggToolResult` sequence
`rag.py::_run_kb_data_half()` takes): `status=ToolStatus.OK`,
`aggregate_kind='officer_role_pair_overlap'`, 944 chars, 3 of 3.

### 4.2 KB3's own gold question — and the honest limit of this module

**KB3's data half is still not present live, and this module could not make
it present.** Two independent reasons, both reported rather than worked
around:

1. **The composition entry lives in `rag.py`, which is out of bounds.** The
   brief names `rag.py` as owned by the live Module 65 track and lists it
   first among files not to touch, while the tracker row for this module says
   the landing step "is a one-entry addition to `rag.py::_KB_DATA_HALF_PLANS`".
   Those two instructions conflict; the boundary was respected and the
   one-entry addition is filed as **Module 77** with the sub-query string,
   the expected kind and the pattern list all pinned in `tests/test_xagg.py`
   so it is a copy, not a re-derivation.

2. **KB3 does not currently reach `rag.py` at all.** Measured, 3 of 3:

| run | route | status | wall | data half |
|---|---|---|---|---|
| 1 | **XNETWORK** | done | 9.1 s | absent |
| 2 | **XNETWORK** | done | 10.1 s | absent |
| 3 | **XNETWORK** | done | 10.0 s | absent |

with the identical answer every time — *"No cross-case connections or
patterns were found … nearest cluster found was distance 0.179 against a
relevance cutoff of 0.145. Rather than describing an unrelated cluster, no
cross-case network finding is being reported here."* Module 39 measured KB3
on **RAG** (2/3 answering, statutory half 1/3). On this machine today it is
XNETWORK 3/3, which is a correct refusal from a gate doing its job on a route
that cannot serve this question — and it means a `_KB_DATA_HALF_PLANS` entry
would not fire even once it exists. Filed as **Module 78**.

**Confirmed unchanged by this module:** KB3's route and answer are
byte-identical to what they were before it, because
`_XAGG_ROUTE_OVERRIDE_KINDS` in `router.py` is a named allow-list and
`officer_role_pair_overlap` is deliberately not in it (§7).

---

## 5. Gold comparison

| | gold | measured | verdict |
|---|---|---|---|
| same-person pairs | 68 of 74 | **68 of 74** | ✅ exact |
| share | 92 % | **91.9 %** | ✅ exact |
| split | "only 6 cases" | **6 pairs across 6 cases** | ✅ exact |
| the law's expectation | separation | not computed here — that half is the RAG/statute path's | n/a |

Judged by the brief's standard — same facts or ideas, in its own words, no
digit matching — the aggregate's rendering and the live answer both **cover
gold's data half completely**. Nothing was tuned toward gold: the figure was
derived by hand-written Cypher before the aggregate existed, and the grain
decision (edge, not case) was made *because* the case grain gives 5 and gold
says 6, which is the one place a wrong choice would have looked like a small
harmless difference.

Gold's data half is now **computable and correct**; it is not yet **reaching
KB3's answer**, for the two reasons in §4.2.

---

## 6. Non-gold paraphrase

Written before the sweeps.

| paraphrase | before the §2.1 widening | after |
|---|---|---|
| *"Kya thane mein FIR likhne wala officer hi baad mein us case ki tafteesh bhi karta hai, ya ye do alag alag log hote hain?"* (Roman-Urdu; gold KB3 is English) | `station_or_category_counts` ❌ | **`officer_role_pair_overlap`** ✅ |
| *"Is the person who writes up the FIR usually the same officer who later investigates it, or are they different people?"* (English, different vocabulary) | `graph_recurrence_person` ❌ | **`officer_role_pair_overlap`** ✅ |

Four more written **after** the widening and not tuned to it:

| paraphrase | result |
|---|---|
| *"کیا ایف آئی آر لکھنے والا افسر ہی تفتیش بھی کرتا ہے یا الگ الگ افسر ہوتے ہیں؟"* (Urdu script) | ✅ |
| *"In our records, does the officer who lodges the FIR usually run the investigation too, or is that handled by someone else?"* | ❌ **miss, reported not fixed** |
| *"Are the recording officer and the investigating officer different people, or one and the same?"* | ✅ |
| *"Kya FIR darj karne wala aur tafteesh karne wala ek hi shakhs hota hai?"* | ✅ |

**5 of 6.** The miss names both roles but tests sameness only by implication
("…or is that handled by someone else?"), which the third signal does not
read. It is pinned as
`test_module74_a_known_paraphrase_miss_is_recorded_not_hidden` so a later
widening has to change it deliberately rather than rediscover the gap.

---

## 7. Regression guard

**Module 39's data halves — all survive.** Live, 2 runs each, `:8025`:

| Q | route | data-half plan → aggregate | figure in the answer |
|---|---|---|---|
| KB4 | RAG 2/2 | `property_register` → `seized_property_disposition` 2/2 | **45** property entries across 28 FIRs, run 1 |
| KB5 | RAG 2/2 | `violence_against_women` → `dv_report_fir_match` 2/2 | **8** DV reports, 4 matched, 2/2 |
| KB6 | RAG 2/2 | `weapon_register` → `weapon_compliance_scan` 2/2 | **32** weapons, 30 without a licence, 2/2 |
| KB1 | RAG 2/2 | none by design | CrPC ss.154/155, unchanged |
| KB2 | RAG 2/2 | none by design | `status=error`, the pre-existing verifier rejection Modules 38/52 already record — same in both runs, unrelated to this module |

**The questions nearest the new dispatch entries** — 2 runs each, every one
`route=XAGG`, `status=done`, and the `XAGG <kind>` line proving the family:

| Q | runs | aggregate that answered |
|---|---|---|
| CR7 | 2/2 | `criminal_record_court_crosscheck` |
| G3 | 2/2 | `court_readiness_scan` (with `weapon_compliance_scan` / `case_completeness_scan` sub-queries, as before) |
| G2 | 2/2 | `case_completeness_scan` |
| G5 | 2/2 | `weapon_compliance_scan` |
| M4 | 2/2 | `statute_court_stage_join` |
| CP6 | resolver | `placeholder_officer_count`, pinned as a test |

**One test measurement updated, in `tests/test_router.py`.** `router.py`
itself is untouched. `test_module60_broad_form_was_rejected_for_a_measured_reason`
asserts the residue of questions that `resolves_to_specific_aggregate()`
would move but the named allow-list correctly refuses. KB3 joins that residue
(`["A1", "G1", "KB5"]` → `["A1", "G1", "KB3", "KB5"]`, blast radius 8 → 9),
for exactly the reason Module 67 gave for KB5: a legal-KB question resolving
to a real aggregate must not, on its own, become a routing decision. **KB3's
live route is unchanged** — §4.2 measures it.

---

## 8. New defects found

- **Module 77 — the `_KB_DATA_HALF_PLANS` entry for KB3.** One entry:
  the pattern list, the sub-query
  (`_KB3_SQ_OFFICER_ROLE_PAIR` in `tests/test_xagg.py`) and
  `expected_kind="officer_role_pair_overlap"`. Not done here because
  `rag.py` belongs to the live Module 65 track.
- **Module 78 — KB3 routes to XNETWORK, not RAG, 3 of 3.** It gets a correct
  refusal from a gate whose corpus cannot answer it, loses the statutory half
  Modules 30/38/52 built for it, and would not fire a data-half plan even
  once one exists. Module 39 measured KB3 on RAG; this does not reproduce on
  this machine today. Neither half is fixable in `xagg.py`.
- **The canonical quota grep matches millisecond fields in timestamps.**
  Module 54's `rate limit|RESOURCE_EXHAUSTED|429|quota|UNAVAILABLE|503`
  matched exactly one line across 38 live runs, and it is
  `16:19:29,503 … XAGG criminal_record_court_crosscheck: 33 criminal
  record(s)` — the `,503` of the timestamp. `429` has the same exposure. The
  pattern needs word boundaries or an HTTP-status context. Reported, not
  changed: the pattern is canonical in `gold32_score.py` and enforced by a
  test in another module's territory.
- **Module 39's `fir-205-26` breakdown is slightly wrong** (§1.1). Its
  headline figures are exact; only the split-vs-no-counterpart split of the
  6 differs, because `fir-205-26` carries a superseded investigating edge.
  Recorded here rather than edited into that module's file.
