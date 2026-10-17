"""Evaluate saved runtime answers against independently reviewed source answers.

No model judge and no SQL-derived truth. Run records must include the full Answer,
Scope, original source documents and an independent correctness review. A missing
review, missing run, fabricated span or incomplete definitive count fails closed.
This gate is intentionally separate from deterministic unit tests.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
from decimal import Decimal, InvalidOperation
import xml.etree.ElementTree as ET

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from src.evidence.answer import validate_claim
from src.evidence.types import Answer,Scope

LANGUAGES=("en","ur","roman_ur")


def first_attempt(record):
    health=(record or {}).get('answer',{}).get('health') or {}
    return not health.get('repair_used') and not health.get('transient_retries',0)


def deterministic_artifacts_pass(manifest):
    """Boolean declarations alone cannot certify deterministic release checks."""
    artifacts=manifest.get('deterministic_artifacts',{})
    for gate in ['authorization','provenance']:
        artifact=artifacts.get(gate,{})
        path=Path(artifact.get('path',''))
        try:
            content=path.read_bytes()
            if hashlib.sha256(content).hexdigest()!=artifact.get('sha256'): return False
            suites=ET.fromstring(content)
            cases=suites.findall('.//testcase')
            selected=[case for case in cases if case.get('classname') in artifact.get('test_classes',[])]
            if not selected or any(case.find(tag) is not None for case in selected for tag in ['failure','error','skipped']): return False
        except (OSError,ET.ParseError): return False
    return True


def expected_rows_match(actual,expected,tolerance=Decimal('0')):
    """Compare reviewed fields; diagnostic metadata is not part of a measurement."""
    if len(actual)!=len(expected): return False
    for observed,reviewed in zip(actual,expected):
        for key,value in reviewed.items():
            if key not in observed: return False
            if key == 'value' and value is not None:
                try:
                    if abs(Decimal(str(observed[key]))-Decimal(str(value))) > tolerance: return False
                except (InvalidOperation,TypeError): return False
            elif observed[key]!=value: return False
    return True


def check_record(scenario,record):
    failures=[]
    try:
        answer=Answer.model_validate(record["answer"])
        scope=Scope.model_validate(record["scope"])
        sources=record.get("sources",{})
        expected=scenario["expected"]
        if answer.status!=expected["status"]:
            failures.append("status")
        if answer.plan and answer.plan.language!=record["language"]:
            failures.append("language")
        for result in answer.results:
            if result.scope_fingerprint!=scope.fingerprint():
                failures.append("authorization")
            for citation in result.citations:
                source=sources.get(str(citation.document_id))
                if source is None or source[citation.start:citation.end]!=citation.text:
                    failures.append("provenance")
            if result.operation.operation in {"sum","average","count_entities","count_assertions"}:
                if result.status=="answered" and not result.coverage.complete:
                    failures.append("unsupported_count")
        for claim in answer.claims:
            validate_claim(claim,answer.results,scope)
        for measure in expected.get("measures",[]):
            matches=[r for r in answer.results if r.operation.operation==measure["operation"]
                and str(r.operation.predicate_id or r.operation.subject_type)==measure["concept_id"]]
            if (len(matches)!=1 or not expected_rows_match(matches[0].rows,measure['rows'],Decimal(str(measure.get('tolerance',0))))
                    or not any(claim.result_id==matches[0].result_id for claim in answer.claims)):
                failures.append("measure_population_or_value")
            elif measure.get('metric'):
                actual=matches[0].metric.model_dump(mode='json') if matches[0].metric else {}
                if any(actual.get(key)!=value for key,value in measure['metric'].items()): failures.append('metric_denominator')
        # Semantic correctness must be independently judged against original
        # sources; a matching number or a keyword is not sufficient.
        review=record.get("review",{})
        if not review.get("reviewer") or review.get("correct") is not True:
            failures.append("correctness_review")
        if review.get("unauthorized") or review.get("unsupported_claim"):
            failures.append("authorization" if review.get("unauthorized") else "unsupported_claim")
        if not scenario.get("reviewer") or not scenario.get("source_evidence"):
            failures.append("unreviewed_expectation")
        for source in scenario.get("source_evidence",[]):
            if hashlib.sha256(source["text"].encode("utf-8")).hexdigest()!=source["sha256"]:
                failures.append("expectation_source_hash")
    except Exception as exc:
        failures.append("invalid_record:"+type(exc).__name__)
    return sorted(set(failures))


def evaluate(manifest,records):
    scenarios={s["id"]:s for s in manifest["scenarios"]}
    indexed={};failures=[]
    for record in records:
        key=(record["scenario_id"],record["language"],record["attempt"])
        if key in indexed:
            failures.append({"key":key,"failures":["duplicate_run"]})
        indexed[key]=record
    scores={language:Counter() for language in LANGUAGES}
    gates=[]
    if len(scenarios)<100:gates.append("At least 100 distinct reviewed scenarios are required")
    if len(scenarios)!=len(manifest["scenarios"]):gates.append("Scenario identities must be unique")
    if not deterministic_artifacts_pass(manifest):
        gates.append("Deterministic authorization and provenance gates must pass")
    for scenario in scenarios.values():
        for language in LANGUAGES:
            for attempt in (1,2,3):
                key=(scenario["id"],language,attempt)
                record=indexed.get(key)
                errors=check_record(scenario,record) if record else ["missing_run"]
                if language not in scenario.get("questions",{}):errors.append("missing_question_variant")
                if errors:failures.append({"key":key,"failures":errors})
                if scenario.get("core",True) and scenario.get("answerable",True):
                    scores[language]["total"]+=1
                    scores[language]["passed"]+=int(not errors and first_attempt(record))
                elif errors:
                    gates.append("Advanced, ambiguity and failure scenarios must all pass")
    ratios={language:(c["passed"]/c["total"] if c["total"] else 0) for language,c in scores.items()}
    if any(value<.95 for value in ratios.values()):gates.append("First-attempt correctness is below 95% in at least one language")
    for language in LANGUAGES:
        core=[s for s in scenarios.values() if s.get("core",True) and s.get("answerable",True)]
        for attempt in (1,2,3):
            successful=0
            for scenario in core:
                record=indexed.get((scenario["id"],language,attempt))
                if record and not check_record(scenario,record) and first_attempt(record):
                    successful+=1
            if not core or successful/len(core)<.95:
                gates.append(f"{language} run {attempt} is below 95% first-attempt correctness")
    fatal={"authorization","provenance","unsupported_count","unsupported_claim","unreviewed_expectation","expectation_source_hash","missing_run","duplicate_run"}
    if any(fatal.intersection(f["failures"]) for f in failures):gates.append("Evidence, review or run-completeness gate failed")
    return {"release_ready":not gates,"scenario_count":len(scenarios),"required_runs":len(scenarios)*9,
            "recorded_runs":len(indexed),"first_attempt_correctness":ratios,
            "blocked_by":sorted(set(gates)),"failures":failures}


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument("manifest",type=Path)
    parser.add_argument("results",type=Path,help="JSONL containing full run records and independent reviews")
    parser.add_argument("--report",type=Path,default=Path("data/evidence_acceptance/report.json"))
    args=parser.parse_args()
    report=evaluate(json.loads(args.manifest.read_text(encoding="utf-8")),
        [json.loads(line) for line in args.results.read_text(encoding="utf-8").splitlines() if line.strip()])
    args.report.parent.mkdir(parents=True,exist_ok=True)
    args.report.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    print(json.dumps({k:v for k,v in report.items() if k!="failures"}))
    return 0 if report["release_ready"] else 1


if __name__=="__main__":raise SystemExit(main())
