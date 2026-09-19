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

from src.pipeline.aggregate import gate_evaluator as gateval
from src.pipeline.aggregate import gates as gatelib
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


#: Stable code for the approved insufficient-coverage disclosure (§15).
#: A code rather than prose so rendering stays at the presentation
#: boundary, matching how `receipt.caveat` is already handled.
INSUFFICIENT_COVERAGE_CODE = "INSUFFICIENT_DATA_COVERAGE"
INSUFFICIENT_COVERAGE_TEXT = (
    "This result is based on incomplete data coverage and may not represent "
    "the full relevant population."
)


def _coverage_warnings(receipt) -> tuple[str, ...]:
    """Warnings a served result must carry, coverage included.

    The approved policy serves INSUFFICIENT results rather than blocking
    them, so the disclosure is what makes that safe. `receipt.caveat`
    already carries the measured detail ("N of M records carry no ...");
    the explicit code is added alongside it so a consumer can branch on
    the condition without parsing prose.
    """
    out: list[str] = []
    if getattr(receipt, "coverage_verdict", None) == "INSUFFICIENT":
        out.append(f"{INSUFFICIENT_COVERAGE_CODE}: {INSUFFICIENT_COVERAGE_TEXT}")
    if getattr(receipt, "caveat", None):
        out.append(receipt.caveat)
    return tuple(out)


def _selection_provenance(selection: gatelib.SelectionOutcome) -> dict:
    """The auditable record of how the effective profile was chosen."""
    return {
        "selected_profile_id": (
            selection.selected_profile.value
            if selection.selected_profile else None
        ),
        "required_profile_id": selection.required_profile.value,
        "effective_profile_id": selection.effective_profile.value,
        "profile_escalated": selection.escalated,
        # A mislabelled selection replaced by the profile derived from the
        # typed spec. Recorded separately from escalation: one raises the
        # bar, the other only discards a wrong label.
        "profile_corrected": selection.corrected,
        "issue_code": selection.issue.code if selection.issue else None,
        "issue_message": selection.issue.message if selection.issue else None,
    }


def _interpretation(spec: AggregateSpec) -> str:
    """How the number should be read.

    A ratio is a percentage, not a count, and conflating the two would let
    reconciliation compare 5.48 against 4 as though they were rival answers
    to one question.
    """
    if spec.ratio is not None:
        return "percentage" if spec.ratio.as_percentage else "ratio"
    return spec.measure


def _composite_plan_for(
    snapshot: reg.RegistrySnapshot,
    spec: AggregateSpec,
    reqs: gatelib.QueryRequirements,
):
    """A composite plan, when — and only when — one source cannot answer.

    ROUTING IS A TRUSTED DECISION, NOT A MODEL PREFERENCE. The question is
    purely mechanical: does every constraint resolve to ONE authoritative
    source? A date restriction on `incident_date` is Postgres-authoritative
    while the population lives in the graph, so such a question genuinely
    needs both and gets composite execution. Everything else keeps the
    existing trusted direct path, which is better tested and cheaper.

    Returns None when the existing route should handle it — including for
    shapes the composite executor deliberately does not implement (ratios,
    groupings and value measures), so an unsupported shape falls back to
    the route that DOES support it rather than failing.
    """
    from src.pipeline.aggregate import authority as auth
    from src.pipeline.aggregate.composite import (
        CompositeAggregatePlan, Constraint, Metric,
    )

    if not reqs.requires_multiple_sources:
        return None
    if spec.ratio is not None or spec.group_by or spec.compare is not None:
        return None
    if spec.measure not in ("count", "count_distinct"):
        return None
    if spec.time_window is None:
        return None

    entity = spec.population.entity
    info = snapshot.entity(entity)
    subject_key = spec.distinct_key or (info.distinct_key if info else None)
    if not subject_key:
        return None

    constraints: list[Constraint] = []

    # The population's own traversals become a relationship constraint, so
    # the subject is restricted to nodes that actually take the path.
    if spec.population.traversals:
        path = [
            {"rel": t.rel, "target": t.target, "direction": t.direction}
            for t in spec.population.traversals
        ]
        constraints.append(Constraint(
            name="population_path",
            field=spec.population.traversals[0].rel,
            kind=auth.ConstraintKind.RELATIONSHIP,
            key=subject_key,
            spec={"label": entity, "id_property": subject_key, "path": path},
        ))

    for i, pred in enumerate(spec.population.predicates):
        field = getattr(pred, "field", None)
        if field is None:  # RelationCountPredicate — not composite-expressible
            return None
        prop = field.split(".")[-1]
        constraints.append(Constraint(
            name=f"filter_{i}_{prop}",
            field=field,
            kind=auth.ConstraintKind.PROPERTY,
            key=subject_key,
            spec={
                "label": entity, "property": prop,
                "id_property": subject_key,
                "op": pred.op, "value": pred.value,
            },
        ))

    constraints.append(Constraint(
        name=f"window_{spec.time_window.field}",
        field=spec.time_window.field,
        kind=auth.ConstraintKind.TEMPORAL,
        key="case_id",
        spec={"start": spec.time_window.start, "end": spec.time_window.end},
    ))

    return CompositeAggregatePlan(
        question_text=spec.question_text,
        subject_label=entity,
        subject_key=subject_key,
        grain=spec.grain,
        metric=(
            Metric.COUNT_DISTINCT if spec.measure == "count_distinct"
            else Metric.COUNT
        ),
        constraints=tuple(constraints),
    )


async def run(
    snapshot: reg.RegistrySnapshot,
    request: AggregateRouteRequest,
    *,
    spec: Optional[AggregateSpec] = None,
    gate_profile_id: Optional[str] = None,
) -> RouteResult:
    """Run the deterministic route and normalise its outcome.

    `spec` is supplied by the caller (the harness). Phase 4 does not
    generate specs from natural language — that is a later phase, and
    proving three routes can coexist must not be entangled with proving an
    LLM can write a spec.

    `gate_profile_id` is an OPTIONAL model selection from the approved
    catalogue. It can only ever make validation stricter: trusted code
    derives the minimum required profile from the typed spec first, and a
    weaker selection is escalated rather than honoured.
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

    # ── Gate profile: derived from the TYPED spec, then reconciled with
    # whatever the model selected. Derivation happens first so a model
    # choice can never reduce what is enforced.
    reqs = gatelib.requirements_from_spec(target, snapshot)
    selection = gatelib.validate_selection(gate_profile_id, reqs)
    if not selection.accepted:
        return routes.refusal(
            ROUTE_STRUCTURED,
            selection.issue.code if selection.issue else "invalid_gate_profile",
            selection.issue.message if selection.issue else "profile rejected",
            provenance={"gate_profile": _selection_provenance(selection)},
        )

    started = time.perf_counter()

    # ── Multi-path composition, when one source cannot answer alone ────
    composite_result = None
    plan = _composite_plan_for(snapshot, target, reqs)
    if plan is not None:
        from src.pipeline.aggregate.composite_executor import execute_composite

        composite_result = await execute_composite(snapshot, plan)
        if not composite_result.ok:
            # NO SILENT FALLBACK. A composite failure must not re-run the
            # question as a broader single-source query — that is exactly
            # how a dropped date restriction turned 51 into 73.
            evaluation = gateval.evaluate_gates(
                selection.effective_profile,
                composite=composite_result, requirements=reqs,
                escalated_from=selection.selected_profile if selection.escalated else None,
            )
            # `routes.refusal()` takes no execution_metadata — timing rides
            # in provenance instead, so the refusal still records how long
            # the composite attempt took.
            return routes.refusal(
                ROUTE_STRUCTURED,
                composite_result.refusal_code or "composite_refused",
                composite_result.refusal_reason or "composite execution refused",
                provenance={
                    "engine": "multi-path composite",
                    "gate_profile": _selection_provenance(selection),
                    "gate_evaluation": evaluation.to_dict(),
                    "composite": composite_result.provenance(),
                    "elapsed_ms": round(
                        (time.perf_counter() - started) * 1000.0, 1
                    ),
                },
            )

        evaluation = gateval.evaluate_gates(
            selection.effective_profile,
            composite=composite_result, requirements=reqs,
            escalated_from=selection.selected_profile if selection.escalated else None,
        )
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        provenance = {
            "engine": "multi-path composite",
            "gate_profile": _selection_provenance(selection),
            "gate_evaluation": evaluation.to_dict(),
            "composite": composite_result.provenance(),
            "population_definition": describe_population(target),
            "grain": target.grain,
            "distinct_key": plan.subject_key,
        }
        meta = {
            "elapsed_ms": round(elapsed_ms, 1),
            "spec_hash": target.spec_hash(),
            "registry_as_of": snapshot.as_of,
        }

        if not evaluation.overall_passed:
            # A failed required gate BLOCKS the result. It is never
            # downgraded to a caveat or offset by confidence.
            return routes.refusal(
                ROUTE_STRUCTURED, "gate_failed",
                f"Required gate(s) failed: {evaluation.summary()}",
                provenance={**provenance, **meta},
            )

        numeric = NumericResult(
            value=composite_result.value,
            interpretation=target.measure,
            grain=target.grain,
            population=describe_population(target),
        )
        return RouteResult(
            route=ROUTE_STRUCTURED, status=routes.SUCCESS,
            result_shape="scalar", result=numeric,
            provenance=provenance, execution_metadata=meta,
            native=composite_result,
        )

    # ── Single authoritative source: the existing trusted path ─────────
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
    direct_evaluation = gateval.evaluate_gates(
        selection.effective_profile,
        receipt=receipt, requirements=reqs,
        escalated_from=selection.selected_profile if selection.escalated else None,
    )
    provenance = {
        "engine": "deterministic AggregateSpec compiler",
        "gate_profile": _selection_provenance(selection),
        "gate_evaluation": direct_evaluation.to_dict(),
        "coverage_verdict": receipt.coverage_verdict,
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

    # ── D2: required gates BLOCK on the direct path too ───────────────
    #
    # Phase 7C measured five direct-path cases that failed a required gate
    # and were served anyway, because this check did not exist — the
    # composite path blocked correctly while the direct path only recorded
    # its verdict. A gate report nobody enforces is worse than no gate
    # report: it teaches readers that "failed" means nothing.
    #
    # Coverage is deliberately NOT among the required gates (see
    # `gates._UNIVERSAL`), so an INSUFFICIENT-coverage result still serves
    # here — carrying its warning rather than being blocked.
    if not direct_evaluation.overall_passed:
        return routes.refusal(
            ROUTE_STRUCTURED, "gate_failed",
            f"Required gate(s) failed: {direct_evaluation.summary()}",
            provenance={**provenance, **meta},
            # The receipt (and its value) stay available for debugging;
            # they are simply not served as an accepted answer.
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
            warnings=_coverage_warnings(receipt),
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
        warnings=_coverage_warnings(receipt),
        execution_metadata=meta,
        native=receipt,
    )
