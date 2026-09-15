# -*- coding: utf-8 -*-
"""
Phase 4C — the AGE text-to-Cypher route.

WHAT THIS ROUTE IS FOR. An independent computation path. The structured
route computes an aggregate by compiling a typed spec; this one computes it
by asking a model to write Cypher. When the two agree, that agreement is
evidence, because the routes share no code below the question. When they
disagree, the disagreement is the finding — and the architecture exists to
make it visible rather than to hide it.

THE MODEL IS NOT TRUSTED. Its output is text until `cypher_guard.guard()`
says otherwise. `age_client.execute_cypher()`'s contract forbids
caller-built query text (the query is interpolated into SQL because AGE's
`cypher()` takes a `cstring` that cannot be bound), so the guard is what
earns the exception — and a query that fails any check is REFUSED, never
repaired. Repair would both defeat the security argument and destroy the
evaluation, since a repaired query no longer reports what the model did.

WHAT THE MODEL IS TOLD. A schema card built from the live registry: real
labels, real property names with their measured presence, real relationship
triples with measured cardinality, and the dialect constraints AGE actually
enforces. Phase 0 established that most generated-query failures come from
not knowing the schema rather than from writing bad Cypher — `entity_id`
absent on 2 of 13 labels, `PoliceStation.name` not `canonical_name`,
`Incident` carrying no `incident_date` at all. Withholding that would make
this route fail for uninteresting reasons.

WHAT THE MODEL IS NOT TOLD. The expected answer, the structured route's
spec, or any hint of what the other routes computed. The evaluation is
worthless if the routes are coupled.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from src.pipeline.aggregate import cypher_guard
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
_MAX_RELS_IN_CARD = 26
_MAX_PROPS_PER_LABEL = 10


def build_schema_card(snapshot: reg.RegistrySnapshot) -> str:
    """A factual description of the graph, generated from measurement.

    Every figure here was measured by `build_registry()`; nothing is
    hand-asserted. Presence counts are included because they are the
    difference between "Person has an age" (false for 411 of 430 nodes) and
    a filter that silently discards most of the population.
    """
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
            lines.append(f"      .{p.name}  present on {p.present_n}/{p.total_n}")

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
    return "\n".join(lines)


_SYSTEM_PROMPT = """You write read-only Apache AGE Cypher queries that compute \
one aggregate figure over a police evidence graph.

Return ONLY JSON: {"cypher": "<query>", "params": {}, "interpretation": \
"<count|count_distinct|sum|avg|min|max|percentage>", "grain": \
"<ENTITY|RELATIONSHIP|ROLE_PAIR|EVENT|RECORD>", "reasoning": "<one sentence>"}

HARD RULES — a query breaking any of these is rejected unexecuted:
- READ ONLY. No CREATE, MERGE, DELETE, SET, REMOVE, DROP, CALL, LOAD.
- ONE statement. No semicolons.
- Every RETURN term MUST have an explicit alias: `RETURN count(*) AS value`.
- Bind values as $params. Never inline a string literal in a comparison.

APACHE AGE IS NOT NEO4J. These are syntax errors here:
- `EXISTS { MATCH ... }`            -> not supported at all
- `WHERE NOT (a)-[:R]->(:L)`        -> not supported; count the complement
- `ORDER BY <alias>`                -> use `WITH ... RETURN ... ORDER BY`
Supported: WITH ... WHERE (as HAVING), count(DISTINCT x), toFloat(), avg(),
min(), max(), sum(), collect(), CASE, OPTIONAL MATCH.

COUNTING CORRECTLY — this is where these queries usually go wrong:
- Counting ENTITIES across a FANNING relationship requires
  count(DISTINCT x.<key>). A plain count() counts relationship rows.
  These are different numbers, not rounding differences.
- A label with merge tombstones needs `WHERE x.merged_into IS NULL`,
  or the count includes duplicate records of the same real person.
- A VERSIONED relationship needs `WHERE r.superseded_by IS NULL`, or
  historical edge versions are counted alongside current ones.
- Only use property names the schema below actually lists. A property the
  label does not carry reads as NULL on every node and silently collapses
  a grouping into one bucket.

If the question cannot be answered safely and unambiguously from this
schema, return {"cypher": "", "refuse": "<why>"} instead of guessing."""


def _validate_payload(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    if payload.get("refuse"):
        return True
    return isinstance(payload.get("cypher"), str) and bool(payload["cypher"].strip())


async def run(
    snapshot: reg.RegistrySnapshot,
    request: AggregateRouteRequest,
    *,
    schema_card: Optional[str] = None,
) -> RouteResult:
    """Generate, guard, and (only then) execute a Cypher aggregate."""
    role = getattr(request.scope, "user_role", None)
    if role not in _CROSS_CASE_ROLES:
        return routes.refusal(
            ROUTE_AGE, "scope_denied",
            f"Cross-case aggregates require supervisor role or higher; caller "
            f"role is {role!r}.",
        )

    from src.pipeline.json_extract import call_llm_json

    card = schema_card if schema_card is not None else build_schema_card(snapshot)
    started = time.perf_counter()

    try:
        payload, raw = await call_llm_json(
            _SYSTEM_PROMPT,
            f"SCHEMA:\n{card}\n\nQUESTION: {request.question}",
            max_tokens=900,
            temperature=0.0,
            validate=_validate_payload,
            schema_hint='{"cypher": "...", "params": {}, "interpretation": "count", "grain": "ENTITY"}',
        )
    except Exception as exc:  # noqa: BLE001 — model unreachable is not a refusal
        return routes.refusal(
            ROUTE_AGE, "generation_failed", f"Model call failed: {exc}",
            status=routes.EXECUTION_ERROR,
        )

    gen_ms = (time.perf_counter() - started) * 1000.0
    meta = {"generation_ms": round(gen_ms, 1)}

    if payload is None:
        return routes.refusal(
            ROUTE_AGE, "generation_unparseable",
            f"Model did not return usable JSON after retries. Raw: {raw[:200]!r}",
            status=routes.EXECUTION_ERROR,
            provenance={"raw_response": raw[:500]},
        )

    if payload.get("refuse"):
        return routes.refusal(
            ROUTE_AGE, "model_declined",
            f"Model declined to answer: {payload['refuse']}",
            provenance={"model_reasoning": payload.get("reasoning", "")},
        )

    query = (payload.get("cypher") or "").strip()
    params = payload.get("params") or {}
    provenance = {
        "generated_cypher": query,
        "generated_params": params,
        "model_interpretation": payload.get("interpretation"),
        "model_grain": payload.get("grain"),
        "model_reasoning": payload.get("reasoning", ""),
    }

    # ── The guard. Nothing executes before this passes. ───────────────
    report = cypher_guard.guard(query, snapshot)
    provenance["guard_violations"] = list(report.codes())
    if not report.safe:
        return routes.refusal(
            ROUTE_AGE,
            f"guard_{report.worst_severity().lower()}",
            f"Generated query refused: {report.summary()}",
            provenance=provenance,
        )

    columns = cypher_guard.expected_columns(query)
    if not columns:
        return routes.refusal(
            ROUTE_AGE, "unparseable_result_shape",
            "Every RETURN term must carry an explicit alias; AGE cannot infer "
            "result shape at runtime, so the query cannot be executed safely.",
            provenance=provenance,
        )
    provenance["expected_columns"] = list(columns)

    if not isinstance(params, dict) or any(
        not isinstance(k, str) for k in params
    ):
        return routes.refusal(
            ROUTE_AGE, "bad_parameters",
            "Generated parameters are not a string-keyed mapping.",
            provenance=provenance,
        )

    # ── Execution ─────────────────────────────────────────────────────
    from src.graph import age_client
    from src.database.postgres import current_cross_case, current_rls_active

    current_rls_active.set(True)
    current_cross_case.set(True)

    exec_started = time.perf_counter()
    try:
        rows = await age_client.execute_cypher(
            query, params=params, columns=list(columns)
        )
    except Exception as exc:  # noqa: BLE001
        meta["execution_ms"] = round((time.perf_counter() - exec_started) * 1000.0, 1)
        return routes.refusal(
            ROUTE_AGE, "execution_failed",
            f"Generated query failed at execution: {exc}",
            status=routes.EXECUTION_ERROR,
            provenance=provenance,
        )
    meta["execution_ms"] = round((time.perf_counter() - exec_started) * 1000.0, 1)
    meta["row_count"] = len(rows)

    if not rows:
        return routes.refusal(
            ROUTE_AGE, "empty_result",
            "Generated query returned no rows, so no aggregate value could be "
            "read from it.",
            provenance=provenance, native=rows,
        )

    # Multi-row results are grouped breakdowns; single-row single-column is
    # a scalar. Anything else is a shape this route will not interpret.
    if len(rows) > 1 or len(columns) > 1:
        return RouteResult(
            route=ROUTE_AGE, status=routes.SUCCESS, result_shape="grouped",
            result=[dict(r) for r in rows],
            provenance=provenance, execution_metadata=meta, native=rows,
        )

    value = rows[0].get(columns[0])
    if not isinstance(value, (int, float)):
        return routes.refusal(
            ROUTE_AGE, "non_numeric_result",
            f"Generated query returned {value!r}, which is not a number.",
            provenance=provenance, native=rows,
        )

    numeric = NumericResult(
        value=value,
        interpretation=str(payload.get("interpretation") or "count"),
        grain=payload.get("grain"),
        population=f"generated: {query[:120]}",
    )
    return RouteResult(
        route=ROUTE_AGE, status=routes.SUCCESS, result_shape="scalar",
        result=numeric, provenance=provenance, execution_metadata=meta,
        native=rows,
    )
