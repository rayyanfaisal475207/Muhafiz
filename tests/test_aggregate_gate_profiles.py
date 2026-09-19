"""
Phase 7B — trusted gate catalogue, profile selection policy, gate evaluation.

THE SECURITY PROPERTY THESE DEFEND. A model may SELECT an approved
validation profile; it may not create one, and it may not weaken one. The
minimum profile is derived from the TYPED spec before the model's choice is
read, so a deliberately weak selection cannot cause a requested constraint
to go unchecked. `test_weak_selection_cannot_skip_a_constraint` is the
direct statement of that.

Gates are binary eligibility, separate from confidence: a failed required
gate blocks the result and is never offset by a strong confidence vector.
"""
import pytest

from src.pipeline.aggregate import gate_evaluator as gateval
from src.pipeline.aggregate import gates as G
from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate.spec import (
    AggregateSpec,
    FieldPredicate,
    GroupBy,
    PopulationNode,
    Ratio,
    Scope,
    TimeWindow,
    Traversal,
)

P = G.GateProfileId


def _prop(name, present, total):
    return reg.PropertyInfo(name=name, present_n=present, total_n=total)


@pytest.fixture
def snapshot() -> reg.RegistrySnapshot:
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
            },
            has_tombstones=True, tombstoned_n=222,
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
        as_of="2026-09-16T00:00:00+00:00", entities=entities,
        relationships=relationships, logical_fields=logical,
    )


_SCOPE = Scope(kind="cross_case", user_role="supervisor", user_id="u1")


def _spec(**kw) -> AggregateSpec:
    """A minimal VALID spec.

    `distinct_key` is supplied because the existing validator refuses an
    ENTITY-grain spec without one ("what identifies one Case?"), which is
    correct pre-existing behaviour — the corpus's own `count_cases` passes
    `case_id` for exactly this reason. A fixture omitting it tests the
    validator, not the gate profiles.
    """
    base = dict(
        question_text="q", measure="count",
        population=PopulationNode(entity="Case"), grain="ENTITY",
        distinct_key="case_id", scope=_SCOPE,
    )
    base.update(kw)
    return AggregateSpec(**base)


_SIMPLE = _spec()
_FILTERED = _spec(population=PopulationNode(
    entity="Person",
    predicates=(FieldPredicate(field="Person.age", op="gte", value=20),),
))
_GROUPED = _spec(group_by=(GroupBy(field="PoliceStation.name"),))
_WINDOW = _spec(time_window=TimeWindow(
    field="incident_date", start="2024-01-01", end="2024-12-31"))
_MULTI_DISTINCT = _spec(
    measure="count_distinct", distinct_key="entity_id",
    population=PopulationNode(
        entity="Person",
        traversals=(Traversal(rel="BELONGS_TO_CASE", target="Case"),),
        predicates=(FieldPredicate(field="Person.age", op="gte", value=20),),
    ),
    time_window=TimeWindow(field="incident_date", start="2024-01-01"),
)
_RATIO = _spec(ratio=Ratio(
    numerator=PopulationNode(entity="Case"),
    denominator=PopulationNode(entity="Case"),
))


# ── §23 catalogue integrity ──────────────────────────────────────────

class TestGateCatalogue:
    def test_every_profile_id_has_an_entry(self):
        assert set(G.GateProfileId) == set(G.CATALOGUE)

    def test_profile_ids_are_unique_and_stable(self):
        ids = [p.value for p in G.GateProfileId]
        assert len(ids) == len(set(ids))

    def test_no_profile_references_an_unknown_gate(self):
        known = set(G.Gate)
        for profile in G.CATALOGUE.values():
            assert set(profile.required_gates) <= known

    def test_no_profile_repeats_a_gate(self):
        for profile in G.CATALOGUE.values():
            assert len(profile.required_gates) == len(set(profile.required_gates))

    def test_every_profile_requires_the_universal_gates(self):
        """Some checks are never optional, whatever the shape."""
        for profile in G.CATALOGUE.values():
            assert G.Gate.EXECUTION_SUCCESS in profile.required_gates
            assert G.Gate.POPULATION_IDENTIFIED in profile.required_gates
            assert G.Gate.GRAIN_VERIFIED in profile.required_gates

    def test_no_subjective_gate_exists(self):
        """Every gate must be measurable from recorded evidence."""
        names = {g.value for g in G.Gate}
        for banned in (
            "MODEL_SEEMS_CONFIDENT", "ANSWER_LOOKS_REASONABLE",
            "LLM_CONFIDENT", "PLAUSIBLE",
        ):
            assert banned not in names

    def test_catalogue_renders_into_the_prompt(self):
        rendered = G.render_catalogue_for_prompt()
        for profile in G.GateProfileId:
            assert profile.value in rendered

    def test_prompt_forbids_inventing_gates(self):
        rendered = G.render_catalogue_for_prompt().lower()
        assert "may not invent" in rendered or "not invent" in rendered

    def test_runtime_and_prompt_share_one_registry(self):
        """Prompt drift is prevented structurally, not by discipline."""
        import inspect

        src = inspect.getsource(G.render_catalogue_for_prompt)
        assert "CATALOGUE" in src


# ── §25 / approved selection policy ──────────────────────────────────

class TestSelectionPolicy:
    def test_1_exact_match_is_accepted_without_escalation(self, snapshot):
        reqs = G.requirements_from_spec(_SIMPLE, snapshot)
        out = G.validate_selection(P.SIMPLE_COUNT.value, reqs)
        assert out.accepted and not out.escalated
        assert out.effective_profile is P.SIMPLE_COUNT

    def test_2_underpowered_selection_is_escalated(self, snapshot):
        """The model cannot choose weaker gates than the query demands."""
        reqs = G.requirements_from_spec(_MULTI_DISTINCT, snapshot)
        out = G.validate_selection(P.FILTERED_COUNT.value, reqs)
        assert out.accepted
        assert out.escalated
        assert out.effective_profile is P.MULTI_SOURCE_FILTERED_DISTINCT_COUNT
        assert out.selected_profile is P.FILTERED_COUNT

    def test_2_escalation_is_recorded_as_a_validation_issue(self, snapshot):
        reqs = G.requirements_from_spec(_MULTI_DISTINCT, snapshot)
        out = G.validate_selection(P.SIMPLE_COUNT.value, reqs)
        assert out.issue is not None
        assert out.issue.code == "gate_profile_escalated"
        assert out.issue.field == "gate_profile_id"

    def test_2_escalation_enforces_the_stronger_gates(self, snapshot):
        """Escalation must actually change which gates run."""
        reqs = G.requirements_from_spec(_MULTI_DISTINCT, snapshot)
        out = G.validate_selection(P.SIMPLE_COUNT.value, reqs)
        weak = set(G.CATALOGUE[P.SIMPLE_COUNT].required_gates)
        effective = set(G.CATALOGUE[out.effective_profile].required_gates)
        assert weak < effective
        assert G.Gate.ALL_REQUESTED_CONSTRAINTS_APPLIED in effective

    def test_3_stronger_compatible_profile_is_never_downgraded(self, snapshot):
        reqs = G.requirements_from_spec(_MULTI_DISTINCT, snapshot)
        out = G.validate_selection(
            P.MULTI_SOURCE_FILTERED_DISTINCT_COUNT.value, reqs
        )
        assert out.accepted and not out.escalated
        assert out.effective_profile is P.MULTI_SOURCE_FILTERED_DISTINCT_COUNT

    @pytest.mark.parametrize("unknown", [
        "CUSTOM_GATE", "simple_count", "", "SUPER_PROFILE", "None",
    ])
    def test_4_unknown_profile_is_refused_not_inferred(self, snapshot, unknown):
        reqs = G.requirements_from_spec(_SIMPLE, snapshot)
        out = G.validate_selection(unknown, reqs)
        assert not out.accepted
        assert out.issue.code == "unknown_gate_profile"

    def test_5_ratio_profile_on_a_non_ratio_query_is_refused(self, snapshot):
        """A proportion and a count are different computations."""
        reqs = G.requirements_from_spec(_SIMPLE, snapshot)
        out = G.validate_selection(P.RATIO_OR_PERCENTAGE.value, reqs)
        assert not out.accepted
        assert out.issue.code == "incompatible_gate_profile"

    def test_5_count_profile_on_a_ratio_query_is_refused(self, snapshot):
        reqs = G.requirements_from_spec(_RATIO, snapshot)
        out = G.validate_selection(P.FILTERED_COUNT.value, reqs)
        assert not out.accepted
        assert out.issue.code == "incompatible_gate_profile"

    def test_5_grouped_profile_on_a_flat_query_is_corrected(self, snapshot):
        """PHASE 7D REPLACED THIS TEST'S EXPECTATION.

        Grouped-on-flat was a refusal. It is now a CORRECTION to the
        profile derived from the typed spec, because refusing destroyed
        valid answers: the corpus lost `min_person_age` (24) and
        `max_person_age` (49) when the planner labelled an ungrouped MIN
        as GROUPED_AGGREGATE. The spec was valid and the computation
        supported — only the label was wrong.

        Semantic incoherence (ratio vs count) still refuses, because
        substituting there would change what the answer MEANS. That
        distinction is asserted in `test_5_ratio_profile_...` below.
        """
        reqs = G.requirements_from_spec(_SIMPLE, snapshot)
        out = G.validate_selection(P.GROUPED_AGGREGATE.value, reqs)
        assert out.accepted
        assert out.corrected
        assert out.effective_profile is out.required_profile
        assert out.issue.code == "gate_profile_corrected"

    def test_no_selection_uses_the_derived_requirement(self, snapshot):
        reqs = G.requirements_from_spec(_MULTI_DISTINCT, snapshot)
        out = G.validate_selection(None, reqs)
        assert out.accepted
        assert out.effective_profile is P.MULTI_SOURCE_FILTERED_DISTINCT_COUNT

    @pytest.mark.parametrize("selected", [p.value for p in P] + [None])
    def test_invariant_effective_is_never_weaker_than_required(
        self, snapshot, selected
    ):
        """THE security invariant: effective >= minimum required, always."""
        for spec in (_SIMPLE, _FILTERED, _GROUPED, _WINDOW, _MULTI_DISTINCT, _RATIO):
            reqs = G.requirements_from_spec(spec, snapshot)
            out = G.validate_selection(selected, reqs)
            if not out.accepted:
                continue
            assert (
                G.CATALOGUE[out.effective_profile].rank
                >= G.CATALOGUE[out.required_profile].rank
            )

    def test_weak_selection_cannot_skip_a_constraint(self, snapshot):
        """A deliberately weak choice must not drop a required check."""
        reqs = G.requirements_from_spec(_MULTI_DISTINCT, snapshot)
        out = G.validate_selection(P.SIMPLE_COUNT.value, reqs)
        gates = set(G.CATALOGUE[out.effective_profile].required_gates)
        assert G.Gate.ALL_REQUESTED_CONSTRAINTS_APPLIED in gates
        assert G.Gate.NO_SILENT_BROADENING in gates


# ── §5 requirement derivation, from typed specs only ─────────────────

class TestRequirementDerivation:
    def test_simple_spec_needs_only_a_simple_profile(self, snapshot):
        reqs = G.requirements_from_spec(_SIMPLE, snapshot)
        assert not reqs.has_filters
        assert G.required_profile_for(reqs) is P.SIMPLE_COUNT

    def test_a_filter_raises_the_requirement(self, snapshot):
        reqs = G.requirements_from_spec(_FILTERED, snapshot)
        assert reqs.filter_count == 1
        assert G.required_profile_for(reqs) is P.FILTERED_COUNT

    def test_a_time_window_makes_the_query_multi_source(self, snapshot):
        """incident_date is Postgres-authoritative; the population is graph."""
        reqs = G.requirements_from_spec(_WINDOW, snapshot)
        assert reqs.requires_multiple_sources
        assert set(reqs.distinct_sources) == {"graph", "postgres"}

    def test_distinct_is_not_implied_by_entity_grain_alone(self, snapshot):
        """Regression: keying off grain made every spec 'distinct'.

        That pulled DISTINCT_KEY_VERIFIED and VERSION_RULES_APPLIED into
        flat counts that do not need them — stronger gates refusing valid
        queries.
        """
        reqs = G.requirements_from_spec(_WINDOW, snapshot)
        assert reqs.traversal_count == 0
        assert not reqs.requires_distinct_entity
        assert G.required_profile_for(reqs) is P.MULTI_SOURCE_FILTERED_COUNT

    def test_a_traversal_does_require_distinct(self, snapshot):
        """A fanning hop makes plain counting unsound — the 73->449 case."""
        reqs = G.requirements_from_spec(_MULTI_DISTINCT, snapshot)
        assert reqs.requires_distinct_entity
        assert G.required_profile_for(reqs) is P.MULTI_SOURCE_FILTERED_DISTINCT_COUNT

    def test_derivation_reads_no_model_output(self):
        """Checked against EXECUTABLE lines only.

        The docstring legitimately discusses what the model selected while
        explaining why this function ignores it; a whole-source scan would
        fail on its own documentation.
        """
        import inspect

        src = inspect.getsource(G.requirements_from_spec)
        code = "\n".join(
            ln for ln in src.splitlines() if not ln.strip().startswith("#")
        )
        if G.requirements_from_spec.__doc__:
            code = code.replace(G.requirements_from_spec.__doc__, "")
        for token in ("gate_profile_id", "payload", "selected_profile"):
            assert token not in code


# ── §14 / §15 gate evaluation ────────────────────────────────────────

class _Receipt:
    """Minimal stand-in carrying the fields the evaluator reads."""

    def __init__(self, **kw):
        self.source = kw.get("source", "graph")
        self.population_definition = kw.get("population_definition", "Case")
        self.grain = kw.get("grain", "ENTITY")
        self.distinct_key = kw.get("distinct_key", "case_id")
        self.injected_predicates = kw.get("injected_predicates", ())
        self.excluded_merged_n = kw.get("excluded_merged_n", 0)
        self.excluded_superseded_n = kw.get("excluded_superseded_n", 0)
        self.filters_applied = kw.get("filters_applied", ())
        self.filters_dropped = kw.get("filters_dropped", ())
        self.execution_status = kw.get("execution_status", "ok")
        self.coverage_verdict = kw.get("coverage_verdict", "COMPLETE")
        self.coverage = kw.get("coverage")
        self.group_keys = kw.get("group_keys", ())
        self.denominator_n = kw.get("denominator_n")
        self.refusal_reason = kw.get("refusal_reason")


class TestGateEvaluation:
    def test_a_clean_simple_count_passes(self):
        out = gateval.evaluate_gates(P.SIMPLE_COUNT, receipt=_Receipt())
        assert out.overall_passed, out.summary()

    def test_dropped_filters_fail_the_constraint_gate(self):
        receipt = _Receipt(filters_dropped=("incident_date window",))
        out = gateval.evaluate_gates(
            P.FILTERED_COUNT, receipt=receipt,
            requirements=G.QueryRequirements(filter_count=1),
        )
        assert not out.overall_passed
        assert G.Gate.ALL_REQUESTED_CONSTRAINTS_APPLIED in out.failed_required_gates
        assert G.Gate.NO_SILENT_BROADENING in out.failed_required_gates

    def test_failed_execution_fails_the_execution_gate(self):
        receipt = _Receipt(execution_status="failed")
        out = gateval.evaluate_gates(P.SIMPLE_COUNT, receipt=receipt)
        assert not out.overall_passed
        assert G.Gate.EXECUTION_SUCCESS in out.failed_required_gates

    def test_insufficient_coverage_is_not_a_required_gate_failure(self):
        """PHASE 7D REPLACED THIS TEST'S EXPECTATION.

        It previously asserted that INSUFFICIENT coverage FAILED a required
        gate. The approved policy now serves such results with an explicit
        warning, so coverage is quality evidence rather than eligibility.
        A required gate blocks by definition; those two rules cannot both
        hold for one check.

        Phase 7C measured the contradiction directly — three cases recorded
        `COVERAGE_ACCEPTABLE: failed` and were served anyway, because the
        direct path did not enforce its gates. `COVERAGE_ACCEPTABLE` is now
        absent from every profile's required set.
        """
        receipt = _Receipt(coverage_verdict="INSUFFICIENT")
        out = gateval.evaluate_gates(P.SIMPLE_COUNT, receipt=receipt)
        assert out.overall_passed
        assert G.Gate.COVERAGE_ACCEPTABLE not in out.failed_required_gates

    def test_coverage_is_required_by_no_profile(self):
        for profile in G.CATALOGUE.values():
            assert G.Gate.COVERAGE_ACCEPTABLE not in profile.required_gates

    def test_caveat_coverage_passes(self):
        """CAVEAT is served today with a disclosure; the gate must not
        silently tighten that established behaviour."""
        receipt = _Receipt(coverage_verdict="CAVEAT")
        out = gateval.evaluate_gates(P.SIMPLE_COUNT, receipt=receipt)
        assert out.overall_passed

    def test_every_profile_still_requires_execution_success(self):
        """Removing coverage must not have loosened the real gates."""
        for profile in G.CATALOGUE.values():
            assert G.Gate.EXECUTION_SUCCESS in profile.required_gates
            assert G.Gate.SOURCE_AUTHORITY_RESOLVED in profile.required_gates

    def test_missing_version_rules_fail_a_distinct_profile(self):
        receipt = _Receipt(injected_predicates=(), excluded_merged_n=0)
        out = gateval.evaluate_gates(
            P.MULTI_SOURCE_FILTERED_DISTINCT_COUNT, receipt=receipt
        )
        assert G.Gate.VERSION_RULES_APPLIED in out.failed_required_gates

    def test_version_rules_read_injected_predicates_not_invariants(self):
        """`invariants_checked` records grouping/range checks, never
        version resolution — asserting on it would pass vacuously.

        Executable lines only: the docstring names `invariants_checked`
        precisely to explain why the code does not read it.
        """
        import inspect

        src = inspect.getsource(gateval._check_version_rules)
        code = "\n".join(
            ln for ln in src.splitlines() if not ln.strip().startswith("#")
        )
        if gateval._check_version_rules.__doc__:
            code = code.replace(gateval._check_version_rules.__doc__, "")
        assert "injected_predicates" in code
        assert "invariants_checked" not in code

    def test_multi_source_profile_fails_without_composition(self):
        out = gateval.evaluate_gates(
            P.MULTI_SOURCE_FILTERED_COUNT, receipt=_Receipt(), composite=None
        )
        assert not out.overall_passed
        assert G.Gate.STABLE_JOIN_KEYS in out.failed_required_gates
        assert G.Gate.SET_COMPOSITION_COMPLETE in out.failed_required_gates

    def test_evaluation_records_evidence_for_every_check(self):
        out = gateval.evaluate_gates(P.SIMPLE_COUNT, receipt=_Receipt())
        assert out.checks
        for check in out.checks:
            assert check.evidence

    def test_evaluation_serialises_for_provenance(self):
        out = gateval.evaluate_gates(P.SIMPLE_COUNT, receipt=_Receipt())
        d = out.to_dict()
        assert d["profile_id"] == P.SIMPLE_COUNT.value
        assert "checks" in d and d["checks"]
        assert "overall_passed" in d


# ── §16 gates stay separate from confidence ──────────────────────────

class TestGatesAreNotConfidence:
    def test_gate_evaluator_does_not_read_confidence(self):
        import inspect

        src = inspect.getsource(gateval)
        assert "ResultConfidenceEvidence" not in src
        assert "combine_weights" not in src

    def test_gate_checks_are_binary(self):
        out = gateval.evaluate_gates(P.SIMPLE_COUNT, receipt=_Receipt())
        for check in out.checks:
            assert isinstance(check.passed, bool)

    def test_no_scalar_score_exists_on_the_evaluation(self):
        import dataclasses

        fields = {f.name for f in dataclasses.fields(gateval.GateEvaluation)}
        for banned in ("score", "confidence", "weight", "rating"):
            assert banned not in fields


# ── §10 the model selects, it does not define ────────────────────────

class TestModelCannotInventPolicy:
    @pytest.mark.parametrize("field", [
        "custom_gates", "gate_expression", "gate_code", "custom_weights",
        "custom_thresholds", "gates", "gate_definition", "confidence_weight",
    ])
    def test_policy_fields_are_rejected_by_strict_parsing(self, field):
        from src.pipeline.aggregate import age_plan as ap

        with pytest.raises(Exception):
            ap.AgeQueryPlan(
                root_alias="a", root_label="Case", aggregate="COUNT",
                aggregate_alias="a", grain="ENTITY", **{field: "anything"},
            )

    def test_gate_profile_id_is_the_only_policy_field(self):
        from src.pipeline.aggregate import age_plan as ap

        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Case", aggregate="COUNT",
            aggregate_alias="a", grain="ENTITY",
            gate_profile_id="SIMPLE_COUNT",
        )
        assert plan.gate_profile_id == "SIMPLE_COUNT"

    def test_forbidden_field_list_covers_policy_invention(self):
        from src.pipeline.aggregate import age_plan as ap

        for field in ("custom_gates", "gate_expression", "custom_weights"):
            assert field in ap.FORBIDDEN_PLAN_FIELDS


# ── §18/§19 routing decision ─────────────────────────────────────────

class TestCompositeRouting:
    def _plan(self, snapshot, spec):
        from src.pipeline.aggregate import route_structured as rs

        reqs = G.requirements_from_spec(spec, snapshot)
        return rs._composite_plan_for(snapshot, spec, reqs)

    def test_simple_questions_stay_on_the_direct_path(self, snapshot):
        assert self._plan(snapshot, _SIMPLE) is None

    def test_single_source_filters_stay_direct(self, snapshot):
        assert self._plan(snapshot, _FILTERED) is None

    def test_grouped_stays_direct(self, snapshot):
        assert self._plan(snapshot, _GROUPED) is None

    def test_a_time_window_triggers_composite(self, snapshot):
        plan = self._plan(snapshot, _WINDOW)
        assert plan is not None
        assert any("window" in c.name for c in plan.constraints)

    def test_multi_constraint_question_carries_every_constraint(self, snapshot):
        plan = self._plan(snapshot, _MULTI_DISTINCT)
        assert plan is not None
        names = [c.name for c in plan.constraints]
        assert any("window" in n for n in names)
        assert any("filter" in n for n in names)
        assert any("population_path" in n for n in names)

    def test_ratio_falls_back_to_the_route_that_supports_it(self, snapshot):
        """The composite executor has no ratio emitter; falling back to the
        route that does is correct, not a silent broadening."""
        spec = _spec(
            ratio=Ratio(
                numerator=PopulationNode(entity="Case"),
                denominator=PopulationNode(entity="Case"),
            ),
            time_window=TimeWindow(field="incident_date", start="2024-01-01"),
        )
        assert self._plan(snapshot, spec) is None

    def test_routing_is_decided_by_authority_not_by_a_model(self):
        import inspect

        from src.pipeline.aggregate import route_structured as rs

        src = inspect.getsource(rs._composite_plan_for)
        assert "requires_multiple_sources" in src
        assert "gate_profile_id" not in src


# ── §20 no silent fallback ───────────────────────────────────────────

class TestNoSilentFallback:
    def test_composite_refusal_does_not_rerun_as_a_broader_query(self):
        """A failed composite must refuse, never re-execute unrestricted."""
        import inspect

        from src.pipeline.aggregate import route_structured as rs

        src = inspect.getsource(rs.run)
        composite_block = src.split("if plan is not None:")[1]
        refusal_first = composite_block.split("if not composite_result.ok:")[1]
        # The failure branch must return before reaching `execute(`.
        head = refusal_first.split("return")[0]
        assert "execute(snapshot" not in head
