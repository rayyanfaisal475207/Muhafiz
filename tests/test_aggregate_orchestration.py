"""
Phase 9 — the production query path, end to end.

These tests exercise the wiring, not the routes. The routes have their own
suites; what is unproven until now is that a question reaches a spec, a
spec reaches a verification decision, and that decision actually controls
which routes run.

The six scenarios the brief names are each pinned by a class below. The
sixth is the one that matters most: a model recommending NONE against a
spec that fires a mandatory trigger must still be verified, because that
is the difference between "deterministic rules are authoritative" being a
property of the code and being a sentence in a prompt.

Routes are stubbed throughout. A test that needed a live AGE planner would
take 41-92 s per case and would be measuring the database, not the policy.
"""
import pytest

from src.pipeline.aggregate import nl_spec
from src.pipeline.aggregate import orchestrator as orch
from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate import routes
from src.pipeline.aggregate import verification_policy as vp
from src.pipeline.aggregate.routes import NumericResult, RouteResult
from src.pipeline.aggregate.spec import (
    AggregateSpec,
    FieldPredicate,
    PopulationNode,
    Ratio,
    Scope,
    TimeWindow,
    Traversal,
)

_SCOPE = Scope(kind="cross_case", user_role="supervisor", user_id="u1")


def _prop(name, present, total):
    return reg.PropertyInfo(name=name, present_n=present, total_n=total)


@pytest.fixture
def snapshot() -> reg.RegistrySnapshot:
    """A snapshot carrying the measured facts these tests turn on.

    The figures are the real ones: Person.age present on 19 of 430,
    BELONGS_TO_CASE fanning 449 edges over 208 distinct people, and
    incident_date owned by Postgres while the population lives in the graph.
    """
    entities = {
        "Person": reg.EntityInfo(
            label="Person", total_n=430, distinct_key="entity_id",
            properties={
                "entity_id": _prop("entity_id", 430, 430),
                "age": _prop("age", 19, 430),
                "gender": _prop("gender", 119, 430),
            },
            has_tombstones=True, tombstoned_n=222,
        ),
        "Case": reg.EntityInfo(
            label="Case", total_n=73, distinct_key="case_id",
            properties={
                "case_id": _prop("case_id", 73, 73),
                "status": _prop("status", 73, 73),
            },
        ),
        "Weapon": reg.EntityInfo(
            label="Weapon", total_n=32, distinct_key="weapon_id",
            properties={
                "weapon_id": _prop("weapon_id", 32, 32),
                "license_status": _prop("license_status", 30, 32),
            },
        ),
        "StructuredRecord": reg.EntityInfo(
            label="StructuredRecord", total_n=713, distinct_key="record_id",
            properties={
                "record_id": _prop("record_id", 713, 713),
                "record_type": _prop("record_type", 713, 713),
            },
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
    # A spec names a LOGICAL field; the registry decides which physical
    # path answers it. A property that exists on a label but has no logical
    # field is not filterable, so every field these specs filter on needs an
    # entry here — omitting them is what makes a spec UNSUPPORTED.
    def _graph_field(label, prop, present, total):
        return reg.LogicalField(
            name=f"{label}.{prop}", source="graph", path=f"{label}.{prop}",
            present_n=present, total_n=total,
            authority_basis="measured on the graph",
        )

    logical = {
        "incident_date": reg.LogicalField(
            name="incident_date", source="postgres",
            path="cases.incident_date", present_n=64, total_n=73,
            authority_basis="case rows are authoritative",
        ),
        "Weapon.license_status": _graph_field("Weapon", "license_status", 30, 32),
        "Person.age": _graph_field("Person", "age", 19, 430),
        "Person.gender": _graph_field("Person", "gender", 119, 430),
        "Case.status": _graph_field("Case", "status", 73, 73),
        "StructuredRecord.record_type": _graph_field(
            "StructuredRecord", "record_type", 713, 713
        ),
    }
    return reg.RegistrySnapshot(
        as_of="2026-09-17T00:00:00+00:00", entities=entities,
        relationships=relationships, logical_fields=logical,
    )


# ══════════════════════════════════════════════════════════════════════
# Specs
# ══════════════════════════════════════════════════════════════════════
def _simple_count() -> AggregateSpec:
    """A plain count: one source, no traversal, no literal, ENTITY grain."""
    return AggregateSpec(
        question_text="How many cases are registered?",
        measure="count", population=PopulationNode(entity="Case"),
        grain="ENTITY", distinct_key="case_id", scope=_SCOPE,
    )


def _multi_source() -> AggregateSpec:
    """A count restricted by a Postgres-authoritative date."""
    return AggregateSpec(
        question_text="How many cases in 2024?",
        measure="count", population=PopulationNode(entity="Case"),
        grain="ENTITY", distinct_key="case_id", scope=_SCOPE,
        time_window=TimeWindow(
            field="incident_date", start="2024-01-01", end="2024-12-31"
        ),
    )


def _value_literal() -> AggregateSpec:
    """The measured weapons case: a chosen string literal."""
    return AggregateSpec(
        question_text="How many unlicensed weapons?",
        measure="count_distinct",
        population=PopulationNode(
            entity="Weapon",
            predicates=(
                FieldPredicate(
                    field="license_status", op="contains", value="بغیر لائسنس"
                ),
            ),
        ),
        grain="ENTITY", distinct_key="weapon_id", scope=_SCOPE,
    )


def _record_grain() -> AggregateSpec:
    """The measured malkhana case: RECORD grain, dense property."""
    return AggregateSpec(
        question_text="How many malkhana records?",
        measure="count_distinct",
        population=PopulationNode(entity="StructuredRecord"),
        grain="RECORD", distinct_key="record_id", scope=_SCOPE,
    )


def _fanning_traversal() -> AggregateSpec:
    return AggregateSpec(
        question_text="How many people are involved in cases?",
        measure="count_distinct",
        population=PopulationNode(
            entity="Person",
            traversals=(Traversal(rel="BELONGS_TO_CASE", target="Case"),),
        ),
        grain="ENTITY", distinct_key="entity_id", scope=_SCOPE,
    )


# ══════════════════════════════════════════════════════════════════════
# Route stubs
# ══════════════════════════════════════════════════════════════════════
def _numeric(route, value, interpretation="count", grain="ENTITY"):
    return RouteResult(
        route=route, status=routes.SUCCESS, result_shape="scalar",
        result=NumericResult(
            value=value, interpretation=interpretation, grain=grain,
            population="test",
        ),
    )


class _Spy:
    """Records which routes ran, and answers for them."""

    def __init__(self, structured=73, age=73, semantic=True):
        self.calls: list[str] = []
        self._structured = structured
        self._age = age
        self._semantic = semantic

    async def structured(self, snapshot, request, *, spec=None, gate_profile_id=None):
        self.calls.append("structured")
        return _numeric(routes.ROUTE_STRUCTURED, self._structured)

    async def age(self, snapshot, request, *, schema_card=None):
        self.calls.append("age")
        return _numeric(routes.ROUTE_AGE, self._age)

    async def semantic(self, snapshot, request, **kw):
        self.calls.append("semantic")
        return RouteResult(
            route=routes.ROUTE_SEMANTIC, status=routes.SUCCESS,
            result_shape="evidence",
            result=routes.EvidenceResult(evidence_count=2),
        )

    def install(self, monkeypatch):
        monkeypatch.setattr(orch.route_structured, "run", self.structured)
        monkeypatch.setattr(orch.route_age, "run", self.age)
        monkeypatch.setattr(orch.route_semantic, "run", self.semantic)
        return self


@pytest.fixture
def spy(monkeypatch):
    return _Spy().install(monkeypatch)


async def _answer(snapshot, spec, **kw):
    return await orch.answer_question(
        snapshot, spec.question_text, _SCOPE, spec=spec, **kw
    )


# ══════════════════════════════════════════════════════════════════════
# 1. Simple query -> structured only
# ══════════════════════════════════════════════════════════════════════
class TestSimpleQueryStructuredOnly:

    @pytest.mark.asyncio
    async def test_no_trigger_fires(self, snapshot):
        decision = vp.decide(_simple_count(), snapshot=snapshot)
        assert decision.level == vp.NONE
        assert decision.mandatory_codes == ()

    @pytest.mark.asyncio
    async def test_only_structured_route_runs(self, snapshot, spy):
        answer = await _answer(snapshot, _simple_count())
        assert spy.calls == ["structured"]
        assert answer.status == "ANSWERED"
        assert answer.value == 73

    @pytest.mark.asyncio
    async def test_unverified_answer_says_so(self, snapshot, spy):
        answer = await _answer(snapshot, _simple_count())
        assert answer.independently_verified is False
        assert "Not verified" in answer.verification_note
        assert any("Not verified" in w for w in answer.warnings)


# ══════════════════════════════════════════════════════════════════════
# 2. Multi-source query -> verification (M1)
# ══════════════════════════════════════════════════════════════════════
class TestMultiSourceQuery:

    @pytest.mark.asyncio
    async def test_m1_fires(self, snapshot):
        decision = vp.decide(_multi_source(), snapshot=snapshot)
        assert "M1_multi_source" in decision.mandatory_codes
        assert decision.verify is True

    @pytest.mark.asyncio
    async def test_time_window_is_partial_not_full(self, snapshot):
        """AGE cannot evaluate a Postgres-authoritative date.

        Measured: the graph's Incident.incident_date is present on 0 of 73.
        Running AGE here would produce a number answering a different
        question, so the level is PARTIAL and AGE does not run.
        """
        decision = vp.decide(_multi_source(), snapshot=snapshot)
        assert decision.level == vp.PARTIAL
        assert decision.run_age is False
        assert decision.run_semantic is True
        assert decision.claims_independent is False

    @pytest.mark.asyncio
    async def test_partial_result_is_marked(self, snapshot, spy):
        answer = await _answer(snapshot, _multi_source())
        assert spy.calls == ["structured", "semantic"]
        assert answer.independently_verified is False
        assert "Not independently verified" in answer.verification_note


# ══════════════════════════════════════════════════════════════════════
# 3. Value-literal filter -> mandatory verification (M3)
# ══════════════════════════════════════════════════════════════════════
class TestValueLiteralFilter:
    """The weapons case. V1 served this structured-only; it disagreed."""

    @pytest.mark.asyncio
    async def test_m3_fires(self, snapshot):
        decision = vp.decide(_value_literal(), snapshot=snapshot)
        assert "M3_value_literal" in decision.mandatory_codes
        assert decision.level == vp.FULL

    @pytest.mark.asyncio
    async def test_all_three_routes_run(self, snapshot, spy):
        answer = await _answer(snapshot, _value_literal())
        assert spy.calls == ["structured", "age", "semantic"]
        assert answer.independently_verified is True

    @pytest.mark.asyncio
    async def test_numeric_comparison_does_not_fire_m3(self, snapshot):
        """A wrong number is visible; a wrong string is not.

        `age gt 30` must not force verification, or M3 would fire on
        essentially every filtered query and the policy would collapse back
        into V1's 76%.
        """
        spec = AggregateSpec(
            question_text="How many people over 30?",
            measure="count_distinct",
            population=PopulationNode(
                entity="Person",
                predicates=(FieldPredicate(field="age", op="gt", value=30),),
            ),
            grain="ENTITY", distinct_key="entity_id", scope=_SCOPE,
        )
        decision = vp.decide(spec, snapshot=snapshot)
        assert "M3_value_literal" not in decision.mandatory_codes


# ══════════════════════════════════════════════════════════════════════
# 4. Non-ENTITY grain -> mandatory verification (M4)
# ══════════════════════════════════════════════════════════════════════
class TestNonEntityGrain:
    """The malkhana case: dense property, no traversal, one source."""

    @pytest.mark.asyncio
    async def test_m4_fires(self, snapshot):
        decision = vp.decide(_record_grain(), snapshot=snapshot)
        assert "M4_non_entity_grain" in decision.mandatory_codes
        assert decision.level == vp.FULL

    @pytest.mark.asyncio
    async def test_all_three_routes_run(self, snapshot, spy):
        await _answer(snapshot, _record_grain())
        assert spy.calls == ["structured", "age", "semantic"]

    @pytest.mark.parametrize(
        "grain", ["RELATIONSHIP", "ROLE_PAIR", "EVENT", "RECORD"]
    )
    def test_every_non_entity_grain_triggers(self, snapshot, grain):
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="Case"),
            grain=grain, distinct_key="case_id", scope=_SCOPE,
        )
        assert "M4_non_entity_grain" in vp.decide(
            spec, snapshot=snapshot
        ).mandatory_codes


# ══════════════════════════════════════════════════════════════════════
# 5. LLM recommends verification -> level increases
# ══════════════════════════════════════════════════════════════════════
class TestLLMRaisesVerification:

    def test_recommendation_raises_none_to_full(self, snapshot):
        spec = _simple_count()
        assert vp.decide(spec, snapshot=snapshot).level == vp.NONE

        raised = vp.decide(
            spec, snapshot=snapshot, llm_recommendation="FULL",
            llm_rationale="The question says this is for a court filing.",
        )
        assert raised.level == vp.FULL
        assert raised.deterministic_level == vp.NONE
        assert raised.llm_rationale

    @pytest.mark.asyncio
    async def test_raised_level_actually_runs_the_routes(self, snapshot, monkeypatch, spy):
        """The recommendation must change behaviour, not just a field."""
        async def fake_generate(snapshot_, question, scope, **kw):
            return nl_spec.SpecGeneration(
                spec=_simple_count(),
                verification_recommendation="FULL",
                verification_rationale="ambiguous term",
            )

        monkeypatch.setattr(orch.nl_spec, "generate_spec", fake_generate)
        answer = await orch.answer_question(
            snapshot, "How many cases are registered?", _SCOPE
        )
        assert spy.calls == ["structured", "age", "semantic"]
        assert answer.decision.llm_recommendation == "FULL"

    def test_recommendation_cannot_exceed_capability(self, snapshot):
        """A model asking for FULL does not give AGE the ability to
        evaluate a Postgres-owned date."""
        raised = vp.decide(
            _multi_source(), snapshot=snapshot, llm_recommendation="FULL"
        )
        assert raised.level == vp.PARTIAL
        assert raised.partial_reason


# ══════════════════════════════════════════════════════════════════════
# 6. LLM recommends NONE but a rule fires -> verification still happens
# ══════════════════════════════════════════════════════════════════════
class TestDeterministicRulesAreAuthoritative:
    """The property that makes the policy a guarantee rather than a hope."""

    @pytest.mark.parametrize(
        "spec_fn,code",
        [
            (_value_literal, "M3_value_literal"),
            (_record_grain, "M4_non_entity_grain"),
            (_fanning_traversal, "M2_fanning_traversal"),
        ],
    )
    def test_none_recommendation_cannot_lower_a_mandatory_trigger(
        self, snapshot, spec_fn, code
    ):
        decision = vp.decide(
            spec_fn(), snapshot=snapshot, llm_recommendation="NONE"
        )
        assert code in decision.mandatory_codes
        assert decision.level == vp.FULL
        assert decision.llm_recommendation == "NONE"
        assert decision.deterministic_level == vp.FULL

    @pytest.mark.asyncio
    async def test_routes_run_despite_none_recommendation(
        self, snapshot, monkeypatch, spy
    ):
        async def fake_generate(snapshot_, question, scope, **kw):
            return nl_spec.SpecGeneration(
                spec=_value_literal(),
                verification_recommendation="NONE",
                verification_rationale="looks simple to me",
            )

        monkeypatch.setattr(orch.nl_spec, "generate_spec", fake_generate)
        answer = await orch.answer_question(
            snapshot, "How many unlicensed weapons?", _SCOPE
        )
        assert spy.calls == ["structured", "age", "semantic"]
        assert answer.decision.level == vp.FULL

    def test_partial_recommendation_cannot_lower_full(self, snapshot):
        decision = vp.decide(
            _value_literal(), snapshot=snapshot, llm_recommendation="PARTIAL"
        )
        assert decision.level == vp.FULL


# ══════════════════════════════════════════════════════════════════════
# M2 — fanning specifically, not traversal generally
# ══════════════════════════════════════════════════════════════════════
class TestFanningTraversal:

    def test_fanning_traversal_is_mandatory(self, snapshot):
        decision = vp.decide(_fanning_traversal(), snapshot=snapshot)
        assert "M2_fanning_traversal" in decision.mandatory_codes

    def test_reverse_direction_is_read_correctly(self, snapshot):
        """Cardinality is directional. Case->Person reaches 62 people per
        case; reading the forward figure would call this safe."""
        spec = AggregateSpec(
            question_text="people per case", measure="count_distinct",
            population=PopulationNode(
                entity="Case",
                traversals=(
                    Traversal(
                        rel="BELONGS_TO_CASE", target="Person", direction="in"
                    ),
                ),
            ),
            grain="ENTITY", distinct_key="case_id", scope=_SCOPE,
        )
        decision = vp.decide(spec, snapshot=snapshot)
        assert "M2_fanning_traversal" in decision.mandatory_codes

    def test_traversal_without_fanout_is_advisory_only(self, snapshot):
        """A1, not M2. This is the reclassification that cuts V1's volume:
        a non-fanning traversal fired T2 in V1 and caught nothing."""
        entities = dict(snapshot.entities)
        rels = {
            ("Person", "HAS_ONE", "Case"): reg.RelationshipInfo(
                source_label="Person", rel_type="HAS_ONE", target_label="Case",
                edge_n=73, distinct_sources_n=73, distinct_targets_n=73,
                max_fanout=1, cardinality=reg.classify_cardinality(73, 73, 73),
                max_reverse_fanout=1,
            ),
        }
        snap = reg.RegistrySnapshot(
            as_of=snapshot.as_of, entities=entities, relationships=rels,
            logical_fields=snapshot.logical_fields,
        )
        spec = AggregateSpec(
            question_text="q", measure="count_distinct",
            population=PopulationNode(
                entity="Person",
                traversals=(Traversal(rel="HAS_ONE", target="Case"),),
            ),
            grain="ENTITY", distinct_key="entity_id", scope=_SCOPE,
        )
        decision = vp.decide(spec, snapshot=snap)
        assert "M2_fanning_traversal" not in decision.mandatory_codes
        assert "A1_traversal" in decision.advisory_codes
        assert decision.level == vp.NONE


# ══════════════════════════════════════════════════════════════════════
# Capability handling
# ══════════════════════════════════════════════════════════════════════
class TestCapabilityLevels:

    def test_ratio_is_never_full(self, snapshot):
        spec = AggregateSpec(
            question_text="what percentage of cases have a weapon?",
            measure="count", population=PopulationNode(entity="Case"),
            grain="RELATIONSHIP", distinct_key="case_id", scope=_SCOPE,
            ratio=Ratio(
                numerator=PopulationNode(entity="Case"),
                denominator=PopulationNode(entity="Case"),
            ),
        )
        decision = vp.decide(spec, snapshot=snapshot)
        assert decision.level == vp.PARTIAL
        assert decision.run_age is False
        assert "ratio" in decision.partial_reason.lower()

    def test_median_is_never_full(self, snapshot):
        spec = AggregateSpec(
            question_text="median age", measure="median",
            value_field="age", population=PopulationNode(entity="Person"),
            grain="RECORD", distinct_key="entity_id", scope=_SCOPE,
        )
        decision = vp.decide(spec, snapshot=snapshot)
        assert decision.level == vp.PARTIAL
        assert "median" in decision.partial_reason.lower()

    def test_comparable_shape_is_full(self, snapshot):
        assert vp.decide(_value_literal(), snapshot=snapshot).level == vp.FULL


# ══════════════════════════════════════════════════════════════════════
# Orchestration invariants
# ══════════════════════════════════════════════════════════════════════
class TestOrchestrationInvariants:

    @pytest.mark.asyncio
    async def test_conflict_serves_no_value(self, snapshot, monkeypatch):
        """Reconciliation withholds a value on CONFLICT; the orchestrator
        must not repair that by preferring a route."""
        spy = _Spy(structured=92, age=208).install(monkeypatch)
        answer = await _answer(snapshot, _value_literal())
        assert answer.status == "CONFLICT"
        assert answer.value is None
        assert answer.independently_verified is False

    @pytest.mark.asyncio
    async def test_age_failure_does_not_lose_the_answer(
        self, snapshot, monkeypatch
    ):
        spy = _Spy().install(monkeypatch)

        async def boom(*a, **kw):
            spy.calls.append("age")
            raise RuntimeError("planner unreachable")

        monkeypatch.setattr(orch.route_age, "run", boom)
        answer = await _answer(snapshot, _value_literal())
        assert answer.status == "ANSWERED"
        assert answer.value == 73
        assert answer.independently_verified is False

    @pytest.mark.asyncio
    async def test_scope_is_enforced_before_any_model_call(
        self, snapshot, monkeypatch
    ):
        async def must_not_run(*a, **kw):
            raise AssertionError("generation ran for an unauthorised caller")

        monkeypatch.setattr(orch.nl_spec, "generate_spec", must_not_run)
        answer = await orch.answer_question(
            snapshot, "How many cases?",
            Scope(kind="cross_case", user_role="officer", user_id="u2"),
        )
        assert answer.status == "REFUSED"
        assert answer.refusal_code == "scope_denied"

    @pytest.mark.asyncio
    async def test_invalid_spec_refuses_before_execution(
        self, snapshot, monkeypatch
    ):
        spy = _Spy().install(monkeypatch)
        spec = AggregateSpec(
            question_text="q", measure="count",
            population=PopulationNode(entity="NoSuchLabel"),
            grain="ENTITY", scope=_SCOPE,
        )
        answer = await _answer(snapshot, spec)
        assert answer.status == "REFUSED"
        assert answer.refusal_code == "spec_invalid"
        assert spy.calls == []

    @pytest.mark.asyncio
    async def test_answer_serialises(self, snapshot, spy):
        answer = await _answer(snapshot, _value_literal())
        payload = answer.to_dict()
        assert payload["status"] == "ANSWERED"
        assert payload["independently_verified"] is True
        assert payload["decision"]["mandatory_triggers"] == ["M3_value_literal"]
        assert payload["spec_hash"]


# ══════════════════════════════════════════════════════════════════════
# NL -> spec parsing (no model in the loop)
# ══════════════════════════════════════════════════════════════════════
class TestSpecGeneration:

    @pytest.mark.asyncio
    async def test_scope_comes_from_the_caller_not_the_model(self, snapshot):
        """A question is untrusted input. If it could widen scope, natural
        language would be a privilege-escalation path."""
        async def fake_llm(system, user, **kw):
            return {
                "measure": "count", "entity": "Case", "grain": "ENTITY",
                "distinct_key": "case_id",
                # A hostile payload trying to widen its own scope.
                "scope": {"kind": "cross_case", "user_role": "platform_admin"},
            }, "ok"

        generation = await nl_spec.generate_spec(
            snapshot, "how many cases", _SCOPE,
            schema_card="card", _call_llm_json=fake_llm,
        )
        assert generation.spec.scope is _SCOPE
        assert generation.spec.scope.user_role == "supervisor"

    @pytest.mark.asyncio
    async def test_unparseable_reply_raises(self, snapshot):
        async def fake_llm(system, user, **kw):
            return None, "model returned prose"

        with pytest.raises(nl_spec.SpecGenerationError):
            await nl_spec.generate_spec(
                snapshot, "q", _SCOPE, schema_card="card",
                _call_llm_json=fake_llm,
            )

    @pytest.mark.asyncio
    async def test_traversals_and_predicates_are_typed(self, snapshot):
        async def fake_llm(system, user, **kw):
            return {
                "measure": "count_distinct", "entity": "Person",
                "grain": "ENTITY", "distinct_key": "entity_id",
                "traversals": [
                    {"rel": "BELONGS_TO_CASE", "target": "Case",
                     "direction": "out"}
                ],
                "predicates": [
                    {"kind": "field", "field": "gender", "op": "eq",
                     "value": "male"}
                ],
                "verification_recommendation": "FULL",
            }, "ok"

        generation = await nl_spec.generate_spec(
            snapshot, "q", _SCOPE, schema_card="card", _call_llm_json=fake_llm,
        )
        spec = generation.spec
        assert isinstance(spec.population.traversals[0], Traversal)
        assert isinstance(spec.population.predicates[0], FieldPredicate)
        assert generation.verification_recommendation == "FULL"

    @pytest.mark.asyncio
    async def test_malformed_predicate_is_dropped_not_guessed(self, snapshot):
        """A dropped predicate widens the population, which coverage can
        see. A guessed one is wrong in a way nothing downstream detects."""
        async def fake_llm(system, user, **kw):
            return {
                "measure": "count", "entity": "Case", "grain": "ENTITY",
                "predicates": [
                    {"kind": "field", "op": "nonsense_op", "value": 1},
                    {"kind": "field", "field": "status", "op": "eq",
                     "value": "open"},
                ],
            }, "ok"

        generation = await nl_spec.generate_spec(
            snapshot, "q", _SCOPE, schema_card="card", _call_llm_json=fake_llm,
        )
        predicates = generation.spec.population.predicates
        assert len(predicates) == 1
        assert predicates[0].field == "status"
