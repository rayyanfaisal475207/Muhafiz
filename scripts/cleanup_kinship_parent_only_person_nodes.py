# ============================================================
# One-off data cleanup: remove Person nodes for a name that is ONLY ever
# a kinship-formula PARENT reference ("X ولد <name>"), never an
# independent party to any case — HANDOFF_TO_TEAMMATE.md's domain-
# confirmed finding for محمد رمضان in fir-1001-26.
#
# ROOT CAUSE (confirmed by reading src/extraction/ner.py, not guessed):
# _KINSHIP_RE matches "<child> ولد/بنت <parent>" and emits BOTH names as
# independent `"person"` NERMentions (ner.py lines ~381-387) — there is
# no father_name-attribute linkage at the NER layer, unlike
# structured_projection.py's own handling of a FIR's formal
# complainant/accused fields, which correctly attaches father_name as a
# property on the child's own Person node (confirmed live: the real
# complainant فیصل node carries father_name='محمد رمضان' as a property,
# via the STRUCTURED path, not a separate node). Every prose occurrence
# of "X ولد محمد رمضان" therefore mints ANOTHER independent "محمد رمضان"
# Person mention purely from the kinship formula — measured live: 176
# separate Person nodes, one per narrative occurrence in a single
# document (psrms_fir_fir-1001-26#narrative_c8bf2613), NONE carrying a
# CNIC, ALL already collapsed into one fully-connected component via
# confirmed SAME_AS edges (175 edges connecting exactly 176 nodes — a
# spanning tree, not a fragmented cluster).
#
# WHY THIS IS A NAME-LEVEL CLEANUP, NOT A CHANGE TO ner.py's REGEX:
# fixing _KINSHIP_RE's parent-emission is a corpus-wide behavior change
# with broader blast radius (every patronymic in every document) that
# has NOT been verified safe against relationship_extraction.py's own
# person-pairing logic — a real fix, but a separate, larger decision.
# This script only acts on ONE domain-confirmed name (HANDOFF_TO_
# TEAMMATE.md, 2026-08-27, cross-checked against the FIR narrative and
# the Muhafiz API record: he is the patronymic of five different
# complainants across the corpus and never a complainant/witness/accused
# himself in any of 73 cases) — same "growing blocklist, not a
# generalizable filter" discipline scripts/cleanup_boundary_bleed_
# person_nodes.py's own header already documents and accepts.
#
# SAFETY GUARD: only ever matches a Person node with this EXACT
# canonical_name AND no cnic — a genuinely independent party actually
# named محمد رمضان would very likely carry his own CNIC from a formal
# identification line; this guard protects that node from ever being
# swept up here, corpus-wide, not just in fir-1001-26.
#
# DETACH DELETE, not a SAME_AS-confirm/collapse — same reasoning as the
# boundary-bleed script: these nodes are not a person at all (they are
# each an over-extracted kinship-formula artifact), so there is nothing
# to canonicalize toward. Removing them also removes their APPEARS_IN,
# BELONGS_TO_CASE, and (now-meaningless, entirely-internal-to-this-
# cluster) SAME_AS edges.
#
# Run: python scripts/cleanup_kinship_parent_only_person_nodes.py --dry-run
#      python scripts/cleanup_kinship_parent_only_person_nodes.py --apply
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

from src.graph import age_client

# Domain-confirmed kinship-parent-only names — see module docstring.
# Corpus-wide by canonical_name match, same precedent as the boundary-
# bleed noise list: this exact string is not an independent party
# anywhere it occurs in this corpus, so a match elsewhere would be the
# same defect, not a different one.
_KINSHIP_PARENT_ONLY_NAMES = [
    "محمد رمضان",
]

_FETCH_QUERY = (
    "MATCH (p:Person) WHERE p.canonical_name IN $names "
    "AND (p.cnic IS NULL OR p.cnic = '') "
    "RETURN p.entity_id AS entity_id, p.canonical_name AS canonical_name, "
    "p.source_doc_id AS source_doc_id"
)


async def _fetch_matching_nodes() -> list[dict]:
    return await age_client.execute_cypher(
        _FETCH_QUERY, params={"names": _KINSHIP_PARENT_ONLY_NAMES},
        columns=["entity_id", "canonical_name", "source_doc_id"],
    )


async def _same_as_edge_count(entity_ids: list[str]) -> int:
    if not entity_ids:
        return 0
    rows = await age_client.execute_cypher(
        "MATCH (a:Person)-[r:SAME_AS]->(b:Person) "
        "WHERE a.entity_id IN $ids OR b.entity_id IN $ids "
        "RETURN count(r) AS c",
        params={"ids": entity_ids}, columns=["c"],
    )
    return rows[0]["c"] if rows else 0


async def main() -> None:
    apply = "--apply" in sys.argv
    dry_run = not apply

    nodes = await _fetch_matching_nodes()
    print(f"Kinship-parent-only Person nodes matching {_KINSHIP_PARENT_ONLY_NAMES!r} (no CNIC): {len(nodes)}")

    if not nodes:
        print("Nothing to clean up.")
        return

    entity_ids = [n["entity_id"] for n in nodes]
    same_as_count = await _same_as_edge_count(entity_ids)
    print(f"SAME_AS edges touching these nodes (will be removed with them): {same_as_count}")

    from collections import Counter
    by_doc = Counter(n["source_doc_id"] for n in nodes)
    print("By source_doc_id:")
    for doc, count in by_doc.most_common(10):
        print(f"  {doc}: {count}")

    if dry_run:
        print(f"\nDRY RUN — no changes made. Re-run with --apply to DETACH DELETE these {len(nodes)} node(s).")
        return

    result = await age_client.execute_cypher(
        "MATCH (p:Person) WHERE p.entity_id IN $ids DETACH DELETE p",
        params={"ids": entity_ids}, columns=["result"],
    )
    print(f"\nDeleted {len(entity_ids)} node(s) and their edges.")


if __name__ == "__main__":
    asyncio.run(main())
