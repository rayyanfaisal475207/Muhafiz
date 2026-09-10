# Module 41 — G2/G5 regression: Meta-Analysis over-decomposition

**Branch:** `fix/supervisor-skip-decomposition-for-resolvable-aggregates`
(off `main` @ `06de8ea`, later merged with `origin/main`)
**Questions:** G2, G5 — regressed **0.4 → 0.0** and **0.6 → 0.0** in the
2026-09-08 post-fix evaluation. A regression this wave caused.
**Files:** `src/pipeline/xagg.py`, `src/pipeline/harness/supervisor.py`,
`tests/test_harness_supervisor.py`

---

## 1. Root cause

**The brief's diagnosis was correct in every particular.** Nothing had to be
re-derived — only re-confirmed. All three layers were checked independently.

**Layer 1 — the aggregates are correct.** Run in isolation against the live
Postgres/AGE data on this branch:

```
_case_completeness_scan()  -> total_cases=73, missing_incident_date=9, missing_status=52
_weapon_compliance_scan()  -> total_weapons=32, unlicensed_count=30, no_status_count=2
```

Exactly the figures the evaluator reported.

**Layer 2 — dispatch resolution is correct.** `run_aggregate()` resolves G2 to
`case_completeness_scan` and G5 to `weapon_compliance_scan`.

**Layer 3 — the failure is above both, in sub-agent dispatch.** Re-confirmed
in one command over all 32 gold questions before any code was written:

| Q | `_match_decomposition_plan()` | matches `_META_ANALYSIS_TRIGGER_PATTERNS` | matches `_TIME_COMPARISON_XAGG_PATTERNS` |
|---|---|---|---|
| **G2** | `None` | **yes** | **no** |
| **G5** | `None` | **yes** | **no** |
| M1 | `None` | yes | **yes** — Module 26's guard fires |
| CR3 | `record_consistency` | yes | no |
| G1 | `caseload_review` | yes | no |
| G6 | `orientation_note` | yes | no |

G2 and G5 route `XAGG`, match the Meta-Analysis trigger list, and match no
time-comparison pattern — so Module 26's skip guard, which fires *only* for
`_TIME_COMPARISON_XAGG_PATTERNS`, never fired. Module 29's deterministic plans
return `None` for both, confirming the decomposition comes from the **LLM
decomposer fallback**, whose sub-queries drop the vocabulary that classified
the original question and leave each half to a fresh LLM router call one level
down.

### Measured live on the pre-fix build

Backend rebuilt at `06de8ea` (the exact commit the evaluation ran against),
four runs each, `rate limit` count **0** in every run:

```
[G5 before] supervisor:dispatch: Classified query as route='XAGG'  -> sub-agent='Meta-Analysis'
            supervisor:dispatch: Classified query as route='XGRAPH' -> sub-agent='Cross-Case Linkage'
            supervisor:dispatch: Cross-Case Linkage completed with status=empty

[G2 before] supervisor:dispatch: Classified query as route='XAGG' -> sub-agent='Meta-Analysis'
            supervisor:dispatch: (x4 sub-query dispatches)
            supervisor:dispatch: Meta-Analysis completed with status=abstained
```

The first trace is the mechanism verbatim: a sub-query of a *weapon-compliance*
question was re-classified onto **XGRAPH / Cross-Case Linkage**, which returned
`empty`.

Intermittent exactly as warned:

| Q | run 1 | run 2 | run 3 | run 4 |
|---|---|---|---|---|
| **G2** | wrong question answered (2024→2026 case-type shift) | correct | **abstained** — 4 sub-questions timed out | correct |
| **G5** | **"No information was found for any part of this question"** | **same; route flipped to `GRAPH_HYBRID`** | correct | **same failure** |

**2 of 4 failed for each.** Pre-fix latency **78.8 – 214.2 s**.

---

## 2. Change

The structural option, as instructed — not a second pattern list.

### 2a. `src/pipeline/xagg.py` — resolution-only dispatch (own commit)

`run_aggregate()` carried its ~30-branch keyword chain inline, so the only way
to learn which aggregate family would answer a question was to **run** it: a
graph traversal, several gateway reads, an audit event and an RLS arming.
Unusable at routing time, which is precisely where the answer is needed.

The chain is **extracted, not duplicated**, as the pure
`resolve_aggregate_kind(query_text) -> str`. `run_aggregate()` now calls it
once and dispatches on the key — `if kind == "case_completeness_scan":` in
place of `if _matches_any(query_lower, _COMPLETENESS_KEYWORDS):` — so every
predicate, its order, its body and its hard-won precedence comments are
untouched, and there is exactly **one** copy of the ordering. An aggregate
added to `run_aggregate()` becomes visible to the guard automatically. That is
the whole point of doing it structurally: Modules 31–36 would each have had to
remember to extend a parallel pattern list. (Confirmed in practice — when
Modules 35/36 landed on `origin/main` and were merged into this branch, the
resolver absorbed their two new families with no change to the guard.)

**Cost: nothing is executed.** `resolve_aggregate_kind()` is pure substring
matching — no `await`, no gateway, no `current_rls_active` / `current_cross_case`
write, no `log_audit_event`. A unit test parses its AST and asserts all of
that, so the property cannot silently rot.

`resolves_to_specific_aggregate()` is the predicate the supervisor consumes.
`True` for every family **except** two sets:

- **`_GENERIC_AGGREGATE_KINDS`** — `case_listing`, `total_count`,
  `station_or_category_counts`: the chain's trailing fall-throughs. Reaching
  one means *nothing matched*, the opposite of evidence that XAGG has a
  single-call answer.
- **`_ENTITY_RECURRENCE_AGGREGATE_KINDS`** — `graph_recurrence_person` /
  `_vehicle` / `_weapon`. These fire on a bare **noun** with no signal about
  what is asked of it. They are the right aggregate for S3 and CR2, but this
  file's own comments already record two cases of a compound question landing
  on them and being answered wrongly with no caveat (Module 32's relationship
  sub-question; Module 24's M4 via "لوگوں" ⊃ "لوگ"). **This narrowing was not
  in the brief — it was forced by a measured case** (§3).

### 2b. `src/pipeline/harness/supervisor.py` — the guard

```python
if route == "XAGG" and (
    any(pat.search(query_text) for pat in _TIME_COMPARISON_XAGG_PATTERNS)
    or _xagg_answers_in_one_call(query_text)
):
    sub_agent = _ROUTE_TO_SUBAGENT.get(route, SEMANTIC_SEARCH)
```

Module 26's pattern check is **kept** as the first disjunct rather than
replaced, so M1 stays guarded on its own terms even if the aggregate chain is
later reordered underneath it.

```python
def _xagg_answers_in_one_call(query_text: str) -> bool:
    from src.pipeline.harness.agents.meta_analysis import _match_decomposition_plan
    if _match_decomposition_plan(query_text) is not None:
        return False
    return resolves_to_specific_aggregate(query_text)
```

**Subordinate to Module 29's `_DECOMPOSITION_PLANS`, by construction.** G1 is
the load-bearing case, not CR3 or G6: **G1 does resolve to a specific
aggregate** (`case_completeness_scan`), so without this veto the new guard
would have silently repealed Module 29 for it. The import is lazy because
`meta_analysis.py` imports `supervisor.py` at module scope; if it ever fails
the guard returns `False`, leaving dispatch exactly as it was rather than
guessing.

### 2c. `unsupported_aggregate` counts as **resolved** — deliberate

The three honest refusals (`unsupported_station_type` — M2's shape,
`unsupported_officer`, `unsupported_trend`) are purpose-built outcomes: Module
1b added them so a query with no data path gets a *stated limitation* instead
of an unrelated number. Decomposing one is strictly worse, because the
sub-queries drop the vocabulary that earned the refusal and each half is then
answered with an invented split — precisely the "fluent, on-topic, confidently
wrong" mode the 2026-09-08 report names as worse for an evidence platform than
abstaining. Module 44's brief asks for exactly this movement on M2, and §7
shows it measured.

---

## 3. Unit tests

```
PYTHONPATH=. .../python.exe -m pytest tests/test_harness_supervisor.py \
    tests/test_harness_agent_meta_analysis.py tests/test_xagg.py \
    tests/test_harness_tool_xagg.py tests/test_router.py -q
→ 538 passed          (at the pre-merge commit)

PYTHONPATH=. .../python.exe -m pytest tests/test_harness_*.py \
    tests/test_orchestrator*.py tests/test_pipeline*.py
→ 704 passed, 1 xpassed

PYTHONPATH=. .../python.exe -m pytest tests/test_harness_supervisor.py \
    tests/test_xagg.py tests/test_harness_agent_meta_analysis.py \
    tests/test_harness_tool_xagg.py
→ 483 passed          (after merging origin/main with Modules 35/36)
```

The full `pytest -q` suite is deliberately **not** run — it empties the
`muhafiz_entity_descriptions` Chroma collection.

### New tests (`tests/test_harness_supervisor.py`)

Pinned to the **literal gold text**, asserted byte-exact against
`Gold_QA_Dataset_Final32_With_Answers.json`:

| Test | What it pins |
|---|---|
| `..._g2_gold_text_skips_decomposition_and_reaches_the_aggregate` | G2 → `case_completeness_scan`, dispatch `LARGE_SCALE_AGGREGATE` |
| `..._g5_gold_text_skips_decomposition_and_reaches_the_aggregate` | G5 → `weapon_compliance_scan`, dispatch `LARGE_SCALE_AGGREGATE` |
| `..._g2_and_g5_still_match_the_meta_analysis_trigger_patterns` | the negative half — both still match the trigger list, so the two above cannot start passing for the wrong reason |
| `..._m1_still_skips_via_the_original_time_comparison_guard` | M1 guarded by the **pattern list**, independent of the aggregate chain |
| `..._guard_is_subordinate_to_module29_decomposition_plans` | CR3 / G1 / G6 still reach `META_ANALYSIS` via their named plans |
| `..._g1_would_resolve_specifically_but_for_its_plan` | pins that G1's case is not a coincidence |
| `..._m2_station_type_refusal_skips_decomposition` | the `unsupported_aggregate` decision |
| `..._generic_fallbacks_do_not_count_as_resolvable` | the three trailing catch-alls |
| `..._bare_entity_recurrence_does_not_count_as_resolvable` | the noun-only tier |
| `..._guard_is_scoped_to_the_xagg_route_only` | XNETWORK / XGRAPH still decompose |
| `..._guard_respects_allow_meta_analysis_false_recursion_guard` | sub-query dispatch unchanged |
| `..._resolution_is_side_effect_free` | AST check: no `await`, no `gateway`, no RLS/audit in the resolver |
| `..._all_32_gold_questions_dispatch_change_is_exactly_the_expected_set` | **the all-32 negative control** |

### Two existing tests changed premise, not assertion

1. `test_time_comparison_guard_does_not_suppress_unrelated_xagg_meta_analysis_triggers`
   asserted over **M2's gold text**, encoding Module 26's narrowness as an
   invariant. Module 41 deliberately supersedes that. Swapped for a
   caseload-review paraphrase resolving to `station_or_category_counts` (a
   genuine catch-all), so it still decomposes and for the right reason; M2's
   new behaviour is asserted separately.
2. `test_handle_allow_meta_analysis_false_reaches_classify_to_subagent` used a
   synthetic query — *"Aggregate the weapon types used across all cases this
   year and flag any case where the weapon matches an unresolved case's
   weapon"* — that now resolves to `weapon_compliance_scan`, because
   **`"flag"` is a literal `_COMPLIANCE_TERMS` entry**. Swapped for CR3's
   record-consistency shape, which Module 29 owns outright.

**The second failure is a real limitation, not just a test to update.** It is a
genuine false positive of the structural approach: a compound question
carrying the right noun and one loose keyword gets claimed by a specific
aggregate that answers only half of it. The `_ENTITY_RECURRENCE_AGGREGATE_KINDS`
narrowing in §2a was added *because* of this class — an equivalent
vehicle-worded query was measured falling into `graph_recurrence_vehicle` and
is now excluded. The `_COMPLIANCE_TERMS` `"flag"` case is **not** fixed here;
see §8.

---

## 4. Live verification

Backend on **port 8009**, `admin@example.com` / `MuhafizAdmin2026!`, **All
Cases** (no `case_id`). `.env` verified byte-identical to the live one except
for an absolutised `CHROMA_PERSIST_DIR`, with all four Groq rotation keys
matching. **`grep -c "rate limit" backend.log` = 0 across every run, before and
after.** The backend logged exactly one `Application startup complete` and ran
with no reloader, so every result below came from one continuously-loaded
build.

### G2 — before / after

**Question (verbatim):**
> فرض کریں آپ کسی ایس ایچ او کو بریفنگ دے رہے ہیں کہ کون سے مقدمے دب کر یا نظر سے اوجھل ہو کر رہ سکتے ہیں — آپ کن چیزوں کی نشاندہی کریں گے؟

**BEFORE:** `supervisor:dispatch: Classified query as route='XAGG' -> sub-agent='Meta-Analysis'`
**AFTER:** `supervisor:dispatch: Classified query as route='XAGG' -> sub-agent='Large-Scale Aggregate'`
followed by `Large-Scale Aggregate completed with status=ok` — identical on all three runs.

| run | elapsed | status | dispatch events |
|---|---|---|---|
| 1 | 20.9 s | done | 2 (one dispatch, one completion) |
| 2 | 54.0 s | done | 2 |
| 3 | 31.3 s | done | 2 |

**Verbatim answer (run 1):**

> When briefing an SHO on cases at risk of stalling or being overlooked, focus on these critical gaps:
>
> 1. **Missing Incident Dates**: 9 FIRs (fir-205-26, fir-211-26, fir-340-25, fir-406-26, fir-410-25, fir-422-26, fir-457-26, fir-467-26, fir-77-26) lack recorded incident dates, leaving timelines unanchored and investigations without a clear starting point [Document 1].
>
> 2. **Untracked Investigation Status**: 52 FIRs have no recorded investigation status, making it impossible to track their progress or ensure they are not silently stalled [Document 1].
>
> These cases require immediate attention to prevent procedural gaps and ensure accountability.

Runs 2 and 3 returned the same two figures in the same order.

### G5 — before / after

**Question (verbatim):**
> Baramad shuda hathiyaron ki record keeping ko dekhte hue, kya koi aisi baat hai jo compliance ke lihaz se flag karne layak ho?

**BEFORE:**
```
supervisor:dispatch: Classified query as route='XAGG'  -> sub-agent='Meta-Analysis'
supervisor:dispatch: Classified query as route='XGRAPH' -> sub-agent='Cross-Case Linkage'
supervisor:dispatch: Cross-Case Linkage completed with status=empty
```
**AFTER:** `supervisor:dispatch: Classified query as route='XAGG' -> sub-agent='Large-Scale Aggregate'`
— identical on all three runs.

| run | elapsed | status | dispatch events |
|---|---|---|---|
| 1 | 15.8 s | done | 2 |
| 2 | 22.5 s | done | 2 |
| 3 | 19.1 s | done | 2 |

**Verbatim answer (run 1):**

> According to [Document 1], reviewing the 32 weapon-register entries reveals two compliance-related flags:
> 1. **30 of 32 recovered weapons (94%) are recorded without a licence**, indicating a widespread pattern that cannot be addressed on a case-by-case basis and requires a standing compliance check.
> 2. **2 entries carry no licence status at all**, making it impossible to confirm compliance for these cases.
>
> These issues highlight systemic gaps in record-keeping and licence verification.
>
> _A cited claim ([Document 1]) could not be confirmed against its source: Claim cites figure(s)/identifier(s) not found in its source text: 1._

Runs 2 and 3 were near-identical (one word differs: "requires" / "warrants").

**Latency collapsed**: 78–214 s → 16–54 s, and variance with it.

---

## 5. Gold comparison

### G2

Gold's three points: (1) 9 FIRs with no incident date; (2) 13 of 73 FIRs whose
officer-departure time precedes the report time; (3) walk-in complaints and
FIRs linked only by an optional shared tag.

| Gold value | Answered | Verdict |
|---|---|---|
| 9 FIRs missing incident date | **9**, with all nine FIR ids | ✅ exact |
| 52 missing investigation status | **52** | ✅ exact |
| 73 total FIRs | implied, not printed | ⚠️ |
| 13/73 departure-before-report inversion | not mentioned | ❌ |
| walk-in ↔ FIR soft-tag linkage | not mentioned | ❌ |

**Honest verdict: a partial match — materially better than 0.0, not a 1.0.**
G2's gold draws on three *different* aggregates; the completeness scan supplies
one exactly and the other two are outside its reach. Notably the **pre-fix run
4 covered more ground** (it also reached the unlicensed-weapon figure), because
decomposition, when it happened to work, sampled several aggregates. That is
the honest trade this fix makes: reliability and correctness over occasional
accidental breadth. The missing aggregates are logged in §8.

### G5

Gold's headline is *"32 mein se 30 baramad hathiyar (94%) bila-license darj
hain"*, and gold itself names the unlicensed rate as the most prominent point.

| Gold value | Answered | Verdict |
|---|---|---|
| 30 of 32 unlicensed | **30 of 32** | ✅ exact |
| 94% | **94%** | ✅ exact |
| "needs its own compliance note rather than case-by-case" | *"cannot be addressed on a case-by-case basis and requires a standing compliance check"* | ✅ same conclusion |
| no packaging / photo / chain-of-custody column | not mentioned | ❌ |
| weapons join cases by a soft FIR-code match | not mentioned | ❌ |

**Honest verdict: the headline gold fact is returned exactly, every run.** The
two schema-shape observations are not in the aggregate's output at all. G5
should land well above its 0.6 pre-regression baseline; it will not be 1.0
without those.

**One blemish, reported rather than tuned away.** Every G5 run carries
*"A cited claim ([Document 1]) could not be confirmed against its source: Claim
cites figure(s)/identifier(s) not found in its source text: 1."* The `1.` it
objects to is the answer's own **numbered-list marker**, not a factual claim.
It appeared identically in the *correct* pre-fix run, so it is not caused by
this module. Logged in §8.

---

## 6. Non-gold paraphrase

Two paraphrases, chosen before measuring and not adjusted afterwards.

**G5 paraphrase** — *"Looking at how we log recovered firearms, is there
anything a compliance officer would want raised?"*

Resolves to `weapon_compliance_scan`; `resolves_to_specific_aggregate() = True`;
`classify_to_subagent(XAGG, ...) = Large-Scale Aggregate`. **The capability
generalises** — the guard fires on the paraphrase for the same structural
reason it fires on the gold text, with no vocabulary shared with the gold
string beyond "compliance" and a weapon noun.

**G2 paraphrase** — *"If you were warning a station house officer about files
that could quietly go nowhere, which ones would you point to?"*

Resolves to `station_or_category_counts` — a **generic catch-all**;
`resolves_to_specific_aggregate() = False`. It reaches Large-Scale Aggregate
anyway, but only because it matches no Meta-Analysis trigger either, so the new
guard is **not** what routes it.

**Honest reading: G5's half generalises; G2's does not.** G2's fix is only as
broad as `_COMPLETENESS_KEYWORDS`, and that keyword list does not cover
"files that could quietly go nowhere". That is a pre-existing coverage gap in
`xagg.py`'s keywords, not a defect in this guard — the guard can only be as
good as the resolution it queries — but it means **G2 remains partly
curve-fit to its gold string**, and saying otherwise would be false. Logged in
§8.

---

## 7. Regression guard

All live, two runs each, `rate limit` = 0 throughout.

| Q | runs | first dispatch | still decomposes? | outcome |
|---|---|---|---|---|
| **M1** | 2/2 done, 18.5 / 23.4 s | `XAGG -> Large-Scale Aggregate` | n/a | ✅ Module 26's pattern guard still fires; correct 2024-vs-2026 statute mix |
| **CR3** | 2 runs, 164.3 s done / 82.0 s error | `XNETWORK -> Meta-Analysis` | ✅ `record_consistency` plan logged | run 1 reproduces Module 29's recorded result exactly (right verdict, both FIRs, wrong case tag); run 2 hit the known verifier rejection |
| **G1** | 2 runs, 161.5 s done / 123.3 s error | `XNETWORK -> Meta-Analysis` | ✅ `caseload_review` plan logged | run 1 correct and rich (9 / 52 plus repeat offenders); run 2 hit the verifier rejection |
| **G6** | 2/2 done, 155.2 / 139.4 s | `XNETWORK -> Meta-Analysis` | ✅ `orientation_note` plan logged | both correct — district focus and case mix |
| **M2** | 2/2 done, 44.4 / 57.5 s | `XAGG -> Large-Scale Aggregate` | now skips | ✅ **now the honest refusal** (below) |
| **G3** | 2/2 done, 31.3 / 24.2 s | `XAGG -> Large-Scale Aggregate` | now skips | ✅ held; **byte-identical answers across both runs** |
| **CR7** | 2/2 done, 17.8 / 12.7 s | `XAGG -> Large-Scale Aggregate` | now skips | ✅ held; correct consistency verdict on FIR 891-24 |

All three of Module 29's deterministic plans were observed firing in
`backend.log` — `record_consistency` ×2, `caseload_review` ×2,
`orientation_note` ×2. **Module 29 is intact.**

**M2's new answer, verbatim:**

> The system's data model does not currently classify police stations as
> general-purpose vs. specialized for specific crime types, so it is not
> possible to compare caseload growth rates between these categories
> [Document 1].

This is the outcome Module 44's brief asks for: M2 no longer asserts a
station-type split it cannot compute. It will still score FactualCorrectness
0.0 against a gold answer that *does* state a split (9 of 73 from 2 of 19
stations) — but it is now **honestly** 0.0 rather than confidently wrong, and
the gold answer's premise deserves the challenge Module 44 anticipated.

**The CR3 and G1 `error` runs are pre-existing, not caused by this change.**
Both are the Meta-Analysis verifier rejection ("The synthesized answer could
not be verified as grounded in the sub-answers") that Modules 25 and 40 own.
Both questions still **reach** Meta-Analysis and still decompose via their
plans, which is what this module's regression guard had to establish.

### Two small accuracy notes, reported not tuned

- **G3** says *"82 of 94"* accused entries have a blank relationship; gold says
  **81 of 94**. Both G3 runs agree on 82, so it is deterministic, not noise.
  Off by one against gold and worth a look, but G3's dispatch and structure are
  unchanged by this module.
- **CR7** says *"1 verdict, 32 ongoing"* of 33 records; gold says **30 under
  trial, 1 convicted**. The load-bearing half — the FIR 891-24 consistency
  verdict — matches gold exactly.

Neither figure moved because of Module 41; both come from the same aggregates
these questions already used.

---

## 8. New defects found — left unfixed, tracked separately

1. **`_COMPLIANCE_TERMS` contains the bare word `"flag"`.** Any question with a
   weapon noun and the word "flag" resolves to `weapon_compliance_scan`,
   including genuinely compound ones the scan answers only half of (measured
   on the synthetic query in §3). Pre-existing looseness that Module 41 merely
   made *consequential*. Not fixed here: `xagg.py`'s keyword precision is the
   Modules 35/36 track's territory.

2. **G2's fix does not generalise to a natural paraphrase** (§6).
   `_COMPLETENESS_KEYWORDS` does not cover "files that could quietly go
   nowhere". Widening it is the same keyword-coverage work as (1), and is the
   single most useful follow-up for G2.

3. **G2's gold answer needs three aggregates, not one.** The
   departure-time-before-report-time inversion (13 of 73) and the walk-in
   complaint ↔ FIR soft-tag linkage are not computed by
   `_case_completeness_scan()`. Same shape as Modules 31–34's G1 sub-aggregates.

4. **G5's gold names two schema-shape observations** no aggregate produces (no
   chain-of-custody column; weapons joined to cases by a soft FIR-code match
   with no enforced key).

5. **Verifier false positive on numbered-list markers.** The grounding verifier
   reads a leading `"1."` in a markdown ordered list as a cited figure and
   appends a spurious "could not be confirmed" caveat. Reproduced on every G5
   run, before and after. `prompts/evaluator.txt` and the verifier are out of
   scope for this module.

6. **G3's 82-vs-81 and CR7's 32-vs-30 discrepancies** against gold (§7). Small,
   deterministic, and pre-existing — but neither has been explained.

7. **Nine of the 32 gold questions change dispatch** — CR6, CR7, CR8, M2, M4,
   M7, G2, G3, G5 (pinned by the all-32 negative control, assuming an XAGG
   route). Six previously scored 0.0. The other three plus G3 already scored
   1.0 and reach the **same** sub-agent as before: the LLM decomposer was
   already returning `decompose: false` for them and falling back to a single
   dispatch, so the guard removes two LLM round trips and their variance rather
   than changing the destination — visible in CR7's 12.7 s and G3's byte-identical
   repeat runs. **CR6, CR8, M4 and M7 were not re-run live here**; CR7 and G3
   were, and both held. Those four are the change most worth watching in
   Module 27's final rerun.

---

## 9. Process incident — must be read before trusting the branch history

**At 16:54 and again at 16:57, a `git merge origin/main` that this module did
not initiate appeared inside this module's worktree** (`D:/Rapids AI/muhafiz-m41`),
with conflicts in `src/pipeline/xagg.py` and staged content from Modules
35/36/45. It removed this file (then uncommitted), and two commits this module
did not make landed on its branch:
`99f10f6 docs: Module 41's tracker rows (pre-merge state)` and
`9295942 Merge origin/main into Module 41, teaching the resolver Modules 35/36`.

This is exactly the shared-directory hazard `WAVE2_ORCHESTRATION_PROMPT.md` §5
warns about — *"Two chats must never share a working directory"* — and it
already cost this project a commit once.

Handled as follows, and stated here so the history is auditable:

- The two commits authored by this module (`75c3fd8`, `598614f`) were pushed to
  `origin` as soon as the collision was noticed, so they could not be lost.
- The conflicted `xagg.py`, the worktree's plan file and the staged merge diff
  were backed up before anything was touched.
- **No live measurement in this file is affected.** The backend logged one
  `Application startup complete` at 16:45 with no reloader and never restarted,
  so every run in §4 and §7 was served by the Module 41 build, not by anything
  the merge wrote to disk afterwards.
- The merged HEAD was re-verified: **483 tests pass**, and the all-32 negative
  control still yields exactly the same nine-question change set.
- The merge left **two** Module 41 sections in `GOLD_QA_REMAINING_FIXES_PLAN.md`
  — this module's finding-stage one and PR #26's original hypothesis-stage one.
  The stale hypothesis section was removed, per the convention that a module's
  section is *replaced* with the finding.

If the merge commits were intentional, nothing is lost. If they were not, they
should be reviewed before this PR is merged.
