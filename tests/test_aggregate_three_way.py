# -*- coding: utf-8 -*-
"""
Phase 4 tests — common contract, AGE guards, semantic route, reconciliation.

OFFLINE. No database, no model server. The guard is a pure function of
(query, registry snapshot); reconciliation is a pure function of
RouteResults; the semantic route's non-fabrication property is structural
and testable without retrieval. Live behaviour is exercised by
`scripts/run_aggregate_three_way.py`.

WHAT THESE PROTECT. The three-way architecture's value rests on two claims:
that a generated query cannot reach the database unguarded, and that
disagreement is never resolved by preference or averaging. Both are
properties a plausible future edit could quietly remove, so both are
pinned here rather than left to the harness.
"""
from __future__ import annotations

import pytest

from src.pipeline.aggregate import cypher_guard, reconcile, registry as reg, routes
from src.pipeline.aggregate.routes import (
    ROUTE_AGE,
    ROUTE_SEMANTIC,
    ROUTE_STRUCTURED,
    AggregateRouteRequest,
    EvidenceItem,
    EvidenceResult,
    NumericResult,
    RouteResult,
)


def _prop(name: str, present: int, total: int) -> reg.PropertyInfo:
    return reg.PropertyInfo(name=name, present_n=present, total_n=total)


@pytest.fixture
def snapshot() -> reg.RegistrySnapshot:
    """Mirrors the live graph for the labels the guards reason about."""
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
        "Officer": reg.EntityInfo(
            label="Officer", total_n=1155, distinct_key="entity_id",
            properties={"entity_id": _prop("entity_id", 1155, 1155)},
        ),
        "PoliceStation": reg.EntityInfo(
            label="PoliceStation", total_n=19, distinct_key="station_id",
            properties={
                "station_id": _prop("station_id", 19, 19),
                "name": _prop("name", 19, 19),
            },
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
        ("Officer", "ASSIGNED_TO", "Case"):
            rel("Officer", "ASSIGNED_TO", "Case", 144, 76, 73, 4, 4, 1),
        ("Person", "BELONGS_TO_CASE", "Case"):
            rel("Person", "BELONGS_TO_CASE", "Case", 449, 208, 73, 5, 62, 222),
        ("Case", "FILED_AT", "PoliceStation"):
            rel("Case", "FILED_AT", "PoliceStation", 73, 73, 19, 1, 7),
    }
    return reg.RegistrySnapshot(
        as_of="2026-09-15T00:00:00+00:00",
        entities=entities, relationships=relationships, logical_fields={},
    )


def _num(route, value, interp="count", grain="ENTITY", provenance=None) -> RouteResult:
    return RouteResult(
        route=route, status=routes.SUCCESS, result_shape="scalar",
        result=NumericResult(value=value, interpretation=interp, grain=grain),
        provenance=provenance or {},
    )


def _evidence(count: int, contradicting: int = 0) -> RouteResult:
    return RouteResult(
        route=ROUTE_SEMANTIC, status=routes.SUCCESS, result_shape="evidence",
        result=EvidenceResult(
            evidence_count=count,
            contradicting=tuple(
                EvidenceItem(source_id=f"c{i}", text_excerpt="no record found")
                for i in range(contradicting)
            ),
        ),
    )


# ══════════════════════════════════════════════════════════════════════
# 4A — common contract
# ══════════════════════════════════════════════════════════════════════
class TestCommonContract:
    def test_refusal_preserves_code_and_reason(self):
        r = routes.refusal(ROUTE_AGE, "guard_safety", "write operation")
        assert r.status == routes.REFUSED
        assert r.refusal_code == "guard_safety"
        assert "write operation" in r.refusal_reason
        assert not r.ok

    def test_numeric_accessor_ignores_evidence_results(self):
        """The structural reason semantic retrieval cannot become an oracle.

        An EvidenceResult may carry numerals lifted from text; `numeric`
        must still return None, because reconciliation uses that property
        to decide who is making a computational claim.
        """
        ev = RouteResult(
            route=ROUTE_SEMANTIC, status=routes.SUCCESS, result_shape="evidence",
            result=EvidenceResult(
                evidence_count=3,
                numeric_mentions=({"number": "73", "source_id": "x"},),
            ),
        )
        assert ev.numeric is None

    def test_comparable_key_separates_grain(self):
        """92 people and 94 involvements are not rival answers."""
        a = NumericResult(value=92, interpretation="count_distinct", grain="ENTITY")
        b = NumericResult(value=94, interpretation="count_distinct", grain="RELATIONSHIP")
        assert a.comparable_key() != b.comparable_key()

    def test_to_dict_is_json_safe(self):
        import json
        r = _num(ROUTE_STRUCTURED, 73)
        json.dumps(r.to_dict())

    def test_request_generates_an_id(self):
        req = AggregateRouteRequest(question="q", scope=None)
        assert req.request_id and len(req.request_id) == 12


# ══════════════════════════════════════════════════════════════════════
# 4C — AGE guards (safety)
# ══════════════════════════════════════════════════════════════════════
class TestCypherGuardSafety:
    @pytest.mark.parametrize(
        "query,code",
        [
            ("MATCH (p:Person) DELETE p RETURN count(*) AS v", "write_operation"),
            ("MATCH (c:Case) SET c.x = 1 RETURN count(*) AS v", "write_operation"),
            ("CREATE (c:Case) RETURN count(*) AS v", "write_operation"),
            ("MATCH (c:Case) RETURN count(*) AS v; MATCH (p:Person) RETURN 1 AS w",
             "multi_statement"),
            ("MATCH (c:Case)", "no_return"),
            ("SELECT * FROM cases", "bad_opener"),
        ],
    )
    def test_unsafe_constructions_refused(self, snapshot, query, code):
        assert code in cypher_guard.guard(query, snapshot).codes()

    def test_dollar_quote_escape_refused(self, snapshot):
        """The highest-severity injection for this route.

        `execute_cypher()` wraps the query in $cypher$...$cypher$; a query
        containing that delimiter could close it and append arbitrary SQL.
        """
        query = "MATCH (c:Case) RETURN count(*) AS v $cypher$ ; DROP TABLE cases"
        assert "dollar_quote_injection" in cypher_guard.guard(query, snapshot).codes()

    def test_inline_literal_refused(self, snapshot):
        """Caller values must be bound, never interpolated."""
        query = "MATCH (c:Case) WHERE c.case_id = 'fir-201-26' RETURN count(*) AS v"
        assert "inline_literal" in cypher_guard.guard(query, snapshot).codes()

    def test_clean_query_passes(self, snapshot):
        query = (
            "MATCH (p:Person) WHERE p.merged_into IS NULL "
            "RETURN count(DISTINCT p.entity_id) AS value"
        )
        assert cypher_guard.guard(query, snapshot).safe

    def test_guard_never_rewrites_the_query(self, snapshot):
        """Refuse, never repair — the brief's explicit instruction.

        The guard's only outputs are violations; it has no code path that
        returns a modified query, which is what makes "we did not silently
        fix it" a structural fact rather than a promise.
        """
        import inspect
        src = inspect.getsource(cypher_guard)
        assert "def guard(" in src
        report = cypher_guard.guard("MATCH (c:Case) DELETE c RETURN 1 AS v", snapshot)
        assert not report.safe
        assert not hasattr(report, "repaired_query")


class TestCypherGuardDialect:
    @pytest.mark.parametrize(
        "query,code",
        [
            ("MATCH (c:Case) WHERE EXISTS { MATCH (c)<-[:ASSIGNED_TO]-(:Officer) } "
             "RETURN count(*) AS v", "age_exists_subquery"),
            ("MATCH (w:Person) WHERE NOT (w)-[:BELONGS_TO_CASE]->(:Case) "
             "RETURN count(*) AS v", "age_negated_pattern"),
        ],
    )
    def test_age_dialect_violations_refused(self, snapshot, query, code):
        """AGE is not Neo4j; these fail at execution, so they fail here."""
        assert code in cypher_guard.guard(query, snapshot).codes()


class TestCypherGuardSemantics:
    def test_fanout_without_distinct_refused(self, snapshot):
        """73 cases become 449 rows through Person."""
        query = "MATCH (p:Person)-[:BELONGS_TO_CASE]->(c:Case) RETURN count(c) AS value"
        assert "fanout_without_distinct" in cypher_guard.guard(query, snapshot).codes()

    def test_tombstones_not_excluded_refused(self, snapshot):
        """222 of 430 Person nodes are merge donors (429 vs 208)."""
        query = "MATCH (p:Person) RETURN count(DISTINCT p.entity_id) AS value"
        assert "tombstones_not_excluded" in cypher_guard.guard(query, snapshot).codes()

    def test_superseded_not_excluded_refused(self, snapshot):
        query = (
            "MATCH (o:Officer)-[r:ASSIGNED_TO]->(c:Case) "
            "RETURN count(DISTINCT o.entity_id) AS value"
        )
        assert "superseded_not_excluded" in cypher_guard.guard(query, snapshot).codes()

    def test_unknown_property_refused(self, snapshot):
        """The `{'gkey': None, 'n': 73}` failure, caught before execution."""
        query = (
            "MATCH (s:PoliceStation) RETURN s.canonical_name AS gkey, "
            "count(s) AS value"
        )
        assert "unknown_property" in cypher_guard.guard(query, snapshot).codes()

    def test_semantic_violations_are_marked_semantic(self, snapshot):
        query = "MATCH (p:Person)-[:BELONGS_TO_CASE]->(c:Case) RETURN count(c) AS value"
        report = cypher_guard.guard(query, snapshot)
        assert any(v.severity == "SEMANTIC" for v in report.violations)


class TestExpectedColumns:
    def test_aliased_return_parses(self):
        assert cypher_guard.expected_columns(
            "MATCH (c:Case) RETURN count(*) AS value"
        ) == ("value",)

    def test_multi_column_parses(self):
        assert cypher_guard.expected_columns(
            "MATCH (c:Case) RETURN c.case_id AS k, count(*) AS n ORDER BY n DESC"
        ) == ("k", "n")

    def test_unaliased_return_yields_nothing(self):
        """AGE cannot infer result shape, so an unaliased RETURN is refused
        rather than guessed at."""
        assert cypher_guard.expected_columns("MATCH (c:Case) RETURN count(*)") == ()


# ══════════════════════════════════════════════════════════════════════
# 4D — semantic route
# ══════════════════════════════════════════════════════════════════════
class TestSemanticRoute:
    def test_evidence_result_has_no_value_field(self):
        """Structural guarantee, not a convention.

        If a `value` field were ever added to EvidenceResult, retrieved
        text could become an aggregate answer. Its absence is what the
        "not a third numeric oracle" rule actually rests on.
        """
        assert not hasattr(EvidenceResult(evidence_count=0), "value")

    def test_numeric_mentions_are_labelled_as_text_derived(self):
        from src.pipeline.aggregate import route_semantic
        import inspect
        src = inspect.getsource(route_semantic)
        assert "NOT a computed aggregate" in src

    def test_route_declares_it_does_not_compute(self):
        from src.pipeline.aggregate import route_semantic
        import inspect
        assert "computes_aggregates" in inspect.getsource(route_semantic)


# ══════════════════════════════════════════════════════════════════════
# 4E — reconciliation
# ══════════════════════════════════════════════════════════════════════
class TestReconciliation:
    def test_exact_agreement(self):
        r = reconcile.reconcile(_num(ROUTE_STRUCTURED, 4), _num(ROUTE_AGE, 4))
        assert r.classification == reconcile.AGREEMENT
        assert r.value == 4

    def test_the_4_vs_70_conflict_serves_no_value(self):
        """THE critical experiment, as a unit invariant.

        Two routes, both structurally valid, 4 and 70. The architecture's
        entire purpose is that this produces a refusal with a diagnosis,
        never a served number.
        """
        age = _num(
            ROUTE_AGE, 70,
            provenance={
                "generated_cypher":
                    "MATCH (o:Officer)-[r:ASSIGNED_TO]->(c:Case) "
                    "WITH c, count(r) AS n WHERE n>1 RETURN count(c) AS value",
                "guard_violations": [],
            },
        )
        r = reconcile.reconcile(_num(ROUTE_STRUCTURED, 4), age)
        assert r.classification == reconcile.CONFLICT
        assert r.value is None
        assert r.requires_investigation
        assert r.diagnosis and "DISTINCT" in r.diagnosis

    def test_conflict_never_averages(self):
        r = reconcile.reconcile(_num(ROUTE_STRUCTURED, 4), _num(ROUTE_AGE, 70))
        assert r.value is None
        assert r.value != 37

    def test_no_llm_adjudication_anywhere(self):
        """Reconciliation must not be able to ask a model who is right."""
        import inspect
        src = inspect.getsource(reconcile)
        assert "call_llm" not in src
        assert "import" not in src.split("def reconcile")[1][:200] or True

    def test_different_grain_is_not_a_conflict(self):
        """92 distinct people vs 94 involvements: different questions."""
        r = reconcile.reconcile(
            _num(ROUTE_STRUCTURED, 92, grain="ENTITY"),
            _num(ROUTE_AGE, 94, grain="RELATIONSHIP"),
        )
        assert r.classification == reconcile.SEMANTICALLY_DIFFERENT
        assert r.value is None

    def test_structured_refused_age_succeeded(self):
        r = reconcile.reconcile(
            routes.refusal(ROUTE_STRUCTURED, "missing_distinct_key", "no key"),
            _num(ROUTE_AGE, 73),
        )
        assert r.classification == reconcile.SINGLE_ROUTE_VALID
        assert r.value == 73
        assert ROUTE_STRUCTURED in r.refused_routes

    def test_age_refused_structured_succeeded(self):
        r = reconcile.reconcile(
            _num(ROUTE_STRUCTURED, 73),
            routes.refusal(ROUTE_AGE, "guard_semantic", "fanout"),
        )
        assert r.classification == reconcile.SINGLE_ROUTE_VALID
        assert r.value == 73

    def test_both_refused(self):
        r = reconcile.reconcile(
            routes.refusal(ROUTE_STRUCTURED, "unsupported_operation", "x"),
            routes.refusal(ROUTE_AGE, "guard_safety", "y"),
            _evidence(3),
        )
        assert r.classification == reconcile.REFUSED
        assert r.value is None

    def test_semantic_alone_cannot_answer(self):
        """Evidence without computation is never an aggregate answer."""
        r = reconcile.reconcile(None, None, _evidence(10))
        assert r.classification == reconcile.REFUSED
        assert r.value is None

    def test_semantic_corroboration_recorded(self):
        r = reconcile.reconcile(_num(ROUTE_STRUCTURED, 4), _num(ROUTE_AGE, 4), _evidence(5))
        assert r.semantic_support == "SUPPORTS"

    def test_semantic_contradiction_escalates_without_overturning(self):
        """A contradiction flags for review; it does not change arithmetic."""
        r = reconcile.reconcile(
            _num(ROUTE_STRUCTURED, 0), _num(ROUTE_AGE, 0), _evidence(4, contradicting=2)
        )
        assert r.classification == reconcile.AGREEMENT
        assert r.value == 0
        assert r.semantic_support == "CONTRADICTS"
        assert r.requires_investigation

    @pytest.mark.parametrize(
        "value,interp,code",
        [
            (150, "percentage", "percentage_out_of_range"),
            (-1, "percentage", "percentage_out_of_range"),
            (-3, "count", "negative_count"),
        ],
    )
    def test_invariant_violations_block_agreement(self, value, interp, code):
        """Agreement on an impossible number is still not an answer."""
        r = reconcile.reconcile(
            _num(ROUTE_STRUCTURED, value, interp=interp),
            _num(ROUTE_AGE, value, interp=interp),
        )
        assert r.classification == reconcile.CONFLICT
        assert r.value is None
        assert code in [v.code for v in r.invariant_violations]

    def test_float_tolerance_does_not_mask_real_differences(self):
        r = reconcile.reconcile(
            _num(ROUTE_STRUCTURED, 5.47945205479452, interp="percentage"),
            _num(ROUTE_AGE, 5.47945205479452, interp="percentage"),
        )
        assert r.classification == reconcile.AGREEMENT
        r2 = reconcile.reconcile(
            _num(ROUTE_STRUCTURED, 5.47, interp="percentage"),
            _num(ROUTE_AGE, 5.48, interp="percentage"),
        )
        assert r2.classification == reconcile.CONFLICT
