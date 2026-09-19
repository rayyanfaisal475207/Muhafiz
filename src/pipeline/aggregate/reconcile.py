# -*- coding: utf-8 -*-
"""
Phase 4E — deterministic reconciliation across the three routes.

THE RULE THIS MODULE ENFORCES ABOVE ALL OTHERS. When two independent
computation routes disagree about a number, this layer does NOT pick a
winner. It does not average, it does not prefer the deterministic route
because it is deterministic, it does not prefer the generated route because
it is flexible, and it never asks a model which answer looks better. It
classifies the disagreement and, where the cause cannot be established
structurally, it refuses.

That is a deliberate and slightly uncomfortable design: the system will
sometimes decline to answer a question for which one of its routes had the
right number. The alternative — picking — means shipping a confident wrong
answer every time the preferred route is the wrong one, which is precisely
the failure Phase 0-3 spent its effort eliminating.

WHY COMPARING VALUES IS NOT ENOUGH. Two routes can both be arithmetically
correct and still produce different numbers, because they answered
different questions. 92 distinct accused and 94 accused involvements are
both right. 4 cases with multiple officers and 70 cases with multiple
assignment rows are both right. So reconciliation compares
`NumericResult.comparable_key()` — interpretation, grain, grouping —
BEFORE it compares values, and a mismatch there is classified as
`SEMANTICALLY_DIFFERENT` rather than as a conflict about arithmetic.

THE SEMANTIC ROUTE IS NOT A THIRD NUMBER. It cannot vote on a value; it can
only corroborate or contradict an interpretation. `RouteResult.numeric`
returns `None` for evidence results, so the structure of the contract —
not a convention here — is what prevents a retrieved numeral from being
treated as a computation.
"""
from __future__ import annotations

import dataclasses
from typing import Any, Literal, Optional

from src.pipeline.aggregate.routes import (
    ROUTE_AGE,
    ROUTE_SEMANTIC,
    ROUTE_STRUCTURED,
    EvidenceResult,
    NumericResult,
    RouteResult,
)

Classification = Literal[
    "AGREEMENT",
    "PARTIAL_AGREEMENT",
    "SEMANTICALLY_DIFFERENT",
    "CONFLICT",
    "INSUFFICIENT_EVIDENCE",
    "SINGLE_ROUTE_VALID",
    "REFUSED",
]

AGREEMENT: Classification = "AGREEMENT"
PARTIAL_AGREEMENT: Classification = "PARTIAL_AGREEMENT"
SEMANTICALLY_DIFFERENT: Classification = "SEMANTICALLY_DIFFERENT"
CONFLICT: Classification = "CONFLICT"
INSUFFICIENT_EVIDENCE: Classification = "INSUFFICIENT_EVIDENCE"
SINGLE_ROUTE_VALID: Classification = "SINGLE_ROUTE_VALID"
REFUSED: Classification = "REFUSED"

#: Relative tolerance for float comparison. Counts are integers and must
#: match exactly; this exists so 5.47945205479452 and the same value
#: recomputed through a different expression are not called a conflict.
_REL_TOL = 1e-9


@dataclasses.dataclass(frozen=True)
class InvariantViolation:
    code: str
    message: str


@dataclasses.dataclass(frozen=True)
class ReconciliationResult:
    classification: Classification
    #: The agreed value, present ONLY when routes agreed or exactly one
    #: computed. Never populated for a CONFLICT — that is the point.
    value: Any = None
    interpretation: Optional[str] = None
    grain: Optional[str] = None

    agreeing_routes: tuple[str, ...] = ()
    disagreeing_routes: tuple[str, ...] = ()
    refused_routes: tuple[str, ...] = ()

    semantic_support: Optional[str] = None   # "SUPPORTS" | "CONTRADICTS" | "NEUTRAL"
    invariant_violations: tuple[InvariantViolation, ...] = ()

    explanation: str = ""
    diagnosis: Optional[str] = None
    requires_investigation: bool = False

    def to_dict(self) -> dict:
        d = dataclasses.asdict(self)
        d["invariant_violations"] = [
            dataclasses.asdict(v) for v in self.invariant_violations
        ]
        return d


def _subject_label(result: Any) -> Optional[str]:
    """The single label a result counted, for the canonicalization proof.

    Returns None when the subject is ambiguous (more than one label in
    play), which makes the proof fail closed rather than pick one.
    """
    prov = getattr(result, "provenance", {}) or {}
    plan = prov.get("plan") or {}
    if plan.get("root_label"):
        return plan["root_label"]
    labels = prov.get("chosen_labels") or []
    if len(labels) == 1:
        return labels[0]
    numeric = getattr(result, "numeric", None)
    population = getattr(numeric, "population", None)
    if isinstance(population, str) and population:
        # Structured populations are described as "Person where ..." —
        # the leading token is the measured label.
        head = population.split()[0].strip()
        return head or None
    return None


def _traversal_count(result: Any) -> int:
    """Relationship hops the computation made.

    Any hop can multiply rows, so a single one is enough to refuse the
    count/count_distinct equivalence proof. An unknown shape counts as
    "traversed" — the conservative direction.
    """
    prov = getattr(result, "provenance", {}) or {}
    plan = prov.get("plan") or {}
    if "patterns" in plan:
        return len(plan.get("patterns") or ())
    rels = prov.get("chosen_relationships")
    if rels is not None:
        return len(rels)
    numeric = getattr(result, "numeric", None)
    population = getattr(numeric, "population", None)
    if isinstance(population, str) and (" via " in population or "->" in population):
        return 1
    return 0


def _values_match(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if float(a) == float(b):
            return True
        scale = max(abs(float(a)), abs(float(b)), 1.0)
        return abs(float(a) - float(b)) <= _REL_TOL * scale
    return a == b


# ══════════════════════════════════════════════════════════════════════
# Invariants
# ══════════════════════════════════════════════════════════════════════
def check_invariants(numeric: NumericResult) -> list[InvariantViolation]:
    """Deterministic checks a single numeric claim must satisfy.

    These are the invariants the aggregate engine already established, and
    they apply to ANY route's output — including the generated one, which
    is the point: a generated query that returns a negative count or a
    percentage above 100 is self-evidently wrong regardless of what any
    other route said.
    """
    out: list[InvariantViolation] = []
    v = numeric.value
    if not isinstance(v, (int, float)):
        return out

    if numeric.interpretation in ("count", "count_distinct") and v < 0:
        out.append(InvariantViolation(
            "negative_count", f"Count is negative ({v})."
        ))
    if numeric.interpretation == "percentage" and not (0.0 <= float(v) <= 100.0):
        out.append(InvariantViolation(
            "percentage_out_of_range", f"Percentage {v} lies outside 0-100."
        ))
    if numeric.interpretation == "ratio" and float(v) < 0:
        out.append(InvariantViolation(
            "negative_ratio", f"Ratio is negative ({v})."
        ))
    return out


# ══════════════════════════════════════════════════════════════════════
# Semantic corroboration
# ══════════════════════════════════════════════════════════════════════
def _semantic_stance(semantic: Optional[RouteResult]) -> Optional[str]:
    """How the evidence route bears on the computed interpretation.

    Returns a STANCE, never a number. A contradiction does not overturn a
    computation — it flags that the corpus contains passages asserting an
    absence, which is a reason for a human to look, not a reason to change
    an arithmetic result.
    """
    if semantic is None or not semantic.ok:
        return None
    result = semantic.result
    if not isinstance(result, EvidenceResult):
        return None
    if result.evidence_count == 0:
        return "NEUTRAL"
    if result.contradicting:
        return "CONTRADICTS"
    return "SUPPORTS"


# ══════════════════════════════════════════════════════════════════════
# Diagnosis of a conflict
# ══════════════════════════════════════════════════════════════════════
def _diagnose(structured: RouteResult, age: RouteResult) -> Optional[str]:
    """Attempt a STRUCTURAL account of why two numbers differ.

    Strictly evidence-based: it reports what the two routes' own metadata
    says, and offers no opinion on which is right. When nothing in the
    metadata explains the gap, it returns None and the caller escalates to
    investigation rather than inventing a story.
    """
    s_num, a_num = structured.numeric, age.numeric
    if s_num is None or a_num is None:
        return None

    notes: list[str] = []

    if s_num.grain and a_num.grain and s_num.grain != a_num.grain:
        notes.append(
            f"grain differs: structured computed at {s_num.grain}, generated "
            f"query declared {a_num.grain}"
        )

    guard_codes = age.provenance.get("guard_violations") or []
    if guard_codes:
        notes.append(f"generated query carried guard flags: {', '.join(guard_codes)}")

    gen = (age.provenance.get("generated_cypher") or "").lower()
    if gen:
        if "distinct" not in gen:
            notes.append(
                "generated query counts without DISTINCT, so it may be counting "
                "relationship rows rather than entities"
            )
        if "merged_into" not in gen:
            notes.append(
                "generated query does not exclude merge tombstones, which the "
                "structured route excludes by construction"
            )
        if "superseded_by" not in gen:
            notes.append(
                "generated query does not exclude superseded edge versions, "
                "which the structured route excludes by construction"
            )

    return "; ".join(notes) if notes else None


# ══════════════════════════════════════════════════════════════════════
# Entry point
# ══════════════════════════════════════════════════════════════════════
def reconcile(
    structured: Optional[RouteResult] = None,
    age: Optional[RouteResult] = None,
    semantic: Optional[RouteResult] = None,
    *,
    snapshot: Optional[Any] = None,
) -> ReconciliationResult:
    """Classify agreement across the routes. Deterministic; no LLM.

    `snapshot` is optional and enables PROVABLE metric canonicalization
    (Phase 7). Phase 6 measured four cases where both routes returned the
    same correct number and this function still reported
    SEMANTICALLY_DIFFERENT, because one declared `count` and the other
    `count_distinct` — understating real corroboration.

    With a snapshot, `canonical.py` proves whether those two spellings are
    equal FOR THIS POPULATION: they are, when the computation traverses
    nothing (one row per node) and the label's distinct key is measured
    present-on-every-node and unique. Where the proof fails — any traversal
    at all, or no unique key — the two stay different, which is what keeps
    the 73->449 inflation and the 4-vs-70 conflict visible.

    Without a snapshot the behaviour is exactly as before, so every
    existing caller is unaffected.
    """
    present = [r for r in (structured, age, semantic) if r is not None]
    refused = tuple(
        r.route for r in present if r.status in ("REFUSED", "UNSUPPORTED", "EXECUTION_ERROR")
    )
    stance = _semantic_stance(semantic)

    numeric_routes = [
        r for r in (structured, age)
        if r is not None and r.ok and r.numeric is not None
    ]

    # ── No computation at all ─────────────────────────────────────────
    if not numeric_routes:
        # Both computation routes declined. Evidence alone cannot answer an
        # aggregate question, so this is a refusal even when retrieval
        # succeeded — that is the "semantic route is not a numeric oracle"
        # rule applied at the top level.
        reasons = "; ".join(
            f"{r.route}: {r.refusal_code}" for r in present
            if r.refusal_code
        )
        return ReconciliationResult(
            classification=REFUSED,
            refused_routes=refused,
            semantic_support=stance,
            explanation=(
                f"No computation route produced a value ({reasons or 'no reason given'}). "
                f"The semantic route corroborates interpretation only and cannot "
                f"supply an aggregate."
            ),
        )

    # ── Invariants, per route, before any comparison ───────────────────
    violations: list[InvariantViolation] = []
    for r in numeric_routes:
        violations.extend(check_invariants(r.numeric))
    if violations:
        return ReconciliationResult(
            classification=CONFLICT,
            disagreeing_routes=tuple(r.route for r in numeric_routes),
            refused_routes=refused,
            semantic_support=stance,
            invariant_violations=tuple(violations),
            explanation=(
                "A computed result violates a deterministic invariant, so no "
                "value is served regardless of route agreement."
            ),
            requires_investigation=True,
        )

    # ── Exactly one computation route ─────────────────────────────────
    if len(numeric_routes) == 1:
        only = numeric_routes[0]
        n = only.numeric
        return ReconciliationResult(
            classification=SINGLE_ROUTE_VALID,
            value=n.value,
            interpretation=n.interpretation,
            grain=n.grain,
            agreeing_routes=(only.route,),
            refused_routes=refused,
            semantic_support=stance,
            explanation=(
                f"Only the {only.route} route computed a value; no independent "
                f"computation was available to corroborate it."
                + (
                    " Retrieved evidence asserts an absence of records relevant "
                    "to this question."
                    if stance == "CONTRADICTS" else ""
                )
            ),
            requires_investigation=(stance == "CONTRADICTS"),
        )

    # ── Two computation routes ────────────────────────────────────────
    s, a = structured, age
    s_num, a_num = s.numeric, a.numeric

    # PHASE 5D REMOVED A SNAPSHOT GATE THAT STOOD HERE. Phase 5C ran the AGE
    # route against a disposable COPY of the graph, so an AGE figure
    # described a possibly-stale vintage and could not be compared directly
    # against a live structured figure; results lacking a snapshot id were
    # demoted to SINGLE_ROUTE_VALID. Phase 5D compiles the AGE route's typed
    # plan into read-only Cypher executed against production
    # `evidence_graph` — the same data the structured route reads — so that
    # asymmetry no longer exists, and keeping the gate would suppress
    # genuine agreements and conflicts.
    #
    # What deliberately REMAINS is the general semantics comparison below:
    # `comparable_key()` asks whether the two routes answered the same
    # question (interpretation, grain, grouping) before asking whether they
    # got the same answer. That is what keeps "92 distinct accused" from
    # being compared against "94 accused involvements", and it is unrelated
    # to evaluator staleness.

    # Did they answer the same question? Asked before "the same answer?".
    #
    # Phase 7: when a snapshot is available, the metric spelling is
    # canonicalised first — but ONLY where equivalence is deterministically
    # provable. Grain and grouping pass through untouched, so a RECORD-vs-
    # ENTITY disagreement about what one unit IS still reads as different.
    s_key, a_key = s_num.comparable_key(), a_num.comparable_key()
    canon_notes: list[str] = []
    if snapshot is not None:
        from src.pipeline.aggregate import canonical as _canon

        s_key, s_canon = _canon.canonical_comparable_key(
            snapshot,
            interpretation=s_num.interpretation, grain=s_num.grain,
            grouping=s_num.grouping,
            label=_subject_label(s), traversal_count=_traversal_count(s),
        )
        a_key, a_canon = _canon.canonical_comparable_key(
            snapshot,
            interpretation=a_num.interpretation, grain=a_num.grain,
            grouping=a_num.grouping,
            label=_subject_label(a), traversal_count=_traversal_count(a),
        )
        canon_notes = [
            c.describe() for c in (s_canon, a_canon) if c.applied
        ]

    if s_key != a_key:
        return ReconciliationResult(
            classification=SEMANTICALLY_DIFFERENT,
            disagreeing_routes=(s.route, a.route),
            refused_routes=refused,
            semantic_support=stance,
            explanation=(
                f"The routes computed different quantities, so their values are "
                f"not comparable: {s.route} produced "
                f"{s_num.interpretation}/{s_num.grain} = {s_num.value}, "
                f"{a.route} produced {a_num.interpretation}/{a_num.grain} = "
                f"{a_num.value}. Both may be arithmetically correct."
            ),
            diagnosis=_diagnose(s, a),
            requires_investigation=True,
        )

    if _values_match(s_num.value, a_num.value):
        return ReconciliationResult(
            classification=AGREEMENT,
            value=s_num.value,
            interpretation=s_num.interpretation,
            grain=s_num.grain,
            agreeing_routes=(s.route, a.route),
            refused_routes=refused,
            semantic_support=stance,
            explanation=(
                f"Two structurally independent computation routes agree on "
                f"{s_num.value} ({s_num.interpretation}, {s_num.grain} grain)."
                + (
                    " Retrieved evidence supports the interpretation."
                    if stance == "SUPPORTS" else ""
                )
                + (
                    " Retrieved evidence asserts an absence of records, which "
                    "warrants review despite the agreement."
                    if stance == "CONTRADICTS" else ""
                )
            ),
            requires_investigation=(stance == "CONTRADICTS"),
        )

    # ── Genuine numeric conflict. No winner is chosen. ────────────────
    diagnosis = _diagnose(s, a)
    return ReconciliationResult(
        classification=CONFLICT,
        disagreeing_routes=(s.route, a.route),
        refused_routes=refused,
        semantic_support=stance,
        explanation=(
            f"Independent routes disagree: {s.route} = {s_num.value}, "
            f"{a.route} = {a_num.value}, both claiming "
            f"{s_num.interpretation} at {s_num.grain} grain. No value is "
            f"served; neither route is preferred, averaged, nor adjudicated."
        ),
        diagnosis=diagnosis,
        requires_investigation=True,
    )
