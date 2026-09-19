# -*- coding: utf-8 -*-
"""
Phase 7 — the composite plan: one answer, several authoritative sources.

WHAT THIS IS FOR. "How many people aged 20-30 were involved in cases during
2024?" needs three facts that no single source owns: `Person.age` (graph),
the person-case relationship (graph), and `incident_date` (Postgres — the
AGE `Case` node has no date at all). Before this module the only options
were to force one route to fake ownership of every field, or to drop the
field it could not express. Phase 6 measured what the second option costs:
the AGE planner dropped a date restriction and answered 73 where the truth
was 51.

WHAT THIS IS NOT. Not a distributed query engine, and not route voting.
A composite plan produces ONE deterministic result from cooperating
sources. Comparing independently-derived results happens afterwards, in
reconciliation — see `consensus.py`. Mixing those two ideas would turn a
corroboration signal into a self-confirming loop.

THE INVARIANT THAT MATTERS MOST. Every constraint is either APPLIED or the
plan REFUSES. There is deliberately no representation for "ignored": a
constraint whose authority cannot be resolved, or whose population cannot
be joined to the subject, produces a refusal carrying that constraint's
name. `unapplied_constraints` being non-empty is a refusal condition, not a
warning to be logged and passed over.

THE MODEL DOES NOT BUILD THESE. It may express the semantic requirements;
trusted code resolves authority (`authority.py`), executes each subplan,
and intersects the results (`populations.py`).
"""
from __future__ import annotations

import dataclasses
import enum
from typing import Any, Optional

from src.pipeline.aggregate import authority as auth
from src.pipeline.aggregate import populations as pops


class Metric(str, enum.Enum):
    """Aggregations a composite plan may request.

    Deliberately narrower than `spec.Measure`: these are the metrics whose
    composite semantics are defined over an intersected id set. Value
    measures (sum/avg/min/max) are not here — they aggregate a property
    over a population rather than counting the population, and the
    structured route already computes them correctly on a single source.
    """

    COUNT = "COUNT"
    COUNT_DISTINCT = "COUNT_DISTINCT"


@dataclasses.dataclass(frozen=True)
class Constraint:
    """One requested restriction, before authority is resolved.

    `field` is a logical name (`incident_date`) or a `Label.property` path
    (`Person.age`). `spec` carries the comparison itself — bounds for a
    temporal constraint, an operator and value for a property one — and is
    passed to the executor for that source, never interpolated anywhere.
    """

    name: str
    field: str
    kind: auth.ConstraintKind
    spec: dict[str, Any] = dataclasses.field(default_factory=dict)
    #: Which identifier this constraint's population is keyed on. Must
    #: match the other constraints' keys for them to be intersected.
    key: str = "case_id"

    def describe(self) -> str:
        return f"{self.name}({self.field})"


@dataclasses.dataclass(frozen=True)
class CompositeAggregatePlan:
    """A target, its constraints, and how their populations combine."""

    question_text: str
    #: The entity being measured, e.g. "Person".
    subject_label: str
    #: The identifier that counts one subject, e.g. "entity_id".
    subject_key: str
    grain: str
    metric: Metric
    constraints: tuple[Constraint, ...] = ()
    #: How constraint populations combine. INTERSECT is the default because
    #: multiple filters in one question must hold simultaneously.
    combine: pops.SetOp = pops.SetOp.INTERSECT

    def constraint_names(self) -> tuple[str, ...]:
        return tuple(c.name for c in self.constraints)


@dataclasses.dataclass(frozen=True)
class ConstraintExecution:
    """What one constraint resolved to, and what it produced."""

    constraint: Constraint
    decision: auth.AuthorityDecision
    population: Optional[pops.Population] = None
    applied: bool = False
    refusal_reason: Optional[str] = None

    def describe(self) -> str:
        if not self.applied:
            return f"{self.constraint.describe()}: NOT APPLIED ({self.refusal_reason})"
        return (
            f"{self.constraint.describe()} via {self.decision.source.value}"
            f":{self.decision.physical_path} -> "
            f"{self.population.n if self.population else 0} "
            f"{self.constraint.key}(s)"
        )


@dataclasses.dataclass(frozen=True)
class CompositeResult:
    """The outcome of a composite computation, answer or refusal.

    Carries enough to reproduce the computation: which constraint went to
    which source, what each produced, how they were combined, and what
    survived. `unapplied_constraints` is the no-silent-drop witness — when
    it is non-empty, `status` is "refused" and `value` is None.
    """

    status: str  # "ok" | "refused" | "failed"
    value: Any = None
    metric: Optional[Metric] = None
    grain: Optional[str] = None
    subject_label: Optional[str] = None

    executions: tuple[ConstraintExecution, ...] = ()
    composition_steps: tuple[pops.CompositionStep, ...] = ()
    final_population: Optional[pops.Population] = None

    refusal_code: Optional[str] = None
    refusal_reason: Optional[str] = None
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok"

    @property
    def applied_constraints(self) -> tuple[str, ...]:
        return tuple(e.constraint.name for e in self.executions if e.applied)

    @property
    def unapplied_constraints(self) -> tuple[str, ...]:
        return tuple(e.constraint.name for e in self.executions if not e.applied)

    def provenance(self) -> dict:
        """Auditable record of the whole composite computation (§13)."""
        return {
            "status": self.status,
            "metric": self.metric.value if self.metric else None,
            "grain": self.grain,
            "subject": self.subject_label,
            "constraints": [
                {
                    "name": e.constraint.name,
                    "field": e.constraint.field,
                    "kind": e.constraint.kind.value,
                    "source": e.decision.source.value,
                    "physical_path": e.decision.physical_path,
                    "authority_basis": e.decision.basis,
                    "applied": e.applied,
                    "matched_n": e.population.n if e.population else None,
                    "refusal_reason": e.refusal_reason,
                }
                for e in self.executions
            ],
            "composition": [s.describe() for s in self.composition_steps],
            "final_population_n": (
                self.final_population.n if self.final_population else None
            ),
            "applied_constraints": list(self.applied_constraints),
            "unapplied_constraints": list(self.unapplied_constraints),
            "value": self.value,
            "notes": list(self.notes),
        }


def refuse(
    plan: CompositeAggregatePlan,
    executions: tuple[ConstraintExecution, ...],
    code: str,
    reason: str,
) -> CompositeResult:
    """Build a refusal that names the constraints responsible."""
    return CompositeResult(
        status="refused",
        metric=plan.metric,
        grain=plan.grain,
        subject_label=plan.subject_label,
        executions=executions,
        refusal_code=code,
        refusal_reason=reason,
    )
