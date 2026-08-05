# ============================================================
# Community detection staleness check (Section 2, GraphRAG-inspired layer)
#
# There is no separate worker/queue in this project (see RUN.md §5 —
# background work runs on the backend's own event loop), so
# community_detection.detect_communities() is admin-invoked, not
# scheduled — a deliberate Stage 1 design decision, not an oversight.
# This script is the manual pre-flight check that decision implies:
# compare the live graph's current raw Person node/edge counts against
# the raw counts recorded at the last community_runs row (migration 017
# — node_count/edge_count on that table are POST-filter counts from the
# clustered graph, not directly comparable to the live graph's raw
# counts; raw_node_count/raw_edge_count are the apples-to-apples baseline
# this check needs), and warn when the graph has drifted enough that a
# re-run is worth doing.
#
# No new infrastructure — run this alongside scripts/reingest_kb.py, or
# on its own after a bulk ingestion pass, and re-run
# community_detection.detect_communities() /
# community_summarization.summarize_communities() manually if it warns.
#
# Run manually: python scripts/check_community_staleness.py
# ============================================================

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Force UTF-8 output — a Windows console defaults to cp1252, which
# crashes on the ⚠/✓ characters below (and on Urdu text elsewhere in this
# codebase's other scripts) unless reconfigured explicitly. Same fix
# scripts/apply_migration.py already applies, for the same reason.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.graph import age_client, community_detection

# A community graph rebuilt from a near-identical Person/edge population
# would produce a near-identical partition — not worth the LLM cost of
# re-summarizing every community. These thresholds are a starting point
# (documented as such, not tuned against real drift data yet, since this
# is the first version of this check) — revisit once there's a real
# history of community_runs rows to look at.
_NODE_DRIFT_WARN_PCT = 0.10
_EDGE_DRIFT_WARN_PCT = 0.10


async def _current_raw_node_count() -> int:
    rows = await age_client.execute_cypher(
        "MATCH (p:Person) RETURN count(p) AS c", columns=["c"],
    )
    return int(rows[0]["c"]) if rows else 0


async def _current_raw_edge_count() -> int:
    # ASSOCIATED_WITH + BELONGS_TO_CASE (Person only) — the same two edge
    # sources community_detection.detect_communities() itself measures
    # into raw_node_count/raw_edge_count (migration 017), for a genuinely
    # comparable baseline. Unrelated edge types (APPEARS_IN, OCCURRED_ON,
    # ...) don't affect community structure, so counting those wouldn't
    # be a meaningful staleness signal.
    rows = await age_client.execute_cypher(
        "MATCH (:Person)-[r:ASSOCIATED_WITH]->(:Person) WHERE r.superseded_by IS NULL "
        "RETURN count(r) AS c",
        columns=["c"],
    )
    associated_with = int(rows[0]["c"]) if rows else 0
    rows2 = await age_client.execute_cypher(
        "MATCH (:Person)-[r:BELONGS_TO_CASE]->(:Case) WHERE r.superseded_by IS NULL "
        "RETURN count(r) AS c",
        columns=["c"],
    )
    belongs_to_case = int(rows2[0]["c"]) if rows2 else 0
    return associated_with + belongs_to_case


async def main() -> None:
    last_run = await community_detection.get_latest_run()
    if last_run is None:
        print("No community detection run found yet — run detect_communities() first, not this check.")
        return

    prior_raw_nodes = last_run.get("raw_node_count")
    prior_raw_edges = last_run.get("raw_edge_count")

    print(f"Last run: {last_run['run_id']} (computed {last_run['computed_at']})")
    print(f"  Post-filter clustered graph at that run: {last_run['node_count']} nodes, {last_run['edge_count']} edges")

    if prior_raw_nodes is None or prior_raw_edges is None:
        # A community_runs row from before migration 017 has no raw
        # counts to compare against — fail toward "recommend a re-run"
        # rather than silently skipping the check or crashing on a
        # None/int comparison.
        print(
            "  Raw pre-filter counts weren't recorded for this run (predates migration 017).\n"
            "  ⚠  STALE (no comparable baseline) — recommend re-running detect_communities()."
        )
        sys.exit(1)

    current_raw_nodes = await _current_raw_node_count()
    current_raw_edges = await _current_raw_edge_count()

    print(f"  Raw graph at that run: {prior_raw_nodes} Person nodes, {prior_raw_edges} ASSOCIATED_WITH+BELONGS_TO_CASE edges")
    print(f"  Raw graph right now:   {current_raw_nodes} Person nodes, {current_raw_edges} ASSOCIATED_WITH+BELONGS_TO_CASE edges")

    node_drift = abs(current_raw_nodes - prior_raw_nodes) / max(prior_raw_nodes, 1)
    edge_drift = abs(current_raw_edges - prior_raw_edges) / max(prior_raw_edges, 1)

    stale = node_drift >= _NODE_DRIFT_WARN_PCT or edge_drift >= _EDGE_DRIFT_WARN_PCT
    print(f"\nNode drift: {node_drift:.1%}   Edge drift: {edge_drift:.1%}")
    if stale:
        print(
            "\n⚠  STALE — the graph has drifted enough that community detection is "
            "likely out of date. Re-run:\n"
            "    python -c \"import asyncio; from src.graph.community_detection import detect_communities; "
            "asyncio.run(detect_communities())\"\n"
            "  followed by:\n"
            "    python -c \"import asyncio; from src.graph.community_summarization import summarize_communities; "
            "asyncio.run(summarize_communities())\""
        )
        sys.exit(1)
    else:
        print("\n✓ Community detection is reasonably current — no action needed.")


if __name__ == "__main__":
    asyncio.run(main())
