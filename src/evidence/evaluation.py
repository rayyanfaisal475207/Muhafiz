"""Mechanical evidence checks; intentionally does not assign semantic correctness."""
from .answer import validate_claim


def inspect_answer(answer, scope):
    failures=[]
    measurements=[]
    for index,claim in enumerate(answer.claims):
        try:
            value=validate_claim(claim,answer.results,scope)
        except ValueError as exc:
            failures.append({"claim_index":index,"reason":str(exc)})
            continue
        result=next(r for r in answer.results if r.result_id==claim.result_id)
        if not claim.span_id and result.operation.operation in {
                "count_entities","count_assertions","sum","average","average_duration","difference","ratio"}:
            measurements.append({"output_id":result.output_id,"value":value,
                                 "unit":result.unit,"coverage":result.coverage.model_dump(),
                                 "population_basis":claim.population_basis,
                                 "metric":result.metric.model_dump(mode="json") if result.metric else None})
    return {"claim_failures":failures,"measurements":measurements,
            "correctness":"unreviewed"}
