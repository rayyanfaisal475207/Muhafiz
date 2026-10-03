"""Qualified measurements must preserve population and denominator semantics."""
from uuid import uuid4

import pytest

from src.evidence.answer import render, validate_claim
from src.evidence.compiler import compile_operation
from src.evidence.types import (Claim, Concept, Coverage, EvidenceResult, MetricEvidence,
                                QueryOperation, QuestionPlan, Scope)


def fixture_metric():
    scope = Scope(user_id=uuid4(), session_id=uuid4())
    subject, start, end = uuid4(), uuid4(), uuid4()
    op = QueryOperation(output_id="delay", operation="average_duration", subject_type=subject,
                        start_predicate_id=start, end_predicate_id=end, allow_observed_subset=True)
    metric = MetricEvidence(eligible_records=3, observed_records=2, missing_records=1,
                            unknown_cohort_records=1, start_predicate_id=start, end_predicate_id=end)
    result = EvidenceResult(output_id="delay", operation=op, scope_fingerprint=scope.fingerprint(),
        label="2024 reporting delay", unit="seconds", rows=[{"value": 900, "observed_records": 2}],
        coverage=Coverage(population_documents=4, complete_documents=4, missing_values=1),
        metric=metric, status="partial")
    return scope, op, result


def test_unextracted_list_does_not_discard_supported_part_of_compound_answer():
    scope,op,result=fixture_metric()
    missing_op=QueryOperation(output_id='events',operation='list',subject_type=uuid4())
    missing=EvidenceResult(output_id='events',operation=missing_op,label='Unextracted events',
        scope_fingerprint=scope.fingerprint(),status='not_extracted',rows=[{'label':'Unverified event'}])
    answer=render(uuid4(),QuestionPlan(question='delay and events?',standalone_question='delay and events?',
        outputs=[op,missing_op]),[result,missing],scope)
    assert answer.status=='partial' and len(answer.claims)==1
    assert '900 seconds' in answer.text and 'Unverified event' not in answer.text


@pytest.mark.parametrize("language", ["en", "ur", "roman_ur"])
def test_observed_average_is_useful_and_explicit_in_each_language(language):
    scope, op, result = fixture_metric()
    answer = render(uuid4(), QuestionPlan(question="delay?", standalone_question="delay?",
                     language=language, outputs=[op]), [result], scope)
    assert "900 seconds" in answer.text and "2/3" in answer.text and "2024" in answer.text
    assert answer.status == "partial" and len(answer.claims) == 1
    assert answer.claims[0].population_basis == "observed_subset"
    assert validate_claim(answer.claims[0], [result], scope) == 900


def test_observed_value_cannot_be_relabelled_as_complete_average():
    scope, _, result = fixture_metric()
    with pytest.raises(ValueError, match="incomplete population"):
        validate_claim(Claim(result_id=result.result_id, output_id=result.output_id), [result], scope)


@pytest.mark.parametrize("failure", ["extraction", "identity", "conflict", "zero_observations", "sum", "opt_out"])
def test_partial_permission_does_not_disable_evidence_checks(failure):
    scope, op, result = fixture_metric()
    if failure == "extraction": result.coverage.complete_documents = 3
    if failure == "identity": result.coverage.unresolved_identities = 1
    if failure == "conflict": result.coverage.disputed_assertions = 1
    if failure == "zero_observations": result.rows[0]["observed_records"] = 0
    if failure == "sum": result.operation = QueryOperation(output_id="delay", operation="sum", predicate_id=uuid4())
    if failure == "opt_out": op.allow_observed_subset = False
    with pytest.raises(ValueError, match="observed-subset"):
        validate_claim(Claim(result_id=result.result_id, output_id="delay", population_basis="observed_subset"), [result], scope)


def test_duration_compilation_separates_cohort_and_deduplicates_timestamps():
    _, op, _ = fixture_metric()
    from datetime import datetime, timezone
    op.occurred_after = datetime(2024, 1, 1, tzinfo=timezone.utc)
    op.occurred_before = datetime(2025, 1, 1, tzinfo=timezone.utc)
    concepts = {key: Concept(concept_id=key, label=label, kind=kind, meaning=label,
                  grain="one event", value_type=value_type) for key, label, kind, value_type in [
        (op.subject_type, "Event", "entity_type", "entity"),
        (op.start_predicate_id, "Incident", "property", "date"),
        (op.end_predicate_id, "Reported", "property", "date")]}
    compiled = compile_operation(op, concepts)
    assert "OR NOT EXISTS" not in compiled.population_sql
    assert "count(DISTINCT t.value_date)" in compiled.sql
    assert "unknown" not in compiled.population_sql
    assert "NOT EXISTS" in compiled.unknown_cohort_sql
    assert op.occurred_after not in compiled.unknown_cohort_params


def test_denominators_must_reconcile():
    with pytest.raises(ValueError, match="reconcile"):
        MetricEvidence(eligible_records=3, observed_records=3, missing_records=1,
                       start_predicate_id=uuid4(), end_predicate_id=uuid4())


def test_standard_comparison_notation_has_identical_typed_meaning():
    from src.evidence.types import PredicateFilter
    key = uuid4()
    assert PredicateFilter(predicate_id=key,operator='>=',value=1).operator == 'gte'
    assert PredicateFilter(predicate_id=key,operator='<',value=2).operator == 'lt'
    with pytest.raises(ValueError): PredicateFilter(predicate_id=key,operator='approximately',value=1)
