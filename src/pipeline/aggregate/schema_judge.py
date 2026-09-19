# -*- coding: utf-8 -*-
"""
LLM-as-judge for schema adherence — the `aggregate_v2` experiment.

WHAT THIS IS FOR. `validator.py` answers "does this field exist?" with a
registry lookup: deterministic, free, and correct by construction. This
module answers the same question by showing a model the schema card and the
spec and asking it, under a deliberately severe prompt, whether the spec
names anything the card does not contain.

It exists so the choice between those two can be MEASURED rather than
argued. Run the blind corpus in `legacy`, `aggregate_v1` and `aggregate_v2`
and compare the one number that matters — how many answers were silently
wrong. Legacy measured 5; v1 measured 0.

WHAT IS ALREADY KNOWN, AND SHOULD SHAPE HOW A RESULT IS READ.

  1. The generator prompt ALREADY contains "NAME ONLY WHAT THE CARD SHOWS …
     Do not invent a property because the question implies one should
     exist." Asked for persons with a criminal risk score above 80, the
     model emitted `criminal_risk_score` in two runs of three anyway — a
     field on no label in the graph — and in the third run dropped the
     filter entirely and answered 208, every person in the database.

  2. So this judge is not a stricter instruction. The instruction exists.
     It is a SECOND LOOK by the same model family over the same evidence,
     and the open question is whether a second look catches what the first
     missed or repeats it. Correlated reviewers do not catch correlated
     errors, and that is precisely what this measures.

  3. The judge sees a well-FORMED spec in both failure modes above. A
     spec naming a plausible-sounding property is syntactically perfect;
     a spec that dropped a filter is simpler and cleaner than the correct
     one. Neither is detectable from the spec's shape — only against the
     schema, which is why v1 uses a lookup.

WHAT IT DOES NOT DO. It does not replace `direction.py`, `fidelity.py`,
gate profiles, coverage or reconciliation. Only the schema-existence check
is swapped, so the comparison isolates one variable. Everything else about
v2 is v1.

FAIL-CLOSED. A judge that errors, times out or returns unparseable output
REFUSES the spec. An experiment whose failure mode is "serve the answer
anyway" would measure the transport, not the judgement.
"""
from __future__ import annotations

import dataclasses
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

#: Output budget. The verdict is a small object; the schema card and spec
#: dominate the request, not the reply.
_MAX_TOKENS = 700


_SYSTEM = """You audit one structured query plan against one database schema.

You are the last check before this plan runs against a live police evidence
database and its answer is put in front of an investigator. A number that is
confidently wrong is worse than no number at all, because someone will act
on it.

YOU ARE GIVEN:
  1. A SCHEMA CARD: every node label, every property on those labels, every
     relationship, and the properties carried on those relationships. It was
     measured from the live database. It is COMPLETE. What is absent from
     the card does not exist.
  2. A QUERY PLAN as JSON.
  3. The QUESTION the plan claims to answer.

YOUR ONE JOB: decide whether the plan names ANYTHING the schema card does
not contain.

THE RULES, IN ORDER OF PRECEDENCE:

1. ABSENT MEANS ABSENT. If the plan names a label, a relationship type, a
   property or an edge property that does not appear on the card, the plan
   is INVALID. There is no exception. Not "it is probably stored elsewhere",
   not "the name is close enough to X", not "a police database would surely
   have this". If it is not on the card, it does not exist.

2. DO NOT REASON ABOUT PLAUSIBILITY. A field like `criminal_risk_score`,
   `priority_level` or `threat_rating` sounds exactly like something this
   database would hold. That feeling is not evidence. Check the card. If it
   is not listed under that label, the plan is INVALID and you say so.

3. CHECK THE OWNER, NOT JUST THE NAME. A property that exists on one label
   does not exist on another. `role` on an INVOLVED_IN edge is not `role` on
   a Person node. A plan filtering a node on an edge property is INVALID,
   and vice versa.

4. CHECK VALUES WHERE THE CARD LISTS THEM. Where the card shows the values a
   property takes, a plan filtering on a value outside that set is INVALID.
   A value that matches nothing returns a clean, plausible, entirely wrong
   zero.

5. VERIFY THE DIRECTION OF EVERY RELATIONSHIP. The card draws each one as
   (Source)-[:TYPE]->(Target). A plan traversing it between two labels it
   does not connect, in a direction the card does not draw, is INVALID.

6. YOU ARE NOT JUDGING QUALITY. Do not comment on whether the plan is the
   best interpretation, whether the measure suits the question, or whether
   it could be simpler. Another check does that. You judge one thing:
   is every name in this plan real?

WHEN IN DOUBT, REFUSE. If you cannot find a name on the card, it is absent.
If you are unsure whether a property belongs to the label the plan attaches
it to, that is INVALID. The cost of wrongly refusing is one unanswered
question. The cost of wrongly approving is a fabricated number in a case
file.

Return ONLY this JSON object, no prose and no markdown fence:

{
  "verdict": "VALID" | "INVALID",
  "violations": [
    {"names": "<the exact label/relationship/property/value in the plan>",
     "where": "<the field of the plan carrying it>",
     "why": "<why the card does not contain it>"}
  ]
}

`violations` is empty when and only when the verdict is VALID."""


@dataclasses.dataclass(frozen=True)
class JudgeVerdict:
    """What the judge concluded, and why."""

    ok: bool
    violations: tuple[dict, ...] = ()
    #: Set when the judge could not be consulted at all. Distinct from a
    #: refusal: one is "the plan is bad", the other is "nothing checked it".
    error: Optional[str] = None

    @property
    def message(self) -> str:
        if self.error:
            return f"schema judge unavailable: {self.error}"
        if self.ok:
            return "schema judge found no absent names"
        parts = [
            f"{v.get('names')!r} in {v.get('where')}: {v.get('why')}"
            for v in self.violations
        ] or ["no detail given"]
        return "; ".join(parts)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "violations": list(self.violations),
            "error": self.error,
        }


def _parse(payload: Any) -> Optional[JudgeVerdict]:
    if not isinstance(payload, dict):
        return None
    verdict = str(payload.get("verdict") or "").strip().upper()
    if verdict not in ("VALID", "INVALID"):
        return None
    raw = payload.get("violations")
    violations = tuple(v for v in raw if isinstance(v, dict)) if isinstance(raw, list) else ()
    # A model that says INVALID without naming anything is still a refusal:
    # the verdict is the decision, the list is the explanation.
    return JudgeVerdict(ok=(verdict == "VALID"), violations=violations)


def _validate_payload(payload: Any) -> bool:
    return _parse(payload) is not None


async def judge(
    spec: Any,
    *,
    schema_card: str,
    question: str,
    _call_llm_json: Any = None,
) -> JudgeVerdict:
    """Ask the judge whether `spec` names anything absent from the schema.

    Fails CLOSED. A transport error, a timeout or an unparseable reply
    returns `ok=False` with `error` set, so an experiment cannot accidentally
    measure "the judge was down" as "the judge approved".
    """
    import json as _json

    if _call_llm_json is None:
        from src.pipeline.json_extract import call_llm_json as _call_llm_json

    try:
        spec_json = _json.dumps(spec.to_dict(), ensure_ascii=False, indent=1)
    except Exception as exc:  # noqa: BLE001 — a spec that will not serialise
        return JudgeVerdict(ok=False, error=f"spec not serialisable: {exc}")

    user_message = (
        f"{schema_card}\n\n"
        f"QUESTION: {question}\n\n"
        f"QUERY PLAN:\n{spec_json}\n\n"
        f"Return the JSON verdict."
    )

    try:
        payload, note = await _call_llm_json(
            _SYSTEM,
            user_message,
            max_tokens=_MAX_TOKENS,
            temperature=0.0,
            validate=_validate_payload,
            schema_hint="verdict, violations",
        )
    except Exception as exc:  # noqa: BLE001 — model/transport failure
        logger.warning("aggregate schema judge: call failed: %s", exc)
        return JudgeVerdict(ok=False, error=str(exc))

    parsed = _parse(payload)
    if parsed is None:
        return JudgeVerdict(
            ok=False, error=f"unusable verdict ({note or 'no detail'})"
        )

    if not parsed.ok:
        logger.info(
            "aggregate schema judge: spec %s refused — %s",
            spec.spec_hash(), parsed.message,
        )
    return parsed
