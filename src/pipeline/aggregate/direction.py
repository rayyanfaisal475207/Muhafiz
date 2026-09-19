# -*- coding: utf-8 -*-
"""
Deterministic traversal-direction reconciliation.

THE OBSERVATION THIS IS BUILT ON. A hop names four things — source label,
relationship type, target label, and the direction it is travelled. The
first three are semantic choices: getting them wrong means asking a
different question. Direction is not, when only one orientation of that
triple exists in the graph. `Person -BELONGS_TO_CASE-> Case` is observed
449 times; `Case -BELONGS_TO_CASE-> Person` is observed zero times. A hop
naming the second one is not expressing a different intent, because there
is no data for that intent to be about. It is a typo with exactly one
possible correction.

WHY THE VALIDATOR CANNOT DO THIS ITSELF. `validate()` reports issues; it
does not rewrite the spec it was handed, and it should not — a validator
that quietly returns something other than what it validated is no longer a
validator. So reconciliation happens here, BEFORE validation, and hands
validation a spec that is either unchanged or corrected-and-recorded.
Whatever this module cannot fix deterministically, the validator still
refuses exactly as before.

THE THREE RULES THAT KEEP THIS HONEST.

  1. ONLY WHEN THE HOP IS ALREADY INVALID. A hop that resolves against the
     registry is left alone. This never "improves" a working spec.

  2. ONLY WHEN THE CORRECTION IS UNIQUE. If flipping the direction does not
     resolve, nothing happens. If the triple somehow resolves BOTH ways,
     nothing happens either — two orientations are two populations, and
     choosing between them is a semantic act this module is not entitled
     to perform.

  3. ONLY DIRECTION. Never the relationship type, never the labels, never a
     role filter. A plan that traverses the wrong relationship is asking
     the wrong question, and that must keep reaching the user as a refusal
     rather than being repaired into a plausible answer. This is the rule
     that stops this file from becoming a spec-fixer.

WHAT IS RECORDED. Every correction produces a `DirectionCorrection` naming
the hop and both orientations. These travel to the receipt, so a spec that
was executed is never silently different from the spec that was generated.
A corrected spec has a different `spec_hash()` from the generated one,
which is intended: the hash identifies the computation that ran.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Optional

from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate.spec import AggregateSpec, PopulationNode, Traversal

logger = logging.getLogger(__name__)

_OPPOSITE: dict[str, str] = {"out": "in", "in": "out"}


@dataclasses.dataclass(frozen=True)
class DirectionCorrection:
    """One hop whose direction was normalised, and the evidence for it."""

    source_label: str
    rel: str
    target: str
    from_direction: str
    to_direction: str

    @property
    def message(self) -> str:
        return (
            f"traversal {self.source_label} -[{self.rel}]- {self.target}: "
            f"direction {self.from_direction!r} is not observed in the graph; "
            f"{self.to_direction!r} is, uniquely, so it was used"
        )

    def to_dict(self) -> dict:
        return {
            "source_label": self.source_label,
            "rel": self.rel,
            "target": self.target,
            "from_direction": self.from_direction,
            "to_direction": self.to_direction,
            "message": self.message,
        }


def _resolves(
    snapshot: reg.RegistrySnapshot, source: str, rel: str, target: str, direction: str
) -> bool:
    """Whether this hop, travelled this way, matches a measured triple.

    Mirrors `validator._lookup_traversal` exactly: "out" reads the edge as
    stored, "in" reads it reversed. Duplicated deliberately rather than
    imported, because if the two ever disagreed this module would be
    correcting hops against a rule the validator does not apply.
    """
    if direction == "out":
        return snapshot.relationship(source, rel, target) is not None
    return snapshot.relationship(target, rel, source) is not None


def _reconcile_hop(
    snapshot: reg.RegistrySnapshot, source: str, hop: Traversal
) -> tuple[Traversal, Optional[DirectionCorrection]]:
    if _resolves(snapshot, source, hop.rel, hop.target, hop.direction):
        return hop, None  # rule 1

    flipped = _OPPOSITE.get(hop.direction)
    if flipped is None:
        return hop, None
    if not _resolves(snapshot, source, hop.rel, hop.target, flipped):
        return hop, None  # rule 2: no unique correction; validator refuses

    correction = DirectionCorrection(
        source_label=source,
        rel=hop.rel,
        target=hop.target,
        from_direction=hop.direction,
        to_direction=flipped,
    )
    return dataclasses.replace(hop, direction=flipped), correction


def _reconcile_population(
    snapshot: reg.RegistrySnapshot, pop: PopulationNode
) -> tuple[PopulationNode, list[DirectionCorrection]]:
    corrections: list[DirectionCorrection] = []
    hops: list[Traversal] = []
    current = pop.entity

    for hop in pop.traversals:
        fixed, correction = _reconcile_hop(snapshot, current, hop)
        if correction is not None:
            corrections.append(correction)
        hops.append(fixed)
        # The walk advances by target regardless, exactly as the validator
        # walks it: a hop this module could not fix is still refused later,
        # and stopping here would hide any further correctable hop.
        current = hop.target

    if not corrections:
        return pop, []
    return dataclasses.replace(pop, traversals=tuple(hops)), corrections


def reconcile_directions(
    snapshot: reg.RegistrySnapshot, spec: AggregateSpec
) -> tuple[AggregateSpec, tuple[DirectionCorrection, ...]]:
    """Normalise unambiguously-wrong hop directions in `spec`.

    Returns the spec unchanged with an empty tuple when there is nothing to
    correct, which is the common case and costs one registry lookup per hop.

    Only the population is walked. Ratio sides and grouping paths are left
    alone: they are separate populations whose correction would need the
    same evidence and the same recording, and doing them half-way here
    would be worse than not doing them at all.
    """
    population, corrections = _reconcile_population(snapshot, spec.population)
    if not corrections:
        return spec, ()

    for c in corrections:
        logger.info("aggregate direction reconciliation: %s", c.message)
    return dataclasses.replace(spec, population=population), tuple(corrections)
