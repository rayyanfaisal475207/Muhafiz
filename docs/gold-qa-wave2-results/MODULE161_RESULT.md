# Module 161 — applicant/accused overlap: the join across the citizen-services silo and the accused roster

**Branch** `feat/applicant-accused-overlap` · **worktree** `D:/Rapids AI/muhafiz-m161` · branched from `origin/main` @ `b782446` (Modules 145, 79 and 150 included).

**Headline, stated before the detail:**

1. **The data holds the join, and the answer exists — exactly once.** The
   citizen-services silo is real: 18 `StructuredRecord` nodes (14
   `pkm_application`, 4 `cms_complaint`) covering **14 distinct citizens**,
   every one carrying a CNIC. The accused roster is 94 `INVOLVED_IN
   {role:'accused'}` edges over **92 distinct persons**, every one carrying
   a CNIC. **One** citizen's CNIC is an accused's: سرفراز احمد
   (`00000-1000055-1`), who applied for a driving licence at the Khidmat
   Markaz on 2021-04-10 (`pkm-app-c14-01`, completed) and is an accused in
   `fir-620-26`.
2. **The key is CNIC and only CNIC, because a name join is 7% precise.**
   Accused names in this corpus are single given names (seven distinct
   accused are "ذیشان"; four are "کنول"). Joining the same 14 citizens on
   `canonical_name` (or `name_skeleton` — identical result) produces the 1
   true match plus **14 false ones on 9 of the 14 citizens**. The aggregate
   therefore matches on CNIC, and *reports* the name-coincidence count so
   the reader sees what a name-keyed answer would have invented (§1.3).
3. **The aggregate is built, gated and wired, and the 32 gold questions do
   not move.** `_applicant_accused_overlap()` reproduces the independently
   measured figures to the digit against the live graph; it goes through
   `run_aggregate()`'s supervisor-or-above gate before a single Cypher read
   (an investigator gets `PermissionError`, the harness wrapper returns
   `DENIED`); all-32 aggregate dispatch and rendered text are identical
   before/after (32/32); the gold equality control with the semantic layer
   force-armed moves 0/32; CR2 still dispatches to `graph_recurrence_person`.
4. **Reaching it through the description alone is where the honest miss
   is.** There is no phrase list for this kind, by design
   (`_SEMANTIC_ONLY_AGGREGATE_KINDS`). Against the first description —
   written from what the aggregate computes — the cross-encoder ranks the
   new kind **first** for Q2, L2 and P1 (they all moved off wrong
   neighbours), at **0.034 / 0.018 / 0.224**: below the 0.40 threshold. P2
   scores **0.996** but never reaches the layer, because "accused" lands it
   on the bare-noun `graph_recurrence_person` tier first (filed, §8). The
   24 hazards score ≤ **0.008** against the new description (zero hazard
   cost). §6 records a pre-declared candidate set and selection rule for the
   description, and §4/§6 record what shipped and what it reaches live.

---

## 1. Root cause — what the data holds (Phase 1, measured before any design)

All queries ran against `evidence_graph` through `docker exec … psql -f`
(`LOAD 'age'; SET search_path = ag_catalog, public;`), written to files and
parsed; the SQL is reproduced in the aggregate's docstring and in
`tests/test_xagg.py`'s `_m161_live_shape()` fixture comment.

### 1.1 The citizen-services silo

`src/graph/cross_silo_projection.py` writes one `StructuredRecord` per PKM
application (`record_type='pkm_application'`, with `service_type`,
`submitted_at`, `status`, `applicant_cnic`) and per CMS walk-in complaint
(`record_type='cms_complaint'`, with `complainant_cnic`). The CNIC is a
plain property **whether or not the record links to a case**. Only records
that resolve to a case get a `Person` minted and an `INVOLVED_IN
{role:'applicant_pkm'|'complainant_cms'}` edge — measured 4/4 CMS and 4/14
PKM (the women-violence reports), so **10 of 18 records have no Person of
their own** and are joinable only through the CNIC property.

| record_type | records | with CNIC | resolved to a Person via INVOLVED_IN |
|---|---|---|---|
| `pkm_application` | 14 | 14 | 4 (`applicant_pkm`, all `women_violence_report`) |
| `cms_complaint` | 4 | 4 | 4 (`complainant_cms`) |
| **total** | **18** | **18** | **8** |

Distinct CNICs across the 18: **14** (four citizens filed two women-violence
reports each). `docs/decisions/0001-muhafiz-api-migration.md` records the
design intent — "Cross-silo CNIC overlap: PKM 10/10, CMS 4/4 … the
cross-case backbone, designed-in" — and it holds: **all 14 CNICs match a
`Person` in the graph**, but 13 of them are the *complainant or victim* of
the FIR their own record was escalated to. The overlap the decision record
counted is "citizen appears in an FIR", not "citizen is accused".

### 1.2 The accused roster

`Person-[:INVOLVED_IN {role:'accused'}]->Incident-[:BELONGS_TO_CASE]->Case`
— Person→Incident, per Module 89's correction; `Incident→Case` carries both
`BELONGS_TO_CASE` and `PART_OF` (73 each). 94 accused edges, **92 distinct
persons, 92 with a CNIC**. No `Person` node in the graph holds both a
service-applicant role and the accused role (0 rows) — the one overlap is
between an *unlinked* PKM record's CNIC property and an FIR's accused.

### 1.3 The join keys, and their precision

| key | citizens with the key | accused with the key | matches | of which true | precision |
|---|---|---|---|---|---|
| **CNIC** (`Person.cnic` = `applicant_cnic`/`complainant_cnic`) | 14/14 | 92/92 | **1** | 1 | **1/1** |
| `canonical_name` | 14/14 | 92/92 | 15 | 1 | 1/15 = 6.7% |
| `name_skeleton` | 14/14 (416/430 Persons carry one) | 92/92 | 15 | 1 | 1/15 = 6.7% |

The 14 name-only false positives, by citizen (distinct accused sharing the
name on a different CNIC): کنول 4, شازیہ 2, نازیہ 2, حمزہ 1, رابعہ 1, صبا 1,
عارفہ 1, مریم 1, نمرہ 1. A name join would tell a supervisor that nine
women who filed violence reports or vehicle verifications are under
criminal investigation. They are not. The aggregate never matches on name;
it counts the coincidences and prints the count as a caveat.

### 1.4 The one match, with its evidence

```
pkm_application:pkm-app-c14-01  service_type=driving_license  submitted 2021-04-10  status=completed
  applicant_cnic 00000-1000055-1  →  Person PERSON-e71d55c47b "سرفراز احمد" (father غلام احمد, مرد)
  INVOLVED_IN{role:accused} → Incident → Case fir-620-26   (source psrms/fir/fir-620-26#structured)
```

So the honest answer to Q2 is **yes, one person**, with that record — not
"no such person", and not the 15 a name join would return.

### 1.5 Why nothing computed it before

`graph_recurrence_person` (CR2's aggregate) counts one Person's
`BELONGS_TO_CASE` recurrence across cases. The unlinked PKM record has no
`BELONGS_TO_CASE` edge and no Person; the citizen's only presence is a CNIC
string on a `StructuredRecord`. No aggregate read the silo against the
roster, so Module 145's 0.002 was correct: nothing to reach.

---

## 2. The change, and what did not change

### `src/pipeline/xagg.py`

- **`_applicant_accused_overlap(jurisdiction_case_ids=None)`** — three
  reads: the silo (`record_type IN ['pkm_application','cms_complaint']`, with
  the CNIC properties), the accused roster (canonicalised through the
  confirmed `SAME_AS` map exactly as `_total_accused_count()` does), and
  the citizens' own names (for the coincidence count only). Joins on CNIC in
  Python; returns every match with its evidence (`cnic`, `entity_id`,
  `name`, `case_ids`, every service record used with `service_type`,
  `submitted_at`, `status`, `record_id`, `match_key="cnic"`), the four
  denominators, records/accused without a CNIC (counted, never dropped),
  and `name_only_collisions` / `citizens_with_name_collision`.
  `jurisdiction_case_ids` scopes the **accused** side only — that is where
  the "under investigation" fact lives; a service record has no jurisdiction
  of its own unless linked, and 10 of 18 are not.
- **`logger.info("XAGG applicant_accused_overlap: …")`** with every figure —
  the only live evidence of which aggregate fired. Live line, verbatim:
  `XAGG applicant_accused_overlap: 18 citizen-service record(s) over 14 distinct citizen(s) (0 record(s) without CNIC) vs 94 accused entr(ies) over 92 distinct accused (0 without CNIC); CNIC join matched 1 [سرفراز احمد:00000-1000055-1:fir-620-26]; a name join would have added 14 false match(es) on 9 citizen(s)`
- **`render_applicant_accused_overlap()`** — "Yes — exactly one person …" /
  "No. None of the N distinct citizens …" / "No citizen-service records …",
  then the basis line ("Matched on CNIC, not on name."), the no-key counts
  when non-zero, and the name-coincidence caveat when non-zero.
- **`_SEMANTIC_ONLY_AGGREGATE_KINDS = {"applicant_accused_overlap"}`** —
  declared next to `_GENERIC_AGGREGATE_KINDS`; the kinds the phrase chain
  never returns. Consumed by the description-table equality test.
- **`run_aggregate()`** dispatch branch, placed after
  `officer_role_pair_overlap` by convention (order is irrelevant for a kind
  with no phrase list).

### `src/pipeline/semantic_dispatch.py` — **the table entry only**

One entry added to `CAPABILITY_DESCRIPTIONS`, after `cms_fir_linkage`. No
other line of this file was touched — `muhafiz-m158` owns the scorer
section below the table and its in-progress diff does not touch the table.

### `src/pipeline/harness/tools/xagg.py`

`render_applicant_accused_overlap` imported; `"applicant_accused_overlap"`
added to the hand-maintained `AggregateKind` Literal (the thirteenth
family); a rendering branch in `_render_aggregate_text()`.

### `src/pipeline/orchestrator.py`

Import plus the two remaining XAGG-route rendering sites (Module 91 left
three in total: these two and the harness tool's).

### Tests, evaluation, results

- `tests/test_xagg.py` — Module 161 section (§3).
- `tests/test_harness_tool_xagg.py` — the `AggregateKind` guard and the
  investigator-DENIED test through the real gate.
- `tests/test_semantic_dispatch.py` — two edits: the chain-equality test
  excludes `_SEMANTIC_ONLY_AGGREGATE_KINDS` by name, and `_PROBE_PATH` now
  reads the newest measurement of the shipped table
  (`module161_probe_after.json`), because the test that pins "the
  committed probe matches the live table" must follow the table.
- `evaluation/module161_semantic_probe.py` (Module 145's probe over the
  enlarged table, output redirected), `evaluation/module161_inprocess_run.py`
  (Module 145's live runner, Module 161 targets),
  `evaluation/module161_description_candidates.py` (§6).
- `docs/gold-qa-wave2-results/module161_targets.json` (pre-written),
  `module161_probe_{before,after}.json`, `module161_description_candidates.json`,
  `module161_live/`.
- `GOLD_QA_REMAINING_FIXES_PLAN.md` — row 161 updated; new rows (§8).

### What did NOT change

- **No phrase list.** `phrase_aggregate_kind()` is byte-identical; Q2
  unprepared still lands on `station_or_category_counts` as on
  `origin/main` (pinned by `test_no_phrase_list_exists_for_it`).
- **No special-casing of Q2's wording anywhere.** The description names
  the two sides of the join in the domain's own vocabulary (§6.1).
- `supervisor.py`, `semantic_dispatch.py` below the table, the threshold
  (0.40), `NON_DISPATCHABLE_KINDS`, `graph_recurrence_person`'s description,
  the ingestion/projection code, the graph.
- The description-table cache (`module145_rerank_cache.json`) grew
  additively (keyed by table hash); `module145_probe.json` is untouched.

---

## 3. Unit tests — fail before, pass after, both directions

Run: `PYTHONPATH=. python -X utf8 -m pytest tests/test_xagg.py tests/test_harness_tool_xagg.py tests/test_semantic_dispatch.py tests/test_harness_supervisor.py tests/test_router.py -p no:cacheprovider` — **904 passed, 0 failed** on this branch.

**Before** (the same three test files copied into a detached pristine
`origin/main` checkout at `b782446`, `PYTHONPATH=.` there): **15 fail, 4
pass**. The four that pass on both trees are the guards that must not
change: CR2 stays on `graph_recurrence_person`, an investigator is refused
by `run_aggregate()`, the wrapper maps that to `DENIED`, and a seeded match
resolves to its kind string.

| test | before | after |
|---|---|---|
| `test_module161_cnic_join_finds_the_one_match_and_counts_name_collisions_without_matching_them` — 5 records / 4 citizens / 6 accused, 1 CNIC match, 4 name collisions on 2 citizens; rendered text names the match and its service, and never lists کنول | fail | pass |
| `test_module161_no_match_is_answered_no_with_the_counts_that_prove_it` | fail | pass |
| `test_module161_empty_silo_says_there_is_no_one_to_compare` | fail | pass |
| `test_module161_records_and_accused_without_a_cnic_are_counted_never_silently_dropped` | fail | pass |
| `test_module161_same_as_canonicalisation_collapses_a_duplicate_accused` | fail | pass |
| `test_module161_jurisdiction_scopes_the_accused_side_only` — `$case_ids` on the roster read, not the silo read | fail | pass |
| `test_module161_emits_its_xagg_log_line_with_the_figures` | fail | pass |
| `TestModule161Dispatch::test_no_phrase_list_exists_for_it` | fail | pass |
| `TestModule161Dispatch::test_a_prepared_semantic_match_dispatches_to_it_and_counts_as_specific` (`resolves_to_specific_aggregate` → True, so Meta-Analysis does not decompose it) | pass | pass |
| `TestModule161Dispatch::test_run_aggregate_dispatches_a_prepared_q2_to_the_join` | fail | pass |
| **`TestModule161Dispatch::test_an_investigator_is_refused_before_the_join_reads_anything`** — `PermissionError`, and the fake client saw **zero** Cypher calls | pass | pass |
| `TestModule161Dispatch::test_supervisor_and_above_receive_it[supervisor\|station-admin\|platform-admin]` | fail ×3 | pass ×3 |
| `TestModule161Dispatch::test_cr2_still_dispatches_to_graph_recurrence_person` — even a planted 1.000 for the new kind cannot move CR2 | pass | pass |
| `TestModule161Dispatch::test_all32_negative_control_equality` — 25 phrase-resolved with a planted 1.000, 7 generic-tier with their measured decisions: none moves, none lands here | fail (probe missing) | pass |
| `test_harness_tool_xagg.py::test_applicant_accused_overlap_renders` — the `AggregateKind` Literal guard | fail (`literal_error`) | pass |
| **`test_harness_tool_xagg.py::test_applicant_accused_overlap_is_denied_to_an_investigator`** — real gate through the wrapper: `DENIED`, `permission_denied`, no chunks | pass | pass |
| `test_semantic_dispatch.py::test_every_kind_the_chain_can_return_has_a_description` — semantic-only kinds are described, dispatchable, not chain kinds; the rest equals the chain | fail | pass |

---

## 4. Live verification

**Blocked by the model server, reported as blocked.** The tunnel
`https://undrafted-remodeler-gravel.ngrok-free.dev` answered `/health` 200
at the start of this module and served the full 261-item after-probe
(§6.2, §7 — 261 `/rerank` calls, mean 1.70 s, max 3.27 s, with three
disconnect/resume cycles), then went to ngrok's own 404 page at ~23:15 PKT
on 2026-09-14 and stayed there for the remaining 85+ minutes of this module
(polled every 5–45 s until 00:38 on 2026-09-15). Every `.env` in every
worktree still names the same URL; only the user's machine can bring it
back (memory: the URL rotates on the local server's restart). Nothing in
this branch depends on it at unit-test time.

**What IS live-verified, without the model server** (the graph and the
gateway are Docker, both up):

- The aggregate against the live `evidence_graph`, in process
  (`xagg._applicant_accused_overlap()` → `render_applicant_accused_overlap()`),
  reproducing §1's independently-measured figures to the digit. `XAGG` line
  verbatim in §2; rendered answer:

  ```
  Yes — exactly one person who used a police service is also recorded as an accused in an FIR we registered, matched on CNIC:
    - سرفراز احمد (CNIC 00000-1000055-1) — accused in fir-620-26; used: Khidmat Markaz application for driving license submitted 2021-04-10 (completed) [pkm_application:pkm-app-c14-01].

  Basis: 18 citizen-service records covering 14 distinct people, compared against 94 accused entries across our FIRs (92 distinct accused). Matched on CNIC, not on name.
  A match on name alone would have added 14 more 'match(es)' on 9 citizen(s) whose given name is shared by an accused on a different CNIC; those are not the same people and are not counted.
  ```

- The all-32 `run_aggregate()` control on both trees (§5.2, §7).
- The cross-encoder's decision on Q2 against the shipped table
  (`module161_probe_after.json`, scored live before the outage):
  `best=applicant_accused_overlap(0.034) runner_up=graph_recurrence_person(0.010)` — **below 0.40, so live Q2 would still fall through to the LLM classifier and refuse at XGRAPH/XNETWORK exactly as Module 145 recorded (§6.2)**. That is what the missing 3×3 would have shown, and it is stated here rather than implied.

**What remains, with the exact commands** (`evaluation/module161_inprocess_run.py`, `evaluation/module161_route_dict_equality.py`; both reuse Module 145's runners and record the answering model from `src.llm.client`'s `Falling back to` lines):

```
# before, in a pristine origin/main checkout:   PYTHONPATH=. python -X utf8 evaluation/module161_inprocess_run.py --tag before --runs 3 --ids Q2
# after, this branch:                            PYTHONPATH=. python -X utf8 evaluation/module161_inprocess_run.py --tag after  --runs 3 --ids Q2,L2,P1,P2,P3
# access boundary, live:  MODULE92_USER_ROLE=investigator PYTHONPATH=. python -X utf8 evaluation/module161_inprocess_run.py --tag after_investigator --runs 1 --ids Q2
# routes, nine fields:    PYTHONPATH=. python -X utf8 evaluation/module161_route_dict_equality.py --capture before|after ; --compare
```

---

## 5. Gold comparison

### 5.1 CR2 explicitly

CR2 (*"کیا کوئی ایسا شخص ہے جو ایک سے زیادہ مقدمات میں ملوث ہو…"*) resolves
by phrase to **`graph_recurrence_person`** and never consults the semantic
layer; `test_cr2_still_dispatches_to_graph_recurrence_person` plants a
full-score match for the new kind and CR2 does not move. In the all-32
aggregate run (§7) CR2's dispatch and rendered text are identical before
and after. The two are different joins and stay on different aggregates.

### 5.2 All-32 dispatch equality

- Phrase layer: unchanged by construction (no phrase list added).
- Semantic layer **force-armed on all 32** at 0.40 with the enlarged table
  (`module161_probe_after.json`): **moved 0/32**. The seven generic-tier
  gold questions' best descriptions after: D1 `total_count` 0.973, S2
  `station_or_category_counts` 0.999, CR3 `accused_relationship_breakdown`
  0.005, G6 `graph_recurrence_weapon` 0.007, KB1 `dv_report_fir_match`
  0.068, KB4 `legal_norm_lookup` 0.003, KB8 `reporting_delay_count` 0.004 —
  none is the new kind, none fires.
- `run_aggregate()` on all 32 against the live graph, both trees
  (`module161_live/module161_all32_aggregates_{before,after}.json`):
  **dispatch kind and rendered text identical on 32 of 32.**

---

## 6. Non-gold paraphrases — written before running, and the description

### 6.1 The set (`module161_targets.json`, written before the description existed)

| id | text | phrase tier | reaches the layer? |
|---|---|---|---|
| Q2 | Is there anyone who used one of our citizen services who also turns out to be under investigation for a crime? | generic | yes |
| L2 | Is there anyone who used one of our citizen services who is also under investigation? (Module 145's recorded wording) | generic | yes |
| P1 | Has anyone who filed a complaint with us later been charged with an offence? | generic | yes |
| P2 | Do any of the people who applied for a police service at the Khidmat Markaz show up as accused in an FIR? | `graph_recurrence_person` ("accused") | **no** |
| P3 (Roman-Urdu) | Kya koi aisa shehri hai jisne police se koi service li ho aur wo kisi case mein mulzim bhi ho? | `graph_recurrence_person` ("mulzim") | **no** |

### 6.2 Scores against the first description (D0), before → after

| id | before: best (score) | after: best (score) | fires at 0.40 |
|---|---|---|---|
| Q2 | graph_recurrence_person (0.010) | **applicant_accused_overlap (0.034)** | no |
| L2 | graph_recurrence_person (0.002) | **applicant_accused_overlap (0.018)** | no |
| P1 | cms_fir_linkage (0.006) | **applicant_accused_overlap (0.224)** | no |
| P2 | criminal_record_local_match_gap (0.007) | applicant_accused_overlap (**0.996**) | never reaches |
| P3 | unsupported_officer (0.181) | unsupported_officer (0.181) | never reaches; Roman-Urdu (Module 145's 157) |

So the description **is** the nearest capability for every reaching
paraphrase — the brief's anti-goal holds (P1 says neither "citizen
services" nor "under investigation" and still lands here first) — but the
cross-encoder's absolute score is far under the threshold for the two
wordings that name the silo as "citizen services" and the roster as "under
investigation", and 0.224 for the one that says "complaint … charged". P2,
which names both sides in the description's own words, scores **0.996**:
the scorer is rewarding lexical overlap, not the join. That is the
Module 145 §6 finding ("bi-encoder measures topic") seen from the other
side — the cross-encoder measures *wording*, and a semantic-only kind has
no other way in. Filed as 175 (§8).

### 6.3 The description shipped, and the candidates not yet measured

Shipped (D0): *"whether any member of the public who applied for a police
service or filed a complaint with the police, for example at a Khidmat
Markaz or through the complaint system, is also named as an accused or
suspect in an FIR, matched by CNIC"* — the two record types and the edge
role, in the words the projection code uses.

`evaluation/module161_description_candidates.py` declares three more
faithful phrasings (D1–D3: the silo named as "citizen service" —
`prompts/router.txt`'s own term for the Khidmat Markaz class of
procedures; the roster named as "accused under investigation" — the phrase
the table's `graph_recurrence_person` entry already uses) **and the
selection rule, written before any of them was scored**: maximise the
minimum over {Q2, L2, P1}, subject to every hazard and every generic-tier
gold question < 0.35; shorter wins ties; a candidate that still misses
0.40 is shipped anyway and reported as a miss. The script ran against the
tunnel and got ngrok's 404 on its first call (six retries); it has **not**
produced a measurement. D0 stays shipped because it is the only measured
one. Whoever picks this up runs the script once the tunnel is back, and if
the rule picks a different candidate, re-runs `module161_semantic_probe.py
--tag after` (the cache is keyed by table hash) and re-checks §5.2/§7.

---

## 7. Regression — all 32 gold questions

- **Aggregate dispatch and rendered text**: identical on 32/32 (§5.2).
- **Module 145's 24 hazards re-scored against the enlarged table**
  (`module161_probe_after.json`, full 261-item corpus re-scored): highest
  hazard against the new description **H14 at 0.008** (*"Who is the
  complainant in FIR 460/26?"*); at 0.40 **0 of 51 hazard-shaped items
  fire**, the true-positive/hazard sweep is unchanged from Module 145 at
  every threshold (13/23 fired TPs, 0/51 hazards at 0.40), paraphrase reach
  unchanged (16→26 of 63), gold force-armed moved 0/32. Thirteen corpus
  items changed *best description* to the new kind — all at ≤ 0.034 except
  the three targets above and P2 — i.e. the new sentence is a better
  nearest-neighbour for near-zero noise, which is the harmless direction.
- **Routes (nine load-bearing fields, 32/32)**: **not captured** —
  `module161_route_dict_equality.py` needs the local classifier for the
  four LLM-decided questions (A1, CR3, G1, G6) and the tunnel was down
  (§4). The structural argument Module 145 §7 made still holds unchanged
  for this branch: the phrase chain is byte-identical, the semantic layer
  fires on 0 of 32 with the enlarged table (§5.2), and the only new code on
  the routing path is one description the layer never fires on for a gold
  question. It is an argument, not a capture, and is labelled as such.

---

## 8. New defects, filed not fixed

Highest number in use at commit time: **173** — PR #98 filed it on
`origin/main` while this module ran (the keyword layer's false-positive
side), so these start at **174** and are filed as tracker rows.

**174 — The phrase chain's bare-noun tier pre-empts the semantic layer.**
P2 (*"…applied for a police service at the Khidmat Markaz show up as
accused in an FIR?"*) scores **0.996** against the new description and
never reaches it: "accused" lands it on `graph_recurrence_person` via
`_PERSON_KEYWORDS`, and `resolve_aggregate_kind()` consults the layer only
from `_GENERIC_AGGREGATE_KINDS`. Roman-Urdu "mulzim" does the same to P3.
Module 145 itself calls that tier "we recognised a noun, not the question",
yet it is treated as resolved for the purposes of the semantic fallback.
Extending the layer to `_ENTITY_RECURRENCE_AGGREGATE_KINDS` has its own
hazard set — S3, CR2 and KB9 resolve on that tier and must not move — so it
is a measured change, not a one-liner.

**175 — A semantic-only kind is reachable only from its description's own
wording.** Q2 0.034, L2 0.018, P1 0.224 against a faithful description that
ranks first for all three; P2 0.996 when the question repeats the
description's nouns. The cross-encoder rewards lexical overlap, so "written
from what the aggregate computes, not from the question" and "clears 0.40"
pull against each other for any capability whose users describe it in
words the code does not use ("citizen services", "under investigation").
`module161_description_candidates.py` is the pre-declared experiment; it is
unmeasured (§6.3). A query-side rewrite (the LLM normalising the question
into the table's vocabulary before scoring) is the alternative to
description-side wording, and has the Roman-Urdu problem (157) as a free
second target.

**176 — Ten of eighteen citizen-service records have no name in the graph.**
`project_pkm_application()`/`project_cms_complaint()` mint a `Person` only
when the record links to a case; the other ten carry only `applicant_cnic`
/`complainant_cnic`. This aggregate names its one match because the CNIC
hits an FIR's Person, but a "who used our services?" listing, or a no-match
answer that wants to say *who* was checked, cannot name anyone unlinked.
Ingestion recommendation: project `applicant.full_name` onto the
`StructuredRecord` (a plain property, soft-reference style, like the CNIC),
not a minted Person — minting would bypass entity_resolution's
corroboration gate, which the projection's own comments already refuse.

**177 — `/health` does not cover the model server, so a dead semantic
layer (and dead embeddings) is invisible to the startup checklist.**
`src/main.py::health_check()` probes Chroma and Postgres; `RERANKER_URL`,
`EMBEDDINGS_URL` and `LOCAL_LLM_URL` are not probed. Observed this module:
`/rerank` returning ngrok's HTML 404 for 85+ minutes while the backend
would have reported `status: ok`; `semantic_dispatch.prepare()` degrades
silently in 60 s cooldown windows (by design) and every RAG embed call
hard-fails (no fallback). One probe of `MODEL_SERVER_BASE_URL/health`,
reported as a separate field, would make the third infra piece visible.
