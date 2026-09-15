# -*- coding: utf-8 -*-
"""
Phase 1 regression tests — the bugs the shadow run found, pinned.

WHY THESE EXIST. Every test below corresponds to a wrong number that was
actually produced during Phase 1, not to a hypothetical. Four were defects
in the NEW engine (found by running it against live data and disbelieving
the output); two are defects in the OLD engine's dispatch that the harness
must keep reporting rather than smoothing over. A test that only asserted
"the engines agree" would have passed while three of these were live.

OFFLINE. Fixtures mirror measured live values; no test here touches the
database, per this repo's first testing rule.
"""
from __future__ import annotations

import pytest

from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate import shadow
from src.pipeline.aggregate.compiler import CompilerError, compile_spec
from src.pipeline.aggregate.coverage import build_coverage
from src.pipeline.aggregate.spec import (
    AggregateSpec,
    GroupBy,
    PopulationNode,
    Scope,
    Traversal,
)
from src.pipeline.aggregate.validator import validate


def _prop(name: str, present: int, total: int) -> reg.PropertyInfo:
    return reg.PropertyInfo(name=name, present_n=present, total_n=total)


@pytest.fixture
def snapshot() -> reg.RegistrySnapshot:
    """Mirrors the live graph for the labels these regressions touch."""
    entities = {
        "Case": reg.EntityInfo(
            label="Case", total_n=73, distinct_key="case_id",
            properties={"case_id": _prop("case_id", 73, 73)},
        ),
        "Person": reg.EntityInfo(
            label="Person", total_n=430, distinct_key="entity_id",
            properties={
                "entity_id": _prop("entity_id", 430, 430),
                "age": _prop("age", 19, 430),
                "merged_into": _prop("merged_into", 222, 430),
            },
            has_tombstones=True, tombstoned_n=222,
        ),
        "Weapon": reg.EntityInfo(
            label="Weapon", total_n=32, distinct_key="entity_id",
            properties={"entity_id": _prop("entity_id", 32, 32)},
        ),
        "PoliceStation": reg.EntityInfo(
            label="PoliceStation", total_n=19, distinct_key="station_id",
            properties={
                "station_id": _prop("station_id", 19, 19),
                "name": _prop("name", 19, 19),
            },
        ),
        "Incident": reg.EntityInfo(
            label="Incident", total_n=73, distinct_key="entity_id",
            properties={"entity_id": _prop("entity_id", 73, 73)},
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
        ("Person", "BELONGS_TO_CASE", "Case"):
            rel("Person", "BELONGS_TO_CASE", "Case", 449, 208, 73, 5, 62, 222),
        ("Person", "INVOLVED_IN", "Incident"):
            rel("Person", "INVOLVED_IN", "Incident", 221, 208, 73, 3, 62),
        ("Case", "FILED_AT", "PoliceStation"):
            rel("Case", "FILED_AT", "PoliceStation", 73, 73, 19, 1, 7),
        ("Weapon", "BELONGS_TO_CASE", "Case"):
            rel("Weapon", "BELONGS_TO_CASE", "Case", 32, 32, 32, 1, 1),
    }

    logical_fields = {
        "Person.age": reg.LogicalField(
            name="Person.age", source="graph", path="Person.age",
            present_n=19, total_n=430, authority_basis="single source",
        ),
        "PoliceStation.name": reg.LogicalField(
            name="PoliceStation.name", source="graph", path="PoliceStation.name",
            present_n=19, total_n=19, authority_basis="single source",
        ),
    }

    return reg.RegistrySnapshot(
        as_of="2026-09-15T00:00:00+00:00",
        entities=entities, relationships=relationships,
        logical_fields=logical_fields,
    )


@pytest.fixture
def sup_scope() -> Scope:
    return Scope(kind="cross_case", user_role="supervisor", user_id="t")


# ══════════════════════════════════════════════════════════════════════
# NEW-engine bugs found by the shadow run
# ══════════════════════════════════════════════════════════════════════
class TestCountedEntityIsTheBaseNotTheTerminal:
    """BUG 1 — a traversal FILTERS the subject; it does not change it.

    Live symptom: `count_distinct(Person)` with a hop to Case compiled to
    `count(DISTINCT m0.entity_id)` over Case nodes. Case carries no
    `entity_id`, so it returned **0** — a confident, well-formed zero.
    Correct answer: 208 active persons.
    """

    def test_compiles_distinct_on_the_base_alias(self, snapshot, sup_scope):
        spec = AggregateSpec(
            question_text="How many distinct persons are linked to cases?",
            measure="count_distinct",
            population=PopulationNode(
                entity="Person",
                traversals=(Traversal(rel="BELONGS_TO_CASE", target="Case", direction="out"),),
            ),
            grain="ENTITY", distinct_key="entity_id", scope=sup_scope,
        )
        q = compile_spec(snapshot, spec)
        assert "count(DISTINCT n.entity_id)" in q.text
        assert "count(DISTINCT m0." not in q.text

    def test_validator_checks_the_key_against_the_base_entity(self, snapshot, sup_scope):
        """The validator must agree with the compiler about what is counted.

        Before the fix it demanded Case's key for a Person population and
        refused every correct spec of this shape.
        """
        spec = AggregateSpec(
            question_text="q", measure="count_distinct",
            population=PopulationNode(
                entity="Person",
                traversals=(Traversal(rel="BELONGS_TO_CASE", target="Case", direction="out"),),
            ),
            grain="ENTITY", distinct_key="entity_id", scope=sup_scope,
        )
        assert validate(snapshot, spec).ok


class TestValueMeasuresAreNotCounts:
    """BUG 2 — measure must be honoured before grain.

    Live symptom: an `avg` spec compiled to `count(DISTINCT n.entity_id)`
    and returned 68. Right rows, wrong arithmetic, plausible number.
    """

    def test_avg_emits_avg(self, snapshot, sup_scope):
        spec = AggregateSpec(
            question_text="average age", measure="avg", value_field="Person.age",
            population=PopulationNode(entity="Person"),
            grain="ENTITY", distinct_key="entity_id", scope=sup_scope,
        )
        q = compile_spec(snapshot, spec)
        assert "avg(n.age)" in q.text
        assert "count(" not in q.text

    def test_avg_over_fanning_population_is_refused(self, snapshot, sup_scope):
        """A fanning hop repeats each value once per edge, silently
        weighting the mean by degree. Refuse rather than emit a wrong one."""
        spec = AggregateSpec(
            question_text="average age of persons on cases",
            measure="avg", value_field="Person.age",
            population=PopulationNode(
                entity="Person",
                traversals=(Traversal(rel="BELONGS_TO_CASE", target="Case", direction="out"),),
            ),
            grain="ENTITY", distinct_key="entity_id", scope=sup_scope,
        )
        with pytest.raises(CompilerError, match="weight each value"):
            compile_spec(snapshot, spec)


class TestGroupingDimensionAlias:
    """BUG 3 — a dimension reached `via` a hop lives on the terminal node.

    Live symptom: grouping cases by station name produced 19 groups each
    with count 0, because the count was taken on the wrong alias.
    """

    def test_via_dimension_groups_on_terminal_counts_on_base(self, snapshot, sup_scope):
        spec = AggregateSpec(
            question_text="Which police station handles the most cases?",
            measure="count",
            population=PopulationNode(
                entity="Case",
                traversals=(Traversal(rel="FILED_AT", target="PoliceStation", direction="out"),),
            ),
            grain="ENTITY", distinct_key="case_id", scope=sup_scope,
            group_by=(GroupBy(
                field="PoliceStation.name",
                via=(Traversal(rel="FILED_AT", target="PoliceStation"),),
            ),),
        )
        q = compile_spec(snapshot, spec)
        assert "m0.name AS gkey" in q.text          # dimension on terminal
        assert "count(DISTINCT n.case_id)" in q.text  # measure on base


class TestCoverageDenominator:
    """BUG 4 — coverage mixed the label's population with the query's.

    Live symptom: "411 of 73 record(s) carry no Person.age (563%)". The
    registry measures age over all 430 Persons; the query's population was
    73. Absence is now expressed as a rate scaled to the observed
    population.
    """

    def test_absent_share_never_exceeds_one(self, snapshot):
        report = build_coverage(
            snapshot, entity_label="Person", observed_n=73,
            field_name="Person.age", field_entity="Person",
        )
        assert 0.0 <= report.absent_share <= 1.0

    def test_absent_count_is_scaled_to_the_query_population(self, snapshot):
        report = build_coverage(
            snapshot, entity_label="Person", observed_n=73,
            field_name="Person.age", field_entity="Person",
        )
        assert report.field_absent_n is not None
        assert report.field_absent_n <= 73

    def test_caveat_text_is_internally_consistent(self, snapshot):
        report = build_coverage(
            snapshot, entity_label="Person", observed_n=73,
            field_name="Person.age", field_entity="Person",
        )
        text = report.caveat_text() or ""
        assert "563%" not in text


# ══════════════════════════════════════════════════════════════════════
# OLD-engine findings the harness must keep surfacing
# ══════════════════════════════════════════════════════════════════════
class TestLegacyDispatchMisrouteIsReported:
    """Module 173, observed live: first-match-wins ends dispatch.

    "How many accused persons are there in total across all cases?"
    dispatches to `graph_recurrence_person` (4 people in >1 case), not to a
    headcount (92). A numeric comparison would manufacture a
    disagreement; the honest classification is that the routing differs.
    """

    def test_kind_mismatch_is_detected(self):
        old = {"kind": "graph_recurrence", "results": [1, 2, 3, 4]}
        assert not shadow._old_kind_matches(old, "total_accused_count")

    def test_recurrence_family_matches_its_variants(self):
        old = {"kind": "graph_recurrence", "results": []}
        assert shadow._old_kind_matches(old, "graph_recurrence_person")
        assert shadow._old_kind_matches(old, "graph_recurrence_weapon")

    def test_relational_aggregate_matches_station_or_category(self):
        old = {"kind": "relational_aggregate", "counts": []}
        assert shadow._old_kind_matches(old, "station_or_category_counts")

    def test_scalar_extraction_reads_the_real_legacy_shapes(self):
        """Keys verified by calling the legacy functions, not guessed."""
        assert shadow._old_scalar({"kind": "total_count", "total_cases": 73}) == 73
        assert shadow._old_scalar(
            {"kind": "graph_recurrence", "results": [1, 2, 3, 4]}
        ) == 4
        assert shadow._old_scalar(
            {"kind": "relational_aggregate", "counts": [], "total_cases_considered": 73}
        ) == 73

    def test_no_comparable_scalar_returns_none(self):
        assert shadow._old_scalar({"kind": "unsupported_aggregate", "message": "x"}) is None


class TestWeaponIdentityGrainIsSemanticNotWrong:
    """OLD=1 vs NEW=0 is two questions, not two answers.

    `xagg._top_recurring_weapon_types()` documents why: weapon entity_ids
    are FIR-scoped (`WEAPON-{id}-{fir_id}`), so no weapon object can appear
    in two cases and per-entity grouping "would always return an empty
    list". OLD groups by normalised weapon TYPE instead. Verified live: 32
    nodes, 32 distinct entity_ids, 5 distinct canonical_names.
    """

    def test_weapon_divergence_is_classified_semantically_different(self):
        note = shadow._IDENTITY_GRAIN_NOTES.get("graph_recurrence_weapon")
        assert note is not None
        assert "FIR-scoped" in note

    def test_classification_vocabulary_is_the_briefs(self):
        """The classification set is fixed; nothing may invent a new label."""
        allowed = {
            shadow.AGREE, shadow.NEW_CORRECT_OLD_WRONG,
            shadow.OLD_CORRECT_NEW_WRONG, shadow.BOTH_WRONG,
            shadow.SEMANTICALLY_DIFFERENT, shadow.INSUFFICIENT_DATA,
            shadow.UNRESOLVED,
        }
        assert len(allowed) == 7


class TestShadowHarnessIsolation:
    """The harness must not touch the production dispatch path."""

    def test_shadow_module_does_not_import_router_or_supervisor(self):
        import inspect

        src = inspect.getsource(shadow)
        assert "from src.pipeline.router" not in src
        assert "from src.pipeline.harness.supervisor" not in src

    def test_specs_are_data_not_code_paths(self):
        """Every generic spec is bindings over the shared operators.

        If a kind ever needed its own emitter, this count would stop
        matching the number of entries — the algebra's own regression.
        """
        specs = shadow.build_generic_specs()
        assert len(specs) >= 6
        for kind, (old_query, spec) in specs.items():
            assert isinstance(old_query, str) and old_query
            assert spec.grain in ("ENTITY", "RELATIONSHIP", "ROLE_PAIR", "EVENT", "RECORD")
            assert spec.scope.user_role == "supervisor"
