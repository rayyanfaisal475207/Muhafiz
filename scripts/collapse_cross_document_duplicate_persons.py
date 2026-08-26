# ============================================================
# One-off data cleanup, pass 2 of 2 — the cross-document half of
# findings.md Module 11's duplicate-Person explosion.
#
# WHY A SECOND SCRIPT RATHER THAN WIDENING THE FIRST:
# scripts/collapse_same_document_duplicate_persons.py's safety bar is
# "both endpoints share the exact same source_doc_id", and that bar is
# precisely what makes it objectively safe — two mentions extracted from
# ONE document naming ONE person are the same person, with no judgement
# call. Loosening that bar in place would silently weaken every future
# run of it. This script takes the deliberately different, weaker case
# and pays for it with three additional guards (below), so the strong
# bar stays strong and the weaker case is auditable on its own terms.
#
# THE CASE THIS COVERS:
# One FIR is ingested along two provenance namespaces — the deterministic
# structured projection ("psrms/fir/{fir}#structured") and the narrative
# chunk ("psrms_fir_{fir}#narrative_{hash}", the sanitized namespace
# src/ingestion/document.py produces by replacing "/" with "_"). The same
# real person is therefore extracted twice, once from each, and the
# resulting pending SAME_AS between them can never qualify for pass 1's
# same-source_doc_id bar. Measured on the live graph: this is what leaves
# the fir-1001-26 families short of full collapse after pass 1.
#
# THE SAFETY BAR — all four must hold:
#   1. pending, non-superseded SAME_AS (same as pass 1)
#   2. byte-identical canonical_name on both endpoints — an exact string
#      match, never a similarity score
#   3. same case_id on both endpoints
#   4. both source_doc_ids reference that same case_id — i.e. these are
#      two documents OF THE SAME FIR, not two documents that merely
#      happen to sit in one case
# and one hard veto:
#   5. if BOTH endpoints carry a CNIC and the CNICs DIFFER, the pair is
#      REJECTED, never confirmed. Two same-named people in one FIR with
#      different national ID numbers are two people. This is the guard
#      that makes an exact-name match safe to act on; without it, a
#      genuine "two men both named محمد in one case" would be silently
#      fused into one person.
#
# Rejected pairs are PRINTED, not silently dropped — a skipped pair is a
# finding (a real same-name-different-CNIC collision worth a human look),
# not noise.
#
# Like pass 1, this confirms EXISTING pending edges via the same
# graph_review.confirm_match() a human clicking "Confirm" in the admin UI
# calls. It creates no new merge mechanism, deletes no node, and mutates
# no node property — community_detection.build_canonical_map() already
# collapses confirmed SAME_AS components to one canonical id at read
# time, which is what actually fixes retrieval/clustering.
#
# This is a real, hard-to-reverse graph mutation (a confirmed SAME_AS
# changes read-time canonicalization everywhere it is consulted) — run
# --dry-run first, read the report, get explicit go-ahead before --apply.
#
# Run: python scripts/collapse_cross_document_duplicate_persons.py --dry-run
#      python scripts/collapse_cross_document_duplicate_persons.py --apply \
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

# Directed, not undirected: AGE compiles `(a)-[r]-(b)` into a Cartesian
# product joined by an un-indexable OR of two AND-pairs (see
# graph_retriever._both_directions() for the measured root cause —
# 48s vs 65ms on this same graph). Every edge has exactly one stored
# direction, so a directed match reaches each pending SAME_AS exactly
# once, which is also what makes the confirm loop below idempotent.
_CANDIDATE_QUERY = (
    "MATCH (a:Person)-[r:SAME_AS]->(b:Person) "
    "WHERE r.status = 'pending' AND r.superseded_by IS NULL "
    "  AND a.canonical_name = b.canonical_name "                 # guard 2
    "  AND a.source_doc_id IS NOT NULL AND b.source_doc_id IS NOT NULL "
    "  AND a.source_doc_id <> b.source_doc_id "                  # the cross-doc case
    "MATCH (a)-[ba:BELONGS_TO_CASE]->(ca:Case) WHERE ba.superseded_by IS NULL "
    "MATCH (b)-[bb:BELONGS_TO_CASE]->(cb:Case) WHERE bb.superseded_by IS NULL "
    "  AND ca.case_id = cb.case_id "                             # guard 3
    "RETURN id(r) AS edge_id, a.entity_id AS a_id, b.entity_id AS b_id, "
    "a.canonical_name AS name, a.cnic AS a_cnic, b.cnic AS b_cnic, "
    "a.source_doc_id AS a_doc, b.source_doc_id AS b_doc, "
    "ca.case_id AS case_id, r.tier AS tier"
)


def _same_fir(row: dict) -> bool:
    """
    Guard 4 — both documents must belong to the SAME FIR, not merely the
    same case. The two real namespaces embed the case id directly
    ("psrms/fir/fir-1001-26#structured" and
    "psrms_fir_fir-1001-26#narrative_<hash>"), so requiring the case id to
    appear in both doc ids is an exact, non-heuristic test. The sanitized
    namespace replaces "/" with "_" but leaves the fir id itself intact,
    so one containment check covers both spellings.
    """
    case_id = row.get("case_id") or ""
    if not case_id:
        return False
    return case_id in (row.get("a_doc") or "") and case_id in (row.get("b_doc") or "")


def _cnic_conflict(row: dict) -> bool:
    """
    Guard 5 (hard veto) — both sides carry a CNIC and they disagree.
    A missing CNIC on either side is NOT a conflict: it is the normal
    case for narrative-extracted mentions, and absence of evidence is
    not evidence of a different person. Only a real, populated
    disagreement vetoes.
    """
    a, b = (row.get("a_cnic") or "").strip(), (row.get("b_cnic") or "").strip()
    return bool(a) and bool(b) and a != b


async def _fetch_candidates() -> tuple[list[dict], list[dict], list[dict]]:
    """Returns (qualifying, rejected_cnic_conflict, rejected_not_same_fir)."""
    rows = await age_client.execute_cypher(
        _CANDIDATE_QUERY,
        columns=[
            "edge_id", "a_id", "b_id", "name", "a_cnic", "b_cnic",
            "a_doc", "b_doc", "case_id", "tier",
        ],
    )
    qualifying, cnic_rejected, fir_rejected = [], [], []
    for row in rows:
        if _cnic_conflict(row):
            cnic_rejected.append(row)
        elif not _same_fir(row):
            fir_rejected.append(row)
        else:
            qualifying.append(row)
    return qualifying, cnic_rejected, fir_rejected


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

    # Resolve the acting admin BEFORE any mutation — a run that cannot be
    # attributed must not start, rather than confirm half the edges and
    # then discover it has no identity to record them under. See
    # scripts/_script_admin.py for the live audit gap this prevents.
    admin = None
    if apply:
        try:
            admin = await resolve_admin(_admin_email_arg())
        except AdminIdentityError as exc:
            print(f"REFUSING TO APPLY — {exc}")
            return
        print(f"Acting as: {admin.email} ({admin.role}, id={admin.id})\n")

    qualifying, cnic_rejected, fir_rejected = await _fetch_candidates()

    print(f"Cross-document pending SAME_AS, exact same name + same case: "
          f"{len(qualifying) + len(cnic_rejected) + len(fir_rejected)} examined")
    print(f"  QUALIFYING (will confirm)          : {len(qualifying)}")
    print(f"  REJECTED — CNIC conflict (guard 5) : {len(cnic_rejected)}")
    print(f"  REJECTED — not the same FIR (g. 4) : {len(fir_rejected)}")

    if qualifying:
        by_group = Counter((e["case_id"], e["name"]) for e in qualifying)
        print("\nQualifying, grouped by (case_id, name):")
        for (case_id, name), count in by_group.most_common(15):
            print(f"  {case_id} / {name!r}: {count} edges")

        print("\nSample (up to 10):")
        for e in qualifying[:10]:
            print(f"  edge_id={e['edge_id']} tier={e['tier']!r}: {e['name']!r}")
            print(f"      {e['a_id']}  [{e['a_doc']}]")
            print(f"      {e['b_id']}  [{e['b_doc']}]")

    # A rejection is a FINDING, not noise — print every one.
    if cnic_rejected:
        print(f"\n!! REJECTED, CNIC conflict — same name, same case, DIFFERENT national ID.")
        print("   These are very likely two different real people. Not confirmed by this")
        print("   script; they need a human look.")
        for e in cnic_rejected:
            print(f"   {e['name']!r}: {e['a_id']} cnic={e['a_cnic']!r} "
                  f"<-> {e['b_id']} cnic={e['b_cnic']!r}  (case {e['case_id']})")

    if fir_rejected:
        print(f"\n!! REJECTED, documents are not the same FIR — same name and case, but the")
        print("   two source documents do not both reference that case id.")
        for e in fir_rejected[:20]:
            print(f"   {e['name']!r}: [{e['a_doc']}] <-> [{e['b_doc']}]  (case {e['case_id']})")

    if not qualifying:
        print("\nNothing to confirm.")
        return

    if dry_run:
        print(f"\nDRY RUN — no changes made. Re-run with --apply to confirm these "
              f"{len(qualifying)} edges.")
        return

    from src.api import graph_review

    print(f"\nApplying: confirm_match() on {len(qualifying)} edges as {admin.email}...")
    action = graph_review.ReviewAction()
    confirmed = 0
    errors: list[dict] = []
    for e in qualifying:
        try:
            await graph_review.confirm_match(e["edge_id"], action, admin)
            confirmed += 1
        except Exception as exc:
            errors.append({"edge_id": e["edge_id"], "error": str(exc)})

    print(f"Confirmed: {confirmed} / {len(qualifying)}")
    if errors:
        print(f"Errors ({len(errors)}):")
        for err in errors[:20]:
            print(f"  edge_id={err['edge_id']}: {err['error']}")

    remaining, _, _ = await _fetch_candidates()
    print(f"\nQualifying edges remaining (should be 0 or == error count): {len(remaining)}")


if __name__ == "__main__":
    asyncio.run(main())
