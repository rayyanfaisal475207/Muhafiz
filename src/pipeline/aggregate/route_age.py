# -*- coding: utf-8 -*-
"""
The independent AGE route — a second opinion, computed a different way.

WHAT THIS ROUTE IS FOR. An independent computation path. The structured
route computes an aggregate by compiling a typed spec; this one computes it
by asking a model to INTERPRET the question into its own typed plan. When
the two agree, that agreement is evidence, because the routes share no
interpretation below the question. When they disagree, the disagreement is
the finding — and the architecture exists to make it visible rather than to
hide it.

PHASE 5D — THE MODEL NO LONGER WRITES CYPHER. Previously the model emitted
executable query text, and safety rested on inspecting that text
(`cypher_guard`) and later on running it somewhere disposable (Phase 5C's
isolated evaluator database). Both worked, and both were the wrong shape:
one is a filtering exercise against an open-ended attack surface, the other
required keeping a synchronised copy of the graph purely so untrusted text
had somewhere safe to run.

Now the model emits an `AgeQueryPlan` — a closed, typed structure with no
field capable of carrying a clause, a function name, or a fragment of query
text — and trusted code validates it against the live registry and compiles
it into read-only Cypher:

    model -> AgeQueryPlan -> validator -> compiler -> guard -> production

Mutation is not rejected here; it is UNREPRESENTABLE. There is no `cypher`
field to smuggle `DETACH DELETE` through, `extra="forbid"` rejects an
invented one, and the compiler has no mutation emitter to call.

INDEPENDENCE IS PRESERVED, DELIBERATELY. Removing the model's ability to
emit syntax must not remove its ability to reach a different semantic
answer. It still chooses labels, relationships, direction, filters, grain
and grouping — and it may choose them differently from the structured
route. The recorded Phase 4 case where this route independently traversed
`BELONGS_TO_CASE` while the structured route used `ASSIGNED_TO` produced a
CONFLICT, which is exactly the outcome this architecture exists to surface.
Nothing here repairs a plan toward what the structured route decided; an
invalid plan is refused, never rewritten.

WHAT THE MODEL IS TOLD. A schema card built from the live registry: real
labels, real property names with their measured presence, real relationship
triples with measured cardinality. Phase 0 established that most failures
came from not knowing the schema — `entity_id` absent on 2 of 13 labels,
`PoliceStation.name` not `canonical_name`. Withholding that would make this
route fail for uninteresting reasons.

WHAT THE MODEL IS NOT TOLD. The expected answer, the structured route's
spec, or any hint of what the other routes computed. The evaluation is
worthless if the routes are coupled.
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from typing import Optional

from src.pipeline.aggregate import age_compiler
from src.pipeline.aggregate import age_execution
from src.pipeline.aggregate import age_plan as ap
from src.pipeline.aggregate import age_plan_validator as apv
from src.pipeline.aggregate import registry as reg
from src.pipeline.aggregate import routes
from src.pipeline.aggregate.routes import (
    ROUTE_AGE,
    AggregateRouteRequest,
    NumericResult,
    RouteResult,
)

logger = logging.getLogger(__name__)

#: Cross-case aggregates require the same role gate the structured route
#: and `xagg.run_aggregate()` enforce. Duplicated verbatim rather than
#: imported, because a security check that silently follows another
#: module's refactor is worse than one stated twice.
_CROSS_CASE_ROLES = ("supervisor", "station-admin", "platform-admin")

_MAX_LABELS_IN_CARD = 14
#: PHASE 1: raised from 26. Ranking relationships by edge count and cutting
#: the tail hid the RARE ones — and a rare relationship is exactly what a
#: specific question is about. Measured live at the old cap: `Case-[CITES]->
#: Case` (9 edges) and `Person-[OWNS]->Weapon` (30 edges) were both off the
#: card, and both were needed. Asked about cases citing another case, the
#: planner substituted a relationship it COULD see and produced an
#: impossible `Case-[BELONGS_TO_CASE]->Case`; asked which accused own a
#: weapon, it reported that no Person-Weapon relationship existed — true of
#: the card, false of the graph, and the same failure the edge-property work
#: fixed one level down.
#:
#: A relationship line is one short row, and the schema holds 34 triples in
#: total, so the whole set costs roughly 400 characters over the old cap.
#: The number is a guard against a pathological schema, not a budget to
#: spend: showing every relationship the graph HAS is the point.
_MAX_RELS_IN_CARD = 60
#: PHASE 6: raised from 10. Measured cause of four wrong refusals — the
#: planner declined min/max/avg/sum over `Person.age` saying "the schema
#: does not contain any properties recording age information", which was
#: TRUE OF THE CARD and false of the graph: `age` ranks 15th of Person's 15
#: properties by presence (19/430), so a cap of 10 hid it. 16 is the
#: smallest cap that restores it; the card grows 6,532 -> ~7,100 chars.
_MAX_PROPS_PER_LABEL = 16

#: Properties with few enough distinct values to show examples. A model
#: cannot guess a literal it has never seen: `Weapon.license_status` is
#: `"بغیر لائسنس"` and `StructuredRecord.record_type` is
#: `"malkhana_register"`, and the planner bound plausible English guesses
#: that matched zero rows — returning 0 where truth was 30 and 45. Both
#: properties WERE on the card; only their values were missing.
_MAX_VALUE_EXAMPLES = 6
_VALUE_EXAMPLE_MAX_DISTINCT = 12

#: Edge qualifiers shown per relationship triple. Lower than the node cap
#: because the card lists up to 26 triples: measured live, the graph holds
#: 48 edge properties across 20 triples, but they are distributed very
#: unevenly — two `SAME_AS` triples carry 8 each, mostly review bookkeeping,
#: while the qualifier that actually changes an answer sits on a triple with
#: two. Ranking by presence and taking the top few keeps the useful ones and
#: stops a wide provenance-heavy edge from crowding out the rest of the card.
_MAX_EDGE_PROPS_PER_REL = 4


def build_schema_card(
    snapshot: reg.RegistrySnapshot,
    value_examples: Optional[dict[tuple[str, str], list]] = None,
) -> str:
    """A factual description of the graph, generated from measurement.

    Every figure here was measured by `build_registry()`; nothing is
    hand-asserted. Presence counts are included because they are the
    difference between "Person has an age" (false for 411 of 430 nodes) and
    a filter that silently discards most of the population.

    `value_examples` maps (label, property) -> a few real values, for
    properties with few enough distinct values to enumerate. Phase 6
    measured why this matters: the planner filtered
    `Weapon.license_status = "unlicensed"` and
    `StructuredRecord.record_type = "malkhana"` — plausible English guesses
    against data that actually holds `"بغیر لائسنس"` and
    `"malkhana_register"` — and both queries returned 0 where the truth was
    30 and 45. The property names were on the card; only their values were
    missing, so the model had no way to know. Pass
    `await collect_value_examples(snapshot)` to populate it.
    """
    examples = value_examples or {}
    lines: list[str] = ["NODE LABELS (with measured property presence):"]
    for label, info in sorted(
        snapshot.entities.items(), key=lambda kv: -kv[1].total_n
    )[:_MAX_LABELS_IN_CARD]:
        key = info.distinct_key or "(no unique key — cannot be counted DISTINCT)"
        tomb = (
            f", {info.tombstoned_n} are merge tombstones (merged_into IS NOT NULL)"
            if info.has_tombstones else ""
        )
        lines.append(f"  ({label})  {info.total_n} nodes, key={key}{tomb}")
        props = sorted(
            info.properties.values(), key=lambda p: -p.present_n
        )[:_MAX_PROPS_PER_LABEL]
        for p in props:
            # The bare name, for the same reason the edge properties below
            # use one: whatever this card writes as a name gets copied into
            # a spec verbatim. The leading-dot form was observed arriving
            # back as distinct_key='.cnic', which the validator then refused
            # for naming a property the label does not have. A card is an
            # input to a machine, not a syntax illustration.
            line = f"      property {p.name}  present on {p.present_n}/{p.total_n}"
            ex = examples.get((label, p.name))
            if ex:
                shown = ", ".join(repr(v) for v in ex[:_MAX_VALUE_EXAMPLES])
                more = " ..." if len(ex) > _MAX_VALUE_EXAMPLES else ""
                line += f"  values: {shown}{more}"
            lines.append(line)

    lines.append("")
    lines.append("RELATIONSHIPS (source)-[TYPE]->(target), with measured fanout:")
    for info in sorted(
        snapshot.relationships.values(), key=lambda r: -r.edge_n
    )[:_MAX_RELS_IN_CARD]:
        fan = ""
        if info.fans_in_direction("out"):
            fan = f"  FANS forward (up to {info.max_fanout} per {info.source_label})"
        elif info.fans_in_direction("in"):
            fan = f"  FANS backward (up to {info.max_reverse_fanout} per {info.target_label})"
        ver = (
            f"  VERSIONED: {info.superseded_n} superseded edge(s)"
            if info.is_versioned else ""
        )
        lines.append(
            f"  ({info.source_label})-[:{info.rel_type}]->({info.target_label})"
            f"  {info.edge_n} edges{fan}{ver}"
        )
        # Qualifiers carried ON the edge. Rendered with presence and, where
        # the value set is small enough to enumerate honestly, the real
        # values — the same treatment node properties get, for the same
        # reason: a filter the planner cannot see is a filter it cannot
        # apply, and the question then gets answered over the whole edge
        # set instead of the subset that was asked for.
        for p in sorted(
            info.properties.values(), key=lambda x: -x.present_n
        )[:_MAX_EDGE_PROPS_PER_REL]:
            # The bare property NAME, not a display form. An earlier
            # rendering wrote it as "[r].role" to mark it as edge-resident,
            # and the planner copied that decoration into the spec verbatim
            # as the field name — the card's notation became part of the
            # answer. Anything shown here must be a value that can be used
            # exactly as written.
            line = (
                f"      edge property {p.name}  "
                f"present on {p.present_n}/{p.total_n}"
            )
            ex = examples.get(
                (info.source_label, info.rel_type, info.target_label, p.name)
            )
            if ex:
                shown = ", ".join(repr(v) for v in ex[:_MAX_VALUE_EXAMPLES])
                more = " ..." if len(ex) > _MAX_VALUE_EXAMPLES else ""
                line += f"  values: {shown}{more}"
            lines.append(line)
    return "\n".join(lines)


async def collect_value_examples(
    snapshot: reg.RegistrySnapshot,
) -> dict[tuple[str, str], list]:
    """Real values for properties with few enough distinct values to list.

    Read-only, and deliberately bounded: only labels the card shows, only
    properties present on at least a few nodes, and only those whose
    distinct count is small enough to enumerate honestly. A property with
    hundreds of values (a name, an id) tells the planner nothing useful and
    would bloat the card, so it is skipped rather than sampled — a partial
    list would imply a closed set that does not exist.

    Failures are swallowed per property: a missing example makes the card
    slightly less useful, never unbuildable.
    """
    from src.graph import age_client

    out: dict[tuple[str, str], list] = {}
    for label, info in sorted(
        snapshot.entities.items(), key=lambda kv: -kv[1].total_n
    )[:_MAX_LABELS_IN_CARD]:
        for p in sorted(
            info.properties.values(), key=lambda x: -x.present_n
        )[:_MAX_PROPS_PER_LABEL]:
            if p.present_n < 3:
                continue
            try:
                rows = await age_client.execute_cypher(
                    f"MATCH (n:{label}) WHERE n.{p.name} IS NOT NULL "
                    f"RETURN n.{p.name} AS v, count(*) AS c",
                    columns=["v", "c"],
                )
            except Exception:  # noqa: BLE001 — an absent example is not fatal
                continue
            if not rows or len(rows) > _VALUE_EXAMPLE_MAX_DISTINCT:
                continue
            ranked = sorted(
                rows, key=lambda r: -(r.get("c") or 0)
            )[:_MAX_VALUE_EXAMPLES]
            vals = [r["v"] for r in ranked if r.get("v") is not None]
            if vals:
                out[(label, p.name)] = vals

    # ── Edge qualifiers, under exactly the same policy ─────────────────
    # Same reason as above, one layer out: a qualifier that lives on the
    # edge is no more guessable than one that lives on a node. The key is
    # the (source, rel, target, property) tuple so two triples sharing a
    # relationship type do not overwrite each other's values.
    for info in sorted(
        snapshot.relationships.values(), key=lambda r: -r.edge_n
    )[:_MAX_RELS_IN_CARD]:
        for p in sorted(
            info.properties.values(), key=lambda x: -x.present_n
        )[:_MAX_EDGE_PROPS_PER_REL]:
            if p.present_n < 3:
                continue
            try:
                rows = await age_client.execute_cypher(
                    f"MATCH (a:{info.source_label})-[r:{info.rel_type}]->"
                    f"(b:{info.target_label}) WHERE r.{p.name} IS NOT NULL "
                    f"RETURN r.{p.name} AS v, count(*) AS c",
                    columns=["v", "c"],
                )
            except Exception:  # noqa: BLE001 — an absent example is not fatal
                continue
            if not rows or len(rows) > _VALUE_EXAMPLE_MAX_DISTINCT:
                continue
            ranked = sorted(
                rows, key=lambda r: -(r.get("c") or 0)
            )[:_MAX_VALUE_EXAMPLES]
            vals = [r["v"] for r in ranked if r.get("v") is not None]
            if vals:
                out[(
                    info.source_label, info.rel_type, info.target_label, p.name
                )] = vals
    return out


_SYSTEM_PROMPT = """You plan ONE aggregate computation over a police evidence \
graph. You do NOT write queries — you describe, as structured JSON, which \
entities and relationships answer the question. Trusted code turns your plan \
into a safe read-only query.

Return ONLY JSON with exactly these fields:
{
  "root_alias": "a",
  "root_label": "<the entity being measured>",
  "patterns": [
    {"from_alias": "a", "from_label": "<Label>", "rel_type": "<REL_TYPE>",
     "direction": "OUT|IN|ANY", "to_alias": "b", "to_label": "<Label>",
     "optional": false}
  ],
  "filters": [
    {"alias": "a", "property": "<prop>",
     "op": "EQ|NE|GT|GTE|LT|LTE|IN|IS_NULL|IS_NOT_NULL", "value": <value>}
  ],
  "aggregate": "COUNT|COUNT_DISTINCT|SUM|AVG|MIN|MAX",
  "aggregate_alias": "a",
  "aggregate_property": "<prop or null>",
  "group_by": {"alias": "a", "property": "<prop>"} or null,
  "limit": <int or null>,
  "grain": "ENTITY|RELATIONSHIP|ROLE_PAIR|EVENT|RECORD",
  "population_description": "<what one counted unit IS, one clause>",
  "reasoning": "<one sentence>",
  "gate_profile_id": "<one id from the approved list below>"
}

Aliases are single letters: a, b, c, d. Any field not listed above is
rejected — do not invent fields, and do not include query text anywhere.

CHOOSING CORRECTLY — this is where plans usually go wrong:
- COUNT counts ROWS. Downstream of a relationship marked FANS, that counts
  relationship rows rather than entities: use COUNT_DISTINCT with the
  label's key. These are different numbers, not rounding differences.
- Use only labels, relationship types and properties the schema lists, and
  only relationship triples in the direction shown. A property a label does
  not carry reads as NULL everywhere and silently empties the population.
- Do not filter tombstones or superseded edges yourself. Trusted code
  injects `merged_into IS NULL` and `superseded_by IS NULL` wherever the
  measured schema says they apply.
- Grouping by a sparse property discards most of the population; prefer a
  property the schema shows present on nearly every node.

If the question cannot be answered from this schema, return
{"refuse": "<why>"} instead of guessing."""


def _validate_payload(payload) -> bool:
    """Cheap shape check for the retry loop. Strict parsing happens after."""
    if not isinstance(payload, dict):
        return False
    if payload.get("refuse"):
        return True
    return bool(payload.get("root_label")) and bool(payload.get("aggregate"))


def _plan_hash(plan: ap.AgeQueryPlan) -> str:
    """Stable identity for a plan, so a receipt can name what ran."""
    blob = json.dumps(plan.model_dump(mode="json"), sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:12]


async def run(
    snapshot: reg.RegistrySnapshot,
    request: AggregateRouteRequest,
    *,
    schema_card: Optional[str] = None,
) -> RouteResult:
    """Plan, validate, compile, guard, and only then execute."""
    role = getattr(request.scope, "user_role", None)
    if role not in _CROSS_CASE_ROLES:
        return routes.refusal(
            ROUTE_AGE, "scope_denied",
            f"Cross-case aggregates require supervisor role or higher; caller "
            f"role is {role!r}.",
        )

    from src.pipeline.json_extract import call_llm_json

    if schema_card is not None:
        card = schema_card
    else:
        # Value examples cost one read per candidate property, so a caller
        # running many questions (the harness) should build the card ONCE
        # and pass it in. A single ad-hoc call builds its own.
        try:
            examples = await collect_value_examples(snapshot)
        except Exception:  # noqa: BLE001 — examples are an aid, not a requirement
            examples = {}
        card = build_schema_card(snapshot, examples)
    started = time.perf_counter()

    try:
        # The gate catalogue is RENDERED from the same trusted registry the
        # runtime enforces (`gates.CATALOGUE`), never hand-copied into this
        # prompt. A hand-maintained second copy is how a prompt and its
        # enforcement drift apart without anyone noticing.
        from src.pipeline.aggregate import gates as _gates

        payload, raw = await call_llm_json(
            _SYSTEM_PROMPT,
            f"SCHEMA:\n{card}\n\n{_gates.render_catalogue_for_prompt()}\n\n"
            f"QUESTION: {request.question}",
            max_tokens=900,
            temperature=0.0,
            validate=_validate_payload,
            schema_hint=(
                '{"root_alias": "a", "root_label": "...", "patterns": [], '
                '"filters": [], "aggregate": "COUNT", "aggregate_alias": "a", '
                '"grain": "ENTITY"}'
            ),
        )
    except Exception as exc:  # noqa: BLE001 — model unreachable is not a refusal
        return routes.refusal(
            ROUTE_AGE, "generation_failed", f"Model call failed: {exc}",
            status=routes.EXECUTION_ERROR,
        )

    meta = {"generation_ms": round((time.perf_counter() - started) * 1000.0, 1)}

    if payload is None:
        return routes.refusal(
            ROUTE_AGE, "generation_unparseable",
            f"Model did not return usable JSON after retries. Raw: {raw[:200]!r}",
            status=routes.EXECUTION_ERROR,
            # PHASE 6: widened from 500. A parse failure is exactly the case
            # where the raw text is the only evidence, and a 500-char slice
            # made one look like a mid-JSON truncation when the cause could
            # not actually be determined from what was stored.
            provenance={"raw_response": raw[:4000], "raw_length": len(raw)},
        )

    if payload.get("refuse"):
        return routes.refusal(
            ROUTE_AGE, "model_declined",
            f"Model declined to answer: {payload['refuse']}",
            provenance={"model_reasoning": payload.get("reasoning", "")},
        )

    # ── Gate 1: strict parsing. ───────────────────────────────────────
    # `extra="forbid"` means an invented field — `cypher`, `raw_where`,
    # `graph_name` — is a parse error, not something quietly dropped.
    try:
        plan = ap.AgeQueryPlan.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — pydantic ValidationError et al
        return routes.refusal(
            ROUTE_AGE, "plan_unparseable",
            f"Model output is not a valid AgeQueryPlan: {exc}",
            provenance={"raw_payload": json.dumps(payload)[:500]},
        )

    provenance = {
        "planner": "independent AGE planner",
        "plan": plan.model_dump(mode="json"),
        "plan_hash": _plan_hash(plan),
        # The semantic choices, surfaced flat so a disagreement is legible
        # without reading the whole plan.
        "chosen_labels": sorted({plan.root_label} | {
            lbl for p in plan.patterns for lbl in (p.from_label, p.to_label)
        }),
        "chosen_relationships": [p.rel_type for p in plan.patterns],
        "model_interpretation": plan.aggregate.value,
        "model_grain": plan.grain.value,
        "model_reasoning": plan.reasoning,
        "population_description": plan.population_description,
        # Phase 7C: the model's gate-profile SELECTION, surfaced so the
        # harness can feed it to the structured route and so selection
        # quality is measurable. Recording it here does not grant it any
        # authority — trusted code still derives the minimum required
        # profile from the typed spec and escalates a weak choice.
        "gate_profile_id": plan.gate_profile_id,
    }

    # ── Gate 2: deterministic validation against measured schema. ─────
    validation = apv.validate_plan(plan, snapshot)
    provenance["plan_violations"] = list(validation.codes())
    provenance["validator_notes"] = list(validation.notes)
    if not validation.ok:
        # NO REPAIR. An invalid plan is refused with its reasons, never
        # rewritten toward what the structured route chose — that would
        # manufacture agreement and destroy this route's only purpose.
        return routes.refusal(
            ROUTE_AGE, "plan_invalid",
            f"Plan refused: {validation.summary()}",
            provenance=provenance,
        )

    # ── Gate 3: deterministic compilation. ────────────────────────────
    try:
        compiled = age_compiler.compile_plan(plan, snapshot, validation)
    except age_compiler.CompilerError as exc:
        return routes.refusal(
            ROUTE_AGE, "compile_failed",
            f"Validated plan could not be compiled: {exc}",
            provenance=provenance,
        )

    provenance["compiled_cypher"] = compiled.cypher
    provenance["compiled_params"] = compiled.params
    provenance["expected_columns"] = list(compiled.columns)
    provenance["injected_predicates"] = list(compiled.injected_predicates)
    provenance["compiler_notes"] = list(compiled.notes)

    # ── Defence in depth: the guard, over TRUSTED output. ─────────────
    # This should never fire. If it does, the trusted compiler emitted a
    # forbidden construction — a far more serious defect than a bad model
    # response — so it is logged CRITICAL and nothing executes.
    try:
        guard_codes = age_execution.assert_read_only(compiled, snapshot)
        provenance["guard_violations"] = list(guard_codes)
    except age_execution.CompilerDefect as exc:
        return routes.refusal(
            ROUTE_AGE, "compiler_defect",
            f"COMPILER DEFECT — compiled query rejected by the safety guard, "
            f"not executed: {exc}",
            status=routes.EXECUTION_ERROR,
            provenance=provenance,
        )

    # ── Execution against production `evidence_graph`. ────────────────
    # The target comes from trusted configuration. There is no graph= or
    # database= to supply, and `execute_compiled_age_query` accepts only a
    # compiler-produced object — a raw string has no route to production.
    provenance["executed_against"] = f"muhafiz.{age_execution.PRODUCTION_GRAPH}"

    exec_started = time.perf_counter()
    try:
        rows = await age_execution.execute_compiled_age_query(compiled)
    except age_execution.AgeExecutionError as exc:
        meta["execution_ms"] = round((time.perf_counter() - exec_started) * 1000.0, 1)
        return routes.refusal(
            ROUTE_AGE, "execution_failed",
            f"Compiled query failed at execution: {exc}",
            status=routes.EXECUTION_ERROR,
            provenance=provenance,
        )
    meta["execution_ms"] = round((time.perf_counter() - exec_started) * 1000.0, 1)
    meta["row_count"] = len(rows)
    meta["plan_hash"] = provenance["plan_hash"]

    if not rows:
        return routes.refusal(
            ROUTE_AGE, "empty_result",
            "Compiled query returned no rows, so no aggregate value could be "
            "read from it.",
            provenance=provenance, native=rows,
        )

    # Multi-row results are grouped breakdowns; single-row single-column is
    # a scalar. Anything else is a shape this route will not interpret.
    if len(rows) > 1 or len(compiled.columns) > 1:
        return RouteResult(
            route=ROUTE_AGE, status=routes.SUCCESS, result_shape="grouped",
            result=[dict(r) for r in rows],
            provenance=provenance, execution_metadata=meta, native=rows,
        )

    value = rows[0].get(compiled.columns[0])
    if not isinstance(value, (int, float)):
        return routes.refusal(
            ROUTE_AGE, "non_numeric_result",
            f"Compiled query returned {value!r}, which is not a number.",
            provenance=provenance, native=rows,
        )

    numeric = NumericResult(
        value=value,
        interpretation=plan.aggregate.value.lower(),
        grain=plan.grain.value,
        # The population states the model's own semantic choice, which is
        # what reconciliation compares before comparing values.
        population=(
            plan.population_description
            or f"{plan.root_label} via {', '.join(p.rel_type for p in plan.patterns) or 'direct match'}"
        ),
    )
    return RouteResult(
        route=ROUTE_AGE, status=routes.SUCCESS, result_shape="scalar",
        result=numeric, provenance=provenance, execution_metadata=meta,
        native=rows,
    )
