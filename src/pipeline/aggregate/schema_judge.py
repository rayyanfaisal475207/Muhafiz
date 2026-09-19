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
  2. A REGISTRY REPORT: for every name this plan uses, what the live schema
     holds for it, looked up for you. Lines reading NOT IN THE SCHEMA, NO
     SUCH PROPERTY or NOT A PROPERTY OF are measured facts, not opinions —
     the lookup was performed against the same data the card was built from.
     Read this FIRST. It is the evidence; the card is there so you can check
     it.
  3. A QUERY PLAN as JSON, and the traversals it walks, resolved.
  4. The QUESTION the plan claims to answer.

THE REGISTRY REPORT DOES NOT DECIDE — YOU DO. It reports what was found; the
verdict, and the violations you list, are yours. If the report and the card
appear to disagree, say so in a violation rather than picking one silently.

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

4. VALUES ARE NOT YOURS TO JUDGE. You decide whether NAMES exist, never
   whether the data happens to hold a particular value.

   The card's value lists are a TRUNCATED SAMPLE, not a closed set: at most
   six values are shown even where more exist, the list is ordered by
   frequency rather than coverage, and a property with many distinct values
   carries no list at all. A value you cannot find in a list may still be in
   the data, and its absence from the card is not evidence of anything.

   More importantly, a filter on a value that genuinely occurs nowhere is a
   LEGITIMATE query whose correct answer is zero. "Are there any unicorn
   weapons?" is answered by returning 0, and coverage reporting downstream
   says so plainly. Refusing it would destroy the system's ability to answer
   "none". Never return INVALID because of a filter VALUE.

5. DIRECTION IS TRAVERSAL, NOT STORAGE. The card draws each relationship in
   the orientation it is STORED: (Source)-[:TYPE]->(Target). A plan's
   `direction` says how that edge is WALKED:

     "out"  follows the arrow, Source to Target
     "in"   walks the SAME stored edge backwards, Target to Source

   Walking backwards is normal, supported and common. "How many cases have a
   weapon linked to them?" starts at Case and walks
   (Weapon)-[:BELONGS_TO_CASE]->(Case) with direction "in". That plan is
   VALID. Do not refuse a traversal because it runs against the arrow.

   A traversal is INVALID only when NO edge of that type connects those two
   labels in EITHER orientation.

6. YOU ARE NOT JUDGING QUALITY. Do not comment on whether the plan is the
   best interpretation, whether the measure suits the question, or whether
   it could be simpler. Another check does that. You judge one thing:
   is every name in this plan real?

WHEN IN DOUBT ABOUT A NAME, REFUSE. If you cannot find a label,
relationship or property on the card, it is absent. If you are unsure whether
a property belongs to the label the plan attaches it to, that is INVALID. The
cost of wrongly refusing is one unanswered question; the cost of wrongly
approving is a fabricated number in a case file.

THAT DOUBT APPLIES TO NAMES ONLY. It is not a licence to refuse a plan whose
names are all real because something else about it looks unusual — a filter
value you cannot see in the data, a traversal walked backwards, an
interpretation you would have made differently. Those are other checks' work.
If every name in the plan is on the card, your verdict is VALID.

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


#: How many sibling names to list when reporting one as absent. Enough to
#: show the judge what the label DOES carry, short enough that the block
#: stays a table rather than becoming a second schema card.
_SIBLINGS_SHOWN = 12


def _sorted_props(info: Any) -> list[str]:
    props = getattr(info, "properties", None) or {}
    return sorted(props)


def _resolve_names(snapshot: Any, spec: Any) -> list[str]:
    """One line per name the spec uses, and what the registry has for it.

    WHY THIS EXISTS. The judge was handed a 13,600-character schema card and
    asked to notice that one property was missing from it. Measured over 75
    runs it missed eight, every one of them the same shape: a property or a
    relationship that does not exist on the label the plan attached it to —
    `arrest_status` on Person, `name` on Person, `Person-[:CITES]->Case`. The
    name was absent from a document the judge had to search, and searching a
    long document for an absence is the task models are worst at.

    So trusted code does the LOOKUP and the judge keeps the RULING. Every
    line below is a fact from the registry, stated plainly; none of them is a
    verdict. The judge still decides, may still disagree, and is still the
    only thing that can refuse. What changes is that it now reads a short
    table instead of scanning a card.

    This does not narrow the grammar. The generator may still emit any name
    it likes; this only reports what the registry knows about the names it
    emitted.
    """
    population = getattr(spec, "population", None)
    if population is None:
        return []

    lines: list[str] = []
    seen: set[tuple] = set()

    def add(key: tuple, text: str) -> None:
        if key not in seen:
            seen.add(key)
            lines.append(text)

    # ── The measured entity ───────────────────────────────────────────
    root = getattr(population, "entity", None)
    if root:
        info = snapshot.entity(root)
        if info is None:
            known = ", ".join(sorted(snapshot.known_labels())[:_SIBLINGS_SHOWN])
            add(("label", root),
                f"  label {root!r}: NOT IN THE SCHEMA. "
                f"Labels that exist: {known}")
        else:
            add(("label", root),
                f"  label {root!r}: exists, {info.total_n} nodes, "
                f"key={info.distinct_key!r}")

    # ── distinct_key, which must identify one unit of the counted label ─
    dk = getattr(spec, "distinct_key", None)
    if dk and root:
        info = snapshot.entity(root)
        if info is not None:
            if dk == info.distinct_key:
                add(("dk", root, dk),
                    f"  distinct_key {dk!r} on {root}: this IS the measured key")
            elif dk in (getattr(info, "properties", None) or {}):
                add(("dk", root, dk),
                    f"  distinct_key {dk!r} on {root}: the property exists but "
                    f"the measured key is {info.distinct_key!r}")
            else:
                add(("dk", root, dk),
                    f"  distinct_key {dk!r} on {root}: NO SUCH PROPERTY. "
                    f"Measured key is {info.distinct_key!r}")

    # ── Traversals, each resolved in the orientation it is walked ──────
    current = root
    for hop in tuple(getattr(population, "traversals", ()) or ()):
        if hop.direction == "out":
            info = snapshot.relationship(current, hop.rel, hop.target)
            drawn = f"({current})-[:{hop.rel}]->({hop.target})"
        else:
            info = snapshot.relationship(hop.target, hop.rel, current)
            drawn = f"({hop.target})-[:{hop.rel}]->({current})"
        key = ("rel", current, hop.rel, hop.target, hop.direction)
        if info is None:
            # Name every orientation this relationship type DOES connect, so
            # an absence is shown rather than merely asserted.
            others = sorted(
                f"({r.source_label})-[:{r.rel_type}]->({r.target_label})"
                for r in snapshot.relationships.values()
                if r.rel_type == hop.rel
            )
            detail = (
                f"{hop.rel} connects: {', '.join(others[:6])}"
                if others else f"no {hop.rel} relationship exists at all"
            )
            add(key,
                f"  edge {drawn}: NOT IN THE SCHEMA. {detail}")
        else:
            add(key, f"  edge {drawn}: exists, {info.edge_n} edges")
            if hop.role_field:
                eprops = getattr(info, "properties", None) or {}
                if hop.role_field in eprops:
                    vals = snapshot.edge_property_values(
                        info.source_label, info.rel_type, info.target_label,
                        hop.role_field,
                    )
                    shown = (
                        f" values: {', '.join(sorted(vals))}"
                        if vals else " values: not enumerated"
                    )
                    add(("eprop", hop.rel, hop.role_field),
                        f"  edge property {hop.role_field!r} on that edge: "
                        f"exists.{shown}")
                else:
                    have = ", ".join(sorted(eprops)) or "none"
                    add(("eprop", hop.rel, hop.role_field),
                        f"  edge property {hop.role_field!r} on that edge: "
                        f"NOT PRESENT. That edge carries: {have}")
        current = hop.target

    # ── Field predicates, checked against the label they attach to ────
    # Attributed to the ROOT entity, not the terminal label of the walk,
    # because that is what `validator._validate_population` does — it calls
    # `_validate_predicate(snapshot, pop.entity, ...)`. Reporting a different
    # owner here would hand the judge an ownership claim the rest of the
    # system does not make, which is the exact confusion this block exists
    # to remove.
    for pred in tuple(getattr(population, "predicates", ()) or ()):
        field = getattr(pred, "field", None)
        if not field:
            continue
        info = snapshot.entity(root) if root else None
        if info is None:
            continue
        props = getattr(info, "properties", None) or {}
        key = ("field", root, field)
        if field in props:
            p = props[field]
            add(key,
                f"  property {field!r} on {root}: exists, present on "
                f"{p.present_n}/{p.total_n}")
        else:
            have = ", ".join(_sorted_props(info)[:_SIBLINGS_SHOWN]) or "none"
            add(key,
                f"  property {field!r} on {root}: NOT A PROPERTY OF "
                f"{root}. {root} carries: {have}")

    # ── value_field and group_by, same ownership question ─────────────
    for name, where in (
        (getattr(spec, "value_field", None), "value_field"),
        *[(g.field, "group_by") for g in (getattr(spec, "group_by", ()) or ())],
    ):
        if not name or not root:
            continue
        info = snapshot.entity(root)
        if info is None:
            continue
        props = getattr(info, "properties", None) or {}
        key = ("field", root, name)
        if key in seen:
            continue
        if name in props:
            add(key, f"  property {name!r} on {root} ({where}): exists")
        else:
            have = ", ".join(_sorted_props(info)[:_SIBLINGS_SHOWN]) or "none"
            add(key,
                f"  property {name!r} on {root} ({where}): NOT A PROPERTY "
                f"OF {root}. {root} carries: {have}")

    return lines


def render_evidence(snapshot: Any, spec: Any) -> str:
    """The evidence block placed directly above the plan."""
    lines = _resolve_names(snapshot, spec)
    if not lines:
        return "  (this plan names nothing that can be resolved)"
    return "\n".join(lines)


def _render_traversals(spec: Any) -> str:
    """Each hop as the concrete orientation it walks, not as a bare flag.

    The spec carries `direction` as the string "in" or "out", which means
    nothing without knowing that "in" walks a stored edge backwards. Handing
    that flag to a judge and expecting it to infer the convention is how a
    legitimate reverse traversal — "cases that have a weapon", walking
    (Weapon)-[:BELONGS_TO_CASE]->(Case) from the Case end — got refused three
    times out of three.

    Rendering the resolved shape removes the inference. The judge still
    decides whether the edge exists; it is simply no longer asked to work out
    what the plan meant first.
    """
    population = getattr(spec, "population", None)
    hops = tuple(getattr(population, "traversals", ()) or ())
    if not hops:
        return "  (none — this plan traverses no relationships)"

    lines: list[str] = []
    current = getattr(population, "entity", "?")
    for i, hop in enumerate(hops, 1):
        rel, target = hop.rel, hop.target
        if hop.direction == "out":
            drawn = f"({current})-[:{rel}]->({target})"
            walked = f"{current} -[:{rel}]-> {target}"
            note = "follows the stored arrow"
        else:
            drawn = f"({target})-[:{rel}]->({current})"
            walked = f"{current} <-[:{rel}]- {target}"
            note = (
                "walks the stored edge BACKWARDS, which is normal and "
                "supported"
            )
        lines.append(f"  hop {i}: {walked}")
        lines.append(f"          stored as {drawn}; this plan {note}")
        if hop.role_field:
            lines.append(
                f"          filtered on edge property {hop.role_field!r}"
                + (f" = {hop.role_value!r}" if hop.role_value is not None else "")
            )
        current = target
    return "\n".join(lines)


async def judge(
    spec: Any,
    *,
    schema_card: str,
    question: str,
    snapshot: Any = None,
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

    # The registry report is built only when a snapshot is supplied, so a
    # caller without one degrades to the card-only judge rather than failing.
    evidence = (
        f"REGISTRY REPORT — what the live schema holds for each name this "
        f"plan uses:\n{render_evidence(snapshot, spec)}\n\n"
        if snapshot is not None else ""
    )
    user_message = (
        f"{schema_card}\n\n"
        f"QUESTION: {question}\n\n"
        f"{evidence}"
        f"QUERY PLAN:\n{spec_json}\n\n"
        f"TRAVERSALS IN THIS PLAN, resolved to the orientation each one "
        f"walks:\n{_render_traversals(spec)}\n\n"
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
