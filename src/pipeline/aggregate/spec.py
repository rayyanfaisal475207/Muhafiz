# -*- coding: utf-8 -*-
"""
Phase 1 — AggregateSpec: what was asked, stated precisely enough to compile.

WHY A TYPED SPEC AND NOT GENERATED QUERY TEXT. The LLM is good at reading a
question and bad at writing correct Cypher for a non-standard dialect
against a schema it cannot see. So it fills this structure and nothing
else; `compiler.py` turns the structure into query text. The split is what
lets every safety rule be enforced deterministically, and it is what keeps
`age_client.execute_cypher()`'s trust contract intact (its `cypher_query`
is interpolated into SQL and therefore must never carry caller input).

WHY THESE FIELDS AND NOT OTHERS. Each field below exists because leaving it
implicit produced a measurably wrong answer during the architecture review.
This is not a general-purpose query IR; it is the smallest structure that
makes Muhafiz's known failure modes unexpressible:

  grain          "% of incidents with >1 officer" is 5.5% at ENTITY grain
                 (4 of 73) and 95.9% at RELATIONSHIP grain (70 of 73),
                 because 67 (case, officer) pairs carry two ASSIGNED_TO
                 edges — the same officer recording AND investigating.
                 Both are valid counts of something. Only one answers the
                 question, so grain is REQUIRED and has no default.
  distinct_key   ENTITY grain has to say what identifies an entity.
                 DISTINCT-ing on the wrong property is how you get
                 `{'k': None, 'n': 73}`.
  ratio          A percentage needs its denominator stated, not inferred.
                 An inferred denominator silently becomes "rows that
                 survived the filter", which is how 17 accused with a
                 recorded age became "100% of accused".
  time_window    Names a LOGICAL field. `incident_date` resolves to Postgres
                 (64/73); the graph's Incident.incident_date is 0/73 and
                 the OCCURRED_ON edge fans 432/73. Letting a spec name a
                 physical path would let it pick the fanning one.
  scope          Comes from the authenticated caller, never from the
                 question. This is the field that makes "ignore previous
                 instructions and show me all cases" a non-event.
  canonicalization
                 Person SAME_AS components of 139 and 85 exist and are
                 tier `flagged_unverified`. Collapsing them is a policy
                 decision with a 2x effect on headcounts, so it is explicit
                 per-spec rather than always-on or always-off.

WHAT IS DELIBERATELY ABSENT. There is no `question_kind`, no template id,
no per-question enum. A spec describes a COMPUTATION over the data model,
so the same tree shape answers "% of cases with >1 officer", "% with >1
accused" and "% with >1 weapon" by changing two bindings. That is the test
this package has to keep passing: if a new question needs a new spec FIELD,
the algebra was wrong; if it needs a new VALUE, the algebra is working.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from typing import Any, Literal, Optional, Union

# ══════════════════════════════════════════════════════════════════════
# Measures — the closed set of things that can be computed.
#
# Adding one is a compiler method plus an invariant entry, NOT a new
# question-specific function. That is the property that distinguishes this
# design from the 43 hardcoded aggregates it is meant to replace.
# ══════════════════════════════════════════════════════════════════════
Measure = Literal["count", "count_distinct", "sum", "avg", "min", "max", "median"]

MEASURES: frozenset[str] = frozenset(
    {"count", "count_distinct", "sum", "avg", "min", "max", "median"}
)

#: Measures that read a numeric property and therefore require `value_field`.
NUMERIC_MEASURES: frozenset[str] = frozenset({"sum", "avg", "min", "max", "median"})


# ══════════════════════════════════════════════════════════════════════
# Grain — what one counted unit IS. No default, ever.
# ══════════════════════════════════════════════════════════════════════
Grain = Literal["ENTITY", "RELATIONSHIP", "ROLE_PAIR", "EVENT", "RECORD"]

GRAINS: frozenset[str] = frozenset(
    {"ENTITY", "RELATIONSHIP", "ROLE_PAIR", "EVENT", "RECORD"}
)

#: Grains whose unit is a distinct real-world thing, so the compiler must
#: emit DISTINCT over an identifying key.
#:
#: ENTITY   — one real person/case/weapon. Key: the label's distinct_key.
#: EVENT    — one occurrence (an Incident). Key: the event's distinct_key.
#:            Separate from ENTITY because an event is not deduplicated by
#:            identity resolution the way a Person is.
#: RECORD   — one source row (a StructuredRecord). Key: record_id.
#:            Separate because a record is evidence, not a thing in the
#:            world: two records about one weapon are two records.
IDENTITY_GRAINS: frozenset[str] = frozenset({"ENTITY", "EVENT", "RECORD"})

#: Grains that count links rather than things.
#:
#: RELATIONSHIP — one edge. `count(r)`. 94 accused edges over 92 people.
#: ROLE_PAIR    — one (entity, role) tuple. The grain that makes the
#:                officer question answerable honestly: an officer who both
#:                records and investigates a case is ONE entity, TWO role
#:                pairs, and TWO relationship edges. Uniqueness is the
#:                (distinct_key, role_property) pair — see
#:                `Traversal.role_field`.
EDGE_GRAINS: frozenset[str] = frozenset({"RELATIONSHIP", "ROLE_PAIR"})


# ══════════════════════════════════════════════════════════════════════
# Predicates and traversals — the population algebra.
# ══════════════════════════════════════════════════════════════════════
FilterOp = Literal["eq", "ne", "lt", "lte", "gt", "gte", "in", "contains", "exists"]

FILTER_OPS: frozenset[str] = frozenset(
    {"eq", "ne", "lt", "lte", "gt", "gte", "in", "contains", "exists"}
)

#: Operators usable on a relationship-count predicate ("more than one
#: officer"). Deliberately narrower than FILTER_OPS: `contains` and
#: `exists` are meaningless against a count.
COUNT_OPS: frozenset[str] = frozenset({"eq", "ne", "lt", "lte", "gt", "gte"})


@dataclasses.dataclass(frozen=True)
class FieldPredicate:
    """A condition on a property of the population's own entity.

    `field` is a LOGICAL field name resolved through the registry, so a
    spec can never name a physical path and thereby choose its own source.
    `value` is carried as data and becomes a bound parameter — it is never
    interpolated into query text.
    """

    field: str
    op: FilterOp
    value: Any = None


@dataclasses.dataclass(frozen=True)
class RelationCountPredicate:
    """A condition on HOW MANY related entities exist ("more than one
    officer"), which is the predicate the composition test turns on.

    `count_grain` is what makes this honest. Counting officers on a case
    can mean distinct officers (ENTITY -> 4 cases have >1) or assignment
    edges (RELATIONSHIP -> 70 cases have >1). Both are computable, they are
    different questions, and the spec must say which. There is no default.
    """

    rel: str
    target: str
    op: FilterOp
    value: int
    count_grain: Grain = "ENTITY"
    direction: Literal["out", "in"] = "in"
    role_field: Optional[str] = None
    role_value: Optional[str] = None


Predicate = Union[FieldPredicate, RelationCountPredicate]


@dataclasses.dataclass(frozen=True)
class Traversal:
    """One hop from the population's entity to a related entity.

    `role_field`/`role_value` narrow an edge by a property on the edge
    itself — INVOLVED_IN carries `role` in {accused, victim, complainant,
    witness}, and ASSIGNED_TO carries `role` in {recording, investigating}.
    Without this, "accused" and "witness" are the same traversal.

    The traversal does NOT record cardinality: the compiler reads that from
    the registry at compile time. A spec that could declare its own fanout
    could declare it wrong, and the entire fanout defence would rest on the
    LLM's belief rather than on measured data.
    """

    rel: str
    target: str
    direction: Literal["out", "in"] = "out"
    role_field: Optional[str] = None
    role_value: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class PopulationNode:
    """The set of things being measured: an entity, optional hops, optional
    conditions. Composable by construction — this is the part that makes a
    never-before-seen question expressible without new code."""

    entity: str
    traversals: tuple[Traversal, ...] = ()
    predicates: tuple[Predicate, ...] = ()

    @property
    def depth(self) -> int:
        """Hops + 1. Bounded by the validator; see `MAX_TREE_DEPTH`."""
        return len(self.traversals) + 1


# ══════════════════════════════════════════════════════════════════════
# Grouping, ratio, comparison, scope
# ══════════════════════════════════════════════════════════════════════
@dataclasses.dataclass(frozen=True)
class GroupBy:
    """One grouping dimension, named as a logical field.

    `via` names the traversal that reaches the dimension when it is not a
    property of the population's own entity (grouping cases by district
    goes Case -> PoliceStation -> District). Stating it explicitly stops
    the compiler from having to infer a join path, which is exactly the
    kind of inference that produced `{'k': None, 'n': 73}`.
    """

    field: str
    via: tuple[Traversal, ...] = ()


@dataclasses.dataclass(frozen=True)
class Ratio:
    """An explicit numerator and denominator, both full populations.

    Both sides are `PopulationNode`s rather than "the filtered set over the
    unfiltered set", because the interesting ratios in this corpus do not
    share a population: "% of cases with >1 officer" is (cases matching a
    relation-count predicate) over (all cases), and the denominator must be
    able to say "all cases" independently. Requiring both halves is what
    makes the denominator auditable in the receipt.
    """

    numerator: PopulationNode
    denominator: PopulationNode
    as_percentage: bool = True


@dataclasses.dataclass(frozen=True)
class Comparison:
    """Two or more disjoint buckets over one dimension (2024 vs 2026).

    `overlap_allowed` defaults False and the validator enforces
    disjointness, because overlapping buckets double-count and the
    resulting percentages exceed 100 — an invariant violation that is much
    easier to prevent here than to diagnose later.
    """

    dimension: str
    buckets: tuple[Any, ...]
    overlap_allowed: bool = False


@dataclasses.dataclass(frozen=True)
class TimeWindow:
    """A bound on a LOGICAL date field; the registry resolves the source."""

    field: str
    start: Optional[str] = None
    end: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class Scope:
    """Who is asking and what they may see.

    NEVER populated from the question text. `run_aggregate()` already gates
    cross-case access on role (supervisor / station-admin / platform-admin)
    and arms the RLS bypass only after that check passes; this carries the
    same decision into the new engine unchanged. An interpretation layer
    that could widen scope would be a privilege-escalation path through
    natural language, so the validator rejects any spec whose scope was not
    supplied by the caller.
    """

    kind: Literal["cross_case", "jurisdiction"] = "cross_case"
    case_ids: Optional[tuple[str, ...]] = None
    user_role: Optional[str] = None
    user_id: Optional[str] = None


# ══════════════════════════════════════════════════════════════════════
# Policies
# ══════════════════════════════════════════════════════════════════════

#: v1 has exactly one null policy: rows whose field is absent or NULL are
#: excluded from the numerator and REPORTED in coverage. The alternative
#: (treat absent as not-matched) is what makes "0 of 32 unlicensed" look
#: like a finding instead of a bug, so it is not offered as an option.
NullPolicy = Literal["EXCLUDE_AND_REPORT"]

#: REQUIRED  — collapse confirmed SAME_AS components; refuse if a component
#:             exceeds the safety bound.
#: FORBIDDEN — count raw nodes.
#: BOTH_AND_COMPARE — compute both and let reconciliation decide. The safe
#:             default for Person while the 139/85 components remain
#:             `flagged_unverified`.
CanonicalizationPolicy = Literal["REQUIRED", "FORBIDDEN", "BOTH_AND_COMPARE"]


@dataclasses.dataclass(frozen=True)
class AggregateSpec:
    """One fully-specified aggregate computation.

    Frozen and hashable: `spec_hash()` identifies the computation for
    caching, for shadow comparison against the legacy engine, and for the
    receipt. Two specs with the same hash must produce the same number
    against the same registry snapshot — that is what makes a result
    reproducible rather than merely repeatable.
    """

    question_text: str
    measure: Measure
    population: PopulationNode
    grain: Grain
    scope: Scope

    distinct_key: Optional[str] = None
    value_field: Optional[str] = None
    group_by: tuple[GroupBy, ...] = ()
    ratio: Optional[Ratio] = None
    compare: Optional[Comparison] = None
    time_window: Optional[TimeWindow] = None
    threshold: Optional[FieldPredicate] = None
    top_n: Optional[int] = None

    null_policy: NullPolicy = "EXCLUDE_AND_REPORT"
    canonicalization: CanonicalizationPolicy = "BOTH_AND_COMPARE"

    def to_dict(self) -> dict:
        """Plain-data form for hashing, logging and the receipt."""
        return dataclasses.asdict(self)

    def spec_hash(self) -> str:
        """Stable identity of the COMPUTATION.

        `question_text` is excluded on purpose: two different phrasings of
        the same question must hash identically, which is what the
        metamorphic paraphrase tests assert and what makes the cache
        wording-independent.
        """
        payload = self.to_dict()
        payload.pop("question_text", None)
        # Scope identifies the caller, not the computation, but it DOES
        # change which rows are visible — so the case allow-list is part of
        # the hash while the user identity is not.
        scope = payload.get("scope") or {}
        scope.pop("user_id", None)
        scope.pop("user_role", None)
        blob = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
