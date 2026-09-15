# -*- coding: utf-8 -*-
"""
Phase 4B — the deterministic structured route, behind the common contract.

DELIBERATELY THIN. This module wraps; it does not reimplement. Every
guarantee the structured route offers — grain validation, fanout
protection, tombstone and supersession exclusion, field authority,
result-shape checks, unsupported-field refusal, time-window resolution,
coverage, deterministic receipts — lives in `validator.py`, `compiler.py`,
`temporal.py`, `shapes.py`, `coverage.py` and `executor.py` and is reached
by calling `executor.execute()` unchanged. If this file grew logic of its
own, that logic would be a second place for those guarantees to drift.

WHAT IT ADDS. Exactly one thing: a translation from `AggregateOutcome` into
`RouteResult`, so the structured route can sit alongside two others that
know nothing about `AggregateReceipt`. The receipt is preserved verbatim in
`native`, so nothing is lost in the narrowing.

WHAT IT REFUSES TO ADD. No trust marker. The brief is explicit that the
deterministic route is not automatically ground truth, and the contract has
no field in which to claim otherwise. It is a constrained, independent
computation route — its authority in the evaluation comes from agreeing
with independently-established figures, not from being this one.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate import routes
from src.pipeline.aggregate.executor import execute
from src.pipeline.aggregate.receipt import describe_population
from src.pipeline.aggregate.routes import (
    ROUTE_STRUCTURED,
    AggregateRouteRequest,
    NumericResult,
    RouteResult,
)
from src.pipeline.aggregate.spec import AggregateSpec

logger = logging.getLogger(__name__)


def _interpretation(spec: AggregateSpec) -> str:
    """How the number should be read.

    A ratio is a percentage, not a count, and conflating the two would let
    reconciliation compare 5.48 against 4 as though they were rival answers
    to one question.
    """
    if spec.ratio is not None:
        return "percentage" if spec.ratio.as_percentage else "ratio"
    return spec.measure


async def run(
    snapshot: reg.RegistrySnapshot,
    request: AggregateRouteRequest,
    *,
    spec: Optional[AggregateSpec] = None,
) -> RouteResult:
    """Run the deterministic route and normalise its outcome.

    `spec` is supplied by the caller (the harness). Phase 4 does not
    generate specs from natural language — that is a later phase, and
    proving three routes can coexist must not be entangled with proving an
    LLM can write a spec.
    """
    target = spec if spec is not None else request.spec
    if target is None:
        return routes.refusal(
            ROUTE_STRUCTURED,
            "no_spec_supplied",
            "The structured route requires an AggregateSpec. Phase 4 does "
            "not generate one from natural language; the harness supplies it.",
            status=routes.UNSUPPORTED,
        )

    started = time.perf_counter()
    outcome = await execute(snapshot, target)
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    receipt = outcome.receipt
    meta = {
        "elapsed_ms": round(elapsed_ms, 1),
        "spec_hash": receipt.spec_hash,
        "registry_as_of": receipt.registry_as_of,
        "queries": [
            {"backend": q.backend, "text": q.text, "params": q.params,
             "rows": q.row_count, "ms": q.duration_ms}
            for q in receipt.queries
        ],
    }
    provenance = {
        "engine": "deterministic AggregateSpec compiler",
        "population_definition": receipt.population_definition,
        "grain": receipt.grain,
        "distinct_key": receipt.distinct_key,
        "injected_predicates": list(receipt.injected_predicates),
        "filters_applied": list(receipt.filters_applied),
        "excluded_merged_n": receipt.excluded_merged_n,
        "excluded_superseded_n": receipt.excluded_superseded_n,
        "calculation_steps": list(receipt.calculation_steps),
    }

    if outcome.status == "refused":
        return RouteResult(
            route=ROUTE_STRUCTURED,
            status=routes.REFUSED,
            refusal_code=receipt.refusal_code,
            refusal_reason=receipt.refusal_reason,
            coverage=receipt.coverage,
            provenance=provenance,
            execution_metadata=meta,
            native=receipt,
        )
    if outcome.status == "failed":
        return RouteResult(
            route=ROUTE_STRUCTURED,
            status=routes.EXECUTION_ERROR,
            refusal_code=receipt.refusal_code,
            refusal_reason=receipt.refusal_reason,
            coverage=receipt.coverage,
            provenance=provenance,
            execution_metadata=meta,
            native=receipt,
        )

    # Grouped results keep their rows; scalars and ratios become a
    # NumericResult carrying the grain that makes them comparable.
    if isinstance(outcome.value, list):
        return RouteResult(
            route=ROUTE_STRUCTURED,
            status=routes.SUCCESS,
            result_shape="grouped",
            result=outcome.value,
            coverage=receipt.coverage,
            provenance=provenance,
            warnings=(receipt.caveat,) if receipt.caveat else (),
            execution_metadata=meta,
            native=receipt,
        )

    numeric = NumericResult(
        value=outcome.value,
        interpretation=_interpretation(target),
        grain=target.grain,
        population=describe_population(target),
        grouping=tuple(gb.field for gb in target.group_by),
        unit="percent" if target.ratio and target.ratio.as_percentage else None,
    )
    return RouteResult(
        route=ROUTE_STRUCTURED,
        status=routes.SUCCESS,
        result_shape="ratio" if target.ratio is not None else "scalar",
        result=numeric,
        coverage=receipt.coverage,
        provenance=provenance,
        warnings=(receipt.caveat,) if receipt.caveat else (),
        execution_metadata=meta,
        native=receipt,
    )
