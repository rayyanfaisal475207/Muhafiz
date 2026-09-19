# -*- coding: utf-8 -*-
"""
Phase 5D — deterministic validation of an `AgeQueryPlan`.

THE SECOND OF THREE GATES. Strict parsing (`age_plan.py`) established that
the plan is structurally a plan. This module establishes that every
identifier in it EXISTS, that the traversal is coherent, and that executing
it will not be ruinously expensive. Only then may the compiler touch it.

IDENTIFIERS ARE CHECKED AGAINST LIVE MEASUREMENT, NOT A LIST. The
authority is `registry.RegistrySnapshot` — the same measured schema the
structured route validates against. That matters for two reasons. It is
live, so a label added by ingestion is usable without editing this file;
and it is *measured*, so the validator knows not just that `Person.age`
exists but that it is present on 19 of 430 nodes, which is the difference
between a grouping dimension and a silently-empty one.

NO REPAIR, EVER. A plan naming an unknown relationship is refused — it is
not rewritten to the nearest known one, and it is certainly not rewritten
to whatever the structured route chose. Repair would destroy the only
thing this route is for: an independently derived second opinion. A plan
that is wrong should produce a refusal a human can read, not a quiet
agreement.

COST LIMITS PROTECT AVAILABILITY, NOT INTEGRITY. A read-only query cannot
corrupt the graph, but it can pin a connection for minutes over 13,467
edges. The bounds here are deliberately crude — pattern count, grouping
arity, limit ceiling — because a real cost model is a research project and
these are sufficient for the shapes the corpus actually contains.
"""
from __future__ import annotations

import dataclasses
from typing import Optional

from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate.age_plan import (
    Aggregate,
    AgeQueryPlan,
    Direction,
    Operator,
)

#: Operators that take no operand. Passing a value with them is a plan
#: error, not something to quietly ignore.
_NULLARY_OPS = (Operator.IS_NULL, Operator.IS_NOT_NULL)

#: Aggregations that measure a numeric PROPERTY rather than counting rows.
_VALUE_AGGREGATES = (Aggregate.SUM, Aggregate.AVG, Aggregate.MIN, Aggregate.MAX)

# ── Cost ceilings, derived from the corpus's actual shapes ───────────
#: The deepest corpus traversal is two hops (Person -> Case -> Station).
#: Three allows headroom; beyond that the row count is unreasoned about.
MAX_PATTERNS = 3
#: One dimension, matching the structured compiler, which emits only
#: `spec.group_by[0]`.
MAX_GROUP_DIMENSIONS = 1
MAX_FILTERS = 8
MAX_LIMIT = 1000
#: A grouping dimension present on fewer than this fraction of nodes
#: produces mostly-NULL buckets — the "silently collapses into one bucket"
#: failure the schema card warns about.
MIN_GROUPING_PRESENCE = 0.5


@dataclasses.dataclass(frozen=True)
class PlanViolation:
    code: str
    message: str


@dataclasses.dataclass(frozen=True)
class PlanValidation:
    violations: tuple[PlanViolation, ...] = ()
    #: Registry facts the compiler needs and should not re-derive: which
    #: traversals fan (so counts must be DISTINCT-ed), which labels carry
    #: tombstones, which relationships are versioned.
    fanning_aliases: frozenset[str] = frozenset()
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.violations

    def codes(self) -> tuple[str, ...]:
        return tuple(v.code for v in self.violations)

    def summary(self) -> str:
        return "; ".join(f"{v.code}: {v.message}" for v in self.violations)


def _alias_labels(plan: AgeQueryPlan) -> dict[str, str]:
    """Map every alias the plan binds to its label.

    Built from the root plus each pattern. An alias used in a filter or
    aggregate that is not in this map was never matched, which would
    compile to a reference to nothing.
    """
    bound: dict[str, str] = {plan.root_alias: plan.root_label}
    for p in plan.patterns:
        bound.setdefault(p.from_alias, p.from_label)
        bound.setdefault(p.to_alias, p.to_label)
    return bound


def validate_plan(
    plan: AgeQueryPlan, snapshot: reg.RegistrySnapshot
) -> PlanValidation:
    """Check a parsed plan against measured schema and cost bounds."""
    v: list[PlanViolation] = []
    notes: list[str] = []
    known_labels = snapshot.known_labels()
    known_rels = snapshot.known_rel_types()

    # ── Labels ────────────────────────────────────────────────────────
    bound = _alias_labels(plan)
    for alias, label in bound.items():
        if label not in known_labels:
            v.append(PlanViolation(
                "unknown_entity",
                f"Label {label!r} (alias {alias!r}) is not in the graph. "
                f"Known labels: {', '.join(sorted(known_labels))}.",
            ))

    # An alias cannot mean two different labels.
    for p in plan.patterns:
        for alias, label in ((p.from_alias, p.from_label), (p.to_alias, p.to_label)):
            if bound.get(alias) != label:
                v.append(PlanViolation(
                    "alias_label_conflict",
                    f"Alias {alias!r} is bound to {bound.get(alias)!r} but also "
                    f"used as {label!r}.",
                ))

    # ── Cost ──────────────────────────────────────────────────────────
    if len(plan.patterns) > MAX_PATTERNS:
        v.append(PlanViolation(
            "too_many_patterns",
            f"{len(plan.patterns)} match patterns exceeds the limit of "
            f"{MAX_PATTERNS}; the resulting row count is not reasoned about.",
        ))
    if len(plan.filters) > MAX_FILTERS:
        v.append(PlanViolation(
            "too_many_filters",
            f"{len(plan.filters)} filters exceeds the limit of {MAX_FILTERS}.",
        ))
    if plan.limit is not None and plan.limit > MAX_LIMIT:
        v.append(PlanViolation(
            "limit_too_large",
            f"LIMIT {plan.limit} exceeds the ceiling of {MAX_LIMIT}.",
        ))

    # ── Relationships ─────────────────────────────────────────────────
    fanning: set[str] = set()
    for p in plan.patterns:
        if p.rel_type not in known_rels:
            v.append(PlanViolation(
                "unknown_relationship",
                f"Relationship type {p.rel_type!r} does not exist in the "
                f"graph.",
            ))
            continue

        # The triple must exist in the direction claimed. Checking the
        # TRIPLE rather than the type alone is what catches
        # (Officer)-[:BELONGS_TO_CASE]->(Case) when the measured edge is
        # (Person)-[:BELONGS_TO_CASE]->(Case).
        if p.direction == Direction.OUT:
            info = snapshot.relationship(p.from_label, p.rel_type, p.to_label)
            travel = "out"
        elif p.direction == Direction.IN:
            info = snapshot.relationship(p.to_label, p.rel_type, p.from_label)
            travel = "in"
        else:
            info = (
                snapshot.relationship(p.from_label, p.rel_type, p.to_label)
                or snapshot.relationship(p.to_label, p.rel_type, p.from_label)
            )
            travel = "out" if snapshot.relationship(
                p.from_label, p.rel_type, p.to_label
            ) else "in"

        if info is None:
            v.append(PlanViolation(
                "unknown_relationship",
                f"No measured edge ({p.from_label})-[:{p.rel_type}]->"
                f"({p.to_label}) in direction {p.direction.value}. The "
                f"relationship type exists but not between these labels.",
            ))
            continue

        # Record fanout so the compiler can DISTINCT correctly. This is the
        # structural defence against the measured 73 -> 449 and 73 -> 2,100
        # inflations.
        if info.fans_in_direction(travel):
            # PHASE 6 DEFECT FIX. This previously added only the SOURCE
            # side when travelling `in`, so the alias that actually
            # multiplies was never marked. Measured escape:
            # `(PoliceStation)<-[:FILED_AT]-(Case)` fans 7 cases per
            # station travelling `in`, but `fanning` held only the station
            # alias, so `COUNT(case_alias)` passed unflagged and the guard
            # caught it downstream as `fanout_without_distinct`.
            #
            # Both endpoints of a fanning hop are marked: rows multiply for
            # anything counted across it, whichever end is named.
            fanning.add(p.from_alias)
            fanning.add(p.to_alias)
            fanning.add(plan.root_alias)
            notes.append(
                f"({p.from_label})-[:{p.rel_type}]->({p.to_label}) fans "
                f"travelling {travel} (max {info.max_fanout_in_direction(travel)} "
                f"per node); entity counts must be DISTINCT."
            )
        if info.is_versioned:
            notes.append(
                f"{p.rel_type} is versioned ({info.superseded_n} superseded "
                f"edges); superseded_by IS NULL will be injected."
            )

    # ── Filters ───────────────────────────────────────────────────────
    for f in plan.filters:
        label = bound.get(f.alias)
        if label is None:
            v.append(PlanViolation(
                "unbound_alias",
                f"Filter references alias {f.alias!r}, which no pattern binds.",
            ))
            continue
        info = snapshot.entity(label)
        if info is not None and f.property not in info.properties:
            v.append(PlanViolation(
                "unknown_field",
                f"{label}.{f.property} was not observed on any node. "
                f"Filtering on it would match nothing.",
            ))
        if f.op in _NULLARY_OPS and f.value is not None:
            v.append(PlanViolation(
                "operator_value_mismatch",
                f"{f.op.value} takes no value, but one was supplied.",
            ))
        if f.op not in _NULLARY_OPS and f.value is None:
            v.append(PlanViolation(
                "operator_value_mismatch",
                f"{f.op.value} requires a value.",
            ))
        if f.op == Operator.IN and not isinstance(f.value, (list, tuple)):
            v.append(PlanViolation(
                "operator_value_mismatch",
                "IN requires a list of values.",
            ))

    # ── Aggregate ─────────────────────────────────────────────────────
    agg_label = bound.get(plan.aggregate_alias)
    if agg_label is None:
        v.append(PlanViolation(
            "unbound_alias",
            f"Aggregate references alias {plan.aggregate_alias!r}, which no "
            f"pattern binds.",
        ))
    else:
        agg_info = snapshot.entity(agg_label)
        if plan.aggregate in _VALUE_AGGREGATES:
            if not plan.aggregate_property:
                v.append(PlanViolation(
                    "missing_value_field",
                    f"{plan.aggregate.value} measures a property, but none "
                    f"was named.",
                ))
            elif agg_info is not None and plan.aggregate_property not in agg_info.properties:
                v.append(PlanViolation(
                    "unknown_field",
                    f"{agg_label}.{plan.aggregate_property} was not observed "
                    f"on any node, so {plan.aggregate.value} over it is "
                    f"undefined.",
                ))
        if plan.aggregate == Aggregate.COUNT_DISTINCT:
            key = plan.aggregate_property or (
                agg_info.distinct_key if agg_info else None
            )
            if not key:
                v.append(PlanViolation(
                    "missing_distinct_key",
                    f"COUNT_DISTINCT over {agg_label} needs a distinct key, "
                    f"and the registry records none for that label.",
                ))
            elif agg_info is not None and key not in agg_info.properties:
                v.append(PlanViolation(
                    "unknown_field",
                    f"{agg_label}.{key} is not a property of that label.",
                ))
        # A plain COUNT downstream of a fanning traversal counts edge rows,
        # not entities. Refuse rather than silently returning the inflated
        # number — this is the 4-vs-70 defect in its original form.
        if (
            plan.aggregate == Aggregate.COUNT
            and plan.aggregate_alias in fanning
        ):
            v.append(PlanViolation(
                "fanout_requires_distinct",
                f"COUNT over {plan.aggregate_alias!r} sits downstream of a "
                f"fanning traversal, so it would count relationship rows "
                f"rather than entities. Use COUNT_DISTINCT with a key.",
            ))

    # ── Grouping ──────────────────────────────────────────────────────
    if plan.group_by is not None:
        gb_label = bound.get(plan.group_by.alias)
        if gb_label is None:
            v.append(PlanViolation(
                "unbound_alias",
                f"group_by references alias {plan.group_by.alias!r}, which no "
                f"pattern binds.",
            ))
        else:
            gb_info = snapshot.entity(gb_label)
            prop = gb_info.property_presence(plan.group_by.property) if gb_info else None
            if prop is None:
                v.append(PlanViolation(
                    "unknown_field",
                    f"{gb_label}.{plan.group_by.property} was not observed; "
                    f"grouping by it would produce one NULL bucket.",
                ))
            elif prop.presence_rate < MIN_GROUPING_PRESENCE:
                v.append(PlanViolation(
                    "grouping_field_too_sparse",
                    f"{gb_label}.{plan.group_by.property} is present on only "
                    f"{prop.present_n}/{prop.total_n} nodes "
                    f"({prop.presence_rate:.0%}); grouping by it would "
                    f"silently discard most of the population.",
                ))

    return PlanValidation(
        violations=tuple(v),
        fanning_aliases=frozenset(fanning),
        notes=tuple(dict.fromkeys(notes)),
    )
