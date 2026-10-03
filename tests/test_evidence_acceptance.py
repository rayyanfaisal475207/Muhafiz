import hashlib
from uuid import uuid4

from scripts.evidence_acceptance import check_record,evaluate,first_attempt,expected_rows_match,deterministic_artifacts_pass
from src.evidence.types import Answer,Scope


def example():
    source="The source does not state a registration date."
    scenario={"id":"registration-date-missing","questions":dict.fromkeys(("en","ur","roman_ur"),"q"),
        "reviewer":"source-reviewer","source_evidence":[{"text":source,"sha256":hashlib.sha256(source.encode()).hexdigest()}],
        "expected":{"status":"not_extracted"}}
    record={"scenario_id":scenario["id"],"language":"en","attempt":1,
        "scope":Scope(user_id=uuid4(),session_id=uuid4()).model_dump(mode="json"),
        "answer":Answer(run_id=uuid4(),status="not_extracted",text="Registration date not recorded.").model_dump(mode="json"),
        "sources":{},"review":{"reviewer":"independent-reviewer","correct":True}}
    return scenario,record


def test_missing_runs_and_small_dataset_cannot_certify_release():
    scenario,record=example()
    report=evaluate({"scenarios":[scenario],"deterministic_authorization_passed":True,
                     "deterministic_provenance_passed":True},[record])
    assert not report["release_ready"]
    assert any("missing_run" in item["failures"] for item in report["failures"])


def test_matching_output_without_source_review_is_not_correctness():
    scenario,record=example();record["review"]={}
    assert "correctness_review" in check_record(scenario,record)


def test_expected_answers_must_preserve_source_hash():
    scenario,record=example();scenario["source_evidence"][0]["text"]="Changed evidence"
    assert "expectation_source_hash" in check_record(scenario,record)


def test_unsupported_claim_is_fatal_even_when_reviewer_marks_correct():
    scenario,record=example();record["review"]["unsupported_claim"]=True
    assert "unsupported_claim" in check_record(scenario,record)


def test_duplicate_runs_do_not_increase_coverage():
    scenario,record=example()
    report=evaluate({"scenarios":[scenario]},[record,record])
    assert not report["release_ready"] and report["recorded_runs"]==1
    assert any("duplicate_run" in item["failures"] for item in report["failures"])


def test_transport_retry_is_not_first_attempt_correctness():
    assert not first_attempt({'answer':{'health':{'transient_retries':1}}})
    assert first_attempt({'answer':{'health':{'transient_retries':0,'repair_used':False}}})


def test_reviewed_values_allow_extra_diagnostics_but_not_wrong_measurement():
    assert expected_rows_match([{'value':'10','observed_records':2}],[{'value':10}])
    assert not expected_rows_match([{'value':'11'}],[{'value':10}])


def test_deterministic_gates_require_actual_matching_successful_artifacts(tmp_path):
    path=tmp_path/'results.xml'
    path.write_text('<testsuites><testsuite><testcase classname="security" name="access"/></testsuite></testsuites>')
    artifact={'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'test_classes':['security']}
    manifest={'deterministic_artifacts':dict.fromkeys(['authorization','provenance'],artifact)}
    assert deterministic_artifacts_pass(manifest)
    path.write_text('<testsuites><testsuite><testcase classname="security"><failure/></testcase></testsuite></testsuites>')
    assert not deterministic_artifacts_pass(manifest)
    artifact['sha256']=hashlib.sha256(path.read_bytes()).hexdigest()
    assert not deterministic_artifacts_pass(manifest)
