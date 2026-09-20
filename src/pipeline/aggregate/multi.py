# -*- coding: utf-8 -*-
"""
Multi-part questions: N questions, each answered in full, presented together.

THE PROBLEM. `AggregateSpec` holds one measure over one population, so
"how many witnesses are there, and how many victims?" is not representable
as a spec. The legacy engine answered the first half and presented it as
the whole (QA-011 returned 76, silently dropping the second figure). The
interim fix refused, which was honest but answered nothing.

THE SHAPE OF THE FIX. The model already decomposes these correctly — the
decomposition has been produced on every run and used only to justify the
refusal. So nothing here parses a question, splits on a conjunction, or
recognises a question shape. It takes the decomposition the model produced
and EXECUTES each part through the ordinary path.

WHY EACH PART RUNS THE WHOLE PATH. It would be cheaper to compute several
values inside one answer. It would also be unsafe. `reconcile` is scalar
throughout: `check_invariants` returns no violations for a non-scalar
value, and `_values_match` compares floats. An answer carrying a list of
values would skip invariant checking entirely while still reporting as
reconciled — negative counts and out-of-range percentages would pass. So
each part is its own `AggregateAnswer`, keeping its own validation, its own
schema judgement, its own verification decision and its own reconciliation.
`MultiPartAnswer` sits ABOVE those; it never replaces one.

THE TWO RULES THIS MODULE EXISTS TO ENFORCE.

  1. A part that failed is NEVER dropped. Serving only the parts that
     worked, with no sign that others were asked, is the same
     silent-substitution failure in a new costume — and it is the one this
     engine was built to prevent.

  2. A part's assurance belongs to THAT part. A verified figure beside an
     unverified one must stay distinguishable; one verification note
     covering both would launder the weaker claim.

WHAT IS NOT HERE. No entity, relationship, property, value or question
appears in this file. The only fixed quantities are structural — how many
parts are allowed, and the fact that a part is itself a question.
"""
from __future__ import annotations

import dataclasses
import logging
import time
from typing import Any, Optional

from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate.nl_spec import RequestedOutput, SpecGeneration
from src.pipeline.aggregate.spec import Scope

logger = logging.getLogger(__name__)

#: How many parts one question may be split into.
#:
#: Each part is a full pipeline pass — a generation, a validation, a schema
#: judgement and up to two route executions — so cost is linear and a
#: question that decomposed into eight parts would take minutes and
#: exhaust the model backend this project has already watched fall over
#: under load. Beyond the cap the question is refused rather than
#: truncated: answering 3 of 8 figures and saying nothing about the rest
#: is precisely the partial answer this module exists to prevent.
MAX_PARTS: int = 3

#: Marks the warning `answer_question` puts on part 1 when a question has
#: more parts than the single-answer entry point can return.
#:
#: WHY A SHARED CONSTANT. That warning is TRUE from `answer_question`,
#: whose contract is one answer, and FALSE inside a `MultiPartAnswer`,
#: where the other parts were in fact answered. So it has to be removed on
#: the way in, and a renderer matching on prose the orchestrator writes
#: would go stale the first time either side was reworded — leaving a
#: correct multi-part answer carrying a warning saying it is incomplete.
PARTIAL_WARNING_PREFIX: str = "This answers only the first of "


def _without_partial_warning(answer: Any) -> Any:
    """Part 1 with the single-answer caveat dropped, since it no longer holds."""
    warnings = tuple(getattr(answer, "warnings", ()) or ())
    kept = tuple(w for w in warnings if not w.startswith(PARTIAL_WARNING_PREFIX))
    if len(kept) == len(warnings):
        return answer
    try:
        return dataclasses.replace(answer, warnings=kept)
    except Exception:  # noqa: BLE001 - a caller's stand-in may not be a dataclass
        return answer


# ══════════════════════════════════════════════════════════════════════
# Result
# ══════════════════════════════════════════════════════════════════════
@dataclasses.dataclass(frozen=True)
class PartAnswer:
    """One part of a multi-part question, with the label it was asked under."""

    label: str
    question: str
    #: The part's own `AggregateAnswer`. Never None: a part that could not
    #: even be attempted is recorded as a REFUSED answer, so that every
    #: part the model asked for has a row here and rule 1 holds by
    #: construction rather than by the renderer remembering to check.
    answer: Any

    @property
    def answered(self) -> bool:
        return getattr(self.answer, "status", None) == "ANSWERED"

    def to_dict(self) -> dict:
        return {
            "label": self.label,
            "question": self.question,
            "answered": self.answered,
            "answer": (
                self.answer.to_dict()
                if hasattr(self.answer, "to_dict") else None
            ),
        }


@dataclasses.dataclass(frozen=True)
class MultiPartAnswer:
    """Several questions, answered independently, reported together.

    Deliberately NOT an `AggregateAnswer`. It has no single `value`, and
    giving it one — the first part's, or a list — is how a caller written
    for the single-answer shape would render one figure as though it were
    the response to the whole question.
    """

    question: str
    request_id: str
    parts: tuple[PartAnswer, ...]
    timings_ms: dict = dataclasses.field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    @property
    def status(self) -> str:
        """ANSWERED when every part was, PARTIAL when some were, else REFUSED.

        PARTIAL is its own status rather than being folded into ANSWERED.
        A caller that treats this as answered would be claiming figures it
        does not have.
        """
        answered = sum(1 for p in self.parts if p.answered)
        if answered == len(self.parts) and answered:
            return "ANSWERED"
        return "PARTIAL" if answered else "REFUSED"

    @property
    def answered_parts(self) -> tuple[PartAnswer, ...]:
        return tuple(p for p in self.parts if p.answered)

    def to_dict(self) -> dict:
        return {
            "question": self.question,
            "request_id": self.request_id,
            "status": self.status,
            "multi_part": True,
            "part_count": len(self.parts),
            "answered_count": len(self.answered_parts),
            "parts": [p.to_dict() for p in self.parts],
            "timings_ms": self.timings_ms,
            "warnings": list(self.warnings),
        }


# WHY THERE IS NO SUB-SPEC DERIVATION HERE. The obvious optimisation is
# to build part 2's spec from part 1's with `dataclasses.replace`, the way
# `compile_ratio` splits a ratio, and save a generation call. It does not
# work: to know that two parts differ ONLY in population you must already
# have part 2's spec, which is the call it would save. So every part after
# the first is generated from its own question text, which is also what
# lets a part differ from its siblings in grain or measure — "how many
# accused, and which districts" is a count beside a breakdown.


# ══════════════════════════════════════════════════════════════════════
# Execution
# ══════════════════════════════════════════════════════════════════════
def plan_parts(
    generation: Optional[SpecGeneration],
) -> tuple[tuple[RequestedOutput, ...], Optional[str]]:
    """The parts to run, or a reason not to run any.

    Returns `((), None)` for the ordinary single-output question, which is
    the signal to take the unchanged single-answer path. A returned reason
    means the question decomposes but cannot be executed as parts, and the
    caller refuses with it — never falls back to answering part 1 alone.
    """
    outputs = tuple(getattr(generation, "requested_outputs", ()) or ())
    if len(outputs) <= 1:
        return (), None

    # A caller-supplied generation may still hold bare strings; normalise
    # so this function has one shape to reason about.
    parts = tuple(
        o if isinstance(o, RequestedOutput) else RequestedOutput(asks="", label=str(o))
        for o in outputs
    )

    if len(parts) > MAX_PARTS:
        listed = "; ".join(p.label or p.asks for p in parts)
        return parts, (
            f"the question asks for {len(parts)} separate figures "
            f"({listed}), and at most {MAX_PARTS} can be answered as one "
            f"request. Ask them as separate questions so each gets its own "
            f"verified answer"
        )

    unanswerable = [p for p in parts if not p.answerable]
    if unanswerable:
        listed = "; ".join(p.label or "(unnamed)" for p in unanswerable)
        n = len(unanswerable)
        return parts, (
            f"the question asks for {len(parts)} separate figures, but "
            f"{n} of them ({listed}) "
            + ("was" if n == 1 else "were")
            + " not restated as a question that can be asked on its own, "
            "so it cannot be answered without guessing what it meant"
        )

    return parts, None


async def answer_multi_part(
    snapshot: reg.RegistrySnapshot,
    question: str,
    scope: Scope,
    parts: tuple[RequestedOutput, ...],
    *,
    first_answer: Any,
    request_id: str,
    schema_card: Optional[str] = None,
    gate_profile_id: Optional[str] = None,
    _answer_question: Any = None,
) -> MultiPartAnswer:
    """Run each part through the full path and collect the results.

    `first_answer` is part 1, computed by the caller from `parts[0].asks` —
    the same isolated text every other part is generated from. The caller
    does it rather than this function so that the spec, validation and
    judgement for part 1 come from the ordinary single-question path,
    unchanged and with nothing here able to influence them.

    NO PART IS PRIVILEGED. An earlier version passed through the answer the
    first pass had already produced from the WHOLE question, which saved a
    generation call and cost correctness: measured live, part 1 was refused
    for a traversal that does not exist while the same figure asked on its
    own answered correctly, and part 2 — generated from its isolated text —
    was correct in that same run.

    Parts run SEQUENTIALLY. Each part's AGE route already costs tens of
    seconds, and fanning them out multiplies peak load on the model backend
    rather than the wall clock this is measured against.

    `scope` is passed into every part unchanged. It comes from the
    authenticated session, and a part that ran under a wider scope than the
    question was asked under would be an authorisation bypass wearing a
    decomposition costume.
    """
    # `answer_question`, NOT `answer`. This is what bounds the recursion:
    # a part that the model splits again is answered as its own first
    # figure, carrying the warning that says so, rather than fanning out a
    # second time. Pointing this at `answer` would make depth unbounded and
    # MAX_PARTS a cap on breadth only.
    if _answer_question is None:
        from src.pipeline.aggregate.orchestrator import answer_question as _answer_question

    started = time.perf_counter()
    collected: list[PartAnswer] = [
        PartAnswer(
            label=parts[0].label or parts[0].asks or "part 1",
            question=parts[0].asks or question,
            answer=_without_partial_warning(first_answer),
        )
    ]

    for index, part in enumerate(parts[1:], start=2):
        label = part.label or part.asks or f"part {index}"
        try:
            answer = await _answer_question(
                snapshot,
                part.asks,
                scope,
                schema_card=schema_card,
                gate_profile_id=gate_profile_id,
                request_id=f"{request_id}-p{index}",
            )
        except Exception as exc:  # noqa: BLE001 — one part must not kill the rest
            # Recorded as a refusal rather than re-raised. The other parts
            # produced real, verified figures and discarding them would
            # serve nothing; the failure is reported in this part's own row.
            logger.warning(
                "aggregate multi-part: part %d (%s) raised — %s",
                index, label, exc,
            )
            answer = _failed_part(part.asks, f"{request_id}-p{index}", exc)
        collected.append(
            PartAnswer(label=label, question=part.asks, answer=answer)
        )

    elapsed = (time.perf_counter() - started) * 1000.0
    result = MultiPartAnswer(
        question=question,
        request_id=request_id,
        parts=tuple(collected),
        timings_ms={"multi_part_total": elapsed},
    )
    logger.info(
        "aggregate multi-part: %d part(s), %d answered, status=%s",
        len(result.parts), len(result.answered_parts), result.status,
    )
    return result


def _failed_part(question: str, request_id: str, exc: BaseException) -> Any:
    """A part that raised, expressed as an ordinary refusal.

    Imported lazily for the same reason `answer_question` is: the
    orchestrator imports this module, so importing it at module scope would
    be circular.
    """
    from src.pipeline.aggregate.orchestrator import AggregateAnswer

    return AggregateAnswer(
        question=question,
        request_id=request_id,
        status="REFUSED",
        refusal_code="part_execution_error",
        refusal_reason=f"This part could not be computed: {exc}",
    )
