# ============================================================
# Ingest the Tier 1 legal/procedural knowledge base
# (Muhafiz_Knowledge_Base/) into the shared/global RAG corpus.
#
# See MUHAFIZ_KNOWLEDGE_BASE_INTEGRATION_PLAN.md for the full rationale.
# Deliberately does NOT touch scripts/reingest_kb.py or reset_collection() —
# this is a small, additive ingest of 7 known files, not a full-collection
# rebuild.
#
# Each file runs in its own subprocess (_ingest_kb_tier1_one_file.py) with a
# hard per-file timeout, same pattern as scripts/reingest_kb_resume.py — a
# stuck Docling conversion can hang the event loop non-cooperatively, so a
# plain asyncio timeout inside one process can't recover from it; an OS-level
# process kill can.
#
# Re-running this script is safe: ingest_file()/Document._generate_id() key
# each chunk on scope + source path + a hash of its text, and storage is an
# upsert — re-ingesting the same file replaces its chunks rather than
# duplicating them (see plan §2, point 6).
#
# Run manually: python scripts/ingest_knowledge_base_tier1.py
# ============================================================

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

KB_DIR = Path(__file__).resolve().parent.parent / "Muhafiz_Knowledge_Base"
WORKER = Path(__file__).resolve().parent / "_ingest_kb_tier1_one_file.py"
PER_FILE_TIMEOUT_SECONDS = 180  # CrPC is the largest source file (~2.6MB); generous margin.


def discover_files(kb_dir: Path) -> list[Path]:
    """
    Pure: every file directly under `kb_dir` that isn't the README, sorted
    for deterministic, reproducible run order. Split out from main() so it's
    unit-testable without touching the filesystem's async/subprocess side.
    """
    if not kb_dir.exists():
        return []
    return sorted(
        f for f in kb_dir.iterdir()
        if f.is_file() and f.name.lower() != "readme.md"
    )


def summarize(results: list[tuple[str, bool, str]]) -> dict:
    """
    Pure: results is a list of (filename, ok, output). Returns the same
    succeeded/failed split scripts/reingest_kb_resume.py prints, as a plain
    dict so it's assertable in a test without capturing stdout.
    """
    succeeded = [name for name, ok, _ in results if ok]
    failed = [(name, output) for name, ok, output in results if not ok]
    return {
        "total": len(results),
        "succeeded": succeeded,
        "failed": failed,
    }


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
    files = discover_files(KB_DIR)
    if not files:
        print(f"No files found in {KB_DIR} — nothing to ingest.")
        return

    print(f"Ingesting {len(files)} Tier 1 knowledge-base file(s) from {KB_DIR}:")
    for f in files:
        print(f"  - {f.name}")

    results: list[tuple[str, bool, str]] = []
    for f in files:
        print(f"\n--- {f.name} ---", flush=True)
        ok, output = await ingest_one_file_isolated(f)
        print(output, flush=True)
        results.append((f.name, ok, output))

    summary = summarize(results)
    print("\n=== Tier 1 knowledge base ingestion complete ===")
    print(f"Total: {summary['total']}")
    print(f"Succeeded: {len(summary['succeeded'])}")
    print(f"Failed: {len(summary['failed'])}")
    for name, err in summary["failed"]:
        print(f"  - {name}: {err}")

    if summary["failed"]:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
