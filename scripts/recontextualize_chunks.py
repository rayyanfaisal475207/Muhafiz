"""Give every chunk the identity of the FIR it belongs to, then re-embed.

THE DEFECT

The database knows which FIR a chunk belongs to. The EMBEDDING does not.

Measured across the 712 case chunks in Chroma, 234 of them (33%) contain
no FIR reference anywhere in their own text:

    [fir-1001-26]  "inance 1965 لاگو۔"                 <- starts mid-word
    [fir-118-26]   "رند ولد غلام رند کو موقع پر..."     <- starts mid-name
    [fir-118-26]   "ذا کے بابت مقدمہ ہذا جناب SHO..."   <- starts mid-word

Chunking split the text by length, so only the first chunk of a document
carries the header that names the FIR, the station and the sections.
Every later chunk was embedded as a context-free fragment.

WHY THAT BREAKS RETRIEVAL AND COUNTING BOTH

A vector computed for "inance 1965 لاگو۔" encodes nothing about which
case, station or offence it came from, so:

  * asked about a station, the passage from that station's own FIR does
    not come back, because its text never mentions the station;
  * two chunks from unrelated FIRs look alike, because procedural
    boilerplate is the strongest signal either one carries;
  * a retrieved chunk cannot be understood on its own by a person or a
    model, even though the metadata to explain it existed all along.

WHAT THIS DOES

Prepends each chunk with the identity of its case before embedding:

    FIR 118/26 | تھانہ لطیف آباد، حیدر آباد | CNSA 1997 | 2026-02-12
    ---
    رند ولد غلام رند کو موقع پر گرفتار کر لیا گیا۔...

The header is built from columns that are ALREADY populated — fir_number,
station, category and incident date are all present for every case or
nearly so. Nothing is invented and no model is involved: this is a join
and a re-embed.

The stored chunk text is left alone. Only the text handed to the embedder
carries the header, so provenance and span offsets are untouched.

WHAT IT DOES NOT FIX

`registered_at`, `reported_at` and `description` are empty for all 73
cases, so a question about when an FIR was REGISTERED still cannot be
answered. That date is present in the document text and was never
extracted; recovering it is a separate, deterministic job.

    python scripts/recontextualize_chunks.py --dry-run
    python scripts/recontextualize_chunks.py
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import asyncpg  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv(".env", override=True)

for _s in (sys.stdout, sys.stderr):
    try:
        _s.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

from src import config  # noqa: E402
from src.retrieval.engine import dsn  # noqa: E402
from src.retrieval.vector_store import COLLECTION_NAME  # noqa: E402


HEADERS_SQL = """
SELECT c.case_id,
       c.fir_number,
       c.category_raw,
       to_char(c.incident_at, 'YYYY-MM-DD') AS incident_on,
       p.name_raw AS station
FROM rebuild_cases c
LEFT JOIN places p ON p.place_id = c.station_place_id
"""


def header_for(row) -> str:
    """The line that gives a fragment its identity.

    Only fields that are actually present are included -- a header
    reading "None | None" would be noise in the embedding, and three
    quarters of cases have no status recorded.
    """
    parts = []
    if row["fir_number"]:
        parts.append(f"FIR {row['fir_number']}")
    if row["station"]:
        parts.append(str(row["station"]))
    if row["category_raw"]:
        parts.append(str(row["category_raw"]))
    if row["incident_on"]:
        parts.append(str(row["incident_on"]))
    return " | ".join(parts)


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true",
                    help="show what would change, embed nothing")
    ap.add_argument("--batch", type=int, default=32)
    args = ap.parse_args()

    conn = await asyncpg.connect(dsn())
    headers = {r["case_id"]: header_for(r)
               for r in await conn.fetch(HEADERS_SQL)}
    await conn.close()
    print(f"built headers for {len(headers)} case(s)")

    import chromadb
    client = chromadb.PersistentClient(path=str(config.CHROMA_PERSIST_DIR))
    col = client.get_collection(COLLECTION_NAME)
    got = col.get(include=["metadatas", "documents"])

    todo = []
    for cid, meta, doc in zip(got["ids"], got["metadatas"], got["documents"]):
        meta = meta or {}
        case_id = meta.get("case_id")
        if not case_id or case_id not in headers:
            continue                      # statutes and reference material
        head = headers[case_id]
        if not head or (doc or "").startswith(head):
            continue                      # already carries its identity
        todo.append((cid, meta, head + chr(10) + "---" + chr(10) + (doc or "")))

    print(f"{len(todo)} of {len(got['ids'])} chunk(s) need their FIR identity")
    if not todo:
        return

    for cid, _meta, text in todo[:3]:
        print(f"\n  {cid}")
        print("   " + " ".join(text[:150].split()))

    if args.dry_run:
        print("\ndry run — nothing written")
        return

    from src.retrieval.embedder import embed_texts
    written = 0
    for i in range(0, len(todo), args.batch):
        batch = todo[i:i + args.batch]
        vectors = await embed_texts([t for _, _, t in batch],
                                    task_type="RETRIEVAL_DOCUMENT")
        if len(vectors) != len(batch):
            sys.exit(f"embedder returned {len(vectors)} vectors for "
                     f"{len(batch)} chunks; refusing to write a misaligned "
                     f"index")
        col.update(ids=[c for c, _, _ in batch],
                   embeddings=[list(map(float, v)) for v in vectors],
                   documents=[t for _, _, t in batch],
                   metadatas=[{**m, "contextualized": True}
                              for _, m, _ in batch])
        written += len(batch)
        print(f"  {written}/{len(todo)}", flush=True)

    print(f"\nre-embedded {written} chunk(s) with their FIR identity")


if __name__ == "__main__":
    asyncio.run(main())
