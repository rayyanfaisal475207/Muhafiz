# ============================================================
# Phase 0.8 — resume the bulk re-ingest after a hang.
#
# scripts/reingest_kb.py's first run hung on WITNESS-FIR-2026-CYBER-006-03.pdf
# after ~80 conversions in one long-running process — likely cumulative
# native-memory pressure in Docling across many sequential conversions
# (two sibling files crashed with std::bad_alloc/MemoryError right before
# it, both handled cleanly; this one just hung instead of erroring). A
# plain asyncio.wait_for timeout can't fix this: Docling's conversion call
# is synchronous and non-yielding, so it blocks the event loop and a
# cooperative timeout never gets a chance to fire.
#
# This script instead runs each remaining file as its own subprocess
# (_ingest_one_file.py) with a hard, OS-level kill-on-timeout — fixing
# both problems: no memory accumulates across files (fresh process each
# time), and a genuine hang gets killed instead of blocking the batch.
#
# Does NOT reset the collection — the first run's ~58 files are already
# correctly embedded at 1024-dim; this ingests only what's missing.
# ============================================================

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src import config
from src.retrieval.vector_store import ChromaVectorStore

PER_FILE_TIMEOUT_SECONDS = 120
WORKER = Path(__file__).resolve().parent / "_ingest_one_file.py"


async def ingest_one_file_isolated(file_path: Path) -> tuple[bool, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, str(WORKER), str(file_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=PER_FILE_TIMEOUT_SECONDS)
        output = stdout.decode(errors="replace").strip()
        return proc.returncode == 0, output
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return False, f"TIMEOUT after {PER_FILE_TIMEOUT_SECONDS}s (process killed)"


async def main():
    store = ChromaVectorStore.get_instance()
    metas = store.get_all_metadata()
    already_done = {m.get("source") for m in metas if m.get("source")}
    print(f"Already ingested: {len(already_done)} sources ({store.count()} chunks)")

    all_files = sorted(
        f for f in config.DOCUMENTS_DIR.iterdir()
        if f.is_file() and f.name != "README.txt"
    )
    remaining = [f for f in all_files if f.name not in already_done]
    print(f"Remaining to ingest: {len(remaining)} of {len(all_files)} total files")

    succeeded = []
    failed = []

    for f in remaining:
        print(f"\n--- {f.name} ---", flush=True)
        ok, output = await ingest_one_file_isolated(f)
        print(output, flush=True)
        if ok:
            succeeded.append(f.name)
        else:
            failed.append((f.name, output))

    print(f"\n=== Resume run complete ===")
    print(f"Succeeded: {len(succeeded)}")
    print(f"Failed: {len(failed)}")
    for name, err in failed:
        print(f"  - {name}: {err}")
    print(f"Final collection count: {ChromaVectorStore.get_instance().count()}")


if __name__ == "__main__":
    asyncio.run(main())
