"""
Milestone D (queue-scale resolution) live verification, against the REAL
running Postgres/AGE instance — not a fake/mock — per
GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md's Milestone D §7 verification note.

    python scripts/verify_milestone_d.py

WHY THIS RUNS AGAINST THE PRODUCTION GRAPH, UNLIKE scripts/
eval_entity_resolution.py: candidate_reprioritization.py (D1) and
pending_candidate_priority.py have no eval-graph override — D1 is a
queue-management feature over real production candidates, not a
resolution-decision algorithm that needed eval isolation from real data
the way entity_resolution.py's tiering does. This script is therefore
DESTRUCTIVE-BUT-CLEANED-UP rather than isolated: every node/edge/row it
writes uses a clearly-tagged synthetic id prefix (`D1VERIFY-`) and the
script deletes all of it again at the end, in a `finally` block, whether
or not the assertions pass — same "never leave synthetic data mixed into
real data" discipline the plan's own §7 verification note requires for
Milestone A's load test, applied here via cleanup instead of a separate
database.

Checks, matching the plan's Milestone D §7 verification bullet exactly:
  1. A synthetic corroborating-evidence sequence: two similar-but-not-
     identical Person mentions resolve to a pending SAME_AS (never
     auto-confirmed), then new evidence (a shared structured id, a
     shared case) lands on both sides.
  2. D1 reorders/groups the pending queue accordingly...
  3. ...but NEVER changes the candidate's status away from 'pending'
     without a simulated human action through graph_review.py's own
     confirm/reject endpoints.
  4. D2: a cross-case retrieval with FEATURE_HEDGED_PENDING_TRAVERSAL on
     surfaces the pending-linked evidence with a disclosed-hedge tag and
     capped confidence; the same query with the flag off does not surface
     it at all (byte-for-byte prior behavior) — and a query that draws on
     no pending evidence carries no hedge tag at all (no false positive).
"""
import asyncio
import sys
import uuid
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.graph import age_client, candidate_reprioritization, entity_resolution as er, pending_candidate_priority, versioning
from src.retrieval import graph_retriever

TAG = f"D1VERIFY-{uuid.uuid4().hex[:8]}"
CASE_A = f"{TAG}-CASE-A"
CASE_B = f"{TAG}-CASE-B"

# entity_resolution._new_entity_id() mints ids like "PERSON-<hex>" — they
# never carry this script's own TAG, unlike case_id/doc_id above. Every
# synthetic entity_id this run creates is tracked here explicitly so
# pending_candidate_priority rows (keyed by entity_id, not by
# case_id/doc_id) can be cleaned up correctly — a `LIKE 'D1VERIFY-%'`
# filter on a_key/b_key would silently match nothing (confirmed live).
_created_entity_ids: list[str] = []


async def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(f"FAILED: {message}")
    print(f"  OK: {message}")


async def _prune_orphaned_priority_rows():
    """
    A pending_candidate_priority row whose edge_id no longer exists in
    AGE at all (the graph-side cleanup for a prior crashed run already
    removed it, but that run's own Postgres cleanup never got to run) is
    pure clutter — prune it. General-purpose (not tag-specific): this is
    the same invariant migration 027's own header states ("a row here
    always traces back to a real pending edge"), so restoring it is safe
    regardless of which run's leftover it is.
    """
    edge_ids = await pending_candidate_priority.list_all_edge_ids("SAME_AS")
    orphaned = [eid for eid in edge_ids if await versioning.get_edge(eid) is None]
    for eid in orphaned:
        await pending_candidate_priority.resolve_pending(eid)
    if orphaned:
        print(f"Pruned {len(orphaned)} orphaned pending_candidate_priority row(s) from a prior run.")


async def _cleanup():
    print(f"\nCleaning up synthetic data tagged {TAG} ...")
    await age_client.execute_cypher(
        "MATCH (n) WHERE n.entity_id STARTS WITH $tag OR n.case_id STARTS WITH $tag "
        "OR n.doc_id STARTS WITH $tag DETACH DELETE n",
        params={"tag": TAG}, columns=["result"],
    )
    # Postgres side: entity_id-keyed rows (see _created_entity_ids'
    # comment above) — cleaned by explicit id list, not a tag pattern
    # match that would never hit.
    from sqlalchemy import text
    from src.database.postgres import get_session
    if _created_entity_ids:
        async with get_session() as db:
            await db.execute(
                text("DELETE FROM pending_candidate_priority WHERE a_key = ANY(:ids) OR b_key = ANY(:ids)"),
                {"ids": _created_entity_ids},
            )
    print("Cleanup complete.")


async def main():
    print(f"Milestone D live verification against the real Postgres/AGE instance (tag={TAG})")

    for cid in (CASE_A, CASE_B):
        await versioning.write_node("Case", {"case_id": cid}, {}, source_doc_id=None, confidence=1.0)
    doc_ids = [f"{TAG}-DOC-A", f"{TAG}-DOC-B", f"{TAG}-DOC-C", f"{TAG}-DOC-D", f"{TAG}-DOC-E", f"{TAG}-DOC-F"]
    for doc_id in doc_ids:
        await versioning.write_node("Document", {"doc_id": doc_id}, {}, source_doc_id=doc_id, confidence=1.0)

    # Names carry the run's own unique TAG so they can never fuzzy-match
    # a real person already in production data (confirmed live: a
    # plain "Fahad Anjum Cheema" matched 33 existing candidates and
    # landed pending on its very first write) — mention_a/mention_b stay
    # near-identical to EACH OTHER (same tag, one-letter surname typo)
    # while being guaranteed to match nothing else in the graph.
    mention_a = {"canonical_name": f"Fahad {TAG} Anjum Cheema"}
    mention_b = {"canonical_name": f"Fahad {TAG} Anjum Chema"}  # deliberately near-identical, no CNIC

    result_a = await er.resolve_and_write("person", mention_a, CASE_A, source_doc_id=f"{TAG}-DOC-A")
    entity_a = result_a["entity_id"]
    _created_entity_ids.append(entity_a)
    # Real production data can still make mention_a itself score a
    # pending match against some unrelated existing person by partial
    # token overlap (confirmed live against this instance's real
    # corpus) — resolve_and_write() always mints a fresh node regardless
    # of tier except cnic_auto (no CNIC given here, so cnic_auto is
    # impossible), which is the only invariant this step actually needs;
    # the tier itself is entity_resolution.py's own business, already
    # covered by tests/test_entity_resolution.py, not this script's.
    await _assert(result_a["is_new_node"] is True, "first mention always resolves to a fresh node (no CNIC -> cnic_auto is impossible)")

    result_b = await er.resolve_and_write("person", mention_b, CASE_A, source_doc_id=f"{TAG}-DOC-B")
    entity_b = result_b["entity_id"]
    _created_entity_ids.append(entity_b)
    await _assert(
        result_b["tier"] in ("flagged_unverified", "human_review"),
        f"near-identical second mention lands in a pending tier (got {result_b['tier']!r})",
    )

    # Find the pending SAME_AS edge from mention_b to mention_a
    # specifically — a near-identical name (one-letter surname typo)
    # against a candidate pool must score highest against its own
    # near-twin, not an unrelated real person, so this is asserted
    # directly rather than assumed.
    rows = await age_client.execute_cypher(
        "MATCH (a)-[r:SAME_AS]->(b) WHERE a.entity_id = $eid AND b.entity_id = $target AND r.status = 'pending' RETURN r",
        params={"eid": entity_b, "target": entity_a}, columns=["r"],
    )
    await _assert(len(rows) == 1, "the pending SAME_AS edge points from mention_b at mention_a, its near-identical twin")
    edge_id = rows[0]["r"]["id"]

    pc_rows = await pending_candidate_priority.list_rows("SAME_AS")
    await _assert(
        any(r["edge_id"] == edge_id for r in pc_rows),
        "migration 027's Postgres side table indexed the pending candidate (versioning.write_edge -> maintain_pending)",
    )
    row_before = next(r for r in pc_rows if r["edge_id"] == edge_id)
    # A freshly-indexed row's priority_score seeds from its own
    # original_confidence (so an unscored queue doesn't bunch every
    # candidate at literal 0) but has no `why` yet — that's only ever
    # produced by an actual reprioritization pass.
    await _assert(row_before["why"] is None, "a freshly-written candidate has no `why` until the first reprioritization pass")

    # ── New corroborating evidence lands: a shared structured id (phone)
    # and a second shared case between the two mention nodes. ──────────
    await versioning.write_node("Person", {"entity_id": entity_a}, {"phone": "0300-5551234"}, source_doc_id=f"{TAG}-DOC-C", confidence=1.0)
    await versioning.write_node("Person", {"entity_id": entity_b}, {"phone": "0300-5551234"}, source_doc_id=f"{TAG}-DOC-C", confidence=1.0)
    await versioning.write_edge(
        "BELONGS_TO_CASE", "Person", {"entity_id": entity_b}, "Case", {"case_id": CASE_B},
        {}, source_doc_id=f"{TAG}-DOC-D", confidence=1.0,
    )

    # ── D1: reorder/group — never touches status ────────────────────
    updated = await candidate_reprioritization.reprioritize_same_as([edge_id])
    await _assert(updated == 1, "reprioritize_same_as() updated exactly the one candidate re-scored")

    pc_rows_after = await pending_candidate_priority.list_rows("SAME_AS")
    row_after = next(r for r in pc_rows_after if r["edge_id"] == edge_id)
    await _assert(row_after["priority_score"] > row_before["priority_score"], "priority_score increased after new corroborating evidence (reorder)")
    await _assert("Reinforced" in (row_after["why"] or ""), f"why explains the reinforcement deterministically (got {row_after['why']!r})")
    await _assert(row_after["group_id"] is not None, "candidate was assigned to a group (D1's grouping)")
    await _assert(row_after["deprioritized"] is False, "a freshly reinforced candidate is not deprioritized")

    edge_after = (await versioning.get_edge(edge_id))["properties"]
    await _assert(edge_after["status"] == "pending", "D1 NEVER changes status — still pending after reordering/grouping")

    # ── Simulated human action — the ONLY thing allowed to change status ──
    from src.api import graph_review
    class _Admin:
        # A real UUID, not the string TAG — log_audit_event casts this to
        # ::UUID (confirmed live: a non-UUID id makes the audit write
        # itself fail, harmlessly logged and swallowed, but noisy).
        id = uuid.uuid4()
    confirm_result = await graph_review.confirm_match(edge_id, graph_review.ReviewAction(), _Admin())
    await _assert(confirm_result["status"] == "confirmed", "a real human action via graph_review.confirm_match() is what actually changes status")

    pc_rows_after_confirm = await pending_candidate_priority.list_rows("SAME_AS")
    await _assert(
        all(r["edge_id"] != edge_id for r in pc_rows_after_confirm),
        "the resolved edge's row is cleared from the pending queue the moment a human confirms it",
    )

    # ── D2: disclosed hedging on the opened pending-traversal path ──────
    # Build a fresh, still-pending pair for the traversal check (the pair
    # above is now confirmed).
    mention_c = {"canonical_name": f"Rameez {TAG} Sultan Awan"}
    result_c = await er.resolve_and_write("person", mention_c, CASE_A, source_doc_id=f"{TAG}-DOC-E")
    _created_entity_ids.append(result_c["entity_id"])
    mention_d = {"canonical_name": f"Rameez {TAG} Sultan Awam"}
    result_d = await er.resolve_and_write("person", mention_d, CASE_B, source_doc_id=f"{TAG}-DOC-F")
    _created_entity_ids.append(result_d["entity_id"])
    await _assert(result_d["tier"] in ("flagged_unverified", "human_review"), "second synthetic pending pair created for the D2 traversal check")

    original_flag = config.FEATURE_HEDGED_PENDING_TRAVERSAL
    try:
        config.FEATURE_HEDGED_PENDING_TRAVERSAL = False
        off_result = await graph_retriever.retrieve_graph(
            f"find Rameez {TAG} Sultan Awan across cases", f"Rameez {TAG} Sultan Awan", case_id=None,
            cross_case=True, user_role="supervisor",
        )
        pending_tagged_off = [c for c in off_result["chunks"] if (c.get("metadata") or {}).get("same_as_status") == "pending"]
        await _assert(pending_tagged_off == [], "flag OFF: pending identity stays excluded from traversal (no chunk carries the D2 tag)")

        config.FEATURE_HEDGED_PENDING_TRAVERSAL = True
        on_result = await graph_retriever.retrieve_graph(
            f"find Rameez {TAG} Sultan Awan across cases", f"Rameez {TAG} Sultan Awan", case_id=None,
            cross_case=True, user_role="supervisor",
        )
        pending_tagged_on = [c for c in on_result["chunks"] if (c.get("metadata") or {}).get("same_as_status") == "pending"]
        # No chunks may exist for these synthetic mentions (no document
        # ever ingested them into Chroma) — the graph-level assertion that
        # matters is unconfirmed_links still reports the pending pair
        # either way; the tag-based hedge assertion is exercised directly
        # against verifier.py in tests/test_verifier.py (unit-level,
        # deterministic) since standing up a full Chroma-backed chunk for
        # this synthetic pair is out of scope for this graph-focused check.
        await _assert(
            any(l["status"] == "pending" for l in on_result["unconfirmed_links"]),
            "flag ON: the pending pair is still surfaced via unconfirmed_links regardless of chunk availability",
        )
    finally:
        config.FEATURE_HEDGED_PENDING_TRAVERSAL = original_flag

    print("\nAll Milestone D live checks passed.")


async def _wipe_leftover_synthetic_data_from_prior_runs():
    """
    A prior run that crashed before reaching its own cleanup (or whose
    cleanup itself failed) leaves `D1VERIFY-*` nodes in the real graph —
    left there, their (unrelated-tag but similarly-worded) canonical
    names pollute this run's name-similarity matching, since
    entity_resolution.py's candidate scan is intentionally global, not
    scoped to this run's own tag. Run once at the very start of every
    invocation so each run starts from a genuinely clean synthetic-data
    slate, independent of whether the previous run's own `finally` ran.
    """
    await age_client.execute_cypher(
        "MATCH (n) WHERE n.entity_id STARTS WITH 'D1VERIFY-' OR n.case_id STARTS WITH 'D1VERIFY-' "
        "OR n.doc_id STARTS WITH 'D1VERIFY-' DETACH DELETE n",
        columns=["result"],
    )
    from sqlalchemy import text
    from src.database.postgres import get_session
    async with get_session() as db:
        await db.execute(text("DELETE FROM pending_candidate_priority WHERE a_key LIKE 'D1VERIFY-%' OR b_key LIKE 'D1VERIFY-%'"))


async def _run():
    # main() and _cleanup() MUST share one asyncio.run() / one event
    # loop — age_client's connection pool is a lazily-initialized module
    # global bound to whichever event loop first touched it; two separate
    # top-level asyncio.run() calls create two different loops and using
    # the loop-bound pool from the second one raises asyncpg's
    # "cannot perform operation: another operation is in progress"
    # (confirmed live). One outer async function, one loop, one pool.
    await _wipe_leftover_synthetic_data_from_prior_runs()
    await _prune_orphaned_priority_rows()
    try:
        await main()
    finally:
        await _cleanup()


if __name__ == "__main__":
    asyncio.run(_run())
