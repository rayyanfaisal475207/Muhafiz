# ============================================================
# On-demand snapshot of the pending SAME_AS review-queue backlog
# (GRAPH_QUALITY_VISIBILITY_FIX_PROMPT.md, Feature A).
#
# Writes one row per (case_id, tier, status) tuple into
# same_as_queue_snapshot (migration 030), including a case_id IS NULL
# global rollup row — see src/graph/same_as_queue_history.py's own
# module docstring for why this is an on-demand script rather than a
# built-in scheduled job (this codebase has no cron/worker
# infrastructure — candidate_reprioritization.py's own docstring is the
# precedent). Run this periodically via an external scheduler (Windows
# Task Scheduler, cron outside this repo) or by hand.
#
# Read-only against AGE, insert-only against its own Postgres table —
# never touches a SAME_AS edge, never confirms/rejects/writes anything
# the graph itself would see.
#
# Run: python scripts/snapshot_same_as_queue.py
# ============================================================

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.graph import same_as_queue_history


async def main() -> None:
    written = await same_as_queue_history.write_snapshot()
    print(f"Wrote {written} snapshot row(s) to same_as_queue_snapshot.")

    global_rows = await same_as_queue_history.read_history(case_id=None, days=1)
    latest = [r for r in global_rows if r["snapshot_at"] == global_rows[0]["snapshot_at"]] if global_rows else []
    total_pending = sum(r["edge_count"] for r in latest if r["status"] == "pending")
    print(f"Current global pending SAME_AS count: {total_pending}")


if __name__ == "__main__":
    asyncio.run(main())
