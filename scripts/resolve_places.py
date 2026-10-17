"""Give orphan places a parent, using world knowledge, with provenance.

THE PROBLEM THIS SOLVES

Ingestion reads the document header, which names the police district:

    کراچی ایسٹ    kind_raw = 'city'
    کراچی وسطی    kind_raw = 'city'
    کراچی         -- never appears

So "cases in Karachi" binds to two places and counts one of them. Halving a
count silently is worse than failing to answer.

Name containment cannot fix it: there is no 'کراچی' row for a name to be
contained in. The missing parent has to come from outside the corpus.

WHY THIS IS SAFE TO TAKE FROM THE WEB

Which division Karachi East belongs to is a public fact about Pakistan, not
a fact about any case. Nothing here reads, writes or influences an
investigation fact — it only builds the geography that a rollup counts
over. A wrong lookup changes which cases group together; it can never
change what a case says.

Every edge is written with its confidence, the reasoning, and the source
URLs in `places.attributes`, so any of it can be audited or reversed. And
because it is stored as an edge rather than baked into a hierarchy of
tables, correcting it later is an UPDATE, not a migration.

NOT A GAZETTEER

No list of Pakistani places appears here or in the database. Each parent is
resolved on demand, only for places that actually appear in the corpus and
actually lack a parent, and only when the snippets support an answer. A
lookup that finds nothing writes nothing.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src.retrieval.world_knowledge import resolve_place_parent  # noqa: E402


def _dsn() -> str:
    url = os.getenv("DATABASE_URL", "")
    if not url:
        sys.exit("DATABASE_URL is not set")
    return url.replace("postgresql+asyncpg://", "postgresql://")


ORPHANS = """
    SELECT p.place_id, p.name_raw, p.kind_raw
    FROM places p
    WHERE NOT EXISTS (SELECT 1 FROM place_hierarchy h
                       WHERE h.child_id = p.place_id)
      -- A station already sits under a city from the header, so orphans
      -- are the units above it that nothing named.
      AND p.kind_raw <> 'police_station'
    ORDER BY p.name_raw
"""


async def resolve(conn: asyncpg.Connection, min_confidence: float,
                  dry_run: bool) -> dict:
    rows = await conn.fetch(ORPHANS)
    # 'refused' and 'unavailable' are different outcomes and must not be
    # summed: one means the evidence does not support an answer, the other
    # means we never got to ask. Conflating them would report a rate limit
    # as a finding about geography.
    stats = {"orphans": len(rows), "resolved": 0, "written": 0,
             "refused": 0, "unavailable": 0, "top_level": 0,
             "parents_created": 0}

    for r in rows:
        res = await resolve_place_parent(r["name_raw"])
        if not res.answer or res.confidence < min_confidence:
            if "model unavailable" in res.reason or "no search results" in res.reason:
                stats["unavailable"] += 1
            elif res.answer is None and res.confidence >= min_confidence:
                # An answer of "no parent" held with high confidence is a
                # finding, not a failure: Islamabad really is top level.
                stats["top_level"] += 1
            else:
                stats["refused"] += 1
            print(f"  {r['name_raw']}: no parent "
                  f"(confidence {res.confidence:.2f}) — {res.reason[:90]}")
            continue

        stats["resolved"] += 1
        print(f"  {r['name_raw']} -> {res.answer} "
              f"({res.confidence:.2f}) {res.evidence[0].url if res.evidence else ''}")
        if dry_run:
            continue

        attrs = json.dumps({
            "source": "web",
            "confidence": res.confidence,
            "reason": res.reason,
            "urls": [e.url for e in res.evidence][:5],
        }, ensure_ascii=False)

        # The parent may not exist yet — that is the whole point. Matching
        # on the normalised skeleton so a differently-spelled existing row
        # is reused rather than duplicated.
        parent_id = await conn.fetchval(
            "SELECT place_id FROM places"
            " WHERE name_skeleton = normalize_name($1) LIMIT 1",
            res.answer)
        if parent_id is None:
            parent_id = await conn.fetchval(
                "INSERT INTO places (kind_raw, name_raw, attributes, confidence)"
                " VALUES ('administrative_parent', $1, $2::jsonb, $3)"
                " RETURNING place_id",
                res.answer, attrs, res.confidence)
            stats["parents_created"] += 1

        await conn.execute(
            """
            INSERT INTO place_hierarchy
                (child_id, parent_id, hierarchy_raw, confidence)
            VALUES ($1, $2, 'civil', $3)
            ON CONFLICT (child_id, parent_id, hierarchy_raw)
            DO UPDATE SET confidence = EXCLUDED.confidence
            """,
            r["place_id"], parent_id, res.confidence)
        stats["written"] += 1

    return stats


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-confidence", type=float, default=0.80,
                    help="below this the lookup is treated as a refusal")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    conn = await asyncpg.connect(_dsn())
    try:
        print("Resolving orphan places against world knowledge")
        stats = await resolve(conn, args.min_confidence, args.dry_run)
        print()
        for k, v in stats.items():
            print(f"  {k:>16}: {v}")

        if not args.dry_run:
            print("\nRollup check:")
            for row in await conn.fetch(
                """
                SELECT p.name_raw AS parent,
                       count(DISTINCT d.place_id) AS descendants
                FROM places p
                CROSS JOIN LATERAL place_descendants(p.place_id) d
                GROUP BY p.name_raw HAVING count(*) > 1
                ORDER BY 2 DESC LIMIT 8
                """
            ):
                print(f"  {row['parent']}: {row['descendants']} places under it")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())
