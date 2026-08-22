# ============================================================
# Entity-resolution review queue — Phase 4.11
#
# Surfaces every pending SAME_AS edge (flagged_unverified / human_review
# tier — CNIC-tier auto-merges never appear here, they don't need a human
# decision) with its basis text, so an investigator can confirm or reject
# a match with the reason actually shown, not a bare confidence percentage
# — the architecture doc's own framing of what makes a match verifiable
# rather than a rubber stamp.
#
# Confirm/reject writes go through versioning.py like every other graph
# write in this build: a new SAME_AS edge with the decided status,
# superseding the pending one — never an in-place mutation.
# ============================================================

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.auth.jwt import require_role
from src.auth.rls_context import cross_case_rls_dependency
from src.data_gateway import get_gateway
from src.database.models import User
from src.graph import age_client, candidate_reprioritization, pending_candidate_priority, versioning

logger = logging.getLogger(__name__)

# Nested under /api/admin — admin-frontend's shared axios client
# (admin-frontend/src/api.ts) is hardcoded to that baseURL, matching
# every other admin-dashboard router (admin.py, cases.py's admin surface).
#
# This queue is deliberately cross-case by product design — reviewed and
# confirmed 2026-07-29 (see docs/graph_schema.md's "Reviewed tradeoff"
# section). Finding the same real-world person across different cases is
# the entire point of this queue; RLS is armed but the case dimension is
# intentionally bypassed here, same as admin.py, as a permanent design
# decision, not a pending gap.
router = APIRouter(
    prefix="/api/admin/graph-review", tags=["graph-review"],
    dependencies=[Depends(cross_case_rls_dependency)],
)


class ReviewAction(BaseModel):
    pass


def _entity_summary(node: dict) -> dict:
    props = node.get("properties", {})
    return {
        "entity_id": props.get("entity_id"),
        "type": node.get("label"),
        "canonical_name": props.get("canonical_name"),
        "cnic": props.get("cnic"),
        "plate": props.get("plate"),
    }


@router.get("/pending")
async def list_pending(case_id: str | None = None, tier: str | None = None, admin: User = Depends(require_role("supervisor"))):
    """
    Every pending SAME_AS edge, newest first, each with the mention/
    candidate entity summaries and the human-readable basis.
    """
    rows = await age_client.execute_cypher(
        "MATCH (a)-[r:SAME_AS]->(b) "
        "WHERE r.status = 'pending' AND r.superseded_by IS NULL "
        "OPTIONAL MATCH (a)-[:BELONGS_TO_CASE]->(ca:Case) "
        "OPTIONAL MATCH (b)-[:BELONGS_TO_CASE]->(cb:Case) "
        "RETURN a, r, b, ca.case_id AS a_case_id, cb.case_id AS b_case_id",
        columns=["a", "r", "b", "a_case_id", "b_case_id"],
    )

    results = []
    for row in rows:
        edge = row["r"]
        props = edge.get("properties", {})
        if tier and props.get("tier") != tier:
            continue
        # A caller passing case_id gets the queue narrowed to matches
        # touching that case — an opt-in filter, not a default
        # restriction. Deliberately NOT the default: this queue is meant
        # to surface cross-case matches too (the P-006 flagship case), so
        # case-scoping it by default would hide exactly the matches most
        # worth an investigator's attention (see module docstring and
        # docs/graph_schema.md's "Reviewed tradeoff" section — the
        # cross-case exposure itself is a confirmed, permanent design
        # decision, not an open question).
        if case_id and case_id not in (row.get("a_case_id"), row.get("b_case_id")):
            continue
        results.append({
            "edge_id": edge["id"],
            "tier": props.get("tier"),
            "confidence": props.get("confidence"),
            "basis": props.get("basis"),
            "as_of": props.get("as_of"),
            "source_doc_id": props.get("source_doc_id"),
            "source_chunk_id": props.get("source_chunk_id"),
            "mention": _entity_summary(row["a"]),
            "candidate": _entity_summary(row["b"]),
        })

    results.sort(key=lambda r: r["as_of"] or "", reverse=True)
    return {"pending": results, "count": len(results)}


@router.get("/stats")
async def review_stats(admin: User = Depends(require_role("supervisor"))):
    """Tier x status counts, for the admin dashboard summary."""
    rows = await age_client.execute_cypher(
        "MATCH ()-[r:SAME_AS]->() WHERE r.superseded_by IS NULL RETURN r",
        columns=["r"],
    )
    counts: dict[str, dict[str, int]] = {}
    for row in rows:
        props = row["r"].get("properties", {})
        tier = props.get("tier", "unknown")
        status = props.get("status", "unknown")
        counts.setdefault(tier, {}).setdefault(status, 0)
        counts[tier][status] += 1
    return counts


async def _get_same_as_edge(edge_id: int) -> tuple[dict, dict, dict]:
    rows = await age_client.execute_cypher(
        "MATCH (a)-[r:SAME_AS]->(b) WHERE id(r) = $edge_id RETURN a, r, b",
        params={"edge_id": edge_id},
        columns=["a", "r", "b"],
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Review edge not found")
    return rows[0]["a"], rows[0]["r"], rows[0]["b"]


@router.post("/{edge_id}/confirm")
async def confirm_match(edge_id: int, action: ReviewAction, admin: User = Depends(require_role("supervisor"))):
    """
    Investigator confirms the match. Writes a new SAME_AS edge with
    status='confirmed', superseding the pending one — the mention node
    itself is NOT merged into the candidate (see docs/graph_schema.md);
    downstream traversal treats a confirmed SAME_AS as identity.
    """
    a, r, b = await _get_same_as_edge(edge_id)
    # Append-only writes never mutate this specific edge's own `status` —
    # confirming/rejecting creates a NEW edge and sets `superseded_by` on
    # this one (versioning.py). So the only reliable "has this already
    # been acted on" check is superseded_by, not re-reading `status` off
    # the same, permanently-unchanged edge.
    if r["properties"].get("superseded_by") is not None:
        raise HTTPException(status_code=409, detail="This match has already been reviewed")

    new_edge = await versioning.write_edge(
        "SAME_AS", a["label"], {"entity_id": a["properties"]["entity_id"]},
        b["label"], {"entity_id": b["properties"]["entity_id"]},
        {
            "tier": r["properties"].get("tier"),
            "basis": r["properties"].get("basis"),
            "status": "confirmed",
            "reviewed_by": str(admin.id),
        },
        source_doc_id=r["properties"].get("source_doc_id"),
        source_chunk_id=r["properties"].get("source_chunk_id"),
        confidence=r["properties"].get("confidence", 0.0),
        supersedes_edge_id=edge_id,
    )
    if new_edge is None:
        raise HTTPException(status_code=500, detail="Failed to write confirmation")

    gateway = await get_gateway()
    await gateway.log_audit_event(
        "graph_review_confirm",
        {
            "edge_id": edge_id,
            "new_edge_id": new_edge["id"],
            "mention_entity_id": a["properties"].get("entity_id"),
            "candidate_entity_id": b["properties"].get("entity_id"),
        },
        str(admin.id),
    )
    return {"status": "confirmed", "new_edge_id": new_edge["id"]}


@router.post("/{edge_id}/reject")
async def reject_match(edge_id: int, action: ReviewAction, admin: User = Depends(require_role("supervisor"))):
    """Investigator rejects the match — same versioned-supersede pattern as confirm."""
    a, r, b = await _get_same_as_edge(edge_id)
    # Append-only writes never mutate this specific edge's own `status` —
    # confirming/rejecting creates a NEW edge and sets `superseded_by` on
    # this one (versioning.py). So the only reliable "has this already
    # been acted on" check is superseded_by, not re-reading `status` off
    # the same, permanently-unchanged edge.
    if r["properties"].get("superseded_by") is not None:
        raise HTTPException(status_code=409, detail="This match has already been reviewed")

    new_edge = await versioning.write_edge(
        "SAME_AS", a["label"], {"entity_id": a["properties"]["entity_id"]},
        b["label"], {"entity_id": b["properties"]["entity_id"]},
        {
            "tier": r["properties"].get("tier"),
            "basis": r["properties"].get("basis"),
            "status": "rejected",
            "reviewed_by": str(admin.id),
        },
        source_doc_id=r["properties"].get("source_doc_id"),
        source_chunk_id=r["properties"].get("source_chunk_id"),
        confidence=r["properties"].get("confidence", 0.0),
        supersedes_edge_id=edge_id,
    )
    if new_edge is None:
        raise HTTPException(status_code=500, detail="Failed to write rejection")

    gateway = await get_gateway()
    await gateway.log_audit_event(
        "graph_review_reject",
        {
            "edge_id": edge_id,
            "new_edge_id": new_edge["id"],
            "mention_entity_id": a["properties"].get("entity_id"),
            "candidate_entity_id": b["properties"].get("entity_id"),
        },
        str(admin.id),
    )
    return {"status": "rejected", "new_edge_id": new_edge["id"]}


# ── CITES review queue — M6b of the Muhafiz Data API migration
# (docs/decisions/0001-muhafiz-api-migration.md) ─────────────────────────
#
# A PARALLEL queue, not a merge into the SAME_AS one above: CITES links two
# Case nodes (a FIR-to-FIR prose citation), not two Person/Vehicle mention
# nodes — forcing it through _entity_summary()/confirm_match()'s
# entity_id-keyed lookups above would be wrong (Case nodes have no
# entity_id, only case_id) and would corrupt the queue's Person/Vehicle-
# shaped rendering for investigators reviewing genuine identity matches.
# Same human-confirmation discipline (pending -> confirmed/rejected via
# versioning's supersede pattern), same audit logging — just keyed and
# summarized for what a CITES edge actually connects.

def _case_summary(node: dict) -> dict:
    props = node.get("properties", {})
    return {"case_id": props.get("case_id"), "type": node.get("label")}


@router.get("/citations/pending")
async def list_pending_citations(admin: User = Depends(require_role("supervisor"))):
    """Every pending CITES edge, newest first."""
    rows = await age_client.execute_cypher(
        "MATCH (a:Case)-[r:CITES]->(b:Case) "
        "WHERE r.status = 'pending' AND r.superseded_by IS NULL "
        "RETURN a, r, b",
        columns=["a", "r", "b"],
    )
    results = []
    for row in rows:
        edge = row["r"]
        props = edge.get("properties", {})
        results.append({
            "edge_id": edge["id"],
            "confidence": props.get("confidence"),
            "basis": props.get("basis"),
            "as_of": props.get("as_of"),
            "source_doc_id": props.get("source_doc_id"),
            "citing_case": _case_summary(row["a"]),
            "cited_case": _case_summary(row["b"]),
        })
    results.sort(key=lambda r: r["as_of"] or "", reverse=True)
    return {"pending": results, "count": len(results)}


async def _get_cites_edge(edge_id: int) -> tuple[dict, dict, dict]:
    rows = await age_client.execute_cypher(
        "MATCH (a:Case)-[r:CITES]->(b:Case) WHERE id(r) = $edge_id RETURN a, r, b",
        params={"edge_id": edge_id},
        columns=["a", "r", "b"],
    )
    if not rows:
        raise HTTPException(status_code=404, detail="Citation edge not found")
    return rows[0]["a"], rows[0]["r"], rows[0]["b"]


async def _decide_citation(edge_id: int, decision: str, admin: User) -> dict:
    a, r, b = await _get_cites_edge(edge_id)
    if r["properties"].get("superseded_by") is not None:
        raise HTTPException(status_code=409, detail="This citation has already been reviewed")

    new_edge = await versioning.write_edge(
        "CITES", "Case", {"case_id": a["properties"]["case_id"]},
        "Case", {"case_id": b["properties"]["case_id"]},
        {
            "basis": r["properties"].get("basis"),
            "status": decision,
            "reviewed_by": str(admin.id),
        },
        source_doc_id=r["properties"].get("source_doc_id"),
        confidence=r["properties"].get("confidence", 0.0),
        supersedes_edge_id=edge_id,
    )
    if new_edge is None:
        raise HTTPException(status_code=500, detail=f"Failed to write {decision}")

    gateway = await get_gateway()
    await gateway.log_audit_event(
        f"graph_review_citation_{decision}",
        {
            "edge_id": edge_id,
            "new_edge_id": new_edge["id"],
            "citing_case_id": a["properties"].get("case_id"),
            "cited_case_id": b["properties"].get("case_id"),
        },
        str(admin.id),
    )
    return {"status": decision, "new_edge_id": new_edge["id"]}


@router.post("/citations/{edge_id}/confirm")
async def confirm_citation(edge_id: int, action: ReviewAction, admin: User = Depends(require_role("supervisor"))):
    return await _decide_citation(edge_id, "confirmed", admin)


@router.post("/citations/{edge_id}/reject")
async def reject_citation(edge_id: int, action: ReviewAction, admin: User = Depends(require_role("supervisor"))):
    return await _decide_citation(edge_id, "rejected", admin)


# ── Reordered/grouped review queue — Milestone D1 (GRAPH_SCALE_SCHEMA_
# EXPANSION_PLAN.md — pending-candidate reprioritization) ────────────────
#
# NEW batch-review surface, additive alongside /pending above (which stays
# unchanged — admin-frontend's existing "confirm one match" flow keeps
# working exactly as before). Backed by the Postgres side table
# src/graph/pending_candidate_priority.py maintains (migration 027), not
# an AGE scan — see that migration's header for why /pending's own
# `WHERE r.status = 'pending'` was the unindexed hot-path this queue
# exists to route around at real volume.
#
# Every write below still goes through confirm_match()/reject_match()
# ABOVE, per edge_id, one at a time — a "batch action" here means one
# supervisor click issuing several of the SAME already-audited,
# already-tested single-edge confirm/reject calls this router has always
# had, never a new graph-write code path. The hard human-confirmation
# rule stays exactly as absolute as it is for /pending: nothing under
# /queue can itself set an edge's status to confirmed/rejected.

async def _fetch_node_by_key(entity_id: str) -> Optional[dict]:
    """Label-less entity_id lookup — same reasoning as candidate_reprioritization._fetch_node_by_entity_id (the priority-index row doesn't carry the node's graph label)."""
    rows = await age_client.execute_cypher(
        "MATCH (n) WHERE n.entity_id = $id RETURN n LIMIT 1",
        params={"id": entity_id}, columns=["n"],
    )
    return rows[0]["n"] if rows else None


@router.get("/queue")
async def list_queue(include_deprioritized: bool = True, admin: User = Depends(require_role("supervisor"))):
    """
    Every pending SAME_AS candidate, REORDERED by priority_score
    (deprioritized/stale candidates sink to the bottom, never dropped) —
    the "reorders the review queue" half of D1. Each entry carries its
    deterministic `why` (see candidate_reprioritization._why — a
    template over scoring fields, never an LLM narration) alongside the
    original tier/basis a supervisor already sees on /pending.

    A candidate never re-scored yet (freshly written, no ingest-triggered
    or manual sweep has touched it) carries `why=None` and a
    `priority_score` seeded from its own original write-time confidence
    (see pending_candidate_priority.maintain_pending) — it simply sorts
    alongside other candidates on that starting value, not silently
    hidden, errored on, or bunched at a meaningless literal 0.
    """
    rows = await pending_candidate_priority.list_rows("SAME_AS", include_deprioritized=include_deprioritized)
    results = []
    for row in rows:
        a_node = await _fetch_node_by_key(row["a_key"])
        b_node = await _fetch_node_by_key(row["b_key"])
        results.append({
            "edge_id": row["edge_id"],
            "tier": row["tier"],
            "priority_score": row["priority_score"],
            "why": row["why"],
            "group_id": row["group_id"],
            "deprioritized": row["deprioritized"],
            "original_basis": row["original_basis"],
            "mention": _entity_summary(a_node) if a_node else {"entity_id": row["a_key"]},
            "candidate": _entity_summary(b_node) if b_node else {"entity_id": row["b_key"]},
        })
    return {"queue": results, "count": len(results)}


@router.get("/queue/groups")
async def list_queue_groups(admin: User = Depends(require_role("supervisor"))):
    """
    Pending SAME_AS candidates clustered into batches by
    candidate_reprioritization's connected-components grouping — the
    "groups related weak candidates... so a human can review/act on many
    at once" half of D1. A candidate never grouped yet gets its own
    singleton group (`UNGROUPED-<edge_id>`) rather than being omitted.
    """
    rows = await pending_candidate_priority.list_rows("SAME_AS")
    groups: dict[str, list[dict]] = {}
    for row in rows:
        gid = row["group_id"] or f"UNGROUPED-{row['edge_id']}"
        groups.setdefault(gid, []).append(row)

    out = []
    for gid, members in groups.items():
        members_sorted = sorted(members, key=lambda r: r["priority_score"] or 0.0, reverse=True)
        out.append({
            "group_id": gid,
            "member_count": len(members_sorted),
            "top_priority_score": members_sorted[0]["priority_score"],
            "why": members_sorted[0]["why"],
            "edge_ids": [m["edge_id"] for m in members_sorted],
            "deprioritized": all(m["deprioritized"] for m in members_sorted),
        })
    out.sort(key=lambda g: g["top_priority_score"] or 0.0, reverse=True)
    return {"groups": out, "count": len(out)}


@router.post("/queue/reprioritize")
async def reprioritize_queue(admin: User = Depends(require_role("supervisor"))):
    """
    Manual full-queue re-score — Milestone D1's execution model point 3,
    path #2 (there is no cron in this codebase to run this on a
    schedule; a supervisor triggers it on demand). Re-scores every
    pending SAME_AS candidate against the graph's current state and
    re-groups them; never confirms/rejects anything.
    """
    updated = await candidate_reprioritization.reprioritize_all()
    return {"rescored": updated}


async def _decide_batch(group_id: str, decision: str, admin: User) -> dict:
    rows = await pending_candidate_priority.list_rows("SAME_AS")
    members = [r for r in rows if (r["group_id"] or f"UNGROUPED-{r['edge_id']}") == group_id]
    if not members:
        raise HTTPException(status_code=404, detail="No pending candidates found in this batch")

    action = ReviewAction()
    results = []
    for row in members:
        try:
            if decision == "confirm":
                result = await confirm_match(row["edge_id"], action, admin)
            else:
                result = await reject_match(row["edge_id"], action, admin)
            results.append({"edge_id": row["edge_id"], **result})
        except HTTPException as exc:
            # One member already reviewed independently (409) or missing
            # (404) must not abort the rest of the batch — each edge_id's
            # own confirm/reject call is independently audited and
            # independently reported back, same as if a supervisor had
            # clicked them one at a time.
            results.append({"edge_id": row["edge_id"], "error": exc.detail, "status_code": exc.status_code})
    return {"group_id": group_id, "decision": decision, "results": results}


@router.post("/queue/batches/{group_id}/confirm")
async def confirm_batch(group_id: str, action: ReviewAction, admin: User = Depends(require_role("supervisor"))):
    """A supervisor's single batch action — internally one confirm_match() call per member edge, see _decide_batch."""
    return await _decide_batch(group_id, "confirm", admin)


@router.post("/queue/batches/{group_id}/reject")
async def reject_batch(group_id: str, action: ReviewAction, admin: User = Depends(require_role("supervisor"))):
    return await _decide_batch(group_id, "reject", admin)
