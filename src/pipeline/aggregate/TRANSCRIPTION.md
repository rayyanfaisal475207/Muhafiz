# Phase 8 — Transcribing the 43 aggregate kinds into the algebra

**Status: analysis only.** Nothing here changes `src/pipeline/xagg.py`. The
43 functions remain the production path.

## Why this document exists

This is the feasibility gate for the whole design. The claim being tested is
narrow and falsifiable:

> The algebra in `spec.py` is expressive enough that the existing aggregates
> are COMBINATIONS of its operators, not 43 special cases.

If most kinds need a bespoke spec field or a bespoke compiler branch, the
algebra is a template library with extra steps, and the redesign should stop
here rather than proceed. So each kind below is classified by what it would
actually take to express, and the specialists are counted honestly rather
than explained away.

Classification:

| Code | Meaning |
|---|---|
| **GENERIC** | Expressible today with the committed operators. No new code. |
| **GENERIC+OP** | Expressible once one named, reusable operator is added (e.g. a date-difference measure). The operator is question-agnostic. |
| **SPECIALIST** | Genuinely not an aggregate over the data model; keep as a named function. |

---

## GENERIC — 24 of 43

Expressible now. Each is `measure` + `population` + `grain` (+ `group_by`,
`ratio`, `filters`), differing only in bindings.

| Kind | Spec sketch |
|---|---|
| `total_count` | `count`, `Case`, ENTITY/`case_id` |
| `total_accused_count` | `count_distinct`, `Person` ←`INVOLVED_IN`(role=accused), ENTITY/`entity_id` |
| `case_listing` | `count`/listing, `Case`, ENTITY |
| `station_total_count` | `count_distinct`, `PoliceStation`, ENTITY/`station_id` |
| `station_or_category_counts` | `count` + `group_by(police_station|crime_category)` |
| `top_districts_by` | `count` + `group_by(District.name via FILED_AT,PART_OF)` + `top_n` |
| `gender_breakdown` | `count` + `group_by(Person.gender)`, ENTITY |
| `offender_age_profile` | `min`/`max`/`avg` over `Person.age`, ENTITY + coverage |
| `graph_recurrence_person` | `count_distinct` `Person` with `RelationCountPredicate(BELONGS_TO_CASE, Case, gt, 1)` |
| `graph_recurrence_vehicle` | same tree, `Vehicle` |
| `graph_recurrence_weapon` | same tree, `Weapon` |
| `arrest_rate` | `ratio` of `Person`(arrest_status matched) over `Person`(accused) |
| `arrested` / `not_arrested_explicit` / `no_arrest_recorded` | the same ratio's numerator with a different `FieldPredicate` value — **three kinds, one tree** |
| `arrested_in_another_case` | `Person` + relation-count ≥2 + arrest predicate |
| `fir_section_case_count` | `count_distinct` `Case` via `StructuredRecord{fir_section}` + `group_by(section)` |
| `placeholder_officer_count` | `count_distinct` `Officer` + `FieldPredicate(name contains marker)` |
| `reporting_delay_count` | `count` `Incident` + `FieldPredicate(reporting_delay_reason exists)` |
| `chalaan_dispatch_count` | `count_distinct` `Case` via `StructuredRecord{record_type=chalaan_dispatch}` |
| `cms_fir_linkage` | `ratio`, `StructuredRecord{cms_complaint}` linked vs total |
| `dv_report_fir_match` | same shape, different `record_type` binding |
| `seized_property_disposition` | `count` + `group_by(disposition)` over `StructuredRecord{malkhana_register}` |
| `accused_relationship_breakdown` | `count` + `group_by(RELATED_TO.relationship)` |
| `incident_time_of_day` | `count` + `group_by(bucketed hour)` — needs the bucket operator below, otherwise generic |
| `weapon_recovery_rate_by_district` | `ratio` + `group_by(District)` |

**The important line in this table** is `arrested` / `not_arrested_explicit` /
`no_arrest_recorded`: three separate hardcoded kinds today, one tree with
three predicate values under the algebra. That is the compression the design
predicted.

---

## GENERIC+OP — 9 of 43

Need one reusable, question-agnostic operator. Each operator, once added,
serves every future question of that shape — this is the "new mathematical
operator without a question-specific function" property.

| Kind | Operator required |
|---|---|
| `incident_to_report_minutes_by_year` | `date_diff(a, b)` measure |
| `statute_mix_by_year` | `date_bucket(field, YEAR)` grouping dimension |
| `weapon_statute_cooccurrence_by_year` | `date_bucket` + 2-dim group (already supported) |
| `incident_time_of_day` | `numeric_bucket(field, boundaries)` |
| `fir_register_completeness` | `count_absent(field)` — the complement of a presence filter |
| `court_readiness_scan` | `count_absent` over several fields |
| `criminal_record_court_crosscheck` | `set_difference` between two populations |
| `criminal_record_local_match_gap` | `set_difference` |
| `officer_role_pair_overlap` | ROLE_PAIR grain + self-join predicate (grain exists; the self-join comparator does not) |

**Four operators** (`date_diff`, `date_bucket`, `numeric_bucket`,
`count_absent`) plus `set_difference` cover all nine. None is
question-specific.

---

## SPECIALIST — 10 of 43

Keep as named functions. Each is genuinely not an aggregate over the graph,
and forcing it into the algebra would be generality for its own sake.

| Kind | Why it is not an aggregate |
|---|---|
| `weapon_compliance_scan` | Its headline finding is a claim about the **register's schema shape** ("no field for packaging, photographs or chain of custody"). `_WEAPON_REGISTER_FIELDS` is the register's own column inventory, deliberately a declared constant because the projection drops columns the register has — deriving it from `keys(w)` would report the opposite of the truth. A count cannot express a claim about a schema. |
| `case_completeness_scan` | Multi-source by design, and **Postgres-authoritative**: the graph backfills `incident_date` and masks the gaps (1 missing in graph vs 9 in case rows). Composes four sub-scans. |
| `weapon_evidence_chain` | Multi-hop narrative chain (weapon → accused → outcome) returning a per-case story, not a number. |
| `statute_court_stage_join` | Join across two StructuredRecord types with stage semantics. |
| `station_caseload_by_specialisation` | Classification derived from **station names** by tokens, not a stored field. |
| `filtered_fir_listing` | Returns records, not an aggregate. |
| `unsupported_officer` | An honest refusal — deliberately a first-class outcome. |
| `unsupported_trend` | Same. |
| `closed` | A status-filter sub-branch, folded into `station_or_category_counts` in practice. |
| `criminal_record_*` (the pair above also appear in GENERIC+OP) | Listed there once `set_difference` exists; specialist until then. |

---

## Result

| Class | Count | Share |
|---|---|---|
| GENERIC (no new code) | 24 | 56% |
| GENERIC+OP (4–5 reusable operators) | 9 | 21% |
| SPECIALIST (stay as functions) | 10 | 23% |

**33 of 43 (77%) are compositional.** The architecture review predicted
"~6 of 43 remain specialists"; the measured figure is 10, which is worse
than predicted but well inside the ≤15 threshold set as the feasibility
gate. The algebra stands.

The specialists share a property worth stating: none of them is a *count of
things matching conditions*. They are schema claims, multi-source scans,
narrative chains, name-derived classifications, record listings and honest
refusals. That is a coherent boundary, not an arbitrary residue — which is
the strongest evidence that the algebra is carved at the right joint.

---

## What this does NOT prove

- That an LLM can reliably produce these specs from natural language. That
  is a later phase, and the validator's job is to refuse it when it cannot.
- That the GENERIC specs return the same NUMBERS as the current functions.
  That is shadow mode, and it is the next phase's gate — several current
  functions apply canonicalization or `superseded_by` filtering
  inconsistently (measured: `merged_into` at 0 of 72 Cypher sites,
  `superseded_by` at 3 of 72), so some disagreements are expected to be the
  OLD engine being wrong. Each must be adjudicated individually, not
  assumed in either direction.
