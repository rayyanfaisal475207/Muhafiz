# -*- coding: utf-8 -*-
"""
The production query path: question in, reconciled answer out.

WHAT THIS MODULE IS. Wiring, and only wiring. Every component it calls
already existed and is called unchanged: `nl_spec.generate_spec`,
`validator.validate`, `verification_policy.decide`, the three route `run`
functions, and `reconcile.reconcile`. Nothing here re-implements a
computation, and nothing here can make a route return something the route
would not otherwise have returned.

WHAT IT DELIBERATELY DOES NOT TOUCH. `executor.run_aggregate()` is not
called, not wrapped and not modified. `shadow.py` uses it as the baseline
against which this engine is compared, so changing it would invalidate
every shadow comparison in the repository — the measurement and the thing
being measured would become the same object. Integration happens here, at
the request layer, above both.

THE ORDER IS LOAD-BEARING. Validation precedes the verification decision,
and the verification decision precedes execution:

  - Validating first means an unsound spec is refused before anything is
    spent on it, and means the policy never reasons about a spec that
    could not have run.
  - Deciding before execution is forced by cost: the AGE route is the
    expensive act being gated (41 s median, 92 s max measured), so a
    policy that read execution results would be deciding after paying.
  - Structured runs before AGE because AGE is the slow one, and a
    structured refusal makes the AGE call pointless.

WHY A PARTIAL RESULT IS MARKED. When AGE cannot compute a comparable
number - a ratio, a median, a Postgres-authoritative time window - the
answer is corroborated but not independently verified. Those are different
claims, and `AggregateAnswer.independently_verified` keeps them different
all the way to the caller. Presenting corroboration as verification would
overstate assurance, which is worse than not verifying at all.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any, Optional

from src import config
from src.pipeline.aggregate import (
    direction,
    fidelity,
    gates as gatelib,
    nl_spec,
    reconcile as rec,
    registry as reg,
    route_age,
    route_semantic,
    route_structured,
    routes,
    schema_judge,
    validator,
    verification_policy as vp,
)
from src.pipeline.aggregate.routes import AggregateRouteRequest
from src.pipeline.aggregate.spec import AggregateSpec, Scope

logger = logging.getLogger(__name__)

#: Roles permitted to ask cross-case aggregate questions. Identical to the
#: set `route_age.run` enforces; checked here too so the refusal happens
#: before an LLM call rather than after one.
_CROSS_CASE_ROLES: frozenset[str] = frozenset(
    {"supervisor", "station_admin", "platform_admin"}
)


# ══════════════════════════════════════════════════════════════════════
# Answer
# ══════════════════════════════════════════════════════════════════════
@dataclasses.dataclass(frozen=True)
class AggregateAnswer:
    """One question, answered or refused, with its full audit trail."""

    question: str
    request_id: str
    status: str  # "ANSWERED" | "REFUSED" | "CONFLICT"
    value: Any = None
    interpretation: Optional[str] = None
    grain: Optional[str] = None

    spec: Optional[AggregateSpec] = None
    spec_generation: Optional[nl_spec.SpecGeneration] = None
    validation: Any = None
    decision: Optional[vp.VerificationDecision] = None
    reconciliation: Any = None

    structured: Any = None
    age: Any = None
    semantic: Any = None

    refusal_code: Optional[str] = None
    refusal_reason: Optional[str] = None
    warnings: tuple[str, ...] = ()
    timings_ms: dict = dataclasses.field(default_factory=dict)
    #: Hop directions normalised deterministically before validation. Empty
    #: for the overwhelming majority of answers; non-empty means the spec
    #: that ran is not byte-identical to the spec that was generated.
    direction_corrections: tuple = ()
    #: What the interpretation claimed the question asks for, checked
    #: against what the spec carries. `None` when the caller supplied the
    #: spec, because then no claim was made on its behalf.
    fidelity: Any = None

    @property
    def independently_verified(self) -> bool:
        """True only when a SECOND COMPUTATION ran and agreed.

        Not "verification was requested" and not "the semantic route ran".
        Both of those can be true while no independent number exists, and
        conflating them is exactly the overstatement this flag prevents.
        """
        if self.decision is None or not self.decision.claims_independent:
            return False
        if self.age is None or not getattr(self.age, "ok", False):
            return False
        classification = getattr(self.reconciliation, "classification", None)
        # AGREEMENT only. PARTIAL_AGREEMENT and SEMANTICALLY_DIFFERENT both
        # mean the two routes did NOT make the same claim, and calling
        # either one "verified" is the overstatement this flag exists to
        # prevent.
        return classification == "AGREEMENT"

    @property
    def verification_note(self) -> str:
        """One sentence a reader can act on."""
        if self.decision is None:
            return "No verification decision was reached."
        if self.independently_verified:
            return "Independently verified: a second route computed this and agreed."
        if self.decision.level == vp.PARTIAL:
            return (
                "Not independently verified. "
                + (self.decision.partial_reason or "Corroborating evidence only.")
            )
        if self.decision.level == vp.FULL:
            return (
                "Verification was required and did not confirm this figure; "
                "see the reconciliation result."
            )
        return "Not verified: no mandatory verification trigger applied."

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "request_id": self.request_id,
            "status": self.status,
            "value": self.value,
            "interpretation": self.interpretation,
            "grain": self.grain,
            "independently_verified": self.independently_verified,
            "verification_note": self.verification_note,
            "spec": self.spec.to_dict() if self.spec is not None else None,
            "spec_hash": self.spec.spec_hash() if self.spec is not None else None,
            "generation": (
                self.spec_generation.to_dict()
                if self.spec_generation is not None else None
            ),
            "validation": {
                "verdict": getattr(self.validation, "verdict", None),
                "issues": [
                    {"code": i.code, "message": i.message}
                    for i in getattr(self.validation, "issues", ()) or ()
                ],
            } if self.validation is not None else None,
            "decision": self.decision.to_dict() if self.decision is not None else None,
            "reconciliation": (
                self.reconciliation.to_dict()
                if hasattr(self.reconciliation, "to_dict") else None
            ),
            "routes": {
                name: (r.to_dict() if r is not None else None)
                for name, r in (
                    ("structured", self.structured),
                    ("age", self.age),
                    ("semantic", self.semantic),
                )
            },
            "refusal_code": self.refusal_code,
            "refusal_reason": self.refusal_reason,
            "warnings": list(self.warnings),
            "timings_ms": self.timings_ms,
            "direction_corrections": [
                c.to_dict() for c in self.direction_corrections
            ],
            "fidelity": (
                self.fidelity.to_dict()
                if hasattr(self.fidelity, "to_dict") else None
            ),
        }


#: The validator issues that answer "does this name exist in the schema?" —
#: exactly the question `schema_judge` is given in `aggregate_v2`. Suppressed
#: in that mode so the judge is measured instead of shadowed by the lookup it
#: is being compared against. Every other issue code still applies.
_SCHEMA_EXISTENCE_CODES: frozenset[str] = frozenset({
    "unknown_entity",
    "unknown_field",
    "unknown_relationship",
    "unknown_edge_property",
    "unknown_edge_property_value",
    "wrong_distinct_key",
    "field_never_populated",
})


def _without_schema_existence_issues(result: Any) -> Any:
    """`result` minus the issues the judge was asked to find instead.

    Returns the result unchanged when it raised none of them, so the common
    path allocates nothing.
    """
    issues = tuple(getattr(result, "issues", ()) or ())
    kept = tuple(i for i in issues if i.code not in _SCHEMA_EXISTENCE_CODES)
    if len(kept) == len(issues):
        return result
    dropped = [i.code for i in issues if i.code in _SCHEMA_EXISTENCE_CODES]
    logger.info(
        "aggregate_v2: schema-existence issue(s) %s left to the judge, "
        "which passed the spec", dropped,
    )
    return dataclasses.replace(
        result,
        verdict="EXECUTABLE" if not kept else result.verdict,
        issues=kept,
    )


def _refused(
    question: str,
    request_id: str,
    code: str,
    reason: str,
    **extra: Any,
) -> AggregateAnswer:
    return AggregateAnswer(
        question=question,
        request_id=request_id,
        status="REFUSED",
        refusal_code=code,
        refusal_reason=reason,
        **extra,
    )


# ══════════════════════════════════════════════════════════════════════
# The path
# ══════════════════════════════════════════════════════════════════════
async def answer_question(
    snapshot: reg.RegistrySnapshot,
    question: str,
    scope: Scope,
    *,
    schema_card: Optional[str] = None,
    spec: Optional[AggregateSpec] = None,
    gate_profile_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> AggregateAnswer:
    """Run one question through the full path.

    `spec` lets a caller supply a pre-built spec and skip generation. That
    is how the tests exercise the policy and orchestration deterministically
    without an LLM in the loop, and how the evaluation harness would reuse
    this path with its own corpus specs.
    """
    request = AggregateRouteRequest(question=question, scope=scope, spec=spec)
    rid = request_id or request.request_id
    timings: dict = {}

    # ── Scope gate. Before any model call: an unauthorised caller should
    # not cause an LLM invocation, let alone a database read.
    role = getattr(scope, "user_role", None)
    if role not in _CROSS_CASE_ROLES:
        return _refused(
            question, rid, "scope_denied",
            f"Cross-case aggregate questions require supervisor role or "
            f"higher; caller role is {role!r}.",
        )

    # ── 1. NL -> AggregateSpec ────────────────────────────────────────
    generation: Optional[nl_spec.SpecGeneration] = None
    if spec is None:
        started = time.perf_counter()
        try:
            generation = await nl_spec.generate_spec(
                snapshot, question, scope, schema_card=schema_card
            )
        except nl_spec.SpecGenerationError as exc:
            timings["generation"] = (time.perf_counter() - started) * 1000.0
            return _refused(
                question, rid, "spec_generation_failed", str(exc),
                timings_ms=timings,
            )
        except Exception as exc:  # noqa: BLE001 — model/transport failure
            timings["generation"] = (time.perf_counter() - started) * 1000.0
            return _refused(
                question, rid, "spec_generation_error",
                f"Could not interpret the question: {exc}",
                timings_ms=timings,
            )
        timings["generation"] = (time.perf_counter() - started) * 1000.0
        spec = generation.spec

    # ── 1b. Deterministic direction reconciliation ────────────────────
    # Before validation, because it either hands validation a spec that
    # resolves or changes nothing at all. Corrections are recorded and
    # surfaced as warnings: a spec that was executed must never be
    # silently different from the spec that was generated.
    spec, direction_corrections = direction.reconcile_directions(snapshot, spec)

    # ── 2. Validation ─────────────────────────────────────────────────
    # `aggregate_v2` is an EXPERIMENT (see config.AGGREGATE_ENGINE_V2): the
    # deterministic schema check below is replaced by an LLM judge shown the
    # same schema card. Only that one check changes, so a corpus run in v1
    # and v2 differs by one variable and the comparison means something.
    #
    # The judge fails CLOSED — an error refuses — because an experiment
    # whose failure mode is "serve it anyway" measures the transport rather
    # than the judgement.
    if config.AGGREGATE_ENGINE_MODE == config.AGGREGATE_ENGINE_V2:
        started = time.perf_counter()
        verdict = await schema_judge.judge(
            spec, schema_card=schema_card or "", question=question,
            snapshot=snapshot,
        )
        timings["schema_judge"] = (time.perf_counter() - started) * 1000.0
        if not verdict.ok:
            return _refused(
                question, rid, "schema_judge_refused",
                f"The question was interpreted as a query naming something "
                f"the schema does not contain ({verdict.message}).",
                spec=spec, spec_generation=generation,
                timings_ms=timings,
                warnings=tuple(
                    f"Interpretation adjusted: {c.message}"
                    for c in direction_corrections
                ),
            )
        # The judge has now ANSWERED the schema-existence question, so the
        # deterministic answer to that same question is suppressed —
        # otherwise v2 would be "judge AND lookup", the lookup would catch
        # everything it catches in v1, and the run would measure nothing.
        #
        # Every other deterministic check still runs: grain rules,
        # distinct-key identity, fanout, time-window authority, result
        # shape. Swapping one check is the experiment; removing the rest
        # would be a different system, not a comparison.
        validation = _without_schema_existence_issues(
            validator.validate(snapshot, spec)
        )
    else:
        validation = validator.validate(snapshot, spec)
    if not validation.ok:
        detail = "; ".join(
            f"{i.code}: {i.message}" for i in validation.issues
        ) or "no detail"
        return _refused(
            question, rid, "spec_invalid",
            f"The question was interpreted as a query that cannot be run "
            f"safely ({detail}).",
            spec=spec, spec_generation=generation, validation=validation,
            timings_ms=timings,
            warnings=tuple(
                f"Interpretation adjusted: {c.message}"
                for c in direction_corrections
            ),
        )

    # ── 2b. Spec fidelity ─────────────────────────────────────────────
    # After validation, because a spec that cannot run safely should be
    # reported as unsafe rather than as unfaithful, and before execution,
    # because the point is to never compute an answer to a question that
    # was not asked. Both checks are skipped when the caller supplied the
    # spec, since no declaration was made on its behalf.
    multi = fidelity.check_single_output(generation)
    if multi is not None:
        return _refused(
            question, rid, "multiple_outputs_requested", multi.message,
            spec=spec, spec_generation=generation, validation=validation,
            timings_ms=timings,
        )

    faithful = fidelity.check(spec, generation)
    if not faithful.ok:
        detail = "; ".join(i.message for i in faithful.issues)
        return _refused(
            question, rid, "constraint_lost",
            f"The question was interpreted in a way that drops part of what "
            f"it asks for, so answering would report a broader figure than "
            f"the question requests ({detail}).",
            spec=spec, spec_generation=generation, validation=validation,
            timings_ms=timings, fidelity=faithful,
        )

    # ── 3. Verification decision ──────────────────────────────────────
    # Requirements and gate selection are derived once here and passed to
    # both the policy and the structured route, so the policy reasons over
    # exactly what the route will enforce rather than a second derivation
    # that could differ.
    requirements = gatelib.requirements_from_spec(spec, snapshot)
    selection = gatelib.validate_selection(gate_profile_id, requirements)

    decision = vp.decide(
        spec,
        snapshot=snapshot,
        requirements=requirements,
        selection=selection,
        llm_recommendation=(
            generation.verification_recommendation if generation else None
        ),
        llm_rationale=(
            generation.verification_rationale if generation else ""
        ),
    )

    # ── 4. Structured execution ───────────────────────────────────────
    started = time.perf_counter()
    structured = await route_structured.run(
        snapshot, request, spec=spec, gate_profile_id=gate_profile_id
    )
    timings["structured"] = (time.perf_counter() - started) * 1000.0

    # ── 5. Verification routes, only if the decision calls for them ───
    age = None
    if decision.run_age:
        started = time.perf_counter()
        try:
            age = await route_age.run(snapshot, request, schema_card=schema_card)
        except Exception as exc:  # noqa: BLE001 — a verification route failing
            # must not take down an answer the structured route produced.
            age = routes.refusal(
                routes.ROUTE_AGE, "age_route_error", str(exc),
                status=routes.EXECUTION_ERROR,
            )
        timings["age"] = (time.perf_counter() - started) * 1000.0

    semantic = None
    if decision.run_semantic:
        started = time.perf_counter()
        try:
            semantic = await route_semantic.run(snapshot, request)
        except Exception as exc:  # noqa: BLE001
            semantic = routes.refusal(
                routes.ROUTE_SEMANTIC, "semantic_route_error", str(exc),
                status=routes.EXECUTION_ERROR,
            )
        timings["semantic"] = (time.perf_counter() - started) * 1000.0

    # ── 6. Reconciliation ─────────────────────────────────────────────
    reconciliation = rec.reconcile(structured, age, semantic, snapshot=snapshot)

    return _assemble(
        question=question,
        request_id=rid,
        spec=spec,
        generation=generation,
        validation=validation,
        decision=decision,
        structured=structured,
        age=age,
        semantic=semantic,
        reconciliation=reconciliation,
        timings=timings,
        direction_corrections=direction_corrections,
        fidelity_result=faithful,
    )


def _assemble(
    *,
    question: str,
    request_id: str,
    spec: AggregateSpec,
    generation: Optional[nl_spec.SpecGeneration],
    validation: Any,
    decision: vp.VerificationDecision,
    structured: Any,
    age: Any,
    semantic: Any,
    reconciliation: Any,
    timings: dict,
    direction_corrections: tuple = (),
    fidelity_result: Any = None,
) -> AggregateAnswer:
    """Turn route results into one answer. No computation happens here.

    The value served is the one RECONCILIATION settled on. This function
    never picks between two disagreeing routes — on CONFLICT reconciliation
    returns no value, and that absence is carried through rather than
    repaired by preferring a route. Choosing a winner here would undo the
    guarantee the whole three-way design exists to provide.
    """
    classification = getattr(reconciliation, "classification", None)
    warnings: list[str] = list(getattr(structured, "warnings", ()) or ())
    # Listed FIRST: a reader deciding whether to trust this number needs to
    # know the executed spec differs from the generated one before reading
    # anything else about it.
    warnings = [
        f"Interpretation adjusted: {c.message}" for c in direction_corrections
    ] + warnings

    # A generation that needed more than one model call used a corrected
    # prompt on the later ones, which is the measured condition under which
    # repeating the same question is most likely to produce a different
    # interpretation. Saying so is the difference between a reproducible
    # answer and one that merely looks reproducible.
    prov = getattr(generation, "provenance", None)
    if prov is not None and getattr(prov, "retried", False):
        warnings.insert(
            0,
            f"Interpretation required {prov.attempts} model attempts; a "
            f"repeat of this question may interpret it differently.",
        )

    common = {
        "spec": spec,
        "spec_generation": generation,
        "validation": validation,
        "decision": decision,
        "reconciliation": reconciliation,
        "structured": structured,
        "age": age,
        "semantic": semantic,
        "timings_ms": timings,
        "direction_corrections": direction_corrections,
        "fidelity": fidelity_result,
    }

    if classification == "CONFLICT":
        return AggregateAnswer(
            question=question,
            request_id=request_id,
            status="CONFLICT",
            refusal_code="routes_disagree",
            refusal_reason=(
                getattr(reconciliation, "explanation", None)
                or "The routes disagreed and no value can be served."
            ),
            warnings=tuple(warnings),
            **common,
        )

    if classification == "REFUSED" or not getattr(structured, "ok", False):
        return AggregateAnswer(
            question=question,
            request_id=request_id,
            status="REFUSED",
            refusal_code=(
                getattr(structured, "refusal_code", None) or "no_value"
            ),
            refusal_reason=(
                getattr(structured, "refusal_reason", None)
                or getattr(reconciliation, "explanation", None)
                or "No route produced a value."
            ),
            warnings=tuple(warnings),
            **common,
        )

    numeric = getattr(structured, "numeric", None)
    value = getattr(reconciliation, "value", None)
    if value is None and numeric is not None:
        value = numeric.value

    if decision.level == vp.PARTIAL:
        warnings.append(
            "Not independently verified: "
            + (decision.partial_reason or "no comparable second computation.")
        )
    elif decision.level == vp.NONE:
        warnings.append(
            "Not verified: no mandatory verification trigger applied to this "
            "question."
        )

    return AggregateAnswer(
        question=question,
        request_id=request_id,
        status="ANSWERED",
        value=value,
        interpretation=getattr(numeric, "interpretation", None),
        grain=getattr(numeric, "grain", None),
        warnings=tuple(warnings),
        **common,
    )
