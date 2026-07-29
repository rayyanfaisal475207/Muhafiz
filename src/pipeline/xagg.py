# ============================================================
# XAGG — cross-case aggregate queries (Phase 5.4).
#
# Deliberately NOT a general text-to-SQL/Cypher system — mirrors
# sql_extractor.py's scoped, template-based approach rather than
# inventing a new paradigm. Two canned aggregate families, selected by a
# simple keyword match on the query:
#   - relational: group Case rows (police_station / investigation_status /
#     crime_category) via the existing gateway.get_cases() — no new
#     DataGateway surface needed.
#   - graph: count how many distinct cases each Vehicle/Person node
#     touches via BELONGS_TO_CASE, for "top recurring X across cases"
#     questions — one Cypher query via age_client.execute_cypher.
# Every result is inherently cross-case (that's the point of XAGG), so
# the caller labels it as a cross-case finding same as XGRAPH.
# ============================================================

from __future__ import annotations

import logging
from collections import Counter
from typing import Optional

from src.graph import age_client
from src.database.postgres import current_cross_case, current_rls_active

logger = logging.getLogger(__name__)

_VEHICLE_KEYWORDS = ("vehicle", "car", "motorcycle", "plate", "گاڑی")
_PERSON_KEYWORDS = ("person", "people", "suspect", "offender", "recidivist", "شخص", "افراد", "لوگ")
_STATION_KEYWORDS = ("station", "تھانہ")
_STATUS_KEYWORDS = ("open", "closed", "status", "pending")
_CATEGORY_KEYWORDS = ("theft", "burglary", "fraud", "category", "type of case")


def _matches_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


async def _top_recurring_nodes(label: str, limit: int = 10) -> list[dict]:
    """Count distinct cases each node of `label` touches via BELONGS_TO_CASE, descending."""
    rows = await age_client.execute_cypher(
        f"MATCH (n:{label})-[:BELONGS_TO_CASE]->(c:Case) "
        "RETURN n, c",
        columns=["n", "c"],
    )
    per_entity_cases: dict[str, set[str]] = {}
    display: dict[str, str] = {}
    for row in rows:
        n_props = (row.get("n") or {}).get("properties", {}) or {}
        c_props = (row.get("c") or {}).get("properties", {}) or {}
        entity_id = n_props.get("entity_id")
        case_id = c_props.get("case_id")
        if not entity_id or not case_id:
            continue
        per_entity_cases.setdefault(entity_id, set()).add(case_id)
        display[entity_id] = n_props.get("canonical_name") or n_props.get("plate") or n_props.get("name") or entity_id

    ranked = sorted(per_entity_cases.items(), key=lambda kv: len(kv[1]), reverse=True)
    return [
        {"entity_id": eid, "name": display.get(eid, eid), "case_count": len(cases), "case_ids": sorted(cases)}
        for eid, cases in ranked[:limit]
        if len(cases) > 1  # "recurring" — appearing in only one case isn't a cross-case pattern
    ]


async def _station_or_category_counts(gateway, query_text: str) -> dict:
    # The caller (run_aggregate) has already verified the requesting user is
    # supervisor-or-above before reaching here — a cross-case aggregate is
    # meant to cover every case platform-wide, not just ones the caller is
    # individually assigned to. gateway.get_cases() only returns everything
    # for "platform-admin"; passing anything else here (including None)
    # tries to join CaseAssignment on a non-existent user and raises.
    cases = await gateway.get_cases(user_id=None, user_role="platform-admin")

    if _matches_any(query_text, _STATUS_KEYWORDS) and _matches_any(query_text, ("open",)):
        cases = [c for c in cases if (c.get("investigation_status") or "").lower() not in ("closed", "resolved")]

    if _matches_any(query_text, _CATEGORY_KEYWORDS):
        for kw in _CATEGORY_KEYWORDS:
            if kw in query_text.lower() and kw not in _STATUS_KEYWORDS + ("category", "type of case"):
                cases = [c for c in cases if kw in (c.get("crime_category") or "").lower()]
                break

    group_field = "police_station" if _matches_any(query_text, _STATION_KEYWORDS) else "crime_category"
    counts = Counter(c.get(group_field) or "unknown" for c in cases)
    return {
        "group_by": group_field,
        "counts": [{"key": k, "count": v} for k, v in counts.most_common(15)],
        "total_cases_considered": len(cases),
    }


async def run_aggregate(
    query_text: str,
    target_entity: Optional[str],
    gateway,
    user_id: Optional[str] = None,
    user_role: str = "investigator",
) -> dict:
    """
    Dispatch to the relational or graph aggregate family based on simple
    keyword matching, and return a small result dict the orchestrator
    formats into the cross-case-labeled response.

    Cross-case, same as XGRAPH — requires the same supervisor-or-higher
    role gate and audit logging (Phase 7 RBAC applies to every cross-case
    route uniformly, not just graph traversal).
    """
    if user_role not in ("supervisor", "station-admin", "platform-admin"):
        logger.warning("Unauthorized cross-case aggregate query attempted by %s (user_id: %s)", user_role, user_id)
        try:
            await gateway.log_audit_event(
                event_type="authorization_violation",
                user_id=user_id,
                case_id=None,
                details={"target_entity": target_entity, "query": query_text, "role": user_role, "route": "XAGG"},
            )
        except Exception as e:
            logger.error("Failed to audit log unauthorized XAGG attempt: %s", e)
        raise PermissionError("Cross-case aggregate queries require supervisor role or higher.")

    try:
        await gateway.log_audit_event(
            event_type="cross_case_aggregate",
            user_id=user_id,
            case_id=None,
            details={"target_entity": target_entity, "query": query_text},
        )
    except Exception as e:
        logger.error("Failed to audit log cross-case aggregate query: %s", e)

    # Phase 2: arm the Postgres RLS cross-case bypass only now that the
    # role check above has passed — same fix/rationale as
    # graph_retriever.py::retrieve_graph(). See that function's comment.
    # Also self-arm rls_active here (security-review addendum): this used
    # to rely entirely on the caller (chat_endpoint's set_case_scope())
    # having already armed it, a convention enforced only by docstring —
    # a future second caller of run_aggregate() that forgets to arm RLS
    # upstream would otherwise run with app.rls_active never set, which
    # migration 010's policies treat as "RLS fully inactive" (fail-open).
    current_rls_active.set(True)
    current_cross_case.set(True)

    query_lower = query_text.lower()

    if _matches_any(query_lower, _VEHICLE_KEYWORDS):
        top = await _top_recurring_nodes("Vehicle")
        return {"kind": "graph_recurrence", "entity_type": "Vehicle", "results": top}

    if _matches_any(query_lower, _PERSON_KEYWORDS):
        top = await _top_recurring_nodes("Person")
        return {"kind": "graph_recurrence", "entity_type": "Person", "results": top}

    result = await _station_or_category_counts(gateway, query_text)
    return {"kind": "relational_aggregate", **result}
