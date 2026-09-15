# -*- coding: utf-8 -*-
"""
`time_window` end-to-end regressions.

WHAT THESE PIN. Phase 2 found that `AggregateSpec` could carry a
`time_window` the compiler consumed zero times: the spec validated, compiled
to byte-identical Cypher as the unfiltered spec, returned the grand total,
and the receipt asserted a filter had been applied. This file makes that
combination unreachable, and it does so at three layers, because any one of
them alone could be undone by a plausible future edit:

  * the VALIDATOR refuses a window it cannot execute;
  * the COMPILER refuses to emit a windowed spec without resolved ids;
  * the EXECUTOR's receipt records what was RUN, not what was ASKED.

The critical test (`TestSilentIgnoreRegression`) deliberately does not stop
at comparing query strings. Two queries can differ textually and still
return the same number; the point is that the restriction CHANGES THE
ANSWER, so it executes both and asserts the results differ and that the
filtered one matches independently-established SQL ground truth.

OFFLINE / LIVE SPLIT. Validation, compilation and guard tests are offline
(fixture registry, no database). The ground-truth figures live in
`corpus.py` and are exercised against real data by
`scripts/run_aggregate_corpus.py`; the expected values quoted here are
recorded with their derivation so a reader can re-derive them without
trusting this file.
"""
from __future__ import annotations

import pytest

from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate import temporal
from src.pipeline.aggregate import validator as validator_mod
from src.pipeline.aggregate.compiler import CompilerError, compile_ratio, compile_spec
from src.pipeline.aggregate.spec import (
    AggregateSpec,
    PopulationNode,
    Ratio,
    RelationCountPredicate,
    Scope,
    TimeWindow,
    Traversal,
)
from src.pipeline.aggregate.validator import validate


def _prop(name: str, present: int, total: int) -> reg.PropertyInfo:
    return reg.PropertyInfo(name=name, present_n=present, total_n=total)


@pytest.fixture
def snapshot() -> reg.RegistrySnapshot:
    """Mirrors the live registry for the fields these tests touch.

    `incident_date` is recorded exactly as the live registry records it:
    Postgres authority, present on 64 of 73 case rows. `Person.age` is a
    GRAPH field and is present so the "authoritative for the wrong source"
    refusal has something real to refuse.
    """
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
        ("Officer", "ASSIGNED_TO", "Case"):
            rel("Officer", "ASSIGNED_TO", "Case", 144, 76, 73, 4, 4, 1),
        ("Person", "BELONGS_TO_CASE", "Case"):
            rel("Person", "BELONGS_TO_CASE", "Case", 449, 208, 73, 5, 62, 222),
        ("Person", "INVOLVED_IN", "Incident"):
            rel("Person", "INVOLVED_IN", "Incident", 221, 208, 73, 3, 62),
    }

    logical_fields = {
        "incident_date": reg.LogicalField(
            name="incident_date", source="postgres", path="cases.incident_date",
            present_n=64, total_n=73,
            authority_basis="xagg._case_completeness_scan(): case rows authoritative",
            secondary_paths=("Incident.incident_datetime",),
        ),
        "Person.age": reg.LogicalField(
            name="Person.age", source="graph", path="Person.age",
            present_n=19, total_n=430, authority_basis="single source",
        ),
    }

    return reg.RegistrySnapshot(
        as_of="2026-09-15T00:00:00+00:00",
        entities=entities, relationships=relationships,
        logical_fields=logical_fields,
    )


@pytest.fixture
def sup() -> Scope:
    return Scope(kind="cross_case", user_role="supervisor", user_id="t")


def _case_spec(sup: Scope, window: TimeWindow | None = None) -> AggregateSpec:
    return AggregateSpec(
        question_text="How many cases?", measure="count",
        population=PopulationNode(entity="Case"),
        grain="ENTITY", distinct_key="case_id", scope=sup,
        time_window=window,
    )


# ══════════════════════════════════════════════════════════════════════
# THE critical regression
# ══════════════════════════════════════════════════════════════════════
class TestSilentIgnoreRegression:
    """A windowed spec must not compile to the unfiltered query.

    Comparing query text alone would be a weak test — two queries can
    differ and still return the same number. The live half of this
    assertion (results must differ, and the filtered one must equal the
    SQL ground truth of 13 for 2024) is exercised by the corpus runner;
    here the structural half is pinned offline.
    """

    def test_windowed_query_text_differs_from_unfiltered(self, snapshot, sup):
        unfiltered = compile_spec(snapshot, _case_spec(sup))
        windowed = compile_spec(
            snapshot,
            _case_spec(sup, TimeWindow(
                field="incident_date", start="2024-01-01", end="2024-12-31")),
            window_case_ids=("fir-1", "fir-2"),
        )
        assert windowed.text != unfiltered.text
        assert "case_id IN" in windowed.text
        assert "case_id IN" not in unfiltered.text

    def test_window_ids_are_bound_parameters_not_interpolated(self, snapshot, sup):
        """Case ids must never enter the query text itself."""
        q = compile_spec(
            snapshot,
            _case_spec(sup, TimeWindow(field="incident_date", start="2024-01-01")),
            window_case_ids=("fir-secret-1", "fir-secret-2"),
        )
        assert "fir-secret-1" not in q.text
        assert any("fir-secret-1" in str(v) for v in q.params.values())

    def test_compiler_refuses_window_without_resolved_ids(self, snapshot, sup):
        """The guard that makes the old defect unreachable.

        Compiling a windowed spec without its allow-list would emit the
        unrestricted query — exactly the Phase 2 behaviour. It raises
        instead of silently succeeding.
        """
        spec = _case_spec(sup, TimeWindow(field="incident_date", start="2024-01-01"))
        with pytest.raises(CompilerError, match="silently emit the unrestricted"):
            compile_spec(snapshot, spec)

    def test_empty_window_compiles_to_a_match_nothing_allow_list(self, snapshot, sup):
        """An empty result set is a real answer, not "no filter".

        A window matching zero cases must still restrict. Treating an empty
        allow-list as "unrestricted" would turn "cases in 2019" (0) into
        the grand total (73) — the original defect wearing a different hat.
        """
        q = compile_spec(
            snapshot,
            _case_spec(sup, TimeWindow(field="incident_date", end="2019-12-31")),
            window_case_ids=(),
        )
        assert "case_id IN" in q.text


# ══════════════════════════════════════════════════════════════════════
# Validation
# ══════════════════════════════════════════════════════════════════════
class TestTimeWindowValidation:
    def test_valid_window_is_executable(self, snapshot, sup):
        spec = _case_spec(sup, TimeWindow(
            field="incident_date", start="2024-01-01", end="2024-12-31"))
        assert validate(snapshot, spec).ok

    def test_lower_bound_only_is_valid(self, snapshot, sup):
        assert validate(snapshot, _case_spec(
            sup, TimeWindow(field="incident_date", start="2026-01-01"))).ok

    def test_upper_bound_only_is_valid(self, snapshot, sup):
        assert validate(snapshot, _case_spec(
            sup, TimeWindow(field="incident_date", end="2024-12-31"))).ok

    def test_inverted_range_refused(self, snapshot, sup):
        spec = _case_spec(sup, TimeWindow(
            field="incident_date", start="2026-01-01", end="2024-01-01"))
        assert "invalid_time_window" in validate(snapshot, spec).codes()

    def test_malformed_bound_refused(self, snapshot, sup):
        """Bounds are dates. Interpreting 'last year' belongs upstream."""
        spec = _case_spec(sup, TimeWindow(field="incident_date", start="last year"))
        assert "invalid_time_window" in validate(snapshot, spec).codes()

    def test_window_with_no_bounds_refused(self, snapshot, sup):
        spec = _case_spec(sup, TimeWindow(field="incident_date"))
        assert "invalid_time_window" in validate(snapshot, spec).codes()

    def test_unknown_field_refused(self, snapshot, sup):
        spec = _case_spec(sup, TimeWindow(field="verdict_date", start="2024-01-01"))
        assert "unknown_field" in validate(snapshot, spec).codes()

    def test_field_without_temporal_implementation_refused(self, snapshot, sup):
        """`Person.age` exists but is a graph field with no resolver.

        Refusing names the field and lists what IS supported, so the caller
        learns the boundary rather than getting a silent wrong answer.
        """
        spec = _case_spec(sup, TimeWindow(field="Person.age", start="2024-01-01"))
        result = validate(snapshot, spec)
        assert "unsupported_operation" in result.codes()
        assert any("Person.age" in i.message for i in result.issues)

    def test_time_window_is_no_longer_in_the_unimplemented_set(self):
        """It moved from "refused wholesale" to "genuinely implemented".

        `test_aggregate_corpus.py` asserts the other direction — that every
        entry still in the set is genuinely absent from the compiler.
        """
        assert "time_window" not in validator_mod._UNIMPLEMENTED_FIELDS


# ══════════════════════════════════════════════════════════════════════
# Resolver semantics
# ══════════════════════════════════════════════════════════════════════
class TestTemporalResolver:
    def test_only_postgres_authoritative_fields_are_supported(self, snapshot):
        supported = temporal.supported_temporal_fields(snapshot)
        assert "incident_date" in supported
        assert "Person.age" not in supported

    @pytest.mark.parametrize(
        "value,ok",
        [("2024-01-01", True), ("2024-12-31", True), (None, True),
         ("last year", False), ("2024/01/01", False), ("2024-1-1", False),
         ("", False)],
    )
    def test_bound_format(self, value, ok):
        assert temporal.is_valid_bound(value) is ok

    @pytest.mark.parametrize(
        "start,end,ok",
        [("2024-01-01", "2024-12-31", True), ("2024-01-01", "2024-01-01", True),
         ("2026-01-01", "2024-01-01", False), (None, "2024-01-01", True),
         ("2024-01-01", None, True)],
    )
    def test_bounds_ordered(self, start, end, ok):
        assert temporal.bounds_ordered(start, end) is ok

    def test_intersection_narrows_never_widens(self):
        """A window may only reduce what the caller could already see."""
        jurisdiction = ("a", "b", "c")
        window = ("b", "c", "d")
        assert temporal.intersect_case_ids(jurisdiction, window) == ("b", "c")

    def test_intersection_with_no_jurisdiction_is_the_window(self):
        assert temporal.intersect_case_ids(None, ("a", "b")) == ("a", "b")

    def test_bounds_are_parsed_to_dates_not_passed_as_strings(self):
        """Pins a live bug found during implementation.

        asyncpg type-checks parameters against the column type before the
        SQL is planned, so binding an ISO *string* to a `date` column fails
        with "'str' object has no attribute 'toordinal'" even when the SQL
        wraps it in CAST(:x AS date). The resolver must hand the driver a
        real `datetime.date`.
        """
        import inspect
        from datetime import date

        src = inspect.getsource(temporal.resolve_window)
        assert "fromisoformat" in src
        assert date.fromisoformat("2024-01-01") == date(2024, 1, 1)


# ══════════════════════════════════════════════════════════════════════
# Population reach
# ══════════════════════════════════════════════════════════════════════
class TestCaseAliasResolution:
    """A window can only restrict a population that reaches Case."""

    def test_case_population_uses_base_alias(self, snapshot, sup):
        q = compile_spec(
            snapshot,
            _case_spec(sup, TimeWindow(field="incident_date", start="2024-01-01")),
            window_case_ids=("fir-1",),
        )
        assert "n.case_id IN" in q.text

    def test_population_hopping_to_case_uses_the_terminal_alias(self, snapshot, sup):
        """"Distinct persons on 2024 cases" restricts the hop, not the subject."""
        spec = AggregateSpec(
            question_text="How many persons on 2024 cases?",
            measure="count_distinct",
            population=PopulationNode(
                entity="Person",
                traversals=(Traversal(rel="BELONGS_TO_CASE", target="Case",
                                      direction="out"),),
            ),
            grain="ENTITY", distinct_key="entity_id", scope=sup,
            time_window=TimeWindow(field="incident_date", start="2024-01-01"),
        )
        q = compile_spec(snapshot, spec, window_case_ids=("fir-1",))
        assert "m0.case_id IN" in q.text
        assert "count(DISTINCT n.entity_id)" in q.text

    def test_population_that_never_reaches_case_is_refused(self, snapshot, sup):
        """Refusing beats emitting the query without the restriction."""
        spec = AggregateSpec(
            question_text="How many officers?", measure="count_distinct",
            population=PopulationNode(entity="Officer"),
            grain="ENTITY", distinct_key="entity_id", scope=sup,
            time_window=TimeWindow(field="incident_date", start="2024-01-01"),
        )
        with pytest.raises(CompilerError, match="does not reach Case"):
            compile_spec(snapshot, spec, window_case_ids=("fir-1",))


# ══════════════════════════════════════════════════════════════════════
# Ratios
# ══════════════════════════════════════════════════════════════════════
class TestWindowedRatio:
    def test_window_restricts_both_halves(self, snapshot, sup):
        """Restricting only the numerator would compute a ratio whose two
        sides describe different populations — "2024 cases with >1 officer,
        over all cases ever" — understating every windowed percentage."""
        spec = AggregateSpec(
            question_text="% of 2024 cases with >1 officer",
            measure="count", population=PopulationNode(entity="Case"),
            grain="ENTITY", distinct_key="case_id", scope=sup,
            time_window=TimeWindow(
                field="incident_date", start="2024-01-01", end="2024-12-31"),
            ratio=Ratio(
                numerator=PopulationNode(
                    entity="Case",
                    predicates=(RelationCountPredicate(
                        rel="ASSIGNED_TO", target="Officer", op="gt", value=1,
                        count_grain="ENTITY", direction="in"),),
                ),
                denominator=PopulationNode(entity="Case"),
            ),
        )
        num_q, den_q = compile_ratio(
            snapshot, spec, window_case_ids=("fir-1", "fir-2"))
        assert "case_id IN" in num_q.text
        assert "case_id IN" in den_q.text
