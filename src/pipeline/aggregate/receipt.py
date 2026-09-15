# -*- coding: utf-8 -*-
"""
Phase 7 — AggregateReceipt: the answer to "what exactly did you count?"

WHY A RECEIPT RATHER THAN BETTER LOGGING. A log line explains a number to
whoever is reading the log at the time. A receipt travels WITH the number,
so the question can be asked months later by someone who was not there —
which for a police deployment is the difference between an auditable
finding and an assertion. It is also what makes shadow comparison
diagnosable: when the new engine and `xagg.py` disagree, the receipt says
which population, which grain and which exclusions produced each figure,
so the disagreement can be resolved instead of merely counted.

WHAT IT REPLACES, EVENTUALLY. `EXHAUSTIVE_SCOPE_META_KEY` currently marks a
chunk as a complete enumeration on a heuristic — `meta_analysis.py` sets it
when `tools_used == {"XAGG"}`. That heuristic is doing real work (it is
what licenses the Verifier to treat "record X is absent from this listing"
as supported rather than invented), but it infers exhaustiveness from which
tool ran. A receipt can PROVE it: a population with no filters, no sparse
fields and an exhaustive source genuinely enumerated its scope. Per the
brief, the existing metadata stays exactly as it is for now; this is the
structure that will later justify it rather than guess it.

DESIGN CONSTRAINT: machine-readable and complete enough to replay. Every
field is either a plain scalar or a plain structure, `to_dict()` is JSON-
safe, and the compiled query plus its bound parameters are recorded
verbatim. A reviewer holding a receipt can re-execute the exact query.

NOT A DUPLICATE OF ToolResult. `XAggToolResult` carries `raw_summary_text`
(the deterministic rendering served when a paraphrase fails verification)
and `aggregate_kind`. The receipt explains HOW the number was reached; the
tool result carries WHAT is shown to the user. When the engine is wired in,
the receipt will attach alongside `raw_summary_text` rather than replacing
it.
"""
from __future__ import annotations

import dataclasses
import json
from datetime import datetime, timezone
from typing import Any, Optional

from src.pipeline.aggregate.coverage import CoverageReport
from src.pipeline.aggregate.spec import AggregateSpec


@dataclasses.dataclass(frozen=True)
class ExecutedQuery:
    """One query as actually run, with everything needed to re-run it.

    `params` is recorded separately from `text` and never interpolated into
    it — the same separation the compiler enforces, preserved into the
    audit trail so a receipt can never be mistaken for an executable string
    with values already baked in.
    """

    backend: str
    text: str
    params: dict[str, Any]
    row_count: int
    duration_ms: Optional[float] = None
    status: str = "ok"
    error: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class CrossCheck:
    """An independent recomputation of the same quantity.

    Mandatory for the high-risk shapes (ratios, fanning traversals, edge
    grains) because no invariant distinguishes 4 from 70 — both satisfy
    numerator <= denominator and 0 <= pct <= 100. Only a second route
    computed differently can catch a grain error, so `agrees=False` is a
    refusal signal, never an input to picking a winner.
    """

    route: str
    value: Any
    agrees: bool
    note: Optional[str] = None


@dataclasses.dataclass(frozen=True)
class AggregateReceipt:
    """Full provenance for one aggregate answer."""

    question_text: str
    spec_hash: str
    spec: dict

    grain: str
    distinct_key: Optional[str]
    population_definition: str

    measure: str
    value: Any
    numerator_n: Optional[float] = None
    denominator_n: Optional[float] = None

    group_keys: tuple[str, ...] = ()
    filters_applied: tuple[str, ...] = ()
    filters_dropped: tuple[str, ...] = ()

    # Compiler-injected invariants. Recorded because a reader must be able
    # to see that they WERE applied — their absence is the 429-vs-208 bug.
    injected_predicates: tuple[str, ...] = ()
    excluded_merged_n: int = 0
    excluded_superseded_n: int = 0
    excluded_null_n: int = 0

    canonicalization_policy: Optional[str] = None
    canonicalization_applied: bool = False
    canonicalization_collapsed_n: int = 0
    canonicalization_max_component: Optional[int] = None

    queries: tuple[ExecutedQuery, ...] = ()
    cross_checks: tuple[CrossCheck, ...] = ()

    invariants_checked: tuple[str, ...] = ()
    invariants_skipped: tuple[str, ...] = ()

    coverage: Optional[CoverageReport] = None
    coverage_verdict: Optional[str] = None
    caveat: Optional[str] = None

    calculation_steps: tuple[str, ...] = ()
    compiler_notes: tuple[str, ...] = ()

    source: Optional[str] = None
    truncated: bool = False
    truncation_note: Optional[str] = None

    registry_as_of: Optional[str] = None
    execution_status: str = "ok"
    refusal_code: Optional[str] = None
    refusal_reason: Optional[str] = None
    as_of: str = dataclasses.field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)

    def to_json(self, *, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, default=str, ensure_ascii=False)

    def explain(self) -> str:
        """A short human-readable account of how the number was reached.

        Deliberately plain text and deterministic — no LLM. This is what a
        reviewer reads first, and what the eventual rendering layer can
        show under an answer without another model call.
        """
        lines = [
            f"Question: {self.question_text}",
            f"Computed: {self.measure} = {self.value}",
            f"Grain: {self.grain}"
            + (f" (distinct on {self.distinct_key})" if self.distinct_key else ""),
            f"Population: {self.population_definition}",
        ]
        if self.numerator_n is not None and self.denominator_n is not None:
            lines.append(f"Ratio: {self.numerator_n} / {self.denominator_n}")
        if self.filters_applied:
            lines.append("Filters: " + "; ".join(self.filters_applied))
        if self.injected_predicates:
            lines.append("Invariant exclusions: " + "; ".join(self.injected_predicates))
        if self.excluded_merged_n:
            lines.append(f"Merged/tombstoned records excluded: {self.excluded_merged_n}")
        if self.excluded_superseded_n:
            lines.append(f"Superseded relationships excluded: {self.excluded_superseded_n}")
        if self.canonicalization_policy:
            lines.append(
                f"Canonicalization: {self.canonicalization_policy}"
                + (
                    f" (applied, {self.canonicalization_collapsed_n} collapsed)"
                    if self.canonicalization_applied
                    else " (not applied)"
                )
            )
        for c in self.cross_checks:
            lines.append(
                f"Cross-check [{c.route}]: {c.value} — "
                + ("agrees" if c.agrees else "DISAGREES")
            )
        if self.caveat:
            lines.append(f"Coverage: {self.caveat}")
        if self.refusal_reason:
            lines.append(f"Refused: {self.refusal_reason}")
        for step in self.calculation_steps:
            lines.append(f"  - {step}")
        return "\n".join(lines)


def refusal_receipt(
    *,
    question_text: str,
    spec: Optional[AggregateSpec],
    code: str,
    reason: str,
    registry_as_of: Optional[str] = None,
) -> AggregateReceipt:
    """A receipt for an answer that was NOT given.

    Refusals get receipts too, and for the same reason answers do: "why
    did it refuse?" is exactly as important as "why that number?", and it
    is the question a user will actually ask. Without this, a refusal is
    indistinguishable from a failure.
    """
    return AggregateReceipt(
        question_text=question_text,
        spec_hash=spec.spec_hash() if spec is not None else "",
        spec=spec.to_dict() if spec is not None else {},
        grain=spec.grain if spec is not None else "",
        distinct_key=spec.distinct_key if spec is not None else None,
        population_definition=(
            describe_population(spec) if spec is not None else ""
        ),
        measure=spec.measure if spec is not None else "",
        value=None,
        execution_status="refused",
        refusal_code=code,
        refusal_reason=reason,
        registry_as_of=registry_as_of,
    )


def describe_population(spec: AggregateSpec) -> str:
    """A human-readable predicate for the population being counted.

    This string is what makes a "0" legible: "0 cases registered in 2019 or
    earlier" is an answer, while a bare "0" is indistinguishable from a
    broken query. Module 144's own fix turned on exactly this distinction.
    """
    parts = [spec.population.entity]
    for t in spec.population.traversals:
        arrow = "->" if t.direction == "out" else "<-"
        role = f"[{t.role_field}={t.role_value}]" if t.role_value else ""
        parts.append(f"{arrow}{t.rel}{role}{arrow and ''}{t.target}")
    desc = " ".join(parts)

    conds: list[str] = []
    for p in spec.population.predicates:
        if hasattr(p, "rel"):
            conds.append(
                f"having {p.op} {p.value} related {p.target} "
                f"(counted at {p.count_grain} grain)"
            )
        else:
            conds.append(f"{p.field} {p.op} {p.value!r}")
    if conds:
        desc += " where " + " and ".join(conds)
    if spec.time_window:
        tw = spec.time_window
        desc += f" within {tw.field} [{tw.start or '-inf'} .. {tw.end or '+inf'}]"
    if spec.scope.kind == "jurisdiction" and spec.scope.case_ids:
        desc += f" restricted to {len(spec.scope.case_ids)} case(s)"
    return desc
