# ============================================================
# Read-only integrity audit over CONFIRMED Person SAME_AS edges.
#
# WHY THIS EXISTS:
# The duplicate-Person backlog (findings.md Module 11, CASE_fir-1001-26_
# PERSON_REVIEW.md) is drained by bulk-confirming pending SAME_AS edges —
# scripts/collapse_same_document_duplicate_persons.py (pass 1) and
# scripts/collapse_cross_document_duplicate_persons.py (pass 2). Both
# scripts enforce a hard veto at confirm-time: if both endpoints carry a
# CNIC and the CNICs differ, the pair is REJECTED, never confirmed — see
# each script's own "guard 5" / "_cnic_conflict()". A third-party dump
# compared against this graph (see CASE_fir-1001-26_PERSON_REVIEW.md) was
# found to carry confirmed SAME_AS edges that looked like exactly this
# invariant being violated — auto-confirmed replay duplicates standing in
# as genuine identity matches. That dump's mutation history is opaque
# (unknown tooling, unknown guards), so its confirmed edges cannot be
# trusted by inspection alone.
#
# This script answers one question about OUR OWN graph, independent of
# which tool produced a confirmed edge: does every currently-confirmed,
# non-superseded Person SAME_AS edge still satisfy the CNIC hard veto?
# A hit here means two nodes with different national ID numbers are
# presently being treated as the same canonical person — via
# community_detection.build_canonical_map()'s read-time collapse — which
# is exactly the "two men, two different CNICs, silently fused" failure
# the veto exists to prevent, regardless of which script (or manual
# review click) produced the edge.
#
# This script WRITES NOTHING — it only reads AGE and reports. Any finding
# it surfaces needs a human decision (supersede/reject via
# src/api/graph_review.py) to fix; this script does not make that call.
#
# The core query/rule lives in src/graph/same_as_integrity.py
# (GRAPH_QUALITY_VISIBILITY_FIX_PROMPT.md, Feature B) — factored out so
# src/graph/ingestion_circuit_breaker.py can run the SAME case-scoped
# check automatically at the end of every case-scoped ingestion run,
# without duplicating the query. This script is now a thin CLI wrapper
# around that module for manual/periodic full-graph use.
#
# Run: python scripts/audit_confirmed_same_as_integrity.py
#      python scripts/audit_confirmed_same_as_integrity.py --case fir-1001-26
#
# Exit code: 0 if no CNIC-conflict findings, 1 if any were found — so this
# can be wired into a CI/verification job the way verify_milestone_d.py
# is, without a human needing to read output to know something's wrong.
# ============================================================

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.graph import age_client  # noqa: F401 — re-exported for tests that patch it here
from src.graph.same_as_integrity import cnic_conflict as _cnic_conflict
from src.graph.same_as_integrity import fetch_confirmed as _fetch_confirmed


def _case_arg() -> str | None:
    for i, arg in enumerate(sys.argv):
        if arg == "--case" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith("--case="):
            return arg.split("=", 1)[1]
    return None


async def main() -> int:
    case_id = _case_arg()
    rows = await _fetch_confirmed(case_id)
    scope = f"case {case_id}" if case_id else "all cases"
    print(f"Confirmed, non-superseded Person SAME_AS edges ({scope}): {len(rows)}")

    violations = [r for r in rows if _cnic_conflict(r)]

    if not violations:
        print("No CNIC-conflict violations found among confirmed edges.")
        return 0

    print(f"\nCNIC-CONFLICT VIOLATIONS: {len(violations)} confirmed edge(s) merge two "
          f"different national ID numbers — these should never have been confirmed.\n")
    for r in violations:
        print(
            f"  edge_id={r['edge_id']} tier={r['tier']!r} reviewed_by={r['reviewed_by']!r}\n"
            f"    a: {r['a_id']} {r['a_name']!r} cnic={r['a_cnic']!r} doc={r['a_doc']!r}\n"
            f"    b: {r['b_id']} {r['b_name']!r} cnic={r['b_cnic']!r} doc={r['b_doc']!r}\n"
        )
    print(
        "Each of these needs a human decision via src/api/graph_review.py "
        "(supersede/reject) — this script only reports, it does not mutate."
    )
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
