# -*- coding: utf-8 -*-
"""
Phase 1 — the executor: run a validated spec and account for the result.

WHERE THIS SITS. Everything before it is pure (registry lookup, validation,
compilation); everything after it is presentation. This module is the only
place in the package that touches the database, which keeps the safety
argument simple: the query it runs came from `compiler.py`, the values it
binds came from `_ParamBag`, and nothing else reaches `execute_cypher`.

THE ORDER OF OPERATIONS IS THE SAFETY MODEL, and it is deliberate:

    validate -> compile -> execute -> SHAPE-CHECK -> coverage -> receipt

The shape check happens BEFORE the number is allowed to become an answer,
because the failure it catches produces a well-formed, plausible integer.
`{'gkey': None, 'value': 73}` is not an error state that surfaces on its
own; it is a correct-looking answer to a question nobody asked. So a
refusal here is a normal outcome, not an exception path.

NOTHING IS REPAIRED. A shape failure produces a refusal receipt carrying
the reason, never a corrected number. The brief is explicit about this and
it is also the only defensible behaviour: repairing means guessing what the
user meant after the query already failed to express it.

ROLE GATE AND RLS. `run_aggregate()` in the legacy engine checks the caller
role, writes an audit event, then arms `current_rls_active` /
`current_cross_case`. This module performs the same check in the same order
for the same reason — the bypass must never be armed for a caller who has
not passed the gate. It is a deliberate duplication rather than a shared
helper, because the legacy path must stay byte-identical during shadow
mode; the two converge when the old engine is retired.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any, Optional

from src.database.postgres import current_cross_case, current_rls_active
from src.graph import age_client
from src.pipeline.aggregate import coverage as cov
from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate import shapes
from src.pipeline.aggregate.compiler import CompiledQuery, compile_ratio, compile_spec
from src.pipeline.aggregate.receipt import (
    AggregateReceipt,
    CrossCheck,
    ExecutedQuery,
    describe_population,
)
from src.pipeline.aggregate.spec import AggregateSpec
from src.pipeline.aggregate.validator import ValidationResult, validate

logger = logging.getLogger(__name__)

#: Roles permitted to run cross-case aggregates. Mirrors the tuple in
#: `xagg.run_aggregate()` exactly — divergence here would be a security
#: bug, so the values are duplicated verbatim rather than derived.
_CROSS_CASE_ROLES = ("supervisor", "station-admin", "platform-admin")

#: Hard ceiling on rows returned by one grouped query. A grouped aggregate
#: over this corpus yields tens of rows (19 stations, 9 districts); a
#: result in the thousands means an unintended join, which the shape layer
#: then refuses. The cap stops a runaway query from being materialised at
#: all.
MAX_ROWS = 10_000


@dataclasses.dataclass(frozen=True)
class AggregateOutcome:
    """The result of one aggregate computation, answer or refusal.

    `value` is None whenever `status != "ok"`. Callers must branch on
    `status` rather than on `value`, because a legitimate zero is a real
    answer and must not be confused with a refusal.
    """

    status: str  # "ok" | "refused" | "failed"
    value: Any
    receipt: AggregateReceipt
    rows: tuple[dict, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status == "ok"


async def _run(query: CompiledQuery) -> tuple[list[dict], float]:
    """Execute one compiled query, returning rows and elapsed milliseconds."""
    started = time.perf_counter()
    rows = await age_client.execute_cypher(
        query.text, params=query.params, columns=list(query.columns)
    )
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return rows, elapsed_ms


def _arm_scope(spec: AggregateSpec) -> None:
    """Arm the RLS cross-case bypass — only after the role gate has passed.

    Same self-arming rationale `run_aggregate()` documents: relying on the
    caller to have armed RLS upstream is a convention enforced only by
    docstring, and migration 010's policies treat an unset `app.rls_active`
    as "RLS fully inactive" (fail-open).
    """
    current_rls_active.set(True)
    current_cross_case.set(True)


def _role_denied(spec: AggregateSpec) -> Optional[str]:
    if spec.scope.kind != "cross_case":
        return None
    if spec.scope.user_role not in _CROSS_CASE_ROLES:
        return (
            f"Cross-case aggregates require supervisor role or higher; caller "
            f"role is {spec.scope.user_role!r}."
        )
    return None


def _refusal(
    spec: AggregateSpec,
    snapshot: reg.RegistrySnapshot,
    code: str,
    reason: str,
    *,
    queries: tuple[ExecutedQuery, ...] = (),
    notes: tuple[str, ...] = (),
) -> AggregateOutcome:
    receipt = AggregateReceipt(
        question_text=spec.question_text,
        spec_hash=spec.spec_hash(),
        spec=spec.to_dict(),
        grain=spec.grain,
        distinct_key=spec.distinct_key,
        population_definition=describe_population(spec),
        measure=spec.measure,
        value=None,
        queries=queries,
        compiler_notes=notes,
        registry_as_of=snapshot.as_of,
        execution_status="refused",
        refusal_code=code,
        refusal_reason=reason,
    )
    return AggregateOutcome(status="refused", value=None, receipt=receipt)


async def execute(
    snapshot: reg.RegistrySnapshot,
    spec: AggregateSpec,
    *,
    validation: Optional[ValidationResult] = None,
) -> AggregateOutcome:
    """Validate, compile, execute, check and account for one aggregate.

    Never raises for a data or specification problem — those become
    refusals with a reason, because a refusal is an answer the caller can
    act on while an exception is not. Genuine infrastructure failures
    (database unreachable) surface as `status="failed"`.
    """
    result = validation or validate(snapshot, spec)
    if not result.ok:
        first = result.issues[0] if result.issues else None
        return _refusal(
            spec, snapshot,
            code=first.code if first else "validation_failed",
            reason="; ".join(i.message for i in result.issues),
        )

    denied = _role_denied(spec)
    if denied:
        return _refusal(spec, snapshot, code="scope_denied", reason=denied)

    _arm_scope(spec)

    if spec.ratio is not None:
        return await _execute_ratio(snapshot, spec)
    return await _execute_simple(snapshot, spec)


async def _execute_simple(
    snapshot: reg.RegistrySnapshot, spec: AggregateSpec
) -> AggregateOutcome:
    try:
        query = compile_spec(snapshot, spec)
    except Exception as exc:  # noqa: BLE001 - compile failure is a refusal
        return _refusal(spec, snapshot, code="compile_failed", reason=str(exc))

    try:
        rows, elapsed = await _run(query)
    except Exception as exc:  # noqa: BLE001
        logger.error("aggregate executor: query failed: %s", exc)
        executed = ExecutedQuery(
            backend=query.backend, text=query.text, params=query.params,
            row_count=0, status="failed", error=str(exc),
        )
        outcome = _refusal(
            spec, snapshot, code="execution_failed", reason=str(exc),
            queries=(executed,), notes=query.notes,
        )
        return dataclasses.replace(outcome, status="failed")

    executed = ExecutedQuery(
        backend=query.backend, text=query.text, params=query.params,
        row_count=len(rows), duration_ms=elapsed,
    )

    if len(rows) > MAX_ROWS:
        return _refusal(
            spec, snapshot, code="result_too_large",
            reason=f"{len(rows)} rows exceeds the {MAX_ROWS}-row ceiling.",
            queries=(executed,), notes=query.notes,
        )

    # ── Shape checks, BEFORE the number becomes an answer ──────────────
    if spec.group_by:
        shape = shapes.validate_grouped_rows(snapshot, spec, rows)
        checked = ("all_group_keys_null", "duplicate_group_keys", "grouping_collapsed")
    else:
        value = rows[0].get("value") if rows else None
        shape = shapes.validate_scalar(value, measure=spec.measure)
        checked = ("null_result", "negative_count")

    if not shape.ok:
        return _refusal(
            spec, snapshot,
            code=shape.issues[0].code if shape.issues else "shape_invalid",
            reason="; ".join(i.message for i in shape.issues),
            queries=(executed,), notes=query.notes,
        )

    if spec.group_by:
        value: Any = [{"key": r.get("gkey"), "count": r.get("value")} for r in rows]
        observed_n = sum(int(r.get("value") or 0) for r in rows)
    else:
        value = rows[0].get("value") if rows else 0
        observed_n = int(value or 0)

    terminal = (
        spec.population.traversals[-1].target
        if spec.population.traversals
        else spec.population.entity
    )
    field_for_coverage = (
        spec.group_by[0].field if spec.group_by else spec.value_field
    )
    coverage = cov.build_coverage(
        snapshot,
        entity_label=terminal,
        observed_n=observed_n,
        field_name=field_for_coverage,
        field_entity=terminal,
        excluded_merged_n=_tombstones_excluded(snapshot, spec),
        source="graph",
    )

    receipt = AggregateReceipt(
        question_text=spec.question_text,
        spec_hash=spec.spec_hash(),
        spec=spec.to_dict(),
        grain=spec.grain,
        distinct_key=spec.distinct_key,
        population_definition=describe_population(spec),
        measure=spec.measure,
        value=value,
        group_keys=tuple(str(r.get("gkey")) for r in rows) if spec.group_by else (),
        injected_predicates=query.injected_predicates,
        excluded_merged_n=_tombstones_excluded(snapshot, spec),
        canonicalization_policy=spec.canonicalization,
        queries=(executed,),
        invariants_checked=checked,
        coverage=coverage,
        coverage_verdict=coverage.verdict,
        caveat=coverage.caveat_text(),
        compiler_notes=query.notes,
        calculation_steps=(f"{spec.measure} over {describe_population(spec)}",),
        source="graph",
        registry_as_of=snapshot.as_of,
        execution_status="ok",
    )
    return AggregateOutcome(status="ok", value=value, receipt=receipt, rows=tuple(rows))


async def _execute_ratio(
    snapshot: reg.RegistrySnapshot, spec: AggregateSpec
) -> AggregateOutcome:
    """Run a ratio as two independent queries, then check invariants.

    The two halves are computed separately on purpose — see
    `compiler.compile_ratio()`. Independence is what lets
    `numerator <= denominator` be a real check rather than a tautology, and
    what lets a zero denominator be refused before any division happens.
    """
    try:
        num_q, den_q = compile_ratio(snapshot, spec)
    except Exception as exc:  # noqa: BLE001
        return _refusal(spec, snapshot, code="compile_failed", reason=str(exc))

    executed: list[ExecutedQuery] = []
    try:
        num_rows, num_ms = await _run(num_q)
        executed.append(ExecutedQuery(
            backend=num_q.backend, text=num_q.text, params=num_q.params,
            row_count=len(num_rows), duration_ms=num_ms,
        ))
        den_rows, den_ms = await _run(den_q)
        executed.append(ExecutedQuery(
            backend=den_q.backend, text=den_q.text, params=den_q.params,
            row_count=len(den_rows), duration_ms=den_ms,
        ))
    except Exception as exc:  # noqa: BLE001
        outcome = _refusal(
            spec, snapshot, code="execution_failed", reason=str(exc),
            queries=tuple(executed),
        )
        return dataclasses.replace(outcome, status="failed")

    numerator = num_rows[0].get("value") if num_rows else None
    denominator = den_rows[0].get("value") if den_rows else None

    shape = shapes.validate_ratio(
        numerator, denominator,
        as_percentage=spec.ratio.as_percentage if spec.ratio else True,
    )
    if not shape.ok:
        return _refusal(
            spec, snapshot,
            code=shape.issues[0].code if shape.issues else "ratio_invalid",
            reason="; ".join(i.message for i in shape.issues),
            queries=tuple(executed),
            notes=num_q.notes + den_q.notes,
        )

    pct = 100.0 * float(numerator) / float(denominator)
    value = pct if (spec.ratio and spec.ratio.as_percentage) else (
        float(numerator) / float(denominator)
    )

    coverage = cov.build_coverage(
        snapshot,
        entity_label=spec.population.entity,
        observed_n=int(denominator or 0),
        effective_denominator_n=int(denominator or 0),
        effective_denominator_definition=describe_population(
            dataclasses.replace(spec, population=spec.ratio.denominator)
        ) if spec.ratio else None,
        excluded_merged_n=_tombstones_excluded(snapshot, spec),
        source="graph",
    )

    receipt = AggregateReceipt(
        question_text=spec.question_text,
        spec_hash=spec.spec_hash(),
        spec=spec.to_dict(),
        grain=spec.grain,
        distinct_key=spec.distinct_key,
        population_definition=describe_population(spec),
        measure=spec.measure,
        value=value,
        numerator_n=float(numerator),
        denominator_n=float(denominator),
        injected_predicates=tuple(
            dict.fromkeys(num_q.injected_predicates + den_q.injected_predicates)
        ),
        excluded_merged_n=_tombstones_excluded(snapshot, spec),
        canonicalization_policy=spec.canonicalization,
        queries=tuple(executed),
        invariants_checked=(
            "numerator_le_denominator", "percentage_in_range",
            "denominator_nonzero",
        ),
        coverage=coverage,
        coverage_verdict=coverage.verdict,
        caveat=coverage.caveat_text(),
        compiler_notes=num_q.notes + den_q.notes,
        calculation_steps=(
            f"numerator = {numerator} ({describe_population(dataclasses.replace(spec, population=spec.ratio.numerator))})",
            f"denominator = {denominator}",
            f"{numerator} / {denominator} = {value:.4f}",
        ),
        source="graph",
        registry_as_of=snapshot.as_of,
        execution_status="ok",
    )
    return AggregateOutcome(status="ok", value=value, receipt=receipt)


def _tombstones_excluded(
    snapshot: reg.RegistrySnapshot, spec: AggregateSpec
) -> int:
    """How many tombstoned nodes the injected predicate removed from scope.

    Reported rather than inferred: a reader of the receipt should be able
    to see that 222 Person donors were excluded without re-deriving it.
    """
    total = 0
    labels = [spec.population.entity] + [t.target for t in spec.population.traversals]
    for label in labels:
        info = snapshot.entity(label)
        if info is not None and info.has_tombstones:
            total += info.tombstoned_n
    return total


async def execute_both_canonicalizations(
    snapshot: reg.RegistrySnapshot, spec: AggregateSpec
) -> tuple[AggregateOutcome, Optional[CrossCheck]]:
    """Run a spec and, under BOTH_AND_COMPARE, report the canonical delta.

    The 222 confirmed Person SAME_AS edges form components of 139 and 85 at
    tier `flagged_unverified`, and their provenance is an open supervisor
    question. So the policy does not pick: it computes the raw figure, then
    reports what canonicalization WOULD change, and leaves the difference
    visible in the receipt as a cross-check rather than silently applying a
    collapse nobody has ratified.
    """
    outcome = await execute(snapshot, spec)
    if not outcome.ok or spec.canonicalization != "BOTH_AND_COMPARE":
        return outcome, None

    from src.graph.community_detection import build_canonical_map, fetch_confirmed_same_as

    try:
        pairs = await fetch_confirmed_same_as()
    except Exception as exc:  # noqa: BLE001 - comparison is advisory
        logger.warning("canonicalization comparison unavailable: %s", exc)
        return outcome, None

    canonical_map = build_canonical_map(pairs)
    collapsed = len(canonical_map) - len(set(canonical_map.values())) if canonical_map else 0
    check = CrossCheck(
        route="canonicalization(confirmed SAME_AS)",
        value=collapsed,
        agrees=(collapsed == 0),
        note=(
            f"{len(pairs)} confirmed SAME_AS pair(s) would collapse {collapsed} "
            f"node(s); policy BOTH_AND_COMPARE leaves the raw figure standing "
            f"because the components' provenance is unresolved."
        ),
    )
    return outcome, check
