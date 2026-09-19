"""
Phase 7 — multi-path composition, canonicalization, normalization, consensus.

THE FAILURE THIS FILE EXISTS FOR. Phase 6 measured an AGE plan that could
not express a date restriction, silently dropped it, and answered 73 where
the truth was 51. Every test here defends one link in the chain that makes
that impossible: constraints resolve to an authoritative source, a
constraint that cannot be applied REFUSES, populations combine
deterministically by stable id, and nothing is normalised or combined until
semantic comparability has been proven.

Live-data tests are marked `requires_postgres` and gated on
RUN_POSTGRES_TESTS, matching tests/test_rls_integration.py.
"""
import os

import pytest

from src.pipeline.aggregate import authority as auth
from src.pipeline.aggregate import canonical as can
from src.pipeline.aggregate import confidence as conf
from src.pipeline.aggregate import consensus
from src.pipeline.aggregate import populations as pops
from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate.composite import (
    CompositeAggregatePlan,
    Constraint,
    Metric,
)
from src.pipeline.aggregate.routes import NumericResult, RouteResult


def _prop(name, present, total):
    return reg.PropertyInfo(name=name, present_n=present, total_n=total)


@pytest.fixture
def snapshot() -> reg.RegistrySnapshot:
    """Mirrors the measured graph for the shapes these tests exercise."""
    entities = {
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
        "StructuredRecord": reg.EntityInfo(
            label="StructuredRecord", total_n=713, distinct_key="record_id",
            properties={"record_id": _prop("record_id", 713, 713)},
        ),
        # A label whose key is NOT present on every node: the proof must fail.
        "Address": reg.EntityInfo(
            label="Address", total_n=2171, distinct_key="entity_id",
            properties={"entity_id": _prop("entity_id", 2100, 2171)},
        ),
        "Case": reg.EntityInfo(
            label="Case", total_n=73, distinct_key="case_id",
            properties={"case_id": _prop("case_id", 73, 73)},
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
        ("Person", "BELONGS_TO_CASE", "Case"):
            rel("Person", "BELONGS_TO_CASE", "Case", 449, 208, 73, 5, 62, 222),
        # Same rel type, DIFFERENT triple, not versioned — the per-triple
        # distinction the executor must honour.
        ("Weapon", "BELONGS_TO_CASE", "Case"):
            rel("Weapon", "BELONGS_TO_CASE", "Case", 32, 32, 32, 1, 1),
    }
    logical = {
        "incident_date": reg.LogicalField(
            name="incident_date", source="postgres",
            path="cases.incident_date", present_n=64, total_n=73,
            authority_basis="case rows are authoritative; the graph backfills",
        ),
        "police_station": reg.LogicalField(
            name="police_station", source="postgres",
            path="cases.police_station", present_n=73, total_n=73,
            authority_basis="measured on the case rows",
        ),
    }
    return reg.RegistrySnapshot(
        as_of="2026-09-16T00:00:00+00:00",
        entities=entities, relationships=relationships,
        logical_fields=logical,
    )


# ── §8 authority resolution ──────────────────────────────────────────

class TestAuthorityResolution:
    def test_date_resolves_to_postgres_not_the_graph(self, snapshot):
        d = auth.resolve_authority(
            snapshot, "incident_date", auth.ConstraintKind.TEMPORAL
        )
        assert d.source is auth.Source.POSTGRES
        assert d.physical_path == "cases.incident_date"
        assert d.basis

    def test_graph_property_resolves_to_graph(self, snapshot):
        d = auth.resolve_authority(
            snapshot, "Person.age", auth.ConstraintKind.PROPERTY
        )
        assert d.source is auth.Source.GRAPH
        assert d.present_n == 19 and d.total_n == 430

    def test_relationships_are_always_graph(self, snapshot):
        d = auth.resolve_authority(
            snapshot, "BELONGS_TO_CASE", auth.ConstraintKind.RELATIONSHIP
        )
        assert d.source is auth.Source.GRAPH

    def test_unknown_field_is_unresolved_not_guessed(self, snapshot):
        d = auth.resolve_authority(
            snapshot, "astrological_sign", auth.ConstraintKind.PROPERTY
        )
        assert d.source is auth.Source.UNRESOLVED
        assert not d.resolved
        assert d.reason

    def test_temporal_field_without_an_executor_is_unresolved(self, snapshot):
        """The registry may know a date field the executor cannot run."""
        d = auth.resolve_authority(
            snapshot, "report_datetime", auth.ConstraintKind.TEMPORAL
        )
        assert d.source is auth.Source.UNRESOLVED


# ── §10 deterministic population algebra ─────────────────────────────

class TestPopulationAlgebra:
    def test_intersection_requires_all_constraints_to_hold(self):
        a = pops.build("case_id", ["c1", "c2", "c3"], "date", "postgres")
        b = pops.build("case_id", ["c2", "c3", "c4"], "district", "graph")
        c = pops.build("case_id", ["c3", "c9"], "age", "graph")
        final, steps = pops.intersect_all([a, b, c])
        assert final.ids == ("c3",)
        assert len(steps) == 2

    def test_mixed_sources_are_marked_composite(self):
        a = pops.build("case_id", ["c1"], "date", "postgres")
        b = pops.build("case_id", ["c1"], "age", "graph")
        out, _ = pops.combine(a, b, pops.SetOp.INTERSECT)
        assert out.source == "composite"

    def test_union_and_difference_exist(self):
        a = pops.build("case_id", ["c1", "c2"], "a", "postgres")
        b = pops.build("case_id", ["c2", "c3"], "b", "postgres")
        assert pops.combine(a, b, pops.SetOp.UNION)[0].ids == ("c1", "c2", "c3")
        assert pops.combine(a, b, pops.SetOp.DIFFERENCE)[0].ids == ("c1",)

    def test_empty_population_is_a_real_answer(self):
        """An empty window means "nothing matched", not "filter unavailable"."""
        a = pops.build("case_id", ["c1", "c2"], "linked", "graph")
        empty = pops.build("case_id", [], "window_2025", "postgres")
        final, _ = pops.intersect_all([a, empty])
        assert final.ids == ()
        assert final.is_empty

    def test_ids_are_deduplicated_and_order_preserved(self):
        p = pops.build("case_id", ["c2", "c1", "c2"], "x", "postgres")
        assert p.ids == ("c2", "c1")

    def test_joining_different_identifiers_is_refused(self):
        """A case_id x entity_id join would silently yield nothing."""
        cases = pops.build("case_id", ["c1"], "date", "postgres")
        persons = pops.build("entity_id", ["p1"], "age", "graph")
        with pytest.raises(pops.PopulationKeyMismatch):
            pops.combine(cases, persons, pops.SetOp.INTERSECT)


# ── §12 no silent constraint dropping ────────────────────────────────

class TestConstraintCompleteness:
    def test_unapplied_constraint_forces_a_refusal(self, snapshot):
        """The Phase 6 failure class, as a unit invariant.

        A plan whose date cannot be resolved must REFUSE — never execute
        the remaining constraints and present the broader answer.
        """
        from src.pipeline.aggregate.composite_executor import _refuse_unapplied
        from src.pipeline.aggregate.composite import ConstraintExecution

        c_ok = Constraint(
            name="age", field="Person.age",
            kind=auth.ConstraintKind.PROPERTY, key="entity_id",
        )
        c_bad = Constraint(
            name="window", field="report_datetime",
            kind=auth.ConstraintKind.TEMPORAL, key="case_id",
        )
        plan = CompositeAggregatePlan(
            question_text="q", subject_label="Person", subject_key="entity_id",
            grain="ENTITY", metric=Metric.COUNT_DISTINCT,
            constraints=(c_ok, c_bad),
        )
        executions = (
            ConstraintExecution(
                c_ok,
                auth.resolve_authority(snapshot, "Person.age", auth.ConstraintKind.PROPERTY),
                pops.build("entity_id", ["p1"], "age", "graph"), applied=True,
            ),
            ConstraintExecution(
                c_bad,
                auth.resolve_authority(snapshot, "report_datetime", auth.ConstraintKind.TEMPORAL),
                applied=False, refusal_reason="no executable temporal authority",
            ),
        )
        refusal = _refuse_unapplied(plan, executions)
        assert refusal is not None
        assert refusal.status == "refused"
        assert refusal.value is None
        assert refusal.refusal_code == "constraint_not_applied"
        assert "window" in refusal.refusal_reason

    def test_all_applied_does_not_refuse(self, snapshot):
        from src.pipeline.aggregate.composite_executor import _refuse_unapplied
        from src.pipeline.aggregate.composite import ConstraintExecution

        c = Constraint(
            name="age", field="Person.age",
            kind=auth.ConstraintKind.PROPERTY, key="entity_id",
        )
        plan = CompositeAggregatePlan(
            question_text="q", subject_label="Person", subject_key="entity_id",
            grain="ENTITY", metric=Metric.COUNT_DISTINCT, constraints=(c,),
        )
        ex = (ConstraintExecution(
            c, auth.resolve_authority(snapshot, "Person.age", auth.ConstraintKind.PROPERTY),
            pops.build("entity_id", ["p1"], "age", "graph"), applied=True,
        ),)
        assert _refuse_unapplied(plan, ex) is None

    def test_there_is_no_ignored_state(self):
        """Structurally: a result reports applied and unapplied, nothing else."""
        from src.pipeline.aggregate.composite import CompositeResult

        assert hasattr(CompositeResult, "applied_constraints")
        assert hasattr(CompositeResult, "unapplied_constraints")

    def test_executor_injects_correctness_invariants(self):
        """Regression: a first version omitted these and returned 429 not 208."""
        import inspect

        from src.pipeline.aggregate import composite_executor as ce

        src = inspect.getsource(ce)
        assert "_tombstone_clause" in src
        assert "_supersession_clause" in src
        # Applied at the subject/case join, where the 429 came from.
        assert "_tombstone_clause(snapshot, subj" in src


# ── §21 provable semantic canonicalization ───────────────────────────

class TestSemanticCanonicalization:
    @pytest.mark.parametrize("label", ["Person", "Weapon", "StructuredRecord"])
    def test_count_equals_count_distinct_when_provable(self, snapshot, label):
        """No traversal + a unique, always-present key => equivalent."""
        r = can.canonicalize_count(
            snapshot, "count", label=label, traversal_count=0
        )
        assert r.applied
        assert r.canonical == can.DISTINCT_ENTITY_COUNT

    def test_a_single_traversal_blocks_the_proof(self, snapshot):
        """Any hop can multiply rows — the 73->449 defect."""
        r = can.canonicalize_count(
            snapshot, "count", label="Person", traversal_count=1
        )
        assert not r.applied
        assert "traverse" in r.basis

    def test_a_key_absent_on_some_nodes_blocks_the_proof(self, snapshot):
        """Address.entity_id is present on 2100 of 2171."""
        r = can.canonicalize_count(
            snapshot, "count", label="Address", traversal_count=0
        )
        assert not r.applied

    def test_unknown_subject_blocks_the_proof(self, snapshot):
        r = can.canonicalize_count(
            snapshot, "count", label=None, traversal_count=0
        )
        assert not r.applied

    def test_grain_is_never_canonicalized(self, snapshot):
        """RECORD vs ENTITY is a real disagreement about what a unit IS."""
        record_key, _ = can.canonical_comparable_key(
            snapshot, interpretation="count_distinct", grain="RECORD",
            grouping=(), label="StructuredRecord", traversal_count=0,
        )
        entity_key, _ = can.canonical_comparable_key(
            snapshot, interpretation="count", grain="ENTITY",
            grouping=(), label="StructuredRecord", traversal_count=0,
        )
        assert record_key != entity_key

    def test_other_metrics_are_untouched(self, snapshot):
        for metric in ("avg", "sum", "percentage", "min"):
            r = can.canonicalize_count(
                snapshot, metric, label="Person", traversal_count=0
            )
            assert not r.applied


# ── §18 typed numeric normalization ──────────────────────────────────

class TestNumericNormalization:
    def test_percentage_divides_by_one_hundred(self):
        assert conf.normalize(64, "percentage").normalized == pytest.approx(0.64)

    def test_count_normalizes_against_a_shared_denominator(self):
        n = conf.normalize(51, "count", denominator=73)
        assert n.normalized == pytest.approx(51 / 73)
        assert n.unit == "count"
        assert n.original == 51

    def test_count_without_a_denominator_is_refused(self):
        with pytest.raises(conf.NormalizationError):
            conf.normalize(51, "count")

    def test_average_has_no_invented_scale(self):
        """31 must not silently become 0.31."""
        with pytest.raises(conf.NormalizationError):
            conf.normalize(31.84, "avg")

    def test_original_units_are_preserved_for_presentation(self):
        n = conf.normalize(64, "percentage")
        assert n.original == 64 and n.unit == "percent"


# ── §15/§30 confidence evidence ──────────────────────────────────────

class TestConfidenceEvidence:
    def test_incomplete_constraints_make_a_result_non_combinable(self):
        class _Composite:
            applied_constraints = ("age",)
            unapplied_constraints = ("window",)

        ev = conf.evidence_from_route_result(
            "structured", RouteResult(route="structured", status="SUCCESS"),
            composite=_Composite(),
        )
        assert ev.constraint_completeness is conf.Completeness.INCOMPLETE
        assert not ev.combinable

    def test_complete_constraints_are_combinable(self):
        class _Composite:
            applied_constraints = ("age", "window")
            unapplied_constraints = ()

        ev = conf.evidence_from_route_result(
            "structured",
            RouteResult(
                route="structured", status="SUCCESS", result_shape="scalar",
                result=NumericResult(value=1, interpretation="count", grain="ENTITY"),
            ),
            composite=_Composite(),
        )
        assert ev.constraint_completeness is conf.Completeness.COMPLETE
        assert ev.combinable

    def test_failed_execution_is_not_combinable(self):
        ev = conf.evidence_from_route_result(
            "age",
            RouteResult(route="age_text2cypher", status="EXECUTION_ERROR"),
        )
        assert not ev.combinable

    def test_no_subjective_dimension_exists(self):
        """Confidence is measured evidence, not how assured the model sounded."""
        import dataclasses

        fields = {f.name for f in dataclasses.fields(conf.ResultConfidenceEvidence)}
        for banned in ("llm_confidence", "model_confidence", "certainty", "belief"):
            assert banned not in fields

    def test_scalar_weighting_is_not_invented(self):
        with pytest.raises(conf.ConsensusPolicyRequired):
            conf.combine_weights()


# ── §17/§20/§29 the consensus gates, in order ────────────────────────

def _numeric(route, value, interp="count", grain="ENTITY", provenance=None):
    return RouteResult(
        route=route, status="SUCCESS", result_shape="scalar",
        result=NumericResult(value=value, interpretation=interp, grain=grain),
        provenance=provenance or {},
    )


class TestConsensusGates:
    def test_different_grain_is_not_comparable(self, snapshot):
        a = _numeric("structured", 45, "count_distinct", "RECORD")
        b = _numeric("age", 45, "count", "ENTITY")
        out = consensus.evaluate(snapshot, a, b)
        assert out.eligibility is consensus.Eligibility.NOT_COMPARABLE
        assert out.left_normalized is None and out.right_normalized is None

    def test_incomparable_pairs_are_never_normalized(self, snapshot):
        a = _numeric("structured", 4, "count", "ENTITY")
        b = _numeric("age", 70, "count", "ROLE_PAIR")
        out = consensus.evaluate(snapshot, a, b)
        assert out.eligibility is consensus.Eligibility.NOT_COMPARABLE
        assert not out.combinable

    def test_comparable_but_incomplete_is_not_combinable(self, snapshot):
        class _Composite:
            applied_constraints = ("age",)
            unapplied_constraints = ("window",)

        a = _numeric("structured", 51, "count", "ENTITY")
        b = _numeric("age", 73, "count", "ENTITY")
        out = consensus.evaluate(
            snapshot, a, b, shared_denominator=73, left_composite=_Composite()
        )
        assert out.eligibility is consensus.Eligibility.NOT_COMBINABLE

    def test_comparable_count_without_denominator_has_no_scale(self, snapshot):
        a = _numeric("structured", 208, "count", "ENTITY",
                     {"plan": {"root_label": "Person"}})
        b = _numeric("age", 208, "count_distinct", "ENTITY",
                     {"plan": {"root_label": "Person", "patterns": []}})
        out = consensus.evaluate(snapshot, a, b)
        assert out.eligibility is consensus.Eligibility.NO_COMMON_SCALE

    def test_all_gates_pass_reaches_eligible_and_stops(self, snapshot):
        """The policy boundary: eligible, normalized, but NOT combined."""
        a = _numeric("structured", 51, "count", "ENTITY",
                     {"plan": {"root_label": "Case"}})
        b = _numeric("age", 51, "count_distinct", "ENTITY",
                     {"plan": {"root_label": "Case", "patterns": []}})
        out = consensus.evaluate(snapshot, a, b, shared_denominator=73)
        assert out.eligibility is consensus.Eligibility.ELIGIBLE
        assert out.awaiting_policy
        assert out.left_normalized.normalized == pytest.approx(51 / 73)
        with pytest.raises(conf.ConsensusPolicyRequired):
            consensus.combine(out)

    def test_canonicalization_lets_equal_counts_become_comparable(self, snapshot):
        """The Phase 6 208-case: count vs count_distinct, provably equal."""
        a = _numeric("structured", 208, "count_distinct", "ENTITY",
                     {"plan": {"root_label": "Person"}})
        b = _numeric("age", 208, "count", "ENTITY",
                     {"plan": {"root_label": "Person", "patterns": []}})
        out = consensus.evaluate(snapshot, a, b, shared_denominator=430)
        assert out.left_key == out.right_key
        assert out.eligibility is consensus.Eligibility.ELIGIBLE

    def test_combination_refuses_when_not_eligible(self, snapshot):
        a = _numeric("structured", 45, "count_distinct", "RECORD")
        b = _numeric("age", 45, "count", "ENTITY")
        out = consensus.evaluate(snapshot, a, b)
        with pytest.raises(conf.ConsensusPolicyRequired):
            consensus.combine(out)


# ── §21 reconciliation integration ───────────────────────────────────

class TestReconciliationCanonicalization:
    def test_provably_equal_counts_now_agree(self, snapshot):
        from src.pipeline.aggregate import reconcile

        s = _numeric("structured", 208, "count_distinct", "ENTITY",
                     {"plan": {"root_label": "Person"}})
        a = _numeric("age_text2cypher", 208, "count", "ENTITY",
                     {"plan": {"root_label": "Person", "patterns": []}})
        out = reconcile.reconcile(s, a, None, snapshot=snapshot)
        assert out.classification == reconcile.AGREEMENT
        assert out.value == 208

    def test_without_a_snapshot_behaviour_is_unchanged(self, snapshot):
        """Every existing caller must be unaffected."""
        from src.pipeline.aggregate import reconcile

        s = _numeric("structured", 208, "count_distinct", "ENTITY")
        a = _numeric("age_text2cypher", 208, "count", "ENTITY")
        out = reconcile.reconcile(s, a, None)
        assert out.classification == reconcile.SEMANTICALLY_DIFFERENT

    def test_traversed_results_stay_semantically_different(self, snapshot):
        """A hop can multiply rows; the spellings are not interchangeable."""
        from src.pipeline.aggregate import reconcile

        s = _numeric("structured", 92, "count_distinct", "ENTITY",
                     {"plan": {"root_label": "Person"}})
        a = _numeric("age_text2cypher", 208, "count", "ENTITY",
                     {"plan": {"root_label": "Person",
                               "patterns": [{"rel_type": "BELONGS_TO_CASE"}]}})
        out = reconcile.reconcile(s, a, None, snapshot=snapshot)
        assert out.classification == reconcile.SEMANTICALLY_DIFFERENT

    def test_real_conflicts_are_still_conflicts(self, snapshot):
        """4 vs 70 must never be laundered into agreement."""
        from src.pipeline.aggregate import reconcile

        s = _numeric("structured", 4, "count", "ENTITY",
                     {"plan": {"root_label": "Case"}})
        a = _numeric("age_text2cypher", 70, "count", "ENTITY",
                     {"plan": {"root_label": "Case", "patterns": []}})
        out = reconcile.reconcile(s, a, None, snapshot=snapshot)
        assert out.classification == reconcile.CONFLICT
        assert out.value is None


# ══════════════════════════════════════════════════════════════════════
# LIVE — skipped by default
# ══════════════════════════════════════════════════════════════════════

_live = pytest.mark.skipif(
    not os.environ.get("RUN_POSTGRES_TESTS"),
    reason="Needs the live Muhafiz database. Set RUN_POSTGRES_TESTS=1.",
)


@pytest.fixture
async def live_database():
    """Deliberately NOT implemented — see the note below.

    These tests need the real Muhafiz database. `conftest.py` prevents that
    twice over: it rewrites `DATABASE_URL` to a disposable name at import
    time, AND wraps `asyncpg.create_pool` in a guard
    (`_install_live_database_guard`) that raises `LiveDatabaseAccessError`
    on any DSN naming `muhafiz`.

    THAT GUARD IS NOT AN OBSTACLE TO ROUTE AROUND. It is the last line of
    defence between the test suite and the production graph, and this
    project has already lost production data once when a safety boundary
    was bypassed for convenience (see PHASE5A_SECURITY_CLEANUP_REPORT.md).
    Disabling it so six integration tests can run would trade a standing
    protection for a convenience, and whether the aggregate suite may reach
    production at all is a policy question rather than an implementation
    detail.

    THE BEHAVIOUR IS VERIFIED REGARDLESS. Every assertion in this class was
    executed against the live database via a standalone script during
    Phase 7 and produced the expected figures — 208 active persons, 28 in
    2024 cases, 9 aged 20-30, 1 satisfying all three constraints, and a
    refusal when a constraint could not be applied. What is unverified is
    only the pytest packaging of those checks.

    SUPERVISOR DECISION REQUIRED to enable them: either sanction a narrowly
    scoped opt-out of the conftest guard for `requires_postgres` tests, or
    point `TEST_DATABASE_URL` at a disposable copy (the Phase 5C evaluator
    database already exists for exactly this purpose) and rerun the
    composite against that instead.
    """
    pytest.skip(
        "live composite tests need the conftest production guard relaxed or a "
        "disposable target; SUPERVISOR DECISION REQUIRED (see fixture docstring)"
    )


def _person_plan(constraints):
    return CompositeAggregatePlan(
        question_text="multi-path integration",
        subject_label="Person", subject_key="entity_id",
        grain="ENTITY", metric=Metric.COUNT_DISTINCT, constraints=constraints,
    )


_AGE_20_30 = Constraint(
    name="age_20_30", field="Person.age",
    kind=auth.ConstraintKind.PROPERTY, key="entity_id",
    spec={"label": "Person", "property": "age", "id_property": "entity_id",
          "op": "between", "low": 20, "high": 30},
)
_LINKED = Constraint(
    name="in_a_case", field="BELONGS_TO_CASE",
    kind=auth.ConstraintKind.RELATIONSHIP, key="entity_id",
    spec={"label": "Person", "id_property": "entity_id",
          "path": [{"rel": "BELONGS_TO_CASE", "target": "Case",
                    "direction": "out"}]},
)
_DATE_2024 = Constraint(
    name="incident_2024", field="incident_date",
    kind=auth.ConstraintKind.TEMPORAL, key="case_id",
    spec={"start": "2024-01-01", "end": "2024-12-31"},
)
_UNRESOLVABLE = Constraint(
    name="report_window", field="report_datetime",
    kind=auth.ConstraintKind.TEMPORAL, key="case_id",
    spec={"start": "2024-01-01", "end": "2024-12-31"},
)


@pytest.mark.requires_postgres
@_live
class TestLiveMultiPath:
    """Ground truth measured independently against the live database."""

    @staticmethod
    async def _run(constraints):
        from src.pipeline.aggregate.composite_executor import execute_composite

        snapshot = await reg.get_registry()
        return await execute_composite(snapshot, _person_plan(constraints))

    async def test_relationship_only_excludes_tombstones(self, live_database):
        """208 active, not 429 — the invariant the first version omitted."""
        out = await self._run((_LINKED,))
        assert out.ok
        assert out.value == 208

    async def test_date_constraint_actually_restricts(self, live_database):
        """§27: 2024 must not broaden to every case."""
        out = await self._run((_LINKED, _DATE_2024))
        assert out.ok
        assert out.value == 28
        assert "incident_2024" in out.applied_constraints

    async def test_age_constraint_applies(self, live_database):
        out = await self._run((_AGE_20_30, _LINKED))
        assert out.ok
        assert out.value == 9

    async def test_all_three_constraints_intersect(self, live_database):
        """§26: age + relationship + date, no constraint dropped."""
        out = await self._run((_AGE_20_30, _LINKED, _DATE_2024))
        assert out.ok
        assert out.value == 1
        assert set(out.applied_constraints) == {
            "age_20_30", "in_a_case", "incident_2024"
        }
        assert out.unapplied_constraints == ()

    async def test_unresolvable_constraint_refuses(self, live_database):
        """§11/§12: never silently broaden."""
        out = await self._run((_AGE_20_30, _LINKED, _UNRESOLVABLE))
        assert out.status == "refused"
        assert out.value is None
        assert "report_window" in out.unapplied_constraints

    async def test_provenance_reproduces_the_computation(self, live_database):
        """§13: which constraint, which source, what survived."""
        out = await self._run((_AGE_20_30, _LINKED, _DATE_2024))
        prov = out.provenance()
        sources = {c["name"]: c["source"] for c in prov["constraints"]}
        assert sources["incident_2024"] == "postgres"
        assert sources["age_20_30"] == "graph"
        assert prov["final_population_n"] == 1
        assert prov["composition"]
