# Evidence Graph Schema (Apache AGE)

Phase 4 of `docs/IMPLEMENTATION_PLAN.md`. Design rationale lives in
`EVIDENCE_INTELLIGENCE_PLATFORM_ARCHITECTURE.md` §6-§7; this document is the
concrete schema `src/graph/*` and `src/extraction/*` implement against.

AGE enforces no schema — labels and properties are created lazily on first
write. **This document plus app-layer validation in `src/graph/*.py` is the
enforcement**, the same way the architecture doc frames it (§7.1). Every
writer goes through `src/graph/versioning.py` (see below), never raw Cypher
from elsewhere in the codebase.

## Graph identity

- Graph name: `evidence_graph` (created by `migrations/005_age_graph.sql`).
- Connection lifecycle: AGE requires `LOAD 'age'; SET search_path =
  ag_catalog, "$user", public;` active for the session a query runs on.
  `src/graph/age_client.py` owns a dedicated `asyncpg` pool, separate from
  the SQLAlchemy engine `src/database/postgres.py` uses for every relational
  table. **Both statements are re-run at the start of every
  `execute_cypher()` call**, on whichever connection the pool hands back —
  not once via asyncpg's `init=` pool callback, which was the first design
  and turned out to be unreliable in practice: empirically, a pool with
  more than one physical connection intermittently failed a `cypher()` call
  on a connection that *was* initialized at creation time (`function
  cypher(unknown, unknown, unknown) does not exist`, `type "agtype" does
  not exist`) — AGE's session-level catalog/type registration doesn't
  reliably survive being set up once and left alone across a pooled
  connection's later reuse. Two extra no-op statements per call is a
  trivial cost next to a graph write silently failing. The relational
  engine and pool are untouched by this; the two pools share the same
  Postgres instance but nothing else.
- Two further AGE-specific quirks age_client.py works around, both
  confirmed empirically rather than assumed (see the module's own
  comments): the pool is created with `statement_cache_size=0` (AGE's
  `cypher()` planner hook breaks under asyncpg's default server-side
  statement caching across differently-shaped queries on the same
  connection), and the `params` argument is always bound as `$1::agtype`
  with an explicit cast (an uncast `$1` intermittently fails function-
  overload resolution on a connection that hasn't just run a `cypher()`
  call). And — the concurrency-specific one — `migrations/005_age_graph.sql`
  pre-creates every label in this document's node/edge catalog via
  `create_vlabel`/`create_elabel` up front, because AGE creates a label's
  underlying table lazily on first write, and two concurrent transactions
  both writing the SAME brand-new label for the first time race to create
  the same catalog objects and one loses with a duplicate-key error.
  Pre-creating every known label before any concurrent ingestion traffic
  exists removes that race entirely.

## Node types

| Label | Key properties | Notes |
|---|---|---|
| `Case` | `case_id` (matches Postgres `cases.case_id`) | Mirrors the relational Case row; not re-derived, just referenced so graph traversal doesn't need a Postgres join for `BELONGS_TO_CASE`. |
| `Person` | `canonical_name`, `cnic` (nullable), `father_name`, `address_text`, `first_seen_as_of` | One node per *resolved* real-world person as far as the system currently believes — see "Entity resolution & canonicalization" below. A person with no CNIC anywhere still gets a node; `cnic` is simply null. |
| `Vehicle` | `plate`, `description` | Plate is the primary structured identifier (`ICT-XX-NNN`). |
| `PhoneNumber` | `number` (normalized digits) | |
| `Address` | `text`, `normalized_text` | Free-text address; not geocoded in this build. |
| `Organization` / `Gang` | `name`, `description` | Single label `Organization` used for both — "gang" is a `description`/role value, not a separate label, since AGE labels aren't hierarchical and the roster's own `type` column doesn't distinguish them either. |
| `Weapon` | `description` | e.g. "بور 30 پستول" — free text, no fixed taxonomy. |
| `Incident` | `incident_date`, `summary` | The event a Case's FIR narrates; distinct from `Document` (the paper) and `Case` (the investigation). |
| `Document` | `doc_id` (matches Postgres `documents.doc_id`), `doc_type` | One node per ingested document. |
| `StructuredRecord` | `record_id`, `record_type` | Typed rows with no identity of their own — declared here since the graph's original design, but never written by any code path until **M6a of the Muhafiz Data API migration** (`src/graph/structured_projection.py`, `docs/decisions/0001-muhafiz-api-migration.md`), which writes one per `fir_section`/`malkhana_register`/`chalaan_dispatch`/`chalaan_outcome`/`fir_zimni_index` row. The original `structured_records/*.csv` source this row referenced was itself never wired into any ingestion path — see the decision record for the full history. Milestone C5 (`docs/decisions/0002-graph-schema-expansion-and-scale.md`) added one exception: a `malkhana_register.item_detail` value shaped like a vehicle plate or phone number resolves into `Vehicle`/`PhoneNumber` instead of a `StructuredRecord` — see the C5 `APPEARS_IN{role:"recovered"}` entry below. |
| `PoliceStation` | `station_id` (`FirRecord.police_station_id`, falling back to the nested object's own `id`, then `name`), `name`, `code` | **Milestone B1** (`src/graph/structured_projection.py::_write_jurisdiction()`). Two independent writers MERGE onto the same node by `station_id`: B1's own filing-side write (real `name`/`code`) and C6's witness-home-jurisdiction write (empty properties, deliberately, so a witness-only reference can never clobber a real station's name — see C6's own `APPEARS_IN`/`LOCATED_AT` entry below). |
| `District` | `district_id` (a station's nested `district.id`/`.name`, or a bare district string), `name`, `province` (when known) | **Milestone B1**, written alongside `PoliceStation` — `Case-[FILED_AT]->PoliceStation-[PART_OF]->District`. |
| `Officer` | `canonical_name`, `belt_no` (nullable), `designation`, `phone` | **Milestone B2** — a fourth entity-resolution type alongside `person`/`vehicle`/`phone` (`src/graph/entity_resolution.py`'s `TYPE_TO_LABEL`/`TYPE_PRIMARY_ID_KEY`), resolved by `belt_no` with the same `cnic_auto`-tier hard-block discipline CNIC-based `Person` resolution gets. An officer mention with no `belt_no` on file (every `fir_zimni.officer_name` row, per C3) still resolves — through entity_resolution's ordinary name-fallback tiering, the same as any other belt_no-less mention, not a gap. |

## Edge types

| Type | Direction | Key properties | Notes |
|---|---|---|---|
| `BELONGS_TO_CASE` | entity/Document/StructuredRecord → `Case` | `as_of`, `source_doc_id` | **Every** node except `Case` itself gets this at write time — not derived by walking through `Incident`. Makes within-case traversal a single filtered hop (architecture §7.1). |
| `APPEARS_IN` | entity → `Document` \| `StructuredRecord` | `char_span`, `confidence`, `as_of`, `superseded_by` | One edge per mention. Multiple mentions of the same canonical `Person` across documents produce multiple `APPEARS_IN` edges from the same node. |
| `ASSOCIATED_WITH` | `Person` → `Person` | `confidence`, `basis`, `as_of`, `superseded_by` | Generic person-to-person relationship extracted from domain-entity extraction (4.6) — e.g. co-accused, informal role. Confidence-scored, not a resolution decision (see `SAME_AS` below, which is a distinct edge type for identity, not association). |
| `SAME_AS` | mention `Person`/`Vehicle`/... → canonical node | `tier` (`cnic_auto`\|`flagged_unverified`\|`human_review`), `confidence`, `basis`, `status` (`pending`\|`confirmed`\|`rejected`), `as_of`, `source_doc_id`, `source_chunk_id`, `superseded_by` | **Not in the original architecture table** — added because the append-only versioning design (§7.4) rules out physically merging two AGE nodes on a probabilistic match. See "Entity resolution & canonicalization" below; this is the edge the review queue (4.11) lists and investigators confirm/reject. |
| `OWNS` | `Person` → `Vehicle`\|`PhoneNumber`\|`Weapon` | `as_of`, `source_doc_id` | Written for the first time in **M6a** (`structured_projection.py`), `Person`(accused)→`Weapon` only, matched by `recovered_from` name **within the same FIR** — matching across FIRs would risk this dataset's measured name collisions. `Person`→`Vehicle`/`PhoneNumber` still unwritten. |
| `REGISTERED_TO` | `Person` → `Vehicle` | `as_of`, `source_doc_id` | Still unwritten — deferred to **M6b** (needs PKM `vehicle_verification`, a cross-silo join, not a single-FIR fact). |
| `LOCATED_AT` | any entity → `Address` | `as_of`, `source_doc_id` | Written for the first time in **M6a**, `Person`→`Address` from `address_text` on complainant/accused/witness structured fields. **Milestone C6** reuses this same label for a second, distinct fact: `Person`(witness)→`PoliceStation`\|`District`, the witness's HOME jurisdiction (`fir_witness.police_station_of_residence_id`/`other_district`) — separate from the case's own filing jurisdiction (`FILED_AT`, below) and from the witness's street `Address` above. The `PoliceStation` side of this write uses empty properties deliberately, so a witness-only reference never overwrites a real station name B1 already populated. |
| `INVOLVED_IN` | `Person` → `Incident` | `role`, `confidence`, `as_of` | Written for the first time in **M6a**, `role` ∈ `complainant`\|`accused`\|`witness`. **Correction (Milestone B2):** the note this row used to carry — "an investigating officer is written as an unresolved Person-labelled node with no cross-document identity, no reliable identifier like `belt_no` is treated as one yet" — is no longer true. B2 gave `Officer`/`belt_no` a full identity-resolution tier (see the `Officer` node entry above); officers are not written through `INVOLVED_IN` at all, they get their own `ASSIGNED_TO`/`OCCURRED_ON` edges below. |
| `PART_OF` | `Incident` → `Case` | `as_of` | Written for the first time in **M6a**, one `Incident` node per FIR (`entity_id = f"INCIDENT-FIR-{fir_id}"`, deterministic — re-projecting the same FIR MERGEs onto the same node rather than duplicating it). Distinct from `BELONGS_TO_CASE` (which every node gets directly) — this is the specific Incident-to-Case structural link the architecture calls out. **Milestone B1** reuses this same label for a second, semantically-consistent structural link: `PoliceStation`→`District`. |
| `FILED_AT` | `Case` → `PoliceStation` | `as_of`, `source_doc_id` | **Milestone B1** (`_write_jurisdiction()`) — the case's own filing station, from `FirRecord.police_station`/`.police_station_id`. Runs on every `--full` re-sync (M9), so this backfills every pre-existing `Case`, not only newly-ingested ones. Distinct from C6's `LOCATED_AT` above (a witness's home station is not the case's filing station). |
| `ASSIGNED_TO` | `Officer` → `Case` | `role` (`investigating`\|`recording`), `assigned_from`, `assigned_to`, `as_of`, `superseded_by` | **Milestone B2** — replaces the collapsed "current officer" string the Postgres `Case.investigation_officer` column carries with the graph's own full history. Investigating-officer rows (`fir_investigating_officer`) are written as a SUPERSESSION CHAIN when more than one exists (measured live: `fir-205-26`'s belt `1854L`→`GEN-0105` reassignment) — the prior edge is marked `superseded_by`, never deleted, so both "who is investigating now" (the edge with no `superseded_by`) and "who ever has" stay answerable. Recording officer (`FirRecord.recording_officer_*`) is a single point-in-time fact per FIR — one edge, nothing to supersede. |
| `RELATED_TO` | `Person` → `Person` | `role` (raw relationship text, e.g. "اجنبی", "بھائی", or `"landlord_of"`\|`"employer_of"`), `as_of`, `source_doc_id` | **Milestone C1** — written directly, confidence 1.0, no `SAME_AS`/pending step (a structured field, not a heuristic). Two sources: `fir_accused.relationship_to_victim`/`.relationship_to_complainant` (direction always accused→victim/complainant), and PKM `tenant_registration`(owner/tenant)/`employee_registration`(employer/employee) — the PKM path is never case-scoped and links only two ALREADY-EXISTING Person nodes matched by CNIC, never mints a fresh one (minting here would bypass entity_resolution's corroboration gate entirely, since there's no case to corroborate against). |
| `CROSS_VERSION_OF` | `Case` → `Case` | `filed_by`, `as_of`, `source_doc_id` | **Milestone C4** — from `psrms.cross_version.related_fir_display_code`, a CONFIRMED structured field (a real column, resolved through the same `display_code_index` CITES/PKM's `forwarded_fir_number` already use), written DIRECTLY like `RELATED_TO`/`OWNS` — no `status`, nothing to score, distinct from `CITES` below (a regex hit against prose, always `pending`). 0 populated rows in the live 73-FIR corpus at the time this landed; exercised against a constructed fixture, not a real-snapshot count. |
| `OCCURRED_ON` | `Incident`/`Person`/`Officer` → `Date` **node** | `locked` (bool), `locked_by`, `locked_at`, `as_of`, `superseded_by`, `event_type`, `detail` | Correction: this row previously said "date (property, not a Date node)" — that was never accurate for the code. `entity_resolution.py`'s own incident-resolution path already writes a real `Date` node (`{date: "YYYY-MM-DD"}`), and **M6a** writes several more from typed timestamps (`incident_datetime`, `fir_zimni.entry_date`, `fir_accused.arrested_date`, `chalaan_dispatch.dispatch_datetime`), each tagged with an `event_type` property to distinguish them — deterministic, no LLM date parsing, unlike the free-text extraction path. **Milestone C3** adds two more `event_type` values on the same idiom (a new `from_label`, not a new edge type): `"zimni_entry"` (`Officer`→`Date`, alongside the existing `Incident` zimni edge, same date) and `"position"` (`Incident`→`Date`, from `fir_position`'s full dated timeline, replacing the old "latest row only" collapse). `locked=true` blocks any writer (including a resolution/extraction re-run) from superseding this edge until an investigator calls `unlock_event()` — see `src/graph/versioning.py`. `Date` is pre-created via `migrations/020_age_date_and_cites_labels.sql` (M8), alongside `CITES` (see below) — both were previously left to AGE's lazy label creation, exposed to the concurrent-first-write race migration 005's own header describes pre-creating labels to prevent. |
| `CONFLICTS_WITH` | `Document`\|`Incident` → `Document`\|`Incident` | `basis`, `as_of` | Phase 8 (conflict detection) writes these through the same versioning primitive; not populated by Phase 4 itself, but the edge type is reserved here so Phase 8 doesn't need a schema change. |
| `CITES` | `Case` → `Case` | `status` (`pending`\|`confirmed`\|`rejected`), `confidence`, `basis`, `as_of`, `source_doc_id`, `superseded_by` | Written by **M6b** (`src/graph/cross_silo_projection.py`) — a confidence-scored FIR→FIR citation parsed from free text (`docs/decisions/0001-muhafiz-api-migration.md`, round-2 review item 3; measured live: 9 of 73 real FIRs cite another real FIR in prose), always written `status: pending`, never confirmed/rejected directly — same human-confirmation bar as a name-based `SAME_AS` candidate, since a regex hit against prose carries the same false-positive risk. Reviewed through `src/api/graph_review.py`'s **separate** `/citations/pending`, `/citations/{id}/confirm`, `/citations/{id}/reject` endpoints (a parallel queue, not merged into the `SAME_AS` one — `Case` nodes have no `entity_id`, only `case_id`, so forcing `CITES` through the identity queue's rendering would be wrong). |

`APPEARS_IN` (defined above the versioning section) also gained two reuses
worth calling out explicitly rather than leaving implicit in the generic
row: **Milestone C2** writes `Person→StructuredRecord` `APPEARS_IN{role:
"chalaan_accused"|"chalaan_witness", surface_text}` edges resolving
`chalaan_dispatch.accused_names`/`.witness_names` back to the FIR's own
already-written `Person` nodes — reusing `weapon_register.recovered_from`'s
existing in-FIR-only name-matching pattern (a name with no match in THIS
FIR's own accused/witness set is left unresolved, never looked up
elsewhere in the graph). **Milestone C5** writes `APPEARS_IN{role:
"recovered", surface_text}` when `malkhana_register.item_detail` classifies
as a plate/phone shape, on top of the generic `APPEARS_IN` the underlying
`Vehicle`/`PhoneNumber` write already produces via
`entity_resolution.resolve_and_write()` — the exact same call
`src/ingestion/service.py` makes for free-text plate/phone extraction, so
this correctly MERGEs onto whichever node already carries that value.

Every edge in the graph carries `confidence` (or is definitionally 1.0, e.g.
`BELONGS_TO_CASE`), `source_doc_id`, and — where the extraction traces to a
specific chunk rather than a whole document — `source_chunk_id`. **No edge
exists without provenance.**

## Versioning convention (append-only, §7.4)

Nothing in `src/graph/*` mutates an existing edge's properties in place.
`src/graph/versioning.py::write_edge(...)`:

1. Writes the new edge with `as_of = now()`.
2. If it supersedes a prior edge (same subject/predicate/object, a newer
   fact), sets `superseded_by = <new edge's id>` on the old edge — the old
   edge is never deleted, so "what did the system believe, and from what
   source, at what point in time" stays answerable.
3. For `OCCURRED_ON` specifically, checks `locked` on the current edge first;
   if `locked=true`, the write is skipped (logged, not raised) rather than
   creating a new version — this is the "suppresses further automatic
   revision" behavior SOW Module 6 asks for. `lock_event()`/`unlock_event()`
   are the only functions allowed to set/clear `locked`.

A query that wants "the current state" filters `WHERE superseded_by IS
NULL`. A query that wants history follows the `superseded_by` chain.

## Entity resolution & canonicalization (§7.3)

Resolution never physically merges two nodes — see `docs/graph_schema.md`'s
own versioning section above for why, and `src/graph/entity_resolution.py`
for the algorithm. Two cases:

- **CNIC auto-merge**: a new mention whose CNIC exactly matches an existing
  `Person` node's `cnic` is not a new node at all — its `APPEARS_IN` /
  `BELONGS_TO_CASE` / attribute edges attach directly to that existing node.
  There is no `SAME_AS` edge for this case; there was never a second entity
  to link.
- **Name-fallback tiers**: a new node is always created for the mention, and
  a `SAME_AS` edge (`status='pending'`) links it to the best candidate.
  Investigator action in the review queue writes a new `SAME_AS` edge with
  `status='confirmed'` or `status='rejected'`, superseding the pending one.

Any traversal that wants "all mentions of this real-world person" must
therefore follow `confirmed` `SAME_AS` edges, not assume one node = one
person. This is a deliberate, visible cost — Phase 5's graph retriever
inherits it — in exchange for never silently and irreversibly conflating two
people, which is the specific failure mode §7.3 calls "the single biggest
way a knowledge graph fails quietly."

**Milestone B2** added a fourth resolvable type, `Officer`, keyed on
`belt_no` — the same `cnic_auto`-equivalent exact-match hard-block tier
CNIC-based `Person` resolution gets (never scored by name similarity
against a candidate carrying a DIFFERENT non-empty `belt_no`), not a
looser parallel mechanism. `belt_no` was added to A1's `identity_index`
in the same module, so `Officer` resolution gets A1's O(1) lookup from
day one.

**Milestone E2** found and closed a gap in how this section's own rule
was enforced: `retrieve_graph()`'s confirmed-`SAME_AS` identity fold (the
mechanism this section describes — "follow `confirmed` edges, not one
node = one person") ran unconditionally, without going through the
per-hop case filter every ordinary hop already did. A confirmed `SAME_AS`
edge is frequently itself a cross-case link (the same person recognized
in two FIRs), so a *within-case* query could silently fold another
case's node into its traversal via that link, bypassing the cross-case
role gate entirely — closed by routing the identity fold's result
through the same case filter (`src/retrieval/graph_retriever.py`'s
`_filter_to_case()`) the ordinary hop path already used. See "Case
isolation for the graph" below for the full access-control picture this
sits inside.

**Milestone D (queue-scale resolution,
`docs/decisions/0002-graph-schema-expansion-and-scale.md`)** extends this
without weakening it. D1 (`src/graph/candidate_reprioritization.py`)
re-scores/reorders/groups the `pending` review queue against fresh
evidence — a deterministic template `why`, never an LLM narration, and
never a status change; confirming a match is still exclusively a human
action through `graph_review.py`. D2
(`src/retrieval/graph_retriever.py`, behind
`config.FEATURE_HEDGED_PENDING_TRAVERSAL`, cross-case retrieval only)
optionally traverses a `pending` `SAME_AS` link too, not just `confirmed`
— but always at a confidence capped below the verifier's hedge
threshold and tagged `same_as_status: "pending"`, so
`src/pipeline/verifier.py` refuses to deliver the answer without a
disclosed hedge. Recall goes up; nothing pending is ever presented as
settled identity.

## Example: 3-hop within-case traversal

```cypher
SELECT * FROM cypher('evidence_graph', $$
    MATCH (p:Person)-[:BELONGS_TO_CASE]->(c:Case {case_id: $case_id})
    MATCH (p)-[r1:APPEARS_IN]->(d:Document)
    MATCH (p)-[r2:ASSOCIATED_WITH]->(p2:Person)-[r3:APPEARS_IN]->(d2:Document)
    WHERE r1.superseded_by IS NULL AND r2.superseded_by IS NULL
      AND r3.superseded_by IS NULL
    RETURN p, r1, d, r2, p2, r3, d2
$$, $1::agtype) AS (p agtype, r1 agtype, d agtype, r2 agtype, p2 agtype, r3 agtype, d2 agtype);
```

Parameters are passed through `age_client.py`'s `execute_cypher(cypher_query,
params, columns)` as a bound `agtype` map (`$1`, explicitly cast — see
above), never string-concatenated — this is what makes Phase 7's
case-scope injection into traversals safe: the case filter becomes another
bound `params` entry, not string-built Cypher. `graph`/`cypher_query`
themselves are always literal templates written in this codebase (never
built from request input) — see age_client.py's module docstring for why
that's a real AGE constraint (`cstring` isn't a bindable wire parameter),
not a stylistic choice.
`d`/`d2` above give the source-document provenance for every hop, per the
"never hand graph results to the generator without their supporting chunks"
rule (architecture Figure 3).

## Case isolation for the graph — what actually backstops it (Phase 2)

AGE has **no native row-level-security equivalent**. Its vertex/edge
labels are real Postgres tables under the hood (`create_vlabel`/
`create_elabel` in `migrations/005_age_graph.sql`), but matching AGE's
internal row shape to an RLS predicate isn't a supported, documented AGE
interface — a Postgres RLS policy the way `documents`/`sessions`/`cases`/
`messages` have (migrations 008/010) is not practically usable here.

The closest structural equivalent this build offers is
`src/graph/case_scope.py::scoped_cypher()` — a chokepoint that every
template *meant* to be single-case-scoped is routed through, and which
refuses (raises, doesn't just log) to run a template that doesn't
reference `$case_id` at all. This is a much weaker guarantee than
relational RLS: it catches "a case-scoped template's filter got deleted,"
not "a bug in `case_scope.py` itself," and it does nothing for templates
that are legitimately, deliberately cross-case (entity resolution's
global CNIC/plate dedup in `entity_resolution.py`, and the
entity-resolution review queue in `graph_review.py` — see its own
"reviewed tradeoff" section below). **State this plainly to anyone
relying on it: `case_scope.py` is a hygiene backstop against future drift
in the small set of templates registered through it, not database-level
defense-in-depth for the graph as a whole.** If that gap matters for a
specific future feature, it needs a bespoke check at that feature's own
call site, not an assumption that `case_scope.py` already covers it.

**Correction (Milestone E, `docs/decisions/0002-...md`):** this
paragraph used to also list `graph_retriever.py`'s identity/hop expansion
helpers as "operate on an already-scoped entity-id frontier" — implying
they were safe by construction. That was true for ordinary
`ASSOCIATED_WITH` hops (filtered every hop via `_filter_to_case()`,
itself routed through `scoped_cypher()`) but NOT for the confirmed-
`SAME_AS` identity fold, which E2 found ran unfiltered — see the "Entity
resolution & canonicalization" section above for the finding and fix.
`case_scope.py`'s own module docstring is more precise than this
document used to be: it names the exact call sites routed through it
(`graph_retriever.py`'s within-case seed lookup, per-hop case filter —
now including the identity fold — and conflict lookup;
`entity_resolution.py`'s case-membership check; the harness's
`timeline_building.py`/`data_quality.py` agents) and is the source of
truth for that list, not this paragraph's own prose.

**Milestone E1** added a second, narrower form of cross-case capability:
station/district-scoped case enumeration
(`src/retrieval/graph_retriever.py::retrieve_jurisdiction_cases()`,
Milestone B1). Reading jurisdiction metadata only, not case evidence, it
is still treated as a BROADER capability than a single cross-case entity
link — "every case filed at this station" — so it reuses the exact same
`_enforce_cross_case_role_gate()` the `cross_case=True` traversal branch
above already enforces, not a looser tier or a second, parallel check.
`resolve_jurisdiction_case_ids()` (the router-facing entry point that
turns a query's free-text station/district into this function's inputs)
calls it unchanged — a second CALLER of the one gate, never a second
gate.

### Reviewed tradeoff: the entity-resolution review queue is deliberately cross-case

`src/api/graph_review.py`'s `/pending`, `/confirm`, `/reject` endpoints
are gated only by a global `supervisor`-or-above role — they do **not**
check `case_assignments` for either case a candidate `SAME_AS` match
touches. This was flagged by the 2026-07-27 audit (`issues.md`) as an
open question (solution.md §9.2): is this a deliberate exemption from
per-case confidentiality, or a gap?

**Resolved (2026-07-29): deliberate exemption, reviewed and confirmed.**
The whole point of this queue is finding the same real-world person across
different cases (the `SAME_AS` name-fallback tier exists precisely because
CNIC-based auto-merge can't catch every match) — restricting it to
reviewers already assigned to *both* cases in a candidate match would
mean the platform could never surface the cross-case link it exists to
find. Any global `supervisor`+ can see and act on cross-case identity
matches; this is intentionally broader than the per-case
`case_assignments` model everything else in the platform uses, and is not
scheduled to change. If a future feature needs this narrowed (e.g. a
dedicated cross-case-reviewer role, or surfacing matches only to both
cases' assigned reviewers), that is new scoped work, not a fix to an
existing bug.
