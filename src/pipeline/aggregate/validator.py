# -*- coding: utf-8 -*-
"""
Phase 2 — the deterministic validator: reject before compiling.

THE JOB. Everything upstream of here may be wrong: an LLM filled the spec,
and an LLM will occasionally name a field that does not exist, ask for a
grain that cannot be computed, or quietly widen a scope. Nothing downstream
of here is allowed to be wrong: the compiler emits query text, and query
text that runs is an answer somebody will read. So this module is the one
place where "I cannot do that" is decided, and it decides it with measured
registry facts rather than with judgement.

WHY REJECTION IS THE POINT. The production engine's failure mode is that it
always answers — a question it does not recognise falls through to a
catch-all and returns a grand total. This validator inverts that: a spec
that is not provably computable is refused, by name, with the reason. A
refusal is a correct outcome here, not a failure of the system.

THE THREE OUTCOMES, and why not two:
  EXECUTABLE   — compile it.
  AMBIGUOUS    — the spec is legal but more than one reading of the
                 question is legal too, and they give materially different
                 numbers (4 vs 70). Asking beats guessing.
  UNSUPPORTED  — the data or the algebra cannot express it. Say which.

A single boolean would collapse AMBIGUOUS into UNSUPPORTED and lose the
one case where the user can trivially resolve it.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Literal, Optional

from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate.spec import (
    COUNT_OPS,
    EDGE_GRAINS,
    FILTER_OPS,
    GRAINS,
    IDENTITY_GRAINS,
    MEASURES,
    NUMERIC_MEASURES,
    AggregateSpec,
    FieldPredicate,
    PopulationNode,
    RelationCountPredicate,
    Traversal,
)

logger = logging.getLogger(__name__)


# ══════════════════════════════════════════════════════════════════════
# Limits. Each is a stated policy with a reason, not a round number.
# ══════════════════════════════════════════════════════════════════════

#: Two hops reaches everything the corpus actually models
#: (Case -> PoliceStation -> District is the deepest real path). A third
#: hop multiplies rows against a 73-case corpus faster than it adds
#: meaning, and every extra hop is another fanout to defend.
MAX_RELATION_HOPS = 2

#: population depth = hops + 1.
MAX_TREE_DEPTH = 4

#: Two dimensions is a cross-tab. A third produces cells of size 0-1 over
#: 73 cases, which is noise presented as analysis.
MAX_GROUP_DIMENSIONS = 2

#: A grouping field absent on more than this share of the population makes
#: the grouping misleading: the absent rows form a silent NULL bucket that
#: looks like a real category. `investigation_status` is present on 21 of
#: 73 cases (71% absent), which is exactly the case this bound refuses.
#: Chosen to sit below that measured value; see coverage.py for the
#: matching caveat/refuse thresholds on non-grouping fields.
MIN_GROUPING_FIELD_PRESENCE = 0.50

#: Below this denominator a percentage is arithmetic, not evidence. At 73
#: cases a bucket of 4 is already 5.5%; a bucket of 2 would be 2.7% and
#: indistinguishable from noise.
MIN_RATIO_DENOMINATOR = 5

#: Two readings of one question are "materially different" when they differ
#: by more than this. 4 vs 70 is 1650% apart; a rounding difference is not.
MATERIAL_DIFFERENCE_RATIO = 0.05

Verdict = Literal["EXECUTABLE", "AMBIGUOUS", "UNSUPPORTED"]


@dataclasses.dataclass(frozen=True)
class ValidationIssue:
    """One reason a spec was refused or flagged.

    `code` is stable and machine-readable so the failure matrix can branch
    on it; `message` is for the operator and the receipt. Both are needed:
    the brief requires that unsupported-operation, unsupported-data,
    ambiguity and coverage failures never collapse into one generic error.
    """

    code: str
    message: str
    field: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class ValidationResult:
    verdict: Verdict
    issues: tuple[ValidationIssue, ...] = ()

    @property
    def ok(self) -> bool:
        return self.verdict == "EXECUTABLE"

    def codes(self) -> tuple[str, ...]:
        return tuple(i.code for i in self.issues)


def _issue(code: str, message: str, field: Optional[str] = None) -> ValidationIssue:
    return ValidationIssue(code=code, message=message, field=field)


# ══════════════════════════════════════════════════════════════════════
# Field resolution
# ══════════════════════════════════════════════════════════════════════
def _resolve_field(
    snapshot: reg.RegistrySnapshot, entity: str, field: str
) -> Optional[reg.LogicalField]:
    """Resolve a logical field name for a given entity.

    Accepts both the bare form ("incident_date", a Postgres case field) and
    the qualified form ("Person.age", a graph property). The spec is not
    allowed to name a physical path, so this is the only place a field name
    becomes a source — which is what keeps source selection deterministic
    and out of the LLM's hands.
    """
    direct = snapshot.logical_field(field)
    if direct is not None:
        return direct
    return snapshot.logical_field(f"{entity}.{field}")


# ══════════════════════════════════════════════════════════════════════
# Population validation
# ══════════════════════════════════════════════════════════════════════
def _validate_population(
    snapshot: reg.RegistrySnapshot, pop: PopulationNode, *, label: str
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []

    entity = snapshot.entity(pop.entity)
    if entity is None:
        return [
            _issue(
                "unknown_entity",
                f"{label}: no entity type {pop.entity!r} exists in the data model. "
                f"Known: {', '.join(sorted(snapshot.known_labels()))}.",
                pop.entity,
            )
        ]

    if len(pop.traversals) > MAX_RELATION_HOPS:
        issues.append(
            _issue(
                "too_many_hops",
                f"{label}: {len(pop.traversals)} relationship hops requested; "
                f"at most {MAX_RELATION_HOPS} are supported.",
            )
        )
    if pop.depth > MAX_TREE_DEPTH:
        issues.append(
            _issue("tree_too_deep", f"{label}: population depth {pop.depth} exceeds {MAX_TREE_DEPTH}.")
        )

    # Walk the hops, checking each against a relationship that actually
    # exists. An unobserved traversal is refused rather than compiled into
    # a query that would silently return zero rows — "no such relationship"
    # and "no matching data" are different answers.
    current = pop.entity
    for hop in pop.traversals:
        info = _lookup_traversal(snapshot, current, hop)
        if info is None:
            issues.append(
                _issue(
                    "unknown_relationship",
                    f"{label}: no {hop.rel} relationship observed between "
                    f"{current} and {hop.target} (direction={hop.direction}).",
                    hop.rel,
                )
            )
            break
        if hop.role_field:
            # A role filter on an edge property that the registry has never
            # seen would silently match nothing.
            issues.extend(_validate_edge_role(snapshot, info, hop, label))
        current = hop.target

    for pred in pop.predicates:
        issues.extend(_validate_predicate(snapshot, pop.entity, pred, label))

    return issues


def _lookup_traversal(
    snapshot: reg.RegistrySnapshot, source: str, hop: Traversal
) -> Optional[reg.RelationshipInfo]:
    """Find the registry entry for a hop in the direction it is travelled."""
    if hop.direction == "out":
        return snapshot.relationship(source, hop.rel, hop.target)
    return snapshot.relationship(hop.target, hop.rel, source)


def _validate_edge_role(
    snapshot: reg.RegistrySnapshot,
    info: reg.RelationshipInfo,
    hop: Traversal,
    label: str,
) -> list[ValidationIssue]:
    """Role filters are accepted only on relationships known to carry them.

    Deliberately permissive about the VALUE and strict about the FIELD: the
    registry measures node properties, not edge properties, so this checks
    that the relationship is one of the role-bearing types rather than that
    a particular role string exists. A wrong value returns zero rows, which
    coverage.py reports honestly; a wrong field name would be a silent
    no-op filter, which is the dangerous case.
    """
    if hop.role_field not in _ROLE_BEARING_RELATIONSHIPS.get(info.rel_type, ()):
        return [
            _issue(
                "unknown_edge_property",
                f"{label}: {info.rel_type} edges do not carry a "
                f"{hop.role_field!r} property.",
                hop.role_field,
            )
        ]
    return []


#: Edge properties usable as role filters, by relationship type. Taken from
#: the write sites in `src/graph/structured_projection.py` (INVOLVED_IN is
#: written with {"role": ...}; ASSIGNED_TO with {"role": ...}), not guessed.
_ROLE_BEARING_RELATIONSHIPS: dict[str, tuple[str, ...]] = {
    "INVOLVED_IN": ("role",),
    "ASSIGNED_TO": ("role",),
}


def _validate_predicate(
    snapshot: reg.RegistrySnapshot, entity: str, pred, label: str
) -> list[ValidationIssue]:
    if isinstance(pred, FieldPredicate):
        if pred.op not in FILTER_OPS:
            return [
                _issue(
                    "unsupported_operator",
                    f"{label}: filter operator {pred.op!r} is not supported. "
                    f"Supported: {', '.join(sorted(FILTER_OPS))}.",
                    pred.field,
                )
            ]
        field = _resolve_field(snapshot, entity, pred.field)
        if field is None:
            return [
                _issue(
                    "unknown_field",
                    f"{label}: no field {pred.field!r} is available on {entity}.",
                    pred.field,
                )
            ]
        # A predicate on a field that exists but is never populated cannot
        # discriminate; it would silently return an empty population.
        if field.present_n == 0:
            return [
                _issue(
                    "field_never_populated",
                    f"{label}: field {pred.field!r} exists in the schema but is "
                    f"populated on 0 of {field.total_n} records, so it cannot "
                    f"be filtered on.",
                    pred.field,
                )
            ]
        return []

    if isinstance(pred, RelationCountPredicate):
        issues: list[ValidationIssue] = []
        if pred.op not in COUNT_OPS:
            issues.append(
                _issue(
                    "unsupported_operator",
                    f"{label}: {pred.op!r} cannot be applied to a relationship "
                    f"count. Supported: {', '.join(sorted(COUNT_OPS))}.",
                    pred.rel,
                )
            )
        hop = Traversal(rel=pred.rel, target=pred.target, direction=pred.direction)
        if _lookup_traversal(snapshot, entity, hop) is None:
            issues.append(
                _issue(
                    "unknown_relationship",
                    f"{label}: no {pred.rel} relationship observed between "
                    f"{entity} and {pred.target}.",
                    pred.rel,
                )
            )
        if pred.count_grain not in GRAINS:
            issues.append(
                _issue("unknown_grain", f"{label}: unknown count grain {pred.count_grain!r}.")
            )
        # The 4-vs-70 defence at predicate level: counting related entities
        # at ENTITY grain needs a key to DISTINCT on.
        if pred.count_grain in IDENTITY_GRAINS:
            target = snapshot.entity(pred.target)
            if target is None or target.distinct_key is None:
                issues.append(
                    _issue(
                        "no_distinct_key",
                        f"{label}: {pred.target} has no property that identifies "
                        f"it uniquely, so its related entities cannot be counted "
                        f"at {pred.count_grain} grain.",
                        pred.target,
                    )
                )
        return issues

    return [_issue("unknown_predicate", f"{label}: unrecognised predicate type {type(pred).__name__}.")]


# ══════════════════════════════════════════════════════════════════════
# Grain validation — the core safety rule.
# ══════════════════════════════════════════════════════════════════════
def _validate_grain(
    snapshot: reg.RegistrySnapshot, spec: AggregateSpec
) -> list[ValidationIssue]:
    """Enforce that the spec says what it is counting, and can count it.

    The rules here are the structural form of the 4-vs-70 finding. They are
    not advisory: a spec that cannot state its grain unambiguously is
    refused, because the alternative is emitting a query whose number is
    indistinguishable from a correct one.
    """
    issues: list[ValidationIssue] = []

    if spec.grain not in GRAINS:
        return [
            _issue(
                "unknown_grain",
                f"Grain {spec.grain!r} is not one of {', '.join(sorted(GRAINS))}.",
            )
        ]

    entity = snapshot.entity(spec.population.entity)
    if entity is None:
        return issues  # already reported by population validation

    # WHAT GETS COUNTED IS THE POPULATION'S OWN ENTITY, NOT THE TERMINAL
    # OF ITS TRAVERSALS.
    #
    # A traversal in a PopulationNode is a FILTER on the subject, not a
    # change of subject: `Person -INVOLVED_IN-> Incident (role=accused)`
    # reads "persons who are accused in an incident", and the thing being
    # counted is still the Person. Checking `distinct_key` against the
    # terminal (Incident) instead would demand the wrong key and refuse
    # every correct spec of this shape — which is exactly what the first
    # shadow run did, refusing `count_distinct(Person)` because `entity_id`
    # "does not identify a Case".
    #
    # `compiler._counted_expression()` counts the alias the population's
    # own MATCH established, so this must agree with it. When the two
    # disagree the validator is the one that is wrong, because the compiler
    # is what actually produces the number.
    counted_label = spec.population.entity
    terminal = snapshot.entity(counted_label)

    if spec.grain in IDENTITY_GRAINS:
        if not spec.distinct_key:
            issues.append(
                _issue(
                    "missing_distinct_key",
                    f"{spec.grain} grain requires distinct_key: what identifies "
                    f"one {counted_label}? Without it a fanning traversal would "
                    f"count links, not things.",
                    "distinct_key",
                )
            )
        elif terminal is not None:
            if terminal.distinct_key is None:
                issues.append(
                    _issue(
                        "no_distinct_key",
                        f"{counted_label} has no property present on every node "
                        f"and unique across them, so it cannot be counted at "
                        f"{spec.grain} grain.",
                        counted_label,
                    )
                )
            elif spec.distinct_key != terminal.distinct_key:
                # Counting DISTINCT on the wrong property is the
                # `{'k': None, 'n': 73}` failure in count form.
                issues.append(
                    _issue(
                        "wrong_distinct_key",
                        f"distinct_key {spec.distinct_key!r} does not identify a "
                        f"{counted_label}; the registry measured "
                        f"{terminal.distinct_key!r} as its key.",
                        "distinct_key",
                    )
                )

    if spec.grain == "ROLE_PAIR":
        # ROLE_PAIR's uniqueness is (entity key, role). A spec at this grain
        # whose traversals carry no role field is really asking for
        # RELATIONSHIP grain and should say so.
        if not any(t.role_field for t in spec.population.traversals) and not any(
            isinstance(p, RelationCountPredicate) and p.role_field
            for p in spec.population.predicates
        ):
            issues.append(
                _issue(
                    "role_pair_without_role",
                    "ROLE_PAIR grain requires a role-bearing traversal; with no "
                    "role property this is RELATIONSHIP grain.",
                    "grain",
                )
            )

    # The fanout rule: an identity-grain count downstream of a fanning
    # traversal MUST have a distinct key, or the count is edge-shaped.
    # Measured: Case <-BELONGS_TO_CASE- Person is MANY_TO_MANY, 449 edges
    # over 73 cases; Case <-BELONGS_TO_CASE- Address fans 2,100 over 73.
    if spec.grain in IDENTITY_GRAINS and not spec.distinct_key:
        current = spec.population.entity
        for hop in spec.population.traversals:
            info = _lookup_traversal(snapshot, current, hop)
            # Directional: the same relationship can be safe one way and
            # catastrophic the other. Address-[:BELONGS_TO_CASE]->Case has
            # a forward fanout of 1 and a reverse fanout of 2,100, so
            # reading the forward `cardinality` here would wave through the
            # single worst inflation in this database.
            if info is not None and info.fans_in_direction(hop.direction):
                issues.append(
                    _issue(
                        "fanout_without_distinct",
                        f"Traversal {current}-[{hop.rel}]->{hop.target} "
                        f"(direction={hop.direction}) multiplies rows: up to "
                        f"{info.max_fanout_in_direction(hop.direction)} per "
                        f"{current}. An {spec.grain}-grain count across it "
                        f"without a distinct key would count relationships, "
                        f"not entities.",
                        hop.rel,
                    )
                )
                break
            current = hop.target

    return issues


# ══════════════════════════════════════════════════════════════════════
# Measure, grouping, ratio, scope
# ══════════════════════════════════════════════════════════════════════
def _validate_measure(
    snapshot: reg.RegistrySnapshot, spec: AggregateSpec
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if spec.measure not in MEASURES:
        return [
            _issue(
                "unsupported_operation",
                f"Measure {spec.measure!r} is not supported. Supported: "
                f"{', '.join(sorted(MEASURES))}.",
                "measure",
            )
        ]
    if spec.measure in NUMERIC_MEASURES:
        if not spec.value_field:
            issues.append(
                _issue(
                    "missing_value_field",
                    f"Measure {spec.measure!r} needs value_field: which number is "
                    f"being aggregated?",
                    "value_field",
                )
            )
        else:
            field = _resolve_field(snapshot, spec.population.entity, spec.value_field)
            if field is None:
                issues.append(
                    _issue(
                        "unknown_field",
                        f"No field {spec.value_field!r} available on "
                        f"{spec.population.entity}.",
                        "value_field",
                    )
                )
            elif field.present_n == 0:
                issues.append(
                    _issue(
                        "field_never_populated",
                        f"Field {spec.value_field!r} is populated on 0 of "
                        f"{field.total_n} records; {spec.measure} over it is undefined.",
                        "value_field",
                    )
                )
    if spec.measure == "count_distinct" and not spec.distinct_key:
        issues.append(
            _issue(
                "missing_distinct_key",
                "count_distinct requires distinct_key.",
                "distinct_key",
            )
        )
    return issues


def _validate_grouping(
    snapshot: reg.RegistrySnapshot, spec: AggregateSpec
) -> list[ValidationIssue]:
    issues: list[ValidationIssue] = []
    if len(spec.group_by) > MAX_GROUP_DIMENSIONS:
        issues.append(
            _issue(
                "too_many_group_dimensions",
                f"{len(spec.group_by)} grouping dimensions requested; at most "
                f"{MAX_GROUP_DIMENSIONS} are supported.",
                "group_by",
            )
        )
    for gb in spec.group_by:
        entity = (
            gb.via[-1].target if gb.via else spec.population.entity
        )
        field = _resolve_field(snapshot, entity, gb.field)
        if field is None:
            issues.append(
                _issue(
                    "unknown_field",
                    f"Cannot group by {gb.field!r}: no such field on {entity}.",
                    gb.field,
                )
            )
            continue
        # A sparse grouping field produces a large silent NULL bucket that
        # reads as a real category. investigation_status (21/73) is the
        # measured case this refuses.
        if field.presence_rate < MIN_GROUPING_FIELD_PRESENCE:
            issues.append(
                _issue(
                    "grouping_field_too_sparse",
                    f"Cannot group by {gb.field!r}: present on "
                    f"{field.present_n} of {field.total_n} records "
                    f"({field.presence_rate:.0%}); below the "
                    f"{MIN_GROUPING_FIELD_PRESENCE:.0%} threshold the absent "
                    f"records form a misleading NULL group.",
                    gb.field,
                )
            )
    return issues


def _validate_ratio(
    snapshot: reg.RegistrySnapshot, spec: AggregateSpec
) -> list[ValidationIssue]:
    if spec.ratio is None:
        return []
    issues: list[ValidationIssue] = []
    issues.extend(_validate_population(snapshot, spec.ratio.numerator, label="ratio.numerator"))
    issues.extend(_validate_population(snapshot, spec.ratio.denominator, label="ratio.denominator"))

    # A ratio whose halves count different KINDS of thing is not a ratio.
    # "% of cases with an officer" over "all officers" is a category error
    # that produces a number between 0 and 100 and means nothing.
    num_entity = (
        spec.ratio.numerator.traversals[-1].target
        if spec.ratio.numerator.traversals
        else spec.ratio.numerator.entity
    )
    den_entity = (
        spec.ratio.denominator.traversals[-1].target
        if spec.ratio.denominator.traversals
        else spec.ratio.denominator.entity
    )
    if num_entity != den_entity:
        issues.append(
            _issue(
                "ratio_population_mismatch",
                f"Ratio numerator counts {num_entity} but denominator counts "
                f"{den_entity}; a percentage requires both halves to count the "
                f"same kind of thing.",
                "ratio",
            )
        )
    return issues


def _validate_scope(spec: AggregateSpec) -> list[ValidationIssue]:
    """Scope must come from the caller, never from the question.

    `run_aggregate()` gates cross-case access on role before arming the RLS
    bypass. Carrying that forward means a spec with no caller role is not
    merely incomplete, it is a spec that would run unauthenticated.
    """
    issues: list[ValidationIssue] = []
    if not spec.scope.user_role:
        issues.append(
            _issue(
                "scope_not_authenticated",
                "Scope carries no caller role; scope must be supplied by the "
                "authenticated caller context, not derived from the question.",
                "scope",
            )
        )
    elif spec.scope.kind == "cross_case" and spec.scope.user_role not in (
        "supervisor", "station-admin", "platform-admin",
    ):
        issues.append(
            _issue(
                "scope_denied",
                f"Cross-case aggregates require supervisor role or higher; "
                f"caller role is {spec.scope.user_role!r}.",
                "scope",
            )
        )
    if spec.scope.kind == "jurisdiction" and not spec.scope.case_ids:
        issues.append(
            _issue(
                "scope_missing_case_ids",
                "Jurisdiction scope requires an explicit case-id allow-list.",
                "scope",
            )
        )
    return issues


def _validate_comparison(spec: AggregateSpec) -> list[ValidationIssue]:
    if spec.compare is None:
        return []
    issues: list[ValidationIssue] = []
    buckets = spec.compare.buckets
    if len(buckets) < 2:
        issues.append(
            _issue("comparison_needs_buckets", "A comparison needs at least two buckets.", "compare")
        )
    if not spec.compare.overlap_allowed and len(set(buckets)) != len(buckets):
        # Overlapping buckets double-count, which drives bucket percentages
        # past 100 and breaks the partition invariant downstream.
        issues.append(
            _issue(
                "comparison_buckets_overlap",
                "Comparison buckets must be disjoint unless overlap_allowed is set.",
                "compare",
            )
        )
    return issues


def _validate_top_n(spec: AggregateSpec) -> list[ValidationIssue]:
    if spec.top_n is None:
        return []
    if spec.top_n <= 0:
        return [_issue("invalid_top_n", f"top_n must be positive, got {spec.top_n}.", "top_n")]
    if not spec.group_by:
        return [
            _issue(
                "top_n_without_grouping",
                "top_n ranks groups, so it requires at least one grouping dimension.",
                "top_n",
            )
        ]
    return []


# ══════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════
# ══════════════════════════════════════════════════════════════════════
# Declared-but-unimplemented fields.
#
# THE DEFECT THIS PREVENTS, measured 2026-09-15. `AggregateSpec` declares
# `compare`, `time_window`, `threshold` and `null_policy`, and
# `MAX_GROUP_DIMENSIONS` permits two grouping dimensions. The compiler
# consumes NONE of the first four and only `group_by[0]`. So a spec saying
# "count cases, restricted to 2024" validated as EXECUTABLE and compiled to
#
#     MATCH (n:Case) RETURN count(DISTINCT n.case_id) AS value
#
# byte-identical to the spec with no time window at all — returning 73, the
# grand total, while the receipt asserted a filter had been applied.
#
# That is Module 144's defect ("How many FIRs were registered in 2019 or
# earlier?" -> 73) reproduced inside the engine built to prevent it, and it
# is strictly worse here because the receipt lends it false provenance.
#
# THE FIX BELONGS AT THIS LAYER, not in the compiler. The compiler's job is
# to emit what it is asked for; a field it cannot emit is a SPECIFICATION
# that cannot be honoured, and refusing unhonourable specifications is this
# module's entire purpose. Silently dropping a filter is the one failure
# mode the whole package exists to make impossible.
#
# This set is the contract between spec.py and compiler.py. When a field
# becomes genuinely supported, its entry is removed here in the SAME change
# that implements it — a test asserts the two stay in step.
_UNIMPLEMENTED_FIELDS: dict[str, str] = {
    "compare": (
        "bucketed comparison is declared in AggregateSpec but not emitted by "
        "the compiler; a spec carrying it would silently return the "
        "uncompared figure"
    ),
    "time_window": (
        "time windows are declared but not emitted; a spec carrying one would "
        "silently return the unfiltered total (this is Module 144's defect)"
    ),
    "threshold": (
        "standalone thresholds are declared but not emitted; express the "
        "condition as a population predicate instead, which IS emitted"
    ),
}


def _validate_implemented(spec: AggregateSpec) -> list[ValidationIssue]:
    """Refuse any spec whose semantics the compiler cannot actually honour."""
    issues: list[ValidationIssue] = []
    for field, reason in _UNIMPLEMENTED_FIELDS.items():
        if getattr(spec, field, None) is not None:
            issues.append(
                _issue(
                    "unsupported_operation",
                    f"{field}: {reason}. Refused rather than ignored.",
                    field,
                )
            )
    # Second and later grouping dimensions are accepted by
    # MAX_GROUP_DIMENSIONS but only `group_by[0]` reaches the query, so a
    # cross-tab would silently collapse to a single-dimension breakdown.
    if len(spec.group_by) > 1:
        issues.append(
            _issue(
                "unsupported_operation",
                f"group_by carries {len(spec.group_by)} dimensions but the "
                f"compiler emits only the first; a cross-tab would silently "
                f"collapse to a one-dimensional breakdown. Refused rather "
                f"than ignored.",
                "group_by",
            )
        )
    # `median` validates but cannot be emitted in Cypher (the compiler
    # raises). Refuse here so the failure is a stated refusal rather than a
    # CompilerError surfacing as an execution fault.
    if spec.measure == "median":
        issues.append(
            _issue(
                "unsupported_operation",
                "median is not computable in AGE and the Python fetch path is "
                "not implemented; refused rather than raised at compile time.",
                "measure",
            )
        )
    return issues


def validate(snapshot: reg.RegistrySnapshot, spec: AggregateSpec) -> ValidationResult:
    """Validate a spec against measured data. Deterministic; no I/O, no LLM.

    Ordering note: scope is checked first and independently, because a
    scope failure is a security outcome and must not be masked by an
    unrelated field-name problem in the same spec.
    """
    issues: list[ValidationIssue] = []
    issues.extend(_validate_scope(spec))
    # Checked early: a spec whose semantics cannot be emitted must be
    # refused before any other diagnosis, so the reported reason is "this
    # cannot be honoured" rather than an incidental field-name complaint.
    issues.extend(_validate_implemented(spec))
    issues.extend(_validate_population(snapshot, spec.population, label="population"))
    issues.extend(_validate_measure(snapshot, spec))
    issues.extend(_validate_grain(snapshot, spec))
    issues.extend(_validate_grouping(snapshot, spec))
    issues.extend(_validate_ratio(snapshot, spec))
    issues.extend(_validate_comparison(spec))
    issues.extend(_validate_top_n(spec))

    if issues:
        logger.info(
            "aggregate validator: spec %s refused — %s",
            spec.spec_hash(), ", ".join(i.code for i in issues),
        )
        return ValidationResult(verdict="UNSUPPORTED", issues=tuple(issues))
    return ValidationResult(verdict="EXECUTABLE")


def is_materially_different(a: float, b: float) -> bool:
    """Whether two candidate readings differ enough to require clarification.

    Relative, with an absolute floor so tiny populations do not trip on a
    single row. 4 vs 70 is materially different; 72 vs 73 is not.
    """
    if a == b:
        return False
    hi, lo = max(abs(a), abs(b)), min(abs(a), abs(b))
    if hi - lo < 2:
        return False
    if hi == 0:
        return False
    return (hi - lo) / hi > MATERIAL_DIFFERENCE_RATIO
