# -*- coding: utf-8 -*-
"""
Phase 2 regression tests — the corpus's own integrity, and the guard that
Phase 2 discovered was missing.

WHY A TEST FILE FOR A TEST CORPUS. `corpus.py` carries expected VALUES.
A corpus whose expectations drift, or whose expectations were derived from
the engine it is meant to check, is worse than no corpus: it converts a
regression into a green tick. So these tests pin the corpus's structural
rules rather than re-asserting its numbers, and one of them exists purely
to stop a future change from "fixing" a disagreement by loosening a
tolerance.

OFFLINE. Nothing here touches the database. The live figures live in
`corpus.py` with their provenance; running them against real data is
`scripts/run_aggregate_corpus.py`'s job.
"""
from __future__ import annotations

import inspect

import pytest

from src.pipeline.aggregate import corpus as corpus_mod
from src.pipeline.aggregate import validator as validator_mod
from src.pipeline.aggregate.corpus import build_corpus, coverage_summary
from src.pipeline.aggregate.spec import AggregateSpec


@pytest.fixture(scope="module")
def cases():
    return build_corpus()


# ══════════════════════════════════════════════════════════════════════
# The Phase 2 defect: declared-but-unemitted spec fields
# ══════════════════════════════════════════════════════════════════════
class TestUnimplementedFieldsAreRefusedNotIgnored:
    """THE Phase 2 finding, measured 2026-09-15.

    `compare`, `time_window`, `threshold` and a second `group_by`
    dimension were all accepted by the validator and consumed by the
    compiler ZERO times. A spec saying "count cases, restricted to 2024"
    compiled to byte-identical Cypher as one with no window at all and
    returned 73 — the grand total — while the receipt asserted a filter had
    been applied.

    That is Module 144's defect ("How many FIRs registered in 2019 or
    earlier?" -> 73) reproduced inside the engine built to prevent it, and
    worse, because the receipt lent it false provenance.
    """

    def test_the_guard_list_matches_compiler_reality(self):
        """The contract between spec.py and compiler.py, enforced.

        Every field named in `_UNIMPLEMENTED_FIELDS` must genuinely be
        absent from the compiler. If someone implements one and forgets to
        remove its entry, the engine would refuse a capability it now has;
        if someone adds a spec field and forgets to add an entry, it would
        silently ignore it. This test fails in the first direction, and
        `test_every_spec_field_is_either_consumed_or_guarded` fails in the
        second.
        """
        from src.pipeline.aggregate import compiler as compiler_mod

        compiler_src = inspect.getsource(compiler_mod)
        for field in validator_mod._UNIMPLEMENTED_FIELDS:
            assert f"spec.{field}" not in compiler_src, (
                f"{field!r} is listed as unimplemented but the compiler now "
                f"reads it — remove it from _UNIMPLEMENTED_FIELDS in the same "
                f"change that implements it"
            )

    def test_every_spec_field_is_either_consumed_or_guarded(self):
        """No spec field may be silently ignored.

        Each declared field must be either read by the compiler/executor or
        listed as unimplemented. A field in neither set is a silent-ignore
        path — exactly the class of bug this phase found.
        """
        from src.pipeline.aggregate import compiler as compiler_mod
        from src.pipeline.aggregate import executor as executor_mod

        consumed = inspect.getsource(compiler_mod) + inspect.getsource(executor_mod)
        guarded = set(validator_mod._UNIMPLEMENTED_FIELDS)
        # Fields that are metadata rather than semantics: they describe the
        # request or its provenance and do not change the computation.
        metadata = {"question_text", "scope", "null_policy", "canonicalization"}

        for field in AggregateSpec.__dataclass_fields__:
            if field in metadata or field in guarded:
                continue
            assert f"spec.{field}" in consumed, (
                f"spec field {field!r} is neither consumed by the "
                f"compiler/executor nor listed in _UNIMPLEMENTED_FIELDS — it "
                f"would be silently ignored"
            )

    @pytest.mark.parametrize("field", ["compare", "time_window", "threshold"])
    def test_named_fields_are_guarded(self, field):
        assert field in validator_mod._UNIMPLEMENTED_FIELDS


# ══════════════════════════════════════════════════════════════════════
# Corpus integrity
# ══════════════════════════════════════════════════════════════════════
class TestCorpusIntegrity:
    def test_corpus_is_non_trivial(self, cases):
        assert len(cases) >= 25

    def test_case_names_are_unique(self, cases):
        names = [c.name for c in cases]
        assert len(names) == len(set(names))

    def test_value_cases_carry_independent_ground_truth(self, cases):
        """A value case without provenance is an unverifiable assertion.

        `route` must name how the figure was established, and it must not
        be this package's own compiler — that is the whole point of the
        independent-ground-truth requirement.
        """
        allowed = {"SQL", "GRAPH", "PYTHON", "REGISTRY"}
        for c in cases:
            if c.outcome != "value":
                continue
            assert c.truth is not None, f"{c.name}: value case with no ground truth"
            assert c.truth.route in allowed, f"{c.name}: route {c.truth.route!r}"
            assert c.truth.note, f"{c.name}: ground truth has no derivation note"

    def test_refusal_cases_name_the_expected_code(self, cases):
        """Refusing for the wrong reason is its own defect.

        A case that only asserted "it refused" would pass while a guard
        fired for an unrelated reason, which is how a fanout bug could hide
        behind a scope check.
        """
        for c in cases:
            if c.outcome == "refused":
                assert c.refusal_code, f"{c.name}: refusal case names no code"

    def test_ratio_ground_truth_is_exact_not_rounded(self, cases):
        """Pins the fixture bug this phase produced.

        The corpus originally held `round(100*4/73, 4)` = 5.4795 while the
        engine correctly returned 5.47945205479452. The engine was right and
        the fixture was lossy — a disagreement that would most easily have
        been "fixed" by widening the comparison tolerance, which would then
        have masked genuine drift. Ratio expectations are therefore held as
        exact expressions.
        """
        for c in cases:
            if c.derived_axis != "ratio" or c.outcome != "value":
                continue
            assert c.truth is not None
            value = c.truth.value
            if isinstance(value, float):
                assert value != round(value, 4) or value == int(value), (
                    f"{c.name}: ratio ground truth {value!r} looks rounded; "
                    f"hold the exact expression instead"
                )

    def test_specs_use_only_the_shared_operator_algebra(self, cases):
        """Evidence that the corpus is bindings, not bespoke code.

        Every case is an `AggregateSpec`. If a case ever needed its own
        emitter or a question-specific field, this assertion is where that
        would surface.
        """
        for c in cases:
            assert isinstance(c.spec, AggregateSpec)
            assert c.spec.grain in (
                "ENTITY", "RELATIONSHIP", "ROLE_PAIR", "EVENT", "RECORD"
            )

    def test_no_case_bypasses_scope(self, cases):
        """Scope is caller-supplied; no case may invent a wider one."""
        for c in cases:
            assert c.spec.scope.kind in ("cross_case", "jurisdiction")


# ══════════════════════════════════════════════════════════════════════
# Coverage accounting
# ══════════════════════════════════════════════════════════════════════
class TestCoverageAccounting:
    def test_summary_counts_every_case_on_every_axis(self, cases):
        summary = coverage_summary(cases)
        for axis, counts in summary.items():
            assert sum(counts.values()) == len(cases), (
                f"axis {axis!r} does not account for every case"
            )

    def test_refusals_and_values_are_counted_separately(self, cases):
        """Coverage must not be inflated by counting guards as capabilities.

        A refusal proves a guard fired; it does not prove the engine can
        compute anything. The report keeps the two apart, and this pins
        that the corpus carries both kinds in meaningful numbers.
        """
        summary = coverage_summary(cases)
        outcomes = summary["outcome"]
        assert outcomes.get("value", 0) >= 10
        assert outcomes.get("refused", 0) >= 10

    def test_measure_axis_spans_more_than_counting(self, cases):
        """count/count_distinct alone would not exercise the algebra."""
        measures = coverage_summary(cases)["measure"]
        for m in ("min", "max", "avg", "sum"):
            assert measures.get(m, 0) >= 1, f"no case exercises {m}"

    def test_guard_axis_covers_the_known_wrong_number_paths(self, cases):
        """Every hazard Phase 0/1 measured must have at least one case."""
        guards = coverage_summary(cases)["guard"]
        for g in (
            "fanout", "tombstone", "sparse_property", "null_grouping",
            "zero_denominator", "scope", "unimplemented",
        ):
            assert guards.get(g, 0) >= 1, f"no case exercises the {g!r} guard"
