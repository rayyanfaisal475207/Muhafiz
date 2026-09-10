# -*- coding: utf-8 -*-
"""
Module 92 — run questions end-to-end IN PROCESS, without occupying a backend.

WHY IN PROCESS. Module 92's brief caps live backends at 2 machine-wide and
notes other agents are using them; when this module reached its live step,
three Muhafiz backends were already listening (127.0.0.1:8001, :8011, :8089)
and none of them was running this branch's code. Starting a fourth to verify
a router change would have been the wrong trade. This runner drives the same
code path `main.py::chat_endpoint()` does — `route_query()` for the cutover
classification, then `run_cutover_query()` through the Supervisor, or
`process_query()` when the route is not in HARNESS_CUTOVER_ROUTES — against
the same graph, the same vector store and the same model server.

It is NOT a full substitute for the HTTP path: no auth, no SSE transport, no
session persistence beyond what the gateway does itself. What it does
reproduce is everything downstream of the router, which is what a routing
change needs verified.

    PYTHONPATH=. python -X utf8 evaluation/module92_inprocess_run.py \
        --questions CR2,CR3,G6,KB3,KB9 --tag after
"""
from __future__ import annotations

import argparse
import asyncio
import io
import json
import os
import sys
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

OUT_DIR = ROOT / "docs" / "gold-qa-wave2-results" / "module92_live"
PARA_PATH = ROOT / "evaluation" / "gold32_paraphrases.json"
GOLD_PATH = ROOT / "evaluation" / "Gold_QA_Dataset_Final32_With_Answers.json"

# The same account `evaluation/gold32_run.py` logs in as (EVAL_ADMIN_EMAIL
# defaults to admin@example.com), resolved to its real id — the gateway keys
# sessions on a UUID with a foreign key to `users`, so a synthetic id fails
# before the pipeline is ever reached, and the role must be a real RBAC value
# or `run_cutover_query()` refuses the request by design.
USER_ID = os.environ.get("MODULE92_USER_ID", "81f347d0-7634-40c2-86a0-2fa42469604a")
USER_ROLE = os.environ.get("MODULE92_USER_ROLE", "platform-admin")


def _load(p: Path):
    return json.load(io.open(p, encoding="utf-8"))


def build_items(qids: list[str], include_gold: bool, include_para: bool) -> list[dict]:
    gold = {g["id"]: g for g in _load(GOLD_PATH)}
    out = []
    for qid in qids:
        if include_gold:
            out.append({"id": qid, "variant": "gold",
                        "language": gold[qid]["language"], "text": gold[qid]["question"]})
    if include_para:
        for p in _load(PARA_PATH)["paraphrases"]:
            if p["gold_id"] in qids:
                out.append({"id": p["gold_id"], "variant": "para",
                            "language": p["language"], "text": p["text"]})
    return out


async def run_one(text: str) -> dict:
    """One query through the same two stages main.py::chat_endpoint() uses."""
    from src import config
    from src.data_gateway import get_gateway
    from src.pipeline.router import route_query
    from src.pipeline.harness.cutover import run_cutover_query
    from src.pipeline.orchestrator import process_query

    t0 = time.monotonic()
    gateway = await get_gateway()
    session_id = str(uuid.uuid4())

    route_info, route_err = None, None
    try:
        route_info = await route_query(text)
    except Exception as exc:
        route_err = f"{type(exc).__name__}: {exc}"

    route = str((route_info or {}).get("route") or "").upper()
    out_fmt = str((route_info or {}).get("output_format") or "chat").lower()
    use_cutover = bool(route and route in config.HARNESS_CUTOVER_ROUTES and out_fmt == "chat")

    await gateway.create_session(session_id, USER_ID, "module92", None, None)

    if use_cutover:
        stream = run_cutover_query(
            session_id=session_id, user_message=text, project_id=None, case_id=None,
            user_id=USER_ID, user_role=USER_ROLE, preferred_language=None, gateway=gateway,
        )
    else:
        stream = process_query(
            session_id, text, project_id=None, case_id=None, user_profile=None,
            user_id=USER_ID, user_role=USER_ROLE, enable_web_search=False,
            precomputed_route=route_info,
        )

    answer, steps, status = [], [], None
    async for ev in stream:
        step = ev.get("step")
        if step == "response" and ev.get("detail"):
            answer.append(str(ev["detail"]))
        else:
            steps.append(f"{step}/{ev.get('status')}: {str(ev.get('detail'))[:160]}")
        if ev.get("status") in ("error", "ok"):
            status = ev.get("status")

    return {
        "route": route or None,
        "route_error": route_err,
        "secondary_methods": (route_info or {}).get("secondary_methods"),
        "confidence": (route_info or {}).get("confidence"),
        "reason": (route_info or {}).get("reason"),
        "used_cutover": use_cutover,
        "answer": "".join(answer).strip(),
        "steps": steps,
        "status": status,
        "elapsed_s": round(time.monotonic() - t0, 1),
    }


async def main_async(a) -> None:
    qids = [q.strip() for q in a.questions.split(",") if q.strip()]
    items = build_items(qids, include_gold=not a.para_only, include_para=not a.gold_only)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / f"module92_inprocess_{a.tag}.json"
    results = _load(path) if path.exists() and not a.refresh else {}

    for n, it in enumerate(items, 1):
        key = f"{it['id']}::{it['variant']}::{it['language']}"
        if key in results and not a.refresh:
            continue
        print(f"[{a.tag}] {n}/{len(items)} {key} ...", flush=True)
        try:
            r = await run_one(it["text"])
        except Exception as exc:
            r = {"route": None, "route_error": f"{type(exc).__name__}: {exc}",
                 "answer": "", "steps": [], "status": "error", "elapsed_s": 0.0}
        r["question"] = it["text"]
        results[key] = r
        print(f"    route={r['route']} status={r['status']} {r['elapsed_s']}s "
              f"answer={len(r['answer'])} chars", flush=True)
        with io.open(path, "w", encoding="utf-8") as f:
            json.dump(results, f, ensure_ascii=False, indent=1, sort_keys=True)
    print(f"\nwrote {path}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--questions", required=True, help="comma-separated gold ids")
    ap.add_argument("--tag", required=True, help="before | after | anything")
    ap.add_argument("--gold-only", action="store_true")
    ap.add_argument("--para-only", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    asyncio.run(main_async(ap.parse_args()))


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv
        load_dotenv(ROOT.parent / "Evidence Intelligence Platform" / ".env")
    except Exception:
        pass
    main()
