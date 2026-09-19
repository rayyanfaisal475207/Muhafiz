# -*- coding: utf-8 -*-
"""
Phase 7 — provable semantic equivalence, and nothing weaker.

THE PROBLEM THIS SOLVES. Phase 6 produced four cases where both routes
returned the SAME correct number and reconciliation still reported
SEMANTICALLY_DIFFERENT, because one declared `count` and the other
`count_distinct`. Real corroboration was being understated.

THE PROBLEM THIS MUST NOT CREATE. Declaring `count == count_distinct`
universally would be far worse than the understatement. Those operations
genuinely differ the moment a traversal can multiply rows — that difference
is the 73 -> 449 inflation and the 4-vs-70 conflict this whole architecture
exists to surface.

SO EQUIVALENCE IS PROVEN, NEVER ASSUMED. `count(a)` equals
`count(DISTINCT a.key)` only when BOTH hold:

  1. the query binds exactly one node and traverses nothing, so the match
     yields one row per node; and
  2. the label's distinct key is measured unique and present on every node.

Both are checked against the live registry. Measured for this database:
Person.entity_id 430/430/430, Weapon.entity_id 32/32/32,
StructuredRecord.record_id 713/713/713 — all provably unique. Where either
condition fails, the two forms stay different and reconciliation keeps
reporting the difference.

WHAT IS DELIBERATELY NOT CANONICALISED. Grain is a separate axis:
`malkhana_records` declared RECORD on one side and ENTITY on the other, and
that is a real disagreement about what one counted unit IS, not a spelling
difference. And `time_window_2024` agreed at 13 while filtering different
fields from different authorities (`Incident.report_datetime` vs
`cases.incident_date`) — numerically equal, semantically unrelated, and
exactly the coincidence that must not be laundered into agreement.
"""
from __future__ import annotations

import dataclasses
from typing import Optional

from src.pipeline.aggregate import registry as reg

#: The canonical form both `count` and `count_distinct` collapse into when
#: — and only when — equivalence is proven.
DISTINCT_ENTITY_COUNT = "DISTINCT_ENTITY_COUNT"


@dataclasses.dataclass(frozen=True)
class CanonicalizationResult:
    """What a metric was canonicalised to, and why it was allowed."""

    original: str
    canonical: str
    applied: bool
    basis: str

    def describe(self) -> str:
        if not self.applied:
            return f"{self.original}: not canonicalised ({self.basis})"
        return f"{self.original} -> {self.canonical} ({self.basis})"


def distinct_key_is_provably_unique(
    snapshot: reg.RegistrySnapshot, label: str
) -> tuple[bool, str]:
    """Is the label's distinct key present on every node and unique?

    Uniqueness is inferred from the registry's own measurement rather than
    re-queried: `_resolve_distinct_key()` selects a key only after checking
    that `count(n) == count(DISTINCT n.key)`, which is precisely this
    property. A label with no recorded key cannot support the proof.
    """
    info = snapshot.entity(label)
    if info is None:
        return False, f"{label} is not a measured label"
    if not info.distinct_key:
        return False, f"{label} has no unique key, so DISTINCT has no subject"

    presence = info.property_presence(info.distinct_key)
    if presence is None:
        return False, (
            f"{label}.{info.distinct_key} was not measured on this label"
        )
    if presence.present_n != presence.total_n:
        return False, (
            f"{label}.{info.distinct_key} is present on only "
            f"{presence.present_n}/{presence.total_n} nodes, so a plain "
            f"count and a DISTINCT count need not agree"
        )
    return True, (
        f"{label}.{info.distinct_key} present on all {presence.total_n} "
        f"nodes and measured unique"
    )


def canonicalize_count(
    snapshot: reg.RegistrySnapshot,
    interpretation: str,
    *,
    label: Optional[str],
    traversal_count: int,
) -> CanonicalizationResult:
    """Canonicalise `count` / `count_distinct` where provably equivalent.

    `traversal_count` is the number of relationship hops the computation
    made. Any hop at all can multiply rows, so a single traversal is enough
    to refuse the proof — this is intentionally strict, because the failure
    it guards against (silently equating an inflated count with a distinct
    one) is exactly the defect class the architecture exists to catch.
    """
    norm = (interpretation or "").strip().lower()
    if norm not in ("count", "count_distinct"):
        return CanonicalizationResult(
            original=interpretation, canonical=interpretation, applied=False,
            basis="only count/count_distinct participate in this equivalence",
        )

    if traversal_count > 0:
        return CanonicalizationResult(
            original=interpretation, canonical=interpretation, applied=False,
            basis=(
                f"the computation traverses {traversal_count} relationship(s), "
                f"which can multiply rows; count and count_distinct are not "
                f"interchangeable here"
            ),
        )

    if not label:
        return CanonicalizationResult(
            original=interpretation, canonical=interpretation, applied=False,
            basis="no subject label recorded, so uniqueness cannot be checked",
        )

    unique, basis = distinct_key_is_provably_unique(snapshot, label)
    if not unique:
        return CanonicalizationResult(
            original=interpretation, canonical=interpretation, applied=False,
            basis=basis,
        )

    return CanonicalizationResult(
        original=interpretation, canonical=DISTINCT_ENTITY_COUNT, applied=True,
        basis=(
            f"no traversal (one row per node) and {basis}; count and "
            f"count_distinct are provably equal for this population"
        ),
    )


def canonical_comparable_key(
    snapshot: reg.RegistrySnapshot,
    *,
    interpretation: str,
    grain: Optional[str],
    grouping: tuple[str, ...],
    label: Optional[str],
    traversal_count: int,
) -> tuple[tuple, CanonicalizationResult]:
    """The comparability key, with count/count_distinct canonicalised.

    Grain and grouping pass through UNCHANGED. Only the metric spelling is
    normalised, and only when proven — so two routes that disagree about
    what a unit IS still compare as different, which is the behaviour
    `malkhana_records` (RECORD vs ENTITY) must keep.
    """
    result = canonicalize_count(
        snapshot, interpretation, label=label, traversal_count=traversal_count
    )
    metric = result.canonical if result.applied else interpretation
    return (metric, grain, grouping), result
