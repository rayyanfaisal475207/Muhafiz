# Phase 4 audit — version and tombstone filtering in the current engine

**Status: findings only. No production code was changed.** Per the brief,
the 72 Cypher sites in `src/pipeline/xagg.py` are documented here and left
working as they are. The new compiler injects these predicates
automatically (see `compiler.py`); this document records the gap it closes
and what would be involved in closing it in the legacy engine.

## Measured, 2026-09-15

Static scan of `src/pipeline/xagg.py`:

| Measure | Count |
|---|---|
| `execute_cypher(` call sites | **71** |
| Sites traversing a versioned relationship (`BELONGS_TO_CASE`, `APPEARS_IN`, `SAME_AS`, `ASSIGNED_TO`) | **41** |
| Sites filtering `superseded_by` | **1** |
| Sites filtering `merged_into` | **0** |
| **Sites traversing versioned edges with NO supersession filter** | **40** |

For comparison, `src/retrieval/graph_retriever.py` applies
`n.merged_into IS NULL AND b.superseded_by IS NULL` at **8** sites — the
same data, the other read path, with the filters applied. The two paths
disagree about what counts as live data.

## Why this matters — measured impact

Live, against the production graph:

```
MATCH (p:Person)-[b:BELONGS_TO_CASE]->(c:Case)
  RETURN count(DISTINCT p.entity_id)                          -> 429
MATCH (p:Person)-[b:BELONGS_TO_CASE]->(c:Case)
  WHERE p.merged_into IS NULL AND b.superseded_by IS NULL
  RETURN count(DISTINCT p.entity_id)                          -> 208
```

**A 2x over-count** on any cross-case person headcount that reaches through
`BELONGS_TO_CASE`.

Graph-wide supersession counts:

| Relationship | Superseded | Live |
|---|---|---|
| `BELONGS_TO_CASE` (all labels) | 222 | 3,383 |
| `Person-SAME_AS-Person` | 222 | 222 |
| `Person-APPEARS_IN-Document` | 222 | 255 |
| `Officer-ASSIGNED_TO-Case` | 1 | 143 |
| `Address-SAME_AS-Address` | 2,097 | 2,099 |

## Why the production engine is not currently wrong everywhere

Two mitigations, both accidental rather than designed:

1. **The 222 tombstoned Persons hold no `INVOLVED_IN` edge** (measured: 0).
   Every aggregate scoped to `role='accused'`/`victim`/`witness` reaches
   Persons through `INVOLVED_IN`, so the tombstones are out of scope for
   those. Confirmed live: `accused distinct` is 92 both with and without
   the filters.
2. **`scripts/merge_confirmed_duplicate_persons.py` superseded the donors'
   edges when it tagged them.** So filtering the EDGE and filtering the NODE
   currently give the same answer (both 208). They are independent
   mechanisms that happen to agree today.

Neither mitigation is a guarantee. A re-ingest that attaches an
`INVOLVED_IN` edge to a tombstoned Person, or a future merge that tags nodes
without superseding their edges, breaks the first and second respectively —
and the failure is silent, because the resulting number is well-formed.

## What the new engine does

`compiler.py` injects both predicates and does not let a spec disable them:

- `merged_into IS NULL` on any label the registry measured as carrying
  tombstones (today: Person only, 222 of 430).
- `superseded_by IS NULL` on any relationship the registry measured as
  versioned.

Both are recorded in `AggregateReceipt.injected_predicates`, so a reader can
confirm they were applied rather than assume it.

The predicates are emitted **only where the registry observed the property**,
so the receipt never claims an exclusion that did not apply.

## Recommended follow-up (NOT done here)

Not attempted in this phase, deliberately: changing 40 query sites in the
production engine is a behaviour change to the system that currently answers
the gold set, and it belongs behind shadow comparison rather than in a
foundation commit.

When it is done, the order should be:

1. Shadow-compare the new engine against the old on the affected kinds.
   Where they disagree, expect some disagreements to be the OLD engine
   over-counting — adjudicate each against the receipt rather than assuming
   either side.
2. Fix the legacy sites in one reviewed pass, with the gold-32 equality
   control as the gate.
3. Only then consider retiring the duplicated logic.

## Open question for supervisor review

The 222 confirmed `Person-SAME_AS-Person` edges form two components of
**139** and **85** nodes, all at tier `flagged_unverified` (name-similarity,
which the architecture explicitly caps below auto-merge confidence), and
**156 of them were confirmed by a single reviewer account**. The merge
script has already acted on them.

Whether those confirmations are genuine investigative decisions or a bulk
operation is **not determinable from the data**, and it materially affects
every person-level count. This needs a human answer before canonicalization
policy is finalised — which is why `spec.py` defaults Person to
`BOTH_AND_COMPARE` rather than `REQUIRED`.
