# -*- coding: utf-8 -*-
"""
Phase 3/4 — the deterministic compiler: validated spec in, safe query out.

THE CONTRACT THIS MODULE EXISTS TO HONOUR. `age_client.execute_cypher()`
embeds its `cypher_query` argument directly into SQL text — AGE declares
`cypher()`'s query_string as `cstring`, which libpq cannot bind as a wire
parameter, so the codebase's own module docstring states the rule: the
query is "a literal Cypher template — written in this codebase, never built
from request/user input", and `params` is "the ONLY safe place a
caller-supplied value belongs".

This compiler satisfies that rule structurally. Query STRUCTURE is built
from a closed algebra whose every token comes from the registry or from a
frozen constant in `spec.py`; query VALUES are always emitted as `$p0`,
`$p1`, ... and passed through `params`. No string from the question, and no
string an LLM produced, is ever concatenated into the returned Cypher.

WHAT IT IS NOT. Not a template library. There is no per-question emitter
and no lookup from question-shape to canned query. The same `compile()`
path emits "% of cases with >1 officer", "% with >1 accused" and "% with >1
weapon" from three specs that differ only in their bindings. If a future
question requires a new EMITTER rather than new bindings, that is the
signal the algebra was too narrow — not an invitation to add a template.

THE TWO PREDICATES THE COMPILER INJECTS, AND WHY THEY ARE NOT SPEC FIELDS.

  merged_into IS NULL   (node tombstones)
  superseded_by IS NULL (edge versions)

Both are correctness invariants of this data model, established by the
repository rather than invented here:
  - `scripts/merge_confirmed_duplicate_persons.py` tags merge donors with
    `merged_into` and keeps them "for provenance"; it never deletes them.
  - `src/graph/versioning.py` supersedes rather than mutates edges.
  - `src/retrieval/graph_retriever.py` already filters BOTH, at 8 sites.

Measured cost of omitting them (live, 2026-09-15):
    MATCH (p:Person)-[b:BELONGS_TO_CASE]->(c:Case)
      RETURN count(DISTINCT p.entity_id)                        -> 429
    ... WHERE p.merged_into IS NULL AND b.superseded_by IS NULL -> 208
A 2x over-count. `src/pipeline/xagg.py` applies `merged_into` at ZERO of
its 72 Cypher sites and `superseded_by` at 3.

They are therefore compiler-injected and cannot be switched off by a spec.
A caller who could request "include merged donors" could request a wrong
number, and no downstream check would catch it — the result is
well-formed, plausible, and off by 2x.

AGE DIALECT CONSTRAINTS, ALL MEASURED AGAINST THIS DATABASE. AGE is not
Neo4j and rejects forms an LLM writes by default:
    ORDER BY <alias>              -> UndefinedColumnError ("could not find
                                     rte for n"); use WITH ... RETURN ...
                                     ORDER BY instead.
    WHERE NOT (a)-[:R]->(:L)      -> PostgresSyntaxError at the anonymous
                                     node; count the complement instead.
    EXISTS { MATCH ... }          -> PostgresSyntaxError at "{".
    p.merged_into AS into         -> PostgresSyntaxError; `into` is
                                     reserved. Aliases are generated, never
                                     taken from user text.
Verified working and used below: WITH ... WHERE as HAVING, parameterised
thresholds, count(DISTINCT x), toFloat() division, nested WITH, multi-label
IN.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Optional

from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate.spec import (
    EDGE_GRAINS,
    IDENTITY_GRAINS,
    AggregateSpec,
    FieldPredicate,
    PopulationNode,
    RelationCountPredicate,
    Traversal,
)

logger = logging.getLogger(__name__)


class CompilerError(RuntimeError):
    """Raised when a spec cannot be compiled SAFELY.

    Distinct from a validation failure: the validator rejects specs a user
    could have phrased better, while this signals that compilation would
    have had to emit something unsafe. It is a bug-catcher of last resort —
    reaching it means a spec passed validation that should not have — so it
    raises rather than returning a degraded query.
    """


@dataclasses.dataclass(frozen=True)
class CompiledQuery:
    """Query text plus bound parameters, and the provenance to explain it.

    `backend` distinguishes AGE from Postgres. `notes` records every
    decision the compiler made that a reader of the number would need to
    know — which DISTINCT was applied and why, which invariant predicates
    were injected — and flows straight into the AggregateReceipt.
    """

    backend: str  # "age" | "postgres"
    text: str
    params: dict[str, Any]
    columns: tuple[str, ...]
    notes: tuple[str, ...] = ()
    injected_predicates: tuple[str, ...] = ()


class _ParamBag:
    """Allocates `$p0`, `$p1`, ... so no literal value enters query text.

    Values are opaque here on purpose: an Urdu station name, an injection
    attempt and an integer threshold are all just `$pN`. That is what makes
    "malicious wording cannot alter the query" a structural property rather
    than a filtering exercise.
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


#: Cypher comparison operators, keyed by the algebra's operator names. A
#: closed map: an operator absent here cannot be emitted at all, so an
#: unexpected string in a spec becomes a CompilerError rather than being
#: interpolated into a WHERE clause.
_CYPHER_OPS: dict[str, str] = {
    "eq": "=", "ne": "<>", "lt": "<", "lte": "<=", "gt": ">", "gte": ">=",
}


def _alias(prefix: str, i: int) -> str:
    """Generated aliases only — never derived from user text.

    Besides safety, this avoids AGE's reserved words: `p.merged_into AS
    into` is a syntax error, and a user-derived alias could collide with
    any of them.
    """
    return f"{prefix}{i}"


def _node_pattern(label: str, alias: str) -> str:
    return f"({alias}:{label})"


def _tombstone_predicate(
    snapshot: reg.RegistrySnapshot, label: str, alias: str
) -> Optional[str]:
    """`alias.merged_into IS NULL`, but only where the label has tombstones.

    Emitting it unconditionally would be harmless in AGE but misleading in
    the receipt: it would claim an exclusion was applied on labels that have
    no merge donors at all. Only Person carries them today (222 of 430).
    """
    info = snapshot.entity(label)
    if info is not None and info.has_tombstones:
        return f"{alias}.{reg.TOMBSTONE_PROPERTY} IS NULL"
    return None


def _supersession_predicate(
    rel_info: Optional[reg.RelationshipInfo], edge_alias: str
) -> Optional[str]:
    """`edge.superseded_by IS NULL`, where that relationship is versioned."""
    if rel_info is not None and rel_info.is_versioned:
        return f"{edge_alias}.{reg.SUPERSEDED_PROPERTY} IS NULL"
    return None


def _field_predicate_cypher(
    pred: FieldPredicate, alias: str, bag: _ParamBag
) -> Optional[str]:
    """Compile one field condition. Values always become parameters."""
    prop = pred.field.split(".")[-1]
    target = f"{alias}.{prop}"

    if pred.op == "exists":
        return f"{target} IS NOT NULL"
    if pred.op == "in":
        return f"{target} IN {bag.add(list(pred.value or []))}"
    if pred.op == "contains":
        # AGE supports CONTAINS; the needle is still a bound parameter.
        return f"{target} CONTAINS {bag.add(pred.value)}"
    op = _CYPHER_OPS.get(pred.op)
    if op is None:
        raise CompilerError(f"operator {pred.op!r} has no safe Cypher emission")
    return f"{target} {op} {bag.add(pred.value)}"


def _compile_relation_count_predicate(
    snapshot: reg.RegistrySnapshot,
    pred: RelationCountPredicate,
    base_label: str,
    base_alias: str,
    bag: _ParamBag,
) -> tuple[str, list[str]]:
    """Compile "has more than N related X" into a correlated WITH block.

    THIS IS THE 4-vs-70 CASE, and the whole reason `count_grain` exists.
    Measured live on Officer-[:ASSIGNED_TO]->Case:

        WITH c, count(DISTINCT o.entity_id) AS n WHERE n > 1  ->  4 cases
        WITH c, count(r) AS n                   WHERE n > 1  -> 70 cases

    The gap is 67 (case, officer) pairs carrying two ASSIGNED_TO edges,
    because the same officer is both `recording` and `investigating`. So:

      ENTITY grain      -> count(DISTINCT <target key>)  — how many PEOPLE
      RELATIONSHIP      -> count(<edge alias>)           — how many LINKS
      ROLE_PAIR         -> count(DISTINCT [key, role])   — how many ROLES

    The compiler cannot pick for the caller; the validator has already
    ensured the spec said which.
    """
    rel_alias = _alias("rc", 0)
    tgt_alias = _alias("rt", 0)

    if pred.direction == "out":
        info = snapshot.relationship(base_label, pred.rel, pred.target)
        pattern = (
            f"({base_alias})-[{rel_alias}:{pred.rel}]->"
            f"{_node_pattern(pred.target, tgt_alias)}"
        )
    else:
        info = snapshot.relationship(pred.target, pred.rel, base_label)
        pattern = (
            f"{_node_pattern(pred.target, tgt_alias)}-[{rel_alias}:{pred.rel}]->"
            f"({base_alias})"
        )

    where: list[str] = []
    injected: list[str] = []

    tomb = _tombstone_predicate(snapshot, pred.target, tgt_alias)
    if tomb:
        where.append(tomb)
        injected.append(tomb)
    sup = _supersession_predicate(info, rel_alias)
    if sup:
        where.append(sup)
        injected.append(sup)

    if pred.role_field and pred.role_value is not None:
        where.append(f"{rel_alias}.{pred.role_field} = {bag.add(pred.role_value)}")

    target_info = snapshot.entity(pred.target)
    if pred.count_grain in IDENTITY_GRAINS:
        key = target_info.distinct_key if target_info else None
        if not key:
            raise CompilerError(
                f"{pred.target} has no distinct key; cannot count its related "
                f"entities at {pred.count_grain} grain"
            )
        counted = f"count(DISTINCT {tgt_alias}.{key})"
    elif pred.count_grain == "ROLE_PAIR":
        key = target_info.distinct_key if target_info else None
        role = pred.role_field or "role"
        counted = f"count(DISTINCT [{tgt_alias}.{key}, {rel_alias}.{role}])"
    else:
        counted = f"count({rel_alias})"

    op = _CYPHER_OPS.get(pred.op)
    if op is None:
        raise CompilerError(f"operator {pred.op!r} has no safe Cypher emission")

    where_clause = (" WHERE " + " AND ".join(where)) if where else ""
    block = (
        f"MATCH {pattern}{where_clause} "
        f"WITH {base_alias}, {counted} AS relcnt "
        f"WHERE relcnt {op} {bag.add(pred.value)}"
    )
    return block, injected


def _compile_population(
    snapshot: reg.RegistrySnapshot,
    pop: PopulationNode,
    bag: _ParamBag,
    *,
    base_alias: str = "n",
) -> tuple[str, str, list[str], list[str]]:
    """Compile a population into MATCH/WHERE clauses.

    Returns (clause_text, terminal_alias, injected_predicates, notes).
    Traversals are emitted as one chained MATCH so the terminal alias is
    what later stages count — the fanout that chain introduces is exactly
    what `_counted_expression()` then has to neutralise.
    """
    injected: list[str] = []
    notes: list[str] = []
    where: list[str] = []

    alias = base_alias
    pattern = _node_pattern(pop.entity, alias)

    tomb = _tombstone_predicate(snapshot, pop.entity, alias)
    if tomb:
        where.append(tomb)
        injected.append(tomb)

    current_label = pop.entity
    for i, hop in enumerate(pop.traversals):
        edge_alias = _alias("e", i)
        next_alias = _alias("m", i)
        if hop.direction == "out":
            info = snapshot.relationship(current_label, hop.rel, hop.target)
            pattern += (
                f"-[{edge_alias}:{hop.rel}]->{_node_pattern(hop.target, next_alias)}"
            )
        else:
            info = snapshot.relationship(hop.target, hop.rel, current_label)
            pattern = (
                f"{_node_pattern(hop.target, next_alias)}-[{edge_alias}:{hop.rel}]->"
                + pattern
            )

        if info is not None and info.fans_in_direction(hop.direction):
            notes.append(
                f"traversal {current_label}-[{hop.rel}]->{hop.target} "
                f"(direction={hop.direction}) multiplies rows, up to "
                f"{info.max_fanout_in_direction(hop.direction)} per "
                f"{current_label}; DISTINCT is required for identity-grain "
                f"counts"
            )

        sup = _supersession_predicate(info, edge_alias)
        if sup:
            where.append(sup)
            injected.append(sup)
        t = _tombstone_predicate(snapshot, hop.target, next_alias)
        if t:
            where.append(t)
            injected.append(t)
        if hop.role_field and hop.role_value is not None:
            where.append(f"{edge_alias}.{hop.role_field} = {bag.add(hop.role_value)}")

        alias = next_alias
        current_label = hop.target

    clause = f"MATCH {pattern}"

    field_preds = [p for p in pop.predicates if isinstance(p, FieldPredicate)]
    for pred in field_preds:
        emitted = _field_predicate_cypher(pred, alias, bag)
        if emitted:
            where.append(emitted)

    if where:
        clause += " WHERE " + " AND ".join(where)

    rel_preds = [p for p in pop.predicates if isinstance(p, RelationCountPredicate)]
    for pred in rel_preds:
        block, inj = _compile_relation_count_predicate(
            snapshot, pred, pop.entity, base_alias, bag
        )
        clause += " " + block
        injected.extend(inj)
        notes.append(
            f"relation-count predicate on {pred.rel}->{pred.target} evaluated at "
            f"{pred.count_grain} grain"
        )

    return clause, alias, injected, notes


def _counted_expression(
    snapshot: reg.RegistrySnapshot, spec: AggregateSpec, alias: str
) -> tuple[str, str]:
    """The measure expression, and a note explaining the grain choice.

    THE ALIAS IS THE POPULATION'S OWN ENTITY, NOT THE TERMINAL OF ITS
    TRAVERSALS. A traversal filters the subject; it does not change it.
    "Persons who belong to a case" counts Persons, so the expression must
    read `n.entity_id` (the base alias), never `m0.<key>` (the Case the hop
    landed on). Emitting the terminal was a real bug caught by the first
    shadow run: `count(DISTINCT m0.entity_id)` over Case nodes returned 0,
    because Case is keyed by `case_id` and carries no `entity_id` at all —
    a confident, well-formed zero. `compile_spec()` therefore passes the
    BASE alias here, and `validator._validate_grain()` checks
    `distinct_key` against the same entity for the same reason.

    MEASURE FIRST, THEN GRAIN. `avg`/`sum`/`min`/`max` are value measures:
    they aggregate a PROPERTY, and the grain governs which rows are in
    scope, not what is computed. Checking grain first (as the first version
    did) emitted `count(DISTINCT ...)` for an `avg` spec — the right rows,
    the wrong arithmetic, and a number that looks like a plausible answer.

    THE STRUCTURAL FANOUT DEFENCE. If any traversal fans, an identity-grain
    count MUST be DISTINCT. Reaching the `raise` below means a spec got
    past the validator that should not have, so it fails loudly rather than
    emitting `count(*)` over a multiplied row set — which is how 73 cases
    becomes 449.
    """
    fanning = _population_fans(snapshot, spec.population)

    # ── Value measures: aggregate a property, whatever the grain ───────
    if spec.measure in ("sum", "avg", "min", "max"):
        prop = (spec.value_field or "").split(".")[-1]
        if not prop:
            raise CompilerError(f"measure {spec.measure} requires value_field")
        note = f"{spec.measure} over {alias}.{prop}"
        if fanning:
            # A fanning traversal repeats the same node's value once per
            # matched edge, which silently weights the average by degree.
            # Refuse rather than emit a plausible, wrong mean.
            raise CompilerError(
                f"{spec.measure} over a population containing a fanning "
                f"traversal would weight each value by its edge count; "
                f"narrow the population or aggregate at RELATIONSHIP grain "
                f"explicitly"
            )
        return f"{spec.measure}({alias}.{prop})", note

    if spec.measure == "median":
        raise CompilerError(
            "median is computed in Python over fetched values, not in Cypher; "
            "use compile_median_fetch()"
        )

    # ── Count measures ────────────────────────────────────────────────
    if spec.grain in IDENTITY_GRAINS or spec.measure == "count_distinct":
        if not spec.distinct_key:
            raise CompilerError(
                f"{spec.grain} grain requires distinct_key; refusing to emit a "
                f"non-DISTINCT count"
                + (" across a fanning traversal" if fanning else "")
            )
        expr = f"count(DISTINCT {alias}.{spec.distinct_key})"
        note = (
            f"counted DISTINCT {alias}.{spec.distinct_key} at {spec.grain} grain"
            + (" (required: population contains a fanning traversal)" if fanning else "")
        )
        return expr, note

    if spec.grain == "ROLE_PAIR":
        raise CompilerError(
            "ROLE_PAIR grain requires the role-bearing edge alias; compile the "
            "population with an explicit role traversal"
        )

    # RELATIONSHIP grain: counting links is the intent, so a fanning
    # traversal is not a hazard here — it is the thing being measured.
    return f"count({alias})", f"counted rows at {spec.grain} grain (links, not entities)"


def _population_fans(snapshot: reg.RegistrySnapshot, pop: PopulationNode) -> bool:
    """Whether any hop multiplies rows IN THE DIRECTION IT IS TRAVELLED."""
    current = pop.entity
    for hop in pop.traversals:
        info = (
            snapshot.relationship(current, hop.rel, hop.target)
            if hop.direction == "out"
            else snapshot.relationship(hop.target, hop.rel, current)
        )
        if info is not None and info.fans_in_direction(hop.direction):
            return True
        current = hop.target
    return False


def compile_spec(
    snapshot: reg.RegistrySnapshot, spec: AggregateSpec
) -> CompiledQuery:
    """Compile a VALIDATED spec into a parameterised AGE query.

    Callers must validate first. This function assumes the spec is legal
    and concerns itself only with emitting it safely; where the two
    overlap, it raises rather than degrading.
    """
    bag = _ParamBag()
    # `base_alias` is the population's own entity — what gets measured.
    # `terminal_alias` is where the traversals landed, used only for
    # grouping dimensions reached `via` a hop.
    clause, terminal_alias, injected, notes = _compile_population(
        snapshot, spec.population, bag
    )
    base_alias = "n"

    # Jurisdiction scope narrows to a caller-supplied allow-list. It is a
    # bound parameter like any other value, so a case id from a question
    # cannot widen it.
    if spec.scope.kind == "jurisdiction" and spec.scope.case_ids:
        scope_alias = base_alias if spec.population.entity == "Case" else None
        if scope_alias:
            connector = " AND " if " WHERE " in clause else " WHERE "
            clause += f"{connector}{scope_alias}.case_id IN {bag.add(list(spec.scope.case_ids))}"

    expr, grain_note = _counted_expression(snapshot, spec, base_alias)
    notes.append(grain_note)

    if spec.group_by:
        gb = spec.group_by[0]
        prop = gb.field.split(".")[-1]
        # A dimension declared `via` a traversal lives on the terminal
        # node; one named bare is a property of the measured entity.
        group_alias = terminal_alias if gb.via else base_alias
        # WITH ... RETURN ... ORDER BY, never ORDER BY <alias>: AGE rejects
        # the alias form with UndefinedColumnError.
        text = (
            f"{clause} "
            f"WITH {group_alias}.{prop} AS gkey, {expr} AS value "
            f"RETURN gkey, value ORDER BY value DESC"
        )
        if spec.top_n:
            text += f" LIMIT {int(spec.top_n)}"
        columns = ("gkey", "value")
    else:
        text = f"{clause} RETURN {expr} AS value"
        columns = ("value",)

    return CompiledQuery(
        backend="age",
        text=text,
        params=bag.values,
        columns=columns,
        notes=tuple(notes),
        injected_predicates=tuple(dict.fromkeys(injected)),
    )


def compile_ratio(
    snapshot: reg.RegistrySnapshot, spec: AggregateSpec
) -> tuple[CompiledQuery, CompiledQuery]:
    """Compile a ratio as TWO independent queries.

    Deliberately not one query with a conditional sum. Independence is the
    point: the numerator and denominator are computed separately, so the
    receipt can state both, the invariant checker can assert
    numerator <= denominator, and a denominator of zero is visible before a
    division is attempted rather than after.
    """
    if spec.ratio is None:
        raise CompilerError("compile_ratio called on a spec with no ratio")

    num_spec = dataclasses.replace(spec, population=spec.ratio.numerator, ratio=None, group_by=())
    den_spec = dataclasses.replace(spec, population=spec.ratio.denominator, ratio=None, group_by=())
    return compile_spec(snapshot, num_spec), compile_spec(snapshot, den_spec)
