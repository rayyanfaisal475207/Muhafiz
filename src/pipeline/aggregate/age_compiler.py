# -*- coding: utf-8 -*-
"""
Phase 5D — the deterministic read-only AGE compiler.

THE THIRD GATE, AND THE REAL BOUNDARY. Parsing proved the plan is a plan;
validation proved its identifiers exist and its cost is bounded. This
module turns it into Cypher — and it is the security boundary because of
what it CANNOT do rather than what it checks.

MUTATION IS UNREPRESENTABLE, NOT FILTERED. There is no code path in this
file that emits CREATE, MERGE, SET, REMOVE, DELETE, DETACH DELETE, DROP,
CALL, or any DDL. Not "those are rejected" — there is no emitter, no
branch, no format string containing those words. A plan cannot request
them because `AgeQueryPlan` has no field that could carry them, and even a
hypothetical malicious plan object reaching this function would produce a
read-only query, because read-only queries are the only thing the code
knows how to write. Compare this with `cypher_guard`, which must recognise
every disguise mutation might wear; here there is no disguise to see
through.

VALUES NEVER BECOME SYNTAX. Every model- or user-derived value goes
through `_ParamBag` and appears in the query only as `$p0`, `$p1`, ...
An Urdu station name, an integer threshold, and a string containing
`' OR 1=1; DROP ...` are indistinguishable at the text level — all three
are `$pN`. This is the same mechanism `compiler.py` uses for the
structured route, deliberately, because it is the property that makes
"malicious wording cannot alter the query" structural rather than a
filtering exercise. Identifiers (labels, relationship types, property
names) cannot be parameterised in Cypher and so ARE interpolated — which
is safe only because the validator cleared each one against the live
registry, and `_safe_identifier` re-asserts the character class here
rather than trusting that.

AGE DIALECT, NOT NEO4J. Measured against this database during Phase 0/1:
`ORDER BY <alias>` raises UndefinedColumnError, `EXISTS { }` and
`WHERE NOT (a)-[:R]->(:L)` are syntax errors, and `into` is reserved. The
emitters below produce `WITH ... RETURN ... ORDER BY`, generated aliases
only, and no negated patterns — so `cypher_guard.check_dialect` should
never fire on this output. If it ever does, that is a compiler defect and
the route treats it as one.

CORRECTNESS INVARIANTS ARE INJECTED, NOT OPTIONAL. Tombstones
(`merged_into IS NULL`) and edge supersession (`superseded_by IS NULL`)
are added automatically wherever the registry says the label or
relationship carries them. Phase 0 measured the cost of omitting them:
429 persons instead of 208, a 2x over-count. The model is not asked to
remember; the compiler does it.
"""
from __future__ import annotations

import dataclasses
import re
from typing import Any, Optional

from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate.age_plan import (
    Aggregate,
    AgeQueryPlan,
    Direction,
    Operator,
)
from src.pipeline.aggregate.age_plan_validator import PlanValidation


class CompilerError(RuntimeError):
    """The compiler was handed something it will not emit.

    Raised rather than degraded: a compiler that "does its best" with an
    unexpected plan is a compiler that emits something nobody designed.
    """


#: Closed operator map. An operator absent here cannot be emitted at all —
#: there is no fallback branch that interpolates the enum's name.
_OPS: dict[Operator, str] = {
    Operator.EQ: "=",
    Operator.NE: "<>",
    Operator.GT: ">",
    Operator.GTE: ">=",
    Operator.LT: "<",
    Operator.LTE: "<=",
    Operator.IN: "IN",
}

#: Identifiers must look like identifiers. The validator already cleared
#: every label/relationship/property against the registry; this is the
#: belt-and-braces check at the point of interpolation, because that is
#: the only place in this file where a string becomes syntax.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _safe_identifier(name: str, kind: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise CompilerError(
            f"refusing to interpolate {kind} {name!r}: not a bare identifier."
        )
    return name


class _ParamBag:
    """Allocates `$p0`, `$p1`, ... so no literal value enters query text.

    Same contract as `compiler._ParamBag`; duplicated rather than imported
    so the AGE route's compiler has no dependency on the structured
    route's internals (see the independence argument in `age_plan.py`).
    """

    def __init__(self) -> None:
        self._values: dict[str, Any] = {}
        self._n = 0

    def add(self, value: Any) -> str:
        name = f"p{self._n}"
        self._n += 1
        self._values[name] = value
        return f"${name}"

    @property
    def values(self) -> dict[str, Any]:
        return dict(self._values)


@dataclasses.dataclass(frozen=True)
class CompiledAgeQuery:
    """A query the deterministic compiler produced.

    The type is the point. `age_execution.execute_compiled_age_query()`
    accepts only this object, so a raw model string cannot be passed to
    the production execution path by accident — it would not type-check
    conceptually and, more usefully, would not have the attributes the
    executor reads. Only `compile_plan()` constructs one.
    """

    cypher: str
    params: dict[str, Any]
    columns: tuple[str, ...]
    #: What the compiler did and why, for the receipt: which DISTINCT was
    #: applied, which invariant predicates were injected.
    notes: tuple[str, ...] = ()
    injected_predicates: tuple[str, ...] = ()


def _node(alias: str, label: str) -> str:
    return f"({_safe_identifier(alias, 'alias')}:{_safe_identifier(label, 'label')})"


def _tombstone(
    snapshot: reg.RegistrySnapshot, label: str, alias: str
) -> Optional[str]:
    """`alias.merged_into IS NULL`, only where the label has tombstones.

    Emitted conditionally so the compiler never filters on a property the
    label does not carry — which would read NULL everywhere and silently
    empty the population.
    """
    info = snapshot.entity(label)
    if info is not None and info.has_tombstones:
        return f"{alias}.{reg.TOMBSTONE_PROPERTY} IS NULL"
    return None


def _supersession(info: Optional[reg.RelationshipInfo], edge_alias: str) -> Optional[str]:
    if info is not None and info.is_versioned:
        return f"{edge_alias}.{reg.SUPERSEDED_PROPERTY} IS NULL"
    return None


def _pattern_text(p, edge_alias: str) -> str:
    """One hop, oriented. ANY is emitted as an undirected pattern."""
    left = _node(p.from_alias, p.from_label)
    right = _node(p.to_alias, p.to_label)
    rel = f"[{edge_alias}:{_safe_identifier(p.rel_type, 'relationship')}]"
    if p.direction == Direction.OUT:
        return f"{left}-{rel}->{right}"
    if p.direction == Direction.IN:
        return f"{left}<-{rel}-{right}"
    return f"{left}-{rel}-{right}"


def _aggregate_expression(
    plan: AgeQueryPlan,
    snapshot: reg.RegistrySnapshot,
    alias_labels: dict[str, str],
) -> tuple[str, str]:
    """The counted/measured expression, plus a note explaining it."""
    alias = _safe_identifier(plan.aggregate_alias, "alias")
    label = alias_labels.get(plan.aggregate_alias, "")

    if plan.aggregate == Aggregate.COUNT:
        return f"count({alias})", f"count of {label} rows"

    if plan.aggregate == Aggregate.COUNT_DISTINCT:
        info = snapshot.entity(label)
        key = plan.aggregate_property or (info.distinct_key if info else None)
        if not key:
            raise CompilerError(
                f"COUNT_DISTINCT over {label} has no distinct key; the "
                f"validator should have refused this plan."
            )
        prop = _safe_identifier(key, "property")
        return (
            f"count(DISTINCT {alias}.{prop})",
            f"distinct {label} by {prop}",
        )

    if not plan.aggregate_property:
        raise CompilerError(
            f"{plan.aggregate.value} requires a property; the validator "
            f"should have refused this plan."
        )
    prop = _safe_identifier(plan.aggregate_property, "property")
    fn = {
        Aggregate.SUM: "sum", Aggregate.AVG: "avg",
        Aggregate.MIN: "min", Aggregate.MAX: "max",
    }[plan.aggregate]
    return f"{fn}({alias}.{prop})", f"{fn} of {label}.{prop}"


def compile_plan(
    plan: AgeQueryPlan,
    snapshot: reg.RegistrySnapshot,
    validation: PlanValidation,
) -> CompiledAgeQuery:
    """Compile a VALIDATED plan into read-only AGE Cypher.

    The caller must validate first; this function assumes legality and
    raises rather than degrading where the two overlap.
    """
    if not validation.ok:
        raise CompilerError(
            f"refusing to compile a plan that failed validation: "
            f"{validation.summary()}"
        )

    bag = _ParamBag()
    notes: list[str] = list(validation.notes)
    injected: list[str] = []

    alias_labels: dict[str, str] = {plan.root_alias: plan.root_label}
    for p in plan.patterns:
        alias_labels.setdefault(p.from_alias, p.from_label)
        alias_labels.setdefault(p.to_alias, p.to_label)

    # ── MATCH ─────────────────────────────────────────────────────────
    required: list[str] = []
    optional: list[str] = []
    where: list[str] = []

    root_clause = _node(plan.root_alias, plan.root_label)
    if not plan.patterns:
        required.append(root_clause)

    for i, p in enumerate(plan.patterns):
        edge_alias = f"e{i}"
        text = _pattern_text(p, edge_alias)
        (optional if p.optional else required).append(text)

        if p.direction == Direction.IN:
            info = snapshot.relationship(p.to_label, p.rel_type, p.from_label)
        else:
            info = (
                snapshot.relationship(p.from_label, p.rel_type, p.to_label)
                or snapshot.relationship(p.to_label, p.rel_type, p.from_label)
            )
        # A supersession filter on an OPTIONAL MATCH belongs in the
        # pattern's own scope, not the global WHERE — in the global WHERE
        # it would turn the optional hop into a required one.
        sup = _supersession(info, edge_alias)
        if sup and not p.optional:
            where.append(sup)
            injected.append(sup)

    # Tombstones for every matched node label.
    for alias, label in alias_labels.items():
        tomb = _tombstone(snapshot, label, _safe_identifier(alias, "alias"))
        if tomb:
            where.append(tomb)
            injected.append(tomb)

    # ── WHERE ─────────────────────────────────────────────────────────
    for f in plan.filters:
        alias = _safe_identifier(f.alias, "alias")
        prop = _safe_identifier(f.property, "property")
        target = f"{alias}.{prop}"
        if f.op == Operator.IS_NULL:
            where.append(f"{target} IS NULL")
            continue
        if f.op == Operator.IS_NOT_NULL:
            where.append(f"{target} IS NOT NULL")
            continue
        op = _OPS.get(f.op)
        if op is None:
            raise CompilerError(f"no emitter for operator {f.op!r}.")
        # The value becomes a bound parameter. This is the line that makes
        # injection structurally impossible rather than filtered.
        where.append(f"{target} {op} {bag.add(f.value)}")

    clause = "MATCH " + ", ".join(required) if required else f"MATCH {root_clause}"
    for opt in optional:
        clause += f" OPTIONAL MATCH {opt}"
    if where:
        # De-duplicated, order preserved. PHASE 6 DEFECT: the compiler
        # injects correctness invariants (`merged_into IS NULL`,
        # `superseded_by IS NULL`) unconditionally, and a model that
        # ALREADY expressed one as an explicit IS_NULL filter produced
        # `WHERE a.merged_into IS NULL AND a.merged_into IS NULL`.
        # Harmless to AGE but wrong to emit, and it misreports what the
        # plan asked for. Observed live on
        # `accused_distinct_through_relationship`.
        clause += " WHERE " + " AND ".join(dict.fromkeys(where))

    # ── RETURN ────────────────────────────────────────────────────────
    expr, agg_note = _aggregate_expression(plan, snapshot, alias_labels)
    notes.append(agg_note)

    if plan.group_by is not None:
        g_alias = _safe_identifier(plan.group_by.alias, "alias")
        g_prop = _safe_identifier(plan.group_by.property, "property")
        # NO ORDER BY. Measured against this database: AGE rejects
        # `ORDER BY <alias>` with UndefinedColumnError, and the guard's
        # `age_order_by_alias` rule refuses it — which it did, blocking a
        # first version of this emitter that copied the structured
        # compiler's `ORDER BY value DESC`. Sorting a grouped result is
        # presentation, the corpus requires no ordering, and the row count
        # is bounded by AGE_QUERY_MAX_ROWS, so the emitter simply does not
        # express it rather than carrying a dialect hazard for cosmetics.
        #
        # Every RETURN term is explicitly aliased: `expected_columns()`
        # returns () for a bare term, which makes the query unexecutable.
        text = (
            f"{clause} "
            f"WITH {g_alias}.{g_prop} AS gkey, {expr} AS value "
            f"RETURN gkey AS gkey, value AS value"
        )
        columns = ("gkey", "value")
    else:
        text = f"{clause} RETURN {expr} AS value"
        columns = ("value",)

    if plan.limit is not None:
        text += f" LIMIT {int(plan.limit)}"

    return CompiledAgeQuery(
        cypher=text,
        params=bag.values,
        columns=columns,
        notes=tuple(dict.fromkeys(notes)),
        injected_predicates=tuple(dict.fromkeys(injected)),
    )
