# -*- coding: utf-8 -*-
"""
Phase 7B — the trusted gate catalogue and the profiles built from it.

THE APPROVED SHAPE. A model may SELECT which predefined policy applies to a
question; it may not CREATE policies. So the catalogue below is closed and
lives in trusted code, the planner prompt is RENDERED from that same
catalogue, and the runtime validates the model's choice against it. One
source of truth, so prompt and enforcement cannot drift.

GATES ARE BINARY ELIGIBILITY, NOT QUALITY. Each gate answers one
deterministically measurable question about runtime evidence — was every
requested constraint applied, did execution succeed, are the join keys
stable. A gate does not score anything. Confidence evidence
(`confidence.py`) describes quality AFTER eligibility, and a failed gate is
never rescued by strong confidence: they are different questions asked in a
fixed order.

NOTHING SUBJECTIVE IS REPRESENTABLE. There is no gate for "the model seemed
sure" or "the answer looks plausible", and no way to add one through model
output. Every gate reads a field some component already recorded.

WHY PROFILES RATHER THAN "ALL GATES ALWAYS". Applying every check to every
question would make simple counts refuse for missing cross-source evidence
they never needed. Profiles describe COMPUTATIONAL SHAPES — a simple count,
a filtered count, a multi-source distinct count — and each names the gates
that shape genuinely requires.

THE MODEL CANNOT WEAKEN VALIDATION BY PICKING A SMALLER PROFILE.
`required_profile_for()` derives the minimum admissible profile from the
TYPED spec, independently of anything the model said, and
`validate_selection()` rejects a weaker choice. Selecting
SIMPLE_COUNT for a question carrying three filters is a refusal, not a
shortcut.
"""
from __future__ import annotations

import dataclasses
import enum
from typing import Any, Optional

from src.pipeline.aggregate.validator import ValidationIssue


class Gate(str, enum.Enum):
    """One deterministically measurable eligibility requirement.

    Each member documents the runtime evidence it reads, because a gate
    whose evidence source is unclear is a gate nobody can trust.
    """

    #: Every constraint resolved to a source the registry measured as
    #: authoritative. Evidence: authority.AuthorityDecision.resolved.
    SOURCE_AUTHORITY_RESOLVED = "SOURCE_AUTHORITY_RESOLVED"

    #: No requested constraint was dropped. Evidence:
    #: CompositeResult.unapplied_constraints / receipt.filters_dropped.
    ALL_REQUESTED_CONSTRAINTS_APPLIED = "ALL_REQUESTED_CONSTRAINTS_APPLIED"

    #: The answer describes the population that was asked about — a
    #: restriction that could not be applied must not broaden it.
    #: Evidence: refusal codes + applied-constraint count vs requested.
    NO_SILENT_BROADENING = "NO_SILENT_BROADENING"

    #: The population being measured is identified. Evidence:
    #: receipt.population_definition / CompositeResult.subject_label.
    POPULATION_IDENTIFIED = "POPULATION_IDENTIFIED"

    #: What one counted unit IS was declared. Evidence: receipt.grain.
    GRAIN_VERIFIED = "GRAIN_VERIFIED"

    #: Entity-grain counting has a real unique key, so DISTINCT has a
    #: subject. Evidence: receipt.distinct_key.
    DISTINCT_KEY_VERIFIED = "DISTINCT_KEY_VERIFIED"

    #: Merge tombstones and superseded edge versions were excluded.
    #: Evidence: receipt.injected_predicates / excluded_* counts. NOT
    #: receipt.invariants_checked — that list records grouping and range
    #: checks, not version resolution.
    VERSION_RULES_APPLIED = "VERSION_RULES_APPLIED"

    #: Cross-source populations were joined on shared stable identifiers.
    #: Evidence: Population.key equality across subplans.
    STABLE_JOIN_KEYS = "STABLE_JOIN_KEYS"

    #: Every subplan population was combined; none was computed and then
    #: discarded. Evidence: CompositionStep count vs population count.
    SET_COMPOSITION_COMPLETE = "SET_COMPOSITION_COMPLETE"

    #: A grouping dimension is valid and not too sparse to group by.
    #: Evidence: receipt.group_keys + coverage.
    GROUPING_FIELD_VALID = "GROUPING_FIELD_VALID"

    #: A ratio's denominator is real and meaningful. Evidence:
    #: receipt.denominator_n + coverage.effective_denominator_n.
    DENOMINATOR_VERIFIED = "DENOMINATOR_VERIFIED"

    #: The query ran. Evidence: execution status.
    EXECUTION_SUCCESS = "EXECUTION_SUCCESS"

    #: ADVISORY ONLY — deliberately absent from every profile's required
    #: gates (see `_UNIVERSAL`). Retained as a named, measurable signal so
    #: coverage can be reported and tested, but it never blocks: the
    #: approved policy serves INSUFFICIENT results with a warning.
    COVERAGE_ACCEPTABLE = "COVERAGE_ACCEPTABLE"

    #: How much of the population lacked the measured field is known and
    #: recorded. Evidence: coverage.field_absent_n / excluded_null_n.
    DATA_COMPLETENESS_KNOWN = "DATA_COMPLETENESS_KNOWN"


class GateProfileId(str, enum.Enum):
    """The closed set of selectable profiles.

    A model returns one of these strings and nothing else. An unknown value
    fails strict parsing before it reaches any validator.
    """

    SIMPLE_COUNT = "SIMPLE_COUNT"
    FILTERED_COUNT = "FILTERED_COUNT"
    GROUPED_AGGREGATE = "GROUPED_AGGREGATE"
    MULTI_SOURCE_FILTERED_COUNT = "MULTI_SOURCE_FILTERED_COUNT"
    MULTI_SOURCE_FILTERED_DISTINCT_COUNT = "MULTI_SOURCE_FILTERED_DISTINCT_COUNT"
    RATIO_OR_PERCENTAGE = "RATIO_OR_PERCENTAGE"


#: Gates every profile requires. Stated once so a new profile cannot
#: accidentally omit the checks that are never optional.
#:
#: PHASE 7D — `COVERAGE_ACCEPTABLE` WAS REMOVED FROM HERE. The approved
#: policy is that an INSUFFICIENT-coverage result MAY be served, with an
#: explicit warning attached. A required gate, by definition, blocks. Those
#: two rules cannot both hold for the same check, and Phase 7C measured the
#: contradiction directly: three cases recorded `COVERAGE_ACCEPTABLE:
#: failed` and were served anyway, because the direct path never enforced
#: its gates. Leaving it required would have blocked results the executor
#: is allowed to serve; leaving it required-but-ignored is worse still,
#: because a "failed" gate that changes nothing teaches readers to
#: disbelieve the gate report.
#:
#: Coverage is therefore quality evidence, surfaced as a warning
#: (`INSUFFICIENT_DATA_COVERAGE`) and preserved in provenance and
#: confidence evidence — never as an eligibility gate.
_UNIVERSAL: tuple[Gate, ...] = (
    Gate.SOURCE_AUTHORITY_RESOLVED,
    Gate.POPULATION_IDENTIFIED,
    Gate.GRAIN_VERIFIED,
    Gate.EXECUTION_SUCCESS,
)

#: Gates any profile that carries filters requires.
_FILTERED: tuple[Gate, ...] = (
    Gate.ALL_REQUESTED_CONSTRAINTS_APPLIED,
    Gate.NO_SILENT_BROADENING,
)

#: Gates any profile spanning more than one authoritative source requires.
_MULTI_SOURCE: tuple[Gate, ...] = (
    Gate.STABLE_JOIN_KEYS,
    Gate.SET_COMPOSITION_COMPLETE,
)


@dataclasses.dataclass(frozen=True)
class GateProfile:
    """One approved computational shape and the gates it requires."""

    id: GateProfileId
    description: str
    required_gates: tuple[Gate, ...]
    #: What the shape implies about the query, used to reject a selection
    #: that does not match the typed spec.
    expects_filters: bool = False
    expects_multi_source: bool = False
    expects_grouping: bool = False
    expects_ratio: bool = False
    expects_distinct_entity: bool = False
    #: Ordering for deterministic escalation — a higher rank profile
    #: subsumes a lower one's obligations.
    rank: int = 0


CATALOGUE: dict[GateProfileId, GateProfile] = {
    GateProfileId.SIMPLE_COUNT: GateProfile(
        id=GateProfileId.SIMPLE_COUNT,
        description="Count one population with no filters and no grouping.",
        required_gates=_UNIVERSAL,
        rank=0,
    ),
    GateProfileId.FILTERED_COUNT: GateProfile(
        id=GateProfileId.FILTERED_COUNT,
        description=(
            "Count one population restricted by filters answerable from a "
            "single authoritative source."
        ),
        required_gates=_UNIVERSAL + _FILTERED + (Gate.DATA_COMPLETENESS_KNOWN,),
        expects_filters=True,
        rank=1,
    ),
    GateProfileId.GROUPED_AGGREGATE: GateProfile(
        id=GateProfileId.GROUPED_AGGREGATE,
        description="Aggregate broken down by one grouping dimension.",
        required_gates=_UNIVERSAL + _FILTERED + (
            Gate.GROUPING_FIELD_VALID, Gate.DATA_COMPLETENESS_KNOWN,
        ),
        expects_grouping=True,
        rank=2,
    ),
    GateProfileId.MULTI_SOURCE_FILTERED_COUNT: GateProfile(
        id=GateProfileId.MULTI_SOURCE_FILTERED_COUNT,
        description=(
            "Count a population whose constraints span more than one "
            "authoritative source, combined by stable identifiers."
        ),
        required_gates=_UNIVERSAL + _FILTERED + _MULTI_SOURCE + (
            Gate.DATA_COMPLETENESS_KNOWN,
        ),
        expects_filters=True,
        expects_multi_source=True,
        rank=3,
    ),
    GateProfileId.MULTI_SOURCE_FILTERED_DISTINCT_COUNT: GateProfile(
        id=GateProfileId.MULTI_SOURCE_FILTERED_DISTINCT_COUNT,
        description=(
            "Count DISTINCT entities across multiple authoritative sources, "
            "excluding merge tombstones and superseded edge versions."
        ),
        required_gates=_UNIVERSAL + _FILTERED + _MULTI_SOURCE + (
            Gate.DISTINCT_KEY_VERIFIED,
            Gate.VERSION_RULES_APPLIED,
            Gate.DATA_COMPLETENESS_KNOWN,
        ),
        expects_filters=True,
        expects_multi_source=True,
        expects_distinct_entity=True,
        rank=4,
    ),
    GateProfileId.RATIO_OR_PERCENTAGE: GateProfile(
        id=GateProfileId.RATIO_OR_PERCENTAGE,
        description=(
            "A proportion whose numerator and denominator share one scope "
            "and population."
        ),
        required_gates=_UNIVERSAL + _FILTERED + (
            Gate.DENOMINATOR_VERIFIED, Gate.DATA_COMPLETENESS_KNOWN,
        ),
        expects_filters=True,
        expects_ratio=True,
        rank=3,
    ),
}


# ══════════════════════════════════════════════════════════════════════
# Deterministic query requirements — derived from the TYPED spec only
# ══════════════════════════════════════════════════════════════════════

@dataclasses.dataclass(frozen=True)
class QueryRequirements:
    """What a question actually needs, read off the typed spec.

    Deliberately independent of anything the model said. This is what makes
    "the model picked a weaker profile" detectable rather than persuasive.
    """

    filter_count: int = 0
    has_time_filter: bool = False
    has_grouping: bool = False
    has_ratio: bool = False
    requires_distinct_entity: bool = False
    requires_multiple_sources: bool = False
    distinct_sources: tuple[str, ...] = ()
    traversal_count: int = 0

    @property
    def has_filters(self) -> bool:
        return self.filter_count > 0


def requirements_from_spec(spec: Any, snapshot: Any = None) -> QueryRequirements:
    """Derive requirements from an `AggregateSpec`. No NL interpretation.

    Source multiplicity is decided by the authority registry: a temporal
    restriction on `incident_date` is Postgres-authoritative while the
    population lives in the graph, so such a spec genuinely needs two
    sources — regardless of which profile anyone selected.
    """
    from src.pipeline.aggregate import authority as auth

    population = getattr(spec, "population", None)
    predicates = tuple(getattr(population, "predicates", ()) or ())
    traversals = tuple(getattr(population, "traversals", ()) or ())
    time_window = getattr(spec, "time_window", None)
    group_by = tuple(getattr(spec, "group_by", ()) or ())
    ratio = getattr(spec, "ratio", None)
    grain = getattr(spec, "grain", None)
    measure = getattr(spec, "measure", None)

    sources: set[str] = set()
    if predicates or traversals or population is not None:
        sources.add("graph")
    if time_window is not None and snapshot is not None:
        decision = auth.resolve_authority(
            snapshot, time_window.field, auth.ConstraintKind.TEMPORAL
        )
        if decision.resolved:
            sources.add(decision.source.value)
        else:
            # Unresolved authority still means the question spans beyond
            # what the graph alone can answer; the gate will fail loudly.
            sources.add("unresolved")
    elif time_window is not None:
        sources.add("postgres")

    filter_count = len(predicates) + (1 if time_window is not None else 0)
    scope = getattr(spec, "scope", None)
    if scope is not None and getattr(scope, "case_ids", None):
        filter_count += 1

    # DISTINCT-ENTITY IS NOT "THE GRAIN IS ENTITY". Almost every spec in
    # this corpus is ENTITY-grained, so keying off grain alone made the
    # requirement fire universally — a plain count over a date window
    # derived MULTI_SOURCE_FILTERED_DISTINCT_COUNT and dragged in
    # DISTINCT_KEY_VERIFIED and VERSION_RULES_APPLIED, gates that shape
    # does not need. Stronger-than-necessary gates refusing otherwise-valid
    # queries is exactly the failure the selection policy warns about.
    #
    # The real trigger is either an explicit DISTINCT measure, or a
    # traversal that can multiply rows — the 73->449 fanout case, where
    # counting without DISTINCT is unsound regardless of declared grain.
    requires_distinct = measure == "count_distinct" or (
        grain == "ENTITY" and len(traversals) > 0
    )

    return QueryRequirements(
        filter_count=filter_count,
        has_time_filter=time_window is not None,
        has_grouping=bool(group_by),
        has_ratio=ratio is not None,
        requires_distinct_entity=requires_distinct,
        requires_multiple_sources=len(sources) > 1,
        distinct_sources=tuple(sorted(sources)),
        traversal_count=len(traversals),
    )


def required_profile_for(reqs: QueryRequirements) -> GateProfileId:
    """The MINIMUM admissible profile for these requirements.

    Computed from typed facts, never from model output. Ordering matters:
    the most demanding shape that applies wins, so a multi-source distinct
    count cannot be downgraded to a filtered count.
    """
    if reqs.has_ratio:
        return GateProfileId.RATIO_OR_PERCENTAGE
    if reqs.requires_multiple_sources:
        if reqs.requires_distinct_entity:
            return GateProfileId.MULTI_SOURCE_FILTERED_DISTINCT_COUNT
        return GateProfileId.MULTI_SOURCE_FILTERED_COUNT
    if reqs.has_grouping:
        return GateProfileId.GROUPED_AGGREGATE
    if reqs.has_filters:
        return GateProfileId.FILTERED_COUNT
    return GateProfileId.SIMPLE_COUNT


@dataclasses.dataclass(frozen=True)
class SelectionOutcome:
    """Whether a model's profile choice is admissible."""

    accepted: bool
    effective_profile: GateProfileId
    selected_profile: Optional[GateProfileId]
    required_profile: GateProfileId
    escalated: bool = False
    #: The selection named a shape the query does not have, and trusted
    #: code substituted the profile it derived itself. Distinct from
    #: `escalated`, which raises the bar; this only REPLACES a mismatched
    #: choice with the derived requirement.
    corrected: bool = False
    issue: Optional[ValidationIssue] = None

    def describe(self) -> str:
        if self.accepted and not (self.escalated or self.corrected):
            return f"selected {self.effective_profile.value}"
        if self.escalated:
            return (
                f"escalated {self.selected_profile.value if self.selected_profile else 'none'}"
                f" -> {self.effective_profile.value}"
            )
        if self.corrected:
            return (
                f"corrected {self.selected_profile.value if self.selected_profile else 'none'}"
                f" -> {self.effective_profile.value}"
            )
        return f"rejected: {self.issue.message if self.issue else ''}"


def _shape_incompatible(
    chosen: GateProfileId, reqs: QueryRequirements
) -> Optional[str]:
    """Is the chosen profile a different computational SHAPE?

    PROFILES DESCRIBE VALIDATION SHAPE, NOT THE METRIC OPERATOR. `MIN`,
    `MAX`, `AVG`, `COUNT` and `COUNT_DISTINCT` are calculation semantics
    carried by the typed spec; what a profile decides is whether the
    computation is single- or multi-source, filtered or not, grouped or
    not, and whether it needs a denominator. So an ungrouped `MIN(age)` is
    a single-source ungrouped shape — exactly `SIMPLE_COUNT`'s shape — and
    must not be refused merely because its operator is not a count.

    PHASE 7D — D3. Shape mismatch is now SYMMETRIC. Previously only three
    one-way checks existed, so a multi-source profile chosen for a
    single-source query slipped through as "stronger compatible", dragging
    in `STABLE_JOIN_KEYS` and `SET_COMPOSITION_COMPLETE` — gates a direct
    result can never satisfy. Measured on two Phase 7C cases. More gates is
    not "stronger"; it is a different shape, and demanding composition
    evidence from a computation that never composed is incoherent rather
    than strict.
    """
    profile = CATALOGUE[chosen]

    # ── Ratio: REFUSE. Substituting would change what the answer MEANS ─
    #
    # A proportion and a count are different questions, not different
    # strictness. Silently computing a count where a proportion was named
    # (or the reverse) would serve a number answering something nobody
    # asked, which is worse than refusing.
    if profile.expects_ratio and not reqs.has_ratio:
        return (
            f"{chosen.value} computes a proportion, but the query has no "
            f"numerator/denominator"
        )
    if reqs.has_ratio and not profile.expects_ratio:
        return (
            f"the query computes a proportion, which {chosen.value} does not "
            f"represent"
        )
    return None


def _shape_mismatched(
    chosen: GateProfileId, reqs: QueryRequirements
) -> Optional[str]:
    """A selection naming a shape the query does not have — CORRECTABLE.

    PHASE 7D. These were refusals, and refusing cost real answers: the
    corpus lost `min_person_age` (24) and `max_person_age` (49) because
    the planner labelled an ungrouped MIN as GROUPED_AGGREGATE, and lost
    `accused_distinct_through_relationship` (92) and
    `active_persons_via_belongs_to_case` (208) because it labelled a
    single-source traversal as MULTI_SOURCE. In every case the typed spec
    was valid and the computation was supported — only the label was wrong.

    WHY CORRECTION IS SAFE HERE, AND NOT A DOWNGRADE. The replacement is
    `required_profile_for(typed_spec)`, derived from the spec before the
    model's choice is read. It cannot be weaker than what the query
    demands, because it IS what the query demands. What is discarded is a
    mislabel, not a legitimately stronger choice — and a stronger choice in
    the same family is still accepted untouched (§10).

    Demanding `STABLE_JOIN_KEYS` of a computation that never composed is
    not strictness; no correct result can satisfy it, so enforcing it only
    destroys answers.
    """
    profile = CATALOGUE[chosen]
    if profile.expects_grouping and not reqs.has_grouping:
        return (
            f"{chosen.value} describes a grouped breakdown, but the query has "
            f"no grouping dimension"
        )
    if profile.expects_multi_source and not reqs.requires_multiple_sources:
        return (
            f"{chosen.value} requires cross-source composition, but authority "
            f"resolution shows this query is answerable from one source "
            f"({list(reqs.distinct_sources) or 'single'})"
        )
    return None


def validate_selection(
    selected: Optional[str], reqs: QueryRequirements
) -> SelectionOutcome:
    """Apply the approved selection policy to a model's profile choice.

    Five outcomes, in the order the policy defines them:

      1. exact/compatible match      -> accept
      2. valid but UNDER-POWERED     -> deterministically escalate to the
                                        minimum required profile, recorded
                                        as a ValidationIssue
      3. stronger but compatible     -> accept, never downgraded
      4. unknown id                  -> REFUSE (never inferred)
      5. incompatible SHAPE          -> REFUSE (never transformed)

    The invariant this exists to hold: `effective_profile` is always at
    least `required_profile_for(typed_query)`. There is no path by which a
    weak model selection causes weak gates to run — the requirement is
    derived from the typed spec before the model's choice is even read.
    """
    required = required_profile_for(reqs)

    # No selection at all: trusted code supplies the requirement itself.
    if selected is None:
        return SelectionOutcome(
            accepted=True, effective_profile=required, selected_profile=None,
            required_profile=required, escalated=False,
        )

    # (4) Unknown id. An unrecognised profile is a contract violation, not
    # an under-powered choice, so it is not escalated into something valid.
    try:
        chosen = GateProfileId(selected)
    except ValueError:
        return SelectionOutcome(
            accepted=False, effective_profile=required, selected_profile=None,
            required_profile=required,
            issue=ValidationIssue(
                code="unknown_gate_profile",
                message=(
                    f"{selected!r} is not an approved gate profile. Valid: "
                    f"{', '.join(p.value for p in GateProfileId)}."
                ),
                field="gate_profile_id",
            ),
        )

    # (5) Semantically incoherent shape — REFUSE. Checked before rank,
    # because substituting would change what the answer means.
    incoherent = _shape_incompatible(chosen, reqs)
    if incoherent is not None:
        return SelectionOutcome(
            accepted=False, effective_profile=required, selected_profile=chosen,
            required_profile=required,
            issue=ValidationIssue(
                code="incompatible_gate_profile",
                message=(
                    f"{incoherent}. Refusing rather than substituting a "
                    f"different computational shape."
                ),
                field="gate_profile_id",
            ),
        )

    # (5b) Mislabelled shape — CORRECT to the derived requirement.
    # The computation is valid and supported; only the label was wrong, so
    # discarding the label costs nothing while refusing costs the answer.
    mismatch = _shape_mismatched(chosen, reqs)
    if mismatch is not None:
        return SelectionOutcome(
            accepted=True, effective_profile=required, selected_profile=chosen,
            required_profile=required, corrected=True,
            issue=ValidationIssue(
                code="gate_profile_corrected",
                message=(
                    f"{mismatch}; the profile derived from the typed "
                    f"specification ({required.value}) was applied instead"
                ),
                field="gate_profile_id",
            ),
        )

    # (2) Under-powered: escalate, never run the weaker gate set.
    if CATALOGUE[chosen].rank < CATALOGUE[required].rank:
        return SelectionOutcome(
            accepted=True, effective_profile=required, selected_profile=chosen,
            required_profile=required, escalated=True,
            issue=ValidationIssue(
                code="gate_profile_escalated",
                message=(
                    f"selected {chosen.value} but the query requires "
                    f"{required.value} ({reqs.filter_count} filter(s), "
                    f"sources={list(reqs.distinct_sources)}); the stronger "
                    f"profile was applied"
                ),
                field="gate_profile_id",
            ),
        )

    # (1)/(3) Exact match, or stronger and compatible. Never downgraded.
    return SelectionOutcome(
        accepted=True, effective_profile=chosen, selected_profile=chosen,
        required_profile=required,
    )


# ══════════════════════════════════════════════════════════════════════
# Prompt rendering — the SAME catalogue the runtime enforces
# ══════════════════════════════════════════════════════════════════════

def render_catalogue_for_prompt() -> str:
    """The approved profiles, as the planner prompt shows them.

    Generated from `CATALOGUE` so the prompt cannot drift from enforcement.
    Compact on purpose: the local model reads this alongside a ~9k-char
    schema card, and a policy essay would crowd out the schema it needs.
    """
    lines = ["APPROVED GATE PROFILES (choose exactly one id):"]
    for profile in sorted(CATALOGUE.values(), key=lambda p: p.rank):
        lines.append(f"  {profile.id.value}")
        lines.append(f"      {profile.description}")
    lines.append("")
    lines.append(
        "Pick the profile matching the question's shape. A question with "
        "filters is not a SIMPLE_COUNT; one needing both a date and graph "
        "facts is MULTI_SOURCE. You may not invent profiles, gates, "
        "conditions, weights or thresholds — only select an id above. "
        "Never drop a requested filter to make a simpler profile fit."
    )
    return "\n".join(lines)
