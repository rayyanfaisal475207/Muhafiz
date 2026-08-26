# ============================================================
# One-off data cleanup: confirm the SAME_AS edges behind
# findings.md Module 11's own live-reproduced explosion — one document
# (fir-1001-26's narrative) minted 368 near-duplicate Person nodes across
# 5 distinct name strings for what the community summarizer itself
# already recognized as one real person ("Muhammad Ramadan is the only
# named individual connected to this case").
#
# WHY CONFIRMING PENDING SAME_AS EDGES IS SUFFICIENT, NOT A NODE MERGE:
# entity_resolution.py's own docstring is explicit — "name-fallback tiers
# never physically merge into the candidate node" — confirming a SAME_AS
# match writes a NEW edge with status='confirmed' (versioning.py's
# append-only pattern, src/api/graph_review.py::confirm_match()), it does
# NOT delete or merge any node. community_detection.py's own
# build_canonical_map() ALREADY collapses confirmed, non-superseded
# SAME_AS components into one canonical id at READ time, before
# clustering — so confirming the right edges here directly fixes
# Module 9's own downstream symptom (one 368-member community dominating
# the graph's density) without inventing any new merge machinery: this
# script is a thin, narrowly-scoped bulk-confirm over EXISTING review-
# queue machinery (src/api/graph_review.py::confirm_match(), the same
# function a human clicking "Confirm" in the admin UI calls), not a new
# resolution mechanism.
#
# THE SAFETY BAR (deliberately narrow — see findings.md Module 11's own
# "Design options" section): only a PENDING SAME_AS edge where BOTH
# endpoints share the exact same source_doc_id AND the exact same
# case_id qualifies. Same-document + same-case is a materially stronger,
# more objective signal than the name-similarity score alone (which is
# all confirm_match() would otherwise be trusting) — this is NOT a
# general "auto-confirm the review queue" tool, and does not touch a
# single cross-document or cross-case pending match.
#
# Same precedented shape as scripts/cleanup_orphaned_person_nodes.py:
# --dry-run reports what would be confirmed and why, --apply calls the
# real graph_review.confirm_match() for each qualifying edge. This is a
# real, hard-to-reverse graph mutation (a confirmed SAME_AS edge changes
# read-time canonicalization everywhere it's consulted) — run --dry-run
# first, read the report, get explicit go-ahead before --apply.
#
# Run: python scripts/collapse_same_document_duplicate_persons.py --dry-run
#      python scripts/collapse_same_document_duplicate_persons.py --apply \
#          --admin-email you@example.com
#
# --admin-email is REQUIRED for --apply: the confirmation is stamped onto
# every edge it touches (reviewed_by) and written to the append-only audit
# log, so it must name a real, accountable platform-admin. See
# scripts/_script_admin.py for the live audit gap that made this mandatory.
# ============================================================

import asyncio
import sys
import uuid
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from scripts._script_admin import AdminIdentityError, resolve_admin
from src.graph import age_client

_SAFE_SUBSET_QUERY = (
    "MATCH (a:Person)-[r:SAME_AS]->(b:Person) "
    "WHERE r.status = 'pending' AND r.superseded_by IS NULL "
    "  AND a.source_doc_id IS NOT NULL AND a.source_doc_id = b.source_doc_id "
    "MATCH (a)-[ba:BELONGS_TO_CASE]->(ca:Case) WHERE ba.superseded_by IS NULL "
    "MATCH (b)-[bb:BELONGS_TO_CASE]->(cb:Case) WHERE bb.superseded_by IS NULL "
    "  AND ca.case_id = cb.case_id "
    "RETURN id(r) AS edge_id, a.entity_id AS mention_id, a.canonical_name AS mention_name, "
    "b.entity_id AS candidate_id, b.canonical_name AS candidate_name, "
    "a.source_doc_id AS source_doc_id, ca.case_id AS case_id, r.tier AS tier"
)


async def _fetch_qualifying_edges() -> list[dict]:
    rows = await age_client.execute_cypher(
        _SAFE_SUBSET_QUERY,
        columns=[
            "edge_id", "mention_id", "mention_name", "candidate_id", "candidate_name",
            "source_doc_id", "case_id", "tier",
        ],
    )
    return rows


def _admin_email_arg() -> str:
    """--admin-email <email>, required for --apply. See _script_admin.py."""
    for i, arg in enumerate(sys.argv):
        if arg == "--admin-email" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith("--admin-email="):
            return arg.split("=", 1)[1]
    return ""


async def main() -> None:
    apply = "--apply" in sys.argv
    dry_run = not apply

    # [audit-gap fix] Resolve a REAL admin BEFORE any mutation. This used
    # to be a locally-minted uuid.uuid4(), whose comment claimed the
    # resulting audit failure was "harmlessly logged and swallowed, but
    # noisy". That was wrong: measured on a real run, 103 confirmations
    # landed in the graph with ZERO audit records, and the fake id was
    # stamped onto every edge as `reviewed_by`. See scripts/_script_admin.py.
    admin = None
    if apply:
        try:
            admin = await resolve_admin(_admin_email_arg())
        except AdminIdentityError as exc:
            print(f"REFUSING TO APPLY — {exc}")
            return
        print(f"Acting as: {admin.email} ({admin.role}, id={admin.id})\n")

    edges = await _fetch_qualifying_edges()

    print(f"Qualifying pending SAME_AS edges (same source_doc_id AND same case_id, both endpoints): {len(edges)}")

    by_group = Counter((e["case_id"], e["source_doc_id"]) for e in edges)
    print(f"\nGrouped by (case_id, source_doc_id):")
    for (case_id, doc_id), count in by_group.most_common(10):
        print(f"  {case_id} / {doc_id}: {count} edges")

    tier_counts = Counter(e["tier"] for e in edges)
    print(f"\nBy tier: {dict(tier_counts)}")

    print(f"\nSample (up to 10):")
    for e in edges[:10]:
        print(f"  edge_id={e['edge_id']} tier={e['tier']!r}: {e['mention_name']!r} ({e['mention_id']}) "
              f"<-> {e['candidate_name']!r} ({e['candidate_id']})")

    if not edges:
        print("\nNothing to confirm.")
        return

    if dry_run:
        print(f"\nDRY RUN — no changes made. Re-run with --apply to confirm these {len(edges)} edges.")
        return

    from src.api import graph_review

    print(f"\nApplying: confirm_match() on {len(edges)} edges as {admin.email}...")
    action = graph_review.ReviewAction()
    confirmed = 0
    errors: list[dict] = []
    for e in edges:
        try:
            await graph_review.confirm_match(e["edge_id"], action, admin)
            confirmed += 1
        except Exception as exc:
            errors.append({"edge_id": e["edge_id"], "error": str(exc)})

    print(f"Confirmed: {confirmed} / {len(edges)}")
    if errors:
        print(f"Errors ({len(errors)}):")
        for err in errors[:20]:
            print(f"  edge_id={err['edge_id']}: {err['error']}")

    remaining = await _fetch_qualifying_edges()
    print(f"\nQualifying pending edges remaining (should be 0 or == error count): {len(remaining)}")


if __name__ == "__main__":
    asyncio.run(main())
