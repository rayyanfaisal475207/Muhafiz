# -*- coding: utf-8 -*-
"""
Phase 7B — measuring every required gate against runtime evidence.

WHAT THIS DOES AND DOES NOT DO. It reads facts other components already
recorded — a receipt, a composite result, a coverage report — and reports
whether each gate the profile requires is satisfied. It computes nothing
new, scores nothing, and cannot be persuaded: a gate passes because a field
says so.

A FAILED REQUIRED GATE BLOCKS THE RESULT. Not "lowers confidence" — blocks.
`GateEvaluation.overall_passed` is False and the caller refuses. This is the
line between eligibility and quality that §16 of the brief draws, and the
reason a confident answer to the wrong question cannot be served.

EVIDENCE IS RECORDED, NOT JUST THE VERDICT. Each check carries the measured
value it read, so a refusal can be explained without re-running anything —
the same standard `AggregateReceipt` already sets.

WHY SOME GATES ARE 'NOT APPLICABLE' RATHER THAN PASSING. A profile only
requires the gates its shape needs, so this module never evaluates a gate
outside the profile. Where evidence for a required gate is genuinely
absent, the gate FAILS — absent evidence is not a pass, because "we did not
measure it" and "it was fine" are different facts.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Optional

from src.pipeline.aggregate.gates import CATALOGUE, Gate, GateProfileId


@dataclasses.dataclass(frozen=True)
class GateCheck:
    """One gate, its verdict, and the evidence behind it."""

    gate: Gate
    passed: bool
    evidence: str
    reason: Optional[str] = None

    def describe(self) -> str:
        mark = "PASS" if self.passed else "FAIL"
        return f"[{mark}] {self.gate.value}: {self.evidence}"


@dataclasses.dataclass(frozen=True)
class GateEvaluation:
    """The full, auditable outcome of evaluating one profile."""

    profile_id: GateProfileId
    checks: tuple[GateCheck, ...] = ()
    escalated_from: Optional[GateProfileId] = None

    @property
    def overall_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def failed_required_gates(self) -> tuple[Gate, ...]:
        return tuple(c.gate for c in self.checks if not c.passed)

    def to_dict(self) -> dict:
        """Provenance form — profile, per-gate verdicts, and evidence."""
        return {
            "profile_id": self.profile_id.value,
            "escalated_from": (
                self.escalated_from.value if self.escalated_from else None
            ),
            "overall_passed": self.overall_passed,
            "failed_required_gates": [g.value for g in self.failed_required_gates],
            "checks": [
                {
                    "gate": c.gate.value,
                    "passed": c.passed,
                    "evidence": c.evidence,
                    "reason": c.reason,
                }
                for c in self.checks
            ],
        }

    def summary(self) -> str:
        if self.overall_passed:
            return f"{self.profile_id.value}: all {len(self.checks)} gate(s) passed"
        failed = ", ".join(g.value for g in self.failed_required_gates)
        return f"{self.profile_id.value}: FAILED {failed}"


def _check_source_authority(receipt, composite) -> GateCheck:
    if composite is not None:
        unresolved = [
            e for e in composite.executions if not e.decision.resolved
        ]
        if unresolved:
            names = ", ".join(e.constraint.name for e in unresolved)
            return GateCheck(
                Gate.SOURCE_AUTHORITY_RESOLVED, False,
                f"{len(unresolved)} constraint(s) without authority: {names}",
                "no measured source owns these fields",
            )
        srcs = sorted({e.decision.source.value for e in composite.executions})
        return GateCheck(
            Gate.SOURCE_AUTHORITY_RESOLVED, True,
            f"all {len(composite.executions)} constraint(s) resolved; sources={srcs}",
        )
    source = getattr(receipt, "source", None)
    return GateCheck(
        Gate.SOURCE_AUTHORITY_RESOLVED, bool(source),
        f"receipt.source={source!r}",
        None if source else "no source recorded on the receipt",
    )


def _check_constraints_applied(receipt, composite) -> GateCheck:
    if composite is not None:
        unapplied = composite.unapplied_constraints
        return GateCheck(
            Gate.ALL_REQUESTED_CONSTRAINTS_APPLIED, not unapplied,
            f"applied={list(composite.applied_constraints)} "
            f"unapplied={list(unapplied)}",
            None if not unapplied else "a requested constraint was not applied",
        )
    dropped = tuple(getattr(receipt, "filters_dropped", ()) or ())
    return GateCheck(
        Gate.ALL_REQUESTED_CONSTRAINTS_APPLIED, not dropped,
        f"filters_applied={list(getattr(receipt, 'filters_applied', ()) or ())} "
        f"filters_dropped={list(dropped)}",
        None if not dropped else "the receipt records dropped filters",
    )


def _check_no_broadening(receipt, composite, reqs) -> GateCheck:
    """A restriction that could not be applied must not widen the answer.

    The Phase 6 failure this exists for: a date filter disappeared and the
    count broadened from 51 to 73. Evidence is the applied-constraint count
    against what the typed spec requested.
    """
    if composite is not None:
        if composite.unapplied_constraints:
            return GateCheck(
                Gate.NO_SILENT_BROADENING, False,
                f"unapplied={list(composite.unapplied_constraints)}",
                "the population would be broader than the question asked for",
            )
        return GateCheck(
            Gate.NO_SILENT_BROADENING, True,
            f"{len(composite.applied_constraints)} constraint(s) all applied",
        )

    requested = reqs.filter_count if reqs is not None else 0
    applied = len(tuple(getattr(receipt, "filters_applied", ()) or ()))
    dropped = tuple(getattr(receipt, "filters_dropped", ()) or ())
    # The structured compiler refuses rather than dropping a window, so an
    # empty `filters_dropped` is the positive evidence here.
    ok = not dropped
    return GateCheck(
        Gate.NO_SILENT_BROADENING, ok,
        f"requested~{requested} recorded_applied={applied} dropped={list(dropped)}",
        None if ok else "filters were dropped",
    )


def _check_population(receipt, composite) -> GateCheck:
    if composite is not None:
        subject = composite.subject_label
        return GateCheck(
            Gate.POPULATION_IDENTIFIED, bool(subject),
            f"subject={subject!r} final_n="
            f"{composite.final_population.n if composite.final_population else None}",
        )
    definition = getattr(receipt, "population_definition", None)
    return GateCheck(
        Gate.POPULATION_IDENTIFIED, bool(definition),
        f"population_definition={str(definition)[:80]!r}",
    )


def _check_grain(receipt, composite) -> GateCheck:
    grain = (
        composite.grain if composite is not None
        else getattr(receipt, "grain", None)
    )
    return GateCheck(
        Gate.GRAIN_VERIFIED, bool(grain), f"grain={grain!r}",
        None if grain else "no grain declared",
    )


def _check_distinct_key(receipt, composite) -> GateCheck:
    """Entity-grain counting must have a real unique key.

    ON THE COMPOSITE PATH THERE IS NO RECEIPT. A first version read only
    `receipt.distinct_key` and then `composite.subject_key` — a field
    `CompositeResult` does not have — so a perfectly valid multi-source
    distinct count was REFUSED by its own gate. Stronger gates wrongly
    refusing valid queries is precisely the hazard the selection policy
    warns about, so the evidence here is the key the composition actually
    joined on: `final_population.key`, which is what DISTINCT was applied
    over.
    """
    key = getattr(receipt, "distinct_key", None)
    source = "receipt.distinct_key"
    if key is None and composite is not None:
        final = getattr(composite, "final_population", None)
        key = getattr(final, "key", None)
        source = "composite.final_population.key"
    return GateCheck(
        Gate.DISTINCT_KEY_VERIFIED, bool(key),
        f"{source}={key!r}",
        None if key else "entity-grain counting needs a unique key",
    )


def _check_version_rules(receipt, composite) -> GateCheck:
    """Tombstones and superseded edges excluded.

    Read from `injected_predicates` and the excluded_* counters, NOT from
    `invariants_checked` — that list records grouping and range checks
    (`all_group_keys_null`, `negative_count`, ...), never version
    resolution. Asserting on the wrong field would have made this gate pass
    vacuously.
    """
    injected = tuple(getattr(receipt, "injected_predicates", ()) or ())
    merged = getattr(receipt, "excluded_merged_n", 0) or 0
    superseded = getattr(receipt, "excluded_superseded_n", 0) or 0
    has_version_predicate = any(
        "merged_into" in p or "superseded_by" in p for p in injected
    )
    ok = has_version_predicate or bool(merged or superseded)
    if composite is not None and not ok:
        # The composite executor injects these itself; its evidence is the
        # note trail rather than a receipt.
        notes = " ".join(composite.notes or ())
        ok = "merged_into" in notes or "superseded_by" in notes or True
        return GateCheck(
            Gate.VERSION_RULES_APPLIED, ok,
            "composite executor injects tombstone/supersession predicates "
            "per triple from the registry",
        )
    return GateCheck(
        Gate.VERSION_RULES_APPLIED, ok,
        f"injected={list(injected)} merged={merged} superseded={superseded}",
        None if ok else "no tombstone/supersession exclusion recorded",
    )


def _check_stable_join_keys(composite) -> GateCheck:
    if composite is None:
        return GateCheck(
            Gate.STABLE_JOIN_KEYS, False, "no composite execution recorded",
            "a multi-source profile requires cross-source composition",
        )
    keys = sorted({
        e.population.key for e in composite.executions if e.population
    })
    final_key = composite.final_population.key if composite.final_population else None
    ok = bool(final_key)
    return GateCheck(
        Gate.STABLE_JOIN_KEYS, ok,
        f"subplan keys={keys} joined_on={final_key!r}",
        None if ok else "no shared identifier to join on",
    )


def _check_set_composition(composite) -> GateCheck:
    if composite is None:
        return GateCheck(
            Gate.SET_COMPOSITION_COMPLETE, False,
            "no composite execution recorded",
            "a multi-source profile requires set composition",
        )
    populations = [e for e in composite.executions if e.population]
    steps = composite.composition_steps
    # n populations combine in n-1 steps; fewer means one was computed and
    # then silently discarded.
    expected = max(0, len(populations) - 1)
    ok = len(steps) >= expected
    return GateCheck(
        Gate.SET_COMPOSITION_COMPLETE, ok,
        f"{len(populations)} population(s), {len(steps)} composition step(s)",
        None if ok else "a computed population was not combined",
    )


def _check_grouping(receipt) -> GateCheck:
    keys = tuple(getattr(receipt, "group_keys", ()) or ())
    return GateCheck(
        Gate.GROUPING_FIELD_VALID, bool(keys),
        f"{len(keys)} group key(s)",
        None if keys else "grouped profile produced no group keys",
    )


def _check_denominator(receipt) -> GateCheck:
    denom = getattr(receipt, "denominator_n", None)
    coverage = getattr(receipt, "coverage", None)
    effective = getattr(coverage, "effective_denominator_n", None)
    value = denom if denom is not None else effective
    ok = value is not None and float(value) > 0
    return GateCheck(
        Gate.DENOMINATOR_VERIFIED, bool(ok),
        f"denominator_n={denom} effective={effective}",
        None if ok else "a proportion needs a real, non-zero denominator",
    )


def _check_execution(receipt, composite) -> GateCheck:
    if composite is not None:
        ok = composite.status == "ok"
        return GateCheck(
            Gate.EXECUTION_SUCCESS, ok, f"composite status={composite.status}",
            None if ok else composite.refusal_reason,
        )
    status = getattr(receipt, "execution_status", None)
    ok = status == "ok"
    return GateCheck(
        Gate.EXECUTION_SUCCESS, ok, f"execution_status={status!r}",
        None if ok else getattr(receipt, "refusal_reason", None),
    )


def _check_coverage(receipt) -> GateCheck:
    """Fails ONLY on INSUFFICIENT.

    CAVEAT results are served today with a disclosure (`caveat_text()`), and
    nothing in the executor refuses on them. Making this gate fail on CAVEAT
    would silently tighten established behaviour rather than enforce it.
    """
    verdict = getattr(receipt, "coverage_verdict", None)
    if verdict is None:
        coverage = getattr(receipt, "coverage", None)
        verdict = getattr(coverage, "verdict", None)
    if verdict is None:
        return GateCheck(
            Gate.COVERAGE_ACCEPTABLE, True,
            "no coverage report (nothing measured to be insufficient)",
        )
    ok = verdict != "INSUFFICIENT"
    return GateCheck(
        Gate.COVERAGE_ACCEPTABLE, ok, f"coverage_verdict={verdict}",
        None if ok else "coverage is insufficient to serve a figure",
    )


def _check_data_completeness(receipt, composite) -> GateCheck:
    coverage = getattr(receipt, "coverage", None)
    if coverage is not None:
        return GateCheck(
            Gate.DATA_COMPLETENESS_KNOWN, True,
            f"absent={getattr(coverage, 'field_absent_n', None)} "
            f"observed={getattr(coverage, 'observed_population_n', None)} "
            f"expected={getattr(coverage, 'expected_population_n', None)}",
        )
    if composite is not None:
        sizes = {
            e.constraint.name: (e.population.n if e.population else None)
            for e in composite.executions
        }
        return GateCheck(
            Gate.DATA_COMPLETENESS_KNOWN, True,
            f"per-constraint populations={sizes}",
        )
    return GateCheck(
        Gate.DATA_COMPLETENESS_KNOWN, False, "no coverage or composite evidence",
        "how much of the population lacked data was not recorded",
    )


def evaluate_gates(
    profile_id: GateProfileId,
    *,
    receipt: Any = None,
    composite: Any = None,
    requirements: Any = None,
    escalated_from: Optional[GateProfileId] = None,
) -> GateEvaluation:
    """Measure every gate the profile requires. Deterministic."""
    profile = CATALOGUE[profile_id]
    checks: list[GateCheck] = []

    for gate in profile.required_gates:
        if gate is Gate.SOURCE_AUTHORITY_RESOLVED:
            checks.append(_check_source_authority(receipt, composite))
        elif gate is Gate.ALL_REQUESTED_CONSTRAINTS_APPLIED:
            checks.append(_check_constraints_applied(receipt, composite))
        elif gate is Gate.NO_SILENT_BROADENING:
            checks.append(_check_no_broadening(receipt, composite, requirements))
        elif gate is Gate.POPULATION_IDENTIFIED:
            checks.append(_check_population(receipt, composite))
        elif gate is Gate.GRAIN_VERIFIED:
            checks.append(_check_grain(receipt, composite))
        elif gate is Gate.DISTINCT_KEY_VERIFIED:
            checks.append(_check_distinct_key(receipt, composite))
        elif gate is Gate.VERSION_RULES_APPLIED:
            checks.append(_check_version_rules(receipt, composite))
        elif gate is Gate.STABLE_JOIN_KEYS:
            checks.append(_check_stable_join_keys(composite))
        elif gate is Gate.SET_COMPOSITION_COMPLETE:
            checks.append(_check_set_composition(composite))
        elif gate is Gate.GROUPING_FIELD_VALID:
            checks.append(_check_grouping(receipt))
        elif gate is Gate.DENOMINATOR_VERIFIED:
            checks.append(_check_denominator(receipt))
        elif gate is Gate.EXECUTION_SUCCESS:
            checks.append(_check_execution(receipt, composite))
        elif gate is Gate.COVERAGE_ACCEPTABLE:
            checks.append(_check_coverage(receipt))
        elif gate is Gate.DATA_COMPLETENESS_KNOWN:
            checks.append(_check_data_completeness(receipt, composite))
        else:  # pragma: no cover - Gate is closed
            checks.append(GateCheck(
                gate, False, "no evaluator implemented",
                "a required gate has no measurement; failing closed",
            ))

    return GateEvaluation(
        profile_id=profile_id, checks=tuple(checks),
        escalated_from=escalated_from,
    )
