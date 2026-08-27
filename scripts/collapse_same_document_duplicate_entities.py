# ============================================================
# One-off data cleanup: confirm the SAME_AS edges behind a duplicate-
# entity explosion for entity types OTHER than Person — same safety bar
# scripts/collapse_same_document_duplicate_persons.py already trusts,
# generalized past the Person-only label filter that script was
# originally scoped to.
#
# WHY A SEPARATE SCRIPT RATHER THAN WIDENING THE PERSON ONE:
# collapse_same_document_duplicate_persons.py is proven, tested, and
# scoped to :Person specifically — generalizing its label filter in
# place would risk that established behavior for no benefit. This
# script targets every OTHER entity label instead, with the identical
# safety bar, so the two never overlap and neither risks the other.
#
# THE PROBLEM THIS COVERS — measured live, not hypothetical:
# entity_resolution.TYPE_PRIMARY_ID_KEY has no entry for "location"/
# "organization"/etc. — only person (cnic), vehicle (plate), phone
# (phone), and officer (belt_no) ever get an exact-match auto-merge
# tier. Every OTHER entity type (Address most of all — canonical
# examples: "اسلام آباد"/Islamabad, "اقبال ٹاؤن"/Iqbal Town) has no hard
# identifier to auto-merge on, so a place name mentioned repeatedly
# throughout a document/corpus (an extremely common shape for the
# capital city's own name) mints a fresh node every time and a fresh
# pending SAME_AS candidate against it — with no idempotency-by-content
# dedup at the document level the way Person mentions get (service.py's
# document_resolved_persons cache is Person-only).
#
# Measured live on this graph before this script existed: 2,099 of
# 2,162 total pending SAME_AS edges (97%) were Address-Address pairs
# converging on just 3 canonical place names, and 2,097 of THOSE were
# byte-identical name matches — the exact same "no judgement call
# needed" shape the Person pass-1 script already exploits.  A further
# ~60 Officer-Officer pairs with byte-identical names (repeats of one
# officer's name within one document) qualify the same way.
#
# THE SAFETY BAR — identical to the Person script, no type-specific
# loosening:
#   1. pending, non-superseded SAME_AS
#   2. byte-identical canonical_name on both endpoints — an exact
#      string match, never a similarity score
#   3. same source_doc_id on both endpoints
#   4. same case_id on both endpoints
#   5. NOT :Person — that type is scripts/collapse_same_document_
#      duplicate_persons.py's own, separately-tested territory
#
# No CNIC-equivalent hard-conflict veto here (unlike the Person/Officer
# scripts) — Address/Organization have no structured identifier to
# conflict on in the first place; byte-identical name + same document +
# same case is already the strongest signal available for these types.
#
# Same confirm_match()-reuse discipline as every other collapse script:
# this creates no new merge mechanism, deletes no node, mutates no node
# property — community_detection.build_canonical_map() already collapses
# confirmed, non-superseded SAME_AS components at read time.
#
# Run: python scripts/collapse_same_document_duplicate_entities.py --dry-run
#      python scripts/collapse_same_document_duplicate_entities.py --apply \
#          --admin-email you@example.com
# ============================================================

import asyncio
import sys
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
    "MATCH (a)-[r:SAME_AS]->(b) "
    "WHERE r.status = 'pending' AND r.superseded_by IS NULL "
    "  AND labels(a)[0] <> 'Person' AND labels(b)[0] <> 'Person' "
    "  AND a.source_doc_id IS NOT NULL AND a.source_doc_id = b.source_doc_id "
    "  AND a.canonical_name = b.canonical_name "
    # [B-4-style guard] A parenthetical name is a role PLACEHOLDER — e.g.
    # "(نامزد ASI)" ("designated ASI", i.e. name-not-yet-known) — not a
    # real identity. Byte-identical placeholder text across two mentions
    # does NOT mean the same real officer; it very likely means two
    # DIFFERENT officers whose actual names simply weren't captured yet.
    # Excluded outright rather than guessed at, same "an incorrect
    # attachment is far more damaging than a missed one" discipline
    # _attach_chunk_identifiers() already applies.
    "  AND NOT a.canonical_name STARTS WITH '(' "
    "MATCH (a)-[ba:BELONGS_TO_CASE]->(ca:Case) WHERE ba.superseded_by IS NULL "
    "MATCH (b)-[bb:BELONGS_TO_CASE]->(cb:Case) WHERE bb.superseded_by IS NULL "
    "  AND ca.case_id = cb.case_id "
    "RETURN id(r) AS edge_id, labels(a) AS a_labels, a.entity_id AS mention_id, "
    "a.canonical_name AS mention_name, "
    "labels(b) AS b_labels, b.entity_id AS candidate_id, b.canonical_name AS candidate_name, "
    "a.source_doc_id AS source_doc_id, ca.case_id AS case_id, r.tier AS tier"
)


async def _fetch_qualifying_edges() -> list[dict]:
    rows = await age_client.execute_cypher(
        _SAFE_SUBSET_QUERY,
        columns=[
            "edge_id", "a_labels", "mention_id", "mention_name",
            "b_labels", "candidate_id", "candidate_name",
            "source_doc_id", "case_id", "tier",
        ],
    )
    return rows


def _admin_email_arg() -> str:
    for i, arg in enumerate(sys.argv):
        if arg == "--admin-email" and i + 1 < len(sys.argv):
            return sys.argv[i + 1]
        if arg.startswith("--admin-email="):
            return arg.split("=", 1)[1]
    return ""


async def main() -> None:
    apply = "--apply" in sys.argv
    dry_run = not apply

    admin = None
    if apply:
        try:
            admin = await resolve_admin(_admin_email_arg())
        except AdminIdentityError as exc:
            print(f"REFUSING TO APPLY — {exc}")
            return
        print(f"Acting as: {admin.email} ({admin.role}, id={admin.id})\n")

    edges = await _fetch_qualifying_edges()

    print(f"Qualifying pending SAME_AS edges (non-Person, same source_doc_id AND same case_id, exact name): {len(edges)}")

    by_label = Counter(tuple(e["a_labels"]) for e in edges)
    print(f"\nBy entity label:")
    for label, count in by_label.most_common(10):
        print(f"  {label}: {count} edges")

    by_target = Counter((tuple(e["a_labels"]), e["candidate_name"]) for e in edges)
    print(f"\nTop target entities (by mention count):")
    for (label, name), count in by_target.most_common(10):
        print(f"  {label} {name!r}: {count} mentions")

    tier_counts = Counter(e["tier"] for e in edges)
    print(f"\nBy tier: {dict(tier_counts)}")

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
        for err in errors[:10]:
            print(f"  {err}")


if __name__ == "__main__":
    asyncio.run(main())
