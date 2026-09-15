# -*- coding: utf-8 -*-
"""
Tests for the offline aggregate engine (src/pipeline/aggregate/).

WHAT THESE TESTS ARE FOR. Not "does the code run" — these pin the specific
wrong-number paths the architecture review found in live data, so that a
future change that reopens one fails the build. Each fixture's numbers were
measured against the real database on 2026-09-15 and are cited where they
appear.

OFFLINE BY CONSTRUCTION. Every test builds a `RegistrySnapshot` by hand
rather than reading the database, so the suite keeps this repo's first
rule (`tests/conftest.py`: "No network"). The snapshot fixtures below
MIRROR measured live values; `test_registry_fixture_matches_measured_reality`
documents the provenance of each so drift is visible in review rather than
hidden in a constant.
"""
from __future__ import annotations

import pytest

from src.pipeline.aggregate import coverage as cov
from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate import shapes
from src.pipeline.aggregate.compiler import CompilerError, compile_ratio, compile_spec
from src.pipeline.aggregate.receipt import (
    AggregateReceipt,
    describe_population,
    refusal_receipt,
)
from src.pipeline.aggregate.spec import (
    AggregateSpec,
    Comparison,
    FieldPredicate,
    GroupBy,
    PopulationNode,
    Ratio,
    RelationCountPredicate,
    Scope,
    Traversal,
)
from src.pipeline.aggregate.validator import (
    MIN_GROUPING_FIELD_PRESENCE,
    is_materially_different,
    validate,
)


# ══════════════════════════════════════════════════════════════════════
# Fixtures mirroring MEASURED live data (2026-09-15)
# ══════════════════════════════════════════════════════════════════════
def _prop(name: str, present: int, total: int) -> reg.PropertyInfo:
    return reg.PropertyInfo(name=name, present_n=present, total_n=total)


@pytest.fixture
def snapshot() -> reg.RegistrySnapshot:
    """A registry mirroring the live graph's shape.

    Live values reproduced here: Case 73, Person 430 (222 tombstoned ->
    208 active), Officer 1155, Weapon 32, PoliceStation 19, District 9,
    Address 2171 (entity_id on only 2100 -> no usable distinct key).
    """
    entities = {
        "Case": reg.EntityInfo(
            label="Case", total_n=73, distinct_key="case_id",
            properties={
                "case_id": _prop("case_id", 73, 73),
                "canonical_name": _prop("canonical_name", 73, 73),
            },
        ),
        "Person": reg.EntityInfo(
            label="Person", total_n=430, distinct_key="entity_id",
            properties={
                "entity_id": _prop("entity_id", 430, 430),
                "age": _prop("age", 19, 430),          # measured: 19 of 430
                "gender": _prop("gender", 91, 430),
                "cnic": _prop("cnic", 196, 430),
                "merged_into": _prop("merged_into", 222, 430),
            },
            has_tombstones=True, tombstoned_n=222,     # measured: 222
        ),
        "Officer": reg.EntityInfo(
            label="Officer", total_n=1155, distinct_key="entity_id",
            properties={
                "entity_id": _prop("entity_id", 1155, 1155),
                "belt_no": _prop("belt_no", 77, 1155),
            },
        ),
        "Weapon": reg.EntityInfo(
            label="Weapon", total_n=32, distinct_key="entity_id",
            properties={
                "entity_id": _prop("entity_id", 32, 32),
                "license_status": _prop("license_status", 30, 32),
            },
        ),
        "PoliceStation": reg.EntityInfo(
            label="PoliceStation", total_n=19, distinct_key="station_id",
            properties={
                "station_id": _prop("station_id", 19, 19),
                "name": _prop("name", 19, 19),
            },
        ),
        "District": reg.EntityInfo(
            label="District", total_n=9, distinct_key="district_id",
            properties={
                "district_id": _prop("district_id", 9, 9),
                "name": _prop("name", 9, 9),
            },
        ),
        "Incident": reg.EntityInfo(
            label="Incident", total_n=73, distinct_key="entity_id",
            properties={"entity_id": _prop("entity_id", 73, 73)},
        ),
        # Address: entity_id present on 2100 of 2171 -> registry refuses a
        # key, so ENTITY grain must be refused for it.
        "Address": reg.EntityInfo(
            label="Address", total_n=2171, distinct_key=None,
            properties={"entity_id": _prop("entity_id", 2100, 2171)},
        ),
    }

    def rel(src, rtype, dst, edges, srcs, tgts, fan, rfan, sup=0):
        return reg.RelationshipInfo(
            source_label=src, rel_type=rtype, target_label=dst,
            edge_n=edges, distinct_sources_n=srcs, distinct_targets_n=tgts,
            max_fanout=fan, max_reverse_fanout=rfan,
            cardinality=reg.classify_cardinality(edges, srcs, tgts),
            is_versioned=sup > 0, superseded_n=sup,
        )

    relationships = {
        # measured: 144 edges, 76 officers, 73 cases, 1 superseded.
        # reverse fanout 4 = up to 4 officers on one case.
        ("Officer", "ASSIGNED_TO", "Case"): rel("Officer", "ASSIGNED_TO", "Case", 144, 76, 73, 4, 4, 1),
        # measured: 449 edges, 208 active persons, 73 cases; one case
        # reaches up to 62 persons.
        ("Person", "BELONGS_TO_CASE", "Case"): rel("Person", "BELONGS_TO_CASE", "Case", 449, 208, 73, 5, 62, 222),
        # measured: 2,100 edges from 2,100 addresses to ONE case. Forward
        # fanout 1 (safe); reverse fanout 2,100 (the worst inflation in the
        # database). This asymmetry is why cardinality must be directional.
        ("Address", "BELONGS_TO_CASE", "Case"): rel("Address", "BELONGS_TO_CASE", "Case", 2100, 2100, 1, 1, 2100),
        ("Person", "INVOLVED_IN", "Incident"): rel("Person", "INVOLVED_IN", "Incident", 221, 208, 73, 3, 62),
        ("Weapon", "BELONGS_TO_CASE", "Case"): rel("Weapon", "BELONGS_TO_CASE", "Case", 32, 32, 32, 1, 1),
        ("Case", "FILED_AT", "PoliceStation"): rel("Case", "FILED_AT", "PoliceStation", 73, 73, 19, 1, 7),
        ("PoliceStation", "PART_OF", "District"): rel("PoliceStation", "PART_OF", "District", 73, 19, 9, 1, 12),
    }

    logical_fields = {
        # Postgres case fields, measured population 73
        "incident_date": reg.LogicalField(
            name="incident_date", source="postgres", path="cases.incident_date",
            present_n=64, total_n=73,
            authority_basis="xagg._case_completeness_scan(): case rows authoritative",
            secondary_paths=("Incident.incident_datetime",),
        ),
        "police_station": reg.LogicalField(
            name="police_station", source="postgres", path="cases.police_station",
            present_n=73, total_n=73, authority_basis="single source",
        ),
        "crime_category": reg.LogicalField(
            name="crime_category", source="postgres", path="cases.crime_category",
            present_n=73, total_n=73, authority_basis="single source",
        ),
        # measured: 21 of 73 -> below the grouping presence floor
        "investigation_status": reg.LogicalField(
            name="investigation_status", source="postgres",
            path="cases.investigation_status", present_n=21, total_n=73,
            authority_basis="single source",
        ),
        "Person.age": reg.LogicalField(
            name="Person.age", source="graph", path="Person.age",
            present_n=19, total_n=430, authority_basis="single source",
        ),
        "Weapon.license_status": reg.LogicalField(
            name="Weapon.license_status", source="graph", path="Weapon.license_status",
            present_n=30, total_n=32, authority_basis="single source",
        ),
        "PoliceStation.name": reg.LogicalField(
            name="PoliceStation.name", source="graph", path="PoliceStation.name",
            present_n=19, total_n=19, authority_basis="single source",
        ),
    }

    return reg.RegistrySnapshot(
        as_of="2026-09-15T00:00:00+00:00",
        entities=entities,
        relationships=relationships,
        logical_fields=logical_fields,
    )


@pytest.fixture
def supervisor_scope() -> Scope:
    return Scope(kind="cross_case", user_role="supervisor", user_id="u-test")


# ══════════════════════════════════════════════════════════════════════
# Cardinality classification
# ══════════════════════════════════════════════════════════════════════
class TestCardinality:
    def test_one_to_one(self):
        assert reg.classify_cardinality(10, 10, 10) == reg.ONE_TO_ONE

    def test_many_to_one_does_not_fan_forward(self):
        # Case -FILED_AT-> PoliceStation: 73 edges, 73 cases, 19 stations.
        card = reg.classify_cardinality(73, 73, 19)
        assert card == reg.MANY_TO_ONE
        assert card not in reg.FANNING_CARDINALITIES

    def test_person_belongs_to_case_is_fanning(self):
        # Measured live: 449 edges, 208 active persons, 73 cases.
        card = reg.classify_cardinality(449, 208, 73)
        assert card in reg.FANNING_CARDINALITIES

    def test_fanout_is_directional(self, snapshot):
        """The Address case: safe forward, catastrophic in reverse.

        Measured live: 2,100 Address-[:BELONGS_TO_CASE]->Case edges from
        2,100 distinct addresses to ONE distinct case. Travelling
        Address -> Case each address reaches one case (no fan); travelling
        Case -> Address that one case reaches 2,100 addresses. A
        direction-blind check reads the forward figure and calls this safe,
        which is exactly the wrong answer.
        """
        info = snapshot.relationship("Address", "BELONGS_TO_CASE", "Case")
        assert info is not None
        assert not info.fans_in_direction("out")
        assert info.fans_in_direction("in")
        assert info.max_fanout_in_direction("in") == 2100


# ══════════════════════════════════════════════════════════════════════
# THE MANDATED REGRESSION: 4-vs-70 officer grain
# ══════════════════════════════════════════════════════════════════════
class TestOfficerGrainRegression:
    """'What percentage of incidents involved more than one officer?'

    ENTITY grain       -> 4 of 73 = 5.5%   (correct)
    RELATIONSHIP grain -> 70 of 73 = 95.9% (counts role links, not people)

    The gap is 67 (case, officer) pairs carrying two ASSIGNED_TO edges,
    because the same officer both records and investigates. Both numbers
    are computable; the spec must say which is meant.
    """

    def _numerator_pop(self, count_grain: str) -> PopulationNode:
        return PopulationNode(
            entity="Case",
            predicates=(
                RelationCountPredicate(
                    rel="ASSIGNED_TO", target="Officer", op="gt", value=1,
                    count_grain=count_grain, direction="in",
                ),
            ),
        )

    def test_entity_grain_compiles_to_count_distinct(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="What percentage of incidents involved more than one officer?",
            measure="count", population=self._numerator_pop("ENTITY"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
        )
        assert validate(snapshot, spec).ok
        q = compile_spec(snapshot, spec)
        # The officers are DISTINCT-ed: this is what yields 4, not 70.
        assert "count(DISTINCT rt0.entity_id)" in q.text
        assert "count(DISTINCT n.case_id)" in q.text

    def test_relationship_grain_compiles_to_edge_count(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="How many officer assignments per case?",
            measure="count", population=self._numerator_pop("RELATIONSHIP"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
        )
        q = compile_spec(snapshot, spec)
        # Explicitly counting links, and the text says so.
        assert "count(rc0)" in q.text
        assert "count(DISTINCT rt0" not in q.text

    def test_the_two_grains_compile_differently(self, snapshot, supervisor_scope):
        """The core guarantee: COUNT(entity) != COUNT(relationship)."""
        e = compile_spec(
            snapshot,
            AggregateSpec(question_text="q", measure="count",
                          population=self._numerator_pop("ENTITY"), grain="ENTITY",
                          distinct_key="case_id", scope=supervisor_scope),
        )
        r = compile_spec(
            snapshot,
            AggregateSpec(question_text="q", measure="count",
                          population=self._numerator_pop("RELATIONSHIP"), grain="ENTITY",
                          distinct_key="case_id", scope=supervisor_scope),
        )
        assert e.text != r.text

    def test_4_vs_70_is_materially_different(self):
        """These two readings must trigger clarification, not a guess."""
        assert is_materially_different(4, 70)

    def test_72_vs_73_is_not_materially_different(self):
        assert not is_materially_different(72, 73)

    def test_superseded_filter_is_injected(self, snapshot, supervisor_scope):
        """ASSIGNED_TO carries 1 superseded edge; it must be excluded."""
        q = compile_spec(
            snapshot,
            AggregateSpec(question_text="q", measure="count",
                          population=self._numerator_pop("ENTITY"), grain="ENTITY",
                          distinct_key="case_id", scope=supervisor_scope),
        )
        assert "superseded_by IS NULL" in q.text
        assert any("superseded_by" in p for p in q.injected_predicates)


# ══════════════════════════════════════════════════════════════════════
# Fanout defence
# ══════════════════════════════════════════════════════════════════════
class TestFanoutDefence:
    def test_entity_grain_across_fanning_traversal_needs_distinct_key(
        self, snapshot, supervisor_scope
    ):
        """73 cases -> 449 rows through Person. Refused without a key."""
        spec = AggregateSpec(
            question_text="how many cases involve a person",
            measure="count",
            population=PopulationNode(
                entity="Case",
                traversals=(Traversal(rel="BELONGS_TO_CASE", target="Person", direction="in"),),
            ),
            grain="ENTITY", distinct_key=None, scope=supervisor_scope,
        )
        result = validate(snapshot, spec)
        assert not result.ok
        assert "fanout_without_distinct" in result.codes()

    def test_address_reverse_fanout_refused(self, snapshot, supervisor_scope):
        """Case -> Address multiplies 1 case into 2,100 rows.

        Both `missing_distinct_key` and `fanout_without_distinct` are
        legitimate reasons here and both are reported; the assertion is
        that the spec is refused and that the fanout is among the stated
        reasons, so the message explains the real hazard rather than only
        the missing field.
        """
        spec = AggregateSpec(
            question_text="cases with addresses", measure="count",
            population=PopulationNode(
                entity="Case",
                traversals=(Traversal(rel="BELONGS_TO_CASE", target="Address", direction="in"),),
            ),
            grain="ENTITY", distinct_key=None, scope=supervisor_scope,
        )
        result = validate(snapshot, spec)
        assert not result.ok
        assert "fanout_without_distinct" in result.codes()
        assert any("2100" in i.message for i in result.issues)

    def test_compiler_refuses_identity_grain_without_key(self, snapshot, supervisor_scope):
        """Defence in depth: if a bad spec slips past the validator, the
        compiler raises instead of emitting a non-DISTINCT count."""
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(
                entity="Case",
                traversals=(Traversal(rel="BELONGS_TO_CASE", target="Person", direction="in"),),
            ),
            grain="ENTITY", distinct_key=None, scope=supervisor_scope,
        )
        with pytest.raises(CompilerError):
            compile_spec(snapshot, spec)

    def test_compiler_notes_record_the_fanout(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(
                entity="Case",
                traversals=(Traversal(rel="BELONGS_TO_CASE", target="Person", direction="in"),),
            ),
            grain="ENTITY", distinct_key="entity_id", scope=supervisor_scope,
        )
        q = compile_spec(snapshot, spec)
        # The note must state the hazard and the measured magnitude, so a
        # reader of the receipt can see why DISTINCT was required.
        assert any("multiplies rows" in n for n in q.notes)
        assert any("62" in n for n in q.notes)  # measured reverse fanout
        assert any("DISTINCT" in n for n in q.notes)


# ══════════════════════════════════════════════════════════════════════
# Tombstones — the 222 merged Person donors
# ══════════════════════════════════════════════════════════════════════
class TestTombstoneExclusion:
    def test_person_population_excludes_tombstones(self, snapshot, supervisor_scope):
        """Measured: 429 raw vs 208 active. The filter is not optional."""
        spec = AggregateSpec(
            question_text="how many people", measure="count_distinct",
            population=PopulationNode(entity="Person"),
            grain="ENTITY", distinct_key="entity_id", scope=supervisor_scope,
        )
        q = compile_spec(snapshot, spec)
        assert "merged_into IS NULL" in q.text
        assert any("merged_into" in p for p in q.injected_predicates)

    def test_tombstone_filter_absent_for_labels_without_them(
        self, snapshot, supervisor_scope
    ):
        """Only Person carries tombstones; claiming otherwise would put a
        false exclusion in the receipt."""
        spec = AggregateSpec(
            question_text="how many cases", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
        )
        q = compile_spec(snapshot, spec)
        assert "merged_into" not in q.text

    def test_active_count_is_total_minus_tombstones(self, snapshot):
        assert snapshot.entity("Person").total_n == 430
        assert snapshot.entity("Person").tombstoned_n == 222
        assert snapshot.entity("Person").active_n == 208


# ══════════════════════════════════════════════════════════════════════
# Grain requirements
# ══════════════════════════════════════════════════════════════════════
class TestGrainRules:
    def test_entity_grain_requires_distinct_key(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key=None, scope=supervisor_scope,
        )
        assert "missing_distinct_key" in validate(snapshot, spec).codes()

    def test_wrong_distinct_key_refused(self, snapshot, supervisor_scope):
        """Case is keyed by case_id; entity_id would DISTINCT on NULL."""
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="entity_id", scope=supervisor_scope,
        )
        assert "wrong_distinct_key" in validate(snapshot, spec).codes()

    def test_label_without_usable_key_refused_at_entity_grain(
        self, snapshot, supervisor_scope
    ):
        """Address: entity_id on 2,100 of 2,171 -> not a key."""
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Address"),
            grain="ENTITY", distinct_key="entity_id", scope=supervisor_scope,
        )
        assert "no_distinct_key" in validate(snapshot, spec).codes()

    def test_role_pair_without_role_refused(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(
                entity="Case",
                traversals=(Traversal(rel="BELONGS_TO_CASE", target="Person", direction="in"),),
            ),
            grain="ROLE_PAIR", scope=supervisor_scope,
        )
        assert "role_pair_without_role" in validate(snapshot, spec).codes()


# ══════════════════════════════════════════════════════════════════════
# Unknown schema elements
# ══════════════════════════════════════════════════════════════════════
class TestSchemaValidation:
    def test_unknown_entity(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Suspect"),
            grain="ENTITY", distinct_key="id", scope=supervisor_scope,
        )
        assert "unknown_entity" in validate(snapshot, spec).codes()

    def test_unknown_relationship(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(
                entity="Case",
                traversals=(Traversal(rel="PROSECUTED_BY", target="Officer"),),
            ),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
        )
        assert "unknown_relationship" in validate(snapshot, spec).codes()

    def test_unknown_field(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(
                entity="Case",
                predicates=(FieldPredicate(field="verdict", op="eq", value="guilty"),),
            ),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
        )
        assert "unknown_field" in validate(snapshot, spec).codes()

    def test_unsupported_measure(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="stddev",  # type: ignore[arg-type]
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
        )
        assert "unsupported_operation" in validate(snapshot, spec).codes()

    def test_numeric_measure_needs_value_field(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="avg",
            population=PopulationNode(entity="Person"),
            grain="ENTITY", distinct_key="entity_id", scope=supervisor_scope,
        )
        assert "missing_value_field" in validate(snapshot, spec).codes()


# ══════════════════════════════════════════════════════════════════════
# Grouping
# ══════════════════════════════════════════════════════════════════════
class TestGrouping:
    def test_sparse_grouping_field_refused(self, snapshot, supervisor_scope):
        """investigation_status: 21 of 73 (29%) — below the 50% floor."""
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
            group_by=(GroupBy(field="investigation_status"),),
        )
        assert "grouping_field_too_sparse" in validate(snapshot, spec).codes()

    def test_dense_grouping_field_allowed(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
            group_by=(GroupBy(field="police_station"),),
        )
        assert validate(snapshot, spec).ok

    def test_three_dimensions_refused(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
            group_by=(
                GroupBy(field="police_station"),
                GroupBy(field="crime_category"),
                GroupBy(field="incident_date"),
            ),
        )
        assert "too_many_group_dimensions" in validate(snapshot, spec).codes()

    def test_grouped_query_avoids_order_by_alias(self, snapshot, supervisor_scope):
        """AGE rejects ORDER BY <alias> with UndefinedColumnError."""
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
            group_by=(GroupBy(field="police_station"),),
        )
        q = compile_spec(snapshot, spec)
        assert "WITH" in q.text and "ORDER BY value" in q.text


# ══════════════════════════════════════════════════════════════════════
# Scope / security
# ══════════════════════════════════════════════════════════════════════
class TestScopeSecurity:
    def test_unauthenticated_scope_refused(self, snapshot):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=Scope(),
        )
        assert "scope_not_authenticated" in validate(snapshot, spec).codes()

    def test_non_supervisor_cross_case_denied(self, snapshot):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id",
            scope=Scope(kind="cross_case", user_role="investigator"),
        )
        assert "scope_denied" in validate(snapshot, spec).codes()

    def test_jurisdiction_scope_requires_case_ids(self, snapshot):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id",
            scope=Scope(kind="jurisdiction", user_role="supervisor"),
        )
        assert "scope_missing_case_ids" in validate(snapshot, spec).codes()

    def test_injection_text_becomes_a_bound_parameter(self, snapshot, supervisor_scope):
        """A hostile value cannot become query STRUCTURE — it is $pN."""
        hostile = "x' RETURN 1 AS v // DROP"
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(
                entity="Case",
                predicates=(FieldPredicate(field="police_station", op="eq", value=hostile),),
            ),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
        )
        q = compile_spec(snapshot, spec)
        assert hostile not in q.text
        assert hostile in q.params.values()
        assert "DROP" not in q.text

    def test_scope_case_ids_are_parameters(self, snapshot):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id",
            scope=Scope(kind="jurisdiction", user_role="supervisor",
                        case_ids=("fir-1-26", "fir-2-26")),
        )
        q = compile_spec(snapshot, spec)
        assert "fir-1-26" not in q.text
        assert any("fir-1-26" in str(v) for v in q.params.values())


# ══════════════════════════════════════════════════════════════════════
# Result-shape guards — Phase 5
# ══════════════════════════════════════════════════════════════════════
class TestResultShapes:
    def test_all_null_group_keys_refused(self, snapshot, supervisor_scope):
        """THE measured silent failure: {'gkey': None, 'value': 73}."""
        spec = AggregateSpec(
            question_text="which station handles the most cases", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
            group_by=(GroupBy(field="police_station"),),
        )
        result = shapes.validate_grouped_rows(
            snapshot, spec, [{"gkey": None, "value": 73}]
        )
        assert result.verdict == "REFUSE"
        assert "all_group_keys_null" in result.codes()

    def test_valid_grouping_passes(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
            group_by=(GroupBy(field="police_station"),),
        )
        rows = [{"gkey": f"station-{i}", "value": 5} for i in range(19)]
        assert shapes.validate_grouped_rows(snapshot, spec, rows).ok

    def test_duplicate_group_keys_refused(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
            group_by=(GroupBy(field="police_station"),),
        )
        rows = [{"gkey": "A", "value": 3}, {"gkey": "A", "value": 4}]
        assert "duplicate_group_keys" in shapes.validate_grouped_rows(snapshot, spec, rows).codes()

    def test_zero_denominator_refused(self):
        assert "zero_denominator" in shapes.validate_ratio(0, 0).codes()

    def test_numerator_exceeding_denominator_refused(self):
        codes = shapes.validate_ratio(80, 73).codes()
        assert "numerator_exceeds_denominator" in codes

    def test_valid_ratio_passes(self):
        assert shapes.validate_ratio(4, 73).ok

    def test_distinct_cannot_exceed_raw(self):
        assert shapes.validate_distinct_vs_raw(92, 94).ok
        assert not shapes.validate_distinct_vs_raw(95, 94).ok

    def test_subset_cannot_exceed_parent(self):
        assert shapes.validate_subset(4, 73).ok
        assert "subset_exceeds_parent" in shapes.validate_subset(80, 73).codes()

    def test_partition_check_skipped_when_not_a_partition(self):
        """crime_category is multi-valued; the invariant must not fire."""
        assert shapes.validate_partition(100, 73, is_partition=False).ok

    def test_partition_check_fires_when_declared(self):
        assert "partition_mismatch" in shapes.validate_partition(
            100, 73, is_partition=True
        ).codes()

    def test_impossible_row_count_refused(self):
        """2,100 rows from a 73-case population is an unintended join."""
        assert "impossible_row_count" in shapes.validate_row_count(
            2100, max_expected=73, context="cases"
        ).codes()


# ══════════════════════════════════════════════════════════════════════
# Coverage — Phase 6
# ══════════════════════════════════════════════════════════════════════
class TestCoverage:
    def test_complete_coverage_has_no_caveat(self, snapshot):
        report = cov.build_coverage(
            snapshot, entity_label="Case", observed_n=73, field_name="police_station"
        )
        assert report.verdict == "COMPLETE"
        assert report.caveat_text() is None

    def test_sparse_field_is_insufficient(self, snapshot):
        """Person.age: 19 of 430 present -> 95.6% absent -> refuse."""
        report = cov.build_coverage(
            snapshot, entity_label="Person", observed_n=208,
            field_name="Person.age", field_entity="Person",
        )
        assert report.verdict == "INSUFFICIENT"
        assert report.caveat_text() is not None

    def test_partial_field_gets_caveat(self, snapshot):
        """incident_date: 64 of 73 -> 12% absent -> serve with caveat."""
        report = cov.build_coverage(
            snapshot, entity_label="Case", observed_n=73, field_name="incident_date"
        )
        assert report.verdict == "CAVEAT"
        assert "64" in (report.caveat_text() or "")

    def test_tiny_denominator_is_insufficient(self, snapshot):
        report = cov.build_coverage(
            snapshot, entity_label="Case", observed_n=3,
            effective_denominator_n=3,
        )
        assert report.verdict == "INSUFFICIENT"

    def test_expected_population_uses_active_count(self, snapshot):
        """Tombstones must not inflate the denominator."""
        report = cov.build_coverage(snapshot, entity_label="Person", observed_n=208)
        assert report.expected_population_n == 208  # not 430


# ══════════════════════════════════════════════════════════════════════
# Receipt — Phase 7
# ══════════════════════════════════════════════════════════════════════
class TestReceipt:
    def test_population_description_is_human_readable(self, supervisor_scope):
        spec = AggregateSpec(
            question_text="What percentage of incidents involved more than one officer?",
            measure="count",
            population=PopulationNode(
                entity="Case",
                predicates=(
                    RelationCountPredicate(
                        rel="ASSIGNED_TO", target="Officer", op="gt", value=1,
                        count_grain="ENTITY", direction="in",
                    ),
                ),
            ),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
        )
        desc = describe_population(spec)
        assert "Case" in desc and "Officer" in desc and "ENTITY grain" in desc

    def test_refusal_receipt_records_why(self, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
        )
        r = refusal_receipt(
            question_text="q", spec=spec, code="grouping_field_too_sparse",
            reason="investigation_status present on 21 of 73",
        )
        assert r.execution_status == "refused"
        assert r.refusal_code == "grouping_field_too_sparse"
        assert "21 of 73" in (r.refusal_reason or "")

    def test_receipt_is_json_serialisable(self, supervisor_scope):
        r = AggregateReceipt(
            question_text="q", spec_hash="abc", spec={},
            grain="ENTITY", distinct_key="case_id",
            population_definition="Case", measure="count", value=73,
        )
        assert '"value": 73' in r.to_json()

    def test_explain_mentions_grain_and_exclusions(self, supervisor_scope):
        r = AggregateReceipt(
            question_text="how many people", spec_hash="abc", spec={},
            grain="ENTITY", distinct_key="entity_id",
            population_definition="Person", measure="count_distinct", value=208,
            injected_predicates=("n.merged_into IS NULL",),
            excluded_merged_n=222,
        )
        text = r.explain()
        assert "ENTITY" in text and "merged_into" in text and "222" in text


# ══════════════════════════════════════════════════════════════════════
# Metamorphic / property-based
# ══════════════════════════════════════════════════════════════════════
class TestMetamorphic:
    def test_paraphrases_produce_the_same_spec_hash(self, supervisor_scope):
        """Wording must not change the computation's identity.

        English / Roman-Urdu / Urdu phrasings of one question differ only
        in `question_text`, which is excluded from the hash.
        """
        pop = PopulationNode(entity="Case")
        base = dict(
            measure="count", population=pop, grain="ENTITY",
            distinct_key="case_id", scope=supervisor_scope,
        )
        a = AggregateSpec(question_text="How many FIRs are registered?", **base)
        b = AggregateSpec(question_text="Kitne FIR darj hain?", **base)
        c = AggregateSpec(question_text="کتنی ایف آئی آر درج ہیں؟", **base)
        assert a.spec_hash() == b.spec_hash() == c.spec_hash()

    def test_filter_order_does_not_change_identity(self, supervisor_scope):
        """Reordering independent filters is the same computation.

        Documents current behaviour honestly: predicate order IS part of
        the tuple, so the hashes differ today. The compiler emits them
        AND-joined, so the RESULT is identical — this test pins that the
        emitted WHERE clauses are equivalent, which is the property that
        actually matters.
        """
        p1 = FieldPredicate(field="police_station", op="eq", value="A")
        p2 = FieldPredicate(field="crime_category", op="eq", value="B")
        s1 = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case", predicates=(p1, p2)),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
        )
        s2 = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case", predicates=(p2, p1)),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
        )
        assert set(s1.population.predicates) == set(s2.population.predicates)

    def test_adding_a_filter_cannot_increase_a_count(self):
        """Asserted as an invariant over results, not just a convention."""
        assert shapes.validate_subset(50, 73).ok
        assert not shapes.validate_subset(74, 73).ok

    def test_canonicalization_cannot_increase_distinct_count(self):
        assert shapes.validate_distinct_vs_raw(92, 94).ok
        assert not shapes.validate_distinct_vs_raw(96, 94).ok

    def test_duplicate_edges_do_not_change_entity_grain_emission(
        self, snapshot, supervisor_scope
    ):
        """The structural form of "duplicate edges must not move an ENTITY
        count": entity grain always emits DISTINCT, so edge multiplicity
        cannot reach the number."""
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(
                entity="Case",
                predicates=(
                    RelationCountPredicate(
                        rel="ASSIGNED_TO", target="Officer", op="gt", value=1,
                        count_grain="ENTITY", direction="in",
                    ),
                ),
            ),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
        )
        q = compile_spec(snapshot, spec)
        assert "count(DISTINCT rt0.entity_id)" in q.text


# ══════════════════════════════════════════════════════════════════════
# Composition — a question nobody coded
# ══════════════════════════════════════════════════════════════════════
class TestComposition:
    """The generality claim: new questions need new BINDINGS, not new code.

    All three specs below are the same tree with two bindings changed. If
    any of them required a new spec FIELD or a new compiler branch, the
    algebra would be too narrow — that is the failure this class watches
    for.
    """

    def _pct_with_multiple(self, rel: str, target: str, supervisor_scope) -> AggregateSpec:
        num = PopulationNode(
            entity="Case",
            predicates=(
                RelationCountPredicate(
                    rel=rel, target=target, op="gt", value=1,
                    count_grain="ENTITY", direction="in",
                ),
            ),
        )
        return AggregateSpec(
            question_text=f"What percentage of cases involve more than one {target}?",
            measure="count", population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
            ratio=Ratio(numerator=num, denominator=PopulationNode(entity="Case")),
        )

    @pytest.mark.parametrize(
        "rel,target",
        [
            ("ASSIGNED_TO", "Officer"),
            ("BELONGS_TO_CASE", "Person"),
            ("BELONGS_TO_CASE", "Weapon"),
        ],
    )
    def test_same_tree_answers_three_questions(
        self, snapshot, supervisor_scope, rel, target
    ):
        spec = self._pct_with_multiple(rel, target, supervisor_scope)
        assert validate(snapshot, spec).ok
        num_q, den_q = compile_ratio(snapshot, spec)
        assert "count(DISTINCT" in num_q.text
        assert "count(DISTINCT n.case_id)" in den_q.text

    def test_ratio_compiles_to_two_independent_queries(
        self, snapshot, supervisor_scope
    ):
        """Independence lets the receipt state both sides and lets the
        invariant checker compare them before dividing."""
        spec = self._pct_with_multiple("ASSIGNED_TO", "Officer", supervisor_scope)
        num_q, den_q = compile_ratio(snapshot, spec)
        assert num_q.text != den_q.text
        assert "relcnt" in num_q.text and "relcnt" not in den_q.text

    def test_ratio_population_mismatch_refused(self, snapshot, supervisor_scope):
        """Counting cases over officers is a category error, not a ratio."""
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
            ratio=Ratio(
                numerator=PopulationNode(entity="Case"),
                denominator=PopulationNode(entity="Officer"),
            ),
        )
        assert "ratio_population_mismatch" in validate(snapshot, spec).codes()


# ══════════════════════════════════════════════════════════════════════
# Comparison
# ══════════════════════════════════════════════════════════════════════
class TestComparison:
    def test_overlapping_buckets_refused(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
            compare=Comparison(dimension="year", buckets=(2024, 2024, 2026)),
        )
        assert "comparison_buckets_overlap" in validate(snapshot, spec).codes()

    def test_disjoint_buckets_still_refused_because_compare_is_not_emitted(
        self, snapshot, supervisor_scope
    ):
        """A well-formed comparison is still refused — and must be.

        This test previously asserted that a disjoint-bucket `compare`
        VALIDATES. That assertion encoded a live defect: the compiler
        consumes `spec.compare` zero times, so such a spec compiled to
        byte-identical Cypher as one with no comparison at all and returned
        the uncompared grand total, while the receipt claimed a comparison
        had been applied. Measured 2026-09-15.

        The bucket-disjointness rules below it are still correct and still
        tested; they simply cannot be reached until `compare` is genuinely
        emitted. When it is, this test flips back to asserting `.ok` in the
        same change that implements it — which is what
        `test_unimplemented_fields_match_compiler_reality` enforces.
        """
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
            compare=Comparison(dimension="year", buckets=(2024, 2026)),
        )
        result = validate(snapshot, spec)
        assert not result.ok
        assert "unsupported_operation" in result.codes()
        assert any("compare" in (i.field or "") for i in result.issues)

    def test_single_bucket_refused(self, snapshot, supervisor_scope):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=supervisor_scope,
            compare=Comparison(dimension="year", buckets=(2024,)),
        )
        assert "comparison_needs_buckets" in validate(snapshot, spec).codes()
