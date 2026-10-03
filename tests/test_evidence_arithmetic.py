from uuid import uuid4

import pytest

from src.evidence.answer import render, validate_claim
from src.evidence.arithmetic import derive
from src.evidence.compiler import InvalidPlan
from src.evidence.types import Claim, Coverage, EvidenceResult, QueryOperation, QuestionPlan, Scope


def operands():
    scope = Scope(user_id=uuid4(),session_id=uuid4())
    concept = uuid4()
    results = [EvidenceResult(output_id=key,scope_fingerprint=scope.fingerprint(),label=key,
        operation=QueryOperation(output_id=key,operation='count_entities',subject_type=concept),
        rows=[{'value':value}],unit='records',coverage=Coverage(population_documents=4,complete_documents=4))
        for key,value in [('before',2),('after',6)]]
    return scope,results


def test_checked_comparison_is_computed_and_rendered_with_operand_provenance():
    scope,results = operands()
    op = QueryOperation(output_id='change',operation='difference',operands=[{'output_id':'after'},{'output_id':'before'}])
    assert op.depends_on == ['after','before']
    result = derive(op,results,scope)
    plan = QuestionPlan(question='compare',standalone_question='compare',outputs=[*(r.operation for r in results),op])
    answer = render(uuid4(),plan,[*results,result],scope)
    assert 'Difference: 4 records' in answer.text
    assert len(result.derivation['claims']) == 2
    result.rows[0]['value'] = '400'
    with pytest.raises(ValueError,match='differs'):
        validate_claim(Claim(result_id=result.result_id,output_id='change'),[*results,result],scope)


@pytest.mark.parametrize('bad',['unit','meaning','coverage','scope','zero'])
def test_incompatible_or_unsupported_operands_are_rejected(bad):
    scope,results = operands()
    if bad == 'unit': results[0].unit='weapons'
    if bad == 'meaning': results[0].operation.subject_type=uuid4()
    if bad == 'coverage': results[0].coverage.complete_documents=1
    if bad == 'scope': results[0].scope_fingerprint='other-user'
    if bad == 'zero': results[0].rows[0]['value']=0
    op = QueryOperation(output_id='ratio',operation='ratio',operands=[{'output_id':'after'},{'output_id':'before'}])
    with pytest.raises(InvalidPlan): derive(op,results,scope)
