# -*- coding: utf-8 -*-
"""
Modules 82/85/86 — read the verbatim text of the chunks that actually reached
the generator, by id, out of the shared Chroma store.

Module 30 recorded an evaluator citing "section 174" that was not in the chunks
it judged, so no claim in this module about what the generator was shown is
allowed to rest on the answer's own "[Document N]". This script is how each
such claim is grounded: ids in, store text out.

READ-ONLY. It opens the shared store with `ChromaVectorStore.get_instance()`
and only ever calls `.get()`. Nothing is written, embedded or re-indexed.

Usage:
    PYTHONPATH=. python scripts/module82_window_probe.py <chunk_id> [chunk_id...]
    PYTHONPATH=. python scripts/module82_window_probe.py --from-runs <runs.json>
"""
from __future__ import annotations

import io
import json
import os
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

from src.retrieval.vector_store import ChromaVectorStore  # noqa: E402


def read(ids: list[str]) -> dict[str, str | None]:
    store = ChromaVectorStore.get_instance()
    found = {r["id"]: r.get("text") for r in store.get_by_ids(ids)}
    return {i: found.get(i) for i in ids}


def main() -> None:
    argv = sys.argv[1:]
    if argv and argv[0] == "--from-runs":
        rows = json.load(open(argv[1], encoding="utf-8"))
        ids: list[str] = []
        for r in rows:
            for a in r.get("attempts", []):
                for c in a.get("chunk_ids", []):
                    if c not in ids and not c.startswith("kb-data-half:"):
                        ids.append(c)
    else:
        ids = argv
    if not ids:
        print("no ids given")
        return
    for cid, text in read(ids).items():
        print("=" * 78)
        print(cid)
        if text is None:
            # An id in a live window that resolves to nothing here is a Module
            # 37 orphan: a `chunk_fulltext` row from the CrPC re-ingest that
            # never reached Chroma. Recorded, not repaired — the store is
            # shared and read-only for this module.
            print("  *** NOT IN CHROMA (Module 37 orphan candidate) ***")
        else:
            print("  " + " ".join(text.split())[:900])


if __name__ == "__main__":
    main()
