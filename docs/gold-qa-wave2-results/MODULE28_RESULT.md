# Module 28 — CR4: weapon-evidence chain routed to cross-case linkage instead of XAGG

**Branch:** `fix/router-weapon-evidence-chain-to-xagg`
**Brief:** `MODULE28_ROUTER_WEAPON_EVIDENCE_CHAIN_PROMPT.md`
**Split out of:** Module 21 (PR #12)
**Date of every measurement below:** 2026-09-08, this worktree
(`D:/Rapids AI/muhafiz-m28`), backend on `:8002`, `muhafiz-postgres` healthy,
model-server tunnel `/health` = 200.

**Question (CR4):**

> If we've got a weapon logged as evidence, can we tell who it was taken off
> and what happened to them?

---

## 1. Root cause

The brief's hypothesis was correct, and it is now confirmed by this module's
**own** live capture rather than inherited from Module 21's report.

Captured on `main` @ `dbd308f` (pre-change, this same worktree, same backend,
same data):

```
[step=supervisor:dispatch] Classified query as route='XGRAPH' -> sub-agent='Cross-Case Linkage'
```

verbatim answer:

> No cross-case connections or patterns were found for this query, across
> either entity-graph search or cross-case pattern synthesis. No community
> cluster in the case corpus is closely related to this specific question
> ("If we've got a weapon logged as evidence, can we tell who it was taken
> off and what happened to them?") (nearest cluster found was distance 0.184
> against a relevance cutoff of 0.145). Rather than describing an unrelated
> cluster, no cross-case network finding is being reported here.

That independently reproduces Module 21's two key numbers (0.184 vs. 0.145)
and its route finding.

The chain of failure:

1. CR4 names **no entity**, so the local LLM classifier sends it to **XGRAPH**.
2. XGRAPH dispatches to Cross-Case Linkage, whose `_recover_target_entity()`
   correctly returns `None` — there genuinely is no entity in the text.
3. With no seed, the tool falls back to its **recurring-entity-across-cases**
   traversal. That is architecturally the wrong shape: CR4 asks for *one
   example chain* (weapon → FIR → accused → status), not a recurrence pattern.
4. The relevance gate then refuses at 0.184 against the 0.145 cutoff — which
   Module 12 set from measured distances and Module 21 re-confirmed. **The
   gate is right; the route was wrong.** `xnetwork.py` and
   `RELEVANCE_DISTANCE_THRESHOLD` are untouched by this module.

**Second finding, not in the brief:** the brief allowed that the aggregate
might already exist ("`xagg.py` has a weapon family"). It does not.
`_weapon_compliance_scan()` (G5), `_weapon_recovery_rate_by_district()` (CP1)
and `_top_recurring_weapon_types()` are the whole weapon family, and none of
them can name who a weapon was taken off. So this module is **router + one
new aggregate**, in two separate commits, as the brief requires.

The data, however, was all there — nothing new is ingested or projected:

| Link | Where it already lives |
|---|---|
| weapon → case | `Weapon-[:BELONGS_TO_CASE]->Case` |
| weapon → person | `Person-[:OWNS]->Weapon`, written by `structured_projection._write_weapons()` from `weapon_register.recovered_from` |
| person → status on that case | `Person-[:INVOLVED_IN {role:'accused', arrest_status}]->Incident` |
| person → court/criminal-record outcome | `StructuredRecord.record_type='criminal_record'`, joined on the FIR number via the existing `_fir_key()` |

---

## 2. Change

Two commits, deliberately separate.

**Commit 1 — `feat(xagg): weapon -> FIR -> accused -> status evidence chain aggregate (CR4)`**

| File | Change |
|---|---|
| `src/pipeline/xagg.py` | `_WEAPON_ATTRIBUTION_TERMS`; `_weapon_evidence_chain()`; `render_weapon_evidence_chain()`; one dispatch line in `run_aggregate()` |
| `src/pipeline/harness/tools/xagg.py` | `weapon_evidence_chain` added to the `AggregateKind` Literal + one render branch |
| `src/pipeline/orchestrator.py` | the same render branch at both of its XAGG rendering sites |
| `tests/test_xagg.py` | 5 new tests |

**Commit 2 — `fix(router): route CR4's weapon-evidence attribution chain to XAGG, not XGRAPH`**

| File | Change |
|---|---|
| `src/pipeline/router.py` | `_WEAPON_EVIDENCE_CHAIN_XAGG_PATTERNS`, appended to `_XAGG_OVERRIDE_PATTERNS` |
| `tests/test_router.py` | 4 new tests (one parametrized over 6 paraphrases); 1 existing test's sample query swapped — see §3 |

**Why the fix sits in `router.py`:** the failure is a route classification, one
layer above the tool. Fixing it inside `cross_case_linkage.py` would mean
teaching a cross-case-recurrence tool to answer a single-example lookup, and
fixing it in `xnetwork.py` would mean loosening a gate that is behaving
correctly — the RC-1 cluster-dump regression this project already fixed once.

**Why no `supervisor.py` change** (Module 26's M1 fix needed one): CR4 and its
paraphrase match **none** of `_META_ANALYSIS_TRIGGER_PATTERNS`, verified
directly, so nothing decomposes the question away from XAGG's single-call
aggregate. The live stream confirms it — exactly one route event.

**The discriminator.** Every router pattern and the `xagg.py` dispatch are a
**conjunction of two term families**: a weapon term AND an attribution term
(`taken off`/`taken from`, `recovered from`, `seized from`, `trace … back`,
`whose`, `کس سے برآمد`, `کس کے قبضے`, `kis se baramad`). This is the house
technique — G5's own entry gates on weapon AND compliance.

A bare weapon keyword would have collided with **four** gold questions that
already work: G5 (weapon-register compliance, 1.0), CP1 (weapon recoveries per
district), M5 (which case types weapons appear in) and KB6 (forensics handling
guidelines). None of them asks *whose* weapon it was. And it cannot fire on
**CR2** — the collision Module 21 explicitly warned about — because CR2
carries no weapon vocabulary at all.

**Not touched:** `src/pipeline/xnetwork.py`, `RELEVANCE_DISTANCE_THRESHOLD`.

---

## 3. Unit tests

Command (shared interpreter, `PYTHONPATH=.` per orchestration doc §2.1):

```
PYTHONPATH=. "D:/Rapids AI/Evidence Intelligence Platform/.venv/Scripts/python.exe" \
  -m pytest tests/test_router.py tests/test_xagg.py tests/test_harness_supervisor.py \
            tests/test_harness_tool_xagg.py tests/test_orchestrator.py \
            tests/test_harness_agent_large_scale_aggregate.py \
            tests/test_harness_agent_cross_case_linkage.py tests/test_xnetwork.py -q
```

**501 passed, 1 skipped, 1 xpassed** — the skip and the xpass are both
pre-existing (`test_matches_m7_and_no_other_gold_question` skips because
`Gold_QA_Dataset_Final32.json`, the answer-less variant, is not in this
checkout). Individually: `tests/test_router.py` **118 passed**;
`tests/test_xagg.py` **90 passed, 1 skipped**.

The full `pytest -q` suite was **not** run — it empties the real
`muhafiz_entity_descriptions` Chroma collection (orchestration doc §2.2).

New tests:

| Test | What it pins |
|---|---|
| `test_cr4_weapon_evidence_chain_fires_to_xagg` | CR4's **literal gold text** routes to XAGG, `case_scope=cross_case` |
| `test_cr4_paraphrases_fire_to_xagg` | 6 non-gold paraphrases (English, Urdu script, Roman-Urdu) |
| `test_cr4_pattern_negative_control_against_all_other_gold_questions` | the pattern family matches CR4 and **none** of the other 31 |
| `test_module28_changes_no_other_gold_question_route` | the stronger all-32 control — see §7 |
| `test_cr4_weapon_evidence_chain_returns_gold_chain` | regression pinned to CR4's literal gold **answer**: FIR 891/24, شہزیب عرف شابی, گرفتار، بعد ازاں سزا یافتہ, plus the FIR-matching caveat text |
| `test_cr4_chain_reports_honestly_when_nothing_is_attributable` | empty/no-owner case degrades to an honest statement, not silence |
| `test_cr4_dispatches_from_run_aggregate` | the weapon-AND-attribution conjunction reaches the new aggregate |
| `test_g5_still_reaches_the_compliance_scan_not_the_chain` | G5 keeps the compliance answer |
| `test_cr4_attribution_terms_negative_control_over_gold32` | the `xagg.py` dispatch half of the all-32 control |

**One existing test changed.**
`test_no_case_id_prefixes_the_llm_call_with_active_case_none` used CR4's own
text as its sample query and stubbed the LLM to return `XGRAPH` — itself a
record of the misroute this module fixes. That text is now intercepted before
the LLM call, so it no longer exercises what the test is about. Its sample was
swapped for another within-case-shaped weapon question that matches no
override (`"What is the condition of this weapon and where is it being
stored?"`). Its assertions are unchanged.

---

## 4. Live verification

Backend `:8002`, `admin@example.com` / `MuhafizAdmin2026!`, **All Cases** (no
`case_id`). Raw SSE streams were captured to disk during the run (one file per
question, `*_BEFORE` for the pre-change baselines) and are quoted verbatim
below; they are working artefacts and are not committed.

**Exact question sent:** CR4's gold text, verbatim.

**Every `route=` event in the whole stream** — read in full, because
Meta-Analysis emits a *second* route event for a decomposed sub-query and a
"last route wins" parse is misleading. There is exactly **one** here, i.e. no
decomposition happened:

```
[step=supervisor:dispatch] Classified query as route='XAGG' -> sub-agent='Large-Scale Aggregate'
[step=supervisor:dispatch] Large-Scale Aggregate completed with status=ok
```

`status=done`, 12.5 s.

**Verbatim answer:**

> Yes, for 30 of the 32 weapons logged as evidence, the register records who
> the weapon was recovered from, and that person’s status on the same case is
> directly documented in the case file [Document 1]. For example:
>
> - A **30 بور پستول, بغیر لائسنس** logged in **FIR 891/24** was recovered
>   from **شہزیب عرف شابی**, who was recorded as **گرفتار، بعد ازاں سزا یافتہ**
>   ("arrested, later convicted"). The criminal-record system also notes
>   "Convicted, on bail pending appeal" for this person in the same FIR.
> - Other weapons (e.g., **30 بور پستول بمعہ 3 گولیاں, بغیر لائسنس** in
>   **FIR 214/26**) were recovered from individuals like **شہزیب عرف شابی**,
>   **کاشف**, **فیصل**, etc., with statuses such as **زیر تفتیش** ("under
>   investigation") or **Under trial**.
>
> However, **2 of the 32 weapons** have no person linked to them—they were
> recovered from crime scenes rather than from accused individuals (e.g.,
> **عام لکڑی کی چھڑی** in **FIR-117-26**; **خالی خول (فائرنگ کا شاہد)** in
> **FIR-266-26**).
>
> **Limitation**: The weapon → person link is not a database-enforced key. The
> **recovered_from** field contains only a name, which is matched to the
> accused named in the **same FIR** (i.e., weapon → FIR number → accused).
> This method works for the data provided but could fail if the same name
> appears in multiple FIRs.

**Where the numbers come from** (direct AGE probe, same session):
32 `Weapon` nodes; 30 with an `OWNS` edge; 2 without
(`عام لکڑی کی چھڑی، ایک عدد` in `fir-117-26`,
`خالی خول (فائرنگ کا شاہد)، جائے وقوعہ سے برآمد` in `fir-266-26`).
`PERSON-685fc54914` = شہزیب عرف شابی has `arrest_status` `گرفتار` on
`fir-214-26` and `گرفتار، بعد ازاں سزا یافتہ` on `fir-891-24`; the
criminal_record with `source_case_ref` `"FIR 891/24, PS Jhang Road
Faisalabad"` carries `conviction_status` `"Convicted, on bail pending
appeal"`.

---

## 5. Gold comparison

Gold: *Yes — e.g. the 30-bore pistol logged as evidence in FIR 891/24 was
recovered from شہزیب عرف شابی, whose status on that case is گرفتار، بعد ازاں
سزا یافتہ. The trail runs weapon → FIR number → accused, relying on FIR-number
matching rather than an enforced key.*

| Gold element | Live answer | Verdict |
|---|---|---|
| Answer is "yes, we can" | "Yes, for 30 of the 32 weapons…" | match |
| The 30-bore pistol | `30 بور پستول` | match |
| Logged in FIR 891/24 | `FIR 891/24` | match |
| Recovered from شہزیب عرف شابی | `شہزیب عرف شابی` | match |
| Status گرفتار، بعد ازاں سزا یافتہ | verbatim, plus the English gloss | match |
| Trail runs weapon → FIR number → accused | stated verbatim as the Limitation | match |
| Relies on FIR-number matching, not an enforced key | stated verbatim | match |

**Verdict: full match on every element gold names**, including gold's own
hedge, which the renderer states rather than hides (brief §3). Before the
change this question returned a refusal and scored 0.0.

Honest additions beyond gold, offered as strengths rather than claimed as
gold matches: the 30-of-32 coverage figure, the two crime-scene weapons with
no attributable owner, and the criminal-record cross-reference
("Convicted, on bail pending appeal").

The worked example is not cherry-picked by hard-coding: the aggregate sorts
chains so the fullest one — an accused status that runs all the way to a
decided outcome (`_conviction_is_settled`, reused from CR7's cross-check)
**and** a criminal-record outcome — comes first. On this data that is FIR
891/24, the same one gold picked. If the data changed, the rule would pick a
different, equally complete chain, not silently lose the answer.

---

## 6. Non-gold paraphrase

**Sent:** *"For weapons we've seized as evidence, can we trace them back to
whoever they were taken from?"* — shares no distinctive keyword with CR4's
literal text ("logged as evidence", "taken off", "what happened to them" are
all absent).

**Before the change** (`main` @ `dbd308f`):
`route='XGRAPH' -> sub-agent='Cross-Case Linkage'`, and the same refusal —
*"nearest cluster found was distance 0.178 against a relevance cutoff of
0.145."*

**After:** one route event,
`route='XAGG' -> sub-agent='Large-Scale Aggregate'`, `status=done`, 11.5 s.
Verbatim (abridged to its first block; full text captured in this run):

> Yes, for 30 of the 32 weapons logged as evidence, the register records who
> the weapon was recovered from … A **30 بور پستول, بغیر لائسنس** (unlicensed
> 30-bore pistol) logged in **FIR 891/24** was recovered from **شہزیب عرف
> شابی** (Shahzib a.k.a Shabi), who was recorded as **گرفتار، بعد ازاں سزا
> یافتہ** (arrested, later convicted). … **Caveat**: The weapon → person link
> is not enforced in the database.

**Result: passes.** The paraphrase produces the same real chain, so this is a
capability fix, not curve-fitting.

---

## 7. Regression guard

### All-32 negative control (mandatory for a routing module)

Two forms, both automated in `tests/test_router.py` / `tests/test_xagg.py`.

**(a) Pattern-family match over the 32 gold questions** — verbatim result:

```
--- router weapon-chain pattern family matches ---
 MATCH CR4 [0]
--- xagg dispatch conjunction matches ---
 XAGG-CHAIN CR4
--- xagg G5 compliance conjunction (must be unchanged) ---
 G5-PATH G5
paraphrase router match: True
paraphrase xagg match: True
```

CR4 and only CR4. Unlike Module 26's M1/M5 pair there is no legitimate
co-match to allow, so the assertion is exact equality with `["CR4"]`.

**(b) Whole-route control** — the stronger form the brief actually asks for,
and the check that would have caught the M4/G3 collision of PR #8. The test
computes `_deterministic_route_override()` for all 32 gold questions with the
new pattern family in place, then again with it removed, and asserts the diff:

```
changed == {"CR4": (None, "XAGG")}
```

i.e. **CR4 is the only gold question whose deterministic route changes**, and
it changes from "no deterministic override at all" to XAGG. `CR2` and `G5` are
additionally asserted by name. Passing.

### Live regression runs

Both re-run on `main` @ `dbd308f` and again on this branch, same backend, same
data, so the comparison is a real before/after and not an inherited claim.

**CR2** — *"Is there anyone with an earlier case already on record who has
since resurfaced as a suspect in a newer, separate case?"* — the specific
collision Module 21 warned about.

| | Before | After |
|---|---|---|
| route events | 1 · `route='XAGG' -> 'Large-Scale Aggregate'` | 1 · `route='XAGG' -> 'Large-Scale Aggregate'` |
| answer | 4 persons: فیصل, طارق (fir-202-26/fir-401-26), شہزیب عرف شابی (fir-214-26/fir-891-24), عاصم رشید (fir-64-26/fir-65-26) | **identical** |
| elapsed | 12.5 s | 8.4 s |

**Unchanged.** (The trailing "could not be verified as an accurate paraphrase;
showing the raw computed aggregate instead" note is present in both — a
pre-existing verifier behaviour, not introduced here.)

**G5** — *"Baramad shuda hathiyaron ki record keeping…"* — lives in the same
weapon keyword space and currently scores 1.0.

| | Before | After |
|---|---|---|
| route events | 2 · `XAGG -> Meta-Analysis`, then `XAGG -> Large-Scale Aggregate` | **identical** |
| answer | 30 of 32 (94%) unlicensed; 2 with no licence status | **identical, word for word** |
| elapsed | 12.6 s | 12.8 s |

**Unchanged.** G5 is also the live instance of the parsing trap the brief
warns about: it really does emit two route events. Reading only the last one
would have reported G5 as "Large-Scale Aggregate" and missed that
Meta-Analysis handled it first.

---

## 8. New defects found

Nothing that warrants a new module. Two observations, both deliberately left
alone:

1. **G5's citation-verifier footnote.** Both the before and after runs end
   with *"A cited claim ([Document 1]) could not be confirmed against its
   source: Claim cites figure(s)/identifier(s) not found in its source text:
   1."* It is byte-identical before and after this change, so it is
   pre-existing and outside this module. It is the same
   verifier-interaction family Module 25 owns; flagging it here for Module 27's
   final rerun rather than opening a module on one footnote.

2. **`_top_recurring_weapon_types()` is structurally dead on this data.** Its
   own docstring already records why (weapon `entity_id`s are FIR-scoped by
   construction, so no weapon node ever spans cases), and the live probe
   confirms it — all 32 weapons belong to exactly one case each. Not a defect
   introduced or worsened here, and Module 23 owns `xagg.py`'s weapon family
   for this wave, so it is left to that track.

**Deliberately not done:** `docs/gold-qa-wave2-results/README.md`'s index row
for Module 28 is left at "in progress". Module 23's row is adjacent in the
same table and Track A is editing it concurrently; a one-line edit there would
create exactly the avoidable conflict the orchestration doc §5 warns about.
