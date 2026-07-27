"""
Purge eval-harness contamination from the production Apache AGE graph
(Phase 3, Module 3.2 — see solution.md and issues.md's Critical "Apache AGE
graph contains synthetic eval-harness test fixtures permanently written
into real cases" finding).

Module 3.1 (migration 011_age_eval_graph.sql) closed the ongoing-damage
landmine by giving the eval harness its own physically separate graph
(evidence_graph_eval) going forward. This script is the cleanup half: it
removes the EVAL-* fixtures that were written into the real evidence_graph
by earlier runs of scripts/eval_entity_resolution.py, before that isolation
existed.

IDENTIFICATION: source_doc_id STARTS WITH 'EVAL-' — never the
source_chunk_id IS NULL proxy. Reconciliation (2026-07-28, live evidence_graph)
confirmed why these two predicates disagree (41 vs 26 for Person, at time of
writing): 15 real, non-eval Person nodes ALSO have source_chunk_id IS NULL,
for an unrelated data-quality reason (they are NER false positives — e.g.
canonical_name values like "Golra" (a place name), "Inspector", "میرے گھر"
("my house") — not people at all, and not eval fixtures). Zero EVAL-*
fixtures were found with a non-NULL source_chunk_id (the reverse check).
This confirms: (a) the prefix-based identification below is correct and
will not touch these 15 real (if low-quality) entities, and (b) the "no
source_chunk_id => never citable" safety property has a second, unrelated
cause worth its own follow-up — flagged in issues.md, not fixed here.

Deletes edges before nodes (never orphan mid-delete). NEVER deletes Case
nodes — only their spurious BELONGS_TO_CASE/SAME_AS edges to fixture
entities are removed; Case nodes carry no source_doc_id property at all
and are excluded from the node-scan label list as well, as a second,
structural safeguard on top of that.

Two explicit modes — dry-run is the default; the real delete requires
BOTH --execute and --yes-i-am-sure, so this can never fire destructively
by accident or by a copy-pasted command missing one flag:

    python scripts/purge_eval_contamination.py                  # dry run (counts only)
    python scripts/purge_eval_contamination.py --execute --yes-i-am-sure   # real DELETE

Take a full pg_dump backup of the instance immediately before running with
--execute — Cypher DELETE is not otherwise reversible.
"""
import argparse
import asyncio
import sys
from pathlib import Path

# Force UTF-8 stdout — several real (and some eval) entity canonical_names
# are Urdu, and a Windows console's default cp1252 encoding crashes mid-run
# otherwise (same fix as scripts/apply_migration.py / eval_entity_resolution.py).
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.graph import age_client

GRAPH = age_client.GRAPH_NAME  # "evidence_graph" — the real, production graph.

# Every vertex label in the schema (migrations/005_age_graph.sql) EXCEPT
# "Case" — Case nodes are structurally excluded from the node-scan list,
# not just skipped by a property check, so a future edit to this file
# cannot accidentally start matching them. "Date"/"DebugTest" are two
# extra ad-hoc labels observed live (created via AGE's lazy label
# creation, outside migration 005/011's static list) — included here so
# the scan covers everything actually present, not just the documented set.
NODE_LABELS = [
    "Person", "Vehicle", "PhoneNumber", "Address", "Organization",
    "Weapon", "Incident", "Document", "StructuredRecord", "Date", "DebugTest",
]

EDGE_LABELS = [
    "BELONGS_TO_CASE", "APPEARS_IN", "ASSOCIATED_WITH", "SAME_AS", "OWNS",
    "REGISTERED_TO", "LOCATED_AT", "INVOLVED_IN", "PART_OF", "OCCURRED_ON",
    "CONFLICTS_WITH",
]

# 2026-07-27 audit snapshot (issues.md / solution.md Module 3.2) — printed
# alongside live counts so any drift since the audit is visible, not to
# assert the live counts must match exactly (real ingestion has continued
# since the audit; growth in real-data labels is expected and fine).
AUDIT_SNAPSHOT = {
    "Document": 72,
    "Person": 26,  # reconciled 2026-07-28: 26 is correct (source_doc_id-based), not 33/41
    "Vehicle": 8,
    "PhoneNumber": 11,
    "Organization": 6,
    "Address": 10,
    "BELONGS_TO_CASE": 144,
    "SAME_AS": 88,
}


async def _count_eval_nodes(label: str) -> int:
    rows = await age_client.execute_cypher(
        f"MATCH (n:{label}) WHERE n.source_doc_id STARTS WITH 'EVAL-' RETURN count(n)",
        columns=["c"],
        graph=GRAPH,
    )
    return int(rows[0]["c"]) if rows else 0


async def _count_eval_edges(label: str) -> int:
    rows = await age_client.execute_cypher(
        f"""
        MATCH (a)-[r:{label}]->(b)
        WHERE r.source_doc_id STARTS WITH 'EVAL-'
           OR a.source_doc_id STARTS WITH 'EVAL-'
           OR b.source_doc_id STARTS WITH 'EVAL-'
        RETURN count(r)
        """,
        columns=["c"],
        graph=GRAPH,
    )
    return int(rows[0]["c"]) if rows else 0


async def _count_eval_case_nodes() -> int:
    """Defense-in-depth positive check: confirm zero Case nodes would ever
    match the EVAL- prefix (Case nodes carry no source_doc_id property at
    all, so this should always be 0) — Case is never in NODE_LABELS, this
    just makes the guarantee explicit and verifiable rather than implicit."""
    rows = await age_client.execute_cypher(
        "MATCH (n:Case) WHERE n.source_doc_id STARTS WITH 'EVAL-' RETURN count(n)",
        columns=["c"],
        graph=GRAPH,
    )
    return int(rows[0]["c"]) if rows else 0


async def dry_run() -> dict:
    """Count-only pass — no DELETE anywhere. Returns {label: count} for
    reporting/comparison against AUDIT_SNAPSHOT."""
    print(f"=== DRY RUN — counting EVAL-* contamination in '{GRAPH}' (no deletes) ===\n")

    node_counts: dict[str, int] = {}
    print("Nodes (source_doc_id STARTS WITH 'EVAL-'):")
    for label in NODE_LABELS:
        c = await _count_eval_nodes(label)
        node_counts[label] = c
        snapshot = AUDIT_SNAPSHOT.get(label)
        marker = f" (audit snapshot: {snapshot})" if snapshot is not None else ""
        drift = ""
        if snapshot is not None and c != snapshot:
            drift = f"  <-- DRIFT from audit snapshot ({c} vs {snapshot})"
        print(f"  {label}: {c}{marker}{drift}")

    edge_counts: dict[str, int] = {}
    print("\nEdges (own or either endpoint's source_doc_id STARTS WITH 'EVAL-'):")
    for label in EDGE_LABELS:
        c = await _count_eval_edges(label)
        edge_counts[label] = c
        snapshot = AUDIT_SNAPSHOT.get(label)
        marker = f" (audit snapshot: {snapshot})" if snapshot is not None else ""
        drift = ""
        if snapshot is not None and c != snapshot:
            drift = f"  <-- DRIFT from audit snapshot ({c} vs {snapshot})"
        print(f"  {label}: {c}{marker}{drift}")

    case_check = await _count_eval_case_nodes()
    print(f"\nCase nodes matching EVAL- prefix (must be 0, Case is never deleted): {case_check}")
    if case_check != 0:
        print("  !!! UNEXPECTED: Case nodes should never carry source_doc_id. "
              "Investigate before proceeding to --execute. !!!")

    total_nodes = sum(node_counts.values())
    total_edges = sum(edge_counts.values())
    print(f"\nTOTAL: {total_nodes} nodes, {total_edges} edges would be deleted.")
    return {"nodes": node_counts, "edges": edge_counts, "case_check": case_check}


async def purge(dry_counts: dict) -> None:
    """Real DELETE — edges first (every label), then nodes (every label
    except Case). Only called from main() when --execute --yes-i-am-sure
    are both passed."""
    print(f"\n=== EXECUTING PURGE against '{GRAPH}' ===\n")

    print("Deleting edges...")
    for label in EDGE_LABELS:
        before = dry_counts["edges"].get(label, 0)
        if before == 0:
            continue
        rows = await age_client.execute_cypher(
            f"""
            MATCH (a)-[r:{label}]->(b)
            WHERE r.source_doc_id STARTS WITH 'EVAL-'
               OR a.source_doc_id STARTS WITH 'EVAL-'
               OR b.source_doc_id STARTS WITH 'EVAL-'
            DELETE r
            RETURN count(r)
            """,
            columns=["c"],
            graph=GRAPH,
        )
        deleted = int(rows[0]["c"]) if rows else 0
        print(f"  {label}: deleted {deleted} (expected {before})")

    print("\nDeleting nodes...")
    for label in NODE_LABELS:
        before = dry_counts["nodes"].get(label, 0)
        if before == 0:
            continue
        # DETACH DELETE as a second safety net: every edge touching an
        # EVAL-* node should already be gone from the pass above (any edge
        # with an EVAL-* endpoint was matched and deleted regardless of
        # the OTHER endpoint), so this should never actually detach a
        # remaining edge — but it means a plain DELETE can never fail with
        # a dangling-edge error if the edge pass above somehow missed one
        # (e.g. an edge label not in EDGE_LABELS).
        rows = await age_client.execute_cypher(
            f"""
            MATCH (n:{label})
            WHERE n.source_doc_id STARTS WITH 'EVAL-'
            DETACH DELETE n
            RETURN count(n)
            """,
            columns=["c"],
            graph=GRAPH,
        )
        deleted = int(rows[0]["c"]) if rows else 0
        print(f"  {label}: deleted {deleted} (expected {before})")

    print("\n=== Purge complete. Run this script again (dry-run) to confirm zero remain. ===")


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true",
                         help="Perform the real DELETE. Requires --yes-i-am-sure too.")
    parser.add_argument("--yes-i-am-sure", action="store_true",
                         help="Required alongside --execute to actually run the delete.")
    args = parser.parse_args()

    counts = await dry_run()

    if args.execute:
        if not args.yes_i_am_sure:
            print("\n--execute given without --yes-i-am-sure — refusing to delete anything. "
                  "Re-run with both flags once the dry-run counts above have been reviewed.")
            sys.exit(1)
        if counts["case_check"] != 0:
            print("\nRefusing to --execute: Case-node safety check above did not return 0.")
            sys.exit(1)
        await purge(counts)
    else:
        print("\n(Dry run only — no changes made. Re-run with --execute --yes-i-am-sure "
              "to perform the real delete, after taking a backup.)")

    await age_client.close_pool()


if __name__ == "__main__":
    asyncio.run(main())
