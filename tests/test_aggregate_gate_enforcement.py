"""
Phase 7D — regression tests for the four Phase 7C defects.

Each class here pins one defect the full-corpus run exposed. They are kept
separate from `test_aggregate_gate_profiles.py` so the failure they guard
against is named, not merged into a general profile suite.

D1  value aggregates (MIN/MAX/AVG) wrongly refused
D2  direct-path gate evaluation was advisory, never blocking
D3  multi-source profile accepted as "stronger" for single-source queries
D4  coverage blocked results the executor is allowed to serve
"""
import pytest

from src.pipeline.aggregate import gate_evaluator as gateval
from src.pipeline.aggregate import gates as G
from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate import route_structured as rs
from src.pipeline.aggregate.spec import (
    AggregateSpec,
    FieldPredicate,
    PopulationNode,
    Scope,
    TimeWindow,
    Traversal,
)

P = G.GateProfileId
_SCOPE = Scope(kind="cross_case", user_role="supervisor", user_id="u1")


def _prop(name, present, total):
    return reg.PropertyInfo(name=name, present_n=present, total_n=total)


@pytest.fixture
def snapshot() -> reg.RegistrySnapshot:
    entities = {
        "Person": reg.EntityInfo(
            label="Person", total_n=430, distinct_key="entity_id",
            properties={
                "entity_id": _prop("entity_id", 430, 430),
                "age": _prop("age", 19, 430),
            },
            has_tombstones=True, tombstoned_n=222,
        ),
        "Case": reg.EntityInfo(
            label="Case", total_n=73, distinct_key="case_id",
            properties={"case_id": _prop("case_id", 73, 73)},
        ),
    }
    relationships = {
        ("Person", "BELONGS_TO_CASE", "Case"): reg.RelationshipInfo(
            source_label="Person", rel_type="BELONGS_TO_CASE",
            target_label="Case", edge_n=449, distinct_sources_n=208,
            distinct_targets_n=73, max_fanout=5,
            cardinality=reg.classify_cardinality(449, 208, 73),
            max_reverse_fanout=62, is_versioned=True, superseded_n=222,
        ),
    }
    logical = {
        "incident_date": reg.LogicalField(
            name="incident_date", source="postgres",
            path="cases.incident_date", present_n=64, total_n=73,
            authority_basis="case rows are authoritative",
        ),
    }
    return reg.RegistrySnapshot(
        as_of="2026-09-17T00:00:00+00:00", entities=entities,
        relationships=relationships, logical_fields=logical,
    )


def _value_spec(measure, **kw):
    base = dict(
        question_text=f"{measure} age", measure=measure,
        value_field="Person.age", population=PopulationNode(entity="Person"),
        grain="ENTITY", distinct_key="entity_id", scope=_SCOPE,
    )
    base.update(kw)
    return AggregateSpec(**base)


class _Receipt:
    """Carries only the fields the gate evaluator reads."""

    def __init__(self, **kw):
        self.source = kw.get("source", "graph")
        self.population_definition = kw.get("population_definition", "Person")
        self.grain = kw.get("grain", "ENTITY")
        self.distinct_key = kw.get("distinct_key", "entity_id")
        self.injected_predicates = kw.get("injected_predicates", ())
        self.excluded_merged_n = kw.get("excluded_merged_n", 0)
        self.excluded_superseded_n = kw.get("excluded_superseded_n", 0)
        self.filters_applied = kw.get("filters_applied", ())
        self.filters_dropped = kw.get("filters_dropped", ())
        self.execution_status = kw.get("execution_status", "ok")
        self.coverage_verdict = kw.get("coverage_verdict", "COMPLETE")
        self.coverage = kw.get("coverage")
        self.caveat = kw.get("caveat")
        self.group_keys = kw.get("group_keys", ())
        self.denominator_n = kw.get("denominator_n")
        self.refusal_reason = kw.get("refusal_reason")


# ── D1 ───────────────────────────────────────────────────────────────

class TestD1ValueAggregates:
    """MIN/MAX/AVG must use an existing single-source profile.

    Phase 7C lost `min_person_age` (24) and `max_person_age` (49) because
    value measures had no profile and any mismatched selection refused.
    Profiles describe VALIDATION SHAPE; the metric operator is carried by
    the typed spec.
    """

    @pytest.mark.parametrize("measure", ["min", "max", "avg", "sum"])
    def test_ungrouped_value_aggregate_requires_a_single_source_profile(
        self, snapshot, measure
    ):
        reqs = G.requirements_from_spec(_value_spec(measure), snapshot)
        assert G.required_profile_for(reqs) is P.SIMPLE_COUNT
        assert not reqs.requires_multiple_sources

    @pytest.mark.parametrize("measure", ["min", "max", "avg", "sum"])
    def test_simple_profile_is_accepted_for_value_aggregates(
        self, snapshot, measure
    ):
        reqs = G.requirements_from_spec(_value_spec(measure), snapshot)
        out = G.validate_selection(P.SIMPLE_COUNT.value, reqs)
        assert out.accepted
        assert out.effective_profile is P.SIMPLE_COUNT

    def test_filtered_value_aggregate_requires_the_filtered_profile(
        self, snapshot
    ):
        spec = _value_spec("max", population=PopulationNode(
            entity="Person",
            predicates=(FieldPredicate(field="Person.age", op="gte", value=20),),
        ))
        reqs = G.requirements_from_spec(spec, snapshot)
        assert G.required_profile_for(reqs) is P.FILTERED_COUNT
        assert G.validate_selection(P.FILTERED_COUNT.value, reqs).accepted

    def test_no_value_aggregate_profile_was_added(self):
        """The approved decision was to REUSE existing profiles."""
        names = {p.value for p in P}
        for banned in ("VALUE_AGGREGATE", "MIN_MAX_AVG", "SCALAR_AGGREGATE"):
            assert banned not in names
        assert len(names) == 6

    def test_composite_does_not_claim_unsupported_value_metrics(self, snapshot):
        """The composite executor supports COUNT/COUNT_DISTINCT only.

        A multi-source value aggregate must not be routed to it — the
        route falls back rather than pretending MIN is composable.
        """
        spec = _value_spec("avg", time_window=TimeWindow(
            field="incident_date", start="2024-01-01", end="2024-12-31"))
        reqs = G.requirements_from_spec(spec, snapshot)
        assert reqs.requires_multiple_sources
        assert rs._composite_plan_for(snapshot, spec, reqs) is None


# ── D2 ───────────────────────────────────────────────────────────────

class TestD2RequiredGatesBlock:
    """A failed required gate must block on EVERY path.

    Phase 7C served five direct-path results whose required gates failed,
    because only the composite path enforced them.
    """

    def test_direct_path_has_a_blocking_check(self):
        import inspect

        src = inspect.getsource(rs.run)
        assert "not direct_evaluation.overall_passed" in src

    def test_composite_path_still_blocks(self):
        import inspect

        src = inspect.getsource(rs.run)
        assert "not evaluation.overall_passed" in src

    def test_failed_execution_gate_fails_the_evaluation(self):
        out = gateval.evaluate_gates(
            P.SIMPLE_COUNT, receipt=_Receipt(execution_status="failed")
        )
        assert not out.overall_passed
        assert G.Gate.EXECUTION_SUCCESS in out.failed_required_gates

    def test_a_valid_number_with_a_failed_gate_is_not_servable(self):
        """Execution produced a value, but a required gate failed.

        The evaluation must report failure so the route refuses; the value
        may remain in the receipt for debugging, never served as accepted.
        """
        receipt = _Receipt(
            execution_status="ok", grain=None,  # GRAIN_VERIFIED will fail
        )
        out = gateval.evaluate_gates(P.SIMPLE_COUNT, receipt=receipt)
        assert not out.overall_passed
        assert G.Gate.GRAIN_VERIFIED in out.failed_required_gates

    def test_dropped_filters_block_a_filtered_profile(self):
        out = gateval.evaluate_gates(
            P.FILTERED_COUNT,
            receipt=_Receipt(filters_dropped=("incident_date window",)),
            requirements=G.QueryRequirements(filter_count=1),
        )
        assert not out.overall_passed
        assert G.Gate.ALL_REQUESTED_CONSTRAINTS_APPLIED in out.failed_required_gates


# ── D3 ───────────────────────────────────────────────────────────────

class TestD3ProfileFamilyCompatibility:
    """More gates is not "stronger" — it is a different shape.

    A single-source result can never satisfy STABLE_JOIN_KEYS or
    SET_COMPOSITION_COMPLETE, so demanding them is incoherent rather than
    strict. Phase 7C hit this on two cases.
    """

    _SINGLE = G.QueryRequirements(
        filter_count=0, requires_multiple_sources=False,
        requires_distinct_entity=True, traversal_count=1,
    )
    _MULTI = G.QueryRequirements(
        filter_count=1, has_time_filter=True, requires_multiple_sources=True,
        distinct_sources=("graph", "postgres"),
    )

    @pytest.mark.parametrize("selected", [
        P.MULTI_SOURCE_FILTERED_COUNT.value,
        P.MULTI_SOURCE_FILTERED_DISTINCT_COUNT.value,
    ])
    def test_multi_source_profile_on_single_source_query_is_corrected(
        self, selected
    ):
        """A mislabelled shape is CORRECTED, not refused.

        An earlier version of this fix refused instead, and the corpus lost
        `accused_distinct_through_relationship` (92) and
        `active_persons_via_belongs_to_case` (208) — both of which Phase 7C
        had served correctly. The typed spec was valid and the computation
        supported; only the label was wrong, so refusing destroyed answers
        while correcting costs nothing.
        """
        out = G.validate_selection(selected, self._SINGLE)
        assert out.accepted
        assert out.corrected
        assert out.effective_profile is P.SIMPLE_COUNT
        assert out.issue.code == "gate_profile_corrected"

    def test_correction_never_lands_below_the_derived_requirement(self):
        """Correction replaces a label with the DERIVED requirement.

        It cannot weaken enforcement, because the replacement is computed
        from the typed spec before the model's choice is read.
        """
        out = G.validate_selection(
            P.MULTI_SOURCE_FILTERED_DISTINCT_COUNT.value, self._SINGLE
        )
        assert (
            G.CATALOGUE[out.effective_profile].rank
            >= G.CATALOGUE[out.required_profile].rank
        )

    def test_a_ratio_mislabel_still_refuses(self):
        """Semantic incoherence is not correctable.

        Substituting a count for a proportion would change what the answer
        MEANS, which is worse than refusing.
        """
        out = G.validate_selection(P.RATIO_OR_PERCENTAGE.value, self._SINGLE)
        assert not out.accepted
        assert out.issue.code == "incompatible_gate_profile"

    def test_single_source_result_never_requires_composition_gates(self):
        out = G.validate_selection(P.SIMPLE_COUNT.value, self._SINGLE)
        assert out.accepted
        gates = set(G.CATALOGUE[out.effective_profile].required_gates)
        assert G.Gate.STABLE_JOIN_KEYS not in gates
        assert G.Gate.SET_COMPOSITION_COMPLETE not in gates

    def test_genuine_multi_source_query_still_escalates(self):
        """Escalation within the compatible family is preserved."""
        out = G.validate_selection(P.FILTERED_COUNT.value, self._MULTI)
        assert out.accepted
        assert out.escalated
        assert out.effective_profile is P.MULTI_SOURCE_FILTERED_COUNT

    def test_escalation_adds_the_composition_gates_when_required(self):
        out = G.validate_selection(P.SIMPLE_COUNT.value, self._MULTI)
        gates = set(G.CATALOGUE[out.effective_profile].required_gates)
        assert G.Gate.STABLE_JOIN_KEYS in gates
        assert G.Gate.SET_COMPOSITION_COMPLETE in gates

    def test_filtered_escalation_within_family(self):
        """SIMPLE -> FILTERED when filters exist, same single-source family."""
        reqs = G.QueryRequirements(filter_count=2, requires_multiple_sources=False)
        out = G.validate_selection(P.SIMPLE_COUNT.value, reqs)
        assert out.accepted and out.escalated
        assert out.effective_profile is P.FILTERED_COUNT

    def test_a_stronger_profile_is_never_downgraded(self):
        out = G.validate_selection(
            P.MULTI_SOURCE_FILTERED_DISTINCT_COUNT.value, self._MULTI
        )
        assert out.accepted and not out.escalated
        assert out.effective_profile is P.MULTI_SOURCE_FILTERED_DISTINCT_COUNT


# ── D4 ───────────────────────────────────────────────────────────────

class TestD4CoverageIsAdvisory:
    """INSUFFICIENT coverage is served WITH a warning, never blocked."""

    def test_coverage_is_not_required_by_any_profile(self):
        for profile in G.CATALOGUE.values():
            assert G.Gate.COVERAGE_ACCEPTABLE not in profile.required_gates

    @pytest.mark.parametrize("verdict", ["COMPLETE", "CAVEAT", "INSUFFICIENT"])
    def test_no_coverage_verdict_blocks_evaluation(self, verdict):
        out = gateval.evaluate_gates(
            P.SIMPLE_COUNT, receipt=_Receipt(coverage_verdict=verdict)
        )
        assert out.overall_passed

    def test_insufficient_coverage_produces_a_coded_warning(self):
        warnings = rs._coverage_warnings(
            _Receipt(coverage_verdict="INSUFFICIENT")
        )
        assert warnings
        assert any(rs.INSUFFICIENT_COVERAGE_CODE in w for w in warnings)

    def test_the_warning_states_the_limitation_plainly(self):
        warnings = rs._coverage_warnings(
            _Receipt(coverage_verdict="INSUFFICIENT")
        )
        joined = " ".join(warnings).lower()
        assert "incomplete data coverage" in joined
        assert "may not represent" in joined

    def test_caveat_verdict_still_carries_its_existing_caveat(self):
        warnings = rs._coverage_warnings(
            _Receipt(coverage_verdict="CAVEAT", caveat="Computed over 19 of 430")
        )
        assert "Computed over 19 of 430" in " ".join(warnings)

    def test_complete_coverage_adds_no_warning(self):
        assert rs._coverage_warnings(_Receipt(coverage_verdict="COMPLETE")) == ()

    def test_insufficient_is_not_silently_relabelled(self):
        """The verdict must survive into provenance, not be softened."""
        import inspect

        src = inspect.getsource(rs.run)
        assert '"coverage_verdict": receipt.coverage_verdict' in src
