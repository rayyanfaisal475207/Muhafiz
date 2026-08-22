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
| `StructuredRecord` | `record_id`, `record_type` | Typed rows with no identity of their own — declared here since the graph's original design, but never written by any code path until **M6a of the Muhafiz Data API migration** (`src/graph/structured_projection.py`, `docs/decisions/0001-muhafiz-api-migration.md`), which writes one per `fir_section`/`malkhana_register`/`chalaan_dispatch`/`chalaan_outcome`/`fir_zimni_index` row. The original `structured_records/*.csv` source this row referenced was itself never wired into any ingestion path — see the decision record for the full history. |

## Edge types

| Type | Direction | Key properties | Notes |
|---|---|---|---|
| `BELONGS_TO_CASE` | entity/Document/StructuredRecord → `Case` | `as_of`, `source_doc_id` | **Every** node except `Case` itself gets this at write time — not derived by walking through `Incident`. Makes within-case traversal a single filtered hop (architecture §7.1). |
| `APPEARS_IN` | entity → `Document` \| `StructuredRecord` | `char_span`, `confidence`, `as_of`, `superseded_by` | One edge per mention. Multiple mentions of the same canonical `Person` across documents produce multiple `APPEARS_IN` edges from the same node. |
| `ASSOCIATED_WITH` | `Person` → `Person` | `confidence`, `basis`, `as_of`, `superseded_by` | Generic person-to-person relationship extracted from domain-entity extraction (4.6) — e.g. co-accused, informal role. Confidence-scored, not a resolution decision (see `SAME_AS` below, which is a distinct edge type for identity, not association). |
| `SAME_AS` | mention `Person`/`Vehicle`/... → canonical node | `tier` (`cnic_auto`\|`flagged_unverified`\|`human_review`), `confidence`, `basis`, `status` (`pending`\|`confirmed`\|`rejected`), `as_of`, `source_doc_id`, `source_chunk_id`, `superseded_by` | **Not in the original architecture table** — added because the append-only versioning design (§7.4) rules out physically merging two AGE nodes on a probabilistic match. See "Entity resolution & canonicalization" below; this is the edge the review queue (4.11) lists and investigators confirm/reject. |
| `OWNS` | `Person` → `Vehicle`\|`PhoneNumber`\|`Weapon` | `as_of`, `source_doc_id` | Written for the first time in **M6a** (`structured_projection.py`), `Person`(accused)→`Weapon` only, matched by `recovered_from` name **within the same FIR** — matching across FIRs would risk this dataset's measured name collisions. `Person`→`Vehicle`/`PhoneNumber` still unwritten. |
| `REGISTERED_TO` | `Person` → `Vehicle` | `as_of`, `source_doc_id` | Still unwritten — deferred to **M6b** (needs PKM `vehicle_verification`, a cross-silo join, not a single-FIR fact). |
| `LOCATED_AT` | any entity → `Address` | `as_of`, `source_doc_id` | Written for the first time in **M6a**, `Person`→`Address` from `address_text` on complainant/accused/witness structured fields. |
| `INVOLVED_IN` | `Person` → `Incident` | `role`, `confidence`, `as_of` | Written for the first time in **M6a**, `role` ∈ `complainant`\|`accused`\|`witness` (an investigating officer is written as an unresolved `Person`-labelled node with no cross-document identity — no reliable identifier like `belt_no` is treated as one yet — not currently given an `INVOLVED_IN` edge). |
| `PART_OF` | `Incident` → `Case` | `as_of` | Written for the first time in **M6a**, one `Incident` node per FIR (`entity_id = f"INCIDENT-FIR-{fir_id}"`, deterministic — re-projecting the same FIR MERGEs onto the same node rather than duplicating it). Distinct from `BELONGS_TO_CASE` (which every node gets directly) — this is the specific Incident-to-Case structural link the architecture calls out. |
| `OCCURRED_ON` | `Incident`/`Person` → `Date` **node** | `locked` (bool), `locked_by`, `locked_at`, `as_of`, `superseded_by`, `event_type` | Correction: this row previously said "date (property, not a Date node)" — that was never accurate for the code. `entity_resolution.py`'s own incident-resolution path already writes a real `Date` node (`{date: "YYYY-MM-DD"}`), and **M6a** writes several more from typed timestamps (`incident_datetime`, `fir_zimni.entry_date`, `fir_accused.arrested_date`, `chalaan_dispatch.dispatch_datetime`), each tagged with an `event_type` property to distinguish them — deterministic, no LLM date parsing, unlike the free-text extraction path. `locked=true` blocks any writer (including a resolution/extraction re-run) from superseding this edge until an investigator calls `unlock_event()` — see `src/graph/versioning.py`. `Date` is pre-created via `migrations/020_age_date_and_cites_labels.sql` (M8), alongside `CITES` (see below) — both were previously left to AGE's lazy label creation, exposed to the concurrent-first-write race migration 005's own header describes pre-creating labels to prevent. |
| `CONFLICTS_WITH` | `Document`\|`Incident` → `Document`\|`Incident` | `basis`, `as_of` | Phase 8 (conflict detection) writes these through the same versioning primitive; not populated by Phase 4 itself, but the edge type is reserved here so Phase 8 doesn't need a schema change. |
| `CITES` | `Case` → `Case` | `status` (`pending`\|`confirmed`\|`rejected`), `confidence`, `basis`, `as_of`, `source_doc_id`, `superseded_by` | Written by **M6b** (`src/graph/cross_silo_projection.py`) — a confidence-scored FIR→FIR citation parsed from free text (`docs/decisions/0001-muhafiz-api-migration.md`, round-2 review item 3; measured live: 9 of 73 real FIRs cite another real FIR in prose), always written `status: pending`, never confirmed/rejected directly — same human-confirmation bar as a name-based `SAME_AS` candidate, since a regex hit against prose carries the same false-positive risk. Reviewed through `src/api/graph_review.py`'s **separate** `/citations/pending`, `/citations/{id}/confirm`, `/citations/{id}/reject` endpoints (a parallel queue, not merged into the `SAME_AS` one — `Case` nodes have no `entity_id`, only `case_id`, so forcing `CITES` through the identity queue's rendering would be wrong). |

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
global CNIC/plate dedup in `entity_resolution.py`, the identity/hop
expansion helpers in `graph_retriever.py` that operate on an
already-scoped entity-id frontier, and the entity-resolution review queue
in `graph_review.py`. **State this plainly to anyone relying on it:
`case_scope.py` is a hygiene backstop against future drift in the small
set of templates registered through it, not database-level defense-in-depth
for the graph as a whole.** If that gap matters for a specific future
feature, it needs a bespoke check at that feature's own call site, not an
assumption that `case_scope.py` already covers it.

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
