"""One-off: re-run graph extraction only (not full re-ingest) for every
document that already has a case_id in Postgres, so the fixed extraction
path (phone/vehicle wiring, domain_entities retry) backfills
PhoneNumber/Vehicle nodes into the existing graph.

Two phases, deliberately NOT both concurrent:
1. Load + chunk SERIALLY. docling's native PDF pipeline is not safe for
   concurrent invocation from multiple threads (confirmed live: running 6
   ingest_file() calls concurrently produced repeated `std::bad_alloc`
   crashes in docling.pipeline.standard_pdf_pipeline, silently returning
   empty text for ~90% of documents in a first attempt at this script).
2. Run _run_graph_extraction() (pure LLM + DB calls, no native/OCR code)
   CONCURRENTLY across documents — this is where the real wall-clock cost
   is, and it's safe to parallelize.

Reuses each document's EXISTING doc_id from Postgres (not a freshly
regenerated one) so every write MERGEs into the current Document/Case
nodes rather than risking a mismatch. Does not touch Chroma at all —
embeddings/chunks already there are unaffected; this only adds to the
graph. Not a permanent script.
"""
import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Batch offset/limit — run in small batches, each its own fresh `python`
# process (see the driver loop in this repo's shell notes), not looped
# in-process. Confirmed live: docling's native PDF pipeline accumulates
# memory across repeated calls within one long-running process (this
# machine only has ~3.4GB free RAM) and eventually throws std::bad_alloc
# around document #20-25 regardless of concurrency — a fresh process per
# batch lets the OS reclaim everything between batches instead.
_OFFSET = int(sys.argv[1]) if len(sys.argv) > 1 else 0
_LIMIT = int(sys.argv[2]) if len(sys.argv) > 2 else 1_000_000

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("docling").setLevel(logging.WARNING)

from src import config
from src.ingestion.loader_router import route_and_load
from src.ingestion.text_normalizer import normalize_whitespace, normalize_urdu
from src.ingestion.chunker import chunk_documents
from src.ingestion.service import _run_graph_extraction
from src.database.postgres import AsyncSessionLocal
from sqlalchemy import text as sql_text

_EXTRACT_CONCURRENCY = 8


async def _load_and_chunk(file_path: Path, case_id: str):
    documents = await asyncio.to_thread(route_and_load, file_path)
    if not documents:
        return None, None
    for doc in documents:
        doc.text = normalize_whitespace(normalize_urdu(doc.text))
    chunks = chunk_documents(documents, chunk_size=config.CHUNK_SIZE, chunk_overlap=config.CHUNK_OVERLAP, case_id=case_id)
    return documents, chunks


async def main():
    async with AsyncSessionLocal() as db:
        rows = (await db.execute(sql_text(
            "SELECT DISTINCT ON (filename) filename, case_id, doc_id FROM documents "
            "WHERE case_id IS NOT NULL ORDER BY filename, doc_id"
        ))).fetchall()

    total = len(rows)
    rows = rows[_OFFSET:_OFFSET + _LIMIT]
    print(f"{total} distinct documents with a case_id total; this batch: rows[{_OFFSET}:{_OFFSET + _LIMIT}] = {len(rows)} documents")

    # Phase 1 — serial load+chunk.
    loaded = []
    for i, (filename, case_id, doc_id) in enumerate(rows, 1):
        file_path = config.DOCUMENTS_DIR / filename
        if not file_path.exists():
            print(f"[{i}/{len(rows)}] SKIP (missing on disk): {filename}")
            continue
        try:
            documents, chunks = await _load_and_chunk(file_path, case_id)
            if not documents or not chunks:
                print(f"[{i}/{len(rows)}] LOAD-EMPTY: {filename}")
                continue
            loaded.append((filename, case_id, doc_id, file_path, documents, chunks))
            print(f"[{i}/{len(rows)}] loaded {filename}: {len(chunks)} chunks")
        except Exception as exc:
            print(f"[{i}/{len(rows)}] LOAD-FAILED {filename}: {exc}")

    print(f"\nLoaded {len(loaded)}/{len(rows)}. Running graph extraction, concurrency={_EXTRACT_CONCURRENCY} ...")

    sem = asyncio.Semaphore(_EXTRACT_CONCURRENCY)
    counters = {"ok": 0, "failed": 0}

    async def _extract_one(item):
        filename, case_id, doc_id, file_path, documents, chunks = item
        async with sem:
            try:
                stats = await _run_graph_extraction(file_path, documents, chunks, case_id, doc_id)
                print(f"EXTRACT DONE  {filename}: resolved={stats['entities_resolved']} "
                      f"unresolved={stats['entities_unresolved']} errors={len(stats['errors'])}")
                counters["ok"] += 1
            except Exception as exc:
                print(f"EXTRACT FAILED {filename}: {exc}")
                counters["failed"] += 1

    await asyncio.gather(*[_extract_one(item) for item in loaded])
    print(f"\nDone. ok={counters['ok']} failed={counters['failed']} skipped={len(rows) - len(loaded)}")


if __name__ == "__main__":
    asyncio.run(main())
