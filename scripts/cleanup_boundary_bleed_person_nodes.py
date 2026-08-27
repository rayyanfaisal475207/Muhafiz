# ============================================================
# One-off data cleanup: remove Person nodes whose canonical_name is a
# confirmed extraction-boundary-bleed artifact (findings.md Module 11),
# not a real name.
#
# Fixed at the source in src/extraction/ner.py (_STOPWORDS/
# _NON_NAME_CONTENT_WORDS additions) — this script only cleans up nodes
# that were already written to the graph BEFORE that fix landed. New
# ingestions can no longer produce these specific strings.
#
# Every string below was live-verified against the real graph (case
# fir-1001-26) and its actual source-chunk context via
# scripts/review_case_person_duplicates.py before being added here —
# this is a fixed, narrow list of confirmed-noise strings, not a
# generalizable filter (same "growing blocklist, not a generalizable
# fix" limitation community_detection.py's own _NON_NAME_PHRASES already
# documents, accepted here for the same reason).
#
#   قبضے                    "possession/custody" — common noun
#   تحت فیصل                 "under" + name bleed (کے تحت فیصل ولد ...)
#   بجے فیصل                 "o'clock" + name bleed (17:10 بجے فیصل ولد ...)
#   محمد رمضان ساکنہ محلہ      name + "resident of" run-together
#
# "مدعی فیصل" (role marker + name bleed, مدعی فیصل ولد ...) was ORIGINALLY
# on this list and has been REMOVED — a teammate's domain review against
# the FIR narrative and the Muhafiz API record (HANDOFF_TO_TEAMMATE.md,
# 2026-08-27) found it is NOT a grammar fragment: "مدعی" means
# "complainant" and "مدعی فیصل" is a role-prefixed mention of the SAME
# real complainant as the clean "فیصل" node (CNIC 00000-9000057-1), not
# noise. Deleting a node matching this string would have deleted a real
# person's mention, not an artifact — confirmed live no such node
# currently exists in the graph, so removing it from this list here cost
# nothing. src/extraction/ner.py's _ROLE_MARKERS trim set already strips
# "مدعی" from NEW extractions at the source; this list only ever existed
# to clean up nodes written before that fix landed.
#
# Corpus-wide by canonical_name match, not restricted to one case_id —
# these four strings are not real names anywhere they'd occur, so a
# match elsewhere in the corpus would be the same defect, not a
# different one.
#
# DETACH DELETE, not a SAME_AS-confirm — unlike
# scripts/collapse_same_document_duplicate_persons.py (which merges
# duplicate mentions of a REAL person), these nodes aren't a person at
# all, so there's nothing to canonicalize toward; removing them (and
# their now-meaningless APPEARS_IN/BELONGS_TO_CASE/SAME_AS edges) is the
# correct action, not a merge.
#
# Same dry-run/apply convention as scripts/cleanup_orphaned_person_nodes.py:
# a real, destructive, hard-to-reverse graph mutation — run --dry-run
# first, read the report, get explicit go-ahead before --apply.
#
# Run: python scripts/cleanup_boundary_bleed_person_nodes.py --dry-run
#      python scripts/cleanup_boundary_bleed_person_nodes.py --apply
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

from src.graph import age_client

_NOISE_STRINGS = [
    "قبضے",
    "تحت فیصل",
    "بجے فیصل",
    "محمد رمضان ساکنہ محلہ",
]

_FETCH_QUERY = (
    "MATCH (p:Person) WHERE p.canonical_name IN $names "
    "RETURN p.entity_id AS entity_id, p.canonical_name AS canonical_name, "
    "p.source_doc_id AS source_doc_id"
)


async def _fetch_matching_nodes() -> list[dict]:
    return await age_client.execute_cypher(
        _FETCH_QUERY,
        params={"names": _NOISE_STRINGS},
        columns=["entity_id", "canonical_name", "source_doc_id"],
    )


async def main() -> None:
    apply = "--apply" in sys.argv
    dry_run = not apply

    rows = await _fetch_matching_nodes()
    by_entity: dict[str, dict] = {r["entity_id"]: r for r in rows}
    nodes = list(by_entity.values())

    print(f"Person nodes matching confirmed noise strings: {len(nodes)}")
    counts = Counter(n["canonical_name"] for n in nodes)
    for name, count in counts.most_common():
        print(f"  {name!r}: {count}")

    by_doc = Counter(n["source_doc_id"] for n in nodes)
    print(f"\nGrouped by source_doc_id:")
    for doc_id, count in by_doc.most_common(10):
        print(f"  {doc_id}: {count}")

    if not nodes:
        print("\nNothing to remove.")
        return

    if dry_run:
        print(f"\nDRY RUN — no changes made. Re-run with --apply to DETACH DELETE these {len(nodes)} nodes.")
        return

    print(f"\nApplying: DETACH DELETE on {len(nodes)} nodes...")
    result = await age_client.execute_cypher(
        "MATCH (p:Person) WHERE p.entity_id IN $ids DETACH DELETE p RETURN count(p) AS deleted",
        params={"ids": list(by_entity.keys())},
        columns=["deleted"],
    )
    print(f"Delete call result: {result}")

    remaining = await _fetch_matching_nodes()
    print(f"\nMatching nodes remaining (should be 0): {len(remaining)}")
    if remaining:
        print("  (unexpected — investigate before assuming cleanup is complete)")


if __name__ == "__main__":
    asyncio.run(main())
