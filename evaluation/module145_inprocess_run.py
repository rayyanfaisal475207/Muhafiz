# -*- coding: utf-8 -*-
"""
Module 145 — the live queries end-to-end, in process, with the model that
answered each run recorded.

Reuses Module 92's `run_one()` (`evaluation/module92_inprocess_run.py`),
which drives exactly the two stages `main.py::chat_endpoint()` does —
`route_query()`, then `run_cutover_query()` through the Supervisor — against
the live graph, vector store and model server, without occupying one of the
two machine-wide backend slots (both were taken by other tracks when this
module reached its live step: 127.0.0.1:8079 and :8143).

Two things Module 92's runner did not capture are captured here, per run:

  * every `SEMANTIC-DISPATCH` log line (the layer's only live evidence);
  * every `Falling back to <provider>` line from `src.llm.client`, so the
    model that answered is recorded rather than assumed — "local" when no
    fallback line appears, otherwise the provider named.

    PYTHONPATH=. python -X utf8 evaluation/module145_inprocess_run.py --tag after --runs 3
    PYTHONPATH=. python -X utf8 evaluation/module145_inprocess_run.py --tag before --runs 3 --disable
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import logging
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "evaluation"))

OUT_DIR = ROOT / "docs" / "gold-qa-wave2-results" / "module145_live"
TARGETS = ROOT / "docs" / "gold-qa-wave2-results" / "module145_targets.json"


class _Capture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.lines: list[str] = []

    def emit(self, record):
        msg = record.getMessage()
        if "SEMANTIC-DISPATCH" in msg or "Falling back to" in msg or "XAGG " in msg[:6]:
            self.lines.append(f"{record.name}: {msg}"[:400])


async def main_async(a) -> None:
    from src import config
    if a.disable and hasattr(config, "SEMANTIC_DISPATCH_ENABLED"):
        config.SEMANTIC_DISPATCH_ENABLED = False
    from module92_inprocess_run import run_one
    try:
        from src.pipeline import semantic_dispatch as sd
    except ImportError:  # the pre-module tree (the 'before' arm on origin/main)
        sd = None

    targets = json.load(io.open(TARGETS, encoding="utf-8"))["targets"]
    wanted = [t for t in targets if not a.ids or t["id"] in a.ids.split(",")]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"module145_inprocess_{a.tag}.json"
    results = json.load(io.open(path, encoding="utf-8")) if path.exists() and not a.refresh else {}

    cap = _Capture()
    logging.getLogger().addHandler(cap)
    logging.getLogger("src").setLevel(logging.INFO)

    for t in wanted:
        for n in range(1, a.runs + 1):
            key = f"{t['id']}::run{n}"
            if key in results and not a.refresh:
                continue
            if sd is not None:
                sd.reset_for_tests()  # every run scores afresh; no cross-run cache
            cap.lines.clear()
            print(f"[{a.tag}] {key} ...", flush=True)
            try:
                r = await run_one(t["text"])
            except Exception as exc:  # noqa: BLE001
                r = {"route": None, "route_error": f"{type(exc).__name__}: {exc}",
                     "answer": "", "steps": [], "status": "error", "elapsed_s": 0.0}
            fallbacks = [ln for ln in cap.lines if "Falling back to" in ln]
            r["question"] = t["text"]
            r["expected_kind"] = t["expected_kind"]
            r["semantic_dispatch_lines"] = [ln for ln in cap.lines if "SEMANTIC-DISPATCH" in ln]
            r["xagg_lines"] = [ln for ln in cap.lines if ln.split(": ", 1)[-1].startswith("XAGG ")]
            r["model"] = "local" if not fallbacks else "cloud fallback: " + " | ".join(fallbacks)[:200]
            r["semantic_dispatch_enabled"] = not a.disable
            results[key] = r
            print(f"    route={r['route']} status={r['status']} {r['elapsed_s']}s "
                  f"answer={len(r['answer'])} chars model={r['model'][:40]}", flush=True)
            for ln in r["semantic_dispatch_lines"]:
                print(f"    {ln[:200]}", flush=True)
            with io.open(path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"\nwrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--ids", default="", help="comma-separated target ids (default: all)")
    ap.add_argument("--disable", action="store_true", help="run with the semantic layer off (the 'before' arm)")
    ap.add_argument("--refresh", action="store_true")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:
        pass
    main()
