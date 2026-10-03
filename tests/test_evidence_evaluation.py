from uuid import uuid4
from src.evidence.evaluation import inspect_answer
from src.evidence.types import Answer,Claim,Coverage,EvidenceResult,QueryOperation,Scope,SourceSpan


def test_quoted_statute_numbers_are_not_aggregate_claims():
    scope=Scope(user_id=uuid4(),session_id=uuid4())
    text="Section 154"
    span=SourceSpan(span_id=uuid4(),document_id=uuid4(),start=0,end=len(text),text=text)
    result=EvidenceResult(output_id="law",scope_fingerprint=scope.fingerprint(),
       operation=QueryOperation(output_id="law",operation="search",text="law"),label="Law",citations=[span])
    answer=Answer(run_id=uuid4(),status="partial",text=text,results=[result],
       claims=[Claim(result_id=result.result_id,output_id="law",span_id=span.span_id,quote=text)])
    checks=inspect_answer(answer,scope)
    assert checks["measurements"]==[] and checks["claim_failures"]==[]
    assert checks["correctness"]=="unreviewed"


def test_incomplete_population_claim_is_a_failure_without_text_heuristics():
    scope=Scope(user_id=uuid4(),session_id=uuid4())
    op=QueryOperation(output_id="count",operation="count_entities",subject_type=uuid4())
    result=EvidenceResult(output_id="count",scope_fingerprint=scope.fingerprint(),operation=op,label="Count",
        rows=[{"value":19}],coverage=Coverage(population_documents=10,complete_documents=1))
    answer=Answer(run_id=uuid4(),status="answered",text="Documents checked",results=[result],
        claims=[Claim(result_id=result.result_id,output_id="count")])
    assert inspect_answer(answer,scope)["claim_failures"]
