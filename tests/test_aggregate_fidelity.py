# -*- coding: utf-8 -*-
"""Spec fidelity and deterministic direction reconciliation.

These cover the two Phase 1 mechanisms that change whether an answer is
served at all, so the cases here are about behaviour at the boundary:
what is refused, what is corrected, and what is deliberately left alone.
"""
from __future__ import annotations

import pytest

from src.pipeline.aggregate import direction, fidelity
from src.pipeline.aggregate.nl_spec import DeclaredConstraint, SpecGeneration
from src.pipeline.aggregate.spec import (
    AggregateSpec,
    FieldPredicate,
    PopulationNode,
    Scope,
    Traversal,
)

SCOPE = Scope(kind="cross_case", user_role="supervisor", user_id="t")


def _spec(**kw) -> AggregateSpec:
    population = kw.pop("population", PopulationNode(entity="Person"))
    return AggregateSpec(
        question_text=kw.pop("question_text", "q"),
        measure=kw.pop("measure", "count_distinct"),
        population=population,
        grain=kw.pop("grain", "ENTITY"),
        scope=SCOPE,
        distinct_key=kw.pop("distinct_key", "entity_id"),
        **kw,
    )


def _gen(spec, constraints=(), outputs=()) -> SpecGeneration:
    return SpecGeneration(
        spec=spec,
        declared_constraints=tuple(constraints),
        requested_outputs=tuple(outputs),
    )


# ── Fidelity ──────────────────────────────────────────────────────────
class TestFidelity:
    def test_no_generation_makes_no_claim(self):
        """A caller-supplied spec declared nothing, so nothing is checked."""
        assert fidelity.check(_spec(), None).ok

    def test_empty_declaration_is_not_a_failure(self):
        """A model that omits the field must not refuse every question."""
        assert fidelity.check(_spec(), _gen(_spec())).ok

    def test_unfiltered_spec_with_no_constraints_is_fine(self):
        """'How many persons' genuinely imposes no condition."""
        spec = _spec()
        assert fidelity.check(spec, _gen(spec, [])).ok

    def test_applied_predicate_that_exists_passes(self):
        spec = _spec(
            population=PopulationNode(
                entity="Person",
                predicates=(FieldPredicate(field="gender", op="eq", value="x"),),
            )
        )
        gen = _gen(spec, [DeclaredConstraint("must be female", True, "predicate")])
        assert fidelity.check(spec, gen).ok

    def test_constraint_declared_applied_but_absent_is_refused(self):
        """The QA-022 shape: filter claimed, structure carries none."""
        spec = _spec()  # no predicates at all
        gen = _gen(spec, [DeclaredConstraint("risk score above 80", True, "predicate")])
        result = fidelity.check(spec, gen)
        assert not result.ok
        assert result.issues[0].code == "constraint_lost"

    def test_constraint_declared_unapplied_is_refused(self):
        """An honest 'I could not express this' must not become an answer."""
        spec = _spec()
        gen = _gen(
            spec,
            [DeclaredConstraint("role must be accused", False, "", "no such field")],
        )
        result = fidelity.check(spec, gen)
        assert not result.ok
        assert result.issues[0].code == "constraint_not_expressed"
        assert "no such field" in result.issues[0].message

    def test_unapplied_claim_is_dropped_when_the_spec_carries_it(self):
        """"Nothing will match" is an ANSWER, not a capability failure.

        Observed live: asked about a category that does not exist in the
        data, the model built the predicate correctly and THEN marked it
        unapplied, reasoning that no row holds that value. Refusing there
        turns every honest zero into a refusal and destroys the ability to
        answer "none" — which is exactly what the adversarial control
        question is for.
        """
        spec = _spec(
            population=PopulationNode(
                entity="Weapon",
                predicates=(
                    FieldPredicate(field="canonical_name", op="eq", value="unicorn"),
                ),
            )
        )
        gen = _gen(
            spec,
            [
                DeclaredConstraint(
                    "filter by name", False, "predicate",
                    "no weapon has that value",
                ),
            ],
        )
        assert fidelity.check(spec, gen).ok

    def test_unapplied_claim_still_refuses_when_the_spec_lacks_it(self):
        """The capability failure case must keep refusing."""
        spec = _spec()  # no predicates at all
        gen = _gen(
            spec,
            [DeclaredConstraint("filter by score", False, "predicate", "no such field")],
        )
        result = fidelity.check(spec, gen)
        assert not result.ok
        assert result.issues[0].code == "constraint_not_expressed"

    def test_role_is_counted_separately_from_traversal(self):
        """Traversing the edge does not satisfy a claim about the role on it.

        This is the QA-013 shape: the right relationship, no role filter.
        """
        spec = _spec(
            population=PopulationNode(
                entity="Person",
                traversals=(Traversal(rel="INVOLVED_IN", target="Incident"),),
            )
        )
        gen = _gen(
            spec,
            [
                DeclaredConstraint("involved in an incident", True, "traversal"),
                DeclaredConstraint("in the accused role", True, "role"),
            ],
        )
        result = fidelity.check(spec, gen)
        assert not result.ok
        assert any(i.code == "constraint_lost" for i in result.issues)

    def test_role_claim_passes_when_the_hop_carries_one(self):
        spec = _spec(
            population=PopulationNode(
                entity="Person",
                traversals=(
                    Traversal(
                        rel="INVOLVED_IN", target="Incident",
                        role_field="role", role_value="accused",
                    ),
                ),
            )
        )
        gen = _gen(
            spec,
            [
                DeclaredConstraint("involved in an incident", True, "traversal"),
                DeclaredConstraint("in the accused role", True, "role"),
            ],
        )
        assert fidelity.check(spec, gen).ok

    def test_plural_where_is_the_same_location(self):
        """Observed live: the same model wrote 'predicate' then 'predicates'.

        Spelling variation must not silently downgrade a claim to
        unverifiable — that would quietly disable the check.
        """
        spec = _spec()  # no predicates
        gen = _gen(spec, [DeclaredConstraint("a filter", True, "predicates")])
        assert not fidelity.check(spec, gen).ok

    def test_where_spelling_is_normalised_for_multiword_locations(self):
        spec = _spec()  # no time window
        gen = _gen(spec, [DeclaredConstraint("in 2026", True, "Time Window")])
        assert not fidelity.check(spec, gen).ok

    def test_unknown_where_is_not_held_against_the_spec(self):
        """An uncheckable claim proves nothing; it must not refuse either."""
        spec = _spec()
        gen = _gen(spec, [DeclaredConstraint("something", True, "nowhere")])
        assert fidelity.check(spec, gen).ok

    def test_counting_catches_a_dropped_filter_among_several(self):
        """Two predicates claimed, one present."""
        spec = _spec(
            population=PopulationNode(
                entity="Weapon",
                predicates=(FieldPredicate(field="license_status", op="eq", value="x"),),
            )
        )
        gen = _gen(
            spec,
            [
                DeclaredConstraint("is licensed", True, "predicate"),
                DeclaredConstraint("was seized", True, "predicate"),
            ],
        )
        assert not fidelity.check(spec, gen).ok


class TestSingleOutput:
    def test_one_output_is_fine(self):
        assert fidelity.check_single_output(_gen(_spec(), outputs=["count"])) is None

    def test_no_declaration_is_fine(self):
        assert fidelity.check_single_output(_gen(_spec())) is None

    def test_none_generation_is_fine(self):
        assert fidelity.check_single_output(None) is None

    def test_two_outputs_are_reported(self):
        issue = fidelity.check_single_output(
            _gen(_spec(), outputs=["how many officers", "how many cases"])
        )
        assert issue is not None
        assert issue.code == "multiple_outputs_requested"
        assert "how many cases" in issue.message


# ── Direction reconciliation ──────────────────────────────────────────
class _FakeSnapshot:
    """Only what `direction` uses: relationship(source, rel, target)."""

    def __init__(self, triples):
        self._t = set(triples)

    def relationship(self, source, rel, target):
        return object() if (source, rel, target) in self._t else None


PERSON_CASE = _FakeSnapshot({("Person", "BELONGS_TO_CASE", "Case")})


class TestDirectionReconciliation:
    def test_valid_direction_is_untouched(self):
        spec = _spec(
            population=PopulationNode(
                entity="Person",
                traversals=(
                    Traversal(rel="BELONGS_TO_CASE", target="Case", direction="out"),
                ),
            )
        )
        fixed, corrections = direction.reconcile_directions(PERSON_CASE, spec)
        assert corrections == ()
        assert fixed is spec

    def test_unique_reverse_is_corrected_and_recorded(self):
        spec = _spec(
            population=PopulationNode(
                entity="Person",
                traversals=(
                    Traversal(rel="BELONGS_TO_CASE", target="Case", direction="in"),
                ),
            )
        )
        fixed, corrections = direction.reconcile_directions(PERSON_CASE, spec)
        assert len(corrections) == 1
        assert fixed.population.traversals[0].direction == "out"
        assert corrections[0].from_direction == "in"
        assert corrections[0].to_direction == "out"

    def test_wrong_relationship_type_is_never_repaired(self):
        """Direction only. A wrong edge is a different question."""
        spec = _spec(
            population=PopulationNode(
                entity="Person",
                traversals=(
                    Traversal(rel="CITES", target="Case", direction="in"),
                ),
            )
        )
        fixed, corrections = direction.reconcile_directions(PERSON_CASE, spec)
        assert corrections == ()
        assert fixed is spec

    def test_ambiguous_both_orientations_is_left_alone(self):
        """Two real orientations are two populations; choosing is semantic."""
        both = _FakeSnapshot(
            {("A", "R", "B"), ("B", "R", "A")}
        )
        spec = _spec(
            population=PopulationNode(
                entity="A", traversals=(Traversal(rel="R", target="B", direction="in"),)
            )
        )
        fixed, corrections = direction.reconcile_directions(both, spec)
        # "in" resolves via (B,R,A), so the hop is already valid — untouched.
        assert corrections == ()
        assert fixed is spec

    def test_correction_changes_the_spec_hash(self):
        """The hash must identify the computation that actually ran."""
        spec = _spec(
            population=PopulationNode(
                entity="Person",
                traversals=(
                    Traversal(rel="BELONGS_TO_CASE", target="Case", direction="in"),
                ),
            )
        )
        fixed, _ = direction.reconcile_directions(PERSON_CASE, spec)
        assert fixed.spec_hash() != spec.spec_hash()

    def test_multi_hop_corrects_only_the_wrong_hop(self):
        """The walk advances by target, so hop 2 is checked from hop 1's end.

        Getting this wrong would either miss the second hop entirely or
        check it against the population's root label, which is a different
        triple and would "correct" a hop that was already right.
        """
        chain = _FakeSnapshot({
            ("Case", "FILED_AT", "PoliceStation"),
            ("PoliceStation", "PART_OF", "District"),
        })
        spec = _spec(
            distinct_key="case_id",
            population=PopulationNode(
                entity="Case",
                traversals=(
                    Traversal(rel="FILED_AT", target="PoliceStation", direction="out"),
                    Traversal(rel="PART_OF", target="District", direction="in"),
                ),
            ),
        )
        fixed, corrections = direction.reconcile_directions(chain, spec)
        assert len(corrections) == 1
        assert corrections[0].source_label == "PoliceStation"
        assert [t.direction for t in fixed.population.traversals] == ["out", "out"]

    def test_no_traversals_is_a_no_op(self):
        spec = _spec()
        fixed, corrections = direction.reconcile_directions(PERSON_CASE, spec)
        assert corrections == ()
        assert fixed is spec


# ── Edge role validation, driven by measurement ───────────────────────
class TestEdgeRoleValidation:
    """Role filters checked against the registry, not a hand-kept list.

    This replaced a literal table of two relationship names — the exact
    shape the brief prohibits — whose own justification ("the registry
    measures node properties, not edge properties") stopped being true once
    edge properties were measured. The value check closes a real hole: a
    role value outside the measured set previously validated as EXECUTABLE
    and would have returned a confident, plausible 0.
    """

    def _snapshot(self, props, values=None):
        from src.pipeline.aggregate.registry import (
            EntityInfo, PropertyInfo, RegistrySnapshot, RelationshipInfo,
        )

        rel = RelationshipInfo(
            source_label="A", rel_type="R", target_label="B", edge_n=10,
            distinct_sources_n=5, distinct_targets_n=5, max_fanout=2,
            cardinality="MANY_TO_MANY",
            properties={
                p: PropertyInfo(name=p, present_n=10, total_n=10) for p in props
            },
        )
        return RegistrySnapshot(
            as_of="t",
            entities={
                lab: EntityInfo(label=lab, total_n=5, distinct_key="entity_id")
                for lab in ("A", "B")
            },
            relationships={("A", "R", "B"): rel},
            logical_fields={},
            edge_property_values_map=values or {},
        )

    def _spec(self, field, value):
        return AggregateSpec(
            question_text="q", measure="count_distinct",
            population=PopulationNode(
                entity="A",
                traversals=(
                    Traversal(rel="R", target="B", direction="out",
                              role_field=field, role_value=value),
                ),
            ),
            grain="ENTITY", scope=SCOPE, distinct_key="entity_id",
        )

    def test_a_measured_edge_property_is_accepted(self):
        from src.pipeline.aggregate import validator

        snap = self._snapshot(["kind"])
        assert validator.validate(snap, self._spec("kind", None)).verdict == "EXECUTABLE"

    def test_an_unmeasured_edge_property_is_refused(self):
        from src.pipeline.aggregate import validator

        snap = self._snapshot(["kind"])
        result = validator.validate(snap, self._spec("nope", None))
        assert result.verdict == "UNSUPPORTED"
        assert result.issues[0].code == "unknown_edge_property"

    def test_a_value_outside_the_measured_set_is_refused(self):
        from src.pipeline.aggregate import validator

        snap = self._snapshot(
            ["kind"], {("A", "R", "B", "kind"): frozenset({"x", "y"})}
        )
        result = validator.validate(snap, self._spec("kind", "NOT_REAL"))
        assert result.verdict == "UNSUPPORTED"
        assert result.issues[0].code == "unknown_edge_property_value"

    def test_a_value_inside_the_measured_set_is_accepted(self):
        from src.pipeline.aggregate import validator

        snap = self._snapshot(
            ["kind"], {("A", "R", "B", "kind"): frozenset({"x", "y"})}
        )
        assert validator.validate(snap, self._spec("kind", "x")).verdict == "EXECUTABLE"

    def test_an_unenumerated_property_constrains_nothing(self):
        """Empty means "not measured", never "no values exist".

        Refusing a value because the set was too large to enumerate would
        reject real data.
        """
        from src.pipeline.aggregate import validator

        snap = self._snapshot(["kind"])  # no value map entry
        assert validator.validate(
            snap, self._spec("kind", "anything")
        ).verdict == "EXECUTABLE"

    def test_no_relationship_type_is_named_in_validator_code(self):
        """The hand-kept table is gone and must not come back.

        Comments may still name a relationship to explain a measured
        example — that is documentation, not behaviour. What must not exist
        is a relationship name the CODE branches on.
        """
        import inspect

        from src.pipeline.aggregate import validator

        source = inspect.getsource(validator)
        assert "_ROLE_BEARING_RELATIONSHIPS" not in source

        code_lines = [
            line for line in source.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ]
        code = "\n".join(code_lines)
        for rel in ("INVOLVED_IN", "ASSIGNED_TO", "BELONGS_TO_CASE", "CITES"):
            assert rel not in code, f"{rel} is branched on in validator code"


# ── Registry-backed case linking ──────────────────────────────────────
class TestRegistryCaseLink:
    """The subject -> Case bridge, resolved from measured data.

    `plan_link` could only report a link the PLAN declared, and a plan whose
    only constraint is a time window declares none — so the refusal said no
    path existed for subjects where the registry had measured one. These
    cover the three outcomes: unique (use it), several (refuse and name
    them), none (say so honestly).
    """

    def _snapshot(self, triples):
        from src.pipeline.aggregate.registry import (
            EntityInfo, RegistrySnapshot, RelationshipInfo,
        )

        rels = {}
        for src, rel, dst in triples:
            rels[(src, rel, dst)] = RelationshipInfo(
                source_label=src, rel_type=rel, target_label=dst,
                edge_n=1, distinct_sources_n=1, distinct_targets_n=1,
                max_fanout=1, cardinality="MANY_TO_ONE",
            )
        labels = {t[0] for t in triples} | {t[2] for t in triples}
        return RegistrySnapshot(
            as_of="t",
            entities={
                lab: EntityInfo(label=lab, total_n=1, distinct_key="entity_id")
                for lab in labels
            },
            relationships=rels,
            logical_fields={},
        )

    def test_unique_forward_link_is_used(self):
        from src.pipeline.aggregate.composite_executor import registry_case_link

        snap = self._snapshot([("Weapon", "BELONGS_TO_CASE", "Case")])
        link, reason = registry_case_link(snap, "Weapon")
        assert link == ("BELONGS_TO_CASE", "out")
        assert reason is None

    def test_unique_reverse_link_is_used(self):
        """Case -> subject is still a path; it is simply travelled backwards."""
        from src.pipeline.aggregate.composite_executor import registry_case_link

        snap = self._snapshot([("Case", "HAS_THING", "Thing")])
        link, reason = registry_case_link(snap, "Thing")
        assert link == ("HAS_THING", "in")
        assert reason is None

    def test_several_links_refuse_and_name_the_candidates(self):
        """Two edges are two populations; picking one silently is the bug."""
        from src.pipeline.aggregate.composite_executor import registry_case_link

        snap = self._snapshot([
            ("Incident", "BELONGS_TO_CASE", "Case"),
            ("Incident", "PART_OF", "Case"),
        ])
        link, reason = registry_case_link(snap, "Incident")
        assert link is None
        assert "BELONGS_TO_CASE" in reason and "PART_OF" in reason

    def test_no_link_reports_nothing_rather_than_guessing(self):
        from src.pipeline.aggregate.composite_executor import registry_case_link

        snap = self._snapshot([("Person", "KNOWS", "Person")])
        link, reason = registry_case_link(snap, "Person")
        assert link is None
        assert reason is None


# ── Coverage bookkeeping for non-numeric aggregates ───────────────────
class TestObservedRows:
    """`observed_n` is coverage bookkeeping, not the answer.

    It used to be `int(value or 0)` unconditionally, which raised on a
    min/max over a date — discarding a query that had already succeeded.
    The rule is about kinds of value, not about dates.
    """

    def _f(self):
        from src.pipeline.aggregate.executor import _observed_rows

        return _observed_rows

    def test_a_count_is_its_own_observed_figure(self):
        assert self._f()(73) == 73

    def test_zero_and_none_are_zero(self):
        assert self._f()(0) == 0
        assert self._f()(None) == 0

    def test_a_timestamp_is_one_observed_value(self):
        assert self._f()("2024-09-14T22:00:00Z") == 1

    def test_a_category_is_one_observed_value(self):
        assert self._f()("accused") == 1

    def test_a_bool_is_categorical_not_a_count(self):
        assert self._f()(True) == 1

    def test_a_negative_count_does_not_reach_coverage(self):
        assert self._f()(-5) == 0

    def test_counting_measures_are_distinguished_from_summaries(self):
        """A count's value IS a population size; a min's value is not.

        Conflating them made coverage announce "only 1 record qualifies"
        about a min() computed over the whole label — the crash fix traded
        an exception for a misleading caveat until this distinction existed.
        """
        from src.pipeline.aggregate.executor import _is_counting_measure

        assert _is_counting_measure("count")
        assert _is_counting_measure("count_distinct")
        for m in ("min", "max", "avg", "sum", "median"):
            assert not _is_counting_measure(m)


# ── Schema card ───────────────────────────────────────────────────────
class TestSchemaCardEdgeProperties:
    """Edge qualifiers must be visible AND usable exactly as written.

    The first version of this rendering wrote properties as "[r].role" to
    mark them as edge-resident. The planner copied the decoration into the
    spec as the field name, producing `role_field="[r].role"` — the card's
    display notation became part of the answer. Anything the card shows as
    a name must be a name that can be used verbatim.
    """

    def _card(self):
        from src.pipeline.aggregate import route_age
        from src.pipeline.aggregate.registry import (
            EntityInfo, PropertyInfo, RegistrySnapshot, RelationshipInfo,
        )

        rel = RelationshipInfo(
            source_label="Person", rel_type="INVOLVED_IN",
            target_label="Incident", edge_n=221,
            distinct_sources_n=206, distinct_targets_n=73,
            max_fanout=3, cardinality="MANY_TO_MANY",
            properties={
                "role": PropertyInfo(name="role", present_n=221, total_n=221),
            },
        )
        snapshot = RegistrySnapshot(
            as_of="t",
            entities={
                "Person": EntityInfo(
                    label="Person", total_n=430, distinct_key="entity_id"
                ),
            },
            relationships={("Person", "INVOLVED_IN", "Incident"): rel},
            logical_fields={},
        )
        return route_age.build_schema_card(
            snapshot,
            {("Person", "INVOLVED_IN", "Incident", "role"): ["accused", "witness"]},
        )

    def test_edge_property_name_appears(self):
        assert "role" in self._card()

    def test_no_display_decoration_in_the_name(self):
        assert "[r]." not in self._card()

    def test_edge_values_are_shown(self):
        card = self._card()
        assert "accused" in card and "witness" in card

    def test_node_property_names_carry_no_leading_dot(self):
        """Same failure class as the edge case, on the older notation.

        Node properties were rendered as ".cnic", and that arrived back as
        `distinct_key='.cnic'` — refused for naming a property the label
        does not have. Every name this card prints must be usable verbatim.
        """
        from src.pipeline.aggregate import route_age
        from src.pipeline.aggregate.registry import (
            EntityInfo, PropertyInfo, RegistrySnapshot,
        )

        snapshot = RegistrySnapshot(
            as_of="t",
            entities={
                "Person": EntityInfo(
                    label="Person", total_n=430, distinct_key="entity_id",
                    properties={
                        "cnic": PropertyInfo(name="cnic", present_n=196, total_n=430),
                    },
                ),
            },
            relationships={},
            logical_fields={},
        )
        card = route_age.build_schema_card(snapshot, {})
        assert "cnic" in card
        assert ".cnic" not in card
