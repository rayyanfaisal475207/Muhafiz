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

_VEHICLE_KEYWORDS = (
    "vehicle", "car", "motorcycle", "plate", "gari", "gaari", "motorcycle",
    "گاڑی", "موٹرسائیکل", "نمبر پلیٹ",
)
_PERSON_KEYWORDS = (
    "person", "people", "suspect", "offender", "recidivist", "accused",
    "mulzim", "shakhs",
    "شخص", "افراد", "لوگ", "ملزم",
)
_STATION_KEYWORDS = ("station", "thana", "تھانہ", "چوکی")
# Previously English-only, unlike the three keyword sets above — an Urdu
# query mentioning "بند" (closed) or "چوری" (theft) silently skipped the
# status/category filter entirely rather than applying it, since none of
# these matched. Same class of gap as verifier.py's _HEDGE_PHRASES.
# 2026-08-04: extended again — the Urdu-script fix above still had zero
# Roman-Urdu coverage in any of the six lists (confirmed live: Roman-Urdu
# queries never matched any keyword and silently fell through to the
# generic category-count default), and "زیر تفتیش" (under investigation)
# was missing from both language sides.
_STATUS_KEYWORDS = (
    "open", "closed", "status", "pending", "under investigation",
    "band", "khula", "khuli", "zair-e-tafteesh", "zair e tafteesh", "kholay",
    "کھلا", "بند", "حالت", "زیر التواء", "زیر تفتیش",
)
_CATEGORY_KEYWORDS = (
    "theft", "burglary", "fraud", "category", "type of case",
    "chori", "dhoka",
    "چوری", "ڈکیتی", "نقب زنی", "دھوکہ دہی", "قسم",
)
# A plain "list/show every case" request — distinct from the grouped-count
# queries below (which always answer "counts of cases by X", never the raw
# records). Router previously had nowhere to send this ("list of all cases"
# names no entity, so it isn't XGRAPH's people/vehicle/org enumeration
# either) — it fell through to XGRAPH by wording proximity to "list of all
# PEOPLE mentioned in the cases" and traversed nothing, since "a case" isn't
# a graph node XGRAPH can seed from.
_LIST_ALL_KEYWORDS = (
    "list", "show", "all cases", "every case", "dikhao", "sab cases",
    "فہرست", "تمام مقدمات", "دکھائیں",
)
# A bare "how many total" request — distinct from _LIST_ALL_KEYWORDS (which
# wants the raw records) and from the grouped-count default below (which
# always breaks the answer down by station/category). Live-observed gap
# (demotestfinal.md §7): "کل کتنے کیسز ہیں؟" ("how many cases in total")
# had no keyword family routing to a single number, so it fell through to
# _station_or_category_counts and came back as a category-by-category
# breakdown instead of one total. Bilingual from the start (English +
# Urdu-script + Roman-Urdu), matching the established pattern the other
# keyword sets in this module were each retrofitted to after being found
# English-only first.
_TOTAL_KEYWORDS = (
    "total", "grand total", "how many cases", "how many cases are there",
    "how many cases in total", "overall count",
    "kitne cases", "kul kitne", "kitne kul", "total kitne",
    "کل کتنے", "کتنے کیسز", "کل تعداد", "مجموعی تعداد", "کل کیسز",
)


def _matches_any(text: str, keywords: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(kw in lowered for kw in keywords)


async def _top_recurring_nodes(
    label: str, limit: int = 10, jurisdiction_case_ids: Optional[list[str]] = None,
) -> list[dict]:
    """
    Count distinct cases each node of `label` touches via BELONGS_TO_CASE,
    descending. [Milestone E1] `jurisdiction_case_ids`, when given,
    restricts the match to that case set before the recurrence count is
    computed — the same candidate-set-narrowing role
    `graph_retriever._find_recurring_entities_for_query()`'s own
    `jurisdiction_case_ids` param plays for XGRAPH.
    """
    if jurisdiction_case_ids is not None:
        rows = await age_client.execute_cypher(
            f"MATCH (n:{label})-[:BELONGS_TO_CASE]->(c:Case) WHERE c.case_id IN $case_ids "
            "RETURN n, c",
            params={"case_ids": jurisdiction_case_ids}, columns=["n", "c"],
        )
    else:
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


async def _filtered_cases(
    gateway, query_text: str, jurisdiction_case_ids: Optional[list[str]] = None,
) -> list[dict]:
    """
    The open/closed + category filtering shared by both the grouped-count
    path (_station_or_category_counts) and the grand-total path
    (_total_count) below — pulled out so a query like "how many closed
    cases in total" still respects the status filter instead of the
    grand-total path bypassing it entirely.

    [Milestone E1] `jurisdiction_case_ids`, when given, narrows the case
    set to that allow-list FIRST — before the status/category filtering
    below even runs — same "cut the candidate set up front" goal as the
    graph family's own `jurisdiction_case_ids` handling in
    `_top_recurring_nodes`.
    """
    # The caller (run_aggregate) has already verified the requesting user is
    # supervisor-or-above before reaching here — a cross-case aggregate is
    # meant to cover every case platform-wide, not just ones the caller is
    # individually assigned to. gateway.get_cases() only returns everything
    # for "platform-admin"; passing anything else here (including None)
    # tries to join CaseAssignment on a non-existent user and raises.
    cases = await gateway.get_cases(user_id=None, user_role="platform-admin")
    if jurisdiction_case_ids is not None:
        allowed = set(jurisdiction_case_ids)
        cases = [c for c in cases if c.get("case_id") in allowed]

    # "open" and "closed" are opposite filters — a query naming one must
    # never silently apply the other or (worse) apply neither. Previously
    # only "open" had a branch at all: a query asking for CLOSED cases
    # (English "closed", Urdu "بند"/Roman-Urdu "band") matched
    # _STATUS_KEYWORDS (so the code below it, e.g. category filtering,
    # still ran) but the status filter itself silently no-opped, returning
    # every case regardless of status instead of just the closed ones.
    # Real investigation_status values (see migrations/004_case_model.sql
    # seed data) are "Closed – Convicted"/"Closed – Untraced" style, not a
    # bare "Closed" — substring match on "closed", not an exact-value set.
    _OPEN_TERMS = ("open", "khula", "khuli", "کھلا", "pending", "زیر التواء", "under investigation", "زیر تفتیش")
    _CLOSED_TERMS = ("closed", "band", "بند")

    def _is_closed(c: dict) -> bool:
        return "closed" in (c.get("investigation_status") or "").lower()

    if _matches_any(query_text, _CLOSED_TERMS):
        cases = [c for c in cases if _is_closed(c)]
    elif _matches_any(query_text, _OPEN_TERMS):
        cases = [c for c in cases if not _is_closed(c)]

    if _matches_any(query_text, _CATEGORY_KEYWORDS):
        for kw in _CATEGORY_KEYWORDS:
            if kw in query_text.lower() and kw not in _STATUS_KEYWORDS + ("category", "type of case"):
                cases = [c for c in cases if kw in (c.get("crime_category") or "").lower()]
                break

    return cases


async def _station_or_category_counts(
    gateway, query_text: str, jurisdiction_case_ids: Optional[list[str]] = None,
) -> dict:
    cases = await _filtered_cases(gateway, query_text, jurisdiction_case_ids)
    group_field = "police_station" if _matches_any(query_text, _STATION_KEYWORDS) else "crime_category"
    counts = Counter(c.get(group_field) or "unknown" for c in cases)
    return {
        "group_by": group_field,
        "counts": [{"key": k, "count": v} for k, v in counts.most_common(15)],
        "total_cases_considered": len(cases),
    }


async def _total_count(
    gateway, query_text: str, jurisdiction_case_ids: Optional[list[str]] = None,
) -> dict:
    """A bare "how many total" answer — no grouping, one number. Still
    honors any status/category filter present (e.g. "how many closed
    cases in total"), it just skips the group-by breakdown entirely."""
    cases = await _filtered_cases(gateway, query_text, jurisdiction_case_ids)
    return {"kind": "total_count", "total_cases": len(cases)}


async def run_aggregate(
    query_text: str,
    target_entity: Optional[str],
    gateway,
    user_id: Optional[str] = None,
    user_role: str = "investigator",
    jurisdiction_case_ids: Optional[list[str]] = None,
) -> dict:
    """
    Dispatch to the relational or graph aggregate family based on simple
    keyword matching, and return a small result dict the orchestrator
    formats into the cross-case-labeled response.

    Cross-case, same as XGRAPH — requires the same supervisor-or-higher
    role gate and audit logging (Phase 7 RBAC applies to every cross-case
    route uniformly, not just graph traversal).

    `jurisdiction_case_ids` [Milestone E1]: the case_id allow-list from
    `graph_retriever.resolve_jurisdiction_case_ids()`, already resolved
    (and role-gated) by the orchestrator before this call — `None` when
    the query named no station/district, in which case every family below
    runs exactly as it did before this milestone.
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
        top = await _top_recurring_nodes("Vehicle", jurisdiction_case_ids=jurisdiction_case_ids)
        return {"kind": "graph_recurrence", "entity_type": "Vehicle", "results": top}

    if _matches_any(query_lower, _PERSON_KEYWORDS):
        top = await _top_recurring_nodes("Person", jurisdiction_case_ids=jurisdiction_case_ids)
        return {"kind": "graph_recurrence", "entity_type": "Person", "results": top}

    # A plain enumeration ("list of all cases") with no station/category/status
    # grouping language present — answer with the raw case records rather than
    # forcing it through _station_or_category_counts's group-by, which would
    # silently turn "list all cases" into "counts of cases by category" instead
    # of actually listing them.
    if _matches_any(query_lower, _LIST_ALL_KEYWORDS) and not _matches_any(
        query_lower, _STATION_KEYWORDS + _STATUS_KEYWORDS + _CATEGORY_KEYWORDS
    ):
        cases = await gateway.get_cases(user_id=None, user_role="platform-admin")
        if jurisdiction_case_ids is not None:
            allowed = set(jurisdiction_case_ids)
            cases = [c for c in cases if c.get("case_id") in allowed]
        return {
            "kind": "case_listing",
            "cases": [
                {
                    "case_id": c.get("case_id"),
                    "fir_number": c.get("fir_number"),
                    "crime_category": c.get("crime_category"),
                    "investigation_status": c.get("investigation_status"),
                    "police_station": c.get("police_station"),
                }
                for c in cases
            ],
        }

    # Grand-total: "how many cases in total", with no explicit group-by
    # signal (station/category) present — a query naming a group-by
    # dimension alongside "total" (e.g. "total cases per station") still
    # wants the breakdown, not a bare number, so this only fires when no
    # grouping keyword is also present, the same precedence _LIST_ALL_KEYWORDS
    # already uses above.
    if _matches_any(query_lower, _TOTAL_KEYWORDS) and not _matches_any(
        query_lower, _STATION_KEYWORDS + _CATEGORY_KEYWORDS
    ):
        return await _total_count(gateway, query_text, jurisdiction_case_ids)

    result = await _station_or_category_counts(gateway, query_text, jurisdiction_case_ids)
    return {"kind": "relational_aggregate", **result}
