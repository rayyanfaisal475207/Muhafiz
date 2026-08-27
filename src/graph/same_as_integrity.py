# ============================================================
# Confirmed Person SAME_AS CNIC-conflict check — the core query/rule
# behind scripts/audit_confirmed_same_as_integrity.py, factored out to
# src/graph/ so src/graph/ingestion_circuit_breaker.py (Module G2) can
# call it directly without src/ reaching into scripts/ (this codebase's
# scripts/ wrap src/, never the reverse).
#
# WHY THIS RULE: scripts/collapse_same_document_duplicate_persons.py and
# scripts/collapse_cross_document_duplicate_persons.py both enforce a
# hard veto at confirm-time — two Person nodes with different, populated
# CNICs are never confirmed as the same entity. This module answers the
# same question in reverse, over already-CONFIRMED edges, independent of
# which tool produced them: does every currently-confirmed, non-
# superseded Person SAME_AS edge still satisfy that veto? A hit means two
# different national ID numbers are presently being treated as one
# canonical person via community_detection.build_canonical_map()'s read-
# time collapse.
#
# READ-ONLY. Nothing in this module writes a SAME_AS edge, confirms,
# rejects, or supersedes anything — same "adds visibility, never a new
# judgment call" discipline as ingestion_quality.py/candidate_
# reprioritization.py.
# ============================================================

from __future__ import annotations

from typing import Optional

from src.graph import age_client

# Directed match only — see collapse_cross_document_duplicate_persons.py's
# own comment on why `(a)-[r]-(b)` (undirected) is both slower (measured
# 48s vs 65ms on this graph, an un-indexable Cartesian OR) and would visit
# every edge twice. Every SAME_AS edge has exactly one stored direction.
_CONFIRMED_QUERY = (
    "MATCH (a:Person)-[r:SAME_AS]->(b:Person) "
    "WHERE r.status = 'confirmed' AND r.superseded_by IS NULL "
    "RETURN id(r) AS edge_id, a.entity_id AS a_id, b.entity_id AS b_id, "
    "a.canonical_name AS a_name, b.canonical_name AS b_name, "
    "a.cnic AS a_cnic, b.cnic AS b_cnic, "
    "a.source_doc_id AS a_doc, b.source_doc_id AS b_doc, "
    "r.tier AS tier, r.reviewed_by AS reviewed_by"
)

_CONFIRMED_QUERY_BY_CASE = (
    "MATCH (a:Person)-[r:SAME_AS]->(b:Person) "
    "WHERE r.status = 'confirmed' AND r.superseded_by IS NULL "
    "MATCH (a)-[ba:BELONGS_TO_CASE]->(ca:Case) WHERE ba.superseded_by IS NULL "
    "  AND ca.case_id = $case_id "
    "RETURN id(r) AS edge_id, a.entity_id AS a_id, b.entity_id AS b_id, "
    "a.canonical_name AS a_name, b.canonical_name AS b_name, "
    "a.cnic AS a_cnic, b.cnic AS b_cnic, "
    "a.source_doc_id AS a_doc, b.source_doc_id AS b_doc, "
    "r.tier AS tier, r.reviewed_by AS reviewed_by"
)

_COLUMNS = [
    "edge_id", "a_id", "b_id", "a_name", "b_name", "a_cnic", "b_cnic",
    "a_doc", "b_doc", "tier", "reviewed_by",
]


def cnic_conflict(row: dict) -> bool:
    """
    Same rule as both collapse scripts' guard 5, applied here in reverse —
    to DETECT a violation instead of preventing one. A missing CNIC on
    either side is not a conflict (the normal case for narrative-extracted
    mentions); only a real, populated disagreement counts.
    """
    a, b = (row.get("a_cnic") or "").strip(), (row.get("b_cnic") or "").strip()
    return bool(a) and bool(b) and a != b


async def fetch_confirmed(case_id: Optional[str] = None) -> list[dict]:
    if case_id:
        return await age_client.execute_cypher(
            _CONFIRMED_QUERY_BY_CASE, params={"case_id": case_id}, columns=_COLUMNS,
        )
    return await age_client.execute_cypher(_CONFIRMED_QUERY, columns=_COLUMNS)


async def find_cnic_conflicts(case_id: Optional[str] = None) -> list[dict]:
    """
    The confirmed edges violating the CNIC hard veto, scoped to one case
    when given (bounded — a single-case full scan is cheap enough to run
    once per case-scoped ingestion run) or the whole graph when omitted
    (the unscoped full-label scan — periodic/manual use only, see
    scripts/audit_confirmed_same_as_integrity.py's own docstring for why
    this is never run per-document).
    """
    rows = await fetch_confirmed(case_id)
    return [r for r in rows if cnic_conflict(r)]
