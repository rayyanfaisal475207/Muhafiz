# Module 151 — the verifier rejects a correct answer that states an absence, because nothing in a document corpus can cite one

**Branch:** `fix/verifier-schema-grounded-absence` · **worktree** `D:/Rapids AI/muhafiz-m151` ·
branched from `origin/main` @ `8a80d30` (PR #100, Module 178 filed). **Questions:** KB2, KB5, KB9
(the three the defect was filed from), Module 82's forced-hallucination control plus two arms of
this module's own, and this module's four pre-written paraphrases. **Backend:** `:8151` from this
worktree on `main`'s code for one diagnostic KB9 run; every measured arm ran **in process** through
`evaluation/module92_inprocess_run.run_one()` (as Modules 110, 116, 143 and 150 did) because the
measurement needs the Verifier's *complete* input and verdict, which neither the SSE stream nor
`backend.log` carries. **Model:** local `qwen3:14b` via the ngrok model server (`/health` 200
throughout); `generation` is recorded per row in every evidence file. Groq fallbacks: **0** in every
before/after arm below; the only cloud rows are two paraphrase rows in the superseded cut-2 arm
(§6), during a ~3-minute window in which the model server returned 404.

**Stopped on the coordinator's instruction** before the paraphrase re-run and the 32-question
regression on the final cut; every arm below is labelled with the cut it ran on and its row count.

---

## 0. Summary

**The defect is real and reproduces on demand.** With the relevance evaluator held constant, the
judge rejected KB9's correct answer 2 of 3 times with the filed claim word for word — *"The system
lacks a field to track compliance with this requirement — no chunk discusses the structure of case
records or the presence/absence of specific data fields"* — and 6 of 16 times on replay of captured
inputs (§4.3). The judge is following `prompts/verifier.txt` rule 5 exactly; no chunk can state that
a field does not exist.

**The fix shipped is the third cut, and the first two were measured out.** A classifier + inventory
grounding step (Module 143's method for the classification, Module 95's for the grounding) is added
to `verify_grounding()` under Module 61's gate. Cut 1 confirmed four vague absences on a KB5
paraphrase because the classifier's keywords were invented compounds that match no column (§3.2,
§6); cut 2 was clean on gold shapes but, on the held-out set, confirmed **three event negatives**
(*"the chain of custody was not maintained in any of the 8 cases"*) and **two positive claims** —
the 14B classifier does not separate "there is no field for X" from "X did not happen" or "X is
recorded" on its own. Cut 3 adds a deterministic **sentence guard** (Module 61's "must name an
identifier" analogue: the judge's claim, wrapper stripped, *and* the answer sentence it quotes must
both be negative and both speak of a field/column/khana/what the records track or capture, and the
quote must be in the answer). On cut 3: **MUST_NOT 44/44 clean on two runs** (CR3's fabricated
negative, Module 82's four fabrications, event negatives, positives, data negatives in all three
scripts); MUST_REFUTE 8/10 refuted and 2/10 unconfirmable (still rejected); MUST_CONFIRM
**10/20** — the price of the guard is recall, and §3.2 names each lost item.

**Live, KB9 is the only question the change touches**, because it is the only one whose generator
states the schema fact. KB9 served: end-to-end **0/3 → not measurable** (the evaluator stopped every
run at the 360 s deadline, before and after, §4.1 — filed, row 188); verifier-arm **1/3 → 2/3** (cut
3; cut 1 also 2/3 with one Module 151 overturn); replay of captured inputs 10/16 → 13/16 (cut 2, one
overturn, one false refutation). **KB5** is untouched on every arm (its answers do not state the
gap in schema terms, and the one live rejection was `_check_no_citation()` on an Urdu answer — row
119's defect, not this one). **KB2 still drifts** on every arm and every cut: 6 of 6 flag-on answers
say the system *does* record statements; the verifier now *could* ground gold's sentence, but the
generator never writes it because nothing in its window states the witness record's shape (row 189).

**Module 82's control, all arms, still rejected on the shipped cut** — §5 (rows counted there). CR3's
fabricated negative is pinned rejected by unit test in its live wording and classified
`data_absence` 2/2 on the held-out set.

---

## 1. Root cause — the judge is doing exactly what it is told, and the corpus cannot say "there is no field"

### 1.1 The rejected answers were correct

The 2026-09-14 all-32 run (`evaluation/postfix-check/`, `main` @ `4bfd840`) served KB5 and KB9 as
`status=error` with the text *"The generated answer could not be verified as grounded in the
retrieved documents."* — 0.00 for both. `backend.log` lines 459 and 549 carry the Verifier's reasons:

| Q | Verifier reason (verbatim, `backend.log`) | Gold says |
|---|---|---|
| **KB5** | *"Claims about missing data fields in case records are not supported by any cited chunk, which only addresses FIR linkage and procedural requirements, n…"* (`unsupported=3`) | *"جو چیز درج نہیں ہوتی وہ یہ ہے کہ چین آف کسٹڈی واقعی برقرار رکھی گئی یا نہیں — تقاضے کے اُس حصے کے لیے **کوئی متعلقہ خانہ موجود نہیں**"* — what is not recorded is whether chain of custody was maintained; there is **no relevant field** for that part |
| **KB9** | *"The claim about case records lacking data on inquest report compliance is unsupported — chunks only provide legal rules and FIR statistics, not analys…"* (`unsupported=1`) | *"Magar **schema mein kahin koi inquest, post-mortem ya cause-of-death record nahi**. FIR aur uski dafaat yeh darj karti hain ke maut ka daawa kiya gaya hai; woh alag qanooni maut ki tehqeeqaat ko darj nahi kartin."* |

Both flagged claims *are* gold's own load-bearing sentence. KB9's window that day (`backend.log`
line 535) was five Punjab Police Rules / CrPC chunks plus the composed data half
`kb-data-half:death_investigation_charging` — "10 of 73 FIRs cite PPC §302", the heirs figure —
i.e. exactly the evidence gold's answer is built from. The generator read the statute, read the
aggregate, and drew the one conclusion neither can *state*: that the FIR schema has no inquest
record. The Verifier then asked which chunk says so. None can.

**Reproduced here, verbatim, with the evaluator held constant** (§4.2, flag off, KB9 runs 2 and 3
of 3): `unsupported_claims = ["The system lacks a field to track compliance with this requirement —
no chunk discusses the structure of case records or the presence/absence of specific data
fields."]`. The judge names the mechanism itself.

### 1.2 The mechanism in `verify_grounding()`

`prompts/verifier.txt` rule 5: *"If a chunk only partially covers a claim, the claim is
unsupported unless another chunk fills the gap."* Rule 4 allows *"The documents do not contain X"*
— absence **from the documents** — but a claim that *our records* have no field for X is not
about the documents at all; it is about the database's shape, and no retrieved chunk, narrative
or aggregate, describes the database's shape. Module 61 solved the neighbouring problem for
**data** negatives — "65/26 is absent from the CMS linkage list" is grounded when the list is a
declared-complete enumeration (`EXHAUSTIVE_SCOPE_META_KEY`) and the id is genuinely missing from
it. Nothing equivalent existed for **schema** negatives, and the data-half chunk that carries the
aggregate is a *count*, not an inventory: `_format_chunks_for_verifier()` shows the judge "10 of
73 FIRs cite PPC §302", which cannot license "and there is no post-mortem field".

`verify_grounding()`'s flow on KB9: the four deterministic pre-checks find nothing (Module 82 §8c
— all four are inert on the legal-KB path); the judge returns `grounded=false` with the claim
above; Module 61's override sees no exhaustive chunk (the data half is composed by `rag.py`, not
tagged by `meta_analysis.py`) and does nothing; the merge leaves `grounded=false`;
`semantic_search.py` line 410 refuses to serve it. It repeats on every run where the generator
states the schema fact — which is every run where the generator is *right* — except that the
judge's verdict is sampled: on the same captured input the judge accepted 10 and rejected 6 of 16
replays (§4.3). Module 57/61's coin flip, on a claim the judge can never actually verify.

### 1.3 KB2 is the same defect from the other side

MODULE37_RESULT.md §4.2 measured it: with the orphaned chunks in KB2's window the Verifier blocked
the answer 4/4; with real CrPC/PPR chunks in their place the answer says *"the system does maintain
records of statements made during police interviews"* 7/7, served. Gold's answer is a schema fact
— *"there is no field anywhere in the system for confession or interview-statement text"* — and
`muhafiz_schema.dbml.txt`'s own note on `psrms.fir_witness` says it in as many words: *"NO
statement content field of any kind, and none should ever be added."* That sentence is not in the
statute corpus, so a generator that says it is rejected and a generator that says the opposite
(from CrPC s.161/164's duty to write statements down) is served. Measured again here: **every**
KB2 answer on every arm and cut (§4) says the system *does* have provisions for recording
statements; two of three flag-off runs were rejected only for their "data gap" hedges, and the
served ones are wrong.

### 1.4 Why the fix is not a prompt rule

Telling the judge "accept schema claims" would be a loosening: the judge has no inventory and
would accept a *fabricated* schema absence ("the register has no licence-status column") as
readily as a true one. The judge must stay strict. What is missing is a **second reader with the
inventory in hand**, run only after the judge rejects — Module 61's posture (a deterministic
post-pass, not a resampled verdict), Module 143's method for the classification step (an LLM
reading of a free-text reason, measured on a held-out set), and Module 95's precedent for the
grounding step (`_missing_custody_controls()` — an absence computed as a set difference against
the register's actual columns).

---

## 2. The change

### 2.1 Design — classify, guard, then ground against the live inventory

Two new modules and one block in `verify_grounding()`, placed after Module 61's override and
before the deterministic merge, under **the same gate** as Module 61's (judge rejected, not
off-topic, no leakage, no deterministic pre-check finding — `_check_no_citation()` included):

1. **`src/pipeline/schema_inventory.py` — the record inventory.** `DECLARED_RECORD_FAMILIES`:
   27 record families and the columns each row carries **as the Muhafiz Data API returns them**
   (`tests/fixtures/muhafiz_api_snapshot.json` pins 21 of them exactly; four PKM service tables
   the snapshot carries only as nulls come from `muhafiz_schema.dbml.txt`). `live_inventory()`
   unions that with the **graph's own property keys** — two Cypher reads, `keys(n)` per label and
   per `StructuredRecord.record_type`, cached 300 s — so Module 22's timestamps, Module 95's zimni
   counts and every cross-silo property count as fields our records have. Module 95's finding
   stands: the graph is a *projection* that drops columns (`Weapon` carries none of
   `recovered_from`/`quantity`/`item_detail`), so the graph can only ever **add** to the declared
   shape, never be the sole authority for an absence. Live: `27 families, 400 fields (declared
   API shape + live graph: 13 labels, 9 record types)`. `fields_serving(keywords, scope,
   inventory)` is `_missing_custody_controls()` generalised — the same substring scan over
   normalised identifiers, over any family or over all of them, returning the fields *and family
   names* that serve a concept; `test_fields_serving_generalises_missing_custody_controls`
   asserts it agrees with Module 95's function on Module 95's own three controls. A keyword list
   with no single-word stem (cut 1's finding) is unusable.
2. **`src/pipeline/schema_claims.py` + `prompts/schema_claim.txt` — the classifier.** Each
   judge-flagged claim (the judge's own wording, the unit Module 61 chose and for the same
   reason) is classified as `schema_absence` / `data_absence` / `schema_presence` / `other`,
   shown the answer for scope and the rendered inventory for existing fields. For every claim it
   quotes the **answer sentence** the claim is about; for a schema absence it returns the `scope`
   family, 2–6 single-word `field_keywords`, and `matching_fields` — any field in the inventory
   that already serves the concept. **Fail-closed** everywhere: exception, malformed JSON, a claim
   not returned, an unknown kind → no classification → rejection stands. (Module 143's gate fails
   *open* because there the safe default is to retry; here the safe default is to reject.)
3. **`sentence_guard()` — deterministic, and the reason the classifier's verdict alone is never
   enough (cut 2's finding, §3.2).** The judge's claim with its evidence wrapper stripped
   (*"…is not supported by any chunk"*, *"— no chunk discusses…"*) and the quoted answer sentence
   must **both** contain a negation (`no/not/none/lacks/without…`, `nahi`, `نہیں/کوئی/بغیر`) **and**
   schema vocabulary (`field/column/schema/table/record type/data point`, `track/capture/store`,
   `khana`, `خانہ/فیلڈ/کالم`); the quote must be a genuine substring of the answer (whitespace and
   markup folded). *"records"* as a verb is deliberately not schema vocabulary — *"no zimni entry
   records an arrest"* is the data-negative shape. The judge's un-stripped wording cannot carry the
   test: every claim it writes ends in a negation about the *evidence*.
4. **`ground_against_inventory()` — the conjunction.** A claim is `confirmed_absent` only when it
   is a `schema_absence` **and** passes the guard **and** the classifier named no `matching_fields`
   **and** the keyword scan over its scope finds no field or family. Two independent readers must
   both find nothing. An unknown scope, a keyword list reduced to generic words (`text`, `date`,
   `record`…) or to compounds, or a quote not in the answer is *unconfirmable*, never confirmed.
5. **The verifier block.** Only if **every** flagged claim is `confirmed_absent` is the rejection
   overturned: `grounded=True`, `unsupported_claims=[]`, `schema_absence_grounded=True`, the
   grounded claims listed, and a reason that names the inventory rather than a document.
   `semantic_search.py` appends `SCHEMA_ABSENCE_GROUNDED_CAVEAT` so the overturn is never silent
   (Module 101's discipline; seen live in the cut-1 KB9 overturn, §4.2).
   `config.SCHEMA_ABSENCE_GROUNDING_ENABLED` (env, default on) is the off switch — off is the
   pre-Module-151 verifier byte for byte, and every "before" arm below is exactly that.

### 2.2 Why this is not a loosening, point by point against the brief's anti-goals

- **No threshold changed.** `_SUBSTANTIAL_ANSWER_LEN`, `_HEDGE_WINDOW`, the hedging list, the
  judge's `temperature=0.0` / token budgets — untouched. `prompts/verifier.txt` untouched.
- **No exemption keyed on wording.** The classifier is shown a 27-family inventory and a claim;
  nothing in the prompt, the inventory or the code names KB2, KB5 or KB9, and the held-out set
  (§3.2) uses different wordings from the prompt's examples. The guard's vocabulary is the
  vocabulary of *schema* (field, column, khana), which is how gold itself phrases these in all
  three scripts, not any question's wording.
- **A positive claim never passes on schema grounds.** `schema_presence` is terminal, and if the
  classifier mislabels a positive claim (it did, twice, in cut 2) the guard's negation test stops
  it: pinned by `test_module151_a_positive_claim_mislabelled_schema_is_still_rejected` and measured
  S2/S3 → not confirmed 4/4 on cut 3.
- **A fabricated data negative is never this block's business.** `data_absence` is terminal —
  CR3's *"fir-64-26 does not appear in the linkage list"* (Module 82 §4c, verbatim) is pinned
  rejected. If the classifier mislabels a data negative, the inventory refutes any claim whose
  subject has a family or field (*"no FIR mentions weapons"* → `weapon_register`;
  `test_module151_a_misclassified_data_negative_is_caught_by_the_inventory`), and the guard stops
  every event/content negative that has no field (*"was not maintained"*, *"was photographed"*,
  *"records an arrest"*; pinned by three tests, measured N9/N10/N11 → not confirmed 6/6 on cut 3).
  What survives all three is a negative, schema-worded sentence about a concept with **no** field
  or family anywhere in scope — the class of claim that is true.
- **A mixed rejection stands in full.** One schema absence plus one invented rule number → both
  claims stay in `unsupported_claims`, `grounded=false` (pinned; seen live on KB9 cut 1 run 1 and
  cut 3 run 1, §4.2).
- **Module 82's four fabrications** (rule 27.41(3), the seven-year clock, FIR 512/26, "fully
  compliant") are `other`; pinned by unit test; O1/O2/O4 → not confirmed 6/6 on the held-out set;
  re-run live in §5.
- **`_check_no_citation()` still overrules.** An answer with no `[Document N]` marker never
  reaches this block (same as Module 61) — seen live: KB5's one before-arm rejection (§4.1) is a
  no-citation rejection of an Urdu answer and is *not* rescued.

### 2.3 What was deliberately NOT changed

- `supervisor.py`, anything under `src/pipeline/harness/tools/` (m178's), `xagg.py`
  (`_WEAPON_REGISTER_FIELDS` is *mirrored* and pinned equal by test, not imported — that file is
  Module 144's and 89's), `rag.py`'s `_KB_DATA_HALF_PLANS` (KB2 has no data-half plan; the fix
  it needs is filed, row 189), `validation.py` (its structural tier still caveats a schema claim it
  cannot trace to a chunk — seen on the cut-1 overturn, §4.2; filed, row 191),
  `prompts/verifier.txt`, `_check_no_citation()`, `_check_fabricated_case_ids()`,
  `_check_hedging()`, `_check_temporal()`, `_check_leakage()`, Module 61's override, Module 101's
  exemption.
- **Not relaxed after measurement:** cut 1's compound-keyword confirmations and cut 2's event /
  positive confirmations were each answered by a *tightening* (§3.2), never by widening the
  vocabulary or lowering a requirement. Every degraded path — classifier failure, malformed
  output, unknown scope, no usable keyword, quote not found, graph unreachable — is a failure to
  confirm.

### 2.4 Files touched

| File | Change |
|---|---|
| `src/pipeline/schema_inventory.py` | **New.** `DECLARED_RECORD_FAMILIES`, `live_inventory()`, `fields_serving()`, `has_single_stem_keyword()` |
| `src/pipeline/schema_claims.py` | **New.** `classify_flagged_claims()`, `sentence_guard()`, `claim_core()`, `ground_against_inventory()` |
| `prompts/schema_claim.txt` | **New.** The classification prompt |
| `src/pipeline/verifier.py` | The Module 151 block in `verify_grounding()` (+ its comment block); `SCHEMA_ABSENCE_GROUNDED_KEY` / `_CAVEAT`; four imports. Nothing else in the file |
| `src/pipeline/harness/agents/semantic_search.py` | Appends the caveat when the key is set (10 lines, Module 101's pattern) |
| `src/config.py` | `SCHEMA_ABSENCE_GROUNDING_ENABLED` |
| `tests/test_verifier.py` | +17 tests (`-k module151`) |
| `tests/test_schema_inventory.py` | **New**, 32 tests |
| `scripts/module151_classifier_offline.py`, `scripts/module151_verifier_replay.py`, `scripts/module151_forced_hallucination_control.py`, `scripts/module151_live_runs.py`, `evaluation/module151_inprocess_run.py`, `evaluation/module151_verifier_arm.py`, `evaluation/module151_regression_compare.py` | **New**, measurement only |
| `docs/gold-qa-wave2-results/module151_*`, `module151_live/` | Evidence files; `module151_paraphrases.json` written before any run; `module151_live/cut2/` holds the superseded cuts' rows |
| `GOLD_QA_REMAINING_FIXES_PLAN.md` | Row 151; rows 188–191 |

---

## 3. Unit tests — fail before, pass after, both directions

### 3.1 `tests/test_verifier.py -k module151` (17) and `tests/test_schema_inventory.py` (32)

All 49 pass; the full `tests/test_verifier.py` (Modules 17/25/40/61/70/101 pinned there),
`test_module82_generation_findings.py`, `test_verify_milestone_d_cleanup.py`,
`test_module95_finding_coverage.py`, `test_harness_agent_semantic_search.py` and
`test_validation.py` pass unchanged. "Before" is the block disabled (the old verifier byte for
byte, §2.1): re-running the 17 with the block off, **exactly the two "grounded" tests fail** with
`assert False is True` and the other 15 pass in both states — which is the point of them.

| Direction | Test | Pins |
|---|---|---|
| **confirmed absence → grounded** | `a_confirmed_schema_absence_is_grounded` | KB9's live judge claim, `scope=any`, no inquest/post-mortem field or family → served, key set, reason names the inventory. **Fails before.** |
| | `a_scoped_schema_absence_is_grounded` | KB5's shape scoped to `women_violence_report` → served, even though `custody_classification` / `custody_position` exist *elsewhere*. **Fails before.** |
| **refuted absence → rejected** | `a_refuted_schema_absence_is_still_rejected` | "accused records hold no field for age" — `fir_accused.age` exists |
| | `the_same_custody_claim_unscoped_is_refuted` | the KB5 concept with `scope=any` is FALSE and stays rejected — scope is load-bearing |
| | `the_classifier_naming_an_existing_field_refutes` | second reader: keywords miss, `matching_fields=[fir_accused.age]` refutes |
| **fabricated data negative → rejected** | `a_fabricated_data_negative_is_still_rejected` | CR3 run 2's reason, verbatim (Module 82 §4c), `data_absence` |
| | `a_misclassified_data_negative_is_caught_by_the_inventory` | "no FIR mentions weapons" mislabelled schema → `weapon_register` refutes |
| | `an_event_negative_mislabelled_schema_is_still_rejected` | "the chain of custody was not maintained in any of the 8 cases" mislabelled schema, no field exists → the guard stops it |
| **positive schema claim → not grounded** | `a_positive_schema_claim_is_not_grounded_by_the_inventory` | KB2's drift sentence, `schema_presence` |
| | `a_positive_claim_mislabelled_schema_is_still_rejected` | "our weapon register records packaging" mislabelled schema → no negation → stopped |
| **never overturned** | `a_mixed_rejection_is_not_overturned` · `module82_forced_hallucination_shape_is_still_rejected` · `a_classifier_failure_leaves_the_rejection_standing` · `the_off_switch_restores_the_old_verifier` · `a_deterministic_pre_check_still_overrules` · `an_off_topic_answer_is_never_overturned` · `a_grounded_verdict_never_calls_the_classifier` | |

`test_schema_inventory.py` pins the inventory to its sources (every observed family equals the
API snapshot's row keys; `weapon_register` equals `xagg._WEAPON_REGISTER_FIELDS`;
`fields_serving()` agrees with `_missing_custody_controls()` on all three custody controls; not
one witness row the API ever returned has a statement/confession/interview/testimony field — KB2's
gold checked against data), the matching in both directions, every degraded path (unknown scope,
generic-only or compound-only keywords, Urdu keywords) resolving to *unconfirmable*, and the
sentence guard in all three scripts and against each of cut 2's live failure shapes.

### 3.2 The classifier + guard, measured on a held-out set (real model, live inventory) — three cuts

`scripts/module151_classifier_offline.py`: 38 judge-style flagged claims with an answer context,
held out from the prompt's examples, each run twice through the exact
`classify_flagged_claims()` → `ground_against_inventory()` pair the verifier uses.
`module151_classifier_offline.json` (cut 3); `module151_live/cut2/classifier_offline_cut2.*`
(cut 2, first run only, stopped when its failures were read).

| Bucket | Cut 2 (run 1) | **Cut 3 (2 runs)** | What changed |
|---|---|---|---|
| **MUST_NOT** (12 data/event negatives, 3 positives, 7 other; CR3 and Module 82 among them) | **5 of 22 wrongly confirmed** — N9 *"chain of custody was not maintained in any of the 8 cases"*, N10 *"none of the weapons was photographed"*, N11 *"no zimni entry records an arrest"*, S2/S3 *"the register records packaging"* / *"records confirm chain of custody was maintained"* | **44/44 clean** | prompt: an event is (B), a positive is (C); guard: negation + schema vocabulary on claim core and quoted sentence. On cut 3 the classifier itself now says `data_absence` for N9/N10/N11 and `schema_presence` for S3; S2 is still mislabelled and the guard stops it (`claim_is_not_negative`) |
| **MUST_REFUTE** (5 schema-shaped claims about fields that exist) | 5/5 refuted | 8/10 refuted, 2/10 unconfirmable (R3 *"nowhere in the system is a weapon's licence status recorded"* — no schema word in the judge's claim) | either way rejected |
| **MUST_CONFIRM** (10 true absences: KB2/KB5/KB9/G5 shapes, three scripts) | 9/10 | **10/20** | recall is what the guard costs |

The ten MUST_CONFIRM misses on cut 3, each twice, and why:

| Item | Verdict | Why |
|---|---|---|
| P1 — the 2026-09-14 KB9 reason verbatim (*"…lacking data on inquest report compliance"*) | `claim_has_no_schema_vocabulary` | the judge said "lacking data", not "no field". Today's judge wording on the same question (*"lacks a field to track"*) passes; the 09-14 wording would not |
| P2 / P3 — KB5's 09-14 reason and its Urdu form, over an Urdu KB5 answer | `refuted:field_exists` | the classifier scoped both to `any` (in cut 2 it scoped P2 to `women_violence_report` and confirmed it); at `any`, `custody_classification` (challan) and `custody_position` (accused) refute "custody". A false refutation, in the safe direction |
| P7 — Roman-Urdu *"Koi field nahi jahan gawah ka bayan darj ho"* | `quote_not_in_answer` | the classifier shortened the answer's sentence (dropped the parenthetical "— name, CNIC, address, relationship to the complainant —"); the substring test is strict by design |
| P8 — *"the malkhana register does not record who took custody at handover"* | `claim_has_no_schema_vocabulary` | "does not record" is the data-negative shape; the guard cannot tell it from N11 |

P4 (Roman-Urdu KB9), P5/P9 (KB2 gold shape, English), P6 (G5 gold shape), P10 (KB5 gold shape,
scoped) confirmed 2/2. Classifier cost: 7–13 s per call locally (one call per rejection, never on
a grounded verdict). Two `O7` rows are `classifier_failed` (thinking budget exhausted twice) —
fail-closed, as designed.

**Decision.** The bucket that decides shipping is MUST_NOT, and on the shipped cut it is clean on
every item on both runs, with three independent stops behind it (classifier kind → guard →
inventory). The recall loss is reported, not hidden: half the hand-written true absences are left
rejected, exactly as they are today.

---

## 4. Live verification — KB2, KB5, KB9, before / after

Three arms, all in process, all `generation=local`, 0 Groq fallbacks (`local_llm_fallbacks=0` on
every row). "Judge" is the raw verdict before any post-pass (captured by wrapping
`call_llm_json`, deep-copied — the first capture recorded the mutated dict and was discarded);
"served" is `status != error`.

### 4.1 End-to-end, `main`'s code (`module151_live/module151_before.json`, 9 rows)

| Q | run 1 | run 2 | run 3 | served |
|---|---|---|---|---|
| KB9 | 360 s stop, evaluator F×5 | 360 s stop | 360 s stop, F F F | **0/3** — verifier never reached |
| KB5 | 360 s stop | judge accepted → **`_check_no_citation()` rejected** (Urdu answer cites "دستاویز 1", "[1.8(ii)]"; row 119's defect) | 360 s stop, F F T then deadline | **0/3** |
| KB2 | served, hedged ("documents do not explicitly address…") | served, **wrong** ("the system does keep records… not a data gap") | 360 s stop, F F F F | **2/3 served, 0/3 right** |

A tenth run, KB9 through the `:8151` backend on `main`'s code, stopped at 360 s after four
`relevant=False` verdicts (`module151_live/module151_diag_kb9_backend8151.json`). On 2026-09-14 the same questions passed
the evaluator at attempt 1 (KB9) and 2 (KB5); today each evaluator cycle took 90–120 s and the
Semantic Search deadline (Module 67) fired before the generator ran on **7 of 9** rows. This is
upstream of the Verifier, is not what this module changes, and makes the end-to-end after arm
uninformative — so the after arm was measured with the evaluator held constant (§4.2), filed as
**row 188**.

### 4.2 Verifier arm — evaluator bypassed, everything downstream real (`module151_varm_before.json`, `module151_varm_after.json`, 9 + 9 rows)

`evaluation/module151_verifier_arm.py` (Module 82 §4c's discipline one stage earlier):
`rag.evaluate_relevance` returns `relevant=True` on the first pass, so every run keeps its
first-pass window — the same window the evaluator accepted on 2026-09-14 — and reaches the real
generator and the real `verify_grounding()`. Flag off = the old verifier; flag on = **cut 3, the
shipped code**.

| Q | flag **off** (before) | flag **on** (cut 3, shipped) | cut 1 (superseded, `cut2/module151_varm_after_cut1.json`) |
|---|---|---|---|
| **KB9** | judge accepted 1, **rejected 2** — both with *"The system lacks a field to track compliance with this requirement — no chunk discusses the structure of case records or the presence/absence of specific data fields."* → **1/3 served** | judge accepted 2, rejected 1 (*"Case records do not have a dedicated field to track inquest reports — not stated in any chunk"* + *"This represents a gap between legal requirements and current record-keeping practices"*); Module 151: first claim `unconfirmable:quote_not_in_answer` (the classifier quoted the judge's wording, not the answer's), second `other` → stands → **2/3 served** | judge rejected 2: run 1 mixed (schema absence confirmed + *"No quantification of cases involving deaths in custody"* = `data_absence`) → stands; **run 2 overturned by Module 151** (`inquest reports completion`, scope any) and served with the caveat → 2/3 served |
| **KB5** | judge accepted 3 → 3/3 | judge accepted 3 → 3/3 | 3/3 |
| **KB2** | judge accepted 1 (wrong: *"the system does keep records"*), rejected 2 for *"'data gap' if interviews are not systematically recorded through magistrates — not stated"* → 1/3 served, 0 right | judge accepted 3 → 3/3 served, **0 right** (*"The system does have provisions for recording statements… not required to maintain formal records of these interviews"* ×3) | 3/3 served, 0 right |

KB9's served answers carry s.174 on 2 of 3 flag-on runs and state the schema absence on 3 of 3
(*"No explicit data is recorded on whether investigations into the cause of death… were
conducted"*, *"The system lacks a field to track compliance"*). KB5's carry Rule 3(2) on 1 of 3
and never name chain of custody as the missing field — they say *"the records do not explicitly
state whether the additional legal requirement… was fulfilled"*, which the judge accepts under
rule 4 and which Module 151 never sees. **KB2 does not say "no, by design" on any run; it still
drifts.** The verifier is no longer the reason: on every KB2 run the generator asserts the records
*are* kept, from CrPC s.161/164 chunks, and no chunk in its window states the witness record's
shape (row 189).

### 4.3 Replay of captured verifier inputs, flag off vs on (cut 2 code; `module151_live/cut2/verifier_replay_cut2.json`, 112 rows — superseded by cut 3, reported as measured)

Every (answer, chunks) the verifier was given in §4.1/§4.2/§6 arms, replayed twice per flag
through the real `verify_grounding()`. Cut 2 has the compound-keyword rule but not the sentence
guard; the guard only removes confirmations, so the cut-3 numbers are bounded above by these.

| Q | inputs | flag off: judge accepted / final served | flag on: judge accepted / Module 151 overturned / final served |
|---|---|---|---|
| KB9 (incl. KB9-P1) | 8 | 10/16 · 10/16 | 12/16 · **1** · 13/16 |
| KB5 (incl. KB5-P1) | 9 | 18/18 · 16/18 (2 no-citation) | 18/18 · 0 · 16/18 |
| KB2 (incl. KB2-P1/RU) | 11 | 17/22 · 17/22 | 15/22 · 0 · 15/22 |

The judge's verdict on the *same* input is sampled (temperature 0, but the server is not
deterministic): KB9 live-run 3's input was rejected on replay 1 and accepted on replay 2 with the
flag on. Of the four flag-on KB9 rejections, Module 151 overturned one, one was a **false
refutation** — keyword `reporting` hit `fir.reporting_delay_reason` (the safe direction) — and two
were mixed with a data-absence claim. KB2's rejections are all `other` ("data gap" hedges), never
schema claims. The replay was not re-run on cut 3 (stopped on instruction); the one overturn above
would pass cut 3's guard (*"The system lacks a field to track compliance…"* — negative, "field",
quote in answer).

---

## 5. Module 82's forced-hallucination control, re-run — four arms, shipped cut

`scripts/module151_forced_hallucination_control.py`, in process, Module 151 **on**, everything
below the generation boundary real. Arms A and B import Module 82's and Module 101's fabrications
verbatim; C and D are this module's own and are the ones that can fail: **C** a fabricated *schema*
negative over KB4's live window (*"the malkhana register has no field for the date an item was
entered and no field for the item's condition"* — both columns exist), **D** a fabricated *data*
negative over KB6's live window (*"no FIR carries any weapon entry at all — the weapon register
holds no recovered weapon"* — 32 are on record; CR3's shape, forced rather than waited for).

`module151_control.json`, 2 runs per arm, 8 rows. **Rejected 8 of 8; fabricated markers in the
served text 0 of 8; `schema_absence_grounded` set on 0 of 8.** 7 rows fully local; C run 2 had one
call fall back to Groq (`gen=cloud`), verdict unchanged.

| Arm | run 1 | run 2 | Module 151's own reading |
|---|---|---|---|
| **A** Module 82 verbatim (KB4) | rejected — *"cites non-existent rule 27.41(3) and fabricates seven-year disposal timelines"* | rejected — *"seven-year destruction rule and audit confirmation lack support"* | both flagged claims `other` / `data_absence` → *"rejection stands"* |
| **B** Module 101's uncited variant (KB4) | rejected — `_check_no_citation()` | rejected — `_check_no_citation()` | block never runs (pre-check finding), as designed |
| **C** fabricated **schema** negative (KB4) | rejected — *"claims about the malkhana register's identity and structure are not directly stated"* | rejected | classifier: `schema_absence`, scope `malkhana_register`, and it **named `malkhana_register.date_entered` and `.condition`**; keyword scan hit both → `refuted:classifier_named_existing_field` (both readers refuted independently) |
| **D** fabricated **data** negative (KB6) | rejected — *"Document 5 provides no FIR/weapon register…"* | rejected | classifier output failed to parse on both runs (its partial JSON labelled the claim `schema_absence`) → **fail-closed**, rejection stands. Had it parsed, the guard stops it (`claim_has_no_schema_vocabulary` — "no FIR carries any weapon entry" names no field) and the inventory refutes it (`weapon` → `weapon_register`): three independent stops, checked offline on the exact wording |

Arm D is the honest one: the 14B classifier leaned the wrong way on a data negative, and it was the
deterministic layers, not the classifier, that would have held. That is why the guard exists.

CR3's own fabricated negative — *"The claim that fir-64-26 does not appear in the walk-in-complaint
linkage list is unsupported, as Document 3 does not mention fir-64-26"* — is pinned rejected in its
live wording by `test_module151_a_fabricated_data_negative_is_still_rejected` and classified
`data_absence` (never touched) 2/2 as N1 on the held-out set; Module 61's exhaustive-listing rule,
which is what governs it live, is untouched and its own tests pass.

---

## 6. Non-gold paraphrases — written before running

`module151_paraphrases.json` (written 2026-09-15 before any live run): KB2-P1 (English rewording),
KB2-RU (Roman-Urdu), KB5-P1 (Urdu-script rewording), KB9-P1 (Roman-Urdu rewording). Run through
the verifier arm, flag on, 2 runs each, on **cut 2** (`module151_live/cut2/varm_para_after_cut2.json`,
8 rows; the cut-3 re-run was stopped on instruction). Cut 1's 8 rows are in the same folder.

| Paraphrase | run 1 | run 2 |
|---|---|---|
| KB9-P1 | judge accepted; served; s.174 + s.176 | judge rejected 3 claims; **Module 151 overturned** (*"do not explicitly track whether investigations under s.174/176 were conducted"*, *"lack a dedicated field to document compliance"*, and — the weak one — *"The data focuses on charges…, not investigative processes"*); served with the caveat |
| KB5-P1 | judge accepted; served | judge rejected 2 claims; **Module 151 overturned** (*"the case records do not mention surveillance records"*, *"no data on whether special investigation teams were involved"*) — served |
| KB2-P1 | judge accepted; served; says witness statements *are* recorded in FIR narratives (drift) | same |
| KB2-RU | judge accepted; served; law half right (Art. 38, s.162), never states the schema fact | same |

**Cut 2's two overturns are exactly what cut 3's guard removes.** KB5-P1 run 2's claims say "do not
mention" / "no data on" — content negatives with no schema word — and would now be
`claim_has_no_schema_vocabulary`; KB9-P1 run 2's third claim ("the data focuses on charges") has no
negation about a field and would fail the guard, so that rejection would stand as mixed. The cut-1
run of the same paraphrase confirmed four claims whose keywords were `investigative_team`,
`expedited_time`, `magistrate_referral` — the finding that produced the single-stem rule.
Cut 1's model-server 404 window put two of its eight rows on Groq; every cut-2 row is local.

---

## 7. Regression — all 32 live

**Not run on the shipped cut** — stopped on the coordinator's instruction before the 32-question
pass (`evaluation/module151_regression_compare.py` is in place for it against the Module 116 route
baseline and the 2026-09-14 `main` run). What is known about the change's reach without it:

- The block runs **only** when the judge has rejected and no pre-check fired, and it can only turn
  `grounded=False` into `grounded=True`; on a grounded verdict the classifier is never called
  (pinned by `test_module151_a_grounded_verdict_never_calls_the_classifier`), so every question
  the verifier passes today — KB1, KB6, KB8 among them — is byte-for-byte unaffected in code path
  and in cost.
- Routes are untouched: nothing in `router.py`, `supervisor.py`, `rag.py` or `xagg.py` changed.
- `verify_structured_aggregate_paraphrase()` (the XAGG / cross-case paraphrase gate) is untouched,
  so the 21 aggregate questions cannot see the change.
- In the 18 flag-on rows of §4.2 that the judge accepted (KB2 ×3, KB5 ×3, KB9 ×2, paraphrases ×6
  on cut 2 / ×0 on cut 3), the served text equals what the old verifier would have served —
  the block never ran.

---

## 8. New defects found — filed as tracker rows, not fixed

| Row | Defect | Evidence |
|---|---|---|
| **188** | **The relevance evaluator on the legal-KB path stopped 7 of 9 KB2/KB5/KB9 runs at the 360 s Semantic Search deadline** — 4–6 consecutive `relevant=False` verdicts at 90–120 s per cycle, on the same windows it accepted at attempt 1–2 on 2026-09-14 — and Module 143's gate is scoped off this path by design. A verifier fix is invisible on a day like this; a KB question's pass rate is the evaluator's coin flip first. | §4.1, `module151_before.json`, `module151_live/module151_diag_kb9_backend8151.json` |
| **189** | **KB2 needs an inventory-bearing data half.** The verifier can now ground *"no field for interview-statement text"* (P5/P9 confirmed 4/4, the DBML says it in as many words), but the generator never writes it: every KB2 window is CrPC s.161/164 + PPR and the answer asserts the opposite 6/6 flag-on runs. Rows 96/141's retrieval half — a `_KB_DATA_HALF_PLANS` entry or a scoped `schema_inventory.render()` chunk for `fir_witness` — is the other half of this defect. | §4.2, §6 |
| **190** | **The judge rejects rule-4 statements about a data-half chunk** — *"The summary does not quantify how many cases involved deaths in custody"* / *"No quantification of cases involving deaths in custody"* — as unsupported, and they are `data_absence` to this module, so a KB9 rejection that mixes one with a true schema absence stands. A deterministic rule-4 check (the cited chunk's text genuinely lacks the concept) is Module 61's shape; not added here because it is a second relaxation. | §4.2 cut 1 run 1, §4.3 |
| **191** | **`validation.py`'s structural tier caveats the answer the Verifier just grounded via the inventory** — *"A cited claim ([Document 1]) could not be confirmed against its source: Claim cites figure(s)/identifier(s) not found in its source text: 25.35"* on the cut-1 KB9 overturn — because it has no inventory either; Module 61 shared its rule with both gates, this module did not. | §4.2 cut 1 run 2's served text |

Reported, not filed as new: the judge's verdict on an identical input is sampled (§4.3; Module
57/61's finding, with a schema claim as the subject); the classifier's scope choice for an Urdu KB5
answer flips between `women_violence_report` and `any` across cuts (§3.2 P2), which is where KB5's
recall goes.

---

## 9. Artefacts

- `docs/gold-qa-wave2-results/module151_live/module151_before.json` — §4.1, 9 rows, `main`'s code.
- `module151_live/module151_varm_before.json`, `module151_varm_after.json` — §4.2, 9 + 9 rows, flag off / cut 3.
- `module151_live/cut2/` — cut 1 and cut 2 arms (`module151_varm_after_cut1.json`, `module151_varm_para_after_cut1.json`, `varm_after_cut2.json`, `varm_para_after_cut2.json`), the cut-2 replay (112 rows) and the cut-2 offline run.
- `module151_classifier_offline.json` — §3.2, cut 3, 76 rows.
- `module151_control.json` — §5.
- `module151_paraphrases.json` — §6, written before any run.
- `scripts/module151_classifier_offline.py`, `module151_verifier_replay.py`, `module151_forced_hallucination_control.py`, `module151_live_runs.py`; `evaluation/module151_inprocess_run.py`, `module151_verifier_arm.py`, `module151_regression_compare.py`.
