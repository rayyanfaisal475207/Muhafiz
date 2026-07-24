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

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from src.auth.jwt import require_role
from src.database.models import User
from src.graph import age_client, versioning

logger = logging.getLogger(__name__)

# Nested under /api/admin — admin-frontend's shared axios client
# (admin-frontend/src/api.ts) is hardcoded to that baseURL, matching
# every other admin-dashboard router (admin.py, cases.py's admin surface).
router = APIRouter(prefix="/api/admin/graph-review", tags=["graph-review"])


class ReviewAction(BaseModel):
    reviewed_by: str = "admin"


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
        "RETURN a, r, b",
        columns=["a", "r", "b"],
    )

    results = []
    for row in rows:
        edge = row["r"]
        props = edge.get("properties", {})
        if tier and props.get("tier") != tier:
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

    # case_id isn't a SAME_AS edge property — filtering by it means
    # checking whether either endpoint belongs to that case, which is a
    # second query per candidate. Deliberately not done here: this queue
    # is meant to surface cross-case matches too (the P-006 flagship
    # case), so case-scoping the review queue by default would hide
    # exactly the matches most worth an investigator's attention.
    if case_id:
        logger.debug("case_id filter on the review queue is not applied — see docstring")

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
            "reviewed_by": action.reviewed_by,
        },
        source_doc_id=r["properties"].get("source_doc_id"),
        source_chunk_id=r["properties"].get("source_chunk_id"),
        confidence=r["properties"].get("confidence", 0.0),
        supersedes_edge_id=edge_id,
    )
    if new_edge is None:
        raise HTTPException(status_code=500, detail="Failed to write confirmation")
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
            "reviewed_by": action.reviewed_by,
        },
        source_doc_id=r["properties"].get("source_doc_id"),
        source_chunk_id=r["properties"].get("source_chunk_id"),
        confidence=r["properties"].get("confidence", 0.0),
        supersedes_edge_id=edge_id,
    )
    if new_edge is None:
        raise HTTPException(status_code=500, detail="Failed to write rejection")
    return {"status": "rejected", "new_edge_id": new_edge["id"]}
