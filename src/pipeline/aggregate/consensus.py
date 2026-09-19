# -*- coding: utf-8 -*-
"""
Phase 7 — the consensus gate: comparability, then confidence, then scale.

THE ORDER IS THE ARCHITECTURE. Three gates, and each may only run once the
previous has passed:

    1. COMPARABILITY — did the two routes answer the same question?
    2. CONFIDENCE    — is there a measured reason not to combine either?
    3. NORMALIZATION — is there a legitimate common scale?

Only then may values be combined. Reversing any pair of these produces a
specific, known failure: normalising first lets two different questions be
averaged because both happen to be numbers; weighting first lets a
confident answer to the wrong question outvote a correct one.

WHAT THIS MODULE REFUSES TO DO. It does not compute a scalar confidence
score, because that needs weights nobody has approved. `evaluate()` reports
that the pair is combination-ELIGIBLE and stops there, carrying the
evidence and the normalized values so a future approved policy can be
dropped in at one named place. Inventing `structured=1.0, AGE=0.8` here
would bury a policy decision where no one would review it — and would be
indistinguishable, in the output, from a rule someone had actually agreed.

WHAT NORMALIZATION MAY NEVER DO. It may not make different questions
comparable. `ASSIGNED_TO` vs `BELONGS_TO_CASE`, all-cases vs 2026-cases,
raw records vs distinct persons — those stay different populations, and no
0..1 scaling or confidence weighting overrides that. The comparability gate
runs first precisely so that scaling never gets the chance.
"""
from __future__ import annotations

import dataclasses
import enum
from typing import Any, Optional

from src.pipeline.aggregate import canonical as can
from src.pipeline.aggregate import confidence as conf
from src.pipeline.aggregate import registry as reg


class Eligibility(str, enum.Enum):
    """How far a pair of results got through the gates."""

    #: Different questions. Never normalised, never combined.
    NOT_COMPARABLE = "NOT_COMPARABLE"
    #: Same question, but a measured fact disqualifies one side.
    NOT_COMBINABLE = "NOT_COMBINABLE"
    #: Comparable and admissible, but the metric has no common scale.
    NO_COMMON_SCALE = "NO_COMMON_SCALE"
    #: All gates passed. Combination is permitted once a policy exists.
    ELIGIBLE = "ELIGIBLE"


@dataclasses.dataclass(frozen=True)
class ConsensusAssessment:
    """The outcome of the three gates, with the evidence behind it."""

    eligibility: Eligibility
    reason: str

    left_route: str = ""
    right_route: str = ""

    left_key: Optional[tuple] = None
    right_key: Optional[tuple] = None
    canonicalizations: tuple[can.CanonicalizationResult, ...] = ()

    left_evidence: Optional[conf.ResultConfidenceEvidence] = None
    right_evidence: Optional[conf.ResultConfidenceEvidence] = None

    left_normalized: Optional[conf.NormalizedValue] = None
    right_normalized: Optional[conf.NormalizedValue] = None

    #: True only when a scalar weighting policy would be the ONLY remaining
    #: step. Makes the policy gap visible in output rather than implicit.
    awaiting_policy: bool = False

    @property
    def combinable(self) -> bool:
        return self.eligibility is Eligibility.ELIGIBLE

    def describe(self) -> str:
        return f"{self.eligibility.value}: {self.reason}"


def _subject_of(result: Any) -> Optional[str]:
    """The label a result counted, for the canonicalization proof."""
    prov = getattr(result, "provenance", {}) or {}
    plan = prov.get("plan") or {}
    if plan.get("root_label"):
        return plan["root_label"]
    labels = prov.get("chosen_labels") or []
    return labels[0] if len(labels) == 1 else None


def _traversals_of(result: Any) -> int:
    """How many relationship hops the computation made.

    Any hop can multiply rows, which is what makes count and
    count_distinct non-interchangeable — so this drives the proof in
    `canonical.py`.
    """
    prov = getattr(result, "provenance", {}) or {}
    plan = prov.get("plan") or {}
    if "patterns" in plan:
        return len(plan.get("patterns") or ())
    rels = prov.get("chosen_relationships")
    if rels is not None:
        return len(rels)
    return 0


def evaluate(
    snapshot: reg.RegistrySnapshot,
    left: Any,
    right: Any,
    *,
    left_route: str = "structured",
    right_route: str = "age",
    shared_denominator: Optional[float] = None,
    left_composite: Any = None,
    right_composite: Any = None,
) -> ConsensusAssessment:
    """Run the three gates in order and report how far the pair got."""
    l_num = getattr(left, "numeric", None)
    r_num = getattr(right, "numeric", None)
    if l_num is None or r_num is None:
        return ConsensusAssessment(
            Eligibility.NOT_COMPARABLE,
            "one side made no numeric claim, so there is nothing to compare",
            left_route, right_route,
        )

    # ── GATE 1: comparability, with provable canonicalization ─────────
    l_key, l_canon = can.canonical_comparable_key(
        snapshot,
        interpretation=l_num.interpretation, grain=l_num.grain,
        grouping=l_num.grouping, label=_subject_of(left),
        traversal_count=_traversals_of(left),
    )
    r_key, r_canon = can.canonical_comparable_key(
        snapshot,
        interpretation=r_num.interpretation, grain=r_num.grain,
        grouping=r_num.grouping, label=_subject_of(right),
        traversal_count=_traversals_of(right),
    )
    canons = (l_canon, r_canon)

    if l_key != r_key:
        return ConsensusAssessment(
            Eligibility.NOT_COMPARABLE,
            (
                f"the routes answered different questions: {l_key} vs {r_key}. "
                f"No normalization or weighting may bridge this."
            ),
            left_route, right_route, l_key, r_key, canons,
        )

    # ── GATE 2: confidence evidence as a hard admissibility check ─────
    l_ev = conf.evidence_from_route_result(
        left_route, left, composite=left_composite
    )
    r_ev = conf.evidence_from_route_result(
        right_route, right, composite=right_composite
    )
    for ev in (l_ev, r_ev):
        if not ev.combinable:
            return ConsensusAssessment(
                Eligibility.NOT_COMBINABLE,
                (
                    f"{ev.route} is not admissible: {ev.describe()}. A result "
                    f"missing a requested constraint answers a narrower or "
                    f"broader question and must not be combined."
                ),
                left_route, right_route, l_key, r_key, canons, l_ev, r_ev,
            )

    # ── GATE 3: legitimate common scale ───────────────────────────────
    metric = l_key[0]
    denom = shared_denominator
    if denom is None:
        denom = l_ev.denominator_n or r_ev.denominator_n
    try:
        l_norm = conf.normalize(l_num.value, metric, denominator=denom)
        r_norm = conf.normalize(r_num.value, metric, denominator=denom)
    except conf.NormalizationError as exc:
        return ConsensusAssessment(
            Eligibility.NO_COMMON_SCALE, str(exc),
            left_route, right_route, l_key, r_key, canons, l_ev, r_ev,
        )

    return ConsensusAssessment(
        Eligibility.ELIGIBLE,
        (
            "comparable, admissible and normalizable; combination awaits an "
            "approved confidence-weighting policy"
        ),
        left_route, right_route, l_key, r_key, canons, l_ev, r_ev,
        l_norm, r_norm, awaiting_policy=True,
    )


def combine(assessment: ConsensusAssessment) -> float:
    """Confidence-weighted combination. Deliberately unimplemented.

    Reaching here means all three gates passed and only the weighting
    policy is missing. It raises rather than falling back to an unweighted
    mean: an arithmetic average IS a weighting policy (equal weights), and
    adopting it silently would be exactly the unreviewed decision this
    boundary exists to prevent.
    """
    if not assessment.combinable:
        raise conf.ConsensusPolicyRequired(
            f"cannot combine: {assessment.describe()}"
        )
    raise conf.ConsensusPolicyRequired(
        "SUPERVISOR DECISION REQUIRED — the pair is eligible for "
        "confidence-weighted consensus, but no weighting policy (fixed "
        "per-route weights, dimension weights, or minimum thresholds) has "
        "been approved. Unweighted averaging is not a neutral default."
    )
