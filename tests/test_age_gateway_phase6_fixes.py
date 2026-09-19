"""
Phase 6 — regression tests for defects the 42-case corpus run exposed.

Each test here names a specific wrong behaviour observed in the live run
(`docs/aggregate-shadow/phase4_three_way_report.json`), not a hypothetical.
The corpus run is the only thing that could have found these: every one of
them passed the Phase 5D unit suite, because each needed either real data
(Urdu property values, 15-property labels) or a real model's plan choices
to surface.
"""
import pytest

from src.pipeline.aggregate import age_compiler
from src.pipeline.aggregate import age_plan as ap
from src.pipeline.aggregate import age_plan_validator as apv
from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate import route_age


def _prop(name, present, total):
    return reg.PropertyInfo(name=name, present_n=present, total_n=total)


@pytest.fixture
def snapshot() -> reg.RegistrySnapshot:
    """Mirrors the measured graph for the shapes these defects involve."""
    entities = {
        "Person": reg.EntityInfo(
            label="Person", total_n=430, distinct_key="entity_id",
            properties={
                "entity_id": _prop("entity_id", 430, 430),
                "as_of": _prop("as_of", 430, 430),
                "canonical_name": _prop("canonical_name", 430, 430),
                "confidence": _prop("confidence", 430, 430),
                "extraction_confidence": _prop("extraction_confidence", 430, 430),
                "name_skeleton": _prop("name_skeleton", 416, 430),
                "merged_at": _prop("merged_at", 222, 430),
                "merged_into": _prop("merged_into", 222, 430),
                "source_doc_id": _prop("source_doc_id", 206, 430),
                "cnic": _prop("cnic", 196, 430),
                "gender": _prop("gender", 119, 430),
                "father_name": _prop("father_name", 93, 430),
                "address_text": _prop("address_text", 92, 430),
                "phone": _prop("phone", 90, 430),
                # 15th of 15 by presence — the one a cap of 10 hid.
                "age": _prop("age", 19, 430),
            },
            has_tombstones=True, tombstoned_n=222,
        ),
        "Case": reg.EntityInfo(
            label="Case", total_n=73, distinct_key="case_id",
            properties={"case_id": _prop("case_id", 73, 73)},
        ),
        "PoliceStation": reg.EntityInfo(
            label="PoliceStation", total_n=19, distinct_key="station_id",
            properties={
                "station_id": _prop("station_id", 19, 19),
                "name": _prop("name", 19, 19),
            },
        ),
    }

    def rel(src, t, dst, edges, ds, dt, maxf, maxr=0, sup=0):
        return reg.RelationshipInfo(
            source_label=src, rel_type=t, target_label=dst, edge_n=edges,
            distinct_sources_n=ds, distinct_targets_n=dt, max_fanout=maxf,
            cardinality=reg.classify_cardinality(edges, ds, dt),
            max_reverse_fanout=maxr, is_versioned=sup > 0, superseded_n=sup,
        )

    relationships = {
        # Measured: 73 edges, 73 distinct cases, 19 stations. Does NOT fan
        # travelling out (1 station per case) but DOES travelling in (up to
        # 7 cases per station).
        ("Case", "FILED_AT", "PoliceStation"):
            rel("Case", "FILED_AT", "PoliceStation", 73, 73, 19, 1, 7),
        ("Person", "BELONGS_TO_CASE", "Case"):
            rel("Person", "BELONGS_TO_CASE", "Case", 449, 208, 73, 5, 62, 222),
    }
    return reg.RegistrySnapshot(
        as_of="2026-09-16T14:15:25+00:00",
        entities=entities, relationships=relationships, logical_fields={},
    )


class TestDuplicatedInvariantPredicate:
    """Observed on `accused_distinct_through_relationship`.

    The model legitimately expressed the tombstone rule itself as an
    IS_NULL filter; the compiler injects the same invariant
    unconditionally. Result:
    `WHERE a.merged_into IS NULL AND a.merged_into IS NULL`.
    Harmless to AGE, but it misreports what the plan asked for.
    """

    def test_model_supplied_invariant_is_not_duplicated(self, snapshot):
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Person",
            filters=(ap.Filter(alias="a", property="merged_into",
                               op=ap.Operator.IS_NULL),),
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="entity_id", grain=ap.Grain.ENTITY,
        )
        v = apv.validate_plan(plan, snapshot)
        assert v.ok, v.summary()
        compiled = age_compiler.compile_plan(plan, snapshot, v)
        assert compiled.cypher.count("a.merged_into IS NULL") == 1

    def test_the_invariant_is_still_injected_when_absent(self, snapshot):
        """De-duplication must not become "stop injecting"."""
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Person",
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="entity_id", grain=ap.Grain.ENTITY,
        )
        v = apv.validate_plan(plan, snapshot)
        compiled = age_compiler.compile_plan(plan, snapshot, v)
        assert "a.merged_into IS NULL" in compiled.cypher


class TestReverseFanoutIsDetected:
    """Observed on `cases_by_station_via_traversal`.

    `(PoliceStation)<-[:FILED_AT]-(Case)` fans travelling IN — up to 7
    cases per station — but `fanning` recorded only the SOURCE alias, so a
    plain COUNT on the fanned-to alias passed validation. The guard caught
    it downstream as `fanout_without_distinct`, which is defence in depth
    doing its job for a gap that should not have existed.
    """

    def _plan(self, aggregate, prop=None):
        return ap.AgeQueryPlan(
            root_alias="a", root_label="PoliceStation",
            patterns=(ap.MatchPattern(
                from_alias="a", from_label="PoliceStation",
                rel_type="FILED_AT", direction=ap.Direction.IN,
                to_alias="b", to_label="Case",
            ),),
            aggregate=aggregate, aggregate_alias="b", aggregate_property=prop,
            group_by=ap.GroupDimension(alias="a", property="station_id"),
            grain=ap.Grain.ENTITY,
        )

    def test_both_endpoints_of_a_fanning_hop_are_marked(self, snapshot):
        v = apv.validate_plan(self._plan(ap.Aggregate.COUNT), snapshot)
        assert {"a", "b"} <= set(v.fanning_aliases)

    def test_plain_count_on_the_fanned_to_alias_is_refused(self, snapshot):
        v = apv.validate_plan(self._plan(ap.Aggregate.COUNT), snapshot)
        assert "fanout_requires_distinct" in v.codes()

    def test_count_distinct_over_the_same_shape_still_passes(self, snapshot):
        """The tightening must not over-refuse the correct formulation."""
        v = apv.validate_plan(
            self._plan(ap.Aggregate.COUNT_DISTINCT, "case_id"), snapshot
        )
        assert v.ok, v.summary()


class TestSchemaCardExposesSparseProperties:
    """Observed on min/max/avg/sum `Person.age` — four wrong refusals.

    The planner said "the schema does not contain any properties recording
    age information". That was true of the CARD and false of the graph:
    `age` ranks 15th of Person's 15 properties by presence (19/430), and
    the cap was 10.
    """

    def test_age_is_visible_on_the_card(self, snapshot):
        # Phase 1: property names render bare ("property age") rather than
        # dotted (".age"). The assertion follows the rendering because the
        # thing under test is that the SPARSE PROPERTY IS VISIBLE, not how
        # it is punctuated — the dotted form was itself removed after the
        # planner copied it into a spec verbatim as `distinct_key='.cnic'`.
        card = route_age.build_schema_card(snapshot)
        assert "property age" in card

    def test_cap_admits_every_property_of_the_widest_label(self, snapshot):
        assert route_age._MAX_PROPS_PER_LABEL >= len(
            snapshot.entity("Person").properties
        )


class TestSchemaCardCarriesValueExamples:
    """Observed on `weapons_unlicensed_property_filter` (0 vs 30) and
    `malkhana_records` (0 vs 45).

    Both properties WERE on the card. What was missing were their VALUES:
    the data holds `'بغیر لائسنس'` and `'malkhana_register'`, and the
    planner bound plausible English guesses that matched nothing.
    """

    def test_examples_are_rendered_beside_the_property(self, snapshot):
        card = route_age.build_schema_card(
            snapshot, {("Person", "gender"): ["مرد", "عورت"]}
        )
        assert "values:" in card
        assert "مرد" in card

    def test_card_without_examples_is_still_valid(self, snapshot):
        """Examples are an aid; the card must build without them."""
        card = route_age.build_schema_card(snapshot)
        assert "NODE LABELS" in card
        assert "values:" not in card

    def test_examples_are_capped(self, snapshot):
        many = [f"v{i}" for i in range(50)]
        card = route_age.build_schema_card(
            snapshot, {("Person", "gender"): many}
        )
        assert "v0" in card
        assert "v49" not in card
        assert "..." in card


class TestParseFailureProvenanceIsDiagnosable:
    """Observed on `time_window_2024`.

    The stored raw response was exactly 500 characters and appeared to be a
    mid-JSON truncation — but the 500 was `raw[:500]` in this module's own
    provenance, so the real cause could not be determined from the report.
    A parse failure is precisely the case where the raw text is the only
    evidence.
    """

    def test_raw_response_slice_is_wide_enough_to_diagnose(self):
        import inspect

        src = inspect.getsource(route_age.run)
        assert "raw[:500]" not in src
        assert "raw_length" in src
