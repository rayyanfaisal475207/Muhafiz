# -*- coding: utf-8 -*-
"""
Phase 7 — executing a composite plan across authoritative sources.

THE SHAPE OF ONE RUN. For each constraint: resolve its authoritative source
(`authority.py`), execute it there, and collect a population of stable ids.
Then intersect the populations (`populations.py`), restrict the subject to
what survives, and aggregate. Every step is recorded so the answer can be
reproduced from the receipt alone.

THE RULE THAT GOVERNS EVERY BRANCH BELOW. A constraint is APPLIED or the
plan REFUSES. There is no path through this module that produces a value
while quietly ignoring a requested filter — every early return that skips a
constraint goes to `_refuse_unapplied`, naming it. That is the direct
answer to Phase 6's worst failure, where a dropped date restriction turned
a truth of 51 into a confident 73.

WHY THE SUBJECT IS RESTRICTED BY ID AND NOT BY PREDICATE. The date lives in
Postgres and the person-case edge lives in the graph; there is no single
query language that can express both. Resolving the date to a case-id
allow-list and binding that list as a parameter is what makes the
restriction genuinely execute rather than be approximated. This reuses the
mechanism `temporal.resolve_window()` and the structured compiler already
established — the list is bound as `$ids`, never interpolated.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from src.pipeline.aggregate import authority as auth
from src.pipeline.aggregate import populations as pops
from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate import temporal
from src.pipeline.aggregate.composite import (
    CompositeAggregatePlan,
    CompositeResult,
    Constraint,
    ConstraintExecution,
    Metric,
    refuse,
)

logger = logging.getLogger(__name__)

#: Identifier regex for anything interpolated into Cypher. The validator
#: and compiler already enforce this for the AGE route; a composite subplan
#: builds its own patterns, so it re-asserts the same boundary rather than
#: trusting a caller.
import re

_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _ident(name: str, kind: str) -> str:
    if not isinstance(name, str) or not _IDENT_RE.match(name):
        raise ValueError(f"refusing to interpolate {kind} {name!r}")
    return name


def _tombstone_clause(
    snapshot: reg.RegistrySnapshot, label: str, alias: str
) -> Optional[str]:
    """`alias.merged_into IS NULL`, only where the label carries tombstones.

    NOT OPTIONAL, AND NOT THE CALLER'S JOB. Phase 0 measured the cost of
    omitting it: 429 persons instead of 208, because 222 of 430 Person
    nodes are merge donors kept for provenance. A first version of this
    module omitted these predicates and reproduced exactly that number —
    the defect the structured compiler has injected automatically since
    Phase 1, reappearing in new code because the new code did not inherit
    the habit.

    Emitted only where the registry says the label actually has the
    property, so a filter is never applied to a label that would read NULL
    everywhere and silently empty the population.
    """
    info = snapshot.entity(label)
    if info is not None and info.has_tombstones:
        return f"{alias}.{reg.TOMBSTONE_PROPERTY} IS NULL"
    return None


def _supersession_clause(
    snapshot: reg.RegistrySnapshot,
    source_label: str,
    rel_type: str,
    target_label: str,
    edge_alias: str,
) -> Optional[str]:
    """`edge.superseded_by IS NULL`, where that triple is versioned.

    Resolved PER TRIPLE, not per relationship type: measured here,
    `BELONGS_TO_CASE` is versioned only on (Person)->(Case) (222 superseded)
    and unversioned on the eight other triples. Filtering by type alone
    would apply a predicate to edges that never carry the property.
    """
    info = (
        snapshot.relationship(source_label, rel_type, target_label)
        or snapshot.relationship(target_label, rel_type, source_label)
    )
    if info is not None and info.is_versioned:
        return f"{edge_alias}.{reg.SUPERSEDED_PROPERTY} IS NULL"
    return None


def _refuse_unapplied(
    plan: CompositeAggregatePlan,
    executions: tuple[ConstraintExecution, ...],
) -> Optional[CompositeResult]:
    """Refuse if any constraint failed to apply. The §12 invariant."""
    unapplied = [e for e in executions if not e.applied]
    if not unapplied:
        return None
    names = ", ".join(e.constraint.describe() for e in unapplied)
    reasons = "; ".join(
        f"{e.constraint.name}: {e.refusal_reason}" for e in unapplied
    )
    return refuse(
        plan, executions, "constraint_not_applied",
        f"Refusing rather than answering a different question: the "
        f"constraint(s) {names} could not be applied. {reasons}",
    )


async def _execute_temporal(
    snapshot: reg.RegistrySnapshot,
    constraint: Constraint,
    decision: auth.AuthorityDecision,
) -> ConstraintExecution:
    """Resolve a date bound to a case-id allow-list, via its authority."""
    start = constraint.spec.get("start")
    end = constraint.spec.get("end")

    if start is None and end is None:
        return ConstraintExecution(
            constraint, decision, applied=False,
            refusal_reason="temporal constraint has neither a start nor an end",
        )
    for label, bound in (("start", start), ("end", end)):
        if bound is not None and not temporal.is_valid_bound(bound):
            return ConstraintExecution(
                constraint, decision, applied=False,
                refusal_reason=f"{label} {bound!r} is not an ISO date",
            )
    if not temporal.bounds_ordered(start, end):
        return ConstraintExecution(
            constraint, decision, applied=False,
            refusal_reason=f"inverted range: start {start!r} after end {end!r}",
        )

    try:
        window = await temporal.resolve_window(
            snapshot, constraint.field, start, end
        )
    except Exception as exc:  # noqa: BLE001 — authority unreachable
        logger.error("composite: temporal authority unreachable: %s", exc)
        return ConstraintExecution(
            constraint, decision, applied=False,
            refusal_reason=(
                f"authoritative source for {constraint.field!r} could not be "
                f"reached: {exc}"
            ),
        )

    # An empty window is a REAL ANSWER — "no cases in that range" — not a
    # failed restriction. It applies, and the intersection below will
    # correctly yield nothing.
    population = pops.build(
        key="case_id", ids=window.case_ids,
        origin=constraint.name, source="postgres",
    )
    return ConstraintExecution(constraint, decision, population, applied=True)


async def _execute_graph_property(
    snapshot: reg.RegistrySnapshot,
    constraint: Constraint,
    decision: auth.AuthorityDecision,
) -> ConstraintExecution:
    """Collect the ids of subjects matching a graph property predicate."""
    from src.graph import age_client

    label = constraint.spec.get("label")
    prop = constraint.spec.get("property")
    key = constraint.spec.get("id_property")
    op = (constraint.spec.get("op") or "").lower()
    value = constraint.spec.get("value")

    if not (label and prop and key):
        return ConstraintExecution(
            constraint, decision, applied=False,
            refusal_reason="graph property constraint needs label, property and id_property",
        )

    ops = {
        "eq": "=", "ne": "<>", "gt": ">", "gte": ">=", "lt": "<", "lte": "<=",
    }
    try:
        label_s, prop_s, key_s = (
            _ident(label, "label"), _ident(prop, "property"), _ident(key, "id_property")
        )
    except ValueError as exc:
        return ConstraintExecution(
            constraint, decision, applied=False, refusal_reason=str(exc)
        )

    params: dict = {}
    if op == "between":
        lo, hi = constraint.spec.get("low"), constraint.spec.get("high")
        if lo is None or hi is None:
            return ConstraintExecution(
                constraint, decision, applied=False,
                refusal_reason="between requires low and high",
            )
        where = f"n.{prop_s} >= $lo AND n.{prop_s} <= $hi"
        params = {"lo": lo, "hi": hi}
    elif op in ops:
        where = f"n.{prop_s} {ops[op]} $v"
        params = {"v": value}
    else:
        return ConstraintExecution(
            constraint, decision, applied=False,
            refusal_reason=f"unsupported operator {op!r} for a graph property",
        )

    clauses = [where, f"n.{key_s} IS NOT NULL"]
    tomb = _tombstone_clause(snapshot, label_s, "n")
    if tomb:
        clauses.append(tomb)
    cypher = (
        f"MATCH (n:{label_s}) WHERE {' AND '.join(clauses)} "
        f"RETURN n.{key_s} AS id"
    )
    try:
        rows = await age_client.execute_cypher(cypher, params=params, columns=["id"])
    except Exception as exc:  # noqa: BLE001
        return ConstraintExecution(
            constraint, decision, applied=False,
            refusal_reason=f"graph property query failed: {exc}",
        )

    population = pops.build(
        key=constraint.key, ids=[str(r["id"]) for r in rows],
        origin=constraint.name, source="graph",
    )
    return ConstraintExecution(constraint, decision, population, applied=True)


async def _execute_relationship(
    snapshot: reg.RegistrySnapshot,
    constraint: Constraint,
    decision: auth.AuthorityDecision,
) -> ConstraintExecution:
    """Collect subject ids reachable along a declared path.

    `path` is a list of `{rel, target, direction}` hops written by trusted
    code, not by a model. Each identifier is re-checked before it is
    interpolated.
    """
    from src.graph import age_client

    label = constraint.spec.get("label")
    key = constraint.spec.get("id_property")
    path = constraint.spec.get("path") or []
    if not (label and key and path):
        return ConstraintExecution(
            constraint, decision, applied=False,
            refusal_reason="relationship constraint needs label, id_property and a path",
        )

    invariants: list[str] = []
    try:
        label_s = _ident(label, "label")
        pattern = f"(n:{label_s})"
        current_label = label_s
        for i, hop in enumerate(path):
            rel = _ident(hop["rel"], "relationship")
            tgt = _ident(hop["target"], "label")
            alias = f"h{i}"
            edge_alias = f"e{i}"
            if hop.get("direction", "out") == "out":
                pattern += f"-[{edge_alias}:{rel}]->({alias}:{tgt})"
            else:
                pattern += f"<-[{edge_alias}:{rel}]-({alias}:{tgt})"
            sup = _supersession_clause(
                snapshot, current_label, rel, tgt, edge_alias
            )
            if sup:
                invariants.append(sup)
            tomb_hop = _tombstone_clause(snapshot, tgt, alias)
            if tomb_hop:
                invariants.append(tomb_hop)
            current_label = tgt
        key_s = _ident(key, "id_property")
    except (ValueError, KeyError) as exc:
        return ConstraintExecution(
            constraint, decision, applied=False,
            refusal_reason=f"malformed relationship path: {exc}",
        )

    where = [f"n.{key_s} IS NOT NULL"]
    tomb = _tombstone_clause(snapshot, label_s, "n")
    if tomb:
        where.append(tomb)
    where.extend(invariants)
    params: dict = {}
    terminal = constraint.spec.get("terminal_filter")
    if terminal:
        t_alias = f"h{len(path) - 1}"
        try:
            t_prop = _ident(terminal["property"], "property")
        except (ValueError, KeyError) as exc:
            return ConstraintExecution(
                constraint, decision, applied=False,
                refusal_reason=f"malformed terminal filter: {exc}",
            )
        where.append(f"{t_alias}.{t_prop} = $tv")
        params["tv"] = terminal.get("value")

    cypher = (
        f"MATCH {pattern} WHERE {' AND '.join(where)} "
        f"RETURN n.{key_s} AS id"
    )
    try:
        rows = await age_client.execute_cypher(cypher, params=params, columns=["id"])
    except Exception as exc:  # noqa: BLE001
        return ConstraintExecution(
            constraint, decision, applied=False,
            refusal_reason=f"relationship query failed: {exc}",
        )

    population = pops.build(
        key=constraint.key, ids=[str(r["id"]) for r in rows],
        origin=constraint.name, source="graph",
    )
    return ConstraintExecution(constraint, decision, population, applied=True)


async def _subject_ids_for_cases(
    snapshot: reg.RegistrySnapshot,
    plan: CompositeAggregatePlan,
    case_ids: tuple[str, ...],
) -> tuple[Optional[tuple[str, ...]], Optional[str]]:
    """Subjects linked to an allowed case set, as subject ids.

    This is the bridge that lets a Postgres-resolved case restriction apply
    to a graph-resident subject. The allow-list is a BOUND parameter.
    """
    from src.graph import age_client

    link = plan_link(plan)
    if link is None:
        link, ambiguity = registry_case_link(snapshot, plan.subject_label)
        if link is None:
            return None, (
                ambiguity
                or f"no path from {plan.subject_label} to Case is declared by "
                   f"the plan or observed in the registry, so a case-id "
                   f"restriction cannot be applied to this subject"
            )
    rel, direction = link
    try:
        subj = _ident(plan.subject_label, "label")
        key = _ident(plan.subject_key, "id_property")
        rel_s = _ident(rel, "relationship")
    except ValueError as exc:
        return None, str(exc)

    arrow = f"-[e:{rel_s}]->" if direction == "out" else f"<-[e:{rel_s}]-"
    # Same invariants the structured compiler injects. Without them this
    # join counts merge donors and historical edge versions — measured:
    # 429 persons instead of 208.
    clauses = [f"c.case_id IN $ids", f"n.{key} IS NOT NULL"]
    tomb = _tombstone_clause(snapshot, subj, "n")
    if tomb:
        clauses.append(tomb)
    sup = _supersession_clause(snapshot, subj, rel_s, "Case", "e")
    if sup:
        clauses.append(sup)
    cypher = (
        f"MATCH (n:{subj}){arrow}(c:Case) "
        f"WHERE {' AND '.join(clauses)} "
        f"RETURN n.{key} AS id"
    )
    try:
        rows = await age_client.execute_cypher(
            cypher, params={"ids": list(case_ids)}, columns=["id"]
        )
    except Exception as exc:  # noqa: BLE001
        return None, f"subject/case join failed: {exc}"
    return tuple(str(r["id"]) for r in rows), None


def registry_case_link(
    snapshot: reg.RegistrySnapshot, subject_label: str
) -> tuple[Optional[tuple[str, str]], Optional[str]]:
    """The subject -> Case edge, taken from MEASURED data when it is unique.

    WHY THIS EXISTS. `plan_link` can only report a link the plan declared,
    and a plan whose only constraint is a time window declares none. The
    result was a refusal saying no path existed, for subjects where the
    registry had measured one all along — a statement that was false about
    the graph and unfalsifiable from the message.

    WHY IT REFUSES WHEN THERE IS MORE THAN ONE. Two different edges from a
    subject to Case are two different populations, and picking either one
    silently would change what was counted without saying so. That is the
    same class of error this package exists to prevent, so the ambiguity is
    reported with the candidates named and the caller refuses. A human or a
    planner can then say which one was meant; this layer will not guess.

    Returns `(link, None)` when exactly one edge exists, or
    `(None, reason)` when there are none or several.
    """
    candidates = [
        info for info in snapshot.relationships.values()
        if info.source_label == subject_label and info.target_label == "Case"
    ]
    # The reverse orientation is a real path too: the restriction still
    # applies, it is simply travelled the other way.
    reverse = [
        info for info in snapshot.relationships.values()
        if info.target_label == subject_label and info.source_label == "Case"
    ]

    options: list[tuple[str, str]] = (
        [(i.rel_type, "out") for i in candidates]
        + [(i.rel_type, "in") for i in reverse]
    )
    if not options:
        return None, None
    if len(options) > 1:
        named = ", ".join(
            f"{rel} ({direction})" for rel, direction in sorted(options)
        )
        return None, (
            f"{subject_label} reaches Case by more than one relationship "
            f"({named}); which one defines the population is a semantic "
            f"choice this layer will not make silently, so the case-id "
            f"restriction was not applied"
        )
    return options[0], None


def plan_link(plan: CompositeAggregatePlan) -> Optional[tuple[str, str]]:
    """The relationship joining the subject to Case, if the plan declares one."""
    link = getattr(plan, "subject_case_link", None)
    if link:
        return link
    for c in plan.constraints:
        if c.kind is auth.ConstraintKind.RELATIONSHIP:
            path = c.spec.get("path") or []
            if path and path[0].get("target") == "Case":
                return path[0]["rel"], path[0].get("direction", "out")
    return None


async def execute_composite(
    snapshot: reg.RegistrySnapshot,
    plan: CompositeAggregatePlan,
) -> CompositeResult:
    """Run a composite plan: resolve, execute, intersect, aggregate."""
    started = time.perf_counter()
    executions: list[ConstraintExecution] = []

    # ── Per-constraint authority resolution and execution ─────────────
    for constraint in plan.constraints:
        decision = auth.resolve_authority(snapshot, constraint.field, constraint.kind)
        if not decision.resolved:
            executions.append(ConstraintExecution(
                constraint, decision, applied=False,
                refusal_reason=decision.reason,
            ))
            continue

        if constraint.kind is auth.ConstraintKind.TEMPORAL:
            executions.append(await _execute_temporal(snapshot, constraint, decision))
        elif constraint.kind is auth.ConstraintKind.RELATIONSHIP:
            executions.append(
                await _execute_relationship(snapshot, constraint, decision)
            )
        elif decision.source is auth.Source.GRAPH:
            executions.append(
                await _execute_graph_property(snapshot, constraint, decision)
            )
        else:
            executions.append(ConstraintExecution(
                constraint, decision, applied=False,
                refusal_reason=(
                    f"no composite executor for a {decision.source.value} "
                    f"{constraint.kind.value} constraint"
                ),
            ))

    executions_t = tuple(executions)

    # ── THE INVARIANT: applied or refused, never ignored ──────────────
    blocked = _refuse_unapplied(plan, executions_t)
    if blocked is not None:
        return blocked

    # ── Translate case-keyed populations onto the subject ─────────────
    subject_pops: list[pops.Population] = []
    notes: list[str] = []
    for e in executions_t:
        if e.population is None:
            continue
        if e.population.key == plan.subject_key:
            subject_pops.append(e.population)
            continue
        if e.population.key == "case_id":
            ids, err = await _subject_ids_for_cases(
                snapshot, plan, e.population.ids
            )
            if err is not None:
                return refuse(
                    plan, executions_t, "constraint_not_applied",
                    f"{e.constraint.describe()} resolved to "
                    f"{e.population.n} case(s) but could not be applied to "
                    f"{plan.subject_label}: {err}",
                )
            subject_pops.append(pops.build(
                key=plan.subject_key, ids=ids,
                origin=f"{e.constraint.name}->{plan.subject_label}",
                source="composite",
            ))
            notes.append(
                f"{e.constraint.name}: {e.population.n} case(s) -> "
                f"{len(ids)} {plan.subject_label}(s)"
            )
            continue
        return refuse(
            plan, executions_t, "population_key_mismatch",
            f"{e.constraint.describe()} produced {e.population.key} ids, "
            f"which cannot be joined to {plan.subject_key}",
        )

    if not subject_pops:
        return refuse(
            plan, executions_t, "no_population",
            "no constraint produced a population to aggregate over",
        )

    # ── Deterministic composition ─────────────────────────────────────
    try:
        final, steps = pops.intersect_all(subject_pops)
    except pops.PopulationKeyMismatch as exc:
        return refuse(plan, executions_t, "population_key_mismatch", str(exc))

    value = final.n if final else 0
    notes.append(f"composite computed in {(time.perf_counter()-started)*1000:.0f} ms")

    return CompositeResult(
        status="ok",
        value=value,
        metric=plan.metric,
        grain=plan.grain,
        subject_label=plan.subject_label,
        executions=executions_t,
        composition_steps=steps,
        final_population=final,
        notes=tuple(notes),
    )
