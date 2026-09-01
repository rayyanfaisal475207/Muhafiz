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

# Windows' console/redirected-file stdout defaults to the system codepage
# (cp1252 here), not UTF-8. Vision-fallback OCR output can legitimately
# contain characters cp1252 can't encode (confirmed live: a U+FFFD
# replacement character from Forensics_guidelines.pdf's OCR output crashed
# the whole batch mid-run with UnicodeEncodeError, silently skipping every
# file after the one that triggered it — a bug in this script, not a data
# problem). Reconfigure to UTF-8 with replacement on the rare remaining
# unencodable character, rather than letting one bad line kill the batch.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

KB_DIR = Path(__file__).resolve().parent.parent / "Muhafiz_Knowledge_Base"
WORKER = Path(__file__).resolve().parent / "_ingest_kb_tier1_one_file.py"
PER_FILE_TIMEOUT_SECONDS = 7200  # 2 hours. Docling's layout + table-structure model runs
# CPU-only in this environment and processes every page through that full pipeline
# regardless of whether the page's text is directly extractable (confirmed live, per-page,
# via PyMuPDF: 852 of 863 total pages across all 7 files have a real text layer - OCR
# fallback is not the bottleneck). This was raised twice: 180s (cut every file off before
# real work started) -> 2400s/40min (CrPC, 319 pages, still timed out, ~7000 CPU-seconds
# burned and counting) -> this. Deliberately not "fixed" by changing pdf_loader.py's Docling
# pipeline config (e.g. disabling table-structure detection) - that's shared code used by
# every PDF this platform ingests, evaluated and explicitly deferred as a separate,
# out-of-scope decision (see MUHAFIZ_KNOWLEDGE_BASE_INTEGRATION_PLAN.md execution log). This
# is a one-time ingest; wall-clock cost here is acceptable where it wouldn't be on a hot path.


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


def already_ingested_filenames(metas: list[dict]) -> set[str]:
    """
    Pure: given the vector store's chunk metadata (as returned by
    ChromaVectorStore.get_all_metadata()), return the set of source filenames
    already tagged category="legal_procedural_reference" — i.e. already fully
    ingested by this script. Same idea as reingest_kb_resume.py's own
    already-done check, scoped to this KB's category tag specifically so it
    never skips a file some OTHER ingestion path happened to touch.

    Split out as pure so it's testable without a live Chroma instance — see
    the incident this guards against in
    MUHAFIZ_KNOWLEDGE_BASE_INTEGRATION_PLAN.md's execution log: an orphaned,
    untracked second run of this script completed CrPC and QSO in full while
    a properly-tracked run was independently redoing the same two files from
    scratch, burning real CPU time on already-correct work.
    """
    return {
        m.get("source")
        for m in metas
        if m.get("category") == "legal_procedural_reference" and m.get("source")
    }


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


def safe_print(text: str) -> None:
    """
    print(), but a console-encoding failure on this one line must never take
    down the rest of the batch — belt-and-suspenders alongside the UTF-8
    stdout reconfigure above, in case some other invocation context (a
    different redirect, a CI runner) hits the same class of failure a
    different way.
    """
    try:
        print(text, flush=True)
    except UnicodeEncodeError:
        print(text.encode("ascii", errors="replace").decode("ascii"), flush=True)


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
    force = "--force" in sys.argv

    files = discover_files(KB_DIR)
    if not files:
        print(f"No files found in {KB_DIR} — nothing to ingest.")
        return

    if force:
        print("--force: re-ingesting all files regardless of what's already in the store.")
    else:
        from src.retrieval.vector_store import ChromaVectorStore
        store = ChromaVectorStore.get_instance()
        done = already_ingested_filenames(store.get_all_metadata())
        skipped = [f for f in files if f.name in done]
        files = [f for f in files if f.name not in done]
        if skipped:
            print(f"Skipping {len(skipped)} already-ingested file(s) (pass --force to redo them):")
            for f in skipped:
                print(f"  - {f.name}")
        if not files:
            print("Nothing left to ingest.")
            return

    print(f"Ingesting {len(files)} Tier 1 knowledge-base file(s) from {KB_DIR}:")
    for f in files:
        print(f"  - {f.name}")

    results: list[tuple[str, bool, str]] = []
    for f in files:
        print(f"\n--- {f.name} ---", flush=True)
        ok, output = await ingest_one_file_isolated(f)
        safe_print(output)
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
