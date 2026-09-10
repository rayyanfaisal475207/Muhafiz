# Module 44 — M2 and CS4: fluent but factually wrong

**Branch:** `fix/m7-m2-cs4-factual-accuracy` (PR #38) · **Questions:** M2 (English),
CS4 (roman-Urdu)

## Verdict, stated first

- **CS4 — fixed.** It had no aggregate at all and was being answered by the
  person-recurrence fall-through. It now returns gold exactly — **وقاص
  (Waqas), CNIC 00000-9000020-1** — on **3 of 3** live runs, with gold's own
  "this is expected" caveat earned from the measurement rather than asserted.
- **M2 — gold IS computable, and it now is.** The brief asked whether it was.
  It is: **9 of 73 FIRs (~12.3%) from 2 of 19 stations**, reproduced exactly,
  3 of 3 live. But **gold's answer does not answer gold's own question**, and
  on the growth half the data points the other way. §5 sets out that
  challenge with the numbers.

---

## 1. Root cause

### CS4 — the person-recurrence fall-through, for the third recorded time

`resolve_aggregate_kind()` returned **`graph_recurrence_person`** for CS4's
gold text. Measured, not inferred:

```
resolve_aggregate_kind(CS4 gold text) -> "graph_recurrence_person"
run_aggregate(CS4 gold text)          -> {"kind": "graph_recurrence",
                                          "entity_type": "Person",
                                          "results": [4 people in 2 cases each]}
```

Two causes compounding:

1. **Nothing in `_CRIMINAL_RECORD_KEYWORDS` matches "criminal-history
   records".** That tuple carries `"criminal record"`, `"criminal-record"`
   and `"criminal records"` — none of which is a substring of
   `"criminal-history records"`. CS4 therefore never reached CR7's family
   either, and fell the whole length of the chain to `_PERSON_KEYWORDS`,
   which matches "shakhs".
2. **There was no aggregate for the question anyway.** Even a keyword hit
   would have landed on CR7's status/court-outcome crosscheck, which answers
   something else entirely.

The brief noted CS4 "has been 0.0 since before this wave and was in Module
21's original list; the relevance gate was cleared of blame and nothing has
addressed it since". That is right, and the reason is the second cause: the
gate was never the problem, and no module since has added the missing family.

**The second-order effect matters as much as the first.** Because
`graph_recurrence_person` is in `_ENTITY_RECURRENCE_AGGREGATE_KINDS` — the
"we recognised a noun, not the question" tier —
`resolves_to_specific_aggregate()` was False, so Module 41's guard let CS4 go
to Meta-Analysis. The committed decomposition **inverted the set
difference**:

> "koi aisa shakhs hai jo **criminal-history records mein nahi hai**" —
> which of *our* accused are absent from the criminal-records system

That is the opposite direction from the one asked, over a much larger set.
The recorded CS4 answer then failed on both halves at once: *"The synthesized
answer could not be verified as grounded in the sub-answers.; Could not answer
sub-question (encountered an error) …; No community cluster … is closely
related to this specific question"*, on `route=XGRAPH`.

Giving CS4 a purpose-built aggregate closes both: the dispatch, and the
decomposition the missing dispatch was licensing.

### M2 — a refusal whose claim had stopped being true

The brief was right that M2 now returns the honest station-type refusal
rather than an invented split (Module 41's doing), and right to ask whether
gold is computable at all. **It is**, and the refusal's own wording is where
the defect sits:

> "Station-type-normalized aggregates are not available: this system's data
> model does not currently classify a police station as general-purpose vs.
> specialized for a particular crime type, **so caseload cannot be compared
> across that dimension**."

The first clause is narrowly true — there is no `station_type` field. **The
second does not follow, and is false.** Hand-written Cypher over the live
graph:

```cypher
MATCH (s:PoliceStation) RETURN s.station_id, s.name          -- 19 stations
MATCH (c:Case)-[:FILED_AT]->(s:PoliceStation)
RETURN s.station_id, count(c)                                 -- 73 FIRs
```

```
PS-ISB-CYBER   سائبر کرائم سرکل، اسلام آباد   5      <- Cyber Crime Circle, Islamabad
PS-RWP-CYBER   سائبر کرائم سرکل، راولپنڈی     4      <- Cyber Crime Circle, Rawalpindi
PS-FSD-WOMEN   خواتین تھانہ، فیصل آباد         5
PS-LHR-M2      موٹروے پولیس اسٹیشن ایم ٹو      5
... 15 further تھانہ (thana) entries
```

Two of the 19 stations are **Cyber Crime Circles** — not thanas, but
single-crime-type units — and they carry **9 of the 73 FIRs**. That is gold's
claim exactly (*"9 of 73 FIRs (~12%) from just 2 of 19 stations"*), and it
needs no station-type dimension: only a per-station count and the station's
own recorded name.

This is the same call Module 31 made when it retired `_UNSUPPORTED_AGE`,
recorded in this file's own comments: *"the old message was asserting
something false about the data model."*

---

## 2. Change

| File | Change |
|---|---|
| `src/pipeline/xagg.py` | **CS4:** `_is_criminal_record_local_gap()` (two-signal predicate), `_criminal_record_local_match_gap()`, `render_criminal_record_local_match_gap()`, resolver + dispatch entries, `XAGG <kind>` log line. **M2:** `_classify_station_specialisation()`, `_station_caseload_by_specialisation()`, `render_station_caseload_by_specialisation()`, re-pointed dispatch, `XAGG <kind>` log line. `unsupported_station_type` removed from `_UNSUPPORTED_AGGREGATE_KINDS`; `_UNSUPPORTED_STATION_TYPE` kept as a named constant with the measurement that retired it recorded next to it. |
| `src/pipeline/harness/tools/xagg.py` | Both kinds added to the `AggregateKind` Literal **in the same commit as the aggregates**, plus both renderers. |
| `src/pipeline/orchestrator.py` | Both renderers wired into both legacy XAGG rendering sites. |
| `tests/test_xagg.py`, `tests/test_harness_supervisor.py` | §3. |

**Why the CS4 predicate needs two signals.** The criminal-record vocabulary
alone belongs to **CR7**, which reads the same records, asks a different
question (how many are settled, and do they agree with the court's outcome),
and scores 1.0 today. So the dispatch requires a criminal-history term **and**
an explicit no-match term. CR7's gold text carries "مطابقت رکھتے" — *do the
two agree?*, the POSITIVE form — which is exactly why no bare "match"/"مطابقت"
token is in the gap list. The all-32 equality control enforces the separation.

**Why the M2 classification is name-derived, and what makes that honest.** A
name is weaker evidence than a modelled field and the answer must not pretend
otherwise, so the rendered output states the basis in its own words ("this
system has no station-type field … derived from each station's own recorded
name") and **lists every station with its bucket**, so a reader can check the
call. An unrecognised name stays general-purpose — the classifier never
invents a specialisation.

**Three buckets, not two, and the third is the honest part.** Two further
stations are specialised by something that is *not* a crime type: خواتین
تھانہ (Women's Police Station — a class of complainant, across many crime
types) and موٹروے پولیس اسٹیشن (Motorway — a jurisdiction). M2 asks
specifically about stations "set up for one specific type of crime". Folding
those two in would give 4 stations / 19 FIRs and contradict gold; silently
calling them general-purpose would hide a judgement call the reader should
see. They get their own bucket and are named.

**Where the fix does NOT sit.** Nothing in `supervisor.py`, `router.py`,
`meta_analysis.py` or `xnetwork.py` was touched. CS4's decomposition problem
is fixed *structurally* by Module 41's existing guard the moment CS4 resolves
to a real aggregate — which is what that guard was built to do.

---

## 3. Unit tests

```
PYTHONPATH=. python -m pytest tests/test_xagg.py tests/test_harness_supervisor.py \
    tests/test_harness_tool_xagg.py tests/test_harness_agent_large_scale_aggregate.py \
    tests/test_orchestrator.py tests/test_router.py \
    tests/test_harness_agent_meta_analysis.py -q
728 passed
```

Pinned to the **literal gold text** of each question:

**CS4**
- `test_module44_cs4_finds_the_one_unmatched_subject` — gold's answer:
  exactly one, CNIC `00000-9000020-1`. The stub's accused roster deliberately
  contains a CNIC with no criminal record at all, so an **inverted** set
  difference — the direction Meta-Analysis's decomposition drifted into —
  fails this test.
- `test_module44_cs4_matches_on_cnic_not_name` — the load-bearing join
  decision, asserted alone. The unmatched subject's NAME does appear among
  the accused, on a different CNIC; a name-keyed join returns zero unmatched
  people and turns gold's "yes, exactly one" into "no".
- `test_module44_cs4_records_without_a_cnic_are_neither_matched_nor_unmatched`
  — a record with no join key is counted and reported, never dropped into
  either bucket.
- `test_module44_cs4_predicate_needs_both_signals_and_leaves_cr7_alone` —
  CR7's gold text must not match, and must keep `criminal_record_court_crosscheck`.
- `test_module44_cs4_all32_negative_control_equality` — **equality**: exactly
  `["CS4"]`.
- `test_module44_cs4_render_states_golds_expected_caveat_rather_than_flagging_a_defect`
  — and must earn it by citing 31 of 32 matching, so it is a measured
  judgement rather than gold's wording pasted in.

**M2**
- `test_module44_station_specialisation_classifies_every_real_station_name` —
  parametrised over **all 19 real station names** copied from the live graph.
  The load-bearing cases are خواتین تھانہ (contains تھانہ, so it must not
  fall through on a substring) and the Motorway station.
- `test_module44_m2_reproduces_golds_station_concentration` — 9 of 73, 2 of
  19, share 0.123, and the two Cyber Circles by id; plus the held-out bucket
  at 2 stations / 10 FIRs, which is what folding them in would break.
- `test_module44_m2_lead_carries_both_halves_of_the_question` — pinned to a
  **measured** failure, see §4.
- `test_module44_m2_states_its_derivation_and_declines_a_growth_rate`.
- `test_module44_m2_all32_negative_control_equality` — **equality**: exactly
  `["M2"]`.
- `test_module44_station_type_refusal_is_no_longer_reachable` — no gold
  question resolves to `unsupported_station_type` and it is gone from the
  refusal set, so `resolves_to_specific_aggregate()` is not reasoning about a
  kind nothing produces.
- `test_module44_unrecognised_station_name_stays_general_never_invents_a_specialisation`.

**Supervisor**
- `test_module44_cs4_gold_text_skips_decomposition_and_reaches_the_aggregate`.
- `test_module41_m2_station_type_question_skips_decomposition` — the
  assertion is unchanged from Module 41's; its *reason* changed, and the
  docstring records both.
- `test_module41_unsupported_refusals_still_count_as_resolved` — new, so the
  half of Module 41's policy that survives (officer, trend) keeps a test after
  station-type left the set.
- `test_module41_all_32_gold_questions_dispatch_change_is_exactly_the_expected_set`
  — **CS4 added** to the changed set, from nine questions to ten. That test's
  own docstring says anything added there "is a real behavioural change that
  needs re-verifying live, not a test to update casually"; §4 is that
  verification.

Both kinds were added to `XAggToolResult.aggregate_kind`'s hand-maintained
`Literal` **in the same commit as the aggregates**, and to the membership
test that exists because five earlier modules were bitten by omitting them —
the failure mode where every unit test passes and `/api/chat` returns an empty
answer with `status=None`.

---

## 4. Live verification

`POST /api/chat`, `admin@example.com`, All Cases, port 8014. Three runs each.
`grep -c "rate limit" backend.log` → **0** across every run in this module.

### CS4 — 3 of 3, gold exactly

**Question sent (verbatim gold text):**
`Kya wusee criminal-history records mein koi aisa shakhs hai jo hamare apne darj kiye hue kisi case se match nahi karta?`

| Run | `route=` / sub-agent | Elapsed |
|---|---|---|
| 1 | `route='XAGG'` → `Large-Scale Aggregate` | 81.0 s |
| 2 | `route='XAGG'` → `Large-Scale Aggregate` | 113.2 s |
| 3 | `route='XAGG'` → `Large-Scale Aggregate` | 66.9 s |

Note the route: the recorded pre-fix run had CS4 on **XGRAPH**. It reached
XAGG on all three runs here, and the aggregate log line proves which family
answered:

```
[INFO] src.pipeline.xagg: XAGG criminal_record_local_match_gap: 33 criminal record(s)
  over 32 distinct subject(s) vs 94 local accused entr(ies) (92 distinct CNIC);
  1 subject(s) with no local match [00000-9000020-1]; 0 record(s) carry no CNIC
```

**Verbatim answer (identical on all three runs):**

> Yes — exactly one person in the criminal-records system has no matching
> accused record in any FIR we registered:
>   - **وقاص** (CNIC 00000-9000020-1) — criminal record(s)
>     **criminal_record:CR-C106-1**; the same NAME does appear among our
>     accused, but on a different CNIC, so this is not a spelling mismatch.
>
> This is expected, as the criminal-records system is an external/federal
> record source, not a mirror of our FIRs. It can legitimately hold someone we
> have never charged. 31 of 32 subjects in the criminal-records system DO
> match a local accused record, making this a single outlier, not a systematic
> issue [Document 1].

### M2 — 3 of 3, gold's figures exactly

**Question sent (verbatim gold text):**
`Is caseload growing faster at our general-purpose stations, or at the handful set up for one specific type of crime?`

| Run | `route=` / sub-agent | Elapsed |
|---|---|---|
| 1 | `route='XAGG'` → `Large-Scale Aggregate` | 114.4 s |
| 2 | `route='XAGG'` → `Large-Scale Aggregate` | 127.3 s |
| 3 | `route='XAGG'` → `Large-Scale Aggregate` | 124.4 s |

```
[INFO] src.pipeline.xagg: XAGG station_caseload_by_specialisation: 19 station(s),
  73 FIR(s); crime_type_specialised=2 station(s)/9 FIR(s);
  other_specialised=2 station(s)/10 FIR(s); general_purpose=15 station(s)/54 FIR(s);
  9 FIR(s) with no resolvable incident year, 0 case(s) at an unknown station
```

**Verbatim answer (identical on all three runs), opening lines:**

> Growth: caseload is rising fastest at the 15 general-purpose station(s) — 7
> FIRs in 2024 to 39 in 2026, against 3 to 5 at the 2 single-crime-type
> station(s) — though even so, 9 of 73 FIRs (~12.3%) are carried by just 2 of
> 19 stations, the ones set up for a single type of crime.
>
> 9 of 73 FIRs (~12.3%) are filed at the 2 of 19 stations set up for one
> specific type of crime:
>   - سائبر کرائم سرکل، اسلام آباد (PS-ISB-CYBER): 5 FIRs
>   - سائبر کرائم سرکل، راولپنڈی (PS-RWP-CYBER): 4 FIRs
> …
> The 2024 baseline is 13 FIRs across all 19 stations, so a per-station growth
> RATE is not a responsible figure to quote on this corpus — the counts behind
> each rate are single digits. The share of caseload above is the claim the
> data supports.
>
> Basis: this system has no station-type field. The grouping is derived from
> each station's own recorded name, and every station is listed above so the
> classification can be checked.
>
> _The natural-language summary could not be verified as an accurate
> paraphrase; showing the raw computed aggregate instead._

**Two measured behaviours here, both worth recording honestly.**

**(a) The first version of this rendering lost gold's numbers to the
paraphrase.** With the concentration as the headline and the growth split
further down, three live runs answered the growth half accurately and
**dropped the 9-of-73 / 2-of-19 concentration entirely** — a good answer to
the question that omits the figures the question is scored against. Splitting
the lead across two lines was not enough either: the paraphrase kept the
growth clause and dropped the share clause on 3 of 3 runs. Both halves are
now in **one sentence**, which gives a paraphrase no seam to drop, and
`test_module44_m2_lead_carries_both_halves_of_the_question` pins it.

**(b) The verifier then rejected the paraphrase on all three runs — and it
was right to.** The log gives the reason:

```
[INFO] src.pipeline.verifier: Structured-aggregate verifier: grounded=False
  leaked=None unsupported_numbers=['457'] — Paraphrase states number(s) not
  present in the computed result: 457.
```

`457` is in no aggregate this module produces. 39 ÷ 7 ≈ 5.57, i.e. a **457%
increase** — the model derived exactly the growth rate the rendering says is
not a responsible figure to quote, and the deterministic verifier caught it
and served the raw computed aggregate instead. That is the designed fallback
working, and the served answer contains **every** gold fact plus its
derivation. It is left as it is on purpose: suppressing the year counts would
make the paraphrase pass by removing the evidence for the half of M2 that
actually asks about growth, which is the wrong trade.

---

## 5. Gold comparison

### CS4 — exact match

| Claim | Gold | Measured | Verdict |
|---|---|---|---|
| Is there such a person? | Haan, ek (yes, one) | Yes, exactly one | ✅ |
| Who | Waqas | وقاص | ✅ same person |
| CNIC | 00000-9000020-1 | 00000-9000020-1 | ✅ exact |
| Why it's not a defect | "external/federal record source, not a mirror of local FIRs" | same, and quantified: 31 of 32 subjects DO match | ✅ and stronger |

**One reservation, reported not hidden:** the answer renders the name in Urdu
script (وقاص) because that is how the record stores it; gold writes
"Waqas". The CNIC is identical and unambiguous, but an evaluator matching on
the Latin spelling alone could mark this down. A name-transliteration map
would fix the score and would be tuning to the judge, not a capability
improvement, so it was not added.

### M2 — gold's figures reproduced; gold's own question challenged

| Claim | Gold | Measured | Verdict |
|---|---|---|---|
| Specialised-unit FIR count | 9 | 9 | ✅ exact |
| Corpus size | 73 | 73 | ✅ |
| Share | ~12% | 12.3% | ✅ |
| Specialised stations | 2 | 2 (both Cyber Crime Circles) | ✅ exact |
| Total stations | 19 | 19 | ✅ |
| **"growing faster at … the handful set up for one specific type of crime"** | implied | **contradicted** — see below | ❌ |
| **"disproportionate given they're single-purpose"** | asserted | **overstated** — see below | ⚠️ |

**Challenge 1 — gold does not answer its own question.** M2 asks *which group
is growing faster*. Gold's answer is a static share ("already carry real
load: 9 of 73 FIRs (~12%)"), which is a different quantity. On the growth
question the data points the other way:

| Group | 2024 | 2026 |
|---|---|---|
| Crime-type specialised (2 stations) | 3 | 5 |
| Other specialised (2 stations) | 3 | 7 |
| General-purpose (15 stations) | 7 | 39 |

Caseload is rising **fastest at the general-purpose stations**, by a wide
margin, in absolute terms and in ratio. Gold's framing implies the opposite.
The system says so directly, in its first sentence, and still gives gold's
concentration figures.

**Challenge 2 — "disproportionate" is weaker than gold makes it sound.** 2 of
19 stations is **10.5%** of the stations carrying **12.3%** of the FIRs.
That is barely above proportional — roughly a sixth more than an even split
would give, on a base of 9 FIRs, where a single FIR moves the figure by more
than a percentage point. It is a fair observation that single-purpose units
carry real load; it is not evidence of disproportionate load.

**A caveat on the year split itself, stated in the answer rather than
buried.** 9 of the 73 FIRs carry no resolvable incident year, and the 2024
baseline is only 13 FIRs across 19 stations. Every per-station growth rate
therefore rests on single-digit counts, which is why the aggregate reports
the counts and declines to quote a rate. The group-level comparison above is
robust to that (7 → 39 does not turn on one FIR); a per-station one would not
be.

---

## 6. Non-gold paraphrase

**CS4** — `Kya criminal record system mein koi aisa banda hai jiska hamare FIR
accused se koi match nahi milta?` (roman-Urdu, different wording throughout —
*banda* not *shakhs*, *nahi milta* not *nahi karta*, and no "criminal-history"
at all). `route='XAGG'` → `Large-Scale Aggregate`, 110.6 s:

> Yes — exactly one person in the criminal-records system has no matching
> accused record in any FIR we registered:
>   - **وقاص** (CNIC 00000-9000020-1) … Matched on CNIC, not on name. … 31 of
>   32 subjects **DO** match a local accused record, so this is a single
>   outlier, not a systematic linkage failure.

Two further paraphrases were checked at the dispatch layer and also resolve
correctly: *"Is there anyone in the wider criminal history database who does
not match any accused on our own FIRs?"* and *"Does the criminal records
system list anyone we have no matching FIR accused for?"* — English, and
sharing no phrasing with the gold text. This is a capability, not a string
match.

**M2** — `Are our specialised station types taking on more cases than the
general purpose stations?` `route='XAGG'` → `Large-Scale Aggregate`, 100.0 s,
and here the verifier **passed** the paraphrase, so this is the fluent form of
the answer:

> No, specialised station types are not taking on more cases than
> general-purpose stations. According to [Document 1], the 2 stations set up
> for one specific type of crime (سائبر کرائم سرکل، اسلام آباد (PS-ISB-CYBER)
> and سائبر کرائم سرکل، راولپنڈی (PS-RWP-CYBER)) handle **9 FIRs** (~12.3% of
> the total caseload), while the remaining **15 general-purpose stations**
> handle **54 FIRs**. … The growth rate for general-purpose stations (7 FIRs
> in 2024 to 39 in 2026) also far outpaces that of specialised stations (3 to
> 5 …).

**A coverage limitation, measured and reported rather than papered over.**
M2's dispatch still rests on `_STATION_TYPE_KEYWORDS`, which Module 44 did not
widen — only the kind it returns changed. A genuinely different phrasing that
avoids all of its vocabulary misses: *"Do the specialist units handle a bigger
share of our cases than the ordinary police stations?"* resolves to
`station_or_category_counts`, the trailing catch-all. Widening it was
deliberately not done in this module: `_STATION_KEYWORDS` sits a few rungs
lower in the same chain and every widening candidate ("units", "ordinary
stations") risks pulling plain per-station questions into this family. Filed
as Module 56 in §8 with the measurement attached.

---

## 7. Regression guard

Every question re-run live on the same backend, one run each, after both
modules' changes. `grep -c "rate limit" backend.log` → **0**.

| Q | Route | Result |
|---|---|---|
| **M5** | XAGG → Large-Scale Aggregate | ✅ correct. 2024: Arms Ordinance §13 with PPC 34/392 only; 2026: adds CNSA §9(c) and PPC 302. `XAGG weapon_statute_cooccurrence` in the log. |
| **M4** | XAGG → Large-Scale Aggregate | ✅ correct, and substantive. 218 section entries over 36 statutes vs 1 of 33 criminal records reaching a verdict; states the divergence gold asks for. `XAGG statute_court_stage_join` in the log. |
| **M1** | XAGG → Large-Scale Aggregate | ✅ correct. 2024 PPC 13 / Arms 13; 2026 PPC 39, Arms 16, plus CNSA 12, PECA 9, DV 4. |
| **G2** | XAGG → Large-Scale Aggregate | ✅ correct, and **still fixed by Module 41** — 9 of 73 FIRs with no incident date (listed by FIR number) and 52 with no investigation status. |
| **G5** | XAGG → Large-Scale Aggregate | ✅ correct — 30 of 32 (94%) unlicensed, 2 with no licence status. Carries a citation-consistency footnote (see §8). |
| **G3** | XAGG → Large-Scale Aggregate | ✅ correct, answered in Urdu. Reports 82 of 94 blank relationships where gold says 81 — a pre-existing one-off, unrelated to this module and not introduced by it. |
| **CR7** | XAGG → Large-Scale Aggregate | ✅ correct — 33 criminal records, 30 under trial, 1 convicted-on-appeal, and FIR 891-24 cross-checked as consistent. **This is the question CS4's new family sits directly above in the chain, and it kept its own.** |
| **S3** | XAGG → Large-Scale Aggregate | ✅ unchanged — still `graph_recurrence_person`, four people in two cases each. The entity-recurrence tier CS4 used to fall into still answers the question it was built for. |
| **A7** | XAGG → Large-Scale Aggregate | ✅ unchanged — 8 of 73 FIRs record a delay reason (~11%). The reporting-delay family M7 sits beside is untouched. |
| **G1** (Modules 31–34) | XAGG → Meta-Analysis | 🟡 substantive and correct on what it answered (9 missing incident dates, 52 missing status, the four repeat accused, 30 unlicensed weapons), but **two sub-questions timed out** and the synthesis verifier rejected the paraphrase. Pre-existing; Module 50 owns it. |
| **G6** (Module 35) | XAGG → Meta-Analysis | 🟡 substantive — district spread over 9 districts — but took 373 s. Pre-existing; Module 50 owns it. |
| **CR3** (Module 36) | varies | ❌ **failed on all 3 runs, three different ways.** Not attributable to this module — see below. |

### CR3, examined rather than waved through

Three runs, three distinct failures:

1. `status=error` — *"The synthesized answer could not be verified as grounded
   in the sub-answers."*
2. `status=error` — *"Could not answer sub-question (timed out)"* for **both**
   of `record_consistency`'s sub-queries.
3. `route=None` — the XNETWORK relevance gate: *"nearest cluster found was
   distance 0.156 against a relevance cutoff of 0.145"*.

**Ruled out as a Module 44 effect, by measurement not assertion.** Every
`_SQ_*` sub-query constant in `meta_analysis.py` was resolved through the new
dispatch chain; none changed family. In particular
`_SQ_CRIMINAL_RECORD_VS_COURT` still resolves to
`criminal_record_court_crosscheck` — the new CS4 predicate requires a
no-match term and does not take it — and CR3's own two sub-queries
(`_SQ_PERSON_RECURRENCE`, `_SQ_CMS_LINKAGE`) still resolve to
`graph_recurrence_person` and `cms_fir_linkage` exactly as before. CR3's gold
text itself still resolves to `station_or_category_counts`, unchanged.

What the three runs do show is that **CR3's route is not deterministic** (XAGG
twice, `None` once) and that its Meta-Analysis sub-queries time out under
load. Module 36's result already records CR3's wiring into
`record_consistency` as deferred, and Module 29 recorded it as "partial". This
is that unfinished work surfacing, plus machine load from a long live session.
Filed in §8.

---

## 8. New defects found

Split out rather than folded in:

1. **Module 56 — M2's dispatch vocabulary is narrower than the family it now
   serves.** While M2 returned an honest refusal, a narrow trigger list was
   the safe choice. Now that the family computes a real answer, a question
   that avoids its exact vocabulary ("specialist units", "ordinary police
   stations") falls to the trailing catch-all and gets a plain per-station
   count — the very substitution the original refusal existed to prevent.
   Measured, with the failing phrasing recorded. Not widened here because
   `_STATION_KEYWORDS` sits a few rungs lower in the same chain and every
   candidate token risks pulling ordinary per-station questions in; it needs
   its own all-32 control and its own live check.

2. **Module 57 — CR3 is unstable across runs at three different layers.**
   Router (XAGG vs `None`), Meta-Analysis sub-question timeouts, and synthesis
   verification, on three consecutive runs of the same text. Module 36
   deferred its aggregate wiring and Module 50 owns the consolidation, but
   neither is scoped to the *non-determinism*, which is what makes CR3
   unverifiable rather than merely incomplete.

3. **Observed, not filed — G5's citation-consistency footnote.** G5's answer
   is factually correct and carries *"A cited claim ([Document 1]) could not be
   confirmed against its source: Claim cites figure(s)/identifier(s) not found
   in its source text: 1."* The "1" appears to be the document number itself
   being read as a claimed figure. Nothing in this module touches G5 or the
   citation checker, and one observation is not enough to file a module
   against; recorded here so the next person to see it has a second data
   point.

**Already filed elsewhere and confirmed again here: Module 47.** The M2
premise in this module's brief — that Modules 25 and 29 had found M2
completing cleanly, so its 0.0 "contradicts recorded results" — rests on a
per-question row from a run whose artefacts were never committed. Module 43
establishes that in detail. The work here did not depend on resolving it.
