# -*- coding: utf-8 -*-
"""
Spec fidelity: does the structure carry what the question asked for?

THE FAILURE THIS EXISTS TO CATCH. Every other check in this package
validates the spec against the REGISTRY — are these labels real, is this
traversal observed, is this field populated. None of them validates the
spec against the QUESTION, because by the time a spec exists the question
is a string nobody reads again. So a spec that quietly drops a condition
is not merely undetected; it is UNDETECTABLE downstream, and it is also
*simpler* than the correct spec — fewer predicates, fewer traversals — so
it passes more gates and fires fewer verification triggers than the spec
that would have been right.

Measured consequence: a question asking for a filtered subset was answered
over the entire population, reported COMPLETE coverage, ran at FULL
verification, and carried no warning. In one case two independent routes
computed the same substituted question and AGREED, so the answer was
labelled independently verified. Agreement between two routes is not
agreement with the question.

HOW THIS CHECKS IT WITHOUT READING THE QUESTION. It does not parse natural
language, and it holds no vocabulary. The generator declares, in its own
words, every condition it believes the question imposes, and marks each
one applied or not. This module compares that declaration against what the
structure actually contains:

  - a condition declared UNAPPLIED is taken at its word -> refuse
  - a condition declared APPLIED is checked against the spec: the place it
    claims to live must actually hold something

That second check is what makes this more than a self-report. A model that
drops a filter *and* claims it applied one is caught by counting: it said
there were three conditions and the structure carries one.

WHAT IT DELIBERATELY CANNOT DO. A model that drops a condition and never
declares it is not caught. There is no way to catch that here — it would
require independently understanding the question, which is the task that
produced the error. This narrows the failure class; it does not close it,
and the report says so rather than implying a guarantee.

NO KEYWORDS. There is no term list, no role vocabulary, no question
matching, and nothing in this file names a domain value. Everything it
knows is structural: how many conditions were declared, where each claims
to live, and what the spec holds in those places. A new question about a
qualifier nobody has seen yet is handled by the same code.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Optional

from src.pipeline.aggregate.spec import AggregateSpec

logger = logging.getLogger(__name__)

#: Where a declared condition can live in an AggregateSpec. The value is
#: how that place is counted in the structure; see `_carriers`.
WHERE_PREDICATE = "predicate"
WHERE_TRAVERSAL = "traversal"
WHERE_ROLE = "role"
WHERE_TIME_WINDOW = "time_window"
WHERE_THRESHOLD = "threshold"
WHERE_GRAIN = "grain"

_CARRIER_NAMES: frozenset[str] = frozenset(
    {
        WHERE_PREDICATE,
        WHERE_TRAVERSAL,
        WHERE_ROLE,
        WHERE_TIME_WINDOW,
        WHERE_THRESHOLD,
        WHERE_GRAIN,
    }
)


@dataclasses.dataclass(frozen=True)
class FidelityIssue:
    code: str
    message: str

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


@dataclasses.dataclass(frozen=True)
class FidelityResult:
    """Whether the spec carries what the generator said the question asked."""

    ok: bool
    issues: tuple[FidelityIssue, ...] = ()
    #: Conditions declared, and how many the structure can account for.
    declared_n: int = 0
    carried_n: int = 0

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "declared_n": self.declared_n,
            "carried_n": self.carried_n,
            "issues": [i.to_dict() for i in self.issues],
        }


def _carriers(spec: AggregateSpec) -> dict[str, int]:
    """How many condition-bearing elements the spec holds, by kind.

    Counted structurally. `role` is counted separately from `traversal`
    because a hop with a role filter carries two distinct conditions — the
    relationship AND the part played in it — and conflating them would let
    a spec that traverses the right edge with no role filter satisfy a
    declaration about the role.
    """
    pop = spec.population
    traversals = tuple(pop.traversals or ())
    roles = sum(
        1 for t in traversals
        if getattr(t, "role_field", None) or getattr(t, "role_value", None)
    )
    # A role filter can also be expressed on a relation-count predicate.
    roles += sum(
        1 for p in (pop.predicates or ())
        if getattr(p, "role_field", None) or getattr(p, "role_value", None)
    )
    return {
        WHERE_PREDICATE: len(pop.predicates or ()),
        WHERE_TRAVERSAL: len(traversals),
        WHERE_ROLE: roles,
        WHERE_TIME_WINDOW: 1 if spec.time_window is not None else 0,
        WHERE_THRESHOLD: 1 if spec.threshold is not None else 0,
        # Grain is always set; a condition claiming to live there is
        # satisfied by the spec having chosen a grain at all.
        WHERE_GRAIN: 1,
    }


def _normalise_where(raw: str) -> str:
    """Map a declared location onto one of the structural carriers.

    Tolerant on purpose, and only about SPELLING. Observed live, the same
    model wrote 'predicate' for one question and 'predicates' for the next;
    treating those as different would silently downgrade the second claim
    to unverifiable, which is the opposite of what this module is for. A
    plural, a stray space or a different case is the same location.

    It does NOT guess at meaning: a location this does not recognise stays
    unrecognised, and `check` declines to hold it against the spec rather
    than inventing a mapping for it.
    """
    where = (raw or "").strip().lower().replace(" ", "_").replace("-", "_")
    if where.endswith("s") and where[:-1] in _CARRIER_NAMES:
        where = where[:-1]
    return where


def check(
    spec: AggregateSpec,
    generation: Optional[Any],
) -> FidelityResult:
    """Compare the declared conditions against the structure that was built.

    `generation` is optional: a caller that supplied its own spec made no
    declaration, so there is nothing to check and the result is ok. That
    keeps every existing test and the evaluation harness working unchanged,
    and means this check only ever constrains specs an LLM produced.
    """
    declared = tuple(getattr(generation, "declared_constraints", ()) or ())
    if not declared:
        return FidelityResult(ok=True)

    issues: list[FidelityIssue] = []

    carriers = _carriers(spec)

    # ── 1. Anything the generator itself says it could not express ─────
    #
    # CHECKED, NOT TAKEN ON TRUST. A model marked a filter unapplied
    # because no row would match it — "the schema does not show any weapon
    # with 'unicorn' in canonical_name" — while having built that exact
    # predicate. Those are different statements: "I could not express this"
    # is a capability failure, and "this will match nothing" is an ANSWER,
    # and a correct one. Refusing the second would turn every honest zero
    # into a refusal and destroy the ability to say "none".
    #
    # So an unapplied claim is believed only when the structure agrees with
    # it. If the spec does carry a constraint in the named place, the model
    # underestimated itself and the claim is dropped.
    unapplied = [c for c in declared if not c.applied]
    for c in unapplied:
        where = _normalise_where(c.where)
        if where in carriers and carriers[where] > 0:
            logger.info(
                "aggregate fidelity: spec %s declared %r unapplied, but %s "
                "carries %d — believing the structure",
                spec.spec_hash(), c.describes, where, carriers[where],
            )
            continue
        reason = c.reason_if_not_applied or "no reason given"
        issues.append(
            FidelityIssue(
                code="constraint_not_expressed",
                message=(
                    f"the question requires {c.describes!r}, which the "
                    f"interpretation could not express ({reason})"
                ),
            )
        )

    # ── 2. Claims of application, checked against the structure ────────
    applied = [c for c in declared if c.applied]
    needed: dict[str, int] = {}
    for c in applied:
        where = _normalise_where(c.where)
        if where not in carriers:
            # An unrecognised or missing "where" cannot be checked against
            # anything, so it is not counted against the spec. The claim is
            # recorded in the receipt; it just proves nothing here.
            continue
        needed[where] = needed.get(where, 0) + 1

    for where, want in sorted(needed.items()):
        have = carriers.get(where, 0)
        if have < want:
            issues.append(
                FidelityIssue(
                    code="constraint_lost",
                    message=(
                        f"{want} condition(s) were reported as applied in "
                        f"{where}, but the interpretation contains {have}; "
                        f"at least one condition the question imposes is "
                        f"missing from the query that would have run"
                    ),
                )
            )

    carried = sum(min(want, carriers.get(w, 0)) for w, want in needed.items())
    result = FidelityResult(
        ok=not issues,
        issues=tuple(issues),
        declared_n=len(declared),
        carried_n=carried,
    )
    if issues:
        logger.info(
            "aggregate fidelity: spec %s refused — %s",
            spec.spec_hash(), ", ".join(i.code for i in issues),
        )
    return result


def check_single_output(generation: Optional[Any]) -> Optional[FidelityIssue]:
    """Whether the question asked for more figures than one spec can carry.

    An AggregateSpec holds ONE measure over ONE population. A question
    asking for two numbers is not representable, and the observed behaviour
    was to answer the first and say nothing about the second — a partial
    answer indistinguishable from a complete one.

    NO LONGER THE PRODUCTION PATH. Multi-part questions are now executed
    part by part — `multi.plan_parts` decides whether the parts can be run
    and `orchestrator.answer` runs them — so this is not what refuses them
    any more. It is kept because it states the invariant a single
    `AggregateSpec` obeys, and a caller holding one answer still needs a
    way to ask whether the question wanted more than one.

    Use `multi.plan_parts` to decide whether to DECOMPOSE. Use this to
    decide whether one answer is the whole answer.
    """
    outputs = tuple(getattr(generation, "requested_outputs", ()) or ())
    if len(outputs) <= 1:
        return None
    listed = "; ".join(repr(o) for o in outputs)
    return FidelityIssue(
        code="multiple_outputs_requested",
        message=(
            f"the question asks for {len(outputs)} separate figures "
            f"({listed}), and one aggregate computes one figure. Ask them "
            f"as separate questions so each gets its own verified answer"
        ),
    )
