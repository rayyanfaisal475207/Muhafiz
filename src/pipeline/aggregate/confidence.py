# -*- coding: utf-8 -*-
"""
Phase 7 — confidence as structured EVIDENCE, and normalization with a scale.

TWO THINGS LIVE HERE, AND THE ORDER BETWEEN THEM IS THE POINT.

`ResultConfidenceEvidence` records measurable facts about how a result was
produced. `normalize()` converts comparable values onto a common scale.
Neither may run before semantic comparability has been established — a
confident number answering a different question is still the wrong answer,
and scaling it to 0..1 does not make it comparable. `consensus.py` enforces
that ordering; this module only supplies the ingredients.

EVERY DIMENSION IS A MEASURED FACT. Each field below is derived from
something the runtime actually recorded: coverage reports, receipts,
execution status, constraint application. There is deliberately no
dimension for how assured the model sounded — an LLM's stated confidence is
not evidence, and giving it a slot would let prose outvote measurement.

NO SCALAR SCORE IS COMPUTED HERE, AND THAT IS DELIBERATE. Collapsing these
dimensions into one number requires weights, and no approved weighting rule
exists for this project. Inventing `structured = 1.0, AGE = 0.8` would bury
a policy decision inside an implementation detail, where nobody would ever
review it. `combine_weights()` is therefore a named, testable boundary that
raises until a policy is supplied — see `consensus.py`.
"""
from __future__ import annotations

import dataclasses
import enum
from typing import Any, Optional


class Completeness(str, enum.Enum):
    """Whether every requested constraint was actually applied."""

    COMPLETE = "COMPLETE"          # all constraints applied
    INCOMPLETE = "INCOMPLETE"      # at least one not applied -> not combinable
    UNKNOWN = "UNKNOWN"            # not recorded


@dataclasses.dataclass(frozen=True)
class ResultConfidenceEvidence:
    """Measurable facts about one result's reliability.

    Every field is populated from runtime evidence or left None. None means
    "not measured", never "assume good" — a consumer must treat an absent
    dimension as absent, not as a pass.
    """

    route: str

    #: Which source computed it, and whether that source is authoritative
    #: for the fields involved (from `authority.AuthorityDecision`).
    source: Optional[str] = None
    source_is_authoritative: Optional[bool] = None

    #: From CoverageReport: how much of the intended population was seen.
    observed_population_n: Optional[int] = None
    expected_population_n: Optional[int] = None
    coverage_verdict: Optional[str] = None

    #: From the composite executor: constraint application.
    constraint_completeness: Completeness = Completeness.UNKNOWN
    applied_constraints: tuple[str, ...] = ()
    unapplied_constraints: tuple[str, ...] = ()

    #: Semantic certainty.
    grain: Optional[str] = None
    grain_declared: bool = False
    population_definition: Optional[str] = None
    denominator_n: Optional[float] = None
    denominator_definition: Optional[str] = None

    #: Execution quality.
    execution_status: Optional[str] = None
    row_count: Optional[int] = None
    guard_findings: tuple[str, ...] = ()

    #: Data completeness: nulls and tombstones excluded from the answer.
    excluded_null_n: Optional[int] = None
    excluded_merged_n: Optional[int] = None
    excluded_superseded_n: Optional[int] = None

    @property
    def coverage_rate(self) -> Optional[float]:
        if not self.expected_population_n:
            return None
        return self.observed_population_n / self.expected_population_n

    @property
    def combinable(self) -> bool:
        """Hard gate: facts that disqualify a result from combination.

        This is NOT a quality score. It answers only "is there a measured
        reason this result must not be combined at all" — an incomplete
        constraint set or a failed execution. A result passing this gate
        may still lose on comparability, which is checked first elsewhere.
        """
        if self.constraint_completeness is Completeness.INCOMPLETE:
            return False
        if self.execution_status not in (None, "ok"):
            return False
        return True

    def describe(self) -> str:
        bits = [f"route={self.route}"]
        if self.source:
            bits.append(f"source={self.source}")
        if self.constraint_completeness is not Completeness.UNKNOWN:
            bits.append(f"constraints={self.constraint_completeness.value}")
        if self.unapplied_constraints:
            bits.append(f"unapplied={list(self.unapplied_constraints)}")
        if self.coverage_verdict:
            bits.append(f"coverage={self.coverage_verdict}")
        return ", ".join(bits)


def evidence_from_route_result(
    route: str,
    result: Any,
    *,
    composite: Any = None,
) -> ResultConfidenceEvidence:
    """Build an evidence vector from a RouteResult (and composite, if any).

    Reads only what the runtime already recorded. Anything absent stays
    None rather than being inferred.
    """
    coverage = getattr(result, "coverage", None)
    numeric = getattr(result, "numeric", None)
    provenance = getattr(result, "provenance", {}) or {}
    meta = getattr(result, "execution_metadata", {}) or {}

    completeness = Completeness.UNKNOWN
    applied: tuple[str, ...] = ()
    unapplied: tuple[str, ...] = ()
    if composite is not None:
        applied = composite.applied_constraints
        unapplied = composite.unapplied_constraints
        completeness = (
            Completeness.INCOMPLETE if unapplied else Completeness.COMPLETE
        )

    return ResultConfidenceEvidence(
        route=route,
        source=provenance.get("executed_against") or getattr(coverage, "source", None),
        observed_population_n=getattr(coverage, "observed_population_n", None),
        expected_population_n=getattr(coverage, "expected_population_n", None),
        coverage_verdict=getattr(result, "coverage_verdict", None)
        or getattr(coverage, "verdict", None),
        constraint_completeness=completeness,
        applied_constraints=applied,
        unapplied_constraints=unapplied,
        grain=getattr(numeric, "grain", None),
        grain_declared=bool(getattr(numeric, "grain", None)),
        population_definition=getattr(numeric, "population", None),
        denominator_n=getattr(coverage, "effective_denominator_n", None),
        denominator_definition=getattr(
            coverage, "effective_denominator_definition", None
        ),
        execution_status="ok" if getattr(result, "ok", False) else
        getattr(result, "status", None),
        row_count=meta.get("row_count"),
        guard_findings=tuple(provenance.get("guard_violations") or ()),
        excluded_null_n=getattr(coverage, "excluded_null_n", None),
        excluded_merged_n=getattr(coverage, "excluded_merged_n", None),
        excluded_superseded_n=getattr(coverage, "excluded_superseded_n", None),
    )


# ══════════════════════════════════════════════════════════════════════
# NUMERIC NORMALIZATION
# ══════════════════════════════════════════════════════════════════════

class NormalizationError(ValueError):
    """The metric has no legitimate common scale. Do not combine."""


@dataclasses.dataclass(frozen=True)
class NormalizedValue:
    """A value expressed on a bounded scale, with the original preserved.

    `original` and `unit` are what a person is shown. `normalized` exists
    only so two comparable results can be combined; it is never the
    user-facing figure.
    """

    original: Any
    unit: str
    normalized: float
    basis: str


def normalize(
    value: Any,
    interpretation: str,
    *,
    denominator: Optional[float] = None,
) -> NormalizedValue:
    """Put a comparable value on the 0..1 scale, or refuse.

    TYPED PER METRIC — there is no blind formula. A percentage divides by
    100 because a percentage IS a proportion; a count divides by its
    population because a count out of a known denominator is a proportion.
    An average age has no such denominator: dividing 31 by 100 would invent
    a scale nobody defined, so it raises instead.
    """
    norm = (interpretation or "").strip().lower()

    if norm == "percentage":
        v = float(value)
        return NormalizedValue(
            original=value, unit="percent", normalized=v / 100.0,
            basis="a percentage is a proportion; divided by 100",
        )

    if norm in ("ratio", "proportion"):
        v = float(value)
        return NormalizedValue(
            original=value, unit="ratio", normalized=v,
            basis="already expressed as a proportion",
        )

    if norm in ("count", "count_distinct", "distinct_entity_count"):
        if not denominator:
            raise NormalizationError(
                f"a {norm} has no common scale without a denominator; "
                f"supply the population both results are counted out of, or "
                f"do not combine"
            )
        v = float(value)
        return NormalizedValue(
            original=value, unit="count", normalized=v / float(denominator),
            basis=(
                f"count expressed as a proportion of the shared population "
                f"({denominator:g})"
            ),
        )

    raise NormalizationError(
        f"no approved normalization rule for metric {interpretation!r}; "
        f"combining it would require inventing a scale"
    )


class ConsensusPolicyRequired(RuntimeError):
    """A scalar confidence weighting is needed but none is approved.

    Raised by `combine_weights()` so the gap is loud and testable rather
    than papered over with invented constants. See `consensus.py`.
    """


def combine_weights(*_evidence: ResultConfidenceEvidence) -> dict[str, float]:
    """The policy boundary. Deliberately unimplemented.

    Turning the evidence dimensions above into per-route weights requires
    choosing fixed route weights, dimension weights, or minimum thresholds.
    None of those is approved for this project, so this raises rather than
    guessing — SUPERVISOR DECISION REQUIRED.
    """
    raise ConsensusPolicyRequired(
        "confidence-weighted combination requires an approved weighting "
        "policy (fixed per-route weights, dimension weights, or minimum "
        "thresholds). None is defined; refusing to invent one."
    )
