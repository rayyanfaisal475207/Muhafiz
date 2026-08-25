# ============================================================
# GLOBAL SEARCH — whole-dataset map-reduce reasoning (findings.md Module 9,
# Stage 1). Modeled directly on xnetwork.py's shape (same role gate
# ordering, same audit logging, same RLS cross-case arming pattern) — the
# one deliberate difference is WHAT it fetches: xnetwork.py's
# run_network_query() asks Chroma for a top-k similarity cut of community
# summaries; run_global_search_query() below fetches EVERY community
# report for a hierarchy level directly from Postgres
# (community_detection.get_community_reports_for_level()), because a
# report that's a weak semantic match to the literal query string but
# collectively part of a real dataset-wide pattern must not be excluded
# before the map step ever sees it — that's the whole failure mode this
# module exists to close (findings.md Module 9's "Problem" section).
#
# This module owns the role gate + RLS arming + fetch only, same "thin
# fetch primitive" boundary xnetwork.py draws for itself. The actual
# map-reduce (batching, shuffling, per-batch call_llm_json(), the reduce
# step, final generation + verify_grounding()) lives in the sub-agent that
# composes this via its tool wrapper
# (src/pipeline/harness/agents/global_search.py) — same split
# xnetwork.py/cross_case_linkage.py already establish between "fetch
# primitive" and "generate+verify".
# ============================================================

from __future__ import annotations

import logging
from typing import Optional

from src.graph import community_detection

logger = logging.getLogger(__name__)


async def run_global_search_query(
    query_text: str,
    gateway,
    user_id: Optional[str] = None,
    user_role: str = "investigator",
    hierarchy_level: Optional[int] = None,
    jurisdiction_case_ids: Optional[list[str]] = None,
) -> dict:
    """
    Fetch every community report for one hierarchy level, role-gated the
    same way as XNETWORK/XAGG/XGRAPH.

    `hierarchy_level=None` resolves to the middle of whatever levels
    community_detection.get_available_report_levels() reports as actually
    summarized for the latest run — "default a middle level, tunable" per
    findings.md's Stage 2 proposal. Pre-Stage-2, that list is always just
    `[0]`, so this always resolves to level 0 today; this default logic is
    written now so Stage 2's real hierarchy makes it meaningful without
    another change here.

    `jurisdiction_case_ids`: same POST-filter caveat xnetwork.py's own
    docstring already documents for XNETWORK — narrows the already-fetched
    report set after the fact rather than pushing the filter into the SQL
    itself. Kept for signature parity with run_network_query(); like
    xnetwork.py's own current tool wrapper, the Phase-0 harness tool below
    does not pass this yet (not inventing new scope beyond what XNETWORK
    itself already wires).
    """
    if user_role not in ("supervisor", "station-admin", "platform-admin"):
        logger.warning("Unauthorized global search query attempted by %s (user_id: %s)", user_role, user_id)
        try:
            await gateway.log_audit_event(
                event_type="authorization_violation",
                user_id=user_id,
                case_id=None,
                details={"query": query_text, "role": user_role, "route": "GLOBAL_SEARCH"},
            )
        except Exception as e:
            logger.error("Failed to audit log unauthorized Global Search attempt: %s", e)
        raise PermissionError("Global search queries require supervisor role or higher.")

    try:
        await gateway.log_audit_event(
            event_type="cross_case_global_search",
            user_id=user_id,
            case_id=None,
            details={"query": query_text, "hierarchy_level": hierarchy_level},
        )
    except Exception as e:
        logger.error("Failed to audit log cross-case global search query: %s", e)

    # RLS cross-case bypass, armed only after the role check passes — same
    # fix/rationale as xnetwork.py::run_network_query() and
    # xagg.py::run_aggregate().
    from src.database.postgres import current_cross_case, current_rls_active
    current_rls_active.set(True)
    current_cross_case.set(True)

    resolved_level = hierarchy_level
    if resolved_level is None:
        available_levels = await community_detection.get_available_report_levels()
        resolved_level = available_levels[len(available_levels) // 2] if available_levels else 0

    reports = await community_detection.get_community_reports_for_level(resolved_level)
    if jurisdiction_case_ids is not None:
        allowed = set(jurisdiction_case_ids)
        reports = [r for r in reports if allowed & set(r.get("case_ids") or [])]

    community_ids = [r["community_id"] for r in reports]
    case_ids = sorted({cid for r in reports for cid in (r.get("case_ids") or [])})

    return {
        "kind": "global_search_reports",
        "hierarchy_level": resolved_level,
        "reports": reports,
        "community_ids": community_ids,
        "case_ids": case_ids,
    }
