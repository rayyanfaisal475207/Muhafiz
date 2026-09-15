# -*- coding: utf-8 -*-
"""
Phase 2 — the evaluation corpus: broad coverage of the operator space.

WHAT THIS IS. A deterministic list of aggregate cases, each one a set of
BINDINGS over the existing operators plus an independently-established
expected value. It is data, not code: adding a case costs no compiler
change, no new emitter and no question-specific function. If a case here
ever required one, the algebra would have failed its own test and the
correct response would be to document the missing abstraction — not to
special-case it.

WHERE THE EXPECTED VALUES COME FROM. Every `expected` below was established
by a route that does NOT reuse this package's compiler, because validating
a compiler against itself proves nothing. Three routes were used and, where
they overlap, they agree:

  SQL     — direct Postgres over `cases` (73 rows, 19 stations, 64 dated,
            13 in 2024, 51 in 2026).
  GRAPH   — hand-written Cypher with traversal shapes deliberately unlike
            the compiler's emission.
  PYTHON  — raw rows fetched with no aggregation in Cypher at all, then
            counted in Python (ages n=19 min 24 max 49 mean 31.8421
            median 31 sum 605; 4 of 73 cases with >1 distinct officer;
            430 Person nodes = 208 active + 222 tombstoned).

`GroundTruth.route` records which one, so a reader can re-derive any figure
without trusting this file. A case with no independent route is marked
`UNRESOLVED` and carries no expected value — per the brief, guessing is
worse than admitting the gap.

WHAT IS DELIBERATELY ABSENT. No case asserts "the new engine equals the old
engine". Phase 1 established that the legacy path misroutes and applies
neither tombstone nor supersession filtering, so legacy agreement is
evidence about ROUTING, not about correctness. Legacy comparison lives in
`shadow.py`; this file is about semantics.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Literal, Optional

from src.pipeline.aggregate.spec import (
    AggregateSpec,
    Comparison,
    FieldPredicate,
    GroupBy,
    PopulationNode,
    Ratio,
    RelationCountPredicate,
    Scope,
    TimeWindow,
    Traversal,
)

# ── Coverage axes, used by the report to avoid inflated percentages ───
MeasureAxis = Literal["count", "count_distinct", "sum", "avg", "min", "max", "median"]
PopulationAxis = Literal[
    "direct", "property_filter", "relationship_existence", "through_relationship",
    "relationship_grain", "role_pair", "fanning", "filtered",
]
GroupingAxis = Literal["none", "single", "multi", "sparse", "invalid", "via_traversal"]
DerivedAxis = Literal[
    "none", "ratio", "comparison", "threshold", "top_n", "time_window",
]
GuardAxis = Literal[
    "none", "fanout", "tombstone", "superseded", "sparse_property",
    "null_grouping", "zero_denominator", "canonicalization", "scope",
    "unimplemented", "invariant",
]

#: Expected outcome of running a case.
Outcome = Literal["value", "refused", "unresolved"]


@dataclasses.dataclass(frozen=True)
class GroundTruth:
    """An expected value and the independent route that established it."""

    value: Any
    route: Literal["SQL", "GRAPH", "PYTHON", "REGISTRY", "NONE"]
    note: str = ""


@dataclasses.dataclass(frozen=True)
class Case:
    """One evaluation case: bindings in, expected semantics out."""

    name: str
    description: str
    spec: AggregateSpec
    outcome: Outcome
    truth: Optional[GroundTruth] = None
    #: Expected refusal code when `outcome == "refused"`. Naming the code
    #: (not merely "it refused") is what makes a refusal test meaningful —
    #: refusing for the wrong reason is its own bug.
    refusal_code: Optional[str] = None

    measure_axis: MeasureAxis = "count"
    population_axis: PopulationAxis = "direct"
    grouping_axis: GroupingAxis = "none"
    derived_axis: DerivedAxis = "none"
    guard_axis: GuardAxis = "none"


def _sc(role: str = "supervisor") -> Scope:
    return Scope(kind="cross_case", user_role=role, user_id="corpus")


# ══════════════════════════════════════════════════════════════════════
# The corpus
# ══════════════════════════════════════════════════════════════════════
def build_corpus() -> list[Case]:
    sc = _sc()
    cases: list[Case] = []

    # ── MEASURES ──────────────────────────────────────────────────────
    cases.append(Case(
        name="count_cases",
        description="Total registered cases.",
        spec=AggregateSpec(
            question_text="How many cases are registered?",
            measure="count", population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=sc,
        ),
        outcome="value",
        truth=GroundTruth(73, "SQL", "SELECT count(*) FROM cases"),
        measure_axis="count",
    ))

    cases.append(Case(
        name="count_distinct_active_persons",
        description="Distinct ACTIVE persons — tombstoned donors excluded.",
        spec=AggregateSpec(
            question_text="How many distinct persons are on record?",
            measure="count_distinct", population=PopulationNode(entity="Person"),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
        ),
        outcome="value",
        truth=GroundTruth(
            208, "PYTHON",
            "430 Person nodes fetched raw; 208 have merged_into IS NULL, 222 do not",
        ),
        measure_axis="count_distinct", guard_axis="tombstone",
    ))

    for measure, expected, note in (
        ("min", 24, "min of 19 raw ages"),
        ("max", 49, "max of 19 raw ages"),
        ("avg", 31.842105263157894, "mean of 19 raw ages"),
        ("sum", 605, "sum of 19 raw ages"),
    ):
        cases.append(Case(
            name=f"{measure}_person_age",
            description=f"{measure} of recorded accused/person ages.",
            spec=AggregateSpec(
                question_text=f"What is the {measure} recorded age?",
                measure=measure, value_field="Person.age",  # type: ignore[arg-type]
                population=PopulationNode(entity="Person"),
                grain="ENTITY", distinct_key="entity_id", scope=sc,
            ),
            outcome="value",
            truth=GroundTruth(expected, "PYTHON", note),
            measure_axis=measure,  # type: ignore[arg-type]
            guard_axis="sparse_property",
        ))

    cases.append(Case(
        name="median_refused",
        description="median is declared but not computable in AGE.",
        spec=AggregateSpec(
            question_text="What is the median age?",
            measure="median", value_field="Person.age",
            population=PopulationNode(entity="Person"),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
        ),
        outcome="refused", refusal_code="unsupported_operation",
        measure_axis="median", guard_axis="unimplemented",
    ))

    # ── POPULATION SHAPES ─────────────────────────────────────────────
    cases.append(Case(
        name="weapons_unlicensed_property_filter",
        description="Property filter on a real Urdu value.",
        spec=AggregateSpec(
            question_text="How many recovered weapons are unlicensed?",
            measure="count_distinct",
            population=PopulationNode(
                entity="Weapon",
                predicates=(
                    FieldPredicate(field="Weapon.license_status", op="contains",
                                   value="لائسنس"),
                ),
            ),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
        ),
        outcome="value",
        truth=GroundTruth(
            30, "GRAPH",
            "MATCH (w:Weapon) WHERE w.license_status CONTAINS 'لائسنس'",
        ),
        measure_axis="count_distinct", population_axis="property_filter",
    ))

    cases.append(Case(
        name="cases_with_a_weapon",
        description="Relationship existence, reverse traversal, DISTINCT required.",
        spec=AggregateSpec(
            question_text="How many cases have a weapon recorded?",
            measure="count_distinct",
            population=PopulationNode(
                entity="Case",
                traversals=(Traversal(rel="BELONGS_TO_CASE", target="Weapon",
                                      direction="in"),),
            ),
            grain="ENTITY", distinct_key="case_id", scope=sc,
        ),
        outcome="value",
        truth=GroundTruth(
            32, "GRAPH",
            "MATCH (w:Weapon)-[:BELONGS_TO_CASE]->(c:Case) count(DISTINCT c.case_id)",
        ),
        measure_axis="count_distinct", population_axis="relationship_existence",
    ))

    cases.append(Case(
        name="accused_distinct_through_relationship",
        description="Role-filtered traversal; counts the SUBJECT, not the terminal.",
        spec=AggregateSpec(
            question_text="How many distinct accused persons are there?",
            measure="count_distinct",
            population=PopulationNode(
                entity="Person",
                traversals=(Traversal(
                    rel="INVOLVED_IN", target="Incident", direction="out",
                    role_field="role", role_value="accused",
                ),),
            ),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
        ),
        outcome="value",
        truth=GroundTruth(
            92, "GRAPH",
            "role='accused', count(DISTINCT p.entity_id); 92 people across 94 edges",
        ),
        measure_axis="count_distinct", population_axis="through_relationship",
        guard_axis="fanout",
    ))

    cases.append(Case(
        name="accused_edges_relationship_grain",
        description="The SAME question at RELATIONSHIP grain — 94 links, not 92 people.",
        spec=AggregateSpec(
            question_text="How many accused involvements are recorded?",
            measure="count",
            population=PopulationNode(
                entity="Person",
                traversals=(Traversal(
                    rel="INVOLVED_IN", target="Incident", direction="out",
                    role_field="role", role_value="accused",
                ),),
            ),
            grain="RELATIONSHIP", scope=sc,
        ),
        outcome="value",
        truth=GroundTruth(
            94, "GRAPH", "count(r) over role='accused' edges",
        ),
        measure_axis="count", population_axis="relationship_grain",
        guard_axis="fanout",
    ))

    cases.append(Case(
        name="active_persons_via_belongs_to_case",
        description="Fanning traversal with a key — tombstones and superseded edges excluded.",
        spec=AggregateSpec(
            question_text="How many distinct persons are linked to cases?",
            measure="count_distinct",
            population=PopulationNode(
                entity="Person",
                traversals=(Traversal(rel="BELONGS_TO_CASE", target="Case",
                                      direction="out"),),
            ),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
        ),
        outcome="value",
        truth=GroundTruth(
            208, "GRAPH",
            "merged_into IS NULL AND superseded_by IS NULL; 429 without them",
        ),
        measure_axis="count_distinct", population_axis="fanning",
        guard_axis="tombstone",
    ))

    cases.append(Case(
        name="malkhana_records",
        description="RECORD grain over StructuredRecord, keyed by record_id.",
        spec=AggregateSpec(
            question_text="How many malkhana register entries exist?",
            measure="count_distinct",
            population=PopulationNode(
                entity="StructuredRecord",
                predicates=(FieldPredicate(
                    field="StructuredRecord.record_type", op="eq",
                    value="malkhana_register"),),
            ),
            grain="RECORD", distinct_key="record_id", scope=sc,
        ),
        outcome="value",
        truth=GroundTruth(45, "GRAPH", "record_type='malkhana_register'"),
        measure_axis="count_distinct", population_axis="property_filter",
    ))

    # ── GROUPING ──────────────────────────────────────────────────────
    cases.append(Case(
        name="cases_by_station_via_traversal",
        description="Grouping dimension reached via a hop; measure stays on the base.",
        spec=AggregateSpec(
            question_text="How many cases per police station?",
            measure="count",
            population=PopulationNode(
                entity="Case",
                traversals=(Traversal(rel="FILED_AT", target="PoliceStation",
                                      direction="out"),),
            ),
            grain="ENTITY", distinct_key="case_id", scope=sc,
            group_by=(GroupBy(
                field="PoliceStation.name",
                via=(Traversal(rel="FILED_AT", target="PoliceStation"),),
            ),),
        ),
        outcome="value",
        truth=GroundTruth(
            19, "SQL",
            "SELECT count(DISTINCT police_station) FROM cases = 19 groups; "
            "top group = 7 (تھانہ ماڈل ٹاؤن، لاہور)",
        ),
        measure_axis="count", population_axis="through_relationship",
        grouping_axis="via_traversal",
    ))

    cases.append(Case(
        name="group_by_sparse_field_refused",
        description="investigation_status is 21/73 — below the grouping presence floor.",
        spec=AggregateSpec(
            question_text="How many cases per investigation status?",
            measure="count", population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=sc,
            group_by=(GroupBy(field="investigation_status"),),
        ),
        outcome="refused", refusal_code="grouping_field_too_sparse",
        grouping_axis="sparse", guard_axis="sparse_property",
    ))

    cases.append(Case(
        name="group_by_unknown_field_refused",
        description="A dimension that does not exist must be refused, not NULL-grouped.",
        spec=AggregateSpec(
            question_text="How many cases per verdict?",
            measure="count", population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=sc,
            group_by=(GroupBy(field="verdict"),),
        ),
        outcome="refused", refusal_code="unknown_field",
        grouping_axis="invalid", guard_axis="null_grouping",
    ))

    cases.append(Case(
        name="group_by_two_dimensions_refused",
        description="Cross-tab is declared but only dimension 0 is emitted.",
        spec=AggregateSpec(
            question_text="Cases by station and category?",
            measure="count", population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=sc,
            group_by=(GroupBy(field="police_station"),
                      GroupBy(field="crime_category")),
        ),
        outcome="refused", refusal_code="unsupported_operation",
        grouping_axis="multi", guard_axis="unimplemented",
    ))

    # ── DERIVED ───────────────────────────────────────────────────────
    cases.append(Case(
        name="ratio_cases_multi_officer_entity_grain",
        description="THE mandated case: % of cases with >1 DISTINCT officer.",
        spec=AggregateSpec(
            question_text="What percentage of cases involved more than one officer?",
            measure="count", population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=sc,
            ratio=Ratio(
                numerator=PopulationNode(
                    entity="Case",
                    predicates=(RelationCountPredicate(
                        rel="ASSIGNED_TO", target="Officer", op="gt", value=1,
                        count_grain="ENTITY", direction="in"),),
                ),
                denominator=PopulationNode(entity="Case"),
            ),
        ),
        outcome="value",
        # Held as the exact expression, NOT a rounded decimal. Writing
        # `round(100*4/73, 4)` = 5.4795 here made the corpus disagree with a
        # correct engine result of 5.47945205479452 — a fixture defect that
        # would have been "fixed" by loosening the comparison tolerance,
        # hiding real drift in the process.
        truth=GroundTruth(
            100 * 4 / 73, "PYTHON",
            "raw ASSIGNED_TO rows grouped in Python: 4 of 73 cases have >1 "
            "distinct non-superseded officer",
        ),
        measure_axis="count", population_axis="filtered", derived_axis="ratio",
        guard_axis="invariant",
    ))

    cases.append(Case(
        name="ratio_cases_multi_officer_relationship_grain",
        description="Same question, edge reading — must be reachable only by asking.",
        spec=AggregateSpec(
            question_text="What percentage of cases have more than one officer assignment?",
            measure="count", population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=sc,
            ratio=Ratio(
                numerator=PopulationNode(
                    entity="Case",
                    predicates=(RelationCountPredicate(
                        rel="ASSIGNED_TO", target="Officer", op="gt", value=1,
                        count_grain="RELATIONSHIP", direction="in"),),
                ),
                denominator=PopulationNode(entity="Case"),
            ),
        ),
        outcome="value",
        # Exact expression, for the same reason as the ENTITY-grain case
        # above: a rounded literal here passed only because the comparison
        # tolerance absorbed it, which is luck rather than verification.
        truth=GroundTruth(
            100 * 70 / 73, "GRAPH",
            "count(r) > 1 per case: 70 of 73; the 67 duplicate (case,officer) "
            "pairs are one officer in two roles",
        ),
        measure_axis="count", population_axis="relationship_grain",
        derived_axis="ratio", guard_axis="fanout",
    ))

    cases.append(Case(
        name="threshold_cases_with_more_than_one_weapon",
        description="Threshold over a relation count — composed, not special-cased.",
        spec=AggregateSpec(
            question_text="How many cases involve more than one weapon?",
            measure="count_distinct",
            population=PopulationNode(
                entity="Case",
                predicates=(RelationCountPredicate(
                    rel="BELONGS_TO_CASE", target="Weapon", op="gt", value=1,
                    count_grain="ENTITY", direction="in"),),
            ),
            grain="ENTITY", distinct_key="case_id", scope=sc,
        ),
        outcome="value",
        truth=GroundTruth(
            0, "GRAPH",
            "weapon entity_ids are FIR-scoped, so no case has >1 distinct weapon",
        ),
        measure_axis="count_distinct", population_axis="filtered",
        derived_axis="threshold",
    ))

    cases.append(Case(
        name="ratio_zero_denominator_refused",
        description="Zero denominator must refuse before any division.",
        spec=AggregateSpec(
            question_text="What percentage of vehicles with no plate are stolen?",
            measure="count", population=PopulationNode(entity="Vehicle"),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
            ratio=Ratio(
                numerator=PopulationNode(
                    entity="Vehicle",
                    predicates=(FieldPredicate(
                        field="Vehicle.entity_id", op="eq",
                        value="__nonexistent__"),),
                ),
                denominator=PopulationNode(
                    entity="Vehicle",
                    predicates=(FieldPredicate(
                        field="Vehicle.entity_id", op="eq",
                        value="__nonexistent__"),),
                ),
            ),
        ),
        outcome="refused", refusal_code="zero_denominator",
        derived_axis="ratio", guard_axis="zero_denominator",
    ))

    # ── TIME WINDOWS ──────────────────────────────────────────────────
    #
    # `incident_date` is Postgres-authoritative (64/73) and the AGE Case
    # node carries no date at all, so each window resolves to a case-id
    # allow-list before compilation. Bounds are INCLUSIVE on both ends,
    # which the two boundary cases below prove against the dataset's own
    # extremes rather than asserting.
    #
    # Every expected value here came from direct SQL over `cases`, never
    # from this engine.
    for _name, _start, _end, _expected, _note in (
        ("2024", "2024-01-01", "2024-12-31", 13,
         "WHERE incident_date BETWEEN 2024-01-01 AND 2024-12-31"),
        ("2026", "2026-01-01", "2026-12-31", 51,
         "WHERE incident_date BETWEEN 2026-01-01 AND 2026-12-31"),
        ("2025_empty", "2025-01-01", "2025-12-31", 0,
         "no case has a 2025 incident_date; an empty window is a real "
         "answer, not an absent filter"),
        ("lower_bound_only", "2026-01-01", None, 51,
         "WHERE incident_date >= 2026-01-01"),
        ("upper_bound_only", None, "2024-12-31", 13,
         "WHERE incident_date <= 2024-12-31"),
        ("narrow_sept_2024", "2024-09-01", "2024-09-30", 13,
         "WHERE incident_date BETWEEN 2024-09-01 AND 2024-09-30"),
        ("pre_2020_empty", None, "2019-12-31", 0,
         "Module 144's original question shape; the corpus min is 2024-09-14"),
        ("inclusive_min_boundary", "2024-09-14", "2024-09-14", 1,
         "the dataset's own minimum date, matched by an equal-bound window "
         "— proves the lower bound is inclusive"),
        ("inclusive_max_boundary", "2026-08-01", "2026-08-01", 1,
         "the dataset's own maximum date — proves the upper bound is "
         "inclusive"),
    ):
        cases.append(Case(
            name=f"time_window_{_name}",
            description=f"Temporal restriction on incident_date ({_name}).",
            spec=AggregateSpec(
                question_text=f"How many cases fall in the {_name} window?",
                measure="count", population=PopulationNode(entity="Case"),
                grain="ENTITY", distinct_key="case_id", scope=sc,
                time_window=TimeWindow(
                    field="incident_date", start=_start, end=_end),
            ),
            outcome="value",
            truth=GroundTruth(_expected, "SQL", _note),
            measure_axis="count", population_axis="filtered",
            derived_axis="time_window",
        ))

    cases.append(Case(
        name="time_window_inverted_range_refused",
        description="start after end matches nothing and is a mistake, not an intent.",
        spec=AggregateSpec(
            question_text="How many cases between 2026 and 2024?",
            measure="count", population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=sc,
            time_window=TimeWindow(
                field="incident_date", start="2026-01-01", end="2024-01-01"),
        ),
        outcome="refused", refusal_code="invalid_time_window",
        derived_axis="time_window", guard_axis="invariant",
    ))

    cases.append(Case(
        name="time_window_unsupported_field_refused",
        description="A window on a field with no temporal resolver must refuse.",
        spec=AggregateSpec(
            question_text="How many persons aged within 2024?",
            measure="count_distinct", population=PopulationNode(entity="Person"),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
            time_window=TimeWindow(field="Person.age", start="2024-01-01"),
        ),
        outcome="refused", refusal_code="unsupported_operation",
        derived_axis="time_window", guard_axis="unimplemented",
    ))

    cases.append(Case(
        name="time_window_population_without_case_refused",
        description="A population that never reaches Case cannot be restricted.",
        spec=AggregateSpec(
            question_text="How many officers in 2024?",
            measure="count_distinct", population=PopulationNode(entity="Officer"),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
            time_window=TimeWindow(field="incident_date", start="2024-01-01"),
        ),
        outcome="refused", refusal_code="compile_failed",
        derived_axis="time_window", guard_axis="invariant",
    ))

    cases.append(Case(
        name="comparison_refused_not_emitted",
        description="compare is declared but never reaches the query.",
        spec=AggregateSpec(
            question_text="Compare 2024 with 2026.",
            measure="count", population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=sc,
            compare=Comparison(dimension="year", buckets=(2024, 2026)),
        ),
        outcome="refused", refusal_code="unsupported_operation",
        derived_axis="comparison", guard_axis="unimplemented",
    ))

    # ── GUARDS ────────────────────────────────────────────────────────
    cases.append(Case(
        name="fanout_case_person_no_key_refused",
        description="Case->Person fans 73 into 449; ENTITY count without a key must refuse.",
        spec=AggregateSpec(
            question_text="How many cases involve a person?",
            measure="count",
            population=PopulationNode(
                entity="Case",
                traversals=(Traversal(rel="BELONGS_TO_CASE", target="Person",
                                      direction="in"),),
            ),
            grain="ENTITY", distinct_key=None, scope=sc,
        ),
        outcome="refused", refusal_code="missing_distinct_key",
        population_axis="fanning", guard_axis="fanout",
    ))

    cases.append(Case(
        name="fanout_case_address_no_key_refused",
        description="Case->Address reverse-fans 1 case into 2,100 rows.",
        spec=AggregateSpec(
            question_text="How many cases have an address?",
            measure="count",
            population=PopulationNode(
                entity="Case",
                traversals=(Traversal(rel="BELONGS_TO_CASE", target="Address",
                                      direction="in"),),
            ),
            grain="ENTITY", distinct_key=None, scope=sc,
        ),
        outcome="refused", refusal_code="missing_distinct_key",
        population_axis="fanning", guard_axis="fanout",
    ))

    cases.append(Case(
        name="avg_over_fanning_population_refused",
        description="A fanning hop weights each value by its edge count.",
        spec=AggregateSpec(
            question_text="What is the average age of persons linked to cases?",
            measure="avg", value_field="Person.age",
            population=PopulationNode(
                entity="Person",
                traversals=(Traversal(rel="BELONGS_TO_CASE", target="Case",
                                      direction="out"),),
            ),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
        ),
        outcome="refused", refusal_code="compile_failed",
        measure_axis="avg", population_axis="fanning", guard_axis="fanout",
    ))

    cases.append(Case(
        name="unknown_entity_refused",
        description="An entity the data model does not have.",
        spec=AggregateSpec(
            question_text="How many suspects are there?",
            measure="count", population=PopulationNode(entity="Suspect"),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
        ),
        outcome="refused", refusal_code="unknown_entity",
        guard_axis="invariant",
    ))

    cases.append(Case(
        name="unknown_relationship_refused",
        description="A traversal never observed in the graph.",
        spec=AggregateSpec(
            question_text="How many cases were prosecuted by an officer?",
            measure="count_distinct",
            population=PopulationNode(
                entity="Case",
                traversals=(Traversal(rel="PROSECUTED_BY", target="Officer"),),
            ),
            grain="ENTITY", distinct_key="case_id", scope=sc,
        ),
        outcome="refused", refusal_code="unknown_relationship",
        guard_axis="invariant",
    ))

    cases.append(Case(
        name="entity_grain_without_usable_key_refused",
        description="Address entity_id is on 2,100 of 2,171 nodes — not a key.",
        spec=AggregateSpec(
            question_text="How many distinct addresses are there?",
            measure="count_distinct", population=PopulationNode(entity="Address"),
            grain="ENTITY", distinct_key="entity_id", scope=sc,
        ),
        outcome="refused", refusal_code="no_distinct_key",
        measure_axis="count_distinct", guard_axis="invariant",
    ))

    cases.append(Case(
        name="non_supervisor_cross_case_refused",
        description="Scope comes from the caller and cannot be widened.",
        spec=AggregateSpec(
            question_text="How many cases are registered?",
            measure="count", population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=_sc("investigator"),
        ),
        outcome="refused", refusal_code="scope_denied",
        guard_axis="scope",
    ))

    cases.append(Case(
        name="unauthenticated_scope_refused",
        description="A spec with no caller role would run unauthenticated.",
        spec=AggregateSpec(
            question_text="How many cases are registered?",
            measure="count", population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=Scope(),
        ),
        outcome="refused", refusal_code="scope_not_authenticated",
        guard_axis="scope",
    ))

    return cases


def coverage_summary(cases: list[Case]) -> dict[str, dict[str, int]]:
    """Count cases per axis value — the input to the Phase 2 report.

    Deliberately counts CASES, not "supported features": a refusal case
    proves a guard, not a capability, and the report keeps the two apart so
    coverage cannot be inflated by counting refusals as successes.
    """
    axes = {
        "measure": "measure_axis", "population": "population_axis",
        "grouping": "grouping_axis", "derived": "derived_axis",
        "guard": "guard_axis", "outcome": "outcome",
    }
    out: dict[str, dict[str, int]] = {}
    for label, attr in axes.items():
        counts: dict[str, int] = {}
        for c in cases:
            key = str(getattr(c, attr))
            counts[key] = counts.get(key, 0) + 1
        out[label] = dict(sorted(counts.items()))
    return out
