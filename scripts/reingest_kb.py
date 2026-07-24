# ============================================================
# Phase 0.8 — Full Chroma wipe + re-ingest under the new embedding model
#
# The existing collection was embedded at 384 dimensions (chromadb's
# DefaultEmbeddingFunction fallback). Phase 0.4 switched the default
# EMBEDDING_PROVIDER to "e5" (multilingual-e5-large-instruct, 1024
# dimensions). Chroma stores one fixed dimension per collection, so the
# old and new vectors cannot coexist — this script drops the collection
# and re-ingests every file in config.DOCUMENTS_DIR from scratch, rather
# than appending, which would either hard-error on write or (if somehow
# accepted) leave mixed-dimension vectors that silently corrupt search.
#
# Run manually: python scripts/reingest_kb.py
# ============================================================

import asyncio
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)

from src import config
from src.retrieval.vector_store import reset_collection, ChromaVectorStore
from src.ingestion.service import ingest_directory


async def main():
    store = ChromaVectorStore.get_instance()
    before_count = store.count()
    print(f"\nExisting collection: {before_count} chunks (to be dropped)")

    print("Dropping and recreating the Chroma collection...")
    reset_collection()
    store = ChromaVectorStore.get_instance()
    after_reset_count = store.count()
    print(f"Collection after reset: {after_reset_count} chunks (expected 0)")
    if after_reset_count != 0:
        print("ERROR: collection was not empty after reset — aborting re-ingest.")
        return

    print(f"\nRe-ingesting all files from {config.DOCUMENTS_DIR} ...")
    result = await ingest_directory(is_global=True)
    print(f"\nIngestion result: {result}")

    final_count = ChromaVectorStore.get_instance().count()
    print(f"Final collection count: {final_count} chunks")


if __name__ == "__main__":
    asyncio.run(main())
