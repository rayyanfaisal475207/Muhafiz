# -*- coding: utf-8 -*-
"""
Phase 4A — the common contract the three reasoning routes share.

WHY A SHARED CONTRACT AT ALL. Three routes compute (or corroborate) the
same question by structurally different means. Comparing them requires a
form in which "4" from the deterministic compiler and "4" from a generated
Cypher query are recognisably the same claim — and, just as importantly, in
which "4 people" and "4 relationship rows" are recognisably NOT.

So `NumericResult` carries grain, population and coverage alongside the
number. Reconciliation that compared bare values would have called the
4-vs-70 officer case a disagreement about arithmetic when it is a
disagreement about what was counted.

WHAT THIS IS NOT. Not a new result type competing with `AggregateOutcome`
or `AggregateReceipt`. The structured route already has those and they stay
authoritative for it; `RouteResult` WRAPS them (see `provenance` and
`native`) so no existing guarantee is restated in a weaker form. The
adapter in `route_structured.py` is deliberately thin for that reason.

THE ONE RULE THAT OUTRANKS THE OTHERS. No route is the oracle. The
deterministic route is a trusted, constrained computation — it is not
ground truth by construction, and the evaluation harness compares every
route against independently-established figures rather than against it.
`RouteResult` therefore has no "is_authoritative" flag, and nothing in this
module ranks the routes.
"""
from __future__ import annotations

import dataclasses
import uuid
from typing import Any, Literal, Optional

# ══════════════════════════════════════════════════════════════════════
# Route identity and status
# ══════════════════════════════════════════════════════════════════════
RouteName = Literal["structured", "age_text2cypher", "semantic"]

ROUTE_STRUCTURED: RouteName = "structured"
ROUTE_AGE: RouteName = "age_text2cypher"
ROUTE_SEMANTIC: RouteName = "semantic"

#: SUCCESS          — the route produced a result it stands behind.
#: REFUSED          — the route declined, by design, with a named code. A
#:                    refusal is an outcome, not a failure: it is what the
#:                    whole engine exists to do instead of guessing.
#: EXECUTION_ERROR  — infrastructure failed (database, model server). NOT
#:                    the same as REFUSED, because a refusal is a judgement
#:                    about the question and this is a judgement about
#:                    nothing.
#: UNSUPPORTED      — the route cannot express this class of question at
#:                    all. Distinguished from REFUSED so the harness can
#:                    tell "we won't" from "we can't".
#: CONFLICT         — reserved for reconciliation; a single route never
#:                    returns it.
RouteStatus = Literal[
    "SUCCESS", "REFUSED", "EXECUTION_ERROR", "UNSUPPORTED", "CONFLICT"
]

SUCCESS: RouteStatus = "SUCCESS"
REFUSED: RouteStatus = "REFUSED"
EXECUTION_ERROR: RouteStatus = "EXECUTION_ERROR"
UNSUPPORTED: RouteStatus = "UNSUPPORTED"
CONFLICT: RouteStatus = "CONFLICT"

#: What kind of thing `RouteResult.result` holds. Reconciliation branches on
#: this rather than on `isinstance`, so a route that returns a grouped
#: breakdown is never silently compared against a scalar.
ResultShape = Literal["scalar", "ratio", "grouped", "evidence", "none"]


@dataclasses.dataclass(frozen=True)
class AggregateRouteRequest:
    """One question, asked of one or more routes.

    `scope` carries the authenticated caller context. It is passed through
    verbatim rather than re-derived per route: the structured route's
    validator already refuses an unauthenticated or over-broad scope, and
    the AGE route must inherit exactly the same gate rather than inventing
    a parallel one.
    """

    question: str
    scope: Any  # spec.Scope — untyped here to keep this module import-light
    request_id: str = dataclasses.field(
        default_factory=lambda: uuid.uuid4().hex[:12]
    )
    #: Optional pre-built AggregateSpec for the structured route. Phase 4
    #: deliberately does NOT generate specs from natural language, so the
    #: harness supplies them; this field is how.
    spec: Any = None
    notes: str = ""


@dataclasses.dataclass(frozen=True)
class NumericResult:
    """A number, plus everything needed to know what it counted.

    `grain` and `population` are not decoration. The whole Phase 0-3 body
    of work turns on the fact that two correct numbers can answer different
    questions: 92 distinct accused vs 94 accused involvements, 4 cases with
    multiple officers vs 70 cases with multiple assignment rows. A contract
    that carried only `value` would make those indistinguishable at exactly
    the moment reconciliation needs to tell them apart.
    """

    value: Any
    #: "count" | "count_distinct" | "percentage" | "sum" | "avg" | ...
    interpretation: str
    grain: Optional[str] = None
    population: Optional[str] = None
    grouping: tuple[str, ...] = ()
    unit: Optional[str] = None

    def comparable_key(self) -> tuple:
        """What must match before two results may be compared numerically.

        Deliberately excludes `value`: this is the question of whether the
        two routes answered the SAME question, asked before the question of
        whether they got the same answer.
        """
        return (self.interpretation, self.grain, self.grouping)


@dataclasses.dataclass(frozen=True)
class EvidenceItem:
    """One retrieved piece of supporting or contradicting evidence."""

    source_id: str
    text_excerpt: str
    score: Optional[float] = None
    metadata: dict = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class EvidenceResult:
    """What the semantic route returns. Note the absence of a `value`.

    The semantic route corroborates an interpretation; it does not compute
    aggregates. `numeric_mentions` exists so that numbers APPEARING in
    retrieved text can be surfaced for a human — explicitly labelled as
    text-derived — without ever being promoted into an aggregate answer.
    Reconciliation refuses to treat them as a numeric route (see
    `reconcile.py`), which is the structural form of "semantic retrieval is
    not a third numeric oracle".
    """

    evidence_count: int
    supporting: tuple[EvidenceItem, ...] = ()
    contradicting: tuple[EvidenceItem, ...] = ()
    relevant_entities: tuple[str, ...] = ()
    relevant_relationships: tuple[str, ...] = ()
    interpretation: Optional[str] = None
    #: Numbers seen in retrieved text, with the excerpt they came from.
    #: NEVER an aggregate result. Kept for diagnosis only.
    numeric_mentions: tuple[dict, ...] = ()


@dataclasses.dataclass(frozen=True)
class RouteResult:
    """The common form every route normalises into."""

    route: RouteName
    status: RouteStatus
    result_shape: ResultShape = "none"
    #: NumericResult | EvidenceResult | list[dict] (grouped) | None
    result: Any = None

    coverage: Any = None          # coverage.CoverageReport, where available
    provenance: dict = dataclasses.field(default_factory=dict)
    warnings: tuple[str, ...] = ()
    refusal_code: Optional[str] = None
    refusal_reason: Optional[str] = None
    execution_metadata: dict = dataclasses.field(default_factory=dict)
    #: The route's own richer object (AggregateReceipt, generated query,
    #: raw evidence). Preserved so wrapping never loses information the
    #: route already established.
    native: Any = None

    @property
    def ok(self) -> bool:
        return self.status == SUCCESS

    @property
    def numeric(self) -> Optional[NumericResult]:
        """The numeric claim, or None when this route made none.

        Evidence results deliberately return None even when they carry
        `numeric_mentions`: a number quoted from a document is not a
        computed aggregate, and this property is what reconciliation uses
        to decide who is making a numeric claim.
        """
        if isinstance(self.result, NumericResult):
            return self.result
        return None

    def to_dict(self) -> dict:
        """JSON-safe form for the evaluation report."""
        out = {
            "route": self.route,
            "status": self.status,
            "result_shape": self.result_shape,
            "refusal_code": self.refusal_code,
            "refusal_reason": self.refusal_reason,
            "warnings": list(self.warnings),
            "provenance": self.provenance,
            "execution_metadata": self.execution_metadata,
        }
        if isinstance(self.result, NumericResult):
            out["result"] = dataclasses.asdict(self.result)
        elif isinstance(self.result, EvidenceResult):
            ev = dataclasses.asdict(self.result)
            # Excerpts can be long; the report keeps counts and a sample.
            ev["supporting"] = ev["supporting"][:3]
            ev["contradicting"] = ev["contradicting"][:3]
            out["result"] = ev
        elif self.result is not None:
            out["result"] = self.result
        if self.coverage is not None:
            out["coverage_verdict"] = getattr(self.coverage, "verdict", None)
        return out


def refusal(
    route: RouteName,
    code: str,
    reason: str,
    *,
    status: RouteStatus = REFUSED,
    provenance: Optional[dict] = None,
    native: Any = None,
) -> RouteResult:
    """Build a refusal in the common form.

    A helper rather than a pattern repeated per route, so that every
    refusal carries a code AND a reason. A refusal with no code cannot be
    classified by the harness, and one with no reason cannot be acted on by
    a person.
    """
    return RouteResult(
        route=route,
        status=status,
        result_shape="none",
        refusal_code=code,
        refusal_reason=reason,
        provenance=provenance or {},
        native=native,
    )
