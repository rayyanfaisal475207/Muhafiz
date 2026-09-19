"""
Phase 5D — the AGE safe-plan gateway: plan, validator, compiler, boundary.

WHAT THESE GUARD. The security argument changed shape in this phase.
Previously it was "generated Cypher is inspected before it runs" (Phase 4's
guard) and then "generated Cypher runs somewhere disposable" (Phase 5C's
isolated evaluator). Now it is:

    mutation is not representable in the model's output contract,
    and the trusted compiler has no emitter that could produce it.

That is a property of the TYPES and of the CODE, so these tests assert it
structurally — no forbidden field exists on the plan, no mutation token can
appear in any compiled output, no value can escape parameterisation — not
merely that some particular malicious input was caught.

The forbidden-token list is IMPORTED from `cypher_guard`, never retyped, so
the guard and these assertions cannot drift apart.
"""
import inspect
import re

import pytest

from src.pipeline.aggregate import age_compiler, age_execution
from src.pipeline.aggregate import age_plan as ap
from src.pipeline.aggregate import age_plan_validator as apv
from src.pipeline.aggregate import cypher_guard
from src.pipeline.aggregate import registry as reg


# ── fixtures ─────────────────────────────────────────────────────────

def _prop(name, present, total):
    return reg.PropertyInfo(name=name, present_n=present, total_n=total)


def _snapshot() -> reg.RegistrySnapshot:
    """A registry mirroring the measured production graph closely enough
    for the validator's real decisions (fanout, sparsity, tombstones)."""
    entities = {
        "Case": reg.EntityInfo(
            label="Case", total_n=73, distinct_key="case_id",
            properties={
                "case_id": _prop("case_id", 73, 73),
                "as_of": _prop("as_of", 73, 73),
            },
        ),
        "Person": reg.EntityInfo(
            label="Person", total_n=430, distinct_key="entity_id",
            properties={
                "entity_id": _prop("entity_id", 430, 430),
                "name": _prop("name", 430, 430),
                # Measured sparse: 19 of 430. The validator must refuse
                # grouping by it.
                "age": _prop("age", 19, 430),
                "merged_into": _prop("merged_into", 222, 430),
            },
            has_tombstones=True, tombstoned_n=222,
        ),
        "Officer": reg.EntityInfo(
            label="Officer", total_n=1155, distinct_key="entity_id",
            properties={
                "entity_id": _prop("entity_id", 1155, 1155),
                "name": _prop("name", 1155, 1155),
            },
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
        # Fans in reverse: one case reaches many persons.
        ("Person", "BELONGS_TO_CASE", "Case"):
            rel("Person", "BELONGS_TO_CASE", "Case", 449, 208, 73, 5, 62, 222),
        ("Officer", "ASSIGNED_TO", "Case"):
            rel("Officer", "ASSIGNED_TO", "Case", 144, 144, 73, 1, 6),
        ("Case", "FILED_AT", "PoliceStation"):
            rel("Case", "FILED_AT", "PoliceStation", 73, 73, 19, 1, 7),
    }
    return reg.RegistrySnapshot(
        as_of="2026-09-16T00:00:00+00:00",
        entities=entities, relationships=relationships, logical_fields={},
    )


@pytest.fixture
def snapshot():
    return _snapshot()


def _count_cases() -> ap.AgeQueryPlan:
    return ap.AgeQueryPlan(
        root_alias="a", root_label="Case",
        aggregate=ap.Aggregate.COUNT, aggregate_alias="a",
        grain=ap.Grain.ENTITY,
    )


def _compile(plan, snap):
    v = apv.validate_plan(plan, snap)
    assert v.ok, v.summary()
    return age_compiler.compile_plan(plan, snap, v)


# ══════════════════════════════════════════════════════════════════════
# The plan contract
# ══════════════════════════════════════════════════════════════════════

class TestPlanHasNoEscapeHatch:
    def test_no_forbidden_field_exists_on_the_schema(self):
        """If someone later adds `raw_where` for convenience, this fails."""
        fields = set(ap.AgeQueryPlan.model_fields)
        assert not (fields & ap.FORBIDDEN_PLAN_FIELDS)

    def test_nested_models_carry_no_forbidden_fields(self):
        for model in (ap.MatchPattern, ap.Filter, ap.GroupDimension):
            assert not (set(model.model_fields) & ap.FORBIDDEN_PLAN_FIELDS)

    @pytest.mark.parametrize("smuggled", [
        "cypher", "raw_query", "query_text", "raw_where", "raw_return",
        "clause", "custom_expression", "sql", "function_name",
        "graph_name", "database_name", "database_url", "graph", "role",
    ])
    def test_extra_fields_are_rejected_not_ignored(self, smuggled):
        """A dropped unknown field would be a silent escape hatch."""
        with pytest.raises(Exception):
            ap.AgeQueryPlan(
                root_alias="a", root_label="Case",
                aggregate="COUNT", aggregate_alias="a", grain="ENTITY",
                **{smuggled: "MATCH (n) DETACH DELETE n"},
            )

    def test_plan_is_frozen_so_nothing_mutates_it_post_validation(self):
        plan = _count_cases()
        with pytest.raises(Exception):
            plan.root_label = "Person"

    @pytest.mark.parametrize("bad_alias", [
        "n) RETURN 1 //", "a; DROP", "*", "", "a b",
    ])
    def test_alias_is_a_closed_set(self, bad_alias):
        with pytest.raises(Exception):
            ap.AgeQueryPlan(
                root_alias=bad_alias, root_label="Case",
                aggregate="COUNT", aggregate_alias="a", grain="ENTITY",
            )

    @pytest.mark.parametrize("bad", ["MEDIAN", "DELETE", "count", "EXEC"])
    def test_aggregate_is_a_closed_enum(self, bad):
        with pytest.raises(Exception):
            ap.AgeQueryPlan(
                root_alias="a", root_label="Case",
                aggregate=bad, aggregate_alias="a", grain="ENTITY",
            )


# ══════════════════════════════════════════════════════════════════════
# Adversarial input — mutation cannot become a plan
# ══════════════════════════════════════════════════════════════════════

class TestAdversarialInputsCannotBecomePlans:
    """Phase 5C's destructive proof showed AGE has no read-only mode. These
    assert the payloads that exploited that cannot even be expressed."""

    @pytest.mark.parametrize("payload", [
        {"cypher": "MATCH (n) DETACH DELETE n"},
        {"root_alias": "a", "root_label": "Case", "aggregate": "COUNT",
         "aggregate_alias": "a", "grain": "ENTITY",
         "raw_where": "1=1 OR true"},
        {"root_alias": "a", "root_label": "Case", "aggregate": "DELETE",
         "aggregate_alias": "a", "grain": "ENTITY"},
        {"root_alias": "a", "root_label": "Case", "aggregate": "COUNT",
         "aggregate_alias": "a", "grain": "ENTITY",
         "graph_name": "evidence_graph"},
        {"root_alias": "a", "root_label": "Case", "aggregate": "COUNT",
         "aggregate_alias": "a", "grain": "ENTITY",
         "database_url": "postgresql://u:p@h/muhafiz"},
    ])
    def test_mutation_and_target_selection_fail_parsing(self, payload):
        with pytest.raises(Exception):
            ap.AgeQueryPlan.model_validate(payload)

    @pytest.mark.parametrize("evasion", [
        "DETACH​DELETE",          # zero-width space
        "DELЕTE",                  # Cyrillic homoglyph E
        "dEtAcH dElEtE",                # mixed case
        "MATCH (n) /* comment */ DELETE n",
        "MATCH\t(n)\nDELETE\rn",        # odd whitespace
        "$cypher$ ; DROP TABLE x; --",  # dollar-quote escape
    ])
    def test_textual_evasion_has_nowhere_to_go(self, evasion, snapshot):
        """Previously these had to be DETECTED. Now there is no field to
        put them in — and as a property VALUE they stay data (below)."""
        with pytest.raises(Exception):
            ap.AgeQueryPlan.model_validate({
                "root_alias": "a", "root_label": "Case",
                "aggregate": "COUNT", "aggregate_alias": "a",
                "grain": "ENTITY", "cypher": evasion,
            })

    def test_label_that_looks_like_cypher_is_refused_by_validator(self, snapshot):
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Case) DETACH DELETE (x",
            aggregate=ap.Aggregate.COUNT, aggregate_alias="a",
            grain=ap.Grain.ENTITY,
        )
        v = apv.validate_plan(plan, snapshot)
        assert not v.ok
        assert "unknown_entity" in v.codes()

    def test_compiler_refuses_non_identifier_even_if_validation_bypassed(self):
        """Belt and braces at the interpolation point itself."""
        with pytest.raises(age_compiler.CompilerError):
            age_compiler._safe_identifier("Case) DETACH DELETE (x", "label")
        with pytest.raises(age_compiler.CompilerError):
            age_compiler._safe_identifier("a`b", "alias")


# ══════════════════════════════════════════════════════════════════════
# Validator
# ══════════════════════════════════════════════════════════════════════

class TestValidatorRejectsUnknownIdentifiers:
    def test_unknown_label(self, snapshot):
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Sorcerer",
            aggregate=ap.Aggregate.COUNT, aggregate_alias="a",
            grain=ap.Grain.ENTITY,
        )
        assert "unknown_entity" in apv.validate_plan(plan, snapshot).codes()

    def test_unknown_relationship_type(self, snapshot):
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Person",
            patterns=(ap.MatchPattern(
                from_alias="a", from_label="Person", rel_type="HEXES",
                direction=ap.Direction.OUT, to_alias="b", to_label="Case",
            ),),
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="entity_id", grain=ap.Grain.ENTITY,
        )
        assert "unknown_relationship" in apv.validate_plan(plan, snapshot).codes()

    def test_relationship_between_wrong_labels_is_refused(self, snapshot):
        """The type exists; that triple does not. This is the check that
        catches a plausible-looking but unmeasured traversal."""
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Officer",
            patterns=(ap.MatchPattern(
                from_alias="a", from_label="Officer",
                rel_type="BELONGS_TO_CASE", direction=ap.Direction.OUT,
                to_alias="b", to_label="Case",
            ),),
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="entity_id", grain=ap.Grain.ENTITY,
        )
        assert "unknown_relationship" in apv.validate_plan(plan, snapshot).codes()

    def test_unknown_property_in_filter(self, snapshot):
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Case",
            filters=(ap.Filter(alias="a", property="astrological_sign",
                               op=ap.Operator.EQ, value="Leo"),),
            aggregate=ap.Aggregate.COUNT, aggregate_alias="a",
            grain=ap.Grain.ENTITY,
        )
        assert "unknown_field" in apv.validate_plan(plan, snapshot).codes()

    def test_filter_on_unbound_alias(self, snapshot):
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Case",
            filters=(ap.Filter(alias="d", property="case_id",
                               op=ap.Operator.EQ, value="x"),),
            aggregate=ap.Aggregate.COUNT, aggregate_alias="a",
            grain=ap.Grain.ENTITY,
        )
        assert "unbound_alias" in apv.validate_plan(plan, snapshot).codes()

    def test_nullary_operator_with_value_is_refused(self, snapshot):
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Case",
            filters=(ap.Filter(alias="a", property="case_id",
                               op=ap.Operator.IS_NULL, value="x"),),
            aggregate=ap.Aggregate.COUNT, aggregate_alias="a",
            grain=ap.Grain.ENTITY,
        )
        assert "operator_value_mismatch" in apv.validate_plan(plan, snapshot).codes()


class TestValidatorEnforcesCountingCorrectness:
    def test_plain_count_downstream_of_fanning_traversal_is_refused(self, snapshot):
        """The 73 -> 449 inflation, refused at the plan boundary."""
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Person",
            patterns=(ap.MatchPattern(
                from_alias="a", from_label="Person",
                rel_type="BELONGS_TO_CASE", direction=ap.Direction.OUT,
                to_alias="b", to_label="Case",
            ),),
            aggregate=ap.Aggregate.COUNT, aggregate_alias="a",
            grain=ap.Grain.ENTITY,
        )
        v = apv.validate_plan(plan, snapshot)
        assert "fanout_requires_distinct" in v.codes()

    def test_count_distinct_over_the_same_traversal_is_allowed(self, snapshot):
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Person",
            patterns=(ap.MatchPattern(
                from_alias="a", from_label="Person",
                rel_type="BELONGS_TO_CASE", direction=ap.Direction.OUT,
                to_alias="b", to_label="Case",
            ),),
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="entity_id", grain=ap.Grain.ENTITY,
        )
        assert apv.validate_plan(plan, snapshot).ok

    def test_grouping_by_a_sparse_property_is_refused(self, snapshot):
        """Person.age is present on 19/430; grouping by it would discard
        411 nodes into a NULL bucket."""
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Person",
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="entity_id",
            group_by=ap.GroupDimension(alias="a", property="age"),
            grain=ap.Grain.ENTITY,
        )
        assert "grouping_field_too_sparse" in apv.validate_plan(plan, snapshot).codes()

    def test_value_aggregate_without_property_is_refused(self, snapshot):
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Person",
            aggregate=ap.Aggregate.AVG, aggregate_alias="a",
            grain=ap.Grain.ENTITY,
        )
        assert "missing_value_field" in apv.validate_plan(plan, snapshot).codes()


class TestValidatorEnforcesCostLimits:
    def test_too_many_patterns(self, snapshot):
        pat = ap.MatchPattern(
            from_alias="a", from_label="Case", rel_type="FILED_AT",
            direction=ap.Direction.OUT, to_alias="b", to_label="PoliceStation",
        )
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Case",
            patterns=tuple([pat] * (apv.MAX_PATTERNS + 1)),
            aggregate=ap.Aggregate.COUNT, aggregate_alias="a",
            grain=ap.Grain.ENTITY,
        )
        assert "too_many_patterns" in apv.validate_plan(plan, snapshot).codes()

    def test_limit_ceiling_enforced_at_parse_time(self):
        with pytest.raises(Exception):
            ap.AgeQueryPlan(
                root_alias="a", root_label="Case",
                aggregate="COUNT", aggregate_alias="a", grain="ENTITY",
                limit=10_000,
            )


# ══════════════════════════════════════════════════════════════════════
# Compiler invariants
# ══════════════════════════════════════════════════════════════════════

def _all_valid_plans():
    """Every supported plan shape, for the blanket invariant assertions."""
    return [
        _count_cases(),
        ap.AgeQueryPlan(
            root_alias="a", root_label="Person",
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="entity_id", grain=ap.Grain.ENTITY,
        ),
        ap.AgeQueryPlan(
            root_alias="a", root_label="Person",
            patterns=(ap.MatchPattern(
                from_alias="a", from_label="Person",
                rel_type="BELONGS_TO_CASE", direction=ap.Direction.OUT,
                to_alias="b", to_label="Case",
            ),),
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="entity_id", grain=ap.Grain.ENTITY,
        ),
        ap.AgeQueryPlan(
            root_alias="a", root_label="Case",
            patterns=(ap.MatchPattern(
                from_alias="a", from_label="Case", rel_type="FILED_AT",
                direction=ap.Direction.OUT, to_alias="b",
                to_label="PoliceStation",
            ),),
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="case_id",
            group_by=ap.GroupDimension(alias="b", property="name"),
            grain=ap.Grain.ENTITY,
        ),
        ap.AgeQueryPlan(
            root_alias="a", root_label="Person",
            filters=(ap.Filter(alias="a", property="name",
                               op=ap.Operator.EQ, value="Ali"),),
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="entity_id", grain=ap.Grain.ENTITY,
        ),
        # OPTIONAL MATCH, traversed IN: the root Case is reached FROM an
        # Officer. Alias `a` stays bound to Case throughout — binding it to
        # two labels is a plan error the validator rejects.
        ap.AgeQueryPlan(
            root_alias="a", root_label="Case",
            patterns=(ap.MatchPattern(
                from_alias="b", from_label="Officer", rel_type="ASSIGNED_TO",
                direction=ap.Direction.OUT, to_alias="a", to_label="Case",
                optional=True,
            ),),
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="case_id", grain=ap.Grain.ENTITY,
        ),
    ]


class TestCompilerEmitsOnlyReadOnlyCypher:
    def test_no_mutation_emitter_exists_in_the_source(self):
        """The strongest property: not 'mutation is rejected' but 'there is
        no code that writes it'."""
        src = inspect.getsource(age_compiler)
        body = src.split('"""', 2)[-1]  # drop the module docstring
        for verb in ("DETACH DELETE", "CREATE ", "MERGE ", "SET ", "REMOVE "):
            assert verb not in body, f"compiler contains an emitter for {verb!r}"

    @pytest.mark.parametrize("plan", _all_valid_plans())
    def test_compiled_output_contains_no_forbidden_clause(self, plan, snapshot):
        """Forbidden list imported from the guard so the two cannot drift."""
        compiled = _compile(plan, snapshot)
        stripped = cypher_guard._strip_strings(compiled.cypher).lower()
        for clause in cypher_guard._WRITE_CLAUSES:
            assert not re.search(rf"\b{clause}\b", stripped), (
                f"{clause!r} appeared in compiled output: {compiled.cypher}"
            )

    @pytest.mark.parametrize("plan", _all_valid_plans())
    def test_compiled_output_passes_the_existing_guard(self, plan, snapshot):
        compiled = _compile(plan, snapshot)
        assert age_execution.assert_read_only(compiled, snapshot) == ()

    @pytest.mark.parametrize("plan", _all_valid_plans())
    def test_no_semicolons_or_dollar_quote_escape(self, plan, snapshot):
        compiled = _compile(plan, snapshot)
        assert ";" not in compiled.cypher
        assert "$cypher$" not in compiled.cypher

    @pytest.mark.parametrize("plan", _all_valid_plans())
    def test_every_return_term_is_aliased(self, plan, snapshot):
        """AGE cannot infer result shape; an unaliased term is unexecutable."""
        compiled = _compile(plan, snapshot)
        assert cypher_guard.expected_columns(compiled.cypher) == compiled.columns

    @pytest.mark.parametrize("plan", _all_valid_plans())
    def test_opens_with_an_allowed_clause(self, plan, snapshot):
        compiled = _compile(plan, snapshot)
        assert compiled.cypher.strip().lower().startswith(
            cypher_guard._ALLOWED_OPENERS
        )

    def test_grouping_uses_with_form_and_emits_no_order_by(self, snapshot):
        """AGE raises UndefinedColumnError on ORDER BY <alias>.

        Regression: the first version of the grouped emitter copied the
        structured compiler's `ORDER BY value DESC` and the guard refused
        it, which is precisely what the defence-in-depth layer is for. The
        emitter now does not express ordering at all.
        """
        plan = _all_valid_plans()[3]
        compiled = _compile(plan, snapshot)
        assert " WITH " in compiled.cypher
        assert "ORDER BY" not in compiled.cypher.upper()
        assert cypher_guard.check_dialect(compiled.cypher) == []


class TestCompilerInjectsCorrectnessInvariants:
    def test_tombstone_predicate_injected_for_labels_that_have_them(self, snapshot):
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Person",
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="entity_id", grain=ap.Grain.ENTITY,
        )
        compiled = _compile(plan, snapshot)
        assert "a.merged_into IS NULL" in compiled.cypher

    def test_tombstone_predicate_not_injected_for_labels_without_them(self, snapshot):
        compiled = _compile(_count_cases(), snapshot)
        assert "merged_into" not in compiled.cypher

    def test_supersession_predicate_injected_for_versioned_relationships(self, snapshot):
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Person",
            patterns=(ap.MatchPattern(
                from_alias="a", from_label="Person",
                rel_type="BELONGS_TO_CASE", direction=ap.Direction.OUT,
                to_alias="b", to_label="Case",
            ),),
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="entity_id", grain=ap.Grain.ENTITY,
        )
        compiled = _compile(plan, snapshot)
        assert "superseded_by IS NULL" in compiled.cypher


# ══════════════════════════════════════════════════════════════════════
# Injection — values stay data
# ══════════════════════════════════════════════════════════════════════

class TestValuesCannotBecomeSyntax:
    @pytest.mark.parametrize("hostile", [
        "'; MATCH (n) DETACH DELETE n; --",
        "$cypher$ DROP GRAPH evidence_graph $cypher$",
        "Ali' OR '1'='1",
        'quote" backslash\\ newline\n',
        "DELETE",
        "RETURN 1",
        "​DETACH DELETE",
        "اسلام آباد",
    ])
    def test_hostile_property_value_is_bound_not_interpolated(
        self, hostile, snapshot
    ):
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Person",
            filters=(ap.Filter(alias="a", property="name",
                               op=ap.Operator.EQ, value=hostile),),
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="entity_id", grain=ap.Grain.ENTITY,
        )
        compiled = _compile(plan, snapshot)
        # The value appears ONLY in params, never in the query text.
        assert hostile not in compiled.cypher
        assert hostile in compiled.params.values()
        # And what the text carries is a bound placeholder.
        assert re.search(r"a\.name = \$p\d+", compiled.cypher)
        assert age_execution.assert_read_only(compiled, snapshot) == ()

    def test_in_operator_binds_the_whole_list(self, snapshot):
        values = ["a'; DELETE", "b"]
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Case",
            filters=(ap.Filter(alias="a", property="case_id",
                               op=ap.Operator.IN, value=values),),
            aggregate=ap.Aggregate.COUNT, aggregate_alias="a",
            grain=ap.Grain.ENTITY,
        )
        compiled = _compile(plan, snapshot)
        assert "DELETE" not in compiled.cypher
        assert values in compiled.params.values()


# ══════════════════════════════════════════════════════════════════════
# Execution boundary
# ══════════════════════════════════════════════════════════════════════

class TestExecutionBoundary:
    async def test_raw_string_cannot_be_executed(self):
        """The type is the check: no string overload exists."""
        with pytest.raises(age_execution.AgeExecutionError) as exc:
            await age_execution.execute_compiled_age_query(
                "MATCH (n) DETACH DELETE n"
            )
        assert "CompiledAgeQuery" in str(exc.value)

    async def test_lookalike_object_is_rejected(self):
        class FakeCompiled:
            cypher = "MATCH (n) DETACH DELETE n"
            params: dict = {}
            columns = ("value",)

        with pytest.raises(age_execution.AgeExecutionError):
            await age_execution.execute_compiled_age_query(FakeCompiled())

    def test_graph_target_is_a_constant_not_a_parameter(self):
        params = set(
            inspect.signature(
                age_execution.execute_compiled_age_query
            ).parameters
        )
        assert params == {"compiled"}
        assert age_execution.PRODUCTION_GRAPH == "evidence_graph"

    def test_route_cannot_reach_the_raw_age_client(self):
        """Scoped to the EXECUTION PATH, which is where the property lives.

        PHASE 6 NARROWED THIS. It previously scanned the whole module, and
        began failing when `collect_value_examples()` was added — that
        helper calls `age_client.execute_cypher` with fixed templates built
        from registry measurements, to read example property values for the
        prompt. It runs BEFORE any model call and cannot carry model text
        (asserted separately below), so a module-wide ban was measuring the
        wrong thing.

        The property that matters is that the path which handles model
        output never reaches raw Cypher execution, so that is what is
        asserted here.
        """
        from src.pipeline.aggregate import route_age

        src = inspect.getsource(route_age.run)
        code = "\n".join(
            ln for ln in src.splitlines() if not ln.strip().startswith("#")
        )
        if route_age.run.__doc__:
            code = code.replace(route_age.run.__doc__, "")
        assert "age_execution.execute_compiled_age_query" in code
        assert "age_client.execute_cypher" not in code
        assert "age_eval_client" not in code

    def test_card_builder_never_receives_model_input(self):
        """The one raw-Cypher caller left in the module is model-free.

        `collect_value_examples()` interpolates a label and a property name
        into a fixed template. Both come from `RegistrySnapshot` — live
        measurements of the graph — and the function takes no question,
        plan, or payload argument at all, so there is no parameter through
        which model text could arrive.
        """
        from src.pipeline.aggregate import route_age

        params = set(
            inspect.signature(route_age.collect_value_examples).parameters
        )
        assert params == {"snapshot"}

        body = inspect.getsource(route_age.collect_value_examples)
        # Nothing plan-, question- or payload-derived may appear in it.
        for forbidden in ("plan", "payload", "question", "request", "cypher_text"):
            assert f"{forbidden}." not in body, (
                f"card builder references {forbidden!r}; it must read only "
                f"the registry snapshot"
            )

    def test_compiler_defect_is_raised_not_executed(self, snapshot):
        """If the trusted compiler ever emitted a write, nothing runs."""
        bad = age_compiler.CompiledAgeQuery(
            cypher="MATCH (n:Case) DETACH DELETE n RETURN count(n) AS value",
            params={}, columns=("value",),
        )
        with pytest.raises(age_execution.CompilerDefect):
            age_execution.assert_read_only(bad, snapshot)


# ══════════════════════════════════════════════════════════════════════
# Semantic independence
# ══════════════════════════════════════════════════════════════════════

class TestAgePlannerRemainsIndependent:
    """Removing the model's ability to emit SYNTAX must not remove its
    ability to reach a different SEMANTIC answer."""

    def test_age_may_choose_a_different_relationship_than_structured(self, snapshot):
        """The recorded Phase 4 disagreement, as a unit invariant.

        In `ratio_cases_multi_officer_entity_grain` the AGE route traversed
        BELONGS_TO_CASE while the structured route used ASSIGNED_TO, and
        reconciliation classified it CONFLICT. The objective is NOT to force
        AGE onto ASSIGNED_TO — it is that AGE can still choose, that the
        choice is explicit in the plan, and that reconciliation can see it.
        """
        age_choice = ap.AgeQueryPlan(
            root_alias="a", root_label="Person",
            patterns=(ap.MatchPattern(
                from_alias="a", from_label="Person",
                rel_type="BELONGS_TO_CASE", direction=ap.Direction.OUT,
                to_alias="b", to_label="Case",
            ),),
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="entity_id", grain=ap.Grain.ENTITY,
            population_description="persons linked to a case",
        )
        structured_choice_rel = "ASSIGNED_TO"

        v = apv.validate_plan(age_choice, snapshot)
        assert v.ok, "AGE's independent choice must remain expressible"
        compiled = age_compiler.compile_plan(age_choice, snapshot, v)
        assert "BELONGS_TO_CASE" in compiled.cypher
        assert structured_choice_rel not in compiled.cypher

    def test_the_chosen_relationship_is_visible_in_provenance(self, snapshot):
        """Reconciliation can only classify a disagreement it can see."""
        plan = ap.AgeQueryPlan(
            root_alias="a", root_label="Officer",
            patterns=(ap.MatchPattern(
                from_alias="a", from_label="Officer",
                rel_type="ASSIGNED_TO", direction=ap.Direction.OUT,
                to_alias="b", to_label="Case",
            ),),
            aggregate=ap.Aggregate.COUNT_DISTINCT, aggregate_alias="a",
            aggregate_property="entity_id", grain=ap.Grain.ENTITY,
        )
        assert [p.rel_type for p in plan.patterns] == ["ASSIGNED_TO"]
        assert plan.grain.value == "ENTITY"

    def test_validator_never_rewrites_a_plan(self):
        """No repair path: validation reports, it does not edit."""
        src = inspect.getsource(apv)
        assert "dataclasses.replace" not in src
        assert "model_copy" not in src


# ══════════════════════════════════════════════════════════════════════
# Reconciliation after the pivot
# ══════════════════════════════════════════════════════════════════════

class TestReconciliationAfterSnapshotRemoval:
    def test_snapshot_gating_is_gone(self):
        from src.pipeline.aggregate import reconcile

        src = inspect.getsource(reconcile.reconcile)
        assert "snapshot_id" not in src
        assert "executed_against" not in src

    def test_equal_values_still_agree(self):
        from src.pipeline.aggregate import reconcile
        from src.pipeline.aggregate.routes import (
            NumericResult, RouteResult, ROUTE_AGE, ROUTE_STRUCTURED,
        )

        def num(route, value, prov=None):
            return RouteResult(
                route=route, status="SUCCESS", result_shape="scalar",
                result=NumericResult(value=value, interpretation="count",
                                     grain="ENTITY"),
                provenance=prov or {},
            )

        # Even carrying evaluator-style provenance, which used to demote it.
        r = reconcile.reconcile(
            num(ROUTE_STRUCTURED, 73),
            num(ROUTE_AGE, 73, {"executed_against": "muhafiz.evidence_graph"}),
        )
        assert r.classification == reconcile.AGREEMENT
        assert r.value == 73

    def test_the_4_vs_70_conflict_survives(self):
        from src.pipeline.aggregate import reconcile
        from src.pipeline.aggregate.routes import (
            NumericResult, RouteResult, ROUTE_AGE, ROUTE_STRUCTURED,
        )

        def num(route, value):
            return RouteResult(
                route=route, status="SUCCESS", result_shape="scalar",
                result=NumericResult(value=value, interpretation="count",
                                     grain="ENTITY"),
            )

        r = reconcile.reconcile(num(ROUTE_STRUCTURED, 4), num(ROUTE_AGE, 70))
        assert r.classification == reconcile.CONFLICT
        assert r.value is None
