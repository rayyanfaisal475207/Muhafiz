# -*- coding: utf-8 -*-
"""
Phase 7 — deterministic population sets and the algebra over them.

WHY SETS OF STABLE IDS. A multi-path computation combines results from
different sources, and the only safe join key is one both sides genuinely
share. Postgres knows `cases.case_id`; the graph knows `Case.case_id`.
Those are the same identifier, so `{case_id}` is a sound join. Display
names are not: `PoliceStation.name` is Urdu free text that appears in both
places but is not guaranteed to match byte-for-byte, and joining on it
would silently lose rows.

THE LLM NEVER PERFORMS THE SET OPERATION. It may say two constraints must
hold together; this module computes the intersection. Asking a model to
intersect id lists would put arithmetic in the least reliable component and
make the result unauditable.

INTERSECTION IS THE DEFAULT, AND NARROWING IS THE ONLY SAFE DIRECTION.
`temporal.intersect_case_ids()` already established this for jurisdiction
scope: a filter may only ever narrow what the caller was permitted to see,
because widening turns a filter into privilege escalation. UNION exists
here for completeness but is never applied to a scope allow-list.
"""
from __future__ import annotations

import dataclasses
import enum
from typing import Iterable, Optional


class SetOp(str, enum.Enum):
    INTERSECT = "INTERSECT"
    UNION = "UNION"
    DIFFERENCE = "DIFFERENCE"


@dataclasses.dataclass(frozen=True)
class Population:
    """A set of stable identifiers, with where it came from.

    Ordering is preserved (insertion-ordered, de-duplicated) so a compiled
    allow-list is stable across runs — the same plan must produce the same
    query text, or receipts stop being comparable.
    """

    #: What the ids identify: "case_id", "entity_id", "record_id", ...
    key: str
    ids: tuple[str, ...]
    #: Which constraint produced this set, for provenance.
    origin: str
    source: str  # "postgres" | "graph"

    @property
    def n(self) -> int:
        return len(self.ids)

    @property
    def is_empty(self) -> bool:
        return not self.ids

    def describe(self) -> str:
        return f"{self.origin} -> {self.n} {self.key}(s) from {self.source}"


def _dedupe(ids: Iterable[str]) -> tuple[str, ...]:
    """Order-preserving de-duplication."""
    return tuple(dict.fromkeys(str(i) for i in ids))


def build(key: str, ids: Iterable[str], origin: str, source: str) -> Population:
    return Population(key=key, ids=_dedupe(ids), origin=origin, source=source)


class PopulationKeyMismatch(ValueError):
    """Two populations keyed on different identifiers cannot be combined.

    Raised rather than coerced: intersecting a set of `case_id`s with a set
    of `entity_id`s would produce an empty result that looks like a real
    answer ("no matches") instead of the type error it is.
    """


@dataclasses.dataclass(frozen=True)
class CompositionStep:
    """One set operation, recorded so the composition is reproducible."""

    op: SetOp
    left_origin: str
    right_origin: str
    left_n: int
    right_n: int
    result_n: int

    def describe(self) -> str:
        return (
            f"{self.left_origin}({self.left_n}) {self.op.value} "
            f"{self.right_origin}({self.right_n}) -> {self.result_n}"
        )


def combine(
    left: Population, right: Population, op: SetOp
) -> tuple[Population, CompositionStep]:
    """Apply one set operation, returning the result and its audit record."""
    if left.key != right.key:
        raise PopulationKeyMismatch(
            f"cannot {op.value} a population keyed on {left.key!r} with one "
            f"keyed on {right.key!r}; these identify different things"
        )

    right_set = set(right.ids)
    if op is SetOp.INTERSECT:
        ids = tuple(i for i in left.ids if i in right_set)
    elif op is SetOp.DIFFERENCE:
        ids = tuple(i for i in left.ids if i not in right_set)
    elif op is SetOp.UNION:
        ids = _dedupe(left.ids + right.ids)
    else:  # pragma: no cover - SetOp is closed
        raise ValueError(f"no implementation for set operation {op!r}")

    combined = Population(
        key=left.key,
        ids=ids,
        origin=f"({left.origin} {op.value} {right.origin})",
        # A combined population is only as authoritative as its narrowest
        # input, so the source is recorded as composite rather than
        # inheriting either side's claim.
        source="composite" if left.source != right.source else left.source,
    )
    step = CompositionStep(
        op=op, left_origin=left.origin, right_origin=right.origin,
        left_n=left.n, right_n=right.n, result_n=combined.n,
    )
    return combined, step


def intersect_all(
    populations: list[Population],
) -> tuple[Optional[Population], tuple[CompositionStep, ...]]:
    """Intersect every population, left to right.

    This is the operation nearly every multi-constraint question needs:
    all requested filters must hold SIMULTANEOUSLY. An empty result is a
    real answer ("nothing satisfies all of these"), not a failure — the
    caller must not treat it as "restriction unavailable" and drop it.
    """
    if not populations:
        return None, ()
    current = populations[0]
    steps: list[CompositionStep] = []
    for nxt in populations[1:]:
        current, step = combine(current, nxt, SetOp.INTERSECT)
        steps.append(step)
    return current, tuple(steps)
