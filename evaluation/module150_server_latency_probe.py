# -*- coding: utf-8 -*-
"""
Module 150 — how does the local model server behave under the concurrency a
Meta-Analysis fan-out produces?

Fires the SAME paraphrase-shaped prompt at `LOCAL_LLM_URL` N times at once,
straight through `src.llm.client._post_local` (the real code path, the real
`LOCAL_LLM_TIMEOUT`), and records every call's start offset, end offset,
duration and outcome. Run at N=1, 2, 4, 8 and the shape of the answer tells
you whether the server runs requests in parallel, serialises them, or does
something in between — and therefore whether the k-th of N concurrent calls
can ever finish inside the per-call timeout.

    PYTHONPATH=. python -X utf8 evaluation/module150_server_latency_probe.py --n 1,2,4,8 --repeats 2 --tag before
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "docs" / "gold-qa-wave2-results" / "module150_live"

# Shaped like the structured-aggregate paraphrase call every XAGG sub-query
# makes (system prompt + a computed aggregate to restate), because that is
# the call G6's eight sub-queries each make. Deliberately not byte-identical
# to any production prompt so a server-side cache cannot short-circuit it.
_SYSTEM = (
    "You are a police-records analyst. Restate the computed aggregate below "
    "in two or three plain English sentences for a supervising officer. Use "
    "only the numbers given; do not invent any figure; do not add caveats."
)
_USER_TEMPLATE = (
    "Computed aggregate (probe {i}):\n"
    "- 73 FIRs in scope, 64 carry an incident datetime, 9 do not.\n"
    "- Districts: Faisalabad 19, Lahore 18, Rawalpindi 10, Islamabad 5, "
    "Chiniot 5, Hyderabad 5, Karachi East 5, Karachi Central 4, Multan 2.\n"
    "- Arrests recorded in 11 of 73 FIRs (1 in 6.6); 14 accused entries "
    "classified arrested, 2 explicitly not arrested.\n"
    "- Weapons: 32 recorded, 30 unlicensed, 2 with no licence status.\n"
    "Restate this for the supervising officer."
)


async def _one(i: int, t0: float) -> dict:
    from src.llm import client as llm_client

    start = time.monotonic() - t0
    try:
        text = await llm_client._post_local(_SYSTEM, _USER_TEMPLATE.format(i=i), 0.0, 1000, "reasoning")
        ok, err, n = True, None, len(text or "")
    except Exception as exc:  # noqa: BLE001
        ok, err, n = False, f"{type(exc).__name__}: {exc!r}", 0
    end = time.monotonic() - t0
    return {"i": i, "start_s": round(start, 2), "end_s": round(end, 2),
            "duration_s": round(end - start, 2), "ok": ok, "error": err, "chars": n}


async def _batch(n: int) -> dict:
    t0 = time.monotonic()
    calls = await asyncio.gather(*[_one(i, t0) for i in range(n)])
    wall = round(time.monotonic() - t0, 2)
    oks = [c for c in calls if c["ok"]]
    return {
        "n": n, "wall_s": wall,
        "ok": len(oks), "failed": n - len(oks),
        "durations_s": [c["duration_s"] for c in calls],
        "completion_order_end_s": sorted(c["end_s"] for c in oks),
        "calls": calls,
    }


async def main_async(a) -> None:
    from src import config
    print(f"LOCAL_LLM_URL={config.LOCAL_LLM_URL} model={config.LOCAL_LLM_MODEL} "
          f"LOCAL_LLM_TIMEOUT={config.LOCAL_LLM_TIMEOUT}", flush=True)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"module150_server_latency_{a.tag}.json"
    results = json.load(io.open(path, encoding="utf-8")) if path.exists() else []
    for n in [int(x) for x in a.n.split(",")]:
        for rep in range(1, a.repeats + 1):
            print(f"N={n} rep {rep} ...", flush=True)
            r = await _batch(n)
            r["rep"] = rep
            r["timestamp"] = time.strftime("%Y-%m-%d %H:%M:%S")
            results.append(r)
            print(f"    wall={r['wall_s']}s ok={r['ok']}/{n} durations={r['durations_s']} "
                  f"ends={r['completion_order_end_s']}", flush=True)
            with io.open(path, "w", encoding="utf-8") as f:
                json.dump(results, f, ensure_ascii=False, indent=1)
            if a.pause:
                await asyncio.sleep(a.pause)
    print(f"wrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", default="1,2,4,8")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--pause", type=float, default=5.0, help="seconds between batches")
    ap.add_argument("--tag", required=True)
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT / ".env")
    except Exception:  # noqa: BLE001
        pass
    main()
