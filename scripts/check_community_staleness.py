# ============================================================
# Community detection staleness check (Section 2, GraphRAG-inspired layer)
#
# [Milestone E3 — GRAPH_SCALE_SCHEMA_EXPANSION_PLAN.md] The drift heuristic
# itself now lives in src/graph/community_detection.py's get_staleness() —
# the SAME function src/ingestion/community_refresh_bg.py's automatic
# incremental trigger calls after every ingest. This script is now a thin
# manual CLI wrapper around it, not a second copy of the logic — kept
# because a human occasionally wants to check staleness without waiting
# for (or forcing) an actual refresh, and because the manual full-sweep
# admin endpoint (POST /api/admin/community/refresh, src/api/community_admin.py)
# is API-only, with no CLI equivalent.
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

from src.graph import community_detection


async def main() -> None:
    result = await community_detection.get_staleness()

    if result["last_run_id"] is None:
        print("No community detection run found yet — run detect_communities() first, not this check.")
        sys.exit(1)

    print(f"Last run: {result['last_run_id']}")
    if result["prior_raw_nodes"] is None:
        print(
            "  Raw pre-filter counts weren't recorded for this run (predates migration 017).\n"
            "  ⚠  STALE (no comparable baseline) — recommend re-running detect_communities()."
        )
        sys.exit(1)

    print(
        f"  Raw graph at that run: {result['prior_raw_nodes']} Person nodes, "
        f"{result['prior_raw_edges']} ASSOCIATED_WITH+BELONGS_TO_CASE edges"
    )
    print(
        f"  Raw graph right now:   {result['current_raw_nodes']} Person nodes, "
        f"{result['current_raw_edges']} ASSOCIATED_WITH+BELONGS_TO_CASE edges"
    )
    print(f"\nNode drift: {result['node_drift']:.1%}   Edge drift: {result['edge_drift']:.1%}")

    if result["stale"]:
        print(
            "\n⚠  STALE — the graph has drifted enough that community detection is "
            "likely out of date. Re-run:\n"
            "    python -c \"import asyncio; from src.graph.community_detection import detect_communities; "
            "asyncio.run(detect_communities())\"\n"
            "  followed by:\n"
            "    python -c \"import asyncio; from src.graph.community_summarization import summarize_communities; "
            "asyncio.run(summarize_communities())\"\n"
            "  — or POST /api/admin/community/refresh (supervisor role) to do both in one call.\n"
            "  Note: src/ingestion/community_refresh_bg.py already does this automatically after "
            "ingestion crosses this same threshold — this script is for checking/forcing it by hand."
        )
        sys.exit(1)
    else:
        print("\n✓ Community detection is reasonably current — no action needed.")


if __name__ == "__main__":
    asyncio.run(main())
